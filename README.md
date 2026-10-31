# gemini-media-mcp

[![CI](https://github.com/Wimplunch/gemini-media-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Wimplunch/gemini-media-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Deutsch:** [README.de.md](README.de.md)

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) create
and edit images with Google's **Nano Banana** models and generate videos with
**Gemini Omni Flash**, using the Gemini API.

It is built to be boring in the right places: every paid call is priced and checked against
your budget first, files can only be read from folders you allow, nothing is ever
overwritten, and every output gets a JSON sidecar that records how it was made.

> **Video note (2026-10-31):** Google shut down the Veo 3.1 preview models on the Gemini API
> on 2026-10-22 and recommends Gemini Omni Flash as the replacement. The video tools use
> `gemini-omni-1.1-flash` by default. Details: [docs/research.md](docs/research.md).

## Tools

| Tool | What it does | Costs money |
|---|---|---|
| `generate_image` | Text → image. Options: `model`, `aspect_ratio`, `resolution`, `output_name` | yes |
| `edit_image` | Image(s) + prompt → image. Up to 14 reference images (face, logo, product …) as file paths | yes |
| `start_video` | Starts a video job (text, or an image as first frame) and returns a `job_id` at once. Without `confirm_cost=true` it only returns the price | yes |
| `get_video_status` | Polls a job; when done, downloads the MP4 | no (cost is logged at start) |
| `list_models` | Configured models, prices and options | no |
| `cost_report` | Estimated spend today / this month / total, remaining budget | no |

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) **with billing
  enabled**. None of the image or video models has a free tier.

## Installation

The package is not on PyPI yet. Run it straight from GitHub with `uvx`:

```bash
uvx --from git+https://github.com/Wimplunch/gemini-media-mcp gemini-media-mcp
```

or from a local checkout:

```bash
git clone https://github.com/Wimplunch/gemini-media-mcp
uvx --from ./gemini-media-mcp gemini-media-mcp
```

Once published to PyPI this becomes `uvx gemini-media-mcp`.

### API key

The server reads the key **only** from the `GEMINI_API_KEY` environment variable. Keep it out
of config files and chat windows. On Linux you can make it available to desktop apps like
this (input is hidden, file is readable only by you):

```bash
mkdir -p ~/.config/environment.d && (umask 077; read -rsp "Gemini key: " K && printf 'GEMINI_API_KEY=%s\n' "$K" > ~/.config/environment.d/gemini.conf; unset K)
```

Log out and back in afterwards. On macOS/Windows, set the variable in your shell profile or
system settings.

### Claude Code

```bash
claude mcp add --scope user gemini-media \
  -e GEMINI_MEDIA_OUTPUT_DIR="$HOME/Pictures/gemini" \
  -- uvx --from git+https://github.com/Wimplunch/gemini-media-mcp gemini-media-mcp
```

Claude Code passes its own environment to the server, so `GEMINI_API_KEY` is inherited and
never written into the config. Check with `claude mcp list`.

### Claude Desktop

Edit `claude_desktop_config.json`
(Linux `~/.config/Claude/`, macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "gemini-media": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Wimplunch/gemini-media-mcp", "gemini-media-mcp"],
      "env": {
        "GEMINI_MEDIA_OUTPUT_DIR": "/home/you/Pictures/gemini"
      }
    }
  }
}
```

Restart Claude Desktop. The server inherits `GEMINI_API_KEY` from the app's environment.
Avoid pasting the key into this file, especially if you sync or share it.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | – | Your Gemini API key (required for generation) |
| `GEMINI_MEDIA_OUTPUT_DIR` | `~/gemini-media` | Where images, videos and sidecars are written |
| `GEMINI_MEDIA_ALLOWED_DIRS` | output dir | Folders input images may be read from, separated by `:` (`;` on Windows). Must contain the output dir |
| `GEMINI_MEDIA_MAX_COST_PER_CALL` | `0.50` | Max estimated USD per call |
| `GEMINI_MEDIA_DAILY_BUDGET` | `2.00` | Max estimated USD per day (local time) |
| `GEMINI_MEDIA_MONTHLY_BUDGET` | `20.00` | Max estimated USD per calendar month |
| `GEMINI_MEDIA_MAX_INPUT_MB` | `7` | Max size per input image |
| `GEMINI_MEDIA_DEFAULT_IMAGE_MODEL` | `gemini-nano-banana-2.1` | Default for image tools |
| `GEMINI_MEDIA_DEFAULT_VIDEO_MODEL` | `gemini-omni-1.1-flash` | Default for video tools |
| `GEMINI_MEDIA_CONFIG` | – | Path to a TOML file that overrides or adds models and prices |
| `GEMINI_MEDIA_STATE_DIR` | `<output>/.gemini-media` | Cost log and video job list |
| `GEMINI_MEDIA_TIMEOUT_SECONDS` | `180` | Timeout per API request |
| `GEMINI_MEDIA_LOG_LEVEL` | `WARNING` | Log level (logs go to stderr) |

The default per-call limit of $0.50 blocks videos (a 10 s video costs about $1.01). Raise
`GEMINI_MEDIA_MAX_COST_PER_CALL` deliberately if you want video.

### Models and prices

The bundled catalogue is [`src/gemini_media_mcp/models.toml`](src/gemini_media_mcp/models.toml).
Prices were checked on **2026-10-31** against the
[official price list](https://ai.google.dev/gemini-api/docs/pricing). To change them, copy
the file, edit it and point `GEMINI_MEDIA_CONFIG` at your copy. Each `[models."<id>"]` table
in your file replaces the bundled entry for that ID.

## Cost examples

Estimates at Google's list prices from 2026-10-31 (USD, standard tier):

| What | Model | Estimate |
|---|---|---|
| 1 image, 1K | Nano Banana 2 Lite (`gemini-3.1-flash-lite-image`) | $0.034 |
| 1 image, 1K | Nano Banana 2.1 (`gemini-nano-banana-2.1`) | $0.034 |
| 1 image, 4K | Nano Banana 2.1 | $0.113 |
| 1 image, 2K, 3 reference images | Nano Banana Pro (`gemini-3-pro-image`) | $0.141 |
| 1 image, 4K | Nano Banana Pro | $0.240 |
| 1 video, 720p, up to 10 s | Gemini Omni Flash | ≈ $1.01 |
| 50 images per month, 1K | Nano Banana 2.1 | ≈ $1.70 |

The log stores *estimates*. Your Google bill is what counts; check it in
[AI Studio](https://aistudio.google.com/usage).

## Security

- **API key:** read only from `GEMINI_API_KEY` and sent only as an HTTP header to Google. It
  is never logged, never part of the settings object and removed from every error message.
- **File access:** input images are only read from `GEMINI_MEDIA_ALLOWED_DIRS`. Paths are
  fully resolved (symlinks included) before the check, so `../` tricks and symlinks pointing
  outside are rejected.
- **Input validation:** the file type is detected from the file content (PNG, JPEG, WebP,
  HEIC/HEIF), not from the extension. Size is limited.
- **No overwrites:** outputs are named `YYYYMMDD-HHMMSS_<slug>.<ext>` and created with
  exclusive-create; an existing name gets a `-2`, `-3` … suffix.
- **Cost control:** each call is priced before it is sent and rejected if it would break the
  per-call, daily or monthly limit. Videos need an explicit `confirm_cost=true`.
- **Network:** the server talks only to the Google Gemini API (`*.googleapis.com`). No
  telemetry, no analytics, no update checks.
- **Dependencies:** a handful of direct runtime dependencies, pinned exactly; the lockfile
  pins the rest. Dependabot and a gitleaks secret scan run on the repository.

More in [SECURITY.md](SECURITY.md).

## Transparency: sidecars and AI labelling

Every output gets a `<name>.json` next to it with prompt, model, reference images (path and
SHA-256), parameters, estimated cost and timestamp.

Google embeds an invisible **SynthID** watermark in every image and video generated with
these models. If you publish the results, label them as AI-generated: from 2 August 2026,
**Article 50 of the EU AI Act** requires that deepfakes, and AI-generated text published to
inform the public, are disclosed as artificially generated. The sidecar files help you keep
track of what was generated. (This is a pointer, not legal advice.)

## Example prompts

Ask Claude in plain language, for example:

- "Make a 16:9 header image: a robot eating lunch at a desk, flat illustration style, call it robot-lunch."
- "Use `~/Pictures/gemini/me.jpg` and `~/Pictures/gemini/logo.png`: put the logo on my T-shirt, keep my face unchanged."
- "What would a 9:16 video of waves at sunset cost?"
- "Is the video done yet?"
- "How much have I spent on images this month?"

## Development

```bash
uv sync
uv run pytest          # tests use a fake API, no network
uv run ruff check . && uv run ruff format --check .
uv run mypy
uvx pre-commit install # gitleaks + ruff before each commit
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).

---

Not affiliated with or endorsed by Google. Gemini, Nano Banana and Veo are trademarks of Google LLC.

Built by Wimplunch – Lunch is for wimps.
