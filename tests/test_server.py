from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gemini_media_mcp import server as server_mod
from gemini_media_mcp.backend import GeminiBackend
from gemini_media_mcp.config import Settings
from gemini_media_mcp.service import MediaService

from .conftest import PNG

pytestmark = pytest.mark.anyio

TOOLS = {
    "generate_image",
    "edit_image",
    "start_video",
    "get_video_status",
    "list_models",
    "cost_report",
}


def text_of(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_all_tools_registered(service: MediaService) -> None:
    srv = server_mod.build_server(service)
    tools = {t.name: t for t in await srv.list_tools()}
    assert set(tools) == TOOLS
    assert tools["start_video"].input_schema["properties"]["confirm_cost"]["default"] is False
    annotations = tools["list_models"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True


async def test_tool_calls_end_to_end(service: MediaService, out_dir: Path) -> None:
    srv = server_mod.build_server(service)
    result = await srv.call_tool("generate_image", {"prompt": "a lighthouse"})
    assert "a-lighthouse.png" in text_of(result)
    (out_dir / "ref.png").write_bytes(PNG)
    result = await srv.call_tool("edit_image", {"prompt": "x", "image_paths": ["ref.png"]})
    assert "sidecar" in text_of(result)
    result = await srv.call_tool("start_video", {"prompt": "waves"})
    assert "estimate_only" in text_of(result)
    assert "gemini-omni" in text_of(await srv.call_tool("list_models", {}))
    assert "today_usd" in text_of(await srv.call_tool("cost_report", {}))


async def test_user_errors_become_tool_errors(service: MediaService) -> None:
    srv = server_mod.build_server(service)
    with pytest.raises(ToolError, match="Unknown image model"):
        await srv.call_tool("generate_image", {"prompt": "x", "model": "nope"})
    with pytest.raises(ToolError, match="outside the allowed"):
        await srv.call_tool("edit_image", {"prompt": "x", "image_paths": ["/etc/hostname"]})
    with pytest.raises(ToolError, match="Unknown job_id"):
        await srv.call_tool("get_video_status", {"job_id": "x"})


async def test_missing_key_gives_clear_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    srv = server_mod.build_server(server_mod.create_service(settings))
    with pytest.raises(ToolError, match="GEMINI_API_KEY is not set"):
        await srv.call_tool("generate_image", {"prompt": "x"})


def test_create_service_uses_env_key(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-test-key")
    assert isinstance(server_mod.create_service(settings).backend, GeminiBackend)


def test_main_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_MEDIA_DAILY_BUDGET", "lots")
    with pytest.raises(SystemExit) as exc:
        server_mod.main()
    assert exc.value.code == 2
    assert "DAILY_BUDGET" in capsys.readouterr().err


def test_main_runs_server(
    monkeypatch: pytest.MonkeyPatch, out_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ran = []
    monkeypatch.setenv("GEMINI_MEDIA_OUTPUT_DIR", str(out_dir))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        "gemini_media_mcp.server.MCPServer.run", lambda self, *a, **k: ran.append(self)
    )
    server_mod.main()
    assert len(ran) == 1
    assert "GEMINI_API_KEY is not set" in caplog.text
