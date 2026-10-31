"""Optional OpenRouter backend for the image tools (opt-in via GEMINI_MEDIA_PROVIDER=openrouter).

Uses OpenRouter's Image API (``POST /api/v1/images``) with plain ``httpx``. Requests are
pinned to Google's own providers (AI Studio, Vertex), so the same Google models produce
the image; prompts and reference images pass through OpenRouter on the way. Video is not
offered by OpenRouter, so the video methods raise a clear error.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

import httpx

from .backend import (
    _SAFETY_WORDS,
    AuthError,
    GeminiError,
    GeminiTimeoutError,
    GeneratedMedia,
    InvalidRequestError,
    QuotaError,
    Redactor,
    SafetyBlockedError,
    TransientError,
    UpstreamError,
    VideoJobState,
    with_retries,
)
from .files import InputImage, sniff_image_type

API_HOST = "openrouter.ai"
API_BASE = f"https://{API_HOST}/api/v1"
IMAGES_PATH = "/images"
# Only Google-operated providers may serve our requests; AI Studio first because its prices
# match the bundled price list (some Vertex regions are dearer).
GOOGLE_PROVIDERS = ("google-ai-studio", "google-vertex")
# 524/408: the request may still be running (and billed) upstream, so these are not retried.
TRANSIENT_STATUS = frozenset({500, 502, 503, 504, 529})
TIMEOUT_STATUS = frozenset({408, 524})
OUTPUT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
VIDEO_UNAVAILABLE = (
    "Video is only available with the Google provider. OpenRouter offers no video model; "
    "set GEMINI_MEDIA_PROVIDER=google (with GEMINI_API_KEY) to use start_video."
)


def openrouter_model_id(model: str) -> str:
    """Map a catalogue ID such as 'gemini-3-pro-image' to OpenRouter's 'google/<id>'."""
    return model if "/" in model else f"google/{model}"


def _data_url(image: InputImage) -> str:
    return f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"


def _error_details(body: Any) -> tuple[int | None, str, dict[str, Any]]:
    """Pull (code, message, metadata) out of OpenRouter's ``{"error": {...}}`` envelope."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, "", {}
    code = error.get("code")
    metadata = error.get("metadata")
    return (
        code if isinstance(code, int) else None,
        str(error.get("message") or ""),
        metadata if isinstance(metadata, dict) else {},
    )


def map_openrouter_error(
    status: int, message: str, metadata: dict[str, Any], redact: Redactor
) -> GeminiError:
    """Translate an OpenRouter error status into the user-facing GeminiError subclasses."""
    detail = redact(message) or "no details given"
    reasons = metadata.get("reasons")
    if reasons:
        detail = redact(f"{detail} (reasons: {', '.join(str(r) for r in reasons)})")
    flagged = bool(reasons) or any(word in detail.lower() for word in _SAFETY_WORDS)
    if status == 401:
        return AuthError(
            "OpenRouter rejected the API key (invalid or disabled key). Check OPENROUTER_API_KEY."
        )
    if status == 402:
        return QuotaError(f"OpenRouter: not enough credits for this request: {detail}")
    if status == 403:
        if flagged:
            return SafetyBlockedError(
                f"Blocked by OpenRouter moderation or a safety filter: {detail}"
            )
        return AuthError(
            "OpenRouter refused the request (the key lacks permission for this model, "
            f"or an account guardrail blocked it): {detail}"
        )
    if status == 429:
        return QuotaError(f"Rate limit reached at OpenRouter: {detail}")
    if status in TIMEOUT_STATUS:
        return GeminiTimeoutError(
            f"OpenRouter or the provider did not answer in time ({status}). The image may still "
            "be billed; check your OpenRouter activity before trying again."
        )
    if status in TRANSIENT_STATUS:
        return TransientError(f"OpenRouter or the provider is temporarily unavailable ({status}).")
    if 400 <= status < 500:
        if flagged:
            return SafetyBlockedError(f"Blocked by Google's safety filters: {detail}")
        if status == 404:
            return InvalidRequestError(f"Model not found at OpenRouter ({status}): {detail}")
        return InvalidRequestError(f"OpenRouter rejected the request ({status}): {detail}")
    return UpstreamError(f"Unexpected error from OpenRouter ({status}): {detail}")


def _only_openrouter(request: httpx.Request) -> None:
    """Request hook: never send anything (least of all the key) to another host."""
    if request.url.scheme != "https" or request.url.host != API_HOST:
        raise UpstreamError(
            f"Refusing to contact {request.url.host!r}; only {API_HOST} is allowed."
        )


class OpenRouterBackend:
    """MediaBackend implementation for images via OpenRouter's Image API."""

    def __init__(
        self,
        api_key: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise AuthError("OPENROUTER_API_KEY is not set (GEMINI_MEDIA_PROVIDER=openrouter).")
        self._api_key = api_key
        self._timeout = timeout_s
        self._transport = transport
        self._redact = Redactor(api_key)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
            event_hooks={"request": [self._check_host]},
        )

    @staticmethod
    async def _check_host(request: httpx.Request) -> None:
        _only_openrouter(request)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            try:
                async with self._client() as client:
                    response = await client.post(
                        path,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise TransientError("Could not reach OpenRouter (network problem).") from None
            except httpx.TimeoutException:
                raise GeminiTimeoutError(
                    "OpenRouter did not answer in time. "
                    "Try again, or raise GEMINI_MEDIA_TIMEOUT_SECONDS."
                ) from None
            except httpx.HTTPError as exc:
                detail = self._redact(str(exc) or exc.__class__.__name__)
                raise UpstreamError(f"HTTP error talking to OpenRouter: {detail}") from None
            return self._parse(response)

        return await with_retries(call, self._redact)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        code, message, metadata = _error_details(body)
        if response.status_code >= 400 or code is not None:
            # Errors during generation can arrive with HTTP 200 and an error body.
            status = response.status_code if response.status_code >= 400 else (code or 502)
            raise map_openrouter_error(
                status, message or response.reason_phrase, metadata, self._redact
            )
        if not isinstance(body, dict):
            raise UpstreamError(f"OpenRouter returned no JSON (HTTP {response.status_code}).")
        return body

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[InputImage],
        aspect_ratio: str | None,
        image_size: str,
    ) -> GeneratedMedia:
        payload: dict[str, Any] = {
            "model": openrouter_model_id(model),
            "prompt": prompt,
            "n": 1,
            "resolution": image_size,
            "provider": {"only": list(GOOGLE_PROVIDERS), "order": list(GOOGLE_PROVIDERS)},
        }
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if images:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_url(i)}} for i in images
            ]
        body = await self._post(IMAGES_PATH, payload)
        items = body.get("data")
        first = items[0] if isinstance(items, list) and items else None
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise UpstreamError(
                "No image was returned by OpenRouter (possibly filtered by the provider)."
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise UpstreamError("OpenRouter returned invalid base64 image data.") from None
        # Trust the bytes, not the label: the file extension is derived from this.
        mime = sniff_image_type(data[:32])
        if mime not in OUTPUT_TYPES:
            raise UpstreamError("OpenRouter returned data that is not a PNG, JPEG or WebP image.")
        return GeneratedMedia(data=data, mime_type=mime)

    async def start_video(
        self,
        *,
        model: str,
        prompt: str,
        image: InputImage | None,
        aspect_ratio: str,
        resolution: str,
    ) -> str:
        raise InvalidRequestError(VIDEO_UNAVAILABLE)

    async def get_video(self, job_id: str) -> VideoJobState:
        raise InvalidRequestError(VIDEO_UNAVAILABLE)
