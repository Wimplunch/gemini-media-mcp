## What and why

## Checklist
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest` (no real API calls in tests)
- [ ] CHANGELOG.md updated (if user-facing)
- [ ] Prices/model IDs changed? Source + date updated in `models.toml` and `docs/research.md`
