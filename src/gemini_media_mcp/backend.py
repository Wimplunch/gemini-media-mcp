"""Thin wrapper around the Gemini Interactions API with error mapping and retries.

The rest of the package talks to the ``MediaBackend`` protocol, so tests can swap in a fake
and never touch the network.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from google import genai
from google.genai import types as genai_types
from google.genai._gaos.utils import RetryConfig  # private module; the SDK version is pinned
from google.genai.interactions import Interaction

from .files import InputImage

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RETRIES = 2
RETRY_BASE_DELAY_S = 1.0
TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
PENDING_STATUS = frozenset({"queued", "in_progress"})
_SAFETY_WORDS = ("safety", "blocked", "prohibited", "policy", "harm", "responsible ai")


class GeminiError(Exception):
    """Base class: the message is safe to show to the user (key redacted)."""


class AuthError(GeminiError):
    pass


class QuotaError(GeminiError):
    pass


class SafetyBlockedError(GeminiError):
    pass


class InvalidRequestError(GeminiError):
    pass


class GeminiTimeoutError(GeminiError):
    pass


class TransientError(GeminiError):
    pass


class UpstreamError(GeminiError):
    pass


@dataclass(frozen=True)
class GeneratedMedia:
    data: bytes
    mime_type: str
    text: str | None = None


@dataclass(frozen=True)
class VideoJobState:
    status: str
    video: GeneratedMedia | None = None
    error: str | None = None


class MediaBackend(Protocol):
    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[InputImage],
        aspect_ratio: str | None,
        image_size: str,
    ) -> GeneratedMedia: ...

    async def start_video(
        self,
        *,
        model: str,
        prompt: str,
        image: InputImage | None,
        aspect_ratio: str,
        resolution: str,
    ) -> str: ...

    async def get_video(self, job_id: str) -> VideoJobState: ...


class Redactor:
    """Removes the API key from any text before it leaves this module."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __call__(self, text: str, limit: int = 400) -> str:
        if self._secret:
            text = text.replace(self._secret, "***")
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_sdk_timeout(exc: BaseException) -> bool:
    # The Interactions client raises its own APITimeoutError instead of httpx's.
    return any(cls.__name__ == "APITimeoutError" for cls in type(exc).__mro__)


def map_error(exc: BaseException, redact: Redactor) -> GeminiError:
    """Translate SDK / HTTP exceptions into user-facing GeminiError subclasses."""
    if isinstance(exc, GeminiError):
        return exc
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return TransientError("Could not reach the Gemini API (network problem).")
    if isinstance(exc, httpx.TimeoutException) or _is_sdk_timeout(exc):
        return GeminiTimeoutError(
            "The Gemini API did not answer in time. "
            "Try again, or raise GEMINI_MEDIA_TIMEOUT_SECONDS."
        )
    message = redact(str(exc) or exc.__class__.__name__)
    code = _status_code(exc)
    if code in (401, 403):
        return AuthError(
            "The Gemini API rejected the API key (invalid key, or no access to this model). "
            "Check GEMINI_API_KEY and that billing is enabled for the project."
        )
    if code == 429:
        return QuotaError(f"Quota or rate limit reached at the Gemini API: {message}")
    if code in TRANSIENT_STATUS:
        return TransientError(f"Gemini API temporarily unavailable ({code}).")
    if code is not None and 400 <= code < 500:
        if any(word in message.lower() for word in _SAFETY_WORDS):
            return SafetyBlockedError(f"Blocked by Google's safety filters: {message}")
        if code == 404:
            return InvalidRequestError(f"Model or job not found ({code}): {message}")
        return InvalidRequestError(f"The Gemini API rejected the request ({code}): {message}")
    return UpstreamError(f"Unexpected error from the Gemini API: {message}")


async def with_retries(
    call: Callable[[], Awaitable[T]],
    redact: Redactor,
    *,
    retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_S,
) -> T:
    """Run call, retrying only transient failures (5xx, connection errors), at most `retries` times.

    Read timeouts are *not* retried: the request may already be running (and billed).
    """
    attempt = 0
    while True:
        try:
            return await call()
        except Exception as exc:
            error = map_error(exc, redact)
            if not isinstance(error, TransientError) or attempt >= retries:
                raise error from None
            attempt += 1
            logger.info("Transient Gemini error, retry %d/%d", attempt, retries)
            await asyncio.sleep(base_delay * 2 ** (attempt - 1))


def _decode(data: str | bytes | None) -> bytes:
    if not data:
        return b""
    return base64.b64decode(data, validate=False)


def _explain_missing_output(interaction: Interaction, what: str, redact: Redactor) -> GeminiError:
    """Build a helpful error when an interaction returned no media."""
    parts = [e.message or e.code or "" for e in (interaction.errors or [])]
    if interaction.output_text:
        parts.append(interaction.output_text)
    detail = redact(" | ".join(p for p in parts if p)) or "no details given"
    status = str(interaction.status)
    if status == "budget_exceeded":
        return QuotaError(f"Gemini API budget exceeded: {detail}")
    if any(word in detail.lower() for word in _SAFETY_WORDS):
        return SafetyBlockedError(
            f"No {what} was generated; Google's safety filters blocked it: {detail}"
        )
    return UpstreamError(f"No {what} was returned (status: {status}). Model said: {detail}")


def _image_part(image: InputImage) -> dict[str, Any]:
    return {
        "type": "image",
        "data": base64.b64encode(image.data).decode("ascii"),
        "mime_type": image.mime_type,
    }


def _client_without_sdk_retries(api_key: str) -> genai.Client:
    """Build a client whose own retry layers are off, so with_retries is the only one.

    The SDK retries 408/409/429/5xx and read timeouts on its own and sleeps for whatever
    Retry-After says, without a cap: a quota 429 can stall a tool call for hours, and a
    retried read timeout can bill twice. HttpRetryOptions cannot express "no retries"
    (attempts is clamped to >= 1, which still means one retry for the Interactions
    client), so the Interactions client's RetryConfig is replaced directly. The SDK
    version is pinned; tests/test_backend_http.py fails if this stops working.
    """
    client = genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(attempts=1)
        ),
    )
    client.aio.interactions.sdk_configuration.retry_config = RetryConfig("none", None, False)
    return client


class GeminiBackend:
    """MediaBackend implementation using google-genai's async Interactions API."""

    def __init__(self, api_key: str, timeout_s: float, client: Any | None = None) -> None:
        if not api_key:
            raise AuthError("GEMINI_API_KEY is not set.")
        self._client = client if client is not None else _client_without_sdk_retries(api_key)
        self._timeout = timeout_s
        self._redact = Redactor(api_key)

    async def _create(self, **kwargs: Any) -> Interaction:
        async def call() -> Interaction:
            result = await self._client.aio.interactions.create(timeout=self._timeout, **kwargs)
            if not isinstance(result, Interaction):
                raise UpstreamError("Unexpected streaming response from the Gemini API")
            return result

        return await with_retries(call, self._redact)

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[InputImage],
        aspect_ratio: str | None,
        image_size: str,
    ) -> GeneratedMedia:
        response_format: dict[str, Any] = {"type": "image", "image_size": image_size}
        if aspect_ratio:
            response_format["aspect_ratio"] = aspect_ratio
        content: str | list[dict[str, Any]] = prompt
        if images:
            content = [{"type": "text", "text": prompt}, *(_image_part(i) for i in images)]
        interaction = await self._create(
            model=model, input=content, response_format=response_format, store=False
        )
        image = interaction.output_image
        data = _decode(image.data) if image else b""
        if interaction.status != "completed" or not data:
            raise _explain_missing_output(interaction, "image", self._redact)
        assert image is not None  # noqa: S101 - narrowed by the check above
        return GeneratedMedia(
            data=data,
            mime_type=str(image.mime_type or "image/png"),
            text=interaction.output_text or None,
        )

    async def start_video(
        self,
        *,
        model: str,
        prompt: str,
        image: InputImage | None,
        aspect_ratio: str,
        resolution: str,
    ) -> str:
        content: str | list[dict[str, Any]] = prompt
        if image is not None:
            content = [_image_part(image), {"type": "text", "text": prompt}]
        interaction = await self._create(
            model=model,
            input=content,
            response_format={
                "type": "video",
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            },
            background=True,
            store=True,  # required for background runs and for fetching the result later
        )
        if not interaction.id:
            raise UpstreamError("The Gemini API did not return a job ID for the video.")
        return interaction.id

    async def get_video(self, job_id: str) -> VideoJobState:
        async def call() -> Interaction:
            result = await self._client.aio.interactions.get(job_id, timeout=self._timeout)
            if not isinstance(result, Interaction):
                raise UpstreamError("Unexpected streaming response from the Gemini API")
            return result

        interaction = await with_retries(call, self._redact)
        status = str(interaction.status)
        if status in PENDING_STATUS:
            return VideoJobState(status="in_progress")
        if status != "completed":
            error = _explain_missing_output(interaction, "video", self._redact)
            return VideoJobState(status="failed", error=str(error))

        video = interaction.output_video
        data = _decode(video.data) if video else b""
        if not data and video is not None and video.uri:
            data = await self._download(video.uri)
        if not data:
            error = _explain_missing_output(interaction, "video", self._redact)
            return VideoJobState(status="failed", error=str(error))
        mime = str(video.mime_type) if video and video.mime_type else "video/mp4"
        return VideoJobState(status="completed", video=GeneratedMedia(data=data, mime_type=mime))

    async def _download(self, uri: str) -> bytes:
        host = urlparse(uri).hostname or ""
        if not (host == "googleapis.com" or host.endswith(".googleapis.com")):
            raise UpstreamError("Refusing to download a video from a non-Google host.")

        async def call() -> bytes:
            data = await self._client.aio.files.download(file=uri)
            return bytes(data or b"")

        return await with_retries(call, self._redact)
