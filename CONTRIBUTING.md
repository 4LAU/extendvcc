# Contributing

Fork the repo, create a feature branch, open a PR.

## Setup

```bash
pip install -e '.[dev]'
```

## Checks

Both must pass before merging:

```bash
ruff check src/ tests/
pytest tests/ -v
```

All tests run offline; no real API calls are made.

**Before tagging a release:** run `uv run python scripts/smoke_test.py` against a
real Extend account and confirm `N/N checks passed`. The offline suite cannot catch
the API changing shape; the smoke test can. See `docs/smoke-testing.md`.

## Pre-commit

A gitleaks pre-commit hook is required to prevent accidental secret leaks.
