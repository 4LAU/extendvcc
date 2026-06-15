"""Live smoke-test harness for extendvcc.

Drives the full card lifecycle against the REAL Extend API and cleans up after
itself. Run manually before a release. Never collected by pytest (it lives under
scripts/, not tests/) and never run in CI.

Usage:
    uv run python scripts/smoke_test.py [--yes] [--parent CARD_ID] [--bulk K] [--json]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from extendvcc import _exit_codes
from extendvcc.auth import PayWithExtendAuthError
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
from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError
from extendvcc.models import CardStatus, CreditCard

SMOKE_CARD_BALANCE_CENTS = 11001  # $110.01 — distinctive, easy to spot if cleanup fails
SMOKE_CARD_NAME_PREFIX = "extendvcc-smoke"
LIFECYCLE_STEPS = 10


class SmokeError(Exception):
    """A smoke-test precondition or assertion failed."""


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
                card = close(card_id)
            except Exception as exc:
                leftovers.append((card_id, repr(exc)))  # close raised -> not closed
            else:
                # A non-raising close is not proof: the live API may return 200 with
                # a non-CLOSED status (the exact drift this harness exists to catch).
                # `close_card` returns a VirtualCard; verify its status.
                status = getattr(card, "status", None)
                if status != CardStatus.CLOSED:
                    leftovers.append((card_id, f"close returned status {status!r}, expected CLOSED"))
        dollars = SMOKE_CARD_BALANCE_CENTS / 100
        for card_id, err in leftovers:
            warn(f"LEFTOVER smoke card {card_id} (${dollars:.2f}) not closed: {err} — close it manually")
        return leftovers


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
    # pointed at the real cause (mostly mirrors the CLI's exception->exit-code
    # mapping; base PayWithExtendError intentionally maps to EXIT_API_ERROR here,
    # not the CLI's EXIT_ERROR, so API drift is loud in a release smoke test).
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
            {"name": r.name, "passed": r.passed, "seconds": round(r.seconds, 3), "detail": r.detail} for r in results
        ],
        "created": list(created),
        "leftovers": [{"card_id": cid, "error": err} for cid, err in leftovers],
    }


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


def run_lifecycle(harness: Harness, *, parent_id: str | None, today: date, run_prefix: str) -> None:
    state: dict = {}

    def _accounts():
        state["ctx"] = account_context()

    def _issuers():
        list_issuers()
        state["parent"] = select_parent(list_credit_cards(), requested=parent_id)

    def _create():
        name = run_prefix  # unique per run; the discovery sweep matches on this exact prefix
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
        # Keep the run_prefix as a leading substring so the discovery sweep
        # (which matches name.startswith(run_prefix)) still finds the card after
        # a rename, and append a marker to prove the update applied.
        new_name = f"{run_prefix} updated"
        card = update_card(state["card_id"], name=new_name)
        if "updated" not in card.name:
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


def run_bulk(harness: Harness, *, parent_id: str | None, count: int, today: date, run_prefix: str) -> None:
    def _bulk():
        parent = parent_id or select_parent(list_credit_cards(), requested=None)
        valid_to = (today + timedelta(days=3)).isoformat()
        rows = [
            {
                "name": f"{run_prefix} bulk {i}",  # leads with the unique per-run prefix
                "balance_cents": SMOKE_CARD_BALANCE_CENTS,
                "valid_to": valid_to,
            }
            for i in range(count)
        ]
        # Drive the REAL public bulk helper so its prevalidation/pacing are covered.
        # delay_seconds=0 disables the inter-card sleep (see create_cards_bulk docstring).
        # create_cards_bulk is fail-fast: if card N raises, it propagates BEFORE
        # returning the list, so the already-created (N-1) ids would be lost.
        # We register whatever the helper returns; the Task 9 prefix-discovery sweep
        # is the backstop for ids never returned.
        result = create_cards_bulk(parent, rows, delay_seconds=0)
        for card in result:
            harness.register_created(card.id)

    harness.step("bulk", _bulk)
