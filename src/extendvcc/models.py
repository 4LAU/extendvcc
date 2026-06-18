"""PayWithExtend data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CardStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"
    CLOSED = "CLOSED"
    CONSUMED = "CONSUMED"
    # A newly enrolled parent card sits here until the issuer's email
    # verification completes (observed on Amex enrollment 2026-06-14).
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class VirtualCard:
    id: str
    credit_card_id: str
    name: str
    last4: str
    status: CardStatus
    balance_cents: int
    valid_from: date | None
    valid_to: date | None
    notes: str | None
    created_at: datetime | None
    # Optional spend breakdown — present on the single-card GET, often omitted
    # from list responses, so all three default to None. ``balance_cents`` is the
    # available balance; ``limit_cents`` is the total limit; ``spent_cents`` is
    # settled spend. Pending authorizations (holds) are derived, not returned —
    # see ``cards.held_cents``.
    limit_cents: int | None = None
    spent_cents: int | None = None
    lifetime_spent_cents: int | None = None


@dataclass(frozen=True, slots=True)
class Recurrence:
    """Recurring virtual-card reset schedule (Extend's ``recurrence`` object).

    All three periods and all three terminators are captured from live traffic.

    Fields:
        period:       "DAILY" | "WEEKLY" | "MONTHLY".
        interval:     reset every N periods (>= 1).
        terminator:   "NONE" (never ends) | "DATE" (ends on ``until``) |
                      "COUNT" (ends after ``count`` resets).
        by_month_day: MONTHLY only — day-of-month (1..31) the limit resets on.
        by_week_day:  WEEKLY only — day index (0..6, the UI's own index).
        until:        DATE terminator only — "YYYY-MM-DD" the recurrence stops.
        count:        COUNT terminator only — number of resets before stopping.
    """

    period: str
    interval: int = 1
    terminator: str = "NONE"
    by_month_day: int | None = None
    by_week_day: int | None = None
    until: str | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True)
class Issuer:
    id: str
    name: str
    code: str


@dataclass(frozen=True, slots=True)
class CreditCard:
    id: str
    last4: str
    status: CardStatus
    display_name: str
