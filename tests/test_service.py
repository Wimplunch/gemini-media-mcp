from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from gemini_media_mcp.backend import GeneratedMedia, QuotaError, VideoJobState
from gemini_media_mcp.config import Settings
from gemini_media_mcp.costs import BudgetExceededError
from gemini_media_mcp.files import FileSafetyError
from gemini_media_mcp.service import MediaService, ToolInputError

from .conftest import MP4, PNG, Clock, FakeBackend

pytestmark = pytest.mark.anyio


async def test_generate_image_writes_file_sidecar_and_log(
    service: MediaService, fake_backend: FakeBackend, out_dir: Path
) -> None:
    result = await service.generate_image("Ein Fuchs im Schnee", output_name="Fuchs")
    path = Path(result["file"])
    assert path.parent == out_dir.resolve()
    assert path.name == "20261031-120000_fuchs.png"
    assert path.read_bytes() == PNG
    sidecar = json.loads(Path(result["sidecar"]).read_text())
    assert sidecar["prompt"] == "Ein Fuchs im Schnee"
    assert sidecar["model"] == "gemini-nano-banana-2.1"
    assert sidecar["provider"] == "google"
    assert sidecar["parameters"] == {"aspect_ratio": "1:1", "resolution": "1K"}
    assert sidecar["ai_generated"] is True
    assert "SynthID" in sidecar["notice"]
    assert sidecar["reference_images"] == []
    log = service.ledger.entries()
    assert len(log) == 1
    assert log[0]["output_file"] == str(path)
    assert log[0]["estimated_cost_usd"] == result["estimated_cost_usd"]
    assert fake_backend.image_calls[0]["image_size"] == "1K"


async def test_slug_from_prompt(service: MediaService) -> None:
    result = await service.generate_image("A red bike")
    assert Path(result["file"]).name.endswith("_a-red-bike.png")


async def test_never_overwrites(service: MediaService) -> None:
    first = await service.generate_image("same", output_name="same")
    second = await service.generate_image("same", output_name="same")
    assert first["file"] != second["file"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"prompt": "   "}, "empty"),
        ({"prompt": "x" * 10_001}, "longer"),
        ({"prompt": "x", "model": "gemini-omni-1.1-flash"}, "Unknown image model"),
        ({"prompt": "x", "aspect_ratio": "7:3"}, "Aspect ratio"),
        ({"prompt": "x", "model": "gemini-3.1-flash-lite-image", "resolution": "4K"}, "Resolution"),
    ],
)
async def test_generate_image_validation(
    service: MediaService, fake_backend: FakeBackend, kwargs: dict[str, str], match: str
) -> None:
    with pytest.raises(ToolInputError, match=match):
        await service.generate_image(**kwargs)
    assert fake_backend.image_calls == []


async def test_per_call_limit_blocks_before_api(
    settings: Settings, fake_backend: FakeBackend, clock: Clock
) -> None:
    svc = MediaService(replace(settings, max_cost_per_call=0.01), lambda: fake_backend, clock)
    with pytest.raises(BudgetExceededError, match="per-call"):
        await svc.generate_image("x")
    assert fake_backend.image_calls == []
    assert svc.ledger.entries() == []


async def test_daily_budget_accumulates(
    settings: Settings, fake_backend: FakeBackend, clock: Clock
) -> None:
    svc = MediaService(replace(settings, daily_budget=0.08), lambda: fake_backend, clock)
    await svc.generate_image("one", model="gemini-3.1-flash-lite-image")
    await svc.generate_image("two", model="gemini-3.1-flash-lite-image")
    with pytest.raises(BudgetExceededError, match="Daily"):
        await svc.generate_image("three", model="gemini-3.1-flash-lite-image")
    assert len(fake_backend.image_calls) == 2
    clock.now += timedelta(days=1)
    await svc.generate_image("next day", model="gemini-3.1-flash-lite-image")


async def test_monthly_budget(settings: Settings, fake_backend: FakeBackend, clock: Clock) -> None:
    svc = MediaService(replace(settings, monthly_budget=0.05), lambda: fake_backend, clock)
    await svc.generate_image("one", model="gemini-3.1-flash-lite-image")
    with pytest.raises(BudgetExceededError, match="Monthly"):
        await svc.generate_image("two", model="gemini-3.1-flash-lite-image")


async def test_api_failure_is_not_logged(service: MediaService, fake_backend: FakeBackend) -> None:
    fake_backend.image_error = QuotaError("quota")
    with pytest.raises(QuotaError):
        await service.generate_image("x")
    assert service.ledger.entries() == []


async def test_edit_image_with_references(
    service: MediaService, fake_backend: FakeBackend, out_dir: Path
) -> None:
    (out_dir / "face.png").write_bytes(PNG)
    (out_dir / "logo.png").write_bytes(PNG)
    result = await service.edit_image("put the logo on the shirt", ["face.png", "logo.png"])
    call = fake_backend.image_calls[0]
    assert len(call["images"]) == 2  # type: ignore[arg-type]
    assert call["aspect_ratio"] is None
    sidecar = json.loads(Path(result["sidecar"]).read_text())
    assert [Path(r["path"]).name for r in sidecar["reference_images"]] == ["face.png", "logo.png"]
    assert all(len(r["sha256"]) == 64 for r in sidecar["reference_images"])
    assert sidecar["tool"] == "edit_image"


async def test_edit_image_rejects_outside_paths(
    service: MediaService, fake_backend: FakeBackend, tmp_path: Path
) -> None:
    (tmp_path / "outside.png").write_bytes(PNG)
    with pytest.raises(FileSafetyError):
        await service.edit_image("x", [str(tmp_path / "outside.png")])
    with pytest.raises(FileSafetyError):
        await service.edit_image("x", ["../outside.png"])
    assert fake_backend.image_calls == []


async def test_edit_image_needs_images_and_respects_max(
    service: MediaService, out_dir: Path
) -> None:
    with pytest.raises(ToolInputError, match="at least one"):
        await service.edit_image("x", [])
    (out_dir / "a.png").write_bytes(PNG)
    with pytest.raises(ToolInputError, match="at most 14"):
        await service.edit_image("x", ["a.png"] * 15)


async def test_start_video_estimate_only(service: MediaService, fake_backend: FakeBackend) -> None:
    result = await service.start_video("waves at sunset")
    assert result["status"] == "estimate_only"
    assert result["estimated_cost_usd"] == pytest.approx(1.014, abs=0.001)
    assert "confirm_cost=true" in result["message"]
    assert fake_backend.video_calls == []
    assert service.ledger.entries() == []


async def test_start_video_respects_budget(
    service: MediaService, fake_backend: FakeBackend
) -> None:
    # Default per-call limit is $0.50, a 10 s video is ~$1.01.
    with pytest.raises(BudgetExceededError):
        await service.start_video("waves", confirm_cost=True)
    assert fake_backend.video_calls == []


@pytest.fixture
def video_service(settings: Settings, fake_backend: FakeBackend, clock: Clock) -> MediaService:
    return MediaService(replace(settings, max_cost_per_call=2.0), lambda: fake_backend, clock)


async def test_video_flow(
    video_service: MediaService, fake_backend: FakeBackend, clock: Clock, out_dir: Path
) -> None:
    (out_dir / "start.png").write_bytes(PNG)
    started = await video_service.start_video(
        "waves", start_image="start.png", output_name="Meer", confirm_cost=True
    )
    assert started["status"] == "started"
    job_id = started["job_id"]
    assert fake_backend.video_calls[0]["image"] is not None
    assert video_service.ledger.entries()[0]["job_id"] == job_id

    fake_backend.video_states = [
        VideoJobState(status="in_progress"),
        VideoJobState(status="completed", video=GeneratedMedia(MP4, "video/mp4")),
    ]
    clock.now += timedelta(seconds=90)
    pending = await video_service.get_video_status(job_id)
    assert pending == {"job_id": job_id, "status": "in_progress", "elapsed_seconds": 90}

    done = await video_service.get_video_status(job_id)
    assert done["status"] == "completed"
    assert Path(done["file"]).name.endswith("_meer.mp4")
    assert Path(done["file"]).read_bytes() == MP4
    sidecar = json.loads(Path(done["sidecar"]).read_text())
    assert sidecar["job_id"] == job_id
    assert sidecar["prompt"] == "waves"

    # Second call is served from the job store without touching the API.
    again = await video_service.get_video_status(job_id)
    assert again["file"] == done["file"]
    totals = video_service.ledger.totals(clock())
    assert totals.calls == 1  # download entry costs 0


async def test_video_failure(video_service: MediaService, fake_backend: FakeBackend) -> None:
    job_id = (await video_service.start_video("x", confirm_cost=True))["job_id"]
    fake_backend.video_states = [VideoJobState(status="failed", error="blocked by safety")]
    result = await video_service.get_video_status(job_id)
    assert result == {"job_id": job_id, "status": "failed", "error": "blocked by safety"}
    assert (await video_service.get_video_status(job_id))["status"] == "failed"


async def test_video_validation(video_service: MediaService) -> None:
    with pytest.raises(ToolInputError, match="Aspect ratio"):
        await video_service.start_video("x", aspect_ratio="1:1")
    with pytest.raises(ToolInputError, match="Resolution"):
        await video_service.start_video("x", resolution="4k")
    with pytest.raises(ToolInputError, match="Unknown video model"):
        await video_service.start_video("x", model="gemini-3-pro-image")


async def test_unknown_job(service: MediaService) -> None:
    with pytest.raises(ToolInputError, match="Unknown job_id"):
        await service.get_video_status("not-a-job")


async def test_list_models(service: MediaService) -> None:
    result = service.list_models()
    ids = {m["id"] for m in result["models"]}
    assert {"gemini-nano-banana-2.1", "gemini-3-pro-image", "gemini-omni-1.1-flash"} <= ids
    assert result["pricing_checked"] == "2026-10-31"
    defaults = {m["id"] for m in result["models"] if m["default"]}
    assert defaults == {"gemini-nano-banana-2.1", "gemini-omni-1.1-flash"}
    assert result["provider"] == "google"
    assert result["video_available"] is True


async def test_cost_report(service: MediaService) -> None:
    await service.generate_image("x", model="gemini-3.1-flash-lite-image")
    report = service.cost_report()
    assert report["today_usd"] == pytest.approx(0.0336, abs=1e-4)
    assert report["month_usd"] == report["total_usd"] == report["today_usd"]
    assert report["paid_calls"] == 1
    assert report["limits"]["daily_remaining_usd"] == pytest.approx(2.0 - report["today_usd"])
    assert "gemini-3.1-flash-lite-image" in report["by_model_usd"]


def test_backend_is_created_lazily(settings: Settings) -> None:
    created = []

    def factory() -> FakeBackend:
        created.append(1)
        return FakeBackend()

    svc = MediaService(settings, factory)
    svc.list_models()
    svc.cost_report()
    assert created == []
    assert svc.backend is svc.backend
    assert created == [1]


@pytest.fixture
def openrouter_service(settings: Settings, fake_backend: FakeBackend, clock: Clock) -> MediaService:
    return MediaService(replace(settings, provider="openrouter"), lambda: fake_backend, clock=clock)


async def test_openrouter_keeps_budget_sandbox_and_provenance(
    openrouter_service: MediaService, fake_backend: FakeBackend, settings: Settings
) -> None:
    result = await openrouter_service.generate_image("a fox", model="gemini-3.1-flash-lite-image")
    sidecar = json.loads(Path(result["sidecar"]).read_text())
    assert sidecar["provider"] == "openrouter"
    assert sidecar["model"] == "gemini-3.1-flash-lite-image"
    assert openrouter_service.cost_report()["paid_calls"] == 1

    with pytest.raises(FileSafetyError):
        await openrouter_service.edit_image("x", ["/etc/hostname"])
    strict = replace(openrouter_service.settings, max_cost_per_call=0.01)
    with pytest.raises(BudgetExceededError):
        await MediaService(strict, lambda: fake_backend).generate_image("x")
    assert len(fake_backend.image_calls) == 1


async def test_openrouter_has_no_video(
    openrouter_service: MediaService, fake_backend: FakeBackend
) -> None:
    for confirm in (False, True):
        with pytest.raises(ToolInputError, match="Google provider"):
            await openrouter_service.start_video("waves", confirm_cost=confirm)
    assert fake_backend.video_calls == []
    listing = openrouter_service.list_models()
    assert listing["provider"] == "openrouter"
    assert listing["video_available"] is False
