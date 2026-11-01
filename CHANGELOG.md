# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-31

### Added
- MCP server (`mcp` 2.x `MCPServer`) with six tools: `generate_image`, `edit_image`,
  `start_video`, `get_video_status`, `list_models`, `cost_report`.
- Image generation and editing via the Gemini Interactions API with the Nano Banana family
  (`gemini-nano-banana-2.1`, `gemini-3.1-flash-lite-image`, `gemini-3.1-flash-image`,
  `gemini-3-pro-image`).
- Non-blocking video generation with Gemini Omni Flash (`gemini-omni-1.1-flash`), the
  replacement for the Veo 3.1 previews that Google shut down on 2026-10-22.
- Configurable model catalogue with prices (checked 2026-10-31), TOML override and env overrides.
- Cost control: pre-call estimates, per-call/daily/monthly limits, `confirm_cost` for video,
  JSONL cost log.
- One retry layer only (max. 2 retries, 5xx/connection errors): the SDK's own retries are
  turned off, so a quota 429 with a long `Retry-After` fails fast instead of hanging.
- File safety: allowed directories with symlink resolution, magic-byte type check, size
  limit, never-overwrite output naming.
- Provenance sidecar JSON for every output; AI-labelling notice (SynthID, EU AI Act Art. 50).
- Tests with a mocked API, ruff, mypy (strict), GitHub Actions CI, Dependabot, gitleaks.

[Unreleased]: https://github.com/Wimplunch/gemini-media-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Wimplunch/gemini-media-mcp/releases/tag/v0.1.0
