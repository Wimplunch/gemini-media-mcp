"""MCP server entry point: registers the six tools and maps errors to clean tool errors."""

from __future__ import annotations

import functools
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .backend import GeminiBackend, GeminiError, MediaBackend
from .config import ConfigError, Settings, load_settings
from .costs import BudgetExceededError
from .files import FileSafetyError
from .openrouter import OpenRouterBackend
from .service import MediaService, ToolInputError

logger = logging.getLogger("gemini_media_mcp")

P = ParamSpec("P")
R = TypeVar("R")

USER_ERRORS = (
    GeminiError,
    FileSafetyError,
    BudgetExceededError,
    ToolInputError,
    ConfigError,
)

INSTRUCTIONS = """\
Generate and edit images (Nano Banana models) and videos (Gemini Omni Flash) with the Gemini API.
Every paid call is checked against cost limits first. Files are written to the configured
output directory with a JSON sidecar describing prompt, model and cost. Videos take minutes:
start_video returns a job_id (call it once without confirm_cost to see the price), then poll
get_video_status. Input images must be inside the allowed directories."""


Prompt = Annotated[str, Field(description="What to create, in natural language.")]
Model = Annotated[
    str | None, Field(description="Model ID from list_models. Default: configured default.")
]
OutputName = Annotated[
    str | None,
    Field(description="Short name used in the file name (a slug is derived from it)."),
]


def _user_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Turn expected failures into ToolError so the model sees a clear message."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def build_server(service: MediaService) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        name="gemini-media-mcp",
        title="Gemini Media",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    paid = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
    local = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=paid)
    @_user_errors
    async def generate_image(
        prompt: Prompt,
        model: Model = None,
        aspect_ratio: Annotated[str, Field(description="e.g. 1:1, 16:9, 9:16, 4:5")] = "1:1",
        resolution: Annotated[str, Field(description="Image size: 512, 1K, 2K or 4K")] = "1K",
        output_name: OutputName = None,
    ) -> dict[str, Any]:
        """Create an image from a text prompt. Costs money; checked against the budget first."""
        return await service.generate_image(prompt, model, aspect_ratio, resolution, output_name)

    @server.tool(annotations=paid)
    @_user_errors
    async def edit_image(
        prompt: Prompt,
        image_paths: Annotated[
            list[str],
            Field(description="Input/reference images (face, logo, product...) as file paths."),
        ],
        model: Model = None,
        aspect_ratio: Annotated[
            str | None, Field(description="Output aspect ratio; omit to let the model decide.")
        ] = None,
        resolution: Annotated[str, Field(description="Image size: 512, 1K, 2K or 4K")] = "1K",
        output_name: OutputName = None,
    ) -> dict[str, Any]:
        """Edit or combine one or more images according to the prompt."""
        return await service.edit_image(
            prompt, image_paths, model, aspect_ratio, resolution, output_name
        )

    @server.tool(annotations=paid)
    @_user_errors
    async def start_video(
        prompt: Prompt,
        model: Model = None,
        start_image: Annotated[
            str | None, Field(description="Optional image file to use as the first frame.")
        ] = None,
        aspect_ratio: Annotated[str, Field(description="16:9 or 9:16")] = "16:9",
        resolution: Annotated[str, Field(description="e.g. 720p")] = "720p",
        output_name: OutputName = None,
        confirm_cost: Annotated[
            bool,
            Field(description="Must be true to actually start (and pay). False returns the price."),
        ] = False,
    ) -> dict[str, Any]:
        """Start a video generation job and return a job_id immediately (no waiting)."""
        return await service.start_video(
            prompt, model, start_image, aspect_ratio, resolution, output_name, confirm_cost
        )

    @server.tool(annotations=paid)
    @_user_errors
    async def get_video_status(
        job_id: Annotated[str, Field(description="job_id returned by start_video")],
    ) -> dict[str, Any]:
        """Check a video job; when it is done, download the MP4 to the output directory."""
        return await service.get_video_status(job_id)

    @server.tool(annotations=local)
    @_user_errors
    async def list_models() -> dict[str, Any]:
        """List configured models with estimated prices and supported options."""
        return service.list_models()

    @server.tool(annotations=local)
    @_user_errors
    async def cost_report() -> dict[str, Any]:
        """Show estimated spend today, this month and in total, plus remaining budgets."""
        return service.cost_report()

    return server


API_KEY_ENV = {"google": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def create_service(settings: Settings) -> MediaService:
    def backend() -> MediaBackend:
        key = os.environ.get(API_KEY_ENV[settings.provider], "")
        if settings.provider == "openrouter":
            return OpenRouterBackend(key, settings.request_timeout_s)
        return GeminiBackend(key, settings.request_timeout_s)

    return MediaService(settings, backend)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GEMINI_MEDIA_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,  # stdout belongs to the MCP protocol
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"gemini-media-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    key_env = API_KEY_ENV[settings.provider]
    if settings.provider != "google":
        logger.info(
            "Provider %s: prompts and reference images are sent to openrouter.ai. "
            "Video tools are unavailable.",
            settings.provider,
        )
    if not os.environ.get(key_env):
        logger.warning("%s is not set; generation tools will fail until it is.", key_env)
    build_server(create_service(settings)).run()
