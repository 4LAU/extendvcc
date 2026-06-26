"""Tests for extendvcc.cards account context and read functions."""

from __future__ import annotations

from typing import Any

import pytest

from extendvcc import cards, ledger
from extendvcc._paths import configure as configure_paths
from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError

_BASE_SESSION = {
    "email": "user@example.com",
    "access_token": "old_token",
    "refresh_token": "rt_abc",
    "client_id": "client123",
    "org_id": "org_abc",
}

_REFRESHED_SESSION = {
    **_BASE_SESSION,
    "access_token": "new_token",
}


def _patch_ledger(monkeypatch, tmp_path):
    """Configure ledger to use a temp path for the test."""
    configure_paths(ledger_path=tmp_path / "cards.jsonl")


# ---------------------------------------------------------------------------
# Password-free guarantee — read_credentials / ensure_valid_token /
# authenticate must never be invoked on either branch
# ---------------------------------------------------------------------------


def _bang(*args, **kwargs):
    raise AssertionError("must not be called")


def test_no_password_read_org_present(monkeypatch):
    monkeypatch.setattr(cards.auth, "load_session", lambda: dict(_BASE_SESSION))
    monkeypatch.setattr(cards.auth, "refresh_tokens", lambda session: dict(_REFRESHED_SESSION))
    monkeypatch.setattr(cards.auth, "fetch_current_user", _bang)
    monkeypatch.setattr(cards.auth, "read_credentials", _bang)
    monkeypatch.setattr(cards.auth, "ensure_valid_token", _bang)
    monkeypatch.setattr(cards.auth, "authenticate", _bang)

    # Should succeed without hitting any of the banned paths
    cards.account_context()


def test_no_password_read_org_absent(monkeypatch):
    session_without_org = {k: v for k, v in _BASE_SESSION.items() if k != "org_id"}
    refreshed_without_org = {**session_without_org, "access_token": "new_token"}

    monkeypatch.setattr(cards.auth, "load_session", lambda: dict(session_without_org))
    monkeypatch.setattr(cards.auth, "refresh_tokens", lambda session: dict(refreshed_without_org))
    monkeypatch.setattr(
        cards.auth,
        "fetch_current_user",
        lambda token, client=None: ({"orgId": "org_x"}, {}),
    )
    monkeypatch.setattr(cards.auth, "save_session", lambda s: None)
    monkeypatch.setattr(cards.auth, "read_credentials", _bang)
    monkeypatch.setattr(cards.auth, "ensure_valid_token", _bang)
    monkeypatch.setattr(cards.auth, "authenticate", _bang)

    cards.account_context()


# ---------------------------------------------------------------------------
# Fake client helper
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal fake client that records calls and returns canned responses."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, params))
        # Strip query params from path for lookup
        base_path = path.split("?")[0]
        if base_path in self._responses:
            return self._responses[base_path]
        raise KeyError(f"FakeClient: no response for path {path!r}")


# ---------------------------------------------------------------------------
# list_credit_cards
# ---------------------------------------------------------------------------


def test_list_credit_cards_parses_response():
    fake = _FakeClient(
        {
            "/creditcards": {
                "creditCards": [
                    {"id": "cc_test1", "last4": "1234", "status": "ACTIVE", "displayName": "Test Card A"},
                    {"id": "cc_test2", "last4": "5678", "status": "CANCELLED", "displayName": "Test Card B"},
                ]
            }
        }
    )
    result = cards.list_credit_cards(client=fake)

    assert len(result) == 2
    assert result[0].id == "cc_test1"
    assert result[0].last4 == "1234"
    assert result[0].status == cards.CardStatus.ACTIVE
    assert result[0].display_name == "Test Card A"
    assert result[1].id == "cc_test2"
    assert result[1].status == cards.CardStatus.CANCELLED


# ---------------------------------------------------------------------------
# list_cards — wire contract and pagination
# ---------------------------------------------------------------------------

_VC_TEMPLATE = {
    "id": "vc_test1",
    "creditCardId": "cc_test1",
    "displayName": "Test Virtual Card",
    "last4": "9999",
    "status": "ACTIVE",
    "balanceCents": 5000,
    "validFrom": "2025-01-01T00:00:00.000+0000",
    "validTo": "2026-01-01T00:00:00.000+0000",
    "createdAt": "2025-01-01T12:00:00.000+0000",
}


def test_list_cards_single_page():
    fake = _FakeClient(
        {
            "/virtualcards": {
                "pagination": {"numberOfPages": 1, "page": 0, "pageItemCount": 1, "totalItems": 1},
                "virtualCards": [_VC_TEMPLATE],
            }
        }
    )
    result = cards.list_cards(client=fake)

    assert len(result) == 1
    assert result[0].id == "vc_test1"
    assert result[0].notes is None


def test_list_cards_paginates():
    """Two pages — fake client must be called with page=0 then page=1."""
    page0_card = {**_VC_TEMPLATE, "id": "vc_page0"}
    page1_card = {**_VC_TEMPLATE, "id": "vc_page1"}

    class _PaginatingFake:
        def __init__(self):
            self.calls: list[tuple[str, dict | None]] = []

        def get(self, path, params=None):
            self.calls.append((path, dict(params) if params is not None else None))
            page = (params or {}).get("page", 0)
            if page == 0:
                return {
                    "pagination": {"numberOfPages": 2, "page": 0, "pageItemCount": 1, "totalItems": 2},
                    "virtualCards": [page0_card],
                }
            return {
                "pagination": {"numberOfPages": 2, "page": 1, "pageItemCount": 1, "totalItems": 2},
                "virtualCards": [page1_card],
            }

    fake = _PaginatingFake()
    result = cards.list_cards(client=fake)

    assert len(result) == 2
    assert result[0].id == "vc_page0"
    assert result[1].id == "vc_page1"
    assert fake.calls[0][1]["page"] == 0
    assert fake.calls[1][1]["page"] == 1


# ---------------------------------------------------------------------------
# get_card
# ---------------------------------------------------------------------------


def test_get_card_parses_virtual_card():
    from datetime import date

    fake = _FakeClient(
        {
            "/virtualcards/vc_test1": {
                "virtualCard": _VC_TEMPLATE,
            }
        }
    )
    result = cards.get_card("vc_test1", client=fake)

    assert result.id == "vc_test1"
    assert result.credit_card_id == "cc_test1"
    assert result.name == "Test Virtual Card"
    assert result.last4 == "9999"
    assert result.status == cards.CardStatus.ACTIVE
    assert result.balance_cents == 5000
    assert result.valid_from == date(2025, 1, 1)
    assert result.valid_to == date(2026, 1, 1)
    assert result.created_at.year == 2025
    assert result.notes is None


def test_map_virtual_card_maps_limit_spent_lifetime():
    """Wire contract: limit/spent amounts map to the right fields.

    A silent key mismatch (e.g. reading ``spentCents`` into ``limit_cents``)
    would surface plausible-but-wrong financial figures with no crash.
    """
    raw = {**_VC_TEMPLATE, "limitCents": 10100, "spentCents": 2500, "lifetimeSpentCents": 9000}
    card = cards._map_virtual_card(raw)
    assert card.limit_cents == 10100
    assert card.spent_cents == 2500
    assert card.lifetime_spent_cents == 9000


def test_held_cents_computes_pending_authorizations():
    """held = limit - settled spend - available balance.

    Wrong arithmetic here misreports financial exposure with plausible numbers.
    A $101 limit with a $100 pending hold and nothing settled = $100 held;
    once the $100 settles (spent=10000) the hold clears to $0.
    """
    from dataclasses import replace
    from datetime import date

    pending = cards.VirtualCard(
        id="vc_h",
        credit_card_id="cc_1",
        name="x",
        last4="1342",
        status=cards.CardStatus.ACTIVE,
        balance_cents=100,
        valid_from=date(2026, 1, 1),
        valid_to=date(2027, 12, 31),
        notes=None,
        created_at=None,
        limit_cents=10100,
        spent_cents=0,
        lifetime_spent_cents=0,
    )
    assert cards.held_cents(pending) == 10000

    settled = replace(pending, spent_cents=10000)
    assert cards.held_cents(settled) == 0


def test_held_cents_none_when_limit_unknown():
    """A list response that omits ``limitCents`` must yield held=None, not a
    bogus number computed against a missing limit."""
    from datetime import date

    card = cards.VirtualCard(
        id="vc_h2",
        credit_card_id="cc_1",
        name="x",
        last4="1342",
        status=cards.CardStatus.ACTIVE,
        balance_cents=100,
        valid_from=date(2026, 1, 1),
        valid_to=date(2027, 12, 31),
        notes=None,
        created_at=None,
    )
    assert card.limit_cents is None
    assert cards.held_cents(card) is None


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

_USAGE_RESPONSE = {
    "features": {
        "ACTIVE_VIRTUAL_CARD_LIMIT": {
            "entitlement": 100,
            "usage": 0,
            "withinLimit": True,
        },
        "OTHER_FEATURE": {
            "entitlement": 50,
            "usage": 10,
            "withinLimit": True,
        },
    }
}


def test_usage_returns_correct_dict(monkeypatch):
    monkeypatch.setattr(
        cards,
        "account_context",
        lambda: {"email": "test@example.com", "org_id": "org_test"},
    )
    fake = _FakeClient({"/saas/org_test/usages": _USAGE_RESPONSE})
    result = cards.usage(client=fake)

    assert result == {"used": 0, "remaining": 100, "limit": 100}
    assert fake.calls[0][0] == "/saas/org_test/usages"


# ---------------------------------------------------------------------------
# Mutation test helpers
# ---------------------------------------------------------------------------

# Safe synthetic card dict — non-Luhn last4, fake ids.
_SYNTH_CARD = {
    "id": "vc_synth1",
    "creditCardId": "cc_synth1",
    "displayName": "Test Card [abc12345]",
    "last4": "1234",
    "status": "ACTIVE",
    "balanceCents": 10000,
    "validFrom": "2026-01-01T00:00:00.000+0000",
    "validTo": "2026-06-15T00:00:00.000+0000",
    "createdAt": "2026-01-01T12:00:00.000+0000",
    "currency": "USD",
    "recurs": False,
    "receiptRulesExempt": False,
    "expenseDetails": [],
    "receiptAttachmentIds": [],
    "lowLimitAlert": {"alertEnabled": False, "amountThresholdCents": None},
}


class _MutatingFakeClient:
    """Fake client for mutation tests. Supports get/post/put with per-path canned responses."""

    def __init__(
        self,
        get_responses: dict | None = None,
        post_responses: dict | None = None,
        put_responses: dict | None = None,
        patch_responses: dict | None = None,
    ) -> None:
        self._get = get_responses or {}
        self._post = post_responses or {}
        self._put = put_responses or {}
        self._patch = patch_responses or {}
        self.get_calls: list[tuple[str, dict | None]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.put_calls: list[tuple[str, Any]] = []
        self.patch_calls: list[tuple[str, Any]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.get_calls.append((path, params))
        if path in self._get:
            return self._get[path]
        raise KeyError(f"FakeMutatingClient: no GET response for {path!r}")

    def post(self, path: str, *, json_body: Any = None, **_kw) -> dict:
        self.post_calls.append((path, json_body))
        if path in self._post:
            return self._post[path]
        raise KeyError(f"FakeMutatingClient: no POST response for {path!r}")

    def put(self, path: str, *, json_body: Any = None, **_kw) -> dict:
        self.put_calls.append((path, json_body))
        if path in self._put:
            return self._put[path]
        raise KeyError(f"FakeMutatingClient: no PUT response for {path!r}")

    def patch(self, path: str, *, json_body: Any = None, **_kw) -> dict:
        self.patch_calls.append((path, json_body))
        if path in self._patch:
            return self._patch[path]
        raise KeyError(f"FakeMutatingClient: no PATCH response for {path!r}")


# Shared response wrapper.
_CARD_RESP = {"virtualCard": _SYNTH_CARD}

# ---------------------------------------------------------------------------
# create_card
# ---------------------------------------------------------------------------


def test_create_card_body_shape(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "owner@example.com", "org_id": "org1"})

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    result = cards.create_card("cc_synth1", "My Card", 10000, "2026-06-15", client=fake)

    assert len(fake.post_calls) == 1
    path, body = fake.post_calls[0]
    assert path == "/virtualcards"
    assert body["creditCardId"] == "cc_synth1"
    assert body["currency"] == "USD"
    assert body["expenseDetails"] == []
    assert body["lowLimitAlert"] == {"alertEnabled": False, "amountThresholdCents": None}
    assert body["recipient"] == "owner@example.com"
    assert body["validTo"] == "2026-06-15"
    assert body["balanceCents"] == 10000

    # displayName must contain the base name plus the unique token suffix.
    display_name = body["displayName"]
    assert display_name.startswith("My Card [")
    assert display_name.endswith("]")

    # The correlation key (displayName) must be what lands in the ledger.
    assert result.id == "vc_synth1"

    # Ledger: one confirmed operation row + one card row.
    pending_gone = ledger.find_pending(display_name)
    assert pending_gone is None  # confirmed, not pending

    ops = [r for r in ledger.list_pending(intent="create")]
    assert ops == []  # all resolved

    # Card row exists.
    card_rows = ledger.query()
    assert any(r["card_id"] == "vc_synth1" for r in card_rows)

    configure_paths()


def test_create_card_recipient_defaults_from_account_context(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "default@example.com", "org_id": "org1"})

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    cards.create_card("cc_synth1", "Card", 5000, "2026-06-30", client=fake)

    _, body = fake.post_calls[0]
    assert body["recipient"] == "default@example.com"

    configure_paths()


def test_create_card_explicit_recipient_overrides_default(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "default@example.com", "org_id": "org1"})

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    cards.create_card("cc_synth1", "Card", 5000, "2026-06-30", recipient="other@example.com", client=fake)

    _, body = fake.post_calls[0]
    assert body["recipient"] == "other@example.com"

    configure_paths()


def test_create_card_unique_correlation_name(monkeypatch, tmp_path):
    """Each create_card call must produce a unique displayName."""
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "u@e.com", "org_id": "o"})

    names: list[str] = []
    call_count = [0]

    def fake_post(path, *, json_body=None, **_kw):
        names.append(json_body["displayName"])
        # Return unique card ids to avoid ledger duplicate errors.
        call_count[0] += 1
        card = {**_SYNTH_CARD, "id": f"vc_uniq{call_count[0]}"}
        return {"virtualCard": card}

    class _CountingClient:
        def post(self, path, *, json_body=None, **kw):
            return fake_post(path, json_body=json_body, **kw)

    for _ in range(3):
        cards.create_card("cc_synth1", "MyCard", 1000, "2026-06-30", client=_CountingClient())

    assert len(set(names)) == 3

    configure_paths()


# ---------------------------------------------------------------------------
# update_card
# ---------------------------------------------------------------------------

# Raw GET response: includes non-allowlist fields that must NOT leak into PUT.
_RAW_CARD_WITH_EXTRA = {
    **_SYNTH_CARD,
    # Non-allowlist fields that a real GET returns:
    "id": "vc_upd1",
    "status": "ACTIVE",
    "last4": "1234",
    "updatedAt": "2026-01-02T00:00:00.000+0000",
    "someServerField": "server-value",
}


def test_update_card_allowlist_projection(monkeypatch, tmp_path, caplog):
    """PUT body must contain only allowlist fields — no id/status/last4/server fields."""
    import logging as _logging

    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_upd1"}}
    fake = _MutatingFakeClient(
        get_responses={"/virtualcards/vc_upd1": {"virtualCard": _RAW_CARD_WITH_EXTRA}},
        put_responses={"/virtualcards/vc_upd1": put_resp},
    )

    with caplog.at_level(_logging.WARNING, logger="extendvcc.cards"):
        result = cards.update_card("vc_upd1", client=fake)

    _, put_body = fake.put_calls[0]
    assert "id" not in put_body
    assert "status" not in put_body
    assert "last4" not in put_body
    assert "updatedAt" not in put_body
    assert "someServerField" not in put_body

    # Allowlist fields present in the raw GET must appear in the PUT.
    assert "creditCardId" in put_body
    assert "currency" in put_body
    assert "recurs" in put_body
    assert "lowLimitAlert" in put_body

    # Drift warning logged for dropped non-allowlist fields.
    assert any("dropping non-allowlist" in record.message for record in caplog.records)

    assert result.id == "vc_upd1"

    configure_paths()


def test_update_card_overrides_translate_correctly(monkeypatch, tmp_path):
    """All four overrides must map to correct API keys with correct serialization."""
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_upd2"}}
    fake = _MutatingFakeClient(
        get_responses={"/virtualcards/vc_upd2": {"virtualCard": {**_RAW_CARD_WITH_EXTRA, "id": "vc_upd2"}}},
        put_responses={"/virtualcards/vc_upd2": put_resp},
    )
    cards.update_card(
        "vc_upd2",
        balance_cents=15000,
        name="New Name",
        valid_to=_date(2026, 6, 16),
        recurs=True,
        client=fake,
    )

    _, put_body = fake.put_calls[0]
    assert put_body["balanceCents"] == 15000
    assert put_body["displayName"] == "New Name"
    assert put_body["validTo"] == "2026-06-16"
    assert put_body["recurs"] is True

    configure_paths()


def test_update_card_server_fields_preserved(monkeypatch, tmp_path):
    """Fields from the GET that are on the allowlist but not overridden must be preserved."""
    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_upd3"}}
    raw_with_recurs = {**_RAW_CARD_WITH_EXTRA, "id": "vc_upd3", "recurs": False, "currency": "USD"}
    fake = _MutatingFakeClient(
        get_responses={"/virtualcards/vc_upd3": {"virtualCard": raw_with_recurs}},
        put_responses={"/virtualcards/vc_upd3": put_resp},
    )
    # Only override balance — recurs/currency/lowLimitAlert must come from GET.
    cards.update_card("vc_upd3", balance_cents=9999, client=fake)

    _, put_body = fake.put_calls[0]
    assert put_body["currency"] == "USD"
    assert put_body["recurs"] is False
    assert put_body["lowLimitAlert"] == {"alertEnabled": False, "amountThresholdCents": None}
    assert put_body["balanceCents"] == 9999  # the override

    configure_paths()


def test_update_card_correlation_key(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_corrkey"}}
    raw = {**_RAW_CARD_WITH_EXTRA, "id": "vc_corrkey"}
    fake = _MutatingFakeClient(
        get_responses={"/virtualcards/vc_corrkey": {"virtualCard": raw}},
        put_responses={"/virtualcards/vc_corrkey": put_resp},
    )
    cards.update_card("vc_corrkey", client=fake)

    # No pending row should survive (resolved confirmed).
    assert ledger.find_pending("update:vc_corrkey") is None

    card_rows = ledger.query()
    assert any(r["card_id"] == "vc_corrkey" for r in card_rows)

    configure_paths()


# ---------------------------------------------------------------------------
# cancel_card / close_card
# ---------------------------------------------------------------------------


def test_cancel_card(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_cancel1", "status": "CANCELLED"}}
    fake = _MutatingFakeClient(put_responses={"/virtualcards/vc_cancel1/cancel": put_resp})

    result = cards.cancel_card("vc_cancel1", client=fake)

    assert result.id == "vc_cancel1"
    path, body = fake.put_calls[0]
    assert path == "/virtualcards/vc_cancel1/cancel"
    assert body is None  # empty body

    # Ledger: confirmed, no pending row.
    assert ledger.find_pending("cancel:vc_cancel1") is None
    card_rows = ledger.query()
    assert any(r["card_id"] == "vc_cancel1" for r in card_rows)

    configure_paths()


def test_close_card(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    put_resp = {"virtualCard": {**_SYNTH_CARD, "id": "vc_close1", "status": "CLOSED"}}
    fake = _MutatingFakeClient(put_responses={"/virtualcards/vc_close1/close": put_resp})

    result = cards.close_card("vc_close1", client=fake)

    assert result.id == "vc_close1"
    path, body = fake.put_calls[0]
    assert path == "/virtualcards/vc_close1/close"
    assert body is None  # empty body

    assert ledger.find_pending("close:vc_close1") is None
    card_rows = ledger.query()
    assert any(r["card_id"] == "vc_close1" for r in card_rows)

    configure_paths()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_4xx_error_resolves_pending_failed(monkeypatch, tmp_path):
    """A 4xx API error must resolve the pending row as failed and re-raise."""
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "u@e.com", "org_id": "o"})

    err = PayWithExtendAPIError("Bad request", status_code=422, path="/virtualcards")

    class _ErrorClient:
        def post(self, path, *, json_body=None, **_kw):
            raise err

    with pytest.raises(PayWithExtendAPIError):
        cards.create_card("cc_synth1", "Card", 1000, "2026-06-30", client=_ErrorClient())

    # All pending rows for "create" intent must be gone (resolved as failed).
    assert ledger.list_pending(intent="create") == []

    configure_paths()


def test_kill_switch_leaves_pending_row(monkeypatch, tmp_path):
    """PayWithExtendDisabled (kill switch / network) must leave the pending row intact."""
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "u@e.com", "org_id": "o"})

    dispatched_name: list[str] = []

    class _KillSwitchClient:
        def post(self, path, *, json_body=None, **_kw):
            dispatched_name.append(json_body["displayName"])
            raise PayWithExtendDisabled("automation disabled")

    with pytest.raises(PayWithExtendDisabled):
        cards.create_card("cc_synth1", "Card", 1000, "2026-06-30", client=_KillSwitchClient())

    # Pending row must still exist.
    assert len(dispatched_name) == 1
    pending = ledger.find_pending(dispatched_name[0])
    assert pending is not None
    assert pending["status"] == "pending"

    configure_paths()


def test_5xx_error_leaves_pending_row(monkeypatch, tmp_path):
    """A 5xx API error (ambiguous) must leave the pending row intact."""
    _patch_ledger(monkeypatch, tmp_path)

    put_resp_500 = PayWithExtendAPIError("Server error", status_code=500, path="/virtualcards/vc_5xx/cancel")
    raw = {**_RAW_CARD_WITH_EXTRA, "id": "vc_5xx"}
    fake = _MutatingFakeClient(
        get_responses={"/virtualcards/vc_5xx": {"virtualCard": raw}},
    )

    class _ErrorPutClient:
        def put(self, path, *, json_body=None, **_kw):
            raise put_resp_500

        def get(self, path, params=None):
            return fake.get(path, params)

    with pytest.raises(PayWithExtendAPIError):
        cards.cancel_card("vc_5xx", client=_ErrorPutClient())

    # Pending row for cancel must survive.
    pending = ledger.find_pending("cancel:vc_5xx")
    assert pending is not None
    assert pending["status"] == "pending"

    configure_paths()


# ---------------------------------------------------------------------------
# reconcile / create recovery
# ---------------------------------------------------------------------------


def test_reconcile_adopts_remote_card(monkeypatch, tmp_path):
    """Stale pending create row matched by name on remote → confirmed, no duplicate POST."""
    _patch_ledger(monkeypatch, tmp_path)

    unique_name = "My Card [deadbeef]"
    # Seed a stale pending row directly.
    ledger.record_pending("create", unique_name)

    # Remote card with the same name.
    remote_card_dict = {**_SYNTH_CARD, "id": "vc_adopted1", "displayName": unique_name}
    remote_resp = {
        "virtualCards": [remote_card_dict],
        "pagination": {"numberOfPages": 1, "page": 0, "pageItemCount": 1, "totalItems": 1},
    }
    fake = _MutatingFakeClient(get_responses={"/virtualcards": remote_resp})

    result = cards.reconcile(client=fake)

    assert "vc_adopted1" in result["adopted"]
    assert result["failed"] == []

    # No pending row survives.
    assert ledger.find_pending(unique_name) is None

    # Card row is in the ledger.
    card_rows = ledger.query()
    assert any(r["card_id"] == "vc_adopted1" for r in card_rows)

    # No POST was issued — reconcile is read-only.
    assert fake.post_calls == []

    configure_paths()


# ---------------------------------------------------------------------------
# reveal_card — now always returns raw credentials (no keychain)
# ---------------------------------------------------------------------------

# Synthetic, non-Luhn, non-secret card credentials for testing.
_REVEAL_RESPONSE = {
    "virtualCard": {
        "vcn": "0000111122223333",
        "securityCode": "123",
        "expires": "2029-09-02T00:00:00.000+0000",
        "last4": "3333",
        "numberFormat": "cardnumber16",
    }
}


class _VaultFakeClient:
    """Fake vault client returning canned reveal response."""

    def get(self, path: str, params: dict | None = None) -> dict:
        return _REVEAL_RESPONSE


def test_reveal_card_returns_raw_credentials():
    """reveal_card always returns raw number/cvc in result."""
    result = cards.reveal_card("vc_x", client=_VaultFakeClient())

    assert result == {
        "number": "0000111122223333",
        "cvc": "123",
        "last4": "3333",
        "expires": "2029-09-02T00:00:00.000+0000",
    }


# ---------------------------------------------------------------------------
# ledger.sync(fetcher=cards.list_cards) date/datetime serialization (Fix B)
# ---------------------------------------------------------------------------


def test_ledger_sync_with_list_cards_serializes_dates(monkeypatch, tmp_path):
    """ledger.sync(fetcher=cards.list_cards) must not crash on date/datetime fields.

    VirtualCard.valid_from/valid_to are date objects; created_at is datetime.
    This test validates the docstring claim that list_cards composes with ledger.sync.
    """
    import json as _json

    _patch_ledger(monkeypatch, tmp_path)

    fake = _FakeClient(
        {
            "/virtualcards": {
                "pagination": {"numberOfPages": 1, "page": 0, "pageItemCount": 1, "totalItems": 1},
                "virtualCards": [_VC_TEMPLATE],
            }
        }
    )

    # Must not raise TypeError from json.dumps on date/datetime objects.
    summary = ledger.sync(fetcher=lambda: cards.list_cards(client=fake))

    assert summary["added"] == ["vc_test1"]

    # Stored values must be ISO strings (round-trip safe).
    ledger_file = tmp_path / "cards.jsonl"
    raw = ledger_file.read_text()
    row = _json.loads(raw.strip().splitlines()[0])
    assert isinstance(row["valid_to"], str), "valid_to must be an ISO string"
    assert isinstance(row["created_at"], str), "created_at must be an ISO string"
    assert row["valid_to"].startswith("2026-01-01")
    assert row["created_at"].startswith("2025-01-01")

    configure_paths()


# ---------------------------------------------------------------------------
# enroll_credit_card
# ---------------------------------------------------------------------------

_ENROLL_PAN = "0000111122223333"  # synthetic, non-Luhn
_ENROLL_CVC = "123"
_ENROLL_ADDR = {
    "address1": "123 Main St",
    "address2": "Apt 4",
    "city": "San Francisco",
    "postal": "94105",
    "province": "CA",
}
_ENROLL_RESP = {
    "creditCard": {
        "id": "cc_new1",
        "last4": "1004",
        "status": "NOT_APPLICABLE",
        "displayName": "Synthetic Parent",
    }
}
# Step 2 (PUT .../virtual) response: same card, now PENDING.
_ENROLL_VIRTUAL_RESP = {
    "creditCard": {
        "id": "cc_new1",
        "last4": "1004",
        "status": "PENDING",
        "displayName": "Synthetic Parent",
    }
}


class _VaultMutatingFakeClient:
    """Vault fake client for enroll tests (POST step 1 + PUT step 2)."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.post_calls: list[tuple[str, Any]] = []
        self.put_calls: list[tuple[str, Any]] = []

    def post(self, path: str, *, json_body: Any = None, **_kw) -> dict:
        self.post_calls.append((path, json_body))
        if path in self._responses:
            return self._responses[path]
        raise KeyError(f"VaultFakeClient: no POST response for {path!r}")

    def put(self, path: str, *, json_body: Any = None, **_kw) -> dict:
        self.put_calls.append((path, json_body))
        return _ENROLL_VIRTUAL_RESP


def _make_enroll_client() -> _VaultMutatingFakeClient:
    return _VaultMutatingFakeClient({"/creditcardsv2": _ENROLL_RESP})


def test_enroll_credit_card_body_correctness(monkeypatch, tmp_path):
    """POST path, all body keys, issuerFields, organizationId, companyName default."""
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "e", "org_id": "org_test"})

    fake = _make_enroll_client()
    cards.enroll_credit_card(
        "Synthetic Parent",
        _ENROLL_PAN,
        _date(2030, 10, 31),
        _ENROLL_CVC,
        "Jane Doe",
        "ii_test",
        _ENROLL_ADDR,
        client=fake,
    )

    assert len(fake.post_calls) == 1
    path, body = fake.post_calls[0]
    assert path == "/creditcardsv2"

    expected_keys = {
        "displayName",
        "country",
        "companyName",
        "cardholderName",
        "address1",
        "address2",
        "city",
        "postal",
        "province",
        "cvc",
        "issuerId",
        "issuerFields",
        "organizationId",
    }
    assert set(body.keys()) == expected_keys

    assert body["issuerFields"] == {"cardNumber": _ENROLL_PAN, "expires": "2030-10-31"}
    assert body["organizationId"] == "org_test"
    # companyName defaults to cardholder_name when company_name omitted
    assert body["companyName"] == "Jane Doe"
    assert body["cardholderName"] == "Jane Doe"
    assert body["country"] == "US"
    assert body["address2"] == "Apt 4"

    configure_paths()


def test_enroll_credit_card_ledger_operation_only(monkeypatch, tmp_path):
    """Confirmed enroll: operation row with intent==enroll + credit_card_id; query() is EMPTY."""
    import json as _json
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "e", "org_id": "org_test"})

    cards.enroll_credit_card(
        "Synthetic Parent",
        _ENROLL_PAN,
        _date(2030, 10, 31),
        _ENROLL_CVC,
        "Jane Doe",
        "ii_test",
        _ENROLL_ADDR,
        client=_make_enroll_client(),
    )

    # No card rows (query returns virtual-card records only)
    assert ledger.query() == []

    # Operation row is confirmed
    ledger_file = tmp_path / "cards.jsonl"
    raw = ledger_file.read_text()
    rows = [_json.loads(line) for line in raw.splitlines() if line.strip()]
    op_rows = [r for r in rows if r.get("record_type") == "operation"]
    assert len(op_rows) == 1
    op = op_rows[0]
    assert op["intent"] == "enroll"
    assert op["status"] == "confirmed"
    assert op["credit_card_id"] == "cc_new1"

    configure_paths()


def test_enroll_credit_card_no_secret_to_ledger(monkeypatch, tmp_path):
    """PAN and CVC must never appear in any ledger row."""
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "e", "org_id": "org_test"})

    cards.enroll_credit_card(
        "Synthetic Parent",
        _ENROLL_PAN,
        _date(2030, 10, 31),
        _ENROLL_CVC,
        "Jane Doe",
        "ii_test",
        _ENROLL_ADDR,
        client=_make_enroll_client(),
    )

    ledger_file = tmp_path / "cards.jsonl"
    raw_text = ledger_file.read_text()
    assert _ENROLL_PAN not in raw_text
    assert _ENROLL_CVC not in raw_text

    configure_paths()


def test_enroll_credit_card_4xx_resolves_failed(monkeypatch, tmp_path):
    """4xx error: pending operation resolved failed and exception re-raised."""
    import json as _json
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "e", "org_id": "org_test"})

    err = PayWithExtendAPIError("Bad request", status_code=422, path="/creditcardsv2")

    class _ErrorVaultClient:
        def post(self, path, *, json_body=None, **_kw):
            raise err

    with pytest.raises(PayWithExtendAPIError):
        cards.enroll_credit_card(
            "Synthetic Parent",
            _ENROLL_PAN,
            _date(2030, 10, 31),
            _ENROLL_CVC,
            "Jane Doe",
            "ii_test",
            _ENROLL_ADDR,
            client=_ErrorVaultClient(),
        )

    # No pending rows survive
    assert ledger.list_pending(intent="enroll") == []

    # Operation row must be failed
    ledger_file = tmp_path / "cards.jsonl"
    raw = ledger_file.read_text()
    rows = [_json.loads(line) for line in raw.splitlines() if line.strip()]
    op_rows = [r for r in rows if r.get("record_type") == "operation"]
    assert len(op_rows) == 1
    assert op_rows[0]["status"] == "failed"

    configure_paths()


def test_enroll_credit_card_5xx_leaves_pending(monkeypatch, tmp_path):
    """Ambiguous failure (5xx): pending enroll row survives and exception re-raises."""
    from datetime import date as _date

    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "e", "org_id": "org_test"})

    err = PayWithExtendAPIError("Server error", status_code=503, path="/creditcardsv2")

    class _ErrorVaultClient:
        def post(self, path, *, json_body=None, **_kw):
            raise err

    with pytest.raises(PayWithExtendAPIError):
        cards.enroll_credit_card(
            "Synthetic Parent",
            _ENROLL_PAN,
            _date(2030, 10, 31),
            _ENROLL_CVC,
            "Jane Doe",
            "ii_test",
            _ENROLL_ADDR,
            client=_ErrorVaultClient(),
        )

    # The pending row must survive as local evidence (not resolved).
    pending = ledger.list_pending(intent="enroll")
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"

    configure_paths()


# ---------------------------------------------------------------------------
# create_cards_bulk
# ---------------------------------------------------------------------------


def _bulk_rows(n: int) -> list[dict]:
    return [{"name": f"Card {i}", "balance_cents": 1000 + i, "valid_to": "2026-06-30"} for i in range(n)]


def test_create_cards_bulk_creates_each_row(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "owner@example.com", "org_id": "o"})

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    result = cards.create_cards_bulk("cc_synth1", _bulk_rows(3), delay_seconds=0, client=fake)

    assert len(result) == 3
    assert all(isinstance(c, cards.VirtualCard) for c in result)
    assert len(fake.post_calls) == 3
    # Each row's balance + name flow through to the POST body.
    balances = [body["balanceCents"] for _, body in fake.post_calls]
    assert balances == [1000, 1001, 1002]
    assert all(body["creditCardId"] == "cc_synth1" for _, body in fake.post_calls)

    configure_paths()


def test_create_cards_bulk_prevalidates_before_any_create(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "o@e.com", "org_id": "o"})

    rows = _bulk_rows(2)
    rows.append({"name": "Bad", "valid_to": "2026-06-30"})  # missing balance_cents

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    with pytest.raises(ValueError, match="balance_cents"):
        cards.create_cards_bulk("cc_synth1", rows, delay_seconds=0, client=fake)

    # Nothing was created — the bad row fails the whole batch up front.
    assert fake.post_calls == []

    configure_paths()


def test_create_cards_bulk_fail_fast_keeps_prior_cards(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "o@e.com", "org_id": "o"})

    err = PayWithExtendAPIError("rejected", status_code=400, path="/virtualcards")

    class _FailSecond:
        def __init__(self):
            self.post_calls = []

        def post(self, path, *, json_body=None, **_kw):
            self.post_calls.append((path, json_body))
            if len(self.post_calls) == 2:
                raise err
            return _CARD_RESP

    fake = _FailSecond()
    with pytest.raises(PayWithExtendAPIError):
        cards.create_cards_bulk("cc_synth1", _bulk_rows(3), delay_seconds=0, client=fake)

    # Stopped at the failing row; the third was never attempted.
    assert len(fake.post_calls) == 2
    # The first card is durably ledgered; the failed row is marked failed.
    assert len(ledger.query()) == 1

    configure_paths()


@pytest.mark.parametrize(
    "bad_row, match",
    [
        ({"name": "", "balance_cents": 1000, "valid_to": "2026-06-30"}, "non-empty string"),
        ({"name": "X", "balance_cents": "1000", "valid_to": "2026-06-30"}, "must be an int"),
        ({"name": "X", "balance_cents": True, "valid_to": "2026-06-30"}, "must be an int"),
        ({"name": "X", "balance_cents": 1000, "valid_to": "Dec 2026"}, "valid_to"),
        ({"name": "X", "balance_cents": 1000, "valid_to": 12345}, "valid_to"),
    ],
)
def test_create_cards_bulk_rejects_malformed_row(monkeypatch, tmp_path, bad_row, match):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "o@e.com", "org_id": "o"})

    rows = _bulk_rows(2)
    rows.append(bad_row)  # malformed value, not just a missing key

    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    with pytest.raises(ValueError, match=match):
        cards.create_cards_bulk("cc_synth1", rows, delay_seconds=0, client=fake)

    # Validity is checked up front — nothing was created.
    assert fake.post_calls == []

    configure_paths()


# ---------------------------------------------------------------------------
# Operation builders (dry-run / real share one body-shaping implementation)
# ---------------------------------------------------------------------------


def test_build_create_card_operation_correlation_key_in_descriptor():
    """The correlation key (UUID-suffixed displayName) must be returned in the
    descriptor, not regenerated, so a dry-run preview matches the real request."""
    op = cards.build_create_card_operation(
        "cc_x",
        "My Card",
        5000,
        "2026-06-30",
        recipient_resolver=lambda: "r@e.com",
        token_factory=lambda: "fixed123",
    )
    assert op["correlation_key"] == "My Card [fixed123]"
    # The body's displayName must equal the returned correlation key exactly.
    assert op["body"]["displayName"] == op["correlation_key"]
    assert op["body"]["recipient"] == "r@e.com"
    assert op["body"]["validTo"] == "2026-06-30"
    assert op["method"] == "POST"
    assert op["path"] == "/virtualcards"


def test_build_create_card_operation_rejects_both_or_neither():
    """Exactly one of valid_to / recurrence — the builder enforces it like create_card."""
    with pytest.raises(ValueError, match="exactly one"):
        cards.build_create_card_operation(
            "cc_x",
            "C",
            1000,
            "2026-06-30",
            recurrence=cards.Recurrence(period="MONTHLY", interval=1, by_month_day=1),
            recipient_resolver=lambda: "r@e.com",
            token_factory=lambda: "t",
        )


def test_build_update_card_operation_allowlist_projection():
    """The update builder projects the fetched card to the allowlist and applies
    overrides — same body the real PUT would send, without any network here."""
    raw = {**_RAW_CARD_WITH_EXTRA, "id": "vc_b"}
    op = cards.build_update_card_operation(
        "vc_b",
        {"balanceCents": 7777},
        fetcher=lambda: {"virtualCard": raw},
    )
    body = op["body"]
    assert "someServerField" not in body  # non-allowlist dropped
    assert "id" not in body
    assert body["balanceCents"] == 7777  # override applied
    assert body["currency"] == "USD"  # allowlist field preserved
    assert op["method"] == "PUT"
    assert op["path"] == "/virtualcards/vc_b"


# Synthetic full credit-card GET object. Flat top-level address fields are
# intentionally STALE relative to the nested `address` object, mirroring the
# real capture. `countryCode` is an unknown nested key used to prove merge.
_RAW_CREDIT_CARD = {
    "id": "cc_synth1",
    "last4": "1040",
    "status": "ACTIVE",
    "displayName": "Parent Card",
    "issuedAmountCents": 150300,
    "issuerId": "ii_x",
    "type": "SOURCE",
    "country": "US",
    "address1": "400 Old St",
    "address2": "",
    "city": "Oldtown",
    "province": "NY",
    "postal": "10001",
    "address": {
        "address1": "400 Old St",
        "address2": "",
        "city": "Oldtown",
        "country": "US",
        "province": "NY",
        "postal": "10001",
        "countryCode": "840",
    },
}
_CC_PUT_RESP = {"creditCard": {"id": "cc_synth1", "last4": "1040", "status": "ACTIVE", "displayName": "Parent Card"}}


def test_build_update_credit_card_merges_nested_address():
    """Override merges into the nested `address`; unknown nested keys survive."""
    fake = _MutatingFakeClient(get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}})
    op = cards.build_update_credit_card_operation(
        "cc_synth1",
        {"address": {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"}},
        fetcher=lambda: fake.get("/creditcards/cc_synth1"),
    )
    body = op["body"]
    assert op["method"] == "PUT"
    assert op["path"] == "/creditcards/cc_synth1"
    assert body["address"]["address1"] == "1 New Rd"
    assert body["address"]["city"] == "Newtown"
    # merge, not replace: the unknown nested key is preserved.
    assert body["address"]["countryCode"] == "840"


def test_build_update_credit_card_leaves_flat_and_other_fields_untouched():
    """Flat address fields stay stale; non-address fields round-trip unchanged."""
    fake = _MutatingFakeClient(get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}})
    op = cards.build_update_credit_card_operation(
        "cc_synth1",
        {"address": {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"}},
        fetcher=lambda: fake.get("/creditcards/cc_synth1"),
    )
    body = op["body"]
    # Flat top-level fields are NOT mirrored — still stale, as the browser left them.
    assert body["address1"] == "400 Old St"
    assert body["city"] == "Oldtown"
    # Unrelated field preserved verbatim.
    assert body["issuedAmountCents"] == 150300
    assert body["type"] == "SOURCE"


def test_build_update_credit_card_rejects_thin_get():
    """A thin GET (list-item shape) must raise, not silently blank the parent card."""
    thin = {"id": "cc_thin", "last4": "1", "status": "ACTIVE", "displayName": "x"}
    fake = _MutatingFakeClient(get_responses={"/creditcards/cc_thin": {"creditCard": thin}})
    with pytest.raises(PayWithExtendError):
        cards.build_update_credit_card_operation(
            "cc_thin", {"address": {"address1": "y"}}, fetcher=lambda: fake.get("/creditcards/cc_thin")
        )


def test_build_update_credit_card_rejects_thin_plus_extra_key():
    """A near-thin GET with stray keys but no nested `address` must STILL raise.

    Guards the fail-safe: a denylist of the exact 4-key shape would let
    `{id,last4,status,displayName,type}` slip through and blank the parent card.
    The guard requires positive evidence of a full object (nested `address`).
    """
    almost = {"id": "cc_x", "last4": "1", "status": "ACTIVE", "displayName": "x", "type": "SOURCE"}
    fake = _MutatingFakeClient(get_responses={"/creditcards/cc_x": {"creditCard": almost}})
    with pytest.raises(PayWithExtendError, match="full card object"):
        cards.build_update_credit_card_operation(
            "cc_x", {"address": {"address1": "y"}}, fetcher=lambda: fake.get("/creditcards/cc_x")
        )


def test_update_credit_card_address_overrides_nested_only(monkeypatch, tmp_path):
    """New address lands in the nested object only; flat fields stay stale."""
    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    result = cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake,
    )
    path, body = fake.put_calls[0]
    assert path == "/creditcards/cc_synth1"
    assert body["address"]["address1"] == "1 New Rd"
    assert body["address"]["countryCode"] == "840"  # merge preserved
    assert body["address1"] == "400 Old St"  # flat untouched
    assert result.id == "cc_synth1"
    configure_paths()


def test_update_credit_card_address_postal_stays_string(monkeypatch, tmp_path):
    """Leading-zero ZIPs must not be coerced to int anywhere."""
    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "MA", "postal": "02134"},
        client=fake,
    )
    _, body = fake.put_calls[0]
    assert body["address"]["postal"] == "02134"
    configure_paths()


def test_update_credit_card_address_country_set_in_two_places(monkeypatch, tmp_path):
    """Explicit country updates both nested and top-level; omitted preserves GET value."""
    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        country="CA",
        client=fake,
    )
    _, body = fake.put_calls[0]
    assert body["country"] == "CA"
    assert body["address"]["country"] == "CA"

    # Omitted country -> GET value preserved.
    fake2 = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake2,
    )
    _, body2 = fake2.put_calls[0]
    assert body2["country"] == "US"
    assert body2["address"]["country"] == "US"
    configure_paths()


def test_update_credit_card_address_missing_field_raises(monkeypatch, tmp_path):
    """A missing required address field fails before any network call."""
    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient()
    with pytest.raises(ValueError, match="address1"):
        cards.update_credit_card_address(
            "cc_synth1", {"city": "Newtown", "province": "CA", "postal": "95051"}, client=fake
        )
    assert fake.get_calls == []
    assert fake.put_calls == []
    configure_paths()


def test_update_credit_card_address_ledger_confirmed(monkeypatch, tmp_path):
    """A successful update writes a pending row and resolves it to confirmed.

    Asserting only `find_pending is None` is too weak — it also passes if
    record_pending never ran. Read the raw ledger file and prove a confirmed
    operation row for this key exists (credit-card updates write no card row, so
    we cannot lean on ledger.query()).
    """
    import json as _json

    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake,
    )
    assert ledger.find_pending("update-cc:cc_synth1") is None  # no longer pending

    rows = [_json.loads(line) for line in (tmp_path / "cards.jsonl").read_text().splitlines() if line.strip()]
    ops = [
        r
        for r in rows
        if r.get("record_type") == "operation"
        and r.get("correlation_key") == "update-cc:cc_synth1"
        and r.get("intent") == "update-cc"
    ]
    assert len(ops) == 1
    assert ops[0]["status"] == "confirmed"
    configure_paths()


def test_update_credit_card_address_4xx_marks_failed(monkeypatch, tmp_path):
    """A 4xx from the PUT marks the pending row failed (retry-safe)."""
    _patch_ledger(monkeypatch, tmp_path)

    class _Fake4xx(_MutatingFakeClient):
        def put(self, path, *, json_body=None, **_kw):
            self.put_calls.append((path, json_body))
            raise PayWithExtendAPIError("bad request", status_code=400, path=path)

    fake = _Fake4xx(get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}})
    with pytest.raises(PayWithExtendAPIError):
        cards.update_credit_card_address(
            "cc_synth1",
            {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
            client=fake,
        )
    # The pending row was resolved (as failed), so no update-cc rows remain pending.
    # `find_pending` matches only PENDING rows, so it returns None here — assert via
    # list_pending, mirroring test_4xx_error_resolves_pending_failed.
    assert ledger.list_pending(intent="update-cc") == []
    assert ledger.find_pending("update-cc:cc_synth1") is None
    configure_paths()


def test_update_credit_card_address_omitting_address2_preserves_existing(monkeypatch, tmp_path):
    """Omitting address2 keeps the card's existing suite line (no silent blanking)."""
    _patch_ledger(monkeypatch, tmp_path)
    raw = {**_RAW_CREDIT_CARD, "address": {**_RAW_CREDIT_CARD["address"], "address2": "Apt 5"}}
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": raw}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake,
    )
    _, body = fake.put_calls[0]
    assert body["address"]["address2"] == "Apt 5"  # preserved, not blanked
    configure_paths()


def test_update_credit_card_address_explicit_empty_address2_clears(monkeypatch, tmp_path):
    """Passing address2='' explicitly clears an existing suite line."""
    _patch_ledger(monkeypatch, tmp_path)
    raw = {**_RAW_CREDIT_CARD, "address": {**_RAW_CREDIT_CARD["address"], "address2": "Apt 5"}}
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": raw}},
        put_responses={"/creditcards/cc_synth1": _CC_PUT_RESP},
    )
    cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "address2": "", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake,
    )
    _, body = fake.put_calls[0]
    assert body["address"]["address2"] == ""  # explicitly cleared
    configure_paths()


def test_update_credit_card_address_odd_success_body_still_confirms(monkeypatch, tmp_path):
    """A 200 PUT with an unrecognizable body must NOT leave a dangling pending row.

    The server already applied the change, so we fall back to the round-tripped card
    (from the GET body) and confirm the ledger row instead of raising.
    """
    import json as _json

    _patch_ledger(monkeypatch, tmp_path)
    fake = _MutatingFakeClient(
        get_responses={"/creditcards/cc_synth1": {"creditCard": _RAW_CREDIT_CARD}},
        put_responses={"/creditcards/cc_synth1": {"ok": True}},  # odd body, no card fields
    )
    result = cards.update_credit_card_address(
        "cc_synth1",
        {"address1": "1 New Rd", "city": "Newtown", "province": "CA", "postal": "95051"},
        client=fake,
    )
    # Fallback card built from the GET body, not the odd PUT response.
    assert result.id == "cc_synth1"
    assert result.last4 == "1040"
    assert result.status == cards.CardStatus.ACTIVE

    assert ledger.find_pending("update-cc:cc_synth1") is None  # not dangling
    rows = [_json.loads(line) for line in (tmp_path / "cards.jsonl").read_text().splitlines() if line.strip()]
    ops = [r for r in rows if r.get("correlation_key") == "update-cc:cc_synth1"]
    assert len(ops) == 1
    assert ops[0]["status"] == "confirmed"
    configure_paths()


def test_update_credit_card_address_is_public():
    import extendvcc

    assert hasattr(extendvcc, "update_credit_card_address")


# ---------------------------------------------------------------------------
# create_card — recurring cards
# ---------------------------------------------------------------------------


def _make_recurring(monkeypatch, tmp_path, recurrence):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "o@e.com", "org_id": "o"})
    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    cards.create_card("cc_x", "Sub", 2000, recurrence=recurrence, client=fake)
    body = fake.post_calls[0][1]
    configure_paths()
    return body


def test_create_card_monthly_recurring_body(monkeypatch, tmp_path):
    rec = cards.Recurrence(period="MONTHLY", interval=1, by_month_day=1)
    body = _make_recurring(monkeypatch, tmp_path, rec)
    assert body["recurs"] is True
    assert "validTo" not in body  # recurring cards never carry an expiry date
    assert body["recurrence"] == {
        "balanceCents": 2000,
        "period": "MONTHLY",
        "interval": 1,
        "terminator": "NONE",
        "byMonthDay": 1,
    }


def test_create_card_weekly_count_terminator(monkeypatch, tmp_path):
    rec = cards.Recurrence(period="WEEKLY", interval=2, by_week_day=3, terminator="COUNT", count=10)
    body = _make_recurring(monkeypatch, tmp_path, rec)
    assert body["recurrence"] == {
        "balanceCents": 2000,
        "period": "WEEKLY",
        "interval": 2,
        "terminator": "COUNT",
        "byWeekDay": 3,
        "count": 10,
    }


def test_create_card_weekly_date_terminator(monkeypatch, tmp_path):
    rec = cards.Recurrence(period="WEEKLY", interval=1, by_week_day=3, terminator="DATE", until="2026-06-15")
    body = _make_recurring(monkeypatch, tmp_path, rec)
    assert body["recurrence"] == {
        "balanceCents": 2000,
        "period": "WEEKLY",
        "interval": 1,
        "terminator": "DATE",
        "byWeekDay": 3,
        "until": "2026-06-15",
    }


def test_create_card_one_time_still_builds_validto(monkeypatch, tmp_path):
    _patch_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(cards, "account_context", lambda: {"email": "o@e.com", "org_id": "o"})
    fake = _MutatingFakeClient(post_responses={"/virtualcards": _CARD_RESP})
    cards.create_card("cc_x", "OneTime", 2000, "2026-06-30", client=fake)
    body = fake.post_calls[0][1]
    assert body["validTo"] == "2026-06-30"
    assert "recurs" not in body
    assert "recurrence" not in body
    configure_paths()
