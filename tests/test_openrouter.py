from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from gemini_media_mcp.backend import (
    AuthError,
    GeminiTimeoutError,
    InvalidRequestError,
    QuotaError,
    Redactor,
    SafetyBlockedError,
    TransientError,
    UpstreamError,
)
from gemini_media_mcp.files import InputImage
from gemini_media_mcp.openrouter import (
    API_HOST,
    GOOGLE_PROVIDERS,
    OpenRouterBackend,
    _only_openrouter,
    map_openrouter_error,
    openrouter_model_id,
)

from .conftest import JPEG, PNG

SECRET = "or-test-key-not-real"  # fake value; must never be a real key
pytestmark = pytest.mark.anyio

IMG = InputImage(path=Path("/x.png"), data=PNG, mime_type="image/png")
Handler = Callable[[httpx.Request], httpx.Response]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ok(data: bytes = PNG, **item: Any) -> httpx.Response:
    return httpx.Response(200, json={"created": 1, "data": [{"b64_json": b64(data), **item}]})


def err(status: int, message: str = "", **metadata: Any) -> httpx.Response:
    error: dict[str, Any] = {"code": status, "message": message}
    if metadata:
        error["metadata"] = metadata
    return httpx.Response(status, json={"error": error})


class Recorder:
    """MockTransport handler that replays responses and records every request."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def make_backend(*responses: httpx.Response | Exception) -> tuple[OpenRouterBackend, Recorder]:
    recorder = Recorder(*responses)
    return OpenRouterBackend(SECRET, 30, transport=httpx.MockTransport(recorder)), recorder


async def generate(backend: OpenRouterBackend, **kwargs: Any) -> Any:
    args: dict[str, Any] = {
        "model": "gemini-3.1-flash-image",
        "prompt": "a cat",
        "images": [],
        "aspect_ratio": None,
        "image_size": "1K",
    }
    return await backend.generate_image(**{**args, **kwargs})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("gemini_media_mcp.backend.asyncio.sleep", instant)


# --- request format -----------------------------------------------------------------------


def test_model_id_mapping() -> None:
    assert openrouter_model_id("gemini-3-pro-image") == "google/gemini-3-pro-image"
    assert openrouter_model_id("google/custom") == "google/custom"


def test_requires_key() -> None:
    with pytest.raises(AuthError, match="OPENROUTER_API_KEY"):
        OpenRouterBackend("", 10)


async def test_generate_text_only() -> None:
    backend, rec = make_backend(ok(media_type="image/png"))
    media = await generate(backend, aspect_ratio="16:9", image_size="2K")
    assert media.data == PNG
    assert media.mime_type == "image/png"
    assert media.text is None
    request = rec.requests[0]
    assert str(request.url) == f"https://{API_HOST}/api/v1/images"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert rec.body() == {
        "model": "google/gemini-3.1-flash-image",
        "prompt": "a cat",
        "n": 1,
        "resolution": "2K",
        "aspect_ratio": "16:9",
        "provider": {"only": list(GOOGLE_PROVIDERS), "order": list(GOOGLE_PROVIDERS)},
    }


async def test_generate_with_references_as_data_urls() -> None:
    backend, rec = make_backend(ok())
    await generate(backend, images=[IMG, IMG])
    refs = rec.body()["input_references"]
    assert len(refs) == 2
    url = refs[0]["image_url"]["url"]
    assert refs[0]["type"] == "image_url"
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG
    assert "aspect_ratio" not in rec.body()


async def test_mime_type_comes_from_bytes_not_label() -> None:
    backend, _ = make_backend(ok(JPEG, media_type="image/png"))
    assert (await generate(backend)).mime_type == "image/jpeg"


# --- bad responses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"created": 1, "data": []}),
        httpx.Response(200, json={"created": 1, "data": [{"b64_json": ""}]}),
        httpx.Response(200, json={"created": 1}),
    ],
)
async def test_no_image_returned(response: httpx.Response) -> None:
    backend, _ = make_backend(response)
    with pytest.raises(UpstreamError, match="No image"):
        await generate(backend)


async def test_invalid_base64() -> None:
    backend, _ = make_backend(httpx.Response(200, json={"data": [{"b64_json": "not base64!"}]}))
    with pytest.raises(UpstreamError, match="base64"):
        await generate(backend)


async def test_non_image_bytes_rejected() -> None:
    backend, _ = make_backend(ok(b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
    with pytest.raises(UpstreamError, match="not a PNG"):
        await generate(backend)


async def test_non_json_body() -> None:
    backend, _ = make_backend(httpx.Response(200, content=b"<html>hi</html>"))
    with pytest.raises(UpstreamError, match="no JSON"):
        await generate(backend)


async def test_error_inside_http_200_is_mapped() -> None:
    backend, rec = make_backend(
        httpx.Response(200, json={"error": {"code": 402, "message": "Insufficient credits"}})
    )
    with pytest.raises(QuotaError, match="credits"):
        await generate(backend)
    assert len(rec.requests) == 1


async def test_error_without_message_uses_reason_phrase() -> None:
    backend, _ = make_backend(httpx.Response(400, content=b"oops"))
    with pytest.raises(InvalidRequestError, match="Bad Request"):
        await generate(backend)


# --- error mapping ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "metadata", "expected"),
    [
        (401, "No auth credentials found", {}, AuthError),
        (402, "Insufficient credits", {}, QuotaError),
        (403, "Only management keys", {}, AuthError),
        (403, "flagged", {"reasons": ["violence"]}, SafetyBlockedError),
        (403, "Request blocked by safety policy", {}, SafetyBlockedError),
        (408, "timeout", {}, GeminiTimeoutError),
        (524, "edge timeout", {}, GeminiTimeoutError),
        (429, "Rate limit exceeded", {}, QuotaError),
        (500, "", {}, TransientError),
        (502, "Provider returned error", {}, TransientError),
        (503, "No provider available", {}, TransientError),
        (529, "overloaded", {}, TransientError),
        (400, "Invalid request parameters", {}, InvalidRequestError),
        (400, "PROHIBITED_CONTENT", {}, SafetyBlockedError),
        (404, "Model not found", {}, InvalidRequestError),
        (413, "Request payload too large", {}, InvalidRequestError),
        (302, "moved", {}, UpstreamError),
    ],
)
def test_map_openrouter_error(
    status: int, message: str, metadata: dict[str, Any], expected: type
) -> None:
    assert isinstance(map_openrouter_error(status, message, metadata, Redactor(SECRET)), expected)


def test_moderation_reasons_are_shown() -> None:
    error = map_openrouter_error(403, "flagged", {"reasons": ["hate"]}, Redactor(SECRET))
    assert "hate" in str(error)


async def test_api_key_never_in_messages() -> None:
    backend, _ = make_backend(err(400, f"bad key {SECRET}"), err(401, f"key {SECRET} invalid"))
    for _ in range(2):
        with pytest.raises((InvalidRequestError, AuthError)) as exc:
            await generate(backend)
        assert SECRET not in str(exc.value)
    message = str(map_openrouter_error(429, f"limit {SECRET}", {}, Redactor(SECRET)))
    assert SECRET not in message
    assert "***" in message


async def test_auth_message_names_the_right_key() -> None:
    backend, _ = make_backend(err(401, "invalid"))
    with pytest.raises(AuthError, match="OPENROUTER_API_KEY"):
        await generate(backend)


# --- retries and network ------------------------------------------------------------------


async def test_retries_transient_then_succeeds() -> None:
    backend, rec = make_backend(err(503), err(502), ok())
    assert (await generate(backend)).data == PNG
    assert len(rec.requests) == 3


async def test_retries_at_most_twice() -> None:
    backend, rec = make_backend(err(503), err(503), err(503), ok())
    with pytest.raises(TransientError):
        await generate(backend)
    assert len(rec.requests) == 3  # 1 try + 2 retries


@pytest.mark.parametrize(
    "response",
    [err(429), err(402), err(400), err(524), httpx.ReadTimeout("slow")],
)
async def test_no_retry_for_non_transient(response: httpx.Response | Exception) -> None:
    backend, rec = make_backend(response, ok())
    with pytest.raises((QuotaError, InvalidRequestError, GeminiTimeoutError)):
        await generate(backend)
    assert len(rec.requests) == 1


async def test_connect_error_is_retried() -> None:
    backend, rec = make_backend(httpx.ConnectError("down"), ok())
    assert (await generate(backend)).data == PNG
    assert len(rec.requests) == 2


async def test_connect_error_message() -> None:
    backend, _ = make_backend(*[httpx.ConnectError("down")] * 3)
    with pytest.raises(TransientError, match="Could not reach OpenRouter"):
        await generate(backend)


async def test_other_http_error_is_redacted() -> None:
    backend, _ = make_backend(httpx.RemoteProtocolError(f"broken {SECRET}"))
    with pytest.raises(UpstreamError, match="HTTP error") as exc:
        await generate(backend)
    assert SECRET not in str(exc.value)


async def test_redirects_are_not_followed() -> None:
    redirect = httpx.Response(307, headers={"location": "https://evil.example/steal"})
    backend, rec = make_backend(redirect)
    with pytest.raises(UpstreamError):
        await generate(backend)
    assert [r.url.host for r in rec.requests] == [API_HOST]


@pytest.mark.parametrize("url", ["https://evil.example/x", "http://openrouter.ai/api/v1/images"])
def test_host_guard(url: str) -> None:
    with pytest.raises(UpstreamError, match="Refusing"):
        _only_openrouter(httpx.Request("POST", url))


def test_host_guard_allows_openrouter() -> None:
    _only_openrouter(httpx.Request("POST", f"https://{API_HOST}/api/v1/images"))


async def test_client_runs_host_guard() -> None:
    backend, _ = make_backend(ok())
    async with backend._client() as client:
        with pytest.raises(UpstreamError, match="Refusing"):
            await client.post("https://evil.example/x")


# --- video --------------------------------------------------------------------------------


async def test_video_is_not_available() -> None:
    backend, rec = make_backend()
    with pytest.raises(InvalidRequestError, match="only available with the Google provider"):
        await backend.start_video(
            model="v", prompt="waves", image=None, aspect_ratio="16:9", resolution="720p"
        )
    with pytest.raises(InvalidRequestError, match="Google provider"):
        await backend.get_video("job")
    assert rec.requests == []
