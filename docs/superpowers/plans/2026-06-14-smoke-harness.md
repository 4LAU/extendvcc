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
    match = re.match(r"(\d{4})-(\d{1,2})", expires.strip())
    if not match:
        return False
    year, month = int(match.group(1)), int(match.group(2))
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

Every created card id is registered. `cleanup()` cancels then closes each one, collecting any failures. Failures are reported via an injected `warn` callback and returned so the caller can force a non-zero exit. Cleanup never raises.

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`register_created` / `cleanup` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — add methods to Harness
    def register_created(self, card_id: str) -> None:
        self._created.append(card_id)

    def cleanup(
        self,
        *,
        cancel: Callable[[str], object],
        close: Callable[[str], object],
        warn: Callable[[str], None],
    ) -> list[tuple[str, str]]:
        leftovers: list[tuple[str, str]] = []
        for card_id in self._created:
            try:
                cancel(card_id)
                close(card_id)
            except Exception as exc:
                leftovers.append((card_id, repr(exc)))
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

Reuses the package exit-code constants. A leftover card is the most serious outcome (`EXIT_ERROR`); a failed step is `EXIT_API_ERROR`; all-pass with clean cleanup is `EXIT_OK`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append
from extendvcc import _exit_codes


def test_exit_code_ok_when_all_pass_and_no_leftovers():
    results = [smoke.StepResult("a", True, 0.1), smoke.StepResult("b", True, 0.2)]
    assert smoke.exit_code(results, leftovers=[]) == _exit_codes.EXIT_OK


def test_exit_code_api_error_on_failed_step():
    results = [smoke.StepResult("a", True, 0.1), smoke.StepResult("b", False, 0.2, "boom")]
    assert smoke.exit_code(results, leftovers=[]) == _exit_codes.EXIT_API_ERROR


def test_exit_code_error_when_leftover_card():
    results = [smoke.StepResult("a", True, 0.1)]
    assert smoke.exit_code(results, leftovers=[("vc_x", "err")]) == _exit_codes.EXIT_ERROR


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
    # never serialize raw card data
    assert "vcn" not in repr(report) and "securityCode" not in repr(report)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`exit_code` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import sys
from extendvcc import _exit_codes


def exit_code(results: list[StepResult], *, leftovers: list[tuple[str, str]]) -> int:
    if leftovers:
        return _exit_codes.EXIT_ERROR
    if all(r.passed for r in results):
        return _exit_codes.EXIT_OK
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
    assert ns.parent is None
    assert ns.bulk == 0
    assert ns.json is False


def test_parse_args_all_flags():
    ns = smoke.parse_args(["--yes", "--parent", "cc_123", "--bulk", "3", "--json"])
    assert ns.yes is True
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
    return CreditCard(id=cid, status=status, display_name=f"card-{cid}")


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
    return VirtualCard(id=cid, name=name, status=status, balance_cents=11001, valid_to=_date(2026, 6, 20))


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
        return [Issuer(id="iss_1", name="Issuer One")]

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
        return {"vcn": "4242424242424242", "securityCode": "123", "expires": "2028-09", "last4": "4242"}

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
        "vcn": "4242424242424241",  # bad Luhn
        "securityCode": "123",
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
        creds = reveal_card(state["card_id"])
        if not luhn_valid(creds["vcn"]):
            raise SmokeError("revealed PAN failed Luhn check")
        if not cvc_valid(creds["securityCode"]):
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

`main()` ties it together: parse args, print the live-account warning, confirm, run the walk inside a `try`, always run cleanup in `finally`, print the summary (or JSON), and return the right exit code. The `auth` step is the very first thing: it calls `account_context()` which forces a real authenticated request (loading or refreshing the session). The bulk option, when `--bulk K > 0`, runs after the lifecycle and registers each bulk card for cleanup.

`main()` is tested offline by patching the card functions and feeding `--yes` so no prompt or network occurs; we assert the exit code and that cleanup ran.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_main_happy_path_returns_ok_and_cleans_up(monkeypatch, capsys):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_OK
    # cleanup cancelled and closed the created card
    assert ("cancel_card", "vc_new") in fake.calls
    assert ("close_card", "vc_new") in fake.calls


def test_main_returns_api_error_and_still_closes_card_on_failure(monkeypatch):
    fake = _FakeCards(fail_on="get")
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_API_ERROR
    # the created card was still cleaned up despite the mid-walk failure
    assert ("close_card", "vc_new") in fake.calls


def test_main_aborts_when_not_confirmed(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_read_confirm", lambda: "no")
    rc = smoke.main([])
    assert rc == _exit_codes.EXIT_OK  # clean, deliberate abort
    assert not any(c[0] == "create_card" for c in fake.calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (`main` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/smoke_test.py — append
import json as _json
import time

_monotonic = time.monotonic  # patchable seam for deterministic tests


def _read_confirm() -> str:
    return input("Create and close a real $110.01 card on the LIVE account? [y/N] ")


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    print(
        "extendvcc live smoke test: creates a real $110.01 virtual card on your "
        "Extend account, exercises the full lifecycle, then cancels and closes it.",
        file=sys.stderr,
    )
    if not confirm(assume_yes=args.yes, reader=_read_confirm):
        print("Aborted; nothing was created.", file=sys.stderr)
        return _exit_codes.EXIT_OK

    harness = Harness(clock=_monotonic)
    today = date.today()
    planned = LIFECYCLE_STEPS + (1 if args.bulk > 0 else 0)
    try:
        run_lifecycle(harness, parent_id=args.parent, today=today)
        if args.bulk > 0:
            run_bulk(harness, parent_id=args.parent, count=args.bulk, today=today)
    except Exception as exc:  # recorded already; cleanup runs below
        _warn(f"walk stopped: {exc!r}")
    finally:
        leftovers = harness.cleanup(cancel=cancel_card, close=close_card, warn=_warn)

    if args.json:
        print(_json.dumps(json_report(harness.results, planned=planned, created=harness._created, leftovers=leftovers), indent=2))
    else:
        print(format_summary(harness.results, planned=planned), file=sys.stderr)
    return exit_code(harness.results, leftovers=leftovers)


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

Adds `run_bulk` (creates `K` cards via the bulk path, registering each for cleanup) and `run_local_checks` (the safe, local-state commands `reconcile` and `status`; `clear-disabled` is intentionally NOT auto-run because toggling kill-switch state in a smoke run is a side effect, so it is documented as manual-only in Task 11). `run_bulk` uses `create_card` per row rather than the CSV-file bulk path so no temp file is needed; each card is still tagged and cleaned.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_smoke.py — append


def test_run_bulk_creates_and_registers_each_card(monkeypatch):
    created_ids = iter(["vc_b1", "vc_b2"])
    fake = _FakeCards()

    def make(parent, name, balance_cents, valid_to, *, client=None):
        cid = next(created_ids)
        fake.calls.append(("create_card", parent, name, balance_cents))
        return _vcard(cid, name)

    fake.create_card = make
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    h._state_parent = "cc_1"  # not needed; pass parent_id directly
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14))
    assert h._created == ["vc_b1", "vc_b2"]
    assert any(r.name == "bulk" and r.passed for r in h.results)


def test_run_bulk_uses_smoke_prefix(monkeypatch):
    names = []
    fake = _FakeCards()

    def make(parent, name, balance_cents, valid_to, *, client=None):
        names.append(name)
        return _vcard(f"vc_{len(names)}", name)

    fake.create_card = make
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14))
    assert all(n.startswith(smoke.SMOKE_CARD_NAME_PREFIX) for n in names)
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
        for i in range(count):
            name = f"{SMOKE_CARD_NAME_PREFIX} bulk {i} {today.isoformat()}"
            card = create_card(parent, name, SMOKE_CARD_BALANCE_CENTS, valid_to)
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
part of the offline `pytest` suite and never runs in CI.

## Why it exists

Every test under `tests/` runs offline against fakes, so nothing in the suite ever
talks to Extend. That is deliberate, but it means the suite cannot catch the code
disagreeing with what the live API actually returns. The v0.1.0 login bug passed
all unit tests and still failed on the first real login. This harness is the layer
that would have caught it.

## What it does

It creates one real virtual card at **$110.01** (`balanceCents = 11001`, a
distinctive amount), named `extendvcc-smoke <date>`, then walks: list accounts,
list issuers and parent cards, create, fetch, list, reveal (validated, never
printed), update, usage, cancel, close. A `finally` block cancels and closes the
card even if a step fails, so a card is never left open. If cleanup itself fails it
prints a loud warning naming the card id and exits non-zero.

## Run it

```bash
# Requires the same credentials the CLI uses:
#   EXTENDVCC_EMAIL, EXTENDVCC_PASSWORD, EXTENDVCC_IMAP_* (for first-time login)
uv run python scripts/smoke_test.py            # prompts before touching the account
uv run python scripts/smoke_test.py --yes      # skip the prompt (scripted local run)
uv run python scripts/smoke_test.py --json     # machine-readable report
uv run python scripts/smoke_test.py --parent cc_xxx   # pick a specific parent card
uv run python scripts/smoke_test.py --bulk 3   # also create/close 3 cards via bulk
```

Exit code is `0` only if every check passed and the test card was cleaned up.

## Coverage map

| Command / method | Covered by | Notes |
|---|---|---|
| `login` / session refresh | `accounts` step (first auth call) | full login if no session |
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
| `bulk` / `create_cards_bulk` path | `--bulk K` | opt-in |
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

**Spec coverage:** every spec section maps to a task. Lifecycle walk → Task 8; cleanup guarantee → Tasks 4 and 9; safety controls (confirm, min footprint, no secrets printed) → Tasks 6, 9, and the reveal step in 8; output and exit codes → Task 5; coverage map and exclusions → Task 11; bulk option → Task 10; harness unit tests → Tasks 2-10; file layout → all tasks; non-goals (no CI, no enroll/activate) → documented in Task 11 and not implemented. No gaps.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; commands have expected output.

**Type consistency:** `Harness(clock=...)`, `harness.step(name, fn)`, `register_created`, `cleanup(cancel=, close=, warn=)`, `run_lifecycle(harness, *, parent_id, today)`, `run_bulk(harness, *, parent_id, count, today)`, `select_parent(cards, *, requested)`, `exit_code(results, *, leftovers)`, `format_summary(results, *, planned)`, `json_report(...)`, `parse_args`, `confirm(assume_yes=, reader=)`, `main(argv)` — names are consistent across tasks. Card functions are bound at module scope (Task 8) so tests monkeypatch them on the `smoke` module. The `_monotonic` and `_read_confirm` seams (Task 9) are patched by name in the same task's tests.
