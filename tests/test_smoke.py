import datetime
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

from extendvcc import _exit_codes
from extendvcc.auth import SessionNotFound
from extendvcc.client import PayWithExtendDisabled, PayWithExtendError
from extendvcc.models import CardStatus, CreditCard

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
