import datetime
import importlib.util
import itertools
import pathlib
import sys
from datetime import date as _date
from types import SimpleNamespace

from extendvcc import _exit_codes
from extendvcc.auth import SessionNotFound
from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError
from extendvcc.models import CardStatus, CreditCard, Issuer, VirtualCard

_SMOKE_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_test", _SMOKE_PATH)
    module = importlib.util.module_from_spec(spec)
    # MUST register in sys.modules BEFORE exec_module: the harness uses
    # `from __future__ import annotations` + @dataclass with a default field
    # (StepResult), and dataclass processing resolves the module by name via
    # sys.modules[cls.__module__]. Without this line exec_module raises
    # AttributeError ('NoneType' has no attribute '__dict__') at import time on
    # every supported Python (3.11-3.14), failing the whole test file.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


def test_module_imports_without_network_and_exposes_constants():
    assert smoke.SMOKE_CARD_BALANCE_CENTS == 11001
    assert smoke.SMOKE_CARD_NAME_PREFIX == "extendvcc-smoke"


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


def _fake_clock():
    ticks = iter([0.0, 0.5, 1.0, 2.5, 3.0, 10.0])
    return lambda: next(ticks)


def _fake_clock_long():
    counter = itertools.count()
    return lambda: float(next(counter))


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


def _closed(cid):
    # cleanup() inspects the returned card's status; a real close_card returns a
    # VirtualCard. A minimal stub with .status == CLOSED is enough here.
    return SimpleNamespace(id=cid, status=CardStatus.CLOSED)


def test_cleanup_cancels_and_closes_every_created_card():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_1")
    h.register_created("vc_2")
    calls = []

    def _close(cid):
        calls.append(("close", cid))
        return _closed(cid)

    leftovers = h.cleanup(
        cancel=lambda cid: calls.append(("cancel", cid)),
        close=_close,
        warn=lambda msg: calls.append(("warn", msg)),
    )
    assert leftovers == []
    assert calls == [
        ("cancel", "vc_1"),
        ("close", "vc_1"),
        ("cancel", "vc_2"),
        ("close", "vc_2"),
    ]


def test_cleanup_skips_cards_already_marked_closed():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_done")
    h.mark_closed("vc_done")
    calls = []
    leftovers = h.cleanup(
        cancel=lambda cid: calls.append(("cancel", cid)),
        close=lambda cid: calls.append(("close", cid)) or _closed(cid),
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


def test_cleanup_reports_leftover_when_close_returns_non_closed_status():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_still_active")
    warnings = []

    leftovers = h.cleanup(
        cancel=lambda cid: None,
        close=lambda cid: SimpleNamespace(id=cid, status=CardStatus.ACTIVE),
        warn=warnings.append,
    )
    assert leftovers and leftovers[0][0] == "vc_still_active"
    assert warnings and "vc_still_active" in warnings[0]
    assert "110.01" in warnings[0]


def test_cleanup_still_closes_when_cancel_fails():
    h = smoke.Harness(clock=_fake_clock())
    h.register_created("vc_cancel_4xx")
    calls = []

    def failing_cancel(cid):
        raise RuntimeError("already cancelled")

    def _close(cid):
        calls.append(("close", cid))
        return _closed(cid)

    leftovers = h.cleanup(
        cancel=failing_cancel,
        close=_close,
        warn=lambda msg: calls.append(("warn", msg)),
    )
    assert ("close", "vc_cancel_4xx") in calls  # close attempted despite cancel failure
    assert leftovers == []  # a failed cancel alone is NOT a leftover; close succeeded


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
        return {"email": "user@example.com", "org_id": "org_123"}

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
            raise PayWithExtendAPIError("get exploded", status_code=500, path="/virtualCards")
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
        "account_context",
        "list_issuers",
        "list_credit_cards",
        "create_card",
        "get_card",
        "list_cards",
        "reveal_card",
        "update_card",
        "usage",
        "cancel_card",
        "close_card",
    ):
        monkeypatch.setattr(smoke, name, getattr(fake, name))


def test_run_lifecycle_happy_path_calls_every_step(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    h = smoke.Harness(clock=_fake_clock_long())
    smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14), run_prefix="extendvcc-smoke run-test")
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
        smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14), run_prefix="extendvcc-smoke run-test")
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
        smoke.run_lifecycle(h, parent_id=None, today=_date(2026, 6, 14), run_prefix="extendvcc-smoke run-test")
    assert any(r.name == "reveal" and not r.passed for r in h.results)


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
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} run-test"
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14), run_prefix=run_prefix)
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
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} run-test"
    smoke.run_bulk(h, parent_id="cc_1", count=2, today=_date(2026, 6, 14), run_prefix=run_prefix)
    assert all(row["name"].startswith(smoke.SMOKE_CARD_NAME_PREFIX) for row in captured["rows"])


def test_main_happy_path_returns_ok_and_cleans_up(monkeypatch, capsys):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_OK
    assert ("cancel_card", "vc_new") in fake.calls
    assert ("close_card", "vc_new") in fake.calls


def test_main_returns_api_error_and_still_closes_card_on_failure(monkeypatch):
    fake = _FakeCards(fail_on="get")
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_API_ERROR
    assert ("close_card", "vc_new") in fake.calls


def test_main_aborts_when_not_confirmed(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_read_confirm", lambda: "no")
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)  # pretend not-CI
    rc = smoke.main([])
    assert rc == _exit_codes.EXIT_ERROR
    assert not any(c[0] == "create_card" for c in fake.calls)


def test_refuse_in_ci_detects_markers():
    assert smoke._refuse_in_ci({"CI": "true"}) == "CI"
    assert smoke._refuse_in_ci({"GITHUB_ACTIONS": "true"}) == "GITHUB_ACTIONS"
    assert smoke._refuse_in_ci({"PATH": "/usr/bin"}) is None
    assert smoke._refuse_in_ci({}) is None


def test_main_refuses_in_ci_before_any_card_call(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: "GITHUB_ACTIONS")
    rc = smoke.main(["--yes"])
    assert rc == _exit_codes.EXIT_ERROR
    assert not any(c[0] == "create_card" for c in fake.calls)


def test_main_login_passes_otp_callback(monkeypatch):
    fake = _FakeCards()
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)
    monkeypatch.setattr(smoke, "make_otp_callback", lambda: lambda prompt: "000000")
    captured = {}

    def fake_setup(*, otp_callback=None):
        captured["otp_callback"] = otp_callback
        return {"email": "user@example.com"}

    monkeypatch.setattr(smoke.auth, "setup", fake_setup)
    rc = smoke.main(["--yes", "--login"])
    assert rc == _exit_codes.EXIT_OK
    assert captured["otp_callback"] is not None  # OTP path is actually wired


def test_main_discovery_sweep_closes_orphaned_smoke_card(monkeypatch):
    import datetime as _dt
    from types import SimpleNamespace as _NS

    frozen = _dt.datetime(2026, 6, 14, 21, 35, 12, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(smoke, "_now_utc", lambda: frozen)
    monkeypatch.setattr(smoke.uuid, "uuid4", lambda: _NS(hex="1a2b3c4d" + "0" * 24))
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} 20260614T213512Z-1a2b3c4d"

    fake = _FakeCards()
    orphan = _vcard("vc_orphan", f"{run_prefix} orphan")

    def exploding_create(parent, name, balance_cents, valid_to, *, client=None):
        fake.calls.append(("create_card", parent, name, balance_cents))
        raise RuntimeError("created remotely but mapping blew up")

    fake.create_card = exploding_create
    fake.list_cards = lambda *, client=None, **kw: [orphan]
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)
    smoke.main(["--yes"])
    assert ("close_card", "vc_orphan") in fake.calls


def test_main_bulk_partial_failure_card_is_still_closed_via_sweep(monkeypatch):
    import datetime as _dt
    from types import SimpleNamespace as _NS

    frozen = _dt.datetime(2026, 6, 14, 21, 35, 12, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(smoke, "_now_utc", lambda: frozen)
    monkeypatch.setattr(smoke.uuid, "uuid4", lambda: _NS(hex="1a2b3c4d" + "0" * 24))
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} 20260614T213512Z-1a2b3c4d"

    fake = _FakeCards()
    orphan = _vcard("vc_bulk_orphan", f"{run_prefix} bulk 0")

    def exploding_bulk(parent, rows, *, delay_seconds=2.0, client=None, **kw):
        fake.calls.append(("create_cards_bulk", parent, len(rows)))
        raise RuntimeError("bulk item 1 failed after item 0 was created")

    fake.create_cards_bulk = exploding_bulk
    fake.list_cards = lambda *, client=None, **kw: [_vcard("vc_new", run_prefix), orphan]
    _patch_cards(monkeypatch, fake)
    monkeypatch.setattr(smoke, "create_cards_bulk", exploding_bulk)
    monkeypatch.setattr(smoke, "_monotonic", _fake_clock_long())
    monkeypatch.setattr(smoke, "_refuse_in_ci", lambda env=None: None)
    smoke.main(["--yes", "--bulk", "2"])
    assert ("close_card", "vc_bulk_orphan") in fake.calls


def test_discover_smoke_leftovers_warns_loudly_when_listing_fails_after_create(monkeypatch):
    # Last line of money-safety defence: if the discovery sweep's own list_cards()
    # fails AFTER a create was attempted, a live card may be open. The harness must
    # emit a LOUD warning naming $110.01 and the run prefix, and must NOT raise out
    # (raising here would skip the cleanup() that follows it in main()'s finally).
    h = smoke.Harness(clock=_fake_clock_long())
    h.results.append(smoke.StepResult("create", False, 0.1, "boom"))  # a create was attempted

    def boom_list(*, client=None, **kw):
        raise RuntimeError("list_cards down")

    monkeypatch.setattr(smoke, "list_cards", boom_list)
    warnings = []
    monkeypatch.setattr(smoke, "_warn", warnings.append)
    run_prefix = f"{smoke.SMOKE_CARD_NAME_PREFIX} 20260614T213512Z-1a2b3c4d"
    smoke.discover_smoke_leftovers(h, run_prefix=run_prefix)  # must not raise
    assert warnings and "110.01" in warnings[0]
    assert run_prefix in warnings[0]


def test_discover_smoke_leftovers_warning_is_harmless_when_no_create(monkeypatch):
    # If discovery fails but NO create was attempted, nothing was created, so the
    # warning is the quiet/harmless variant (no $110.01 money alarm).
    h = smoke.Harness(clock=_fake_clock_long())  # no create step recorded

    def boom_list(*, client=None, **kw):
        raise RuntimeError("list_cards down")

    monkeypatch.setattr(smoke, "list_cards", boom_list)
    warnings = []
    monkeypatch.setattr(smoke, "_warn", warnings.append)
    smoke.discover_smoke_leftovers(h, run_prefix="extendvcc-smoke x")  # must not raise
    assert warnings and "harmless" in warnings[0]
    assert "110.01" not in warnings[0]
