import datetime
import importlib.util
import pathlib
import sys

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
