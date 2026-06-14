# extendvcc

Unofficial Python client and CLI for Extend's private virtual card API. Extracted from `argus/lib/paywithextend/`.

**Stack:** Python 3.11+, src/ layout, hatchling build, impit (Chrome TLS fingerprinting), filelock, argparse CLI, pytest, ruff.

---

## Critical Rules

1. **NEVER expose secrets** — card numbers, CVCs, API tokens, session files, PII. If exposed: STOP, inform L, rotate.
2. **NEVER use `git add .`** — add files individually. gitleaks pre-commit enforces this.
3. **NEVER log or print card numbers, CVCs, or full tokens.** Mask to last 4 digits when logging is necessary.
4. **NEVER make real Extend API calls in tests.** All tests run offline with fakes.
5. **NEVER commit session files, credential caches, or `.env*` files.**
6. **NEVER claim tests pass without showing actual output.**

---

## Definition of Done

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```

All three clean before claiming done.

---

## Code Practices

**Ask L approval for:** auth flow changes, anything touching card number/CVC handling, public API surface changes, destructive ops. Everything else: proceed.

**Generation:** Search existing code first. One function at a time (max 30 lines).

**impit:** All HTTP goes through `impit` with Chrome TLS fingerprinting. Never use bare `httpx` or `requests` — Extend fingerprints non-browser clients.

**Credentials:** Env vars (`EXTENDVCC_EMAIL`, `EXTENDVCC_PASSWORD`, `EXTENDVCC_IMAP_*`). Interactive prompts in CLI. No 1Password integration in this package.

**Paths:** Lazy resolution via `_paths.py`. CLI flags override env vars override defaults. Never use module-level `Path` constants that resolve at import time.

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

## Testing

See `docs/testing-policy.md`. All tests offline, mock only at I/O boundary, every test protects a named invariant.

---

## Communication

Refer to user as **L**. Brief summary first. No praise. Frame what/why, not how.
