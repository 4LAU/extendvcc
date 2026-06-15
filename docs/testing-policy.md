# Testing Policy

Test what fails silently. Delete everything else.

Every test must answer: _"Without this test, a change could ship that [X] and nobody would know until production."_ If X produces a visible crash, traceback, or HTTP error, the app is the test. Delete it.

---

## Offline-Only Rule

**All tests run without network access.** No real Extend API calls, no real IMAP connections. Tests that require network are deleted, not skipped.

---

## Before Writing a Test

Three gates, in order. If any gate passes, do not write the test:

1. **Would this failure produce a visible signal?** Crash, traceback, exception, empty output: all visible. Don't write the test.
2. **Does Python or the type system already enforce this?** Type hints, dataclass validation, enum constraints. Don't write the test.
3. **Is there already a test covering this seam?** Search first. Don't duplicate.

If all three gates fail, meaning the failure would be **silent**, write the test.

---

## What Earns a Test

| Pattern | Why it's silent |
|---------|-----------------|
| Wrong card data | Balances, limits, dates that look plausible but are wrong: financial exposure |
| Silent credential leak | Card numbers or CVCs appearing in logs, error messages, or serialized output |
| Token refresh race | Concurrent CLI invocations corrupt session state; both get invalid tokens silently |
| Auth state machine | Login/refresh/re-login transitions that silently lose the session or skip OTP |
| Idempotence | Retrying a card creation produces duplicates with no visible error |
| Wire contracts | Extend API response shape changes that silently drop fields or misparse amounts |
| OTP extraction | IMAP parser returns wrong or stale code; login succeeds with wrong account |
| Amount/currency math | Cent-to-dollar conversion, balance arithmetic: wrong values look plausible |
| Ledger corruption | Concurrent writes corrupt the JSONL file; pending mutations vanish silently |

---

## What to Delete

- **Happy paths whose failure is visible:** API call fails → CLI crashes
- **Shape/type assertions:** `assert x is not None` as primary assertion
- **Guard rejections:** input validation that raises is visible
- **Wiring tests:** mock 2+ layers to verify A calls B
- **CLI output formatting:** tested by looking at it
- **Third-party library behavior:** don't test that impit works

---

## Invariant Tiers

| Tier | Category | Rule |
|------|----------|------|
| **1** | Credential security, financial data integrity, silent data corruption | Every distinct failure mode. Non-negotiable. |
| **2** | Auth state machine, wire contracts, OTP extraction, session persistence | Happy path + the one error path that causes a silent incident. |
| **3** | CLI output formatting, help text, error message wording | **MANDATORY DELETE.** |

---

## Mock Discipline

- **Mock only at the I/O boundary:** HTTP client (impit), filesystem (session files, ledger), IMAP connection.
- **Never mock internal code.** Mocking `auth` to test `cards` = testing wiring. Delete or restructure.
- **Never mock the math.** Feed real numbers, assert real numbers.
- **One mock layer max.** More than one = wrong test. Delete or restructure.
- **Use monkeypatch**, not `unittest.mock.patch`.

---

## Property-Based Testing (hypothesis)

Functions where silent numeric or financial corruption is possible:

- Amount conversions: random cent values → dollar conversion reversible, never negative, no precision loss
- Card field validation: random strings → validation never crashes, always returns bool
- Session serialization: random session dicts → round-trip through save/load preserves all fields
- Ledger sensitive-data guard: random field names → never permits PAN/CVC-shaped keys

---

## Forbidden Patterns

| Pattern | Action |
|---------|--------|
| `assert x is not None` as primary assertion | Delete |
| `assert isinstance(x, dict)` as primary assertion | Delete |
| `assert len(results) > 0` | Assert specific values |
| Snapshot / golden-output string matching | Delete |
| `@pytest.mark.flaky` or retry decorators | Delete the test |
| Parametrize > 5 cases for same invariant | Collapse to 1-3 |
| Tests that can't state their invariant | Delete |
| Mocking more than one layer | Delete or restructure |
| Any test making a real HTTP request | Delete |
