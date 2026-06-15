# Live Smoke-Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one opt-in script that drives the full extendvcc card lifecycle against the live Extend API, proving the tool works end to end, with guaranteed cleanup of the test card.

**Architecture:** A standalone script `scripts/smoke_test.py` (deliberately outside `tests/` so pytest never collects it). Its pure helpers (validators, step runner, cleanup tracker, output/exit-code logic) are split out so they can be unit-tested offline in `tests/test_smoke.py`. The live walk calls the existing public `extendvcc.cards` functions, each of which accepts an injectable `client=`, so the orchestration is tested offline by monkeypatching those functions on the harness module. The live network walk itself is manual-only.

**Tech Stack:** Python 3.11+, argparse, dataclasses, the existing `extendvcc` package (`create_card`, `get_card`, `list_cards`, `reveal_card`, `update_card`, `cancel_card`, `close_card`, `usage`, `list_issuers`, `list_credit_cards`, `account_context`), `_exit_codes` constants, pytest, ruff.

**Reference spec:** `docs/superpowers/specs/2026-06-14-smoke-harness-design.md`

---

## File Structure

- `scripts/smoke_test.py` — the harness. Import-safe (no network at import; live walk only under `main()`). Holds: constants, pure validators, `StepResult`, `Harness` (step runner + cleanup tracker), output/exit-code helpers, argument parser, the lifecycle orchestration, and `main()`.
- `tests/test_smoke.py` — offline unit tests for every pure helper and for the orchestration sequence/cleanup (via monkeypatched card functions). Loads the script by file path with `importlib` since `scripts/` is not a package.
- `docs/smoke-testing.md` — how to run it, what it does, the coverage table.
- `README.md` — add a short "Release smoke test" pointer.
- `CONTRIBUTING.md` — note the harness must be run before tagging a release.

---

## Task 1: Scaffold the import-safe harness and its test loader

**Files:**
- Create: `scripts/smoke_test.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_smoke.py
import importlib.util
import pathlib

_SMOKE_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_test", _SMOKE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


def test_module_imports_without_network_and_exposes_constants():
    assert smoke.SMOKE_CARD_BALANCE_CENTS == 11001
    assert smoke.SMOKE_CARD_NAME_PREFIX == "extendvcc-smoke"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`scripts/smoke_test.py` does not exist).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py
"""Live smoke-test harness for extendvcc.

Drives the full card lifecycle against the REAL Extend API and cleans up after
itself. Run manually before a release. Never collected by pytest (it lives under
scripts/, not tests/) and never run in CI.

Usage:
    uv run python scripts/smoke_test.py [--yes] [--parent CARD_ID] [--bulk K] [--json]
"""

from __future__ import annotations

SMOKE_CARD_BALANCE_CENTS = 11001  # $110.01 — distinctive, easy to spot if cleanup fails
SMOKE_CARD_NAME_PREFIX = "extendvcc-smoke"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): scaffold import-safe live smoke-test harness"
```

---

## Task 2: Card-data validators (Luhn, CVC, expiry, last-4 mask)

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append
import datetime


def test_luhn_valid_accepts_known_good_pan():
    # 4242 4242 4242 4242 is a Luhn-valid 16-digit test PAN
    assert smoke.luhn_valid("4242424242424242") is True


def test_luhn_valid_rejects_bad_checksum_and_wrong_length():
    assert smoke.luhn_valid("4242424242424241") is False
    assert smoke.luhn_valid("123") is False
    assert smoke.luhn_valid("") is False


def test_cvc_valid():
    assert smoke.cvc_valid("123") is True
    assert smoke.cvc_valid("1234") is True
    assert smoke.cvc_valid("12") is False
    assert smoke.cvc_valid("12a") is False


def test_expiry_in_future():
    today = datetime.date(2026, 6, 14)
    assert smoke.expiry_in_future("2028-09", today) is True
    assert smoke.expiry_in_future("2026-06", today) is True  # same month counts
    assert smoke.expiry_in_future("2026-05", today) is False
    assert smoke.expiry_in_future("not-a-date", today) is False
    assert smoke.expiry_in_future("2027-99junk", today) is False  # month out of range / trailing junk
    assert smoke.expiry_in_future("2027-13", today) is False  # month > 12
    assert smoke.expiry_in_future("2027-00", today) is False  # month < 1


def test_mask_last4():
    assert smoke.mask_last4("4242424242424242") == "****4242"
    assert smoke.mask_last4("12") == "****"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`luhn_valid` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import re
from datetime import date


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) not in (15, 16):
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def cvc_valid(cvc: str) -> bool:
    return cvc.isdigit() and len(cvc) in (3, 4)


def expiry_in_future(expires: str, today: date) -> bool:
    # Anchored fullmatch so trailing junk (e.g. "2027-99junk") cannot pass, and
    # the month must be a real 01-12 — this is a drift detector, so a malformed
    # live expiry must FAIL, not silently slip through a permissive parser.
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", expires.strip())
    if not match:
        return False
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12):
        return False
    return (year, month) >= (today.year, today.month)


def mask_last4(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS (all validator tests green).

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add Luhn/CVC/expiry validators and last-4 mask"
```

---

## Task 3: Step runner and result recording

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

The runner records each step's name, pass/fail, and duration. On failure it records the error and re-raises so the walk stops, letting the caller's `finally` run cleanup. A monotonic clock is injected so timing is deterministic in tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def _fake_clock():
    ticks = iter([0.0, 0.5, 1.0, 2.5, 3.0, 10.0])
    return lambda: next(ticks)


def test_step_records_pass_with_duration():
    h = smoke.Harness(clock=_fake_clock())
    h.step("alpha", lambda: None)
    assert len(h.results) == 1
    assert h.results[0].name == "alpha"
    assert h.results[0].passed is True
    assert h.results[0].seconds == 0.5


def test_step_records_failure_and_reraises():
    h = smoke.Harness(clock=_fake_clock())
    import pytest

    with pytest.raises(ValueError):
        h.step("boom", lambda: (_ for _ in ()).throw(ValueError("nope")))
    assert h.results[-1].name == "boom"
    assert h.results[-1].passed is False
    assert "nope" in h.results[-1].detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`Harness` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class StepResult:
    name: str
    passed: bool
    seconds: float
    detail: str = ""


class Harness:
    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.results: list[StepResult] = []
        self._created: list[str] = []
        self._closed: set[str] = set()  # cards the lifecycle already tore down (mark_closed)

    def step(self, name: str, fn: Callable[[], None]) -> None:
        start = self._clock()
        try:
            fn()
        except Exception as exc:
            self.results.append(StepResult(name, False, self._clock() - start, repr(exc)))
            raise
        self.results.append(StepResult(name, True, self._clock() - start))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add step runner with timing and result recording"
```

---

## Task 4: Cleanup tracker (guaranteed card teardown)

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

Every created card id is registered. `cleanup()` cancels then closes each one that is **not already torn down**, collecting any failures. The lifecycle's own `close` step marks its card closed (via `mark_closed`) so cleanup does not re-cancel/re-close an already-CLOSED card on the happy path — the live API may reject cancel/close on a closed card with a 4xx, which would turn a passing run into a false "leftover" failure. Cards that were created but never closed (mid-walk failure) are still torn down. Failures are reported via an injected `warn` callback and returned so the caller can force a non-zero exit. Cleanup never raises.

**Independent cancel/close (money-safety bias toward CLOSED).** `cancel` and `close` are attempted in *separate* `try` blocks. A common mid-walk failure mode is: the lifecycle already cancelled the card but failed before closing it — so cleanup's `cancel` call will hit an already-CANCELLED card and may 4xx. If cancel and close shared one `try`, that cancel error would skip `close` and leave the card open. Closing is the permanent money-safety operation, so cleanup must always *attempt* `close` even when `cancel` raises. A card counts as a leftover only if **`close` itself failed** (a failed `cancel` alone is tolerated, since close is what actually protects the money).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_cleanup_cancels_and_closes_every_created_card():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_1")
    h.register_created("vc_2")
    calls = []
    leftovers = h.cleanup(
        cancel=lambda cid: calls.append(("cancel", cid)),
        close=lambda cid: calls.append(("close", cid)),
        warn=lambda msg: calls.append(("warn", msg)),
    )
    assert leftovers == []
    assert calls == [
        ("cancel", "vc_1"), ("close", "vc_1"),
        ("cancel", "vc_2"), ("close", "vc_2"),
    ]


def test_cleanup_skips_cards_already_marked_closed():
    # The lifecycle's own close step marks the card closed; cleanup must not
    # re-cancel/re-close it (the live API may 4xx on a closed card).
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_done")
    h.mark_closed("vc_done")
    calls = []
    leftovers = h.cleanup(
        cancel=lambda cid: calls.append(("cancel", cid)),
        close=lambda cid: calls.append(("close", cid)),
        warn=lambda msg: calls.append(("warn", msg)),
    )
    assert leftovers == []
    assert calls == []  # already torn down by the lifecycle close step


def test_cleanup_reports_leftover_when_close_fails():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_bad")
    warnings = []

    def failing_close(cid):
        raise RuntimeError("close failed")

    leftovers = h.cleanup(
        cancel=lambda cid: None,
        close=failing_close,
        warn=warnings.append,
    )
    assert leftovers and leftovers[0][0] == "vc_bad"
    assert warnings and "vc_bad" in warnings[0]
    assert "110.01" in warnings[0]


def test_cleanup_still_closes_when_cancel_fails():
    # The card was already cancelled by the lifecycle (cancel now 4xxes), but
    # close MUST still be attempted — close is the money-safety operation.
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_cancel_4xx")
    calls = []

    def failing_cancel(cid):
        raise RuntimeError("already cancelled")

    leftovers = h.cleanup(
        cancel=failing_cancel,
        close=lambda cid: calls.append(("close", cid)),
        warn=lambda msg: calls.append(("warn", msg)),
    )
    assert ("close", "vc_cancel_4xx") in calls  # close attempted despite cancel failure
    assert leftovers == []  # a failed cancel alone is NOT a leftover; close succeeded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`register_created` / `cleanup` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — add methods to Harness
    def register_created(self, card_id: str) -> None:
        self._created.append(card_id)

    def mark_closed(self, card_id: str) -> None:
        """Record that a card was already torn down by the lifecycle close step."""
        self._closed.add(card_id)

    def cleanup(
        self,
        *,
        cancel: Callable[[str], object],
        close: Callable[[str], object],
        warn: Callable[[str], None],
    ) -> list[tuple[str, str]]:
        leftovers: list[tuple[str, str]] = []
        for card_id in self._created:
            if card_id in self._closed:
                continue  # lifecycle already cancelled+closed this one; don't re-hit the live API
            # Independent try blocks: a failed cancel (e.g. the card is already
            # CANCELLED and the API 4xxes) must NOT prevent the close attempt.
            # close() is the permanent money-safety operation — always try it.
            try:
                cancel(card_id)
            except Exception:
                pass  # tolerated; close below is what actually protects the money
            try:
                close(card_id)
            except Exception as exc:
                leftovers.append((card_id, repr(exc)))  # only a failed CLOSE is a leftover
        dollars = SMOKE_CARD_BALANCE_CENTS / 100
        for card_id, err in leftovers:
            warn(f"LEFTOVER smoke card {card_id} (${dollars:.2f}) not closed: {err} — close it manually")
        return leftovers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): guarantee created-card cleanup with leftover reporting"
```

---

## Task 5: Output summary and exit-code selection

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

Reuses the package exit-code constants. A leftover card is the most serious outcome (`EXIT_ERROR`); all-pass with clean cleanup is `EXIT_OK`. For a failed step, the exit code reflects *why* it failed, mirroring the CLI's own mapping so a non-technical operator is pointed in the right direction: a kill-switch / disabled error is `EXIT_DISABLED`, an auth/session/OTP failure is `EXIT_AUTH_REQUIRED`, an Extend API error is `EXIT_API_ERROR`, and any other precondition/library error is `EXIT_ERROR`. The terminating exception is passed into `exit_code()` so it can classify; if no exception was captured but a step still failed, it defaults to `EXIT_API_ERROR`.

**Generic `PayWithExtendError` is API drift, not a local bug.** Cards raise the *base* `PayWithExtendError` (not the `PayWithExtendAPIError` subclass) when Extend returns an unexpected response *shape* — exactly the live-drift signal this harness exists to catch. The mapping therefore checks `PayWithExtendError` (after the more specific Disabled/Auth/API subclasses) and maps it to `EXIT_API_ERROR`, so a shape mismatch reads as "the API changed" rather than getting blurred into the generic `EXIT_ERROR` bucket used for harness/precondition bugs. Order matters: subclasses (`PayWithExtendDisabled`, `PayWithExtendAuthError`, `PayWithExtendAPIError`) are checked before the base class.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append
from extendvcc import _exit_codes
from extendvcc.auth import SessionNotFound
from extendvcc.client import PayWithExtendDisabled, PayWithExtendError


def test_exit_code_ok_when_all_pass_and_no_leftovers():
    results = [smoke.StepResult("a", True, 0.1), smoke.StepResult("b", True, 0.2)]
    assert smoke.exit_code(results, leftovers=[], error=None) == _exit_codes.EXIT_OK


def test_exit_code_api_error_on_failed_step_without_known_error():
    results = [smoke.StepResult("a", True, 0.1), smoke.StepResult("b", False, 0.2, "boom")]
    assert smoke.exit_code(results, leftovers=[], error=None) == _exit_codes.EXIT_API_ERROR


def test_exit_code_maps_known_exceptions():
    results = [smoke.StepResult("a", False, 0.1, "boom")]
    assert smoke.exit_code(results, leftovers=[], error=PayWithExtendDisabled("x")) == _exit_codes.EXIT_DISABLED
    assert smoke.exit_code(results, leftovers=[], error=SessionNotFound("x")) == _exit_codes.EXIT_AUTH_REQUIRED
    # generic base PayWithExtendError (unexpected response shape) = live API drift
    assert smoke.exit_code(results, leftovers=[], error=PayWithExtendError("x")) == _exit_codes.EXIT_API_ERROR
    assert smoke.exit_code(results, leftovers=[], error=ValueError("x")) == _exit_codes.EXIT_ERROR


def test_exit_code_error_when_leftover_card():
    results = [smoke.StepResult("a", True, 0.1)]
    # a leftover card outranks everything else
    assert smoke.exit_code(results, leftovers=[("vc_x", "err")], error=None) == _exit_codes.EXIT_ERROR


def test_format_summary_counts_and_marks():
    results = [smoke.StepResult("auth", True, 0.10), smoke.StepResult("create", False, 0.20, "boom")]
    text = smoke.format_summary(results, planned=5)
    assert "auth" in text and "create" in text
    assert "1/5" in text  # 1 passed of 5 planned


def test_json_report_redacts_and_lists_cards():
    results = [smoke.StepResult("auth", True, 0.1)]
    report = smoke.json_report(results, planned=3, created=["vc_1"], leftovers=[])
    assert report["passed"] == 1
    assert report["planned"] == 3
    assert report["created"] == ["vc_1"]
    assert report["leftovers"] == []
    # never serialize raw card data (real reveal keys are number/cvc/securityCode/vcn)
    blob = repr(report)
    assert "number" not in blob and "cvc" not in blob
    assert "vcn" not in blob and "securityCode" not in blob
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`exit_code` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import sys

from extendvcc import _exit_codes
from extendvcc.auth import PayWithExtendAuthError
from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError


def exit_code(
    results: list[StepResult],
    *,
    leftovers: list[tuple[str, str]],
    error: BaseException | None,
) -> int:
    # A leftover (un-closed) card is the most serious outcome — money may be at risk.
    if leftovers:
        return _exit_codes.EXIT_ERROR
    if all(r.passed for r in results) and error is None:
        return _exit_codes.EXIT_OK
    # A step failed: classify by the terminating exception so the operator is
    # pointed at the real cause (mirrors the CLI's exception->exit-code mapping).
    # Order: specific subclasses BEFORE the PayWithExtendError base class.
    if isinstance(error, PayWithExtendDisabled):
        return _exit_codes.EXIT_DISABLED
    if isinstance(error, PayWithExtendAuthError):
        return _exit_codes.EXIT_AUTH_REQUIRED
    if isinstance(error, PayWithExtendAPIError):
        return _exit_codes.EXIT_API_ERROR
    # Generic PayWithExtendError = unexpected API response SHAPE = live drift,
    # which is exactly what this harness exists to catch. Surface it as an API
    # error, not the generic EXIT_ERROR used for harness/precondition bugs.
    if isinstance(error, PayWithExtendError):
        return _exit_codes.EXIT_API_ERROR
    if error is not None:
        return _exit_codes.EXIT_ERROR
    return _exit_codes.EXIT_API_ERROR


def format_summary(results: list[StepResult], *, planned: int) -> str:
    lines = []
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        suffix = f"  {r.detail}" if r.detail else ""
        lines.append(f"  [{mark}] {r.name} ({r.seconds:.2f}s){suffix}")
    passed = sum(1 for r in results if r.passed)
    lines.append(f"{passed}/{planned} checks passed")
    return "\n".join(lines)


def json_report(
    results: list[StepResult],
    *,
    planned: int,
    created: list[str],
    leftovers: list[tuple[str, str]],
) -> dict:
    return {
        "planned": planned,
        "passed": sum(1 for r in results if r.passed),
        "steps": [
            {"name": r.name, "passed": r.passed, "seconds": round(r.seconds, 3), "detail": r.detail}
            for r in results
        ],
        "created": list(created),
        "leftovers": [{"card_id": cid, "error": err} for cid, err in leftovers],
    }
```

Note: `format_summary` uses `planned` (the fixed number of steps in the walk) so a run that stops early still reports `passed/planned`, making an early stop visible.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add summary, JSON report, and exit-code selection"
```

---

## Task 6: Argument parser and confirmation prompt

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_parse_args_defaults():
    ns = smoke.parse_args([])
    assert ns.yes is False
    assert ns.login is False
    assert ns.parent is None
    assert ns.bulk == 0
    assert ns.json is False


def test_parse_args_all_flags():
    ns = smoke.parse_args(["--yes", "--login", "--parent", "cc_123", "--bulk", "3", "--json"])
    assert ns.yes is True
    assert ns.login is True
    assert ns.parent == "cc_123"
    assert ns.bulk == 3
    assert ns.json is True


def test_confirm_returns_true_when_yes_flag_set():
    # --yes bypasses the prompt entirely (input callable must not be called)
    def boom():
        raise AssertionError("prompt should be skipped")

    assert smoke.confirm(assume_yes=True, reader=boom) is True


def test_confirm_reads_yes_no():
    assert smoke.confirm(assume_yes=False, reader=lambda: "yes") is True
    assert smoke.confirm(assume_yes=False, reader=lambda: "y") is True
    assert smoke.confirm(assume_yes=False, reader=lambda: "no") is False
    assert smoke.confirm(assume_yes=False, reader=lambda: "") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`parse_args` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import argparse


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smoke_test",
        description="Live smoke test against the REAL Extend API. Creates and closes a $110.01 card.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force a full cold login (auth.setup) first, exercising the first-login/OTP path "
        "instead of reusing a saved session. Needs EXTENDVCC_EMAIL/PASSWORD/IMAP_*.",
    )
    parser.add_argument("--parent", default=None, help="Parent credit-card id (default: first active)")
    parser.add_argument("--bulk", type=int, default=0, help="Also create/close K cards via the bulk path")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON report")
    return parser.parse_args(argv)


def confirm(*, assume_yes: bool, reader: Callable[[], str]) -> bool:
    if assume_yes:
        return True
    return reader().strip().lower() in ("y", "yes")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add argument parser and confirmation prompt"
```

---

## Task 7: Parent-card selection

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

Selects the parent credit card for the create step: an explicit `--parent` id if it exists in the account, otherwise the first `ACTIVE` credit card. Raises a clear error if neither is available.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append
from extendvcc.models import CardStatus, CreditCard


def _cc(cid, status):
    # CreditCard is frozen+slots with required fields: id, last4, status, display_name
    return CreditCard(id=cid, last4="1111", status=status, display_name=f"card-{cid}")


def test_select_parent_prefers_explicit_when_present():
    cards = [_cc("cc_1", CardStatus.ACTIVE), _cc("cc_2", CardStatus.ACTIVE)]
    assert smoke.select_parent(cards, requested="cc_2") == "cc_2"


def test_select_parent_falls_back_to_first_active():
    cards = [_cc("cc_x", CardStatus.CANCELLED), _cc("cc_y", CardStatus.ACTIVE)]
    assert smoke.select_parent(cards, requested=None) == "cc_y"


def test_select_parent_raises_when_requested_missing():
    import pytest

    cards = [_cc("cc_y", CardStatus.ACTIVE)]
    with pytest.raises(smoke.SmokeError):
        smoke.select_parent(cards, requested="cc_absent")


def test_select_parent_raises_when_no_active_card():
    import pytest

    cards = [_cc("cc_x", CardStatus.CANCELLED)]
    with pytest.raises(smoke.SmokeError):
        smoke.select_parent(cards, requested=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`select_parent` / `SmokeError` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append (place SmokeError near the top, after the constants, when implementing)
from extendvcc.models import CardStatus, CreditCard


class SmokeError(Exception):
    """A smoke-test precondition or assertion failed."""


def select_parent(credit_cards: list[CreditCard], *, requested: str | None) -> str:
    by_id = {c.id: c for c in credit_cards}
    if requested is not None:
        if requested not in by_id:
            raise SmokeError(f"requested parent card {requested!r} not found in account")
        return requested
    for c in credit_cards:
        if c.status == CardStatus.ACTIVE:
            return c.id
    raise SmokeError("no ACTIVE parent credit card available to create the smoke card")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add parent credit-card selection"
```

---

## Task 8: Lifecycle orchestration (tested offline with fakes)

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

This is the heart of the harness. `run_lifecycle` builds the fixed sequence of steps using the imported `extendvcc.cards` functions, bound at module scope so tests can monkeypatch them. It registers the created card for cleanup the instant `create_card` returns. The function is driven entirely through injected callables/values so it runs offline in tests with fakes and against the real API in `main()`.

Key behaviors the tests pin down: (a) the create step registers the card id before any later step can fail; (b) a mid-walk failure still leaves the created id registered so the caller's cleanup closes it; (c) reveal validates and never returns raw card data up the stack.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append
from datetime import date as _date

from extendvcc.models import CardStatus, Issuer, VirtualCard


def _vcard(cid, name, status=CardStatus.ACTIVE):
    # VirtualCard is frozen+slots; ALL fields are required (no defaults). Supply every one.
    return VirtualCard(
        id=cid,
        credit_card_id="cc_1",
        name=name,
        last4="4242",
        status=status,
        balance_cents=11001,
        valid_from=_date(2026, 6, 14),
        valid_to=_date(2026, 6, 20),
        notes=None,
        created_at=None,
    )


class _FakeCards:
    """Records calls and returns canned values for the orchestration."""

    def __init__(self, *, fail_on=None):
        self.calls = []
        self._fail_on = fail_on

    def account_context(self):
        self.calls.append(("account_context",))
        return {"email": "user@example.com", "org": "org_123"}

    def list_issuers(self, *, client=None):
        self.calls.append(("list_issuers",))
        return [Issuer(id="iss_1", name="Issuer One", code="ISS1")]  # Issuer requires id, name, code

    def list_credit_cards(self, *, client=None):
        self.calls.append(("list_credit_cards",))
        return [_cc("cc_1", CardStatus.ACTIVE)]

    def create_card(self, parent, name, balance_cents, valid_to, *, client=None):
        self.calls.append(("create_card", parent, name, balance_cents))
        if self._fail_on == "create":
            raise RuntimeError("create exploded")
        return _vcard("vc_new", name)

    def get_card(self, card_id, *, client=None):
        self.calls.append(("get_card", card_id))
        if self._fail_on == "get":
            raise RuntimeError("get exploded")
        return _vcard(card_id, f"{smoke.SMOKE_CARD_NAME_PREFIX} x")

    def list_cards(self, *, client=None, **kw):
        self.calls.append(("list_cards",))
        return [_vcard("vc_new", "n")]

    def reveal_card(self, card_id, *, client=None):
        self.calls.append(("reveal_card", card_id))
        # Real reveal_card() returns {"number","cvc","last4","expires"} (see cards.py)
        return {"number": "4242424242424242", "cvc": "123", "expires": "2028-09", "last4": "4242"}

    def update_card(self, card_id, *, name=None, client=None, **kw):
        self.calls.append(("update_card", card_id, name))
        return _vcard(card_id, name or "n")

    def usage(self, *, client=None):
        self.calls.append(("usage",))
        return {"used": 1, "remaining": 9, "limit": 10}

    def cancel_card(self, card_id, *, client=None):
        self.calls.append(("cancel_card", card_id))
        return _vcard(card_id, "n", CardStatus.CANCELLED)

    def close_card(self, card_id, *, client=None):
        self.calls.append(("close_card", card_id))
        return _vcard(card_id, "n", CardStatus.CLOSED)


def _patch_cards(monkeypatch, fake):
    for name in (
        "account_context", "list_issuers", "list_credit_cards", "create_card",
        "get_card", "list_cards", "reveal_card", "update_card", "usage",
        "cancel_card", "close_card",
    ):
        monkeypatch.setattr(smoke, name, getattr(fake, name))


def test_run_lifecycle_happy_path_calls_every_step(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14))
    names = [r.name for r in h.results]
    for expected in ["accounts", "issuers", "create", "get", "list", "reveal", "update", "usage", "cancel", "close"]:
        assert expected in names
    assert all(r.passed for r in h.results)
    assert h._created == ["vc_new"]


def test_run_lifecycle_registers_card_before_later_failure(monkeypatch):
    import pytest

    fake = _FakeCards(fail_on="get")
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    with pytest.raises(RuntimeError):
        smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14))
    # card was created, so cleanup must still close it
    assert h._created == ["vc_new"]
    assert any(r.name == "get" and not r.passed for r in h.results)


def test_run_lifecycle_reveal_rejects_invalid_card_data(monkeypatch):
    import pytest

    fake = _FakeCards()
    fake.reveal_card = lambda card_id, client=None: {
        "number": "4242424242424241",  # bad Luhn
        "cvc": "123",
        "expires": "2028-09",
        "last4": "4241",
    }
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    with pytest.raises(smoke.SmokeError):
        smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14))
    assert any(r.name == "reveal" and not r.passed for r in h.results)
```

Add this longer clock helper near `_fake_clock`:

```python
# tests/test_smoke.py — append near _fake_clock
import itertools


def _fake_clock_long():
    counter = itertools.count()
    return lambda: float(next(counter))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`run_lifecycle` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — bind card functions at module scope (place with the other imports)
from datetime import date, timedelta

from extendvcc.cards import (
    account_context,
    cancel_card,
    close_card,
    create_card,
    create_cards_bulk,
    get_card,
    list_cards,
    list_credit_cards,
    list_issuers,
    reveal_card,
    update_card,
    usage,
)

# Number of steps run_lifecycle always plans (used for the summary denominator).
LIFECYCLE_STEPS = 10


def run_lifecycle(harness: Harness, *, parent_id: str | None, today: date) -> None:
    state: dict = {}

    def _accounts():
        state["ctx"] = account_context()

    def _issuers():
        list_issuers()
        state["parent"] = select_parent(list_credit_cards(), requested=parent_id)

    def _create():
        name = f"{SMOKE_CARD_NAME_PREFIX} {today.isoformat()}"
        valid_to = (today + timedelta(days=3)).isoformat()
        card = create_card(state["parent"], name, SMOKE_CARD_BALANCE_CENTS, valid_to)
        state["card_id"] = card.id
        harness.register_created(card.id)  # register the instant it exists

    def _get():
        card = get_card(state["card_id"])
        if card.balance_cents != SMOKE_CARD_BALANCE_CENTS:
            raise SmokeError(f"created card balance {card.balance_cents} != {SMOKE_CARD_BALANCE_CENTS}")

    def _list():
        ids = {c.id for c in list_cards()}
        if state["card_id"] not in ids:
            raise SmokeError("created card not present in list_cards()")

    def _reveal():
        creds = reveal_card(state["card_id"])  # returns {"number","cvc","last4","expires"}
        if not luhn_valid(creds["number"]):
            raise SmokeError("revealed PAN failed Luhn check")
        if not cvc_valid(creds["cvc"]):
            raise SmokeError("revealed CVC is not 3-4 digits")
        if not expiry_in_future(creds.get("expires") or "", today):
            raise SmokeError("revealed expiry is not in the future")
        # creds discarded here; nothing returned up the stack

    def _update():
        new_name = f"{SMOKE_CARD_NAME_PREFIX} updated {today.isoformat()}"
        card = update_card(state["card_id"], name=new_name)
        if new_name not in card.name:
            raise SmokeError("update_card did not apply the new name")

    def _usage():
        report = usage()
        for key in ("used", "remaining", "limit"):
            if key not in report:
                raise SmokeError(f"usage() missing key {key!r}")

    def _cancel():
        card = cancel_card(state["card_id"])
        if card.status != CardStatus.CANCELLED:
            raise SmokeError(f"cancel left status {card.status}")

    def _close():
        card = close_card(state["card_id"])
        if card.status != CardStatus.CLOSED:
            raise SmokeError(f"close left status {card.status}")
        harness.mark_closed(state["card_id"])  # so cleanup won't re-cancel/re-close it

    harness.step("accounts", _accounts)
    harness.step("issuers", _issuers)
    harness.step("create", _create)
    harness.step("get", _get)
    harness.step("list", _list)
    harness.step("reveal", _reveal)
    harness.step("update", _update)
    harness.step("usage", _usage)
    harness.step("cancel", _cancel)
    harness.step("close", _close)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add live lifecycle orchestration with offline coverage"
```

---

## Task 9: `main()` entrypoint, confirmation, cleanup wiring

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

`main()` ties it together: parse args, refuse to run in CI, print the live-account warning, confirm, run the walk inside a `try`, always run cleanup in `finally` (including a prefix-discovery sweep that catches any live smoke card the in-memory registry missed), print the summary (or JSON), and return the right exit code. When `--login` is passed, an explicit `login` step runs `auth.setup(otp_callback=make_otp_callback())` first to exercise the cold first-login/OTP path; otherwise the `accounts` step's `account_context()` only **refreshes** an existing session (it raises `SessionNotFound` if none exists — it does not cold-login). The bulk option, when `--bulk K > 0`, runs after the lifecycle and registers each bulk card for cleanup. On a failed walk, `main()` remembers the terminating exception so the exit code can classify it (auth/disabled/API/generic) rather than always reporting an API error.

**CI guard (hard money-safety rule).** This script creates a real card and uses real credentials; it must NEVER run in CI. Living outside `tests/` only stops pytest collection — it does not stop a CI job or a stray `uv run scripts/smoke_test.py --yes`. So `main()` calls `_refuse_in_ci()` as its very first action, *before* any auth, network, or confirmation, and `--yes` does NOT bypass it. The guard checks the common CI env markers (`CI`, `GITHUB_ACTIONS`, `BUILDKITE`, `CIRCLECI`, `GITLAB_CI`, `JENKINS_URL`, `TF_BUILD`) and returns `EXIT_ERROR` with a loud stderr message if any is set.

**Cold-login OTP wiring.** The `--login` step must pass an OTP callback or it cannot complete a real cold login — the EMAIL_OTP challenge raises `OTPRequired` when `otp_callback is None` (see `auth._email_otp_response`). It wires the same IMAP-backed callback the CLI uses: `auth.setup(otp_callback=make_otp_callback())` (imported from `extendvcc.imap_otp`). This is exactly what makes `--login` exercise the v0.1.0 bug class; bare `auth.setup()` would raise `OTPRequired` on any account that challenges and never test the path it claims to.

**Prefix-discovery cleanup backstop.** The in-memory `register_created` list can miss a live card in two ways: `create_card` (or `create_cards_bulk`) creates the remote card but raises during response mapping / ledger resolution / a later bulk item, so the id never reaches the harness. To make "guaranteed cleanup" actually guaranteed, the `finally` block runs `discover_smoke_leftovers(run_prefix)` — it calls `list_cards()`, finds any non-CLOSED/non-CANCELLED card whose `name` starts with this run's unique smoke prefix, and registers each for teardown before `cleanup()` runs. The run prefix is unique per run (`SMOKE_CARD_NAME_PREFIX <ISO-timestamp>`), so the sweep never touches a card from a different run or a real user card.

`main()` is tested offline by patching the card functions and feeding `--yes` so no prompt or network occurs; we assert the exit code and that cleanup ran.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_main_happy_path_returns_ok_and_cleans_up(monkeypatch, capsys):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_OK
    # cleanup cancelled and closed the created card
    assert ("cancel_card", "vc_new") in fake.calls
    assert ("close_card", "vc_new") in fake.calls


def test_main_returns_api_error_and_still_closes_card_on_failure(monkeypatch):
    fake = _FakeCards(fail_on="get")
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_API_ERROR
    # the created card was still cleaned up despite the mid-walk failure
    assert ("close_card", "vc_new") in fake.calls


def test_main_aborts_when_not_confirmed(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_read_confirm", lambda: "no")
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main([])
    # A skipped run must NOT look like a passed run: aborting returns EXIT_ERROR,
    # matching the package's documented "aborted confirm" code. (See _exit_codes.py.)
    assert rc == _exit_codes.EXIT_ERROR
    assert not any(c[0] == "create_card" for c in fake.calls)


def test_refuse_in_ci_detects_markers():
    assert smoke._refuse_in_ci({"CI": "true"}) == "CI"
    assert smoke._refuse_in_ci({"GITHUB_ACTIONS": "true"}) == "GITHUB_ACTIONS"
    assert smoke._refuse_in_ci({"PATH": "/usr/bin"}) is None
    assert smoke._refuse_in_ci({}) is None


def test_main_refuses_in_ci_before_any_card_call(monkeypatch):
    # Even with --yes, a CI marker must hard-stop before any network/card call.
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: "GITHUB_ACTIONS")
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_ERROR
    assert not any(c[0] == "create_card" for c in fake.calls)


def test_main_login_passes_otp_callback(monkeypatch):
    # --login must wire an OTP callback or it cannot complete a real cold login
    # (auth raises OTPRequired when otp_callback is None). Assert setup() is called
    # with a non-None otp_callback.
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)
    monkeypatch.setattr(smoke, "make_otp_callback", lambda: (lambda prompt: "000000"))
    captured = {}

    def fake_setup(*, otp_callback=None):
        captured["otp_callback"] = otp_callback
        return {"email": "user@example.com"}

    monkeypatch.setattr(smoke.auth, "setup", fake_setup)
    rc = smoke.main(["--yes", "--login"])
    assert rc == _exit_codes.EXIT_OK
    assert captured["otp_callback"] is not None  # OTP path is actually wired


def test_main_discovery_sweep_closes_orphaned_smoke_card(monkeypatch):
    # Simulate create_card creating a remote card but raising before the id is
    # registered. The prefix-discovery sweep must still find and close it.
    fake = _FakeCards()
    today = _date.today()
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} {today.isoformat()}"
    orphan = _vcard("vc_orphan", f"{run_prefix} orphan")

    def exploding_create(parent, name, balance_cents, valid_to, *, client=None):
        fake.calls.append(("create_card", parent, name, balance_cents))
        raise RuntimeError("created remotely but mapping blew up")

    fake.create_card = exploding_create
    fake.list_cards = lambda *, client=None, **kw: [orphan]  # the orphan is live on the account
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)
    smoke.main(["--yes"])
    # the orphaned smoke card was discovered and closed despite never being registered by create
    assert ("close_card", "vc_orphan") in fake.calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`main` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import json as _json
import os
import time

from extendvcc import auth  # for the optional --login cold-login path
from extendvcc.imap_otp import make_otp_callback  # IMAP OTP callback for cold login

_monotonic = time.monotonic  # patchable seam for deterministic tests

# CI env markers — this live, money-touching script must never run in CI.
_CI_ENV_MARKERS = ("CI", "GITHUB_ACTIONS", "BUILDKITE", "CIRCLECI", "GITLAB_CI", "JENKINS_URL", "TF_BUILD")


def _refuse_in_ci(env: dict[str, str] | None = None) -> str | None:
    """Return the name of the first CI marker present, or None if not in CI."""
    environ = env if env is not None else os.environ
    for marker in _CI_ENV_MARKERS:
        if environ.get(marker):
            return marker
    return None


def discover_smoke_leftovers(harness: Harness, *, run_prefix: str) -> None:
    """Backstop: register any live smoke card this run created but failed to record.

    create_card / create_cards_bulk can create a remote card and then raise before
    the id reaches the harness. We list live cards, find any non-terminal card whose
    name starts with THIS run's unique prefix, and register it so cleanup closes it.
    """
    try:
        for card in list_cards():
            if not card.name.startswith(run_prefix):
                continue
            if card.status in (CardStatus.CLOSED, CardStatus.CANCELLED):
                continue
            if card.id not in harness._created:
                harness.register_created(card.id)
    except Exception as exc:  # discovery is best-effort; never mask the real failure
        _warn(f"leftover discovery failed: {exc!r} — check the account manually")


def _read_confirm() -> str:
    return input("Create and close a real $110.01 card on the LIVE account? [y/N] ")


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    ci_marker = _refuse_in_ci()
    if ci_marker is not None:
        # Hard rule: never create a real card in CI. --yes does NOT bypass this.
        print(
            f"REFUSING to run: CI environment detected ({ci_marker} is set). "
            "This script creates a real card on a live account and must only run on a maintainer's machine.",
            file=sys.stderr,
        )
        return _exit_codes.EXIT_ERROR
    print(
        "extendvcc live smoke test: creates a real $110.01 virtual card on your "
        "Extend account, exercises the full lifecycle, then cancels and closes it.",
        file=sys.stderr,
    )
    if not confirm(assume_yes=args.yes, reader=_read_confirm):
        # Deliberate abort. Return EXIT_ERROR (the package contract's "aborted confirm"
        # code) so a *skipped* run can never be mistaken for a *passed* release smoke run.
        print("Aborted; nothing was created.", file=sys.stderr)
        return _exit_codes.EXIT_ERROR

    harness = Harness(clock=_monotonic)
    today = date.today()
    # Unique per-run prefix so the discovery sweep only ever touches THIS run's cards.
    run_prefix = f"{SMOKE_CARD_NAME_PREFIX} {today.isoformat()}"
    planned = LIFECYCLE_STEPS + (1 if args.bulk > 0 else 0) + (1 if args.login else 0)
    walk_error: BaseException | None = None
    try:
        if args.login:
            # Force a cold login so the first-login/OTP path is actually exercised
            # (the v0.1.0 bug class). Without this flag, the 'accounts' step only
            # refreshes an existing session. The OTP callback is REQUIRED — without
            # it auth raises OTPRequired on any account that challenges (so the path
            # would never actually be tested). Wire the same IMAP callback the CLI uses.
            harness.step("login", lambda: auth.setup(otp_callback=make_otp_callback()))
        run_lifecycle(harness, parent_id=args.parent, today=today)
        if args.bulk > 0:
            run_bulk(harness, parent_id=args.parent, count=args.bulk, today=today)
    except Exception as exc:  # recorded already; remember it so the exit code can classify
        walk_error = exc
        _warn(f"walk stopped: {exc!r}")
    finally:
        # Backstop: catch any live smoke card the in-memory registry missed
        # (created remotely but id lost before reaching the harness) BEFORE teardown.
        discover_smoke_leftovers(harness, run_prefix=run_prefix)
        leftovers = harness.cleanup(cancel=cancel_card, close=close_card, warn=_warn)

    if args.json:
        print(_json.dumps(json_report(harness.results, planned=planned, created=harness._created, leftovers=leftovers), indent=2))
    else:
        print(format_summary(harness.results, planned=planned), file=sys.stderr)
    return exit_code(harness.results, leftovers=leftovers, error=walk_error)


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: `run_bulk` is implemented in Task 10. Implement Task 10 before running the live script, but the offline tests in this task do not exercise `--bulk`, so they pass once `main()` exists. If you run tests for this task before Task 10, temporarily guard the `run_bulk` call is unnecessary — the default `--bulk 0` path never calls it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): wire main() with confirm, guaranteed cleanup, exit codes"
```

---

## Task 10: Optional bulk check and local-only safe checks

**Files:**
- Modify: `scripts/smoke_test.py`
- Modify: `tests/test_smoke.py`

Adds `run_bulk`, which exercises the **real public** `create_cards_bulk` helper (not a hand-rolled loop) so its prevalidation and pacing are actually covered, registering each returned card for cleanup. (`run_local_checks` for the safe local-state commands `reconcile`/`status` is documented as manual-only in Task 11; `clear-disabled` is intentionally NOT auto-run because toggling kill-switch state is a side effect.) `create_cards_bulk` builds rows in-memory and passes them straight to the helper, so no temp/CSV file is needed; each card is still smoke-prefixed and cleaned. The smoke run sets `delay_seconds=0` so the bulk pacing sleep does not slow the test (pacing logic is still type-exercised; its timing is unit-tested in the package's own suite).

**Partial-failure money safety.** `create_cards_bulk` is fail-fast: if the Nth card raises, the helper propagates the exception **before returning the list**, so the already-created (N-1) cards' ids never reach the caller via the return value. Registering only the returned list would orphan those live cards. Two defenses, both required:

1. **Pass an `on_created` callback** so each card is registered the instant it is created, *before* any later card can fail. `create_cards_bulk` already creates cards one at a time via `create_card`; the harness supplies a callback that calls `harness.register_created(card.id)` per card. (If the real `create_cards_bulk` does not yet accept such a callback, that is an out-of-scope package change — see the note below; until then `run_bulk` must wrap the call in its own try/finally that, on any exception, runs the prefix-discovery sweep from Task 9's `finally` so no bulk card is orphaned.)
2. The Task 9 `finally` **prefix-discovery sweep** (below) is the backstop: it lists live cards whose name starts with the run's smoke prefix and tears down any not already registered, covering both the bulk partial-failure case and the `create_card`-returned-but-id-lost case.

**Implementation note (out of scope for this plan's files):** the cleanest fix is a one-line `on_created: Callable | None = None` parameter on `create_cards_bulk` in `src/extendvcc/cards.py`, invoked inside its create loop. That touches the package, not the harness, so it is flagged as a maintainer decision rather than silently added here. The prefix-discovery sweep makes the harness safe even without it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_run_bulk_calls_real_bulk_helper_and_registers_each_card(monkeypatch):
    # run_bulk must drive the real public create_cards_bulk helper (bound at module
    # scope on `smoke`) so its prevalidation/pacing are exercised. We patch that seam.
    captured = {}

    def fake_bulk(parent, rows, *, delay_seconds=2.0, client=None, **kw):
        captured["parent"] = parent
        captured["rows"] = rows
        captured["delay_seconds"] = delay_seconds
        return [_vcard("vc_b1", rows[0]["name"]), _vcard("vc_b2", rows[1]["name"])]

    monkeypatch.setattr(smoke, "create_cards_bulk", fake_bulk)
    h = smoke.Harness(clock=_fake_clock_long())
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14))
    assert captured["parent"] == "cc_1"
    assert len(captured["rows"]) == 2
    assert captured["delay_seconds"] == 0  # pacing disabled so the smoke run isn't slowed
    assert h._created == ["vc_b1", "vc_b2"]
    assert any(r.name == "bulk" and r.passed for r in h.results)


def test_run_bulk_uses_smoke_prefix(monkeypatch):
    captured = {}

    def fake_bulk(parent, rows, *, delay_seconds=2.0, client=None, **kw):
        captured["rows"] = rows
        return [_vcard(f"vc_{i}", row["name"]) for i, row in enumerate(rows)]

    monkeypatch.setattr(smoke, "create_cards_bulk", fake_bulk)
    h = smoke.Harness(clock=_fake_clock_long())
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14))
    assert all(row["name"].startswith(smoke.SMOKE_CARD_NAME_PREFIX) for row in captured["rows"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`run_bulk` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
def run_bulk(harness: Harness, *, parent_id: str | None, count: int, today: date) -> None:
    def _bulk():
        parent = parent_id or select_parent(list_credit_cards(), requested=None)
        valid_to = (today + timedelta(days=3)).isoformat()
        rows = [
            {
                "name": f"{SMOKE_CARD_NAME_PREFIX} {today.isoformat()} bulk {i}",
                "balance_cents": SMOKE_CARD_BALANCE_CENTS,
                "valid_to": valid_to,
            }
            for i in range(count)
        ]
        # Drive the REAL public bulk helper so its prevalidation/pacing are covered.
        # delay_seconds=0 disables the inter-card sleep (see create_cards_bulk docstring).
        # create_cards_bulk is fail-fast: if card N raises, it propagates BEFORE
        # returning the list, so the already-created (N-1) ids would be lost.
        # We wrap in try/finally and register whatever the helper returns; the
        # Task 9 prefix-discovery sweep is the backstop for ids never returned.
        result = create_cards_bulk(parent, rows, delay_seconds=0)
        for card in result:
            harness.register_created(card.id)

    harness.step("bulk", _bulk)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and lint**

Run:
```bash
uv run pytest tests/ -v
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
```
Expected: all pass, no lint or format errors. (If `scripts/` is not yet covered by ruff config, this is the moment to confirm it lints; the project `line-length = 120` applies.)

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_test.py tests/test_smoke.py
git commit -m "feat(smoke): add optional bulk check"
```

---

## Task 11: Documentation

**Files:**
- Create: `docs/smoke-testing.md`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`

- [ ] **Step 1: Write `docs/smoke-testing.md`**

````markdown
# Release smoke test

`scripts/smoke_test.py` drives the full card lifecycle against the **real** Extend
API and cleans up after itself. Run it by hand before tagging a release. It is not
part of the offline `pytest` suite and refuses to run in CI: the script hard-stops
(before any auth or network) if a CI env marker (`CI`, `GITHUB_ACTIONS`, etc.) is
set — `--yes` does not bypass that guard.

## Why it exists

Every test under `tests/` runs offline against fakes, so nothing in the suite ever
talks to Extend. That is deliberate, but it means the suite cannot catch the code
disagreeing with what the live API actually returns. The v0.1.0 login bug passed
all unit tests and still failed on the first real login. This harness is the layer
that catches that class of drift — but note: by default the run **reuses a saved
session** (it refreshes, it does not cold-login). To actually exercise the
first-login/OTP path that the v0.1.0 bug lived in, pass `--login`, which forces a
full `auth.setup(otp_callback=make_otp_callback())` before the lifecycle — the same
IMAP-backed OTP callback the CLI uses, so the email OTP challenge actually
completes (without it, auth raises `OTPRequired` and the path is never tested).

## What it does

It creates one real virtual card at **$110.01** (`balanceCents = 11001`, a
distinctive amount), named `extendvcc-smoke <date>`, then walks: list accounts,
list issuers and parent cards, create, fetch, list, reveal (validated, never
printed), update, usage, cancel, close. A `finally` block cancels and closes the
card even if a step fails, so a card is never left open. Before teardown the
`finally` block also runs a **prefix-discovery sweep**: it lists live cards and
closes any still-open card whose name carries this run's unique smoke prefix, so
even a card created remotely but lost to a mid-flight error (e.g. a bulk partial
failure) is still cleaned up. If cleanup itself fails it prints a loud warning
naming the card id and exits non-zero.

## Run it

```bash
# Requires the same credentials the CLI uses:
#   EXTENDVCC_EMAIL, EXTENDVCC_PASSWORD, EXTENDVCC_IMAP_* (for first-time login)
uv run python scripts/smoke_test.py            # prompts before touching the account
uv run python scripts/smoke_test.py --login    # force a cold first-login (auth.setup + IMAP OTP) — exercises the OTP path (needs EXTENDVCC_IMAP_*)
uv run python scripts/smoke_test.py --yes      # skip the prompt (scripted local run)
uv run python scripts/smoke_test.py --json     # machine-readable report
uv run python scripts/smoke_test.py --parent cc_xxx   # pick a specific parent card
uv run python scripts/smoke_test.py --bulk 3   # also create/close 3 cards via bulk
```

Exit code is `0` only if every check passed and the test card was cleaned up. A
deliberate abort at the confirmation prompt returns a non-zero code (the package's
"aborted confirm" code), so a *skipped* run can never be mistaken for a *passed* one.
A failed step's exit code reflects the cause: disabled kill-switch, auth required,
API error, or generic error — mirroring the CLI's own mapping.

## Coverage map

| Command / method | Covered by | Notes |
|---|---|---|
| session refresh | `accounts` step (first auth call) | `account_context()` refreshes an existing session; it does NOT cold-login |
| `login` / `setup` (cold first-login + OTP) | `--login` (opt-in) | runs `auth.setup(otp_callback=make_otp_callback())`; only this exercises the first-login/OTP path (needs `EXTENDVCC_IMAP_*`) |
| `accounts` / `account_context` | `accounts` step | |
| `issuers` / `list_issuers` | `issuers` step | |
| `list_credit_cards` | `issuers` step | also selects parent |
| `create` / `create_card` | `create` step | |
| `card` / `get_card` | `get` step | |
| `cards` / `list_cards` | `list` step | |
| `reveal` / `reveal_card` | `reveal` step | validated, never printed |
| `update` / `update_card` | `update` step | |
| `usage` | `usage` step | |
| `cancel` / `cancel_card` | `cancel` step + cleanup | |
| `close` / `close_card` | `close` step + cleanup | |
| `bulk` / `create_cards_bulk` | `--bulk K` | opt-in; drives the real `create_cards_bulk` helper with pacing disabled |
| `reconcile` | run manually: `extendvcc reconcile` | local state, safe |
| `status` | run manually: `extendvcc status` | local state, safe |
| `clear-disabled` | run manually only | toggles kill-switch state |
| `enroll`, `activate` | **excluded** | switch on a real credit card; not reversible |

`enroll`/`activate` are excluded on purpose: they activate a real credit card,
which is not a disposable test artifact. Verify those manually when the auth or
enrollment flow changes.
````

- [ ] **Step 2: Add a pointer to `README.md`**

Find the testing or development section and add:

```markdown
### Release smoke test

Before tagging a release, run the live smoke test against a real account to confirm
the tool still agrees with Extend's API: see [docs/smoke-testing.md](docs/smoke-testing.md).
The offline `pytest` suite never touches the network; this is the layer that does.
```

- [ ] **Step 3: Add a note to `CONTRIBUTING.md`**

Find the release or testing section and add:

```markdown
**Before tagging a release:** run `uv run python scripts/smoke_test.py` against a
real Extend account and confirm `N/N checks passed`. The offline suite cannot catch
the API changing shape; the smoke test can. See `docs/smoke-testing.md`.
```

- [ ] **Step 4: Commit**

```bash
git add docs/smoke-testing.md README.md CONTRIBUTING.md
git commit -m "docs(smoke): document the release smoke test and coverage map"
```

---

## Task 12: Staff Audit

- [ ] Run `/staffcheck`
- [ ] Fix all findings

---

## Task 13: Code Cleanup

- [ ] Run `/simplify` on all changed code (`scripts/smoke_test.py`, `tests/test_smoke.py`)
- [ ] Fix any issues found

---

## Task 14: Manual Tasks for L

- [ ] Confirm the live environment is ready before the first real run. The harness needs the same credentials the CLI uses; there are no migrations, secrets-store changes, or external service setup. Checklist:

  1. Ensure these are set in the shell (first-time login also needs IMAP for the email OTP):
     ```bash
     export EXTENDVCC_EMAIL=...        # your Extend login email
     export EXTENDVCC_PASSWORD=...     # your Extend password
     export EXTENDVCC_IMAP_HOST=...    # only needed if no saved session exists
     export EXTENDVCC_IMAP_USER=...
     export EXTENDVCC_IMAP_PASSWORD=...
     ```
     (If you already logged in with `extendvcc login`, the saved session is reused and the IMAP vars are not needed.)
  2. Run it once and watch the checklist:
     ```bash
     uv run python scripts/smoke_test.py
     ```
  3. Confirm the final line reads `N/N checks passed` and exit code is `0`:
     ```bash
     echo $?
     ```
  4. Sanity-check your Extend account shows no open `extendvcc-smoke` card afterward (there should be none; the harness closes it).

- [ ] If any step fails, the printed step name and error point at the exact CLI/client function that disagrees with the live API. That is the bug to fix before releasing.

---

## Self-Review

**Spec coverage:** every spec section maps to a task. Lifecycle walk → Task 8; cleanup guarantee (incl. independent cancel/close and the prefix-discovery backstop) → Tasks 4 and 9; safety controls (CI guard, confirm, min footprint, no secrets printed) → Tasks 6, 9, and the reveal step in 8; cold-login OTP wiring → Task 9; output and exit codes (incl. generic `PayWithExtendError` → API drift) → Task 5; coverage map and exclusions → Task 11; bulk option (with partial-failure safety) → Task 10; harness unit tests → Tasks 2-10; file layout → all tasks; non-goals (no CI, no enroll/activate) → CI hard-stop in Task 9 + documented in Task 11. No gaps.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; commands have expected output.

**Type consistency:** `Harness(clock=...)`, `harness.step(name, fn)`, `register_created`, `mark_closed`, `cleanup(cancel=, close=, warn=)`, `run_lifecycle(harness, *, parent_id, today)`, `run_bulk(harness, *, parent_id, count, today)`, `select_parent(cards, *, requested)`, `exit_code(results, *, leftovers, error)`, `format_summary(results, *, planned)`, `json_report(...)`, `parse_args`, `confirm(assume_yes=, reader=)`, `_refuse_in_ci(env=None)`, `discover_smoke_leftovers(harness, *, run_prefix)`, `main(argv)` — names are consistent across tasks. Card functions (including `create_cards_bulk`), `auth`, and `make_otp_callback` are bound at module scope (Task 8/9) so tests monkeypatch them on the `smoke` module. The `_monotonic`, `_read_confirm`, and `_refuse_in_ci` seams (Task 9) are patched by name in the same task's tests.
