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

All tests run offline — no real API calls are made.

## Pre-commit

A gitleaks pre-commit hook is required to prevent accidental secret leaks.
