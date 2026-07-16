# extendvcc

Unofficial Python client and CLI for Extend's private virtual card API (`api.paywithextend.com`). Handles Cognito SRP authentication with device remembering and email OTP, the full virtual-card lifecycle (create, list, update, cancel, close, reveal), parent credit-card enrollment and billing-address updates (`update-account`), and a JSONL audit ledger for all card mutations.

**Stack:** Python 3.11+, src/ layout, hatchling build, impit (Chrome TLS fingerprinting), filelock, argparse CLI, pytest, ruff.

This file is read by every AI agent working in this repo. Claude Code users: create a local symlink with `ln -s AGENTS.md CLAUDE.md` (gitignored).

---

## Critical Rules

1. **NEVER expose secrets:** card numbers, CVCs, API tokens, session files, PII. If exposed: STOP and rotate immediately.
2. **NEVER use `git add .`:** add files individually. gitleaks pre-commit enforces this.
3. **NEVER log or print card numbers, CVCs, or full tokens.** Mask to last 4 digits when logging is necessary. The ledger never persists PANs or CVCs.
4. **NEVER make real Extend API calls in tests.** All tests run offline with fakes. Network access in tests is not skipped; it is deleted.
5. **NEVER commit session files, credential caches, or `.env*` files.** These contain live auth tokens.
6. **NEVER claim tests pass without showing actual output.**

---

## Definition of Done

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```

All three clean before claiming done. Install dev dependencies first if needed: `uv pip install -e '.[dev]'`

---

## Releasing

Cut every release with one command:

```bash
uv run python scripts/release.py X.Y.Z
```

It moves the CHANGELOG `[Unreleased]` section into a dated `[X.Y.Z]` section, commits, tags `vX.Y.Z`, and pushes the branch and tag together (`git push --atomic`) — so the tag can never be forgotten. Pushing the tag triggers CI to build binaries and publish to PyPI.

**Then approve the publish.** The `release` GitHub Environment requires a reviewer, so after the tag pushes, the PyPI publish job waits in the Actions tab until the maintainer clicks **Approve**. This is deliberate — nothing reaches PyPI without a human gate. The GitHub release and binaries are not gated; only the PyPI publish is.

**Never bump a version by hand.** There is no version string in `pyproject.toml`; `hatch-vcs` derives the package version from the git tag, which is the single source of truth. Write release notes under `## [Unreleased]` as you work; the script dates them. Requires a clean tree on an up-to-date `main`.

---

## Code Practices

**Get maintainer approval before changing:**
- Auth flow (Cognito SRP, device remembering, OTP, token refresh)
- Anything that touches card number or CVC handling
- Public API surface (`__init__.py` exports, CLI command names/flags)
- Destructive operations (data deletion, credential rotation)

**Proceed without asking:** bug fixes inside existing functions, new tests, documentation updates, dependency version bumps, refactors that don't change the public API.

**Generation:** Search existing code first. One function at a time (max ~30 lines).

**impit:** All HTTP goes through `impit` with Chrome TLS fingerprinting. Never import `httpx` or `requests` directly; Extend detects and blocks non-browser TLS profiles.

**Credentials:** Env vars (`EXTENDVCC_EMAIL`, `EXTENDVCC_PASSWORD`, `EXTENDVCC_IMAP_*`). Interactive prompts in CLI. No hardcoded credentials anywhere. No 1Password integration in this package.

**Paths:** Lazy resolution via `_paths.py` helpers (`state_dir()`, `ledger_path()`). CLI flags override env vars override defaults. Never compute paths at module level; always resolve lazily so flags and env vars take effect.

**Safety at runtime:** Session and state files are written with `0600` permissions. The HTTP client has a kill switch that disables itself on account-risk signals (403s, WAF blocks, verification prompts).

---

## Commit Conventions

Follow Conventional Commits:

```
feat: add bulk-create pacing option
fix: handle expired session token on first request
chore: bump impit to 0.13.0
style: fix trailing whitespace
docs: add reveal example to README
test: add ledger concurrent-write invariant
```

Scope is optional. Keep the subject line under 72 characters.

---

## Module Map

```
src/extendvcc/
├── __init__.py      # Public API re-exports
├── _exit_codes.py   # Stable CLI exit codes (0 ok … 5 API error) — part of the CLI contract
├── _paths.py        # Lazy state_dir() / ledger_path() with CLI override, env, default
├── _jsonl.py        # Vendored append_jsonl helper (0600 file creation)
├── auth.py          # Cognito SRP login, device remembering, token refresh, session persistence
├── client.py        # HTTP client (impit), kill switch, rate limiting, account-risk detection
├── cards.py         # Card CRUD — create, list, get, update, cancel, close, reveal, enroll, bulk
├── imap_otp.py      # IMAP-based OTP retrieval for Cognito EMAIL_OTP challenges
├── ledger.py        # JSONL audit ledger for card mutations (pending/confirm/fail)
├── models.py        # CardStatus, VirtualCard, CreditCard, Issuer, Recurrence
└── cli.py           # Full lifecycle CLI (login, cards, create, reveal, etc.)
```

---

## Testing

See `docs/testing-policy.md`. All tests offline, mock only at the I/O boundary (HTTP client, filesystem, IMAP), every test protects a named invariant.
