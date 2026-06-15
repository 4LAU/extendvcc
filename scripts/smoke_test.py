"""Live smoke-test harness for extendvcc.

Drives the full card lifecycle against the REAL Extend API and cleans up after
itself. Run manually before a release. Never collected by pytest (it lives under
scripts/, not tests/) and never run in CI.

Usage:
    uv run python scripts/smoke_test.py [--yes] [--parent CARD_ID] [--bulk K] [--json]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

SMOKE_CARD_BALANCE_CENTS = 11001  # $110.01 — distinctive, easy to spot if cleanup fails
SMOKE_CARD_NAME_PREFIX = "extendvcc-smoke"


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
