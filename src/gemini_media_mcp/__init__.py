"""MCP server for Gemini image and video generation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gemini-media-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"
