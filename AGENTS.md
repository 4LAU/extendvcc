# extendvcc

Unofficial Python client and CLI for Extend's private virtual card API. This file is the contract for outside contributors and stays free of personal infrastructure.

## Critical rules

1. Never expose or log card numbers, CVCs, API tokens, session files, or PII. Mask to the last 4 digits when logging is unavoidable. The ledger never persists PANs or CVCs.
2. Never commit session files, credential caches, or `.env*`; they hold live auth tokens.
3. Never make real Extend API calls in tests. Network access in tests is deleted, not skipped.
4. Add files to git individually, never `git add .`.

## Done means

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```
All clean, with real output shown.

## Releasing

`uv run python scripts/release.py X.Y.Z` moves CHANGELOG `[Unreleased]` into a dated section, commits, tags `vX.Y.Z`, and pushes branch and tag together (`git push --atomic`). Requires a clean tree on an up-to-date `main`. Pushing the tag builds binaries and publishes to PyPI, but the `release` GitHub Environment requires a maintainer to click Approve in the Actions tab first; nothing reaches PyPI without that gate.
Never bump a version by hand: there is no version string in `pyproject.toml`, `hatch-vcs` derives it from the tag. Write release notes under `## [Unreleased]` as you work.

## Code rules

Get maintainer approval before changing: the auth flow (Cognito SRP, device remembering, OTP, token refresh), card number or CVC handling, the public API surface (`__init__.py` exports, CLI command names/flags), or anything destructive.

- All HTTP goes through `impit` (Chrome TLS fingerprinting). Never import `httpx` or `requests`; Extend blocks non-browser TLS profiles.
- Credentials come from `EXTENDVCC_EMAIL`, `EXTENDVCC_PASSWORD`, `EXTENDVCC_IMAP_*` env vars or interactive prompts. No hardcoded credentials, no 1Password integration in the package.
- Resolve paths lazily through `_paths.py` helpers (`state_dir()`, `ledger_path()`), never at module level, so CLI flags and env vars take effect (flags beat env beats defaults).
- Session and state files are written 0600. The HTTP client has a kill switch that disables automation on account-risk signals (403s, WAF blocks, verification prompts); don't weaken it.
- `_exit_codes.py` codes are part of the CLI contract.
- Conventional Commits, subject under 72 characters.

## Testing

Global policy applies (`~/.claude/testing-policy.md`); the repo's policy and 1% list are in `docs/testing-policy.md`. Tests run offline; mock only at the I/O boundary (HTTP client, filesystem, IMAP). Verification tools: `scripts/smoke_test.py` (live card lifecycle), `--dry-run` on every destructive command, the kill switch.
