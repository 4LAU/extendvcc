"""Tests for the extendvcc CLI (offline only).

Every test names a CLI-layer invariant that fails *silently* if broken:
exit-code contract drift, --json stdout contamination, credential leakage to
stdout, dry-run performing real mutations, and the login password never being
written into the process environment. All card/auth/client functions are
monkeypatched at their source module so no network or filesystem I/O occurs.
"""

from __future__ import annotations

import json
import os

import pytest

from extendvcc.auth import PayWithExtendAuthError, SessionNotFound
from extendvcc.cli import main
from extendvcc.client import (
    AccountRiskDetected,
    PayWithExtendAPIError,
    PayWithExtendDisabled,
    PayWithExtendError,
)


def _bang(*args, **kwargs):
    raise AssertionError("mutating/network function must not be called")


# ---------------------------------------------------------------------------
# 1. Exit-code mapping — exceptions and usage errors map to the stable contract
#    in _exit_codes.py. Drift here silently breaks CI scripts that branch on
#    the exit code.
# ---------------------------------------------------------------------------


def test_create_mutually_exclusive_flags_exit_usage():
    """Invariant: --valid-to + --period together is a CLI usage error -> exit 2."""
    code = main(
        [
            "create",
            "--credit-card-id",
            "cc",
            "--name",
            "n",
            "--balance-cents",
            "100",
            "--valid-to",
            "2030-01-01",
            "--period",
            "MONTHLY",
        ]
    )
    assert code == 2


def test_create_missing_schedule_exit_usage():
    """Invariant: create with neither --valid-to nor --period -> exit 2."""
    code = main(["create", "--credit-card-id", "cc", "--name", "n", "--balance-cents", "100"])
    assert code == 2


def test_bulk_missing_file_exit_usage(tmp_path):
    """Invariant: bulk with a nonexistent CSV -> exit 2."""
    missing = tmp_path / "nope.csv"
    code = main(["bulk", str(missing), "--credit-card-id", "cc"])
    assert code == 2


def test_update_no_fields_exit_usage():
    """Invariant: update with no field flags -> exit 2."""
    code = main(["update", "vc_1"])
    assert code == 2


def test_clear_disabled_without_manual_exit_usage():
    """Invariant: clear-disabled without --manual -> exit 2."""
    code = main(["clear-disabled"])
    assert code == 2


def test_session_not_found_exit_auth_required(monkeypatch):
    """Invariant: an auth error (SessionNotFound) -> exit 3."""
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(SessionNotFound("no session")))
    code = main(["cards"])
    assert code == 3


def test_generic_auth_error_exit_auth_required(monkeypatch):
    """Invariant: base PayWithExtendAuthError -> exit 3."""
    monkeypatch.setattr(
        "extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(PayWithExtendAuthError("auth"))
    )
    code = main(["cards"])
    assert code == 3


def test_disabled_exit_disabled(monkeypatch):
    """Invariant: PayWithExtendDisabled (kill switch) -> exit 4."""
    monkeypatch.setattr(
        "extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(PayWithExtendDisabled("disabled"))
    )
    code = main(["cards"])
    assert code == 4


def test_account_risk_exit_disabled(monkeypatch):
    """Invariant: AccountRiskDetected (subclass of Disabled) -> exit 4."""
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(AccountRiskDetected("risk")))
    code = main(["cards"])
    assert code == 4


def test_api_error_exit_api_error(monkeypatch):
    """Invariant: PayWithExtendAPIError -> exit 5."""
    exc = PayWithExtendAPIError("boom", status_code=500, path="/x")
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(exc))
    code = main(["cards"])
    assert code == 5


def test_generic_library_error_exit_error(monkeypatch):
    """Invariant: base PayWithExtendError -> exit 1."""
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(PayWithExtendError("oops")))
    code = main(["cards"])
    assert code == 1


def test_library_valueerror_is_not_usage(monkeypatch):
    """Invariant: a library-internal ValueError (not CLIInputError) -> exit 1, NOT 2.

    A plain ValueError must not be mistaken for a CLI usage error.
    """
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(ValueError("internal")))
    code = main(["cards"])
    assert code == 1


def test_no_subcommand_exit_usage():
    """Invariant: no subcommand -> exit 2."""
    assert main([]) == 2


def test_argparse_bad_input_exit_usage():
    """Invariant: argparse rejects missing required flags via SystemExit(2)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["create", "--credit-card-id", "cc", "--balance-cents", "100"])  # missing --name
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 2. --json stdout isolation — under --json, stdout must be pure JSON. Stray
#    human text would silently corrupt machine consumers that pipe stdout.
# ---------------------------------------------------------------------------


def test_json_login_stdout_is_pure_json(monkeypatch, capsys):
    """Invariant: --json login emits only JSON to stdout; 'Logged in as' is not on stdout."""
    monkeypatch.setattr("extendvcc.auth.setup", lambda **kw: {"email": "u@x.com", "org_id": "o", "session_path": "/p"})
    monkeypatch.setattr("extendvcc.imap_otp.make_otp_callback", lambda: lambda: "000000")
    monkeypatch.setenv("EXTENDVCC_EMAIL", "u@x.com")
    monkeypatch.setenv("EXTENDVCC_PASSWORD", "secret")

    code = main(["--json", "login"])
    captured = capsys.readouterr()

    assert code == 0
    parsed = json.loads(captured.out)
    assert parsed["email"] == "u@x.com"
    assert "Logged in as" not in captured.out


def test_json_empty_list_stdout_is_empty_array(monkeypatch, capsys):
    """Invariant: --json on an empty cards list -> stdout is '[]'; 'No virtual cards' not on stdout."""
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: [])

    code = main(["--json", "cards"])
    captured = capsys.readouterr()

    assert code == 0
    assert json.loads(captured.out) == []
    assert "No virtual cards" not in captured.out


def test_json_error_path_stdout_has_no_human_text(monkeypatch, capsys):
    """Invariant: under --json, error text goes to stderr only; stdout stays clean."""
    monkeypatch.setattr("extendvcc.cards.list_cards", lambda **kw: (_ for _ in ()).throw(PayWithExtendError("oops")))

    code = main(["--json", "cards"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "oops" in captured.err


# ---------------------------------------------------------------------------
# 3. reveal masking (security, Tier 1) — full PAN/CVC must never reach stdout
#    on any code path. A leak here exposes live card credentials in shell
#    history / CI logs / agent transcripts.
# ---------------------------------------------------------------------------

_FAKE_CREDS = {
    "number": "4111111111111111",
    "cvc": "737",
    "last4": "1111",
    "expires": "2030-12",
}


def test_reveal_human_masks_pan_and_cvc(monkeypatch, capsys):
    """Invariant: human reveal never prints the full PAN or the CVC to stdout."""
    monkeypatch.setattr("extendvcc.cards.reveal_card", lambda _id: dict(_FAKE_CREDS))

    code = main(["reveal", "vc_1"])
    captured = capsys.readouterr()

    assert code == 0
    assert _FAKE_CREDS["number"] not in captured.out
    assert _FAKE_CREDS["cvc"] not in captured.out
    assert "****" in captured.out


def test_reveal_json_masks_pan_and_cvc(monkeypatch, capsys):
    """Invariant: --json reveal (no file path) emits masked JSON; raw PAN/CVC absent."""
    monkeypatch.setattr("extendvcc.cards.reveal_card", lambda _id: dict(_FAKE_CREDS))

    code = main(["--json", "reveal", "vc_1"])
    captured = capsys.readouterr()

    assert code == 0
    parsed = json.loads(captured.out)
    assert parsed["cvc"] == "****"
    assert _FAKE_CREDS["number"] not in captured.out
    assert _FAKE_CREDS["cvc"] not in captured.out


def test_reveal_json_path_writes_0600_file_no_stdout_leak(monkeypatch, capsys, tmp_path):
    """Invariant: --json-path writes full creds to a 0600 file; stdout never holds raw PAN/CVC."""
    monkeypatch.setattr("extendvcc.cards.reveal_card", lambda _id: dict(_FAKE_CREDS))
    out_file = tmp_path / "creds.json"

    code = main(["reveal", "vc_1", "--json-path", str(out_file)])
    captured = capsys.readouterr()

    assert code == 0
    assert oct(os.stat(out_file).st_mode & 0o777) == "0o600"
    on_disk = json.loads(out_file.read_text())
    assert on_disk["number"] == _FAKE_CREDS["number"]
    assert on_disk["cvc"] == _FAKE_CREDS["cvc"]
    assert _FAKE_CREDS["number"] not in captured.out
    assert _FAKE_CREDS["cvc"] not in captured.out


# ---------------------------------------------------------------------------
# 4. --dry-run — must perform NO mutation and NO network call. A dry-run that
#    silently mutates is a financial-exposure incident.
# ---------------------------------------------------------------------------


def test_create_dry_run_no_mutation_no_network(monkeypatch, capsys):
    """Invariant: create --dry-run never calls create_card or account_context."""
    monkeypatch.setattr("extendvcc.cards.create_card", _bang)
    monkeypatch.setattr("extendvcc.cards.account_context", _bang)

    code = main(
        [
            "create",
            "--credit-card-id",
            "cc",
            "--name",
            "n",
            "--balance-cents",
            "100",
            "--valid-to",
            "2030-01-01",
            "--recipient",
            "r@x.com",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    json.loads(captured.out)  # stdout is parseable JSON body
    assert "[dry-run]" in captured.err  # human plan to stderr


def test_create_dry_run_approximate_when_no_recipient_no_session(monkeypatch, capsys):
    """Invariant: create --dry-run falls back to placeholder -> preview is approximate.

    The body must be flagged so a consumer knows the recipient is not exact.
    """
    monkeypatch.setattr("extendvcc.cards.create_card", _bang)
    monkeypatch.setattr("extendvcc.cards.account_context", _bang)
    monkeypatch.setattr("extendvcc.auth.load_session", lambda: None)

    code = main(
        [
            "create",
            "--credit-card-id",
            "cc",
            "--name",
            "n",
            "--balance-cents",
            "100",
            "--valid-to",
            "2030-01-01",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "approximate" in captured.err


def test_bulk_dry_run_no_mutation_no_network(monkeypatch, capsys, tmp_path):
    """Invariant: bulk --dry-run never calls create_cards_bulk or account_context."""
    monkeypatch.setattr("extendvcc.cards.create_cards_bulk", _bang)
    monkeypatch.setattr("extendvcc.cards.account_context", _bang)
    monkeypatch.setattr("extendvcc.auth.load_session", lambda: None)

    csv_file = tmp_path / "cards.csv"
    csv_file.write_text("name,balance_cents,valid_to\nFoo,1000,2030-01-01\n")

    code = main(["bulk", str(csv_file), "--credit-card-id", "cc", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    json.loads(captured.out)
    assert "[dry-run]" in captured.err


def test_cancel_dry_run_no_mutation(monkeypatch, capsys):
    """Invariant: cancel --dry-run never calls cancel_card."""
    monkeypatch.setattr("extendvcc.cards.cancel_card", _bang)

    code = main(["cancel", "vc_1", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    json.loads(captured.out)


def test_close_dry_run_no_mutation(monkeypatch, capsys):
    """Invariant: close --dry-run never calls close_card and skips the confirm prompt."""
    monkeypatch.setattr("extendvcc.cards.close_card", _bang)
    monkeypatch.setattr("builtins.input", _bang)  # confirmation must be skipped

    code = main(["close", "vc_1", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    json.loads(captured.out)


def test_update_dry_run_no_put(monkeypatch, capsys):
    """Invariant: update --dry-run may GET (read-only) but never calls update_card (the PUT)."""
    monkeypatch.setattr("extendvcc.cards.update_card", _bang)

    class _FakeClient:
        def get(self, path):
            return {"virtualCard": {"displayName": "old", "balanceCents": 100}}

    monkeypatch.setattr("extendvcc.cards._default_client", lambda: _FakeClient())

    code = main(["update", "vc_1", "--name", "new", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    body = json.loads(captured.out)
    assert body["displayName"] == "new"  # override applied to merged PUT body
    assert "[dry-run]" in captured.err


# ---------------------------------------------------------------------------
# 5. Confirmation prompt cancellation — answering 'n' must abort without
#    mutating, and the prompt must never pollute stdout.
# ---------------------------------------------------------------------------


def test_close_cancel_aborts_without_mutation(monkeypatch, capsys):
    """Invariant: close without --yes, answered 'n', does not call close_card; prompt on stderr."""
    monkeypatch.setattr("extendvcc.cards.close_card", _bang)
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    code = main(["close", "vc_1"])
    captured = capsys.readouterr()

    assert code == 1  # aborted confirm -> generic error
    assert "Proceed?" in captured.err
    assert "Proceed?" not in captured.out
    assert captured.out == ""


# ---------------------------------------------------------------------------
# 6. login no-secret-leak (Tier 1) — the CLI must never write the plaintext
#    password into the process environment, where it would leak to every
#    child process.
# ---------------------------------------------------------------------------


def test_login_does_not_write_password_to_environ(monkeypatch):
    """Invariant: login never sets EXTENDVCC_PASSWORD in os.environ."""
    captured_kwargs = {}

    def fake_setup(**kwargs):
        captured_kwargs.update(kwargs)
        return {"email": kwargs.get("email", "?")}

    monkeypatch.setattr("extendvcc.auth.setup", fake_setup)
    monkeypatch.setattr("extendvcc.imap_otp.make_otp_callback", lambda: lambda: "000000")
    monkeypatch.setenv("EXTENDVCC_EMAIL", "u@x.com")
    monkeypatch.delenv("EXTENDVCC_PASSWORD", raising=False)
    # Provide password via patched getpass, not env.
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-pw")

    code = main(["login"])

    assert code == 0
    assert captured_kwargs["password"] == "secret-pw"  # password reached setup
    assert os.environ.get("EXTENDVCC_PASSWORD") is None  # but never written to env


# ---------------------------------------------------------------------------
# update-account — parent-card billing address
# ---------------------------------------------------------------------------


def test_update_account_dry_run_no_put(monkeypatch, capsys):
    """update-account --dry-run may GET (read-only) but never calls the mutator."""
    monkeypatch.setattr("extendvcc.cards.update_credit_card_address", _bang)

    class _FakeClient:
        def get(self, path):
            return {
                "creditCard": {
                    "id": "cc_1",
                    "last4": "1040",
                    "status": "ACTIVE",
                    "displayName": "Parent",
                    "issuerId": "ii_x",
                    "address1": "400 Old St",
                    "address": {"address1": "400 Old St", "city": "Oldtown", "country": "US"},
                }
            }

    monkeypatch.setattr("extendvcc.cards._default_client", lambda: _FakeClient())

    code = main(
        [
            "update-account",
            "cc_1",
            "--address1",
            "1 New Rd",
            "--city",
            "Newtown",
            "--province",
            "CA",
            "--postal",
            "95051",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    body = json.loads(captured.out)
    assert body["address"]["address1"] == "1 New Rd"  # override applied in merged body
    assert body["address1"] == "400 Old St"  # flat field untouched
    assert "[dry-run]" in captured.err


def test_update_account_yes_skips_prompt(monkeypatch, capsys):
    """--yes maps flags into the address dict and calls the mutator without prompting."""
    seen = {}

    def _fake_update(card_id, address, *, country=None):
        seen["card_id"] = card_id
        seen["address"] = address
        seen["country"] = country
        from extendvcc.models import CardStatus, CreditCard

        return CreditCard(id=card_id, last4="1040", status=CardStatus.ACTIVE, display_name="Parent")

    monkeypatch.setattr("extendvcc.cards.update_credit_card_address", _fake_update)
    monkeypatch.setattr("builtins.input", _bang)  # prompt must NOT be reached

    code = main(
        [
            "update-account",
            "cc_1",
            "--address1",
            "1 New Rd",
            "--city",
            "Newtown",
            "--province",
            "CA",
            "--postal",
            "02134",
            "--country",
            "US",
            "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert seen["card_id"] == "cc_1"
    assert seen["address"] == {
        "address1": "1 New Rd",
        "address2": "",
        "city": "Newtown",
        "province": "CA",
        "postal": "02134",
    }
    assert seen["country"] == "US"
    assert "Updated: cc_1" in captured.out


def test_update_account_empty_address_rejected_in_dry_run(monkeypatch):
    """An empty required field is rejected even in --dry-run (no GET attempted)."""
    monkeypatch.setattr("extendvcc.cards._default_client", _bang)  # GET must NOT be reached

    code = main(
        [
            "update-account",
            "cc_1",
            "--address1",
            "",
            "--city",
            "Newtown",
            "--province",
            "CA",
            "--postal",
            "95051",
            "--dry-run",
        ]
    )
    assert code == 1  # library ValueError -> EXIT_ERROR
