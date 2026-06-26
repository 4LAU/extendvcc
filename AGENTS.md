# AGENTS.md: Contributor Guide

Tool-agnostic guide for AI agents and human contributors. For Claude Code specifics, see `CLAUDE.md`.

---

## Project Overview

`extendvcc` is an unofficial Python client and CLI for Extend's private virtual card API (`api.paywithextend.com`). It handles Cognito SRP authentication with device remembering and email OTP, the full virtual-card lifecycle (create, list, update, cancel, close, reveal), parent credit-card enrollment and billing-address updates, and a JSONL audit ledger for all card mutations. HTTP uses `impit` for Chrome TLS fingerprinting, which is necessary because Extend blocks non-browser TLS profiles.

---

## Build, Lint, Test

All three must be clean before any change is considered done:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```

Install dev dependencies first if needed: `uv pip install -e '.[dev]'`

---

## Module Map

```
src/extendvcc/
├── __init__.py      # Public API re-exports
├── _paths.py        # Lazy state_dir() / ledger_path() with CLI override, env, default
├── _jsonl.py        # Vendored append_jsonl helper
├── auth.py          # Cognito SRP login, device remembering, token refresh, session persistence
├── client.py        # HTTP client (impit), kill switch, rate limiting, account-risk detection
├── cards.py         # Card CRUD — create, list, get, update, cancel, close, reveal, enroll, bulk
├── imap_otp.py      # IMAP-based OTP retrieval for Cognito EMAIL_OTP challenges
├── ledger.py        # JSONL audit ledger for card mutations (pending/confirm/fail)
├── models.py        # CardStatus, VirtualCard, CreditCard, Issuer, Recurrence
└── cli.py           # Full lifecycle CLI (login, cards, create, reveal, etc.)
```

---

## Coding Style

- **HTTP:** All network requests go through `impit` with Chrome TLS fingerprinting. Never import `httpx` or `requests` directly; Extend detects and blocks non-browser TLS profiles.
- **Paths:** Use `_paths.py` helpers (`state_dir()`, `ledger_path()`). Never compute paths at module level. Always resolve lazily so CLI flags and env vars take effect.
- **Functions:** One function at a time, max ~30 lines. Search existing code before adding anything new.
- **Logging:** Never log or print card numbers, CVCs, or full tokens. Mask to last 4 digits when logging is necessary.
- **Tests:** All tests run offline with fakes. Mock only at the I/O boundary (HTTP client, filesystem, IMAP). See `docs/testing-policy.md`.

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

## What Needs Approval vs. Proceed

**Get maintainer approval before changing:**
- Auth flow (Cognito SRP, device remembering, OTP, token refresh)
- Anything that touches card number or CVC handling
- Public API surface (`__init__.py` exports, CLI command names/flags)
- Destructive operations (data deletion, credential rotation)

**Proceed without asking:**
- Bug fixes inside existing functions
- New tests
- Documentation updates
- Dependency version bumps
- Refactors that don't change the public API

---

## Security Rules

- **Never store or log PAN/CVC:** the ledger never persists card numbers or CVCs. Mask to last 4 when logging.
- **Never make real Extend API calls in tests:** all tests run offline with fakes. Network access in tests is not skipped; it is deleted.
- **Never commit session files, credential caches, or `.env*` files:** these contain live auth tokens.
- **Credentials via env vars:** `EXTENDVCC_EMAIL`, `EXTENDVCC_PASSWORD`, `EXTENDVCC_IMAP_*`. Interactive prompts in CLI. No hardcoded credentials anywhere.
- Session and state files are written with `0600` permissions. The HTTP client has a kill switch that disables itself on risk signals (403, WAF blocks, verification prompts).
