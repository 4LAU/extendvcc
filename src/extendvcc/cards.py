"""PayWithExtend card operations — reads, mutations, and account context helpers."""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from . import auth, ledger
from .client import PayWithExtendAPIError, PayWithExtendError
from .models import CardStatus, CreditCard, Issuer, Recurrence, VirtualCard

logger = logging.getLogger(__name__)

# Fields that may be included in a PUT /virtualcards/{id} body (allowlist).
UPDATE_PAYLOAD_FIELDS = (
    "creditCardId",
    "displayName",
    "expenseDetails",
    "balanceCents",
    "recurs",
    "receiptAttachmentIds",
    "validTo",
    "currency",
    "receiptRulesExempt",
    "lowLimitAlert",
)

_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Private parse helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except (ValueError, TypeError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _require(data: Any, key: str, context: str) -> Any:
    """Return ``data[key]`` or raise a typed error naming the missing field.

    Turns an opaque ``KeyError``/``TypeError`` from an unexpected Extend
    response shape into an actionable ``PayWithExtendError``.
    """
    try:
        return data[key]
    except (KeyError, TypeError) as exc:
        raise PayWithExtendError(f"unexpected Extend {context}: missing field {key!r}") from exc


def _map_virtual_card(card: dict[str, Any]) -> VirtualCard:
    try:
        return VirtualCard(
            id=card["id"],
            credit_card_id=card["creditCardId"],
            name=card["displayName"],
            last4=card["last4"],
            status=CardStatus(card["status"]),
            balance_cents=card["balanceCents"],
            valid_from=_parse_date(card.get("validFrom")),
            valid_to=_parse_date(card.get("validTo")),
            notes=None,
            created_at=_parse_datetime(card.get("createdAt")),
            # Spend breakdown is absent from some list responses; use .get so a
            # missing field maps to None instead of crashing the whole listing.
            limit_cents=card.get("limitCents"),
            spent_cents=card.get("spentCents"),
            lifetime_spent_cents=card.get("lifetimeSpentCents"),
        )
    except (KeyError, TypeError) as exc:
        raise PayWithExtendError(f"unexpected virtual card shape: missing field {exc}") from exc


def held_cents(card: VirtualCard) -> int | None:
    """Pending authorizations (holds) on a card, in cents.

    Extend reports the available balance, the total limit, and settled spend,
    but not holds directly. A hold is the gap between them:

        held = limit - settled spend - available balance

    Returns None when ``limit_cents`` is unknown (e.g. a list response that
    omitted it), since the hold cannot be derived without the limit.
    """
    if card.limit_cents is None:
        return None
    return card.limit_cents - (card.spent_cents or 0) - card.balance_cents


def _default_client() -> Any:
    from .client import PayWithExtendClient

    return PayWithExtendClient()


def _format_date(value: Any) -> str:
    """Format a date/datetime/str as YYYY-MM-DD."""
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value
    raise TypeError(f"_format_date: unsupported type {type(value).__name__}")


def _card_to_ledger_dict(card: VirtualCard) -> dict[str, Any]:
    """Convert a VirtualCard to a JSON-safe dict suitable for the ledger.

    The ledger's JSON serializer cannot handle date/datetime objects so we
    convert them to ISO strings here. No PAN/CVC data is ever present on a
    VirtualCard — mutations do not return secrets.
    """
    return {
        "id": card.id,
        "credit_card_id": card.credit_card_id,
        "name": card.name,
        "last4": card.last4,
        "status": str(card.status),
        "balance_cents": card.balance_cents,
        "valid_from": card.valid_from.isoformat() if card.valid_from is not None else None,
        "valid_to": card.valid_to.isoformat() if card.valid_to is not None else None,
        "notes": card.notes,
        "created_at": card.created_at.isoformat() if card.created_at is not None else None,
    }


def _virtual_card_success(resp: Any) -> tuple[Any, dict[str, Any]]:
    """Default ``on_success``: map a virtual-card response and ledger the card row."""
    card = _map_virtual_card(_require(resp, "virtualCard", "mutation response"))
    return card, {"card_record": _card_to_ledger_dict(card)}


def _ledger_flow(intent: str, key: str, dispatch: Any, on_success: Any = None) -> Any:
    """Record pending, dispatch, resolve. Leave pending on 5xx/network failure.

    ``on_success(resp)`` returns ``(return_value, resolve_fields)``. It defaults
    to the virtual-card mapping used by create/update/cancel/close; enrollment
    passes its own mapper. A 4xx marks the pending row failed; any other failure
    (kill switch, timeout, network) leaves it pending as local evidence.
    """
    if on_success is None:
        on_success = _virtual_card_success
    ledger.record_pending(intent, key)
    try:
        resp = dispatch()
    except PayWithExtendAPIError as exc:
        if 400 <= exc.status_code < 500:
            ledger.resolve_pending(key, "failed", error=str(exc))
        raise
    result, resolve_fields = on_success(resp)
    ledger.resolve_pending(key, "confirmed", **resolve_fields)
    return result


def account_context() -> dict[str, Any]:
    """Return ``{"email": str, "org_id": str | None}`` from the saved session.

    Deliberately avoids ``read_credentials``, ``authenticate``, and
    ``ensure_valid_token`` so the account password is never touched. This refreshes
    unconditionally via the refresh token (which never reads the password); that is
    an intentional safety trade-off, not an oversight — see the password-free tests.

    Raises:
        auth.SessionNotFound: if no session (or no email) exists.
        client.PayWithExtendDisabled: if the kill switch is set (propagated
            from ``refresh_tokens`` / ``fetch_current_user``).
    """
    session = auth.load_session()
    if not session or not session.get("email"):
        raise auth.SessionNotFound("PayWithExtend setup required — run setup()")

    refreshed = auth.refresh_tokens(session)
    email: str = refreshed.get("email") or session["email"]
    access_token: str = refreshed["access_token"]

    org_id: str | None = refreshed.get("org_id") or session.get("org_id")
    if not org_id:
        payload, _ = auth.fetch_current_user(access_token)
        org_id = auth.extract_org_id(payload)
        if org_id:
            updated = {**refreshed, "org_id": org_id}
            auth.save_session(updated)

    return {"email": email, "org_id": org_id}


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------


def list_credit_cards(*, client: Any = None) -> list[CreditCard]:
    """Return all credit cards on the account. GET /creditcards."""
    c = client or _default_client()
    data = c.get("/creditcards")
    try:
        return [
            CreditCard(
                id=card["id"],
                last4=card["last4"],
                status=CardStatus(card["status"]),
                display_name=card["displayName"],
            )
            for card in data.get("creditCards", [])
        ]
    except (KeyError, TypeError) as exc:
        raise PayWithExtendError(f"unexpected credit card shape: missing field {exc}") from exc


def list_issuers(*, client: Any = None) -> list[Issuer]:
    """Return all issuers. GET /issuers."""
    c = client or _default_client()
    data = c.get("/issuers")
    try:
        return [Issuer(id=issuer["id"], name=issuer["name"], code=issuer["code"]) for issuer in data.get("issuers", [])]
    except (KeyError, TypeError) as exc:
        raise PayWithExtendError(f"unexpected issuer shape: missing field {exc}") from exc


def list_cards(
    *,
    status: CardStatus | str | None = None,
    credit_card_id: str | None = None,
    client: Any = None,
) -> list[VirtualCard]:
    """Return all virtual cards, with optional status/credit_card_id filters.

    Uses page-based pagination (0-indexed ``page`` param). Safe to call with
    no arguments so ``ledger.sync(fetcher=cards.list_cards)`` works.
    """
    c = client or _default_client()
    params: dict[str, Any] = {"count": _PAGE_SIZE}
    if credit_card_id:
        params["creditCardId"] = credit_card_id
    if status is not None:
        params["statuses"] = status.value if hasattr(status, "value") else status

    results: list[VirtualCard] = []
    page = 0

    while True:
        data = c.get("/virtualcards", params={**params, "page": page})
        for card in data.get("virtualCards", []):
            results.append(_map_virtual_card(card))
        num_pages = (data.get("pagination") or {}).get("numberOfPages")
        if num_pages is None or page + 1 >= num_pages:
            break
        page += 1

    return results


def get_card(card_id: str, *, client: Any = None) -> VirtualCard:
    """Return a single virtual card by id. GET /virtualcards/{id}."""
    c = client or _default_client()
    data = c.get(f"/virtualcards/{card_id}")
    return _map_virtual_card(_require(data, "virtualCard", "get_card response"))


def usage(*, client: Any = None) -> dict[str, int]:
    """Return active virtual card usage against the account limit.

    Returns ``{"used": int, "remaining": int, "limit": int}``.

    Raises:
        ValueError: if org_id cannot be resolved from the session.
    """
    ctx = account_context()
    org_id = ctx.get("org_id")
    if not org_id:
        raise ValueError("org_id is not available in the session — run account_context() setup first")

    c = client or _default_client()
    data = c.get(f"/saas/{org_id}/usages")
    try:
        feature = data["features"]["ACTIVE_VIRTUAL_CARD_LIMIT"]
        entitlement: int = feature["entitlement"]
        used: int = feature["usage"]
    except (KeyError, TypeError) as exc:
        raise PayWithExtendError(f"unexpected usage response shape: missing field {exc}") from exc
    return {"used": used, "remaining": entitlement - used, "limit": entitlement}


# ---------------------------------------------------------------------------
# Mutation functions
# ---------------------------------------------------------------------------

_RECURRENCE_PERIODS = ("DAILY", "WEEKLY", "MONTHLY")
_RECURRENCE_TERMINATORS = ("NONE", "DATE", "COUNT")


def _recurrence_payload(rec: Recurrence, balance_cents: int) -> dict[str, Any]:
    """Build Extend's ``recurrence`` object from a validated ``Recurrence``.

    Shapes match captured live requests: DAILY (no day field), WEEKLY
    (``byWeekDay``), MONTHLY (``byMonthDay``); terminators NONE, DATE (``until``),
    COUNT (``count``). Invalid combinations raise before any API call.
    """
    if rec.period not in _RECURRENCE_PERIODS:
        raise ValueError(f"recurrence period must be one of {_RECURRENCE_PERIODS}, got {rec.period!r}")
    if rec.terminator not in _RECURRENCE_TERMINATORS:
        raise ValueError(f"recurrence terminator must be one of {_RECURRENCE_TERMINATORS}, got {rec.terminator!r}")
    if rec.interval < 1:
        raise ValueError("recurrence interval must be >= 1")
    payload: dict[str, Any] = {
        "balanceCents": balance_cents,
        "period": rec.period,
        "interval": rec.interval,
        "terminator": rec.terminator,
    }
    if rec.period == "MONTHLY":
        if rec.by_month_day is None or not 1 <= rec.by_month_day <= 31:
            raise ValueError("MONTHLY recurrence requires by_month_day in 1..31")
        payload["byMonthDay"] = rec.by_month_day
    elif rec.period == "WEEKLY":
        if rec.by_week_day is None or not 0 <= rec.by_week_day <= 6:
            raise ValueError("WEEKLY recurrence requires by_week_day in 0..6")
        payload["byWeekDay"] = rec.by_week_day

    if rec.terminator == "DATE":
        if not rec.until:
            raise ValueError("DATE terminator requires until ('YYYY-MM-DD')")
        try:
            date.fromisoformat(rec.until)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"DATE terminator until is invalid: {exc}") from exc
        payload["until"] = rec.until
    elif rec.terminator == "COUNT":
        if rec.count is None or rec.count < 1:
            raise ValueError("COUNT terminator requires count >= 1")
        payload["count"] = rec.count
    return payload


def build_create_card_operation(
    credit_card_id: str,
    name: str,
    balance_cents: int,
    valid_to: Any = None,
    *,
    recurrence: Recurrence | None = None,
    recipient_resolver: Callable[[], str],
    token_factory: Callable[[], str],
) -> dict[str, Any]:
    """Shape a ``POST /virtualcards`` operation without dispatching it.

    Returns an operation descriptor used by both the real ``create_card`` path
    and the CLI dry-run path so the request body has a single source of truth.
    The UUID correlation suffix is baked into the returned descriptor (not
    regenerated per call) so a dry-run preview matches what a real run would send.

    Args:
        recipient_resolver: callable returning the recipient email. Real runs pass
            an ``account_context()`` lookup; dry-run passes a network-free resolver.
        token_factory: callable returning the short correlation token.
    """
    if (valid_to is None) == (recurrence is None):
        raise ValueError("create_card: provide exactly one of valid_to or recurrence")
    correlation_name = f"{name} [{token_factory()}]"
    body: dict[str, Any] = {
        "creditCardId": credit_card_id,
        "displayName": correlation_name,
        "expenseDetails": [],
        "balanceCents": balance_cents,
        "currency": "USD",
        "receiptAttachmentIds": [],
        "lowLimitAlert": {"alertEnabled": False, "amountThresholdCents": None},
        "recipient": recipient_resolver(),
    }
    if recurrence is not None:
        body["recurs"] = True
        body["recurrence"] = _recurrence_payload(recurrence, balance_cents)
    else:
        body["validTo"] = _format_date(valid_to)
    return {
        "method": "POST",
        "path": "/virtualcards",
        "body": body,
        "correlation_key": correlation_name,
        "preview_accuracy": "exact",
    }


def create_card(
    credit_card_id: str,
    name: str,
    balance_cents: int,
    valid_to: Any = None,
    *,
    recipient: str | None = None,
    recurrence: Recurrence | None = None,
    client: Any = None,
) -> VirtualCard:
    """Create a virtual card and record it in the ledger.

    Provide exactly one of ``valid_to`` (one-time card that expires on a date) or
    ``recurrence`` (limit auto-resets each period). The ``displayName`` sent to
    Extend is suffixed with a short UUID token so that a timed-out create can be
    matched by name during ``reconcile()``.
    """
    c = client or _default_client()
    operation = build_create_card_operation(
        credit_card_id,
        name,
        balance_cents,
        valid_to,
        recurrence=recurrence,
        recipient_resolver=lambda: recipient or account_context()["email"],
        token_factory=lambda: uuid.uuid4().hex[:8],
    )
    correlation_name = operation["correlation_key"]
    body = operation["body"]
    return _ledger_flow("create", correlation_name, lambda: c.post("/virtualcards", json_body=body))


_BULK_REQUIRED_FIELDS = ("name", "balance_cents", "valid_to")


def _validate_bulk_row(index: int, row: dict[str, Any]) -> None:
    """Validate a bulk row's shape before any card is created.

    Checks presence *and* well-formedness of the structured fields so a malformed
    row fails the whole batch up front, rather than half-creating the set when a
    bad value only blows up inside ``create_card`` mid-loop.
    """
    missing = [f for f in _BULK_REQUIRED_FIELDS if row.get(f) is None]
    if missing:
        raise ValueError(f"create_cards_bulk: row {index} missing required field(s): {missing}")
    if not isinstance(row["name"], str) or not row["name"].strip():
        raise ValueError(f"create_cards_bulk: row {index} 'name' must be a non-empty string")
    if not isinstance(row["balance_cents"], int) or isinstance(row["balance_cents"], bool):
        raise ValueError(f"create_cards_bulk: row {index} 'balance_cents' must be an int")
    try:
        if isinstance(row["valid_to"], str):
            date.fromisoformat(row["valid_to"])
        else:
            _format_date(row["valid_to"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"create_cards_bulk: row {index} 'valid_to' is invalid: {exc}") from exc


def create_cards_bulk(
    credit_card_id: str,
    rows: Sequence[dict[str, Any]],
    *,
    delay_seconds: float = 2.0,
    jitter_seconds: float = 0.75,
    min_delay_seconds: float = 0.5,
    rng: Callable[[float, float], float] = random.gauss,
    sleeper: Callable[[float], None] = time.sleep,
    client: Any = None,
) -> list[VirtualCard]:
    """Create many virtual cards by looping ``create_card`` with a paced delay.

    This deliberately does NOT use Extend's native bulk-upload endpoint (which is
    async and returns no card ids — see the plan's 4.1). Each row goes through
    ``create_card`` so every card is returned synchronously and ledgered.

    Pacing: between cards (never before the first, never after the last) sleep a
    Gaussian delay ``rng(delay_seconds, jitter_seconds)`` clamped to
    ``>= min_delay_seconds`` so the left tail of the distribution can never burst.
    Set ``delay_seconds=0`` to disable pacing entirely (used by tests).

    Args:
        rows: dicts with required ``name``, ``balance_cents``, ``valid_to`` and an
            optional ``recipient``. Every row is validated before any card is
            created, so a malformed row fails the batch before it touches Extend.

    Fail-fast: if a ``create_card`` raises, the exception propagates immediately;
    cards created before it remain in the ledger as durable evidence.
    """
    c = client or _default_client()

    # Pre-validate the whole batch so a bad row doesn't half-create the set.
    for index, row in enumerate(rows):
        _validate_bulk_row(index, row)

    created: list[VirtualCard] = []
    for index, row in enumerate(rows):
        if index > 0 and delay_seconds > 0:
            sleeper(max(min_delay_seconds, rng(delay_seconds, jitter_seconds)))
        created.append(
            create_card(
                credit_card_id,
                row["name"],
                row["balance_cents"],
                row["valid_to"],
                recipient=row.get("recipient"),
                client=c,
            )
        )
    return created


def build_update_card_operation(
    card_id: str,
    overrides: dict[str, Any],
    *,
    fetcher: Callable[[], Any],
) -> dict[str, Any]:
    """Shape a ``PUT /virtualcards/{id}`` operation via read-modify-write.

    ``fetcher`` performs the read-only GET of the current card; its result is
    projected to the update allowlist, non-allowlist fields are warned about and
    dropped, then ``overrides`` are applied. Shared by ``update_card`` and the
    CLI dry-run path so the PUT body is shaped identically in both.
    """
    raw = _require(fetcher(), "virtualCard", "update_card GET response")

    # Project to allowlist.
    payload: dict[str, Any] = {k: raw[k] for k in UPDATE_PAYLOAD_FIELDS if k in raw}

    # Warn about dropped non-allowlist fields.
    dropped = [k for k in raw if k not in UPDATE_PAYLOAD_FIELDS]
    if dropped:
        logger.warning(
            "update_card dropping non-allowlist GET fields for %s: %s",
            card_id,
            sorted(dropped),
        )

    # Apply overrides.
    payload.update(overrides)

    return {
        "method": "PUT",
        "path": f"/virtualcards/{card_id}",
        "body": payload,
        "preview_accuracy": "exact",
    }


# A GET /creditcards/{id} that returns only these keys is the "thin" list-item
# shape, not the full card object. Round-tripping it as a PUT body would blank
# every other field on the parent card, so we refuse it.
_THIN_CREDIT_CARD_KEYS = frozenset({"id", "last4", "status", "displayName"})


def build_update_credit_card_operation(
    credit_card_id: str,
    overrides: dict[str, Any],
    *,
    fetcher: Callable[[], Any],
) -> dict[str, Any]:
    """Shape a ``PUT /creditcards/{id}`` operation via full-object read-modify-write.

    ``fetcher`` performs the read-only GET of the current card. Its result (wrapped
    in ``creditCard`` or bare) is round-tripped byte-for-byte as the PUT body, then
    ``overrides`` are applied: a dict-valued override is **merged** one level deep
    into the existing field (so the nested ``address`` keeps unknown keys like
    ``countryCode``); any other value replaces.

    Faithful to the captured browser request, which PUTs the whole object and
    changes only the nested ``address``. The credit-card object carries no PAN/CVC,
    so a full round-trip leaks nothing. Note: a full-object PUT is last-writer-wins
    for the entire object — a concurrent edit to any field would be reverted.

    Raises:
        PayWithExtendError: if the GET returns the thin list-item shape (which would
            make the round-trip unsafe) or an otherwise unrecognizable object.
    """
    resp = fetcher()
    raw = resp.get("creditCard", resp) if isinstance(resp, dict) else None
    if not isinstance(raw, dict) or "id" not in raw:
        raise PayWithExtendError("unexpected update_credit_card GET response: not a card object")
    if set(raw.keys()) <= _THIN_CREDIT_CARD_KEYS:
        raise PayWithExtendError(
            f"GET /creditcards/{credit_card_id} returned a thin object; "
            "full-object round-trip is unsafe — aborting to avoid blanking the parent card"
        )

    body = dict(raw)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value

    return {
        "method": "PUT",
        "path": f"/creditcards/{credit_card_id}",
        "body": body,
        "preview_accuracy": "exact",
    }


_CREDIT_CARD_ADDRESS_REQUIRED = ("address1", "city", "province", "postal")


def _require_address_fields(address: dict[str, Any]) -> None:
    """Raise ValueError naming any missing or empty required billing-address field.

    Shared by the library path and the CLI dry-run so a preview enforces the same
    contract as a real run (argparse ``required=True`` guarantees presence, not
    non-emptiness — ``--address1 ""`` must still be rejected).
    """
    missing = [f for f in _CREDIT_CARD_ADDRESS_REQUIRED if not address.get(f)]
    if missing:
        raise ValueError(f"update_credit_card_address: address missing required field(s): {missing}")


def _credit_card_address_overrides(address: dict[str, Any], country: str | None) -> dict[str, Any]:
    """Build the PUT overrides for an address change (shared by lib + CLI dry-run).

    Returns a nested ``address`` override (merged over the GET's address by the
    builder). When ``country`` is given it is set both inside the nested address
    and at the top level, matching where the live object carries it.
    """
    new_address: dict[str, Any] = {
        "address1": address["address1"],
        "address2": address.get("address2", "") or "",
        "city": address["city"],
        "province": address["province"],
        "postal": address["postal"],
    }
    overrides: dict[str, Any] = {"address": new_address}
    if country is not None:
        new_address["country"] = country
        overrides["country"] = country
    return overrides


def update_credit_card_address(
    credit_card_id: str,
    address: dict[str, Any],
    *,
    country: str | None = None,
    client: Any = None,
) -> CreditCard:
    """Update a parent (SOURCE) credit card's billing address. PUT /creditcards/{id}.

    Full-object read-modify-write: GET the card, override only the nested ``address``
    object (merged, so unknown keys survive), round-trip every other field unchanged,
    PUT it back. ``address`` requires ``address1``, ``city``, ``province``, ``postal``
    and accepts an optional ``address2`` (defaults ``""``). ``postal`` must stay a
    string so leading-zero ZIPs survive.

    AVS caveat: this updates the *stored* address. Whether that address reaches the
    issuer's address-verification check at checkout is unverified — confirm against a
    live transaction before relying on it for AVS.

    Raises:
        ValueError: if a required address field is missing (before any network call).
        PayWithExtendError: if the GET returns a thin/unrecognizable object.
    """
    _require_address_fields(address)

    c = client or _default_client()
    overrides = _credit_card_address_overrides(address, country)
    operation = build_update_credit_card_operation(
        credit_card_id,
        overrides,
        fetcher=lambda: c.get(f"/creditcards/{credit_card_id}"),
    )
    payload = operation["body"]

    def _on_success(resp: Any) -> tuple[CreditCard, dict[str, Any]]:
        credit_card = _parse_credit_card(resp, "update_credit_card_address response")
        return credit_card, {"credit_card_id": credit_card.id}

    key = f"update-cc:{credit_card_id}"
    return _ledger_flow(
        "update-cc",
        key,
        lambda: c.put(f"/creditcards/{credit_card_id}", json_body=payload),
        on_success=_on_success,
    )


def _update_overrides(
    *,
    balance_cents: int | None,
    name: str | None,
    valid_to: Any,
    recurs: bool | None,
) -> dict[str, Any]:
    """Translate public update kwargs to the API field overrides (non-None only)."""
    overrides: dict[str, Any] = {}
    if name is not None:
        overrides["displayName"] = name
    if balance_cents is not None:
        overrides["balanceCents"] = balance_cents
    if valid_to is not None:
        overrides["validTo"] = _format_date(valid_to)
    if recurs is not None:
        overrides["recurs"] = recurs
    return overrides


def update_card(
    card_id: str,
    *,
    balance_cents: int | None = None,
    name: str | None = None,
    valid_to: Any = None,
    recurs: bool | None = None,
    client: Any = None,
) -> VirtualCard:
    """Update a virtual card using read-modify-write against the allowlist.

    Only the fields passed as non-None kwargs are overridden. All other
    allowlist fields from the current remote state are preserved.
    """
    c = client or _default_client()
    overrides = _update_overrides(
        balance_cents=balance_cents,
        name=name,
        valid_to=valid_to,
        recurs=recurs,
    )
    operation = build_update_card_operation(
        card_id,
        overrides,
        fetcher=lambda: c.get(f"/virtualcards/{card_id}"),
    )
    payload = operation["body"]

    key = f"update:{card_id}"
    return _ledger_flow("update", key, lambda: c.put(f"/virtualcards/{card_id}", json_body=payload))


def cancel_card(card_id: str, *, client: Any = None) -> VirtualCard:
    """Cancel a virtual card (reversible). PUT /virtualcards/{id}/cancel."""
    c = client or _default_client()
    key = f"cancel:{card_id}"
    return _ledger_flow("cancel", key, lambda: c.put(f"/virtualcards/{card_id}/cancel"))


def close_card(card_id: str, *, client: Any = None) -> VirtualCard:
    """Close a virtual card permanently. PUT /virtualcards/{id}/close."""
    c = client or _default_client()
    key = f"close:{card_id}"
    return _ledger_flow("close", key, lambda: c.put(f"/virtualcards/{card_id}/close"))


def reconcile(*, client: Any = None) -> dict[str, list[str]]:
    """Resolve stale pending create rows by matching against remote cards by name.

    For each pending create row, if a remote card's name matches the correlation
    key (the unique suffixed displayName), the row is confirmed. Otherwise it is
    marked failed so a retry is safe.

    Returns ``{"adopted": [card_ids], "failed": [correlation_keys]}``.
    """
    c = client or _default_client()
    pendings = ledger.list_pending(intent="create")
    remote = list_cards(client=c)
    remote_by_name = {card.name: card for card in remote}

    adopted: list[str] = []
    failed: list[str] = []

    for row in pendings:
        key = row["correlation_key"]
        remote_card = remote_by_name.get(key)
        if remote_card is not None:
            ledger.resolve_pending(key, "confirmed", card_record=_card_to_ledger_dict(remote_card))
            adopted.append(remote_card.id)
        else:
            ledger.resolve_pending(key, "failed", error="reconcile: no remote card matched")
            failed.append(key)

    return {"adopted": adopted, "failed": failed}


def _parse_credit_card(
    resp: Any,
    context: str,
    *,
    fallback: CreditCard | None = None,
    fallback_status: CardStatus | None = None,
) -> CreditCard:
    """Build a CreditCard from a response that may or may not wrap it in ``creditCard``.

    If the response lacks the expected fields but a ``fallback`` card + status are
    given (e.g. a 200 with an unexpected body after a known state transition),
    return the fallback card with the new status rather than failing.
    """
    data = resp.get("creditCard", resp) if isinstance(resp, dict) else None
    if isinstance(data, dict) and {"id", "last4", "status", "displayName"} <= data.keys():
        return CreditCard(
            id=data["id"],
            last4=data["last4"],
            status=CardStatus(data["status"]),
            display_name=data["displayName"],
        )
    if fallback is not None and fallback_status is not None:
        return CreditCard(
            id=fallback.id,
            last4=fallback.last4,
            status=fallback_status,
            display_name=fallback.display_name,
        )
    raise PayWithExtendError(f"unexpected {context} shape")


def enroll_credit_card(
    display_name: str,
    card_number: str,
    expires: Any,
    cvc: str,
    cardholder_name: str,
    issuer_id: str,
    address: dict[str, Any],
    *,
    company_name: str | None = None,
    country: str = "US",
    client: Any = None,
) -> CreditCard:
    """Enroll a parent credit card and start its verification (steps 1+2).

    Step 1: ``POST /creditcardsv2`` (vault host) registers the card — it lands in
    NOT_APPLICABLE. Step 2: ``PUT /creditcardsv2/{id}/virtual`` (api host) enables
    virtual cards, advancing it to PENDING and triggering the issuer's
    cardholder-verification email. Returns the card in PENDING. After the
    cardholder verifies by email, call ``activate_credit_card`` to reach ACTIVE.

    ``card_number`` (PAN) and ``cvc`` are sent only in the HTTPS request body
    and are never logged or written to the ledger.

    Raises:
        ValueError: if org_id cannot be resolved from the session.
    """
    from .client import vault_client

    ctx = account_context()
    org_id = ctx.get("org_id")
    if not org_id:
        raise ValueError("org_id is not available in the session — run account_context() setup first")

    body = {
        "displayName": display_name,
        "country": country,
        "companyName": company_name or cardholder_name,
        "cardholderName": cardholder_name,
        "address1": address["address1"],
        "address2": address.get("address2", ""),
        "city": address["city"],
        "postal": address["postal"],
        "province": address["province"],
        "cvc": cvc,
        "issuerId": issuer_id,
        "issuerFields": {"cardNumber": card_number, "expires": _format_date(expires)},
        "organizationId": org_id,
    }

    vault_c = client or vault_client()  # step 1 (POST) lives on the vault host
    api_c = client or _default_client()  # step 2 (PUT .../virtual) lives on the api host
    key = f"enroll:{display_name} [{uuid.uuid4().hex[:8]}]"

    def _on_success(resp: Any) -> tuple[CreditCard, dict[str, Any]]:
        credit_card = _parse_credit_card(resp, "enroll response")
        return credit_card, {"credit_card_id": credit_card.id}

    # Step 1 — POST the card details. The card lands in NOT_APPLICABLE (Inactive).
    enrolled = _ledger_flow(
        "enroll", key, lambda: vault_c.post("/creditcardsv2", json_body=body), on_success=_on_success
    )
    # Step 2 — enable virtual cards. This advances NOT_APPLICABLE -> PENDING and
    # triggers the issuer's cardholder-verification email. Skipping it leaves the
    # card stuck Inactive with no email ever sent. Once the cardholder verifies,
    # call activate_credit_card() to pull PENDING -> ACTIVE.
    resp = api_c.put(f"/creditcardsv2/{enrolled.id}/virtual", json_body={})
    return _parse_credit_card(resp, "enable-virtual response", fallback=enrolled, fallback_status=CardStatus.PENDING)


def activate_credit_card(credit_card_id: str, *, client: Any = None) -> CreditCard:
    """Step 3 of enrollment: pull a verified parent card from PENDING to ACTIVE.

    After the cardholder clicks the issuer's verification email (the email that
    ``enroll_credit_card``'s step 2 triggers), this refreshes the card's status
    via ``PATCH /creditcards/{id}/status``. Returns the card's current state —
    still ``PENDING`` if verification has not completed, ``ACTIVE`` once it has.
    """
    c = client or _default_client()
    resp = c.patch(f"/creditcards/{credit_card_id}/status", json_body={})
    return _parse_credit_card(resp, "activate response")


def reveal_card(
    card_id: str,
    *,
    client: Any = None,
) -> dict[str, Any]:
    """Retrieve live card credentials from the vault host.

    Returns raw credentials in-memory: number, CVC, last4, and expiry.
    """
    from .client import vault_client

    c = client or vault_client()
    card = _require(c.get(f"/virtualcards/{card_id}"), "virtualCard", "reveal_card response")
    number: str = _require(card, "vcn", "reveal_card response")
    cvc: str = _require(card, "securityCode", "reveal_card response")
    expires: str | None = card.get("expires")
    last4: str | None = card.get("last4")

    return {"number": number, "cvc": cvc, "last4": last4, "expires": expires}
