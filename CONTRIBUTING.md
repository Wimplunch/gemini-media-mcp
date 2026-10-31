# Contributing

Thanks for helping! Small, focused pull requests are easiest to review.

## Setup

```bash
git clone https://github.com/wimplunch/gemini-media-mcp
cd gemini-media-mcp
uv sync
uvx pre-commit install
```

## Before you open a PR

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

- **No real API calls in tests.** Use the fakes in `tests/conftest.py` and `tests/test_backend.py`.
- Keep the security properties intact (see [SECURITY.md](SECURITY.md)). New file access must go
  through `files.resolve_input_path`; new paid calls must go through the budget check.
- Prices or model IDs changed? Update `src/gemini_media_mcp/models.toml` **with the date and
  source**, and `docs/research.md`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:` …).
- Add a line to `CHANGELOG.md` under *Unreleased* for user-facing changes.

## Code style

Typed (mypy strict), small functions, docstrings where the *why* is not obvious. Error
messages are written for the person using Claude: say what went wrong and what to do.
