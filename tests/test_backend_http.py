"""GeminiBackend with the real google-genai client against a local fake API server.

These tests pin the retry behaviour of the SDK itself: only with_retries may retry, and a
Retry-After header must never make a tool call hang.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gemini_media_mcp.backend import (
    GeminiBackend,
    GeminiTimeoutError,
    QuotaError,
    TransientError,
)

from .conftest import PNG

SECRET = "test-key-not-real"  # fake value; must never be a real key
pytestmark = pytest.mark.anyio


@dataclass
class FakeApi:
    status: int = 200
    delay_s: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    port: int = 0

    def body(self) -> bytes:
        if self.status != 200:
            error = {"code": self.status, "message": "fake error", "status": "FAKE"}
            return json.dumps({"error": error}).encode()
        image = {"type": "image", "data": base64.b64encode(PNG).decode(), "mime_type": "image/png"}
        steps = [{"type": "model_output", "content": [image]}]
        return json.dumps({"id": "i-1", "status": "completed", "steps": steps}).encode()


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    api = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            api.requests.append(self.path)
            if api.delay_s:
                time.sleep(api.delay_s)
            data = api.body()
            try:
                self.send_response(api.status)
                for key, value in api.headers.items():
                    self.send_header(key, value)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass  # client gave up (timeout test)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    api.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{api.port}")
    yield api
    server.shutdown()
    server.server_close()


async def _generate(timeout_s: float = 10) -> object:
    backend = GeminiBackend(SECRET, timeout_s)  # real SDK client, no injected fake
    return await backend.generate_image(
        model="gemini-3.1-flash-lite-image",
        prompt="x",
        images=[],
        aspect_ratio="1:1",
        image_size="1K",
    )


async def test_real_client_success(fake_api: FakeApi) -> None:
    media = await _generate()
    assert getattr(media, "data", None) == PNG
    assert fake_api.requests == ["/v1beta/interactions"]


async def test_quota_429_fails_fast_despite_retry_after(fake_api: FakeApi) -> None:
    fake_api.status = 429
    fake_api.headers = {"retry-after": "3600"}
    started = time.monotonic()
    with pytest.raises(QuotaError):
        await _generate()
    assert time.monotonic() - started < 5
    assert len(fake_api.requests) == 1


async def test_read_timeout_is_not_retried(fake_api: FakeApi) -> None:
    fake_api.delay_s = 3
    with pytest.raises(GeminiTimeoutError):
        await _generate(timeout_s=0.5)
    assert len(fake_api.requests) == 1


async def test_only_with_retries_retries_5xx(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("gemini_media_mcp.backend.asyncio.sleep", no_sleep)
    fake_api.status = 503
    fake_api.headers = {"retry-after": "3600"}
    with pytest.raises(TransientError):
        await _generate()
    assert len(fake_api.requests) == 3  # 1 call + 2 retries from with_retries, none from the SDK
