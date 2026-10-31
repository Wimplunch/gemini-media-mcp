from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai.interactions import ImageContent, Interaction, VideoContent

from gemini_media_mcp import backend as backend_mod
from gemini_media_mcp.backend import (
    AuthError,
    GeminiBackend,
    GeminiTimeoutError,
    InvalidRequestError,
    QuotaError,
    Redactor,
    SafetyBlockedError,
    TransientError,
    UpstreamError,
    map_error,
    with_retries,
)
from gemini_media_mcp.files import InputImage

from .conftest import MP4, PNG

SECRET = "test-key-not-real"  # fake value; must never be a real key
pytestmark = pytest.mark.anyio


class HTTPError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def interaction(**fields: Any) -> Interaction:
    return Interaction.model_validate(fields)


def model_output(*content: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps as the real API returns them; the SDK derives output_* from these."""
    return [{"type": "model_output", "content": list(content)}]


class FakeInteractions:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def _next(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def create(self, **kwargs: Any) -> Any:
        return await self._next(**kwargs)

    async def get(self, job_id: str, **kwargs: Any) -> Any:
        return await self._next(id=job_id, **kwargs)


class FakeFiles:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    async def download(self, *, file: str) -> bytes:
        self.downloads.append(file)
        return MP4


def make_backend(results: list[Any]) -> tuple[GeminiBackend, FakeInteractions, FakeFiles]:
    interactions, files = FakeInteractions(results), FakeFiles()
    client = SimpleNamespace(aio=SimpleNamespace(interactions=interactions, files=files))
    return GeminiBackend(SECRET, 30, client=client), interactions, files


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("gemini_media_mcp.backend.asyncio.sleep", instant)


IMG = InputImage(path=Path("/x.png"), data=PNG, mime_type="image/png")


# --- error mapping ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (HTTPError(401), AuthError),
        (HTTPError(403), AuthError),
        (HTTPError(429, "RESOURCE_EXHAUSTED"), QuotaError),
        (HTTPError(503), TransientError),
        (HTTPError(500), TransientError),
        (HTTPError(400, "Image is not valid"), InvalidRequestError),
        (HTTPError(400, "Request blocked by safety policy"), SafetyBlockedError),
        (HTTPError(404, "model not found"), InvalidRequestError),
        (httpx.ConnectError("boom"), TransientError),
        (httpx.ReadTimeout("slow"), GeminiTimeoutError),
        (RuntimeError("weird"), UpstreamError),
    ],
)
def test_map_error(exc: Exception, expected: type) -> None:
    assert isinstance(map_error(exc, Redactor(SECRET)), expected)


def test_map_error_passes_through_gemini_errors() -> None:
    err = QuotaError("x")
    assert map_error(err, Redactor(SECRET)) is err


def test_api_key_never_in_messages() -> None:
    for exc in (
        HTTPError(400, f"bad request for key {SECRET}"),
        HTTPError(429, f"quota {SECRET}"),
        RuntimeError(f"url?key={SECRET}"),
    ):
        message = str(map_error(exc, Redactor(SECRET)))
        assert SECRET not in message
        assert "***" in message


def test_auth_error_message_is_generic() -> None:
    message = str(map_error(HTTPError(403, f"key {SECRET} invalid"), Redactor(SECRET)))
    assert SECRET not in message
    assert "GEMINI_API_KEY" in message


def test_redactor_truncates() -> None:
    assert len(Redactor("")("x" * 1000)) == 400


# --- retries ------------------------------------------------------------------------------


async def test_retries_transient_then_succeeds() -> None:
    attempts = []

    async def call() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise HTTPError(503)
        return "ok"

    assert await with_retries(call, Redactor(SECRET)) == "ok"
    assert len(attempts) == 3


async def test_retries_at_most_twice() -> None:
    attempts = []

    async def call() -> str:
        attempts.append(1)
        raise HTTPError(503)

    with pytest.raises(TransientError):
        await with_retries(call, Redactor(SECRET))
    assert len(attempts) == 3  # 1 try + 2 retries


@pytest.mark.parametrize("exc", [HTTPError(429), HTTPError(400), httpx.ReadTimeout("t")])
async def test_no_retry_for_non_transient(exc: Exception) -> None:
    attempts = []

    async def call() -> str:
        attempts.append(1)
        raise exc

    with pytest.raises(backend_mod.GeminiError):
        await with_retries(call, Redactor(SECRET))
    assert len(attempts) == 1


# --- GeminiBackend ------------------------------------------------------------------------


def test_requires_key() -> None:
    with pytest.raises(AuthError):
        GeminiBackend("", 10)


def test_real_client_is_constructed_without_network() -> None:
    assert GeminiBackend(SECRET, 10)._client is not None


async def test_generate_image_text_only() -> None:
    result = interaction(
        status="completed",
        id="i1",
        steps=model_output(
            {"type": "text", "text": "done"},
            {"type": "image", "data": b64(PNG), "mime_type": "image/png"},
        ),
    )
    backend, interactions, _ = make_backend([result])
    media = await backend.generate_image(
        model="m", prompt="a cat", images=[], aspect_ratio="16:9", image_size="1K"
    )
    assert media.data == PNG
    assert media.mime_type == "image/png"
    assert media.text == "done"
    call = interactions.calls[0]
    assert call["input"] == "a cat"
    assert call["response_format"] == {"type": "image", "image_size": "1K", "aspect_ratio": "16:9"}
    assert call["store"] is False
    assert call["timeout"] == 30


async def test_generate_image_with_references() -> None:
    result = interaction(status="completed", output_image=ImageContent(data=b64(PNG)))
    backend, interactions, _ = make_backend([result])
    media = await backend.generate_image(
        model="m", prompt="combine", images=[IMG, IMG], aspect_ratio=None, image_size="2K"
    )
    assert media.mime_type == "image/png"  # default when the API omits it
    parts = interactions.calls[0]["input"]
    assert parts[0] == {"type": "text", "text": "combine"}
    assert [p["type"] for p in parts[1:]] == ["image", "image"]
    assert base64.b64decode(parts[1]["data"]) == PNG
    assert "aspect_ratio" not in interactions.calls[0]["response_format"]


async def test_generate_image_safety_block() -> None:
    result = interaction(
        status="failed", errors=[{"code": "SAFETY", "message": "Blocked by safety filters"}]
    )
    backend, _, _ = make_backend([result])
    with pytest.raises(SafetyBlockedError, match="safety"):
        await backend.generate_image(
            model="m", prompt="x", images=[], aspect_ratio=None, image_size="1K"
        )


async def test_generate_image_no_output_with_model_text() -> None:
    result = interaction(
        status="completed", steps=model_output({"type": "text", "text": "I can only describe it."})
    )
    backend, _, _ = make_backend([result])
    with pytest.raises(UpstreamError, match="I can only describe it"):
        await backend.generate_image(
            model="m", prompt="x", images=[], aspect_ratio=None, image_size="1K"
        )


async def test_generate_image_budget_status() -> None:
    backend, _, _ = make_backend([interaction(status="budget_exceeded")])
    with pytest.raises(QuotaError):
        await backend.generate_image(
            model="m", prompt="x", images=[], aspect_ratio=None, image_size="1K"
        )


async def test_generate_image_retries_then_quota() -> None:
    backend, interactions, _ = make_backend([HTTPError(503), HTTPError(429, "quota")])
    with pytest.raises(QuotaError):
        await backend.generate_image(
            model="m", prompt="x", images=[], aspect_ratio=None, image_size="1K"
        )
    assert len(interactions.calls) == 2


async def test_stream_response_is_rejected() -> None:
    backend, _, _ = make_backend([object()])
    with pytest.raises(UpstreamError, match="streaming"):
        await backend.generate_image(
            model="m", prompt="x", images=[], aspect_ratio=None, image_size="1K"
        )


async def test_start_video_background() -> None:
    backend, interactions, _ = make_backend([interaction(status="in_progress", id="job-9")])
    job = await backend.start_video(
        model="v", prompt="waves", image=IMG, aspect_ratio="9:16", resolution="720p"
    )
    assert job == "job-9"
    call = interactions.calls[0]
    assert call["background"] is True
    assert call["store"] is True
    assert call["response_format"] == {
        "type": "video",
        "aspect_ratio": "9:16",
        "resolution": "720p",
    }
    assert [p["type"] for p in call["input"]] == ["image", "text"]


async def test_start_video_text_only_and_missing_id() -> None:
    backend, interactions, _ = make_backend([interaction(status="queued", id="")])
    with pytest.raises(UpstreamError, match="job ID"):
        await backend.start_video(
            model="v", prompt="waves", image=None, aspect_ratio="16:9", resolution="720p"
        )
    assert interactions.calls[0]["input"] == "waves"


@pytest.mark.parametrize("status", ["queued", "in_progress"])
async def test_get_video_pending(status: str) -> None:
    backend, interactions, _ = make_backend([interaction(status=status, id="j")])
    assert (await backend.get_video("j")).status == "in_progress"
    assert interactions.calls[0]["id"] == "j"


async def test_get_video_inline() -> None:
    done = interaction(
        status="completed",
        steps=model_output({"type": "video", "data": b64(MP4), "mime_type": "video/mp4"}),
    )
    backend, _, _ = make_backend([done])
    state = await backend.get_video("j")
    assert state.status == "completed"
    assert state.video is not None
    assert state.video.data == MP4


async def test_get_video_via_google_uri() -> None:
    uri = "https://generativelanguage.googleapis.com/v1beta/files/abc:download"
    backend, _, files = make_backend(
        [interaction(status="completed", output_video=VideoContent(uri=uri))]
    )
    state = await backend.get_video("j")
    assert state.video is not None
    assert state.video.data == MP4
    assert state.video.mime_type == "video/mp4"
    assert files.downloads == [uri]


async def test_get_video_refuses_foreign_host() -> None:
    backend, _, files = make_backend(
        [interaction(status="completed", output_video=VideoContent(uri="https://evil.example/x"))]
    )
    with pytest.raises(UpstreamError, match="non-Google"):
        await backend.get_video("j")
    assert files.downloads == []


async def test_get_video_failed_and_empty() -> None:
    failed = interaction(status="failed", errors=[{"message": "prohibited content"}])
    empty = interaction(status="completed")
    backend, _, _ = make_backend([failed, empty])
    state = await backend.get_video("j")
    assert state.status == "failed"
    assert state.error is not None
    assert "safety" in state.error
    assert (await backend.get_video("j")).status == "failed"


async def test_get_video_stream_rejected() -> None:
    backend, _, _ = make_backend([object()])
    with pytest.raises(UpstreamError):
        await backend.get_video("j")
