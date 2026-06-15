from __future__ import annotations

from datetime import date, datetime, timezone

from extendvcc.models import CardStatus, CreditCard, Issuer, VirtualCard


def test_card_status_values() -> None:
    assert [status.value for status in CardStatus] == [
        "ACTIVE",
        "CANCELLED",
        "EXPIRED",
        "PENDING",
        "CLOSED",
        "CONSUMED",
        "NOT_APPLICABLE",
    ]


def test_card_status_parses_not_applicable() -> None:
    # A freshly enrolled parent card sits in NOT_APPLICABLE until the issuer's
    # email verification completes; parsing it must not raise.
    assert CardStatus("NOT_APPLICABLE") is CardStatus.NOT_APPLICABLE


def test_virtual_card_dataclass_fields() -> None:
    card = VirtualCard(
        id="vc_123",
        credit_card_id="cc_123",
        name="Service-001",
        last4="1234",
        status=CardStatus.ACTIVE,
        balance_cents=5000,
        valid_from=date(2026, 5, 30),
        valid_to=date(2027, 1, 1),
        notes="Account: test",
        created_at=datetime(2026, 5, 30, 18, 0, tzinfo=timezone.utc),
    )

    assert card.id == "vc_123"
    assert card.credit_card_id == "cc_123"
    assert card.status is CardStatus.ACTIVE
    assert card.balance_cents == 5000


def test_issuer_dataclass_fields() -> None:
    issuer = Issuer(id="iss_1", name="American Express", code="AMEX")

    assert issuer.id == "iss_1"
    assert issuer.name == "American Express"
    assert issuer.code == "AMEX"


def test_credit_card_dataclass_fields() -> None:
    card = CreditCard(
        id="cc_123",
        last4="9876",
        status=CardStatus.ACTIVE,
        display_name="Amex 9876",
    )

    assert card.id == "cc_123"
    assert card.display_name == "Amex 9876"
