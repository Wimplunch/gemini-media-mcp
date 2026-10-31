"""Shared fixtures. No test talks to the real Gemini API."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gemini_media_mcp.backend import GeneratedMedia, VideoJobState
from gemini_media_mcp.config import Settings, load_settings
from gemini_media_mcp.files import InputImage
from gemini_media_mcp.service import MediaService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
TZ = timezone(timedelta(hours=1))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


@pytest.fixture
def settings(out_dir: Path) -> Settings:
    return load_settings({"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir)})


class FakeBackend:
    """Records calls and returns canned results."""

    def __init__(self) -> None:
        self.image_calls: list[dict[str, object]] = []
        self.video_calls: list[dict[str, object]] = []
        self.video_states: list[VideoJobState] = []
        self.image_error: Exception | None = None

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[InputImage],
        aspect_ratio: str | None,
        image_size: str,
    ) -> GeneratedMedia:
        self.image_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "images": list(images),
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
            }
        )
        if self.image_error:
            raise self.image_error
        return GeneratedMedia(data=PNG, mime_type="image/png", text="here you go")

    async def start_video(
        self,
        *,
        model: str,
        prompt: str,
        image: InputImage | None,
        aspect_ratio: str,
        resolution: str,
    ) -> str:
        self.video_calls.append({"model": model, "image": image, "resolution": resolution})
        return f"job-{len(self.video_calls)}"

    async def get_video(self, job_id: str) -> VideoJobState:
        return self.video_states.pop(0)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(settings: Settings, fake_backend: FakeBackend, clock: Clock) -> MediaService:
    return MediaService(settings, lambda: fake_backend, clock=clock)
