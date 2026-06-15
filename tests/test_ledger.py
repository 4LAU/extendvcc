from __future__ import annotations

import json
import os

import pytest

from extendvcc import ledger
from extendvcc._paths import configure as configure_paths


def _patch_ledger_path(monkeypatch, tmp_path):
    ledger_path = tmp_path / "vault" / "paywithextend" / "cards.jsonl"
    configure_paths(ledger_path=ledger_path)
    return ledger_path


def _read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


SENSITIVE_FIELD_NAMES = (
    "pan",
    "PAN",
    "cardPAN",
    "cardPan",
    "primaryAccountNumber",
    "accountNumber",
    "creditCardNumber",
    "ccNumber",
    "cardCvc",
    "cardCVC",
    "cardCVV",
    "cardCVV2",
    "CVC",
    "CVV",
    "CVV2",
    "cvv2",
    "cardSecurityCode",
    "cardVerificationCode",
    "verificationCode",
    "cardVerificationValue",
    "verificationValue",
    "cardSecurityValue",
    "securityValue",
    "cardIdentificationNumber",
    "cid",
    "CID",
    "csc",
    "CSC",
    "cvn",
    "CVN",
    "cardSecurityNumber",
    "cardValidationCode",
    "cardValidationValue",
    "vcn",
)


def test_append_writes_card_record(monkeypatch, tmp_path):
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)

    row = ledger.append(
        {
            "card_id": "vc_1",
            "credit_card_id": "cc_1",
            "name": "Service-Account-001",
            "last4": "1234",
            "status": "ACTIVE",
            "balance_cents": 5000,
        }
    )

    assert row["record_type"] == "card"
    assert row["card_id"] == "vc_1"
    assert _read_rows(ledger_path) == [row]

    configure_paths()


def test_ledger_file_created_owner_only(monkeypatch, tmp_path):
    # Invariant: the JSONL ledger is owner-only (0600) from its very first write,
    # so a world-readable umask cannot leak the local card audit trail.
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)
    old_umask = os.umask(0o022)  # typical umask -> would otherwise yield 0644
    try:
        ledger.record_pending("create", "Service-Account-001")
        assert ledger_path.exists()
        assert oct(os.stat(ledger_path).st_mode & 0o777) == "0o600"
    finally:
        os.umask(old_umask)

    configure_paths()


def test_append_rejects_pan_and_duplicate_card_id(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.append({"card_id": "vc_1", "name": "safe", "status": "ACTIVE"})

    with pytest.raises(ValueError, match="card already exists"):
        ledger.append({"card_id": "vc_1", "name": "duplicate", "status": "ACTIVE"})

    with pytest.raises(ValueError, match="sensitive"):
        ledger.append(
            {
                "card_id": "vc_2",
                "name": "unsafe",
                "pan": "4111111111111111",
            }
        )

    for index, field_name in enumerate(SENSITIVE_FIELD_NAMES, start=1):
        with pytest.raises(ValueError, match="sensitive"):
            ledger.append(
                {
                    "card_id": f"vc_sensitive_{index}",
                    "name": "unsafe",
                    field_name: "123",
                }
            )

    configure_paths()


def test_record_and_resolve_pending_with_card_record(monkeypatch, tmp_path):
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)

    pending = ledger.record_pending("create", "Service-Account-001")
    resolved = ledger.resolve_pending(
        "Service-Account-001",
        "confirmed",
        response_id="req_123",
        card_record={
            "id": "vc_1",
            "name": "Service-Account-001",
            "last4": "1234",
            "status": "ACTIVE",
        },
    )

    rows = _read_rows(ledger_path)
    assert pending["status"] == "pending"
    assert resolved["status"] == "confirmed"
    assert resolved["response_id"] == "req_123"
    assert rows[0]["record_type"] == "operation"
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["resolved_at"]
    assert rows[1]["record_type"] == "card"
    assert rows[1]["card_id"] == "vc_1"

    configure_paths()


def test_resolve_pending_failure_updates_operation(monkeypatch, tmp_path):
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("close", "vc_1-close")

    resolved = ledger.resolve_pending("vc_1-close", "failed", error="timeout")

    rows = _read_rows(ledger_path)
    assert resolved["status"] == "failed"
    assert rows == [resolved]
    assert rows[0]["error"] == "timeout"

    configure_paths()


def test_update_rewrites_atomically_and_preserves_other_rows(monkeypatch, tmp_path):
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)
    ledger.append({"card_id": "vc_1", "name": "Alpha", "status": "ACTIVE"})
    ledger.append({"card_id": "vc_2", "name": "Beta", "status": "ACTIVE"})
    before_inode = ledger_path.stat().st_ino

    updated = ledger.update("vc_1", status="CLOSED", closed_at="2026-05-30T18:00:00Z")

    rows = _read_rows(ledger_path)
    after_inode = ledger_path.stat().st_ino
    assert updated["status"] == "CLOSED"
    assert rows[0]["card_id"] == "vc_1"
    assert rows[0]["status"] == "CLOSED"
    assert rows[1]["card_id"] == "vc_2"
    assert rows[1]["status"] == "ACTIVE"
    assert before_inode != after_inode
    leftovers = [path for path in ledger_path.parent.glob("cards.jsonl.*") if path.name != "cards.jsonl.lock"]
    assert leftovers == []

    configure_paths()


def test_query_filters_cards_only_by_status_and_name(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "pending-alpha")
    ledger.append({"card_id": "vc_1", "name": "Alpha SaaS", "status": "ACTIVE"})
    ledger.append({"card_id": "vc_2", "name": "Beta Tool", "status": "CLOSED"})
    ledger.append({"card_id": "vc_3", "name": "Alpha Backup", "status": "CLOSED"})

    matches = ledger.query(status="closed", name_pattern="alpha")

    assert [row["card_id"] for row in matches] == ["vc_3"]

    configure_paths()


def test_sync_reconciles_with_injected_fetcher(monkeypatch, tmp_path):
    ledger_path = _patch_ledger_path(monkeypatch, tmp_path)
    ledger.append(
        {
            "card_id": "vc_existing",
            "name": "Existing",
            "status": "ACTIVE",
            "last4": "1111",
        }
    )

    summary = ledger.sync(
        fetcher=lambda: [
            {"id": "vc_existing", "name": "Existing", "status": "CLOSED", "last4": "1111"},
            {"id": "vc_new", "name": "New", "status": "ACTIVE", "last4": "2222"},
        ]
    )

    rows = _read_rows(ledger_path)
    by_id = {row["card_id"]: row for row in rows}
    assert summary == {
        "fetched": 2,
        "added": ["vc_new"],
        "updated": {"vc_existing": {"status": "CLOSED"}},
        "unchanged": [],
    }
    assert by_id["vc_existing"]["status"] == "CLOSED"
    assert by_id["vc_new"]["status"] == "ACTIVE"

    configure_paths()


def test_sync_refuses_when_disabled(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "extendvcc.client.assert_not_disabled",
        lambda: (_ for _ in ()).throw(RuntimeError("disabled")),
    )

    with pytest.raises(RuntimeError, match="disabled"):
        ledger.sync(fetcher=lambda: [{"id": "vc_1"}])

    configure_paths()


def test_sensitive_fields_rejected_across_write_paths(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    for index, field_name in enumerate(SENSITIVE_FIELD_NAMES, start=1):
        card_id = f"vc_path_{index}"
        correlation_key = f"{card_id}-create"
        ledger.append({"card_id": card_id, "name": "Alpha", "status": "ACTIVE"})
        ledger.record_pending("create", correlation_key)

        with pytest.raises(ValueError, match="sensitive"):
            ledger.resolve_pending(
                correlation_key,
                "confirmed",
                card_record={"id": f"{card_id}_resolved", "name": "Beta", field_name: "123"},
            )

        with pytest.raises(ValueError, match="sensitive"):
            ledger.update(card_id, **{field_name: "123"})

        with pytest.raises(ValueError, match="sensitive"):
            ledger.sync(
                fetcher=lambda field_name=field_name: [
                    {"id": f"vc_sync_{field_name}", "name": "Gamma", field_name: "123"}
                ]
            )

    configure_paths()


def test_numeric_luhn_pan_values_rejected_across_write_paths(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    pan_values = (
        4111111111111111,
        5555555555554444,
        378282246310005,
        6011111111111117,
    )

    for index, pan_value in enumerate(pan_values, start=1):
        card_id = f"vc_numeric_{index}"
        correlation_key = f"{card_id}-create"
        ledger.append({"card_id": card_id, "name": "Alpha", "status": "ACTIVE"})
        ledger.record_pending("create", correlation_key)

        with pytest.raises(ValueError, match="PAN"):
            ledger.append({"card_id": f"{card_id}_append", "name": "Unsafe", "memo": pan_value})

        with pytest.raises(ValueError, match="PAN"):
            ledger.resolve_pending(
                correlation_key,
                "confirmed",
                card_record={"id": f"{card_id}_resolved", "name": "Beta", "memo": pan_value},
            )

        with pytest.raises(ValueError, match="PAN"):
            ledger.update(card_id, memo=pan_value)

        with pytest.raises(ValueError, match="PAN"):
            ledger.sync(
                fetcher=lambda pan_value=pan_value: [{"id": f"vc_sync_{pan_value}", "name": "Gamma", "memo": pan_value}]
            )

    configure_paths()


def test_find_pending_returns_none_for_unknown_key(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "key-alpha")

    assert ledger.find_pending("key-unknown") is None

    configure_paths()


def test_find_pending_returns_none_after_resolve(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "key-resolve")
    ledger.resolve_pending("key-resolve", "failed", error="x")

    assert ledger.find_pending("key-resolve") is None

    configure_paths()


def test_list_pending_returns_all_pending_rows(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "key-a")
    ledger.record_pending("update", "key-b")
    ledger.record_pending("close", "key-c")
    ledger.resolve_pending("key-c", "failed", error="x")

    rows = ledger.list_pending()

    keys = [r["correlation_key"] for r in rows]
    assert keys == ["key-a", "key-b"]
    assert all(r["status"] == "pending" for r in rows)

    configure_paths()


def test_list_pending_filters_by_intent(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "key-create-1")
    ledger.record_pending("update", "key-update-1")
    ledger.record_pending("create", "key-create-2")

    rows = ledger.list_pending(intent="create")

    keys = [r["correlation_key"] for r in rows]
    assert keys == ["key-create-1", "key-create-2"]

    configure_paths()


def test_list_pending_excludes_resolved_rows(monkeypatch, tmp_path):
    _patch_ledger_path(monkeypatch, tmp_path)
    ledger.record_pending("create", "key-will-resolve")
    ledger.record_pending("create", "key-stays-pending")
    ledger.resolve_pending("key-will-resolve", "confirmed")

    rows = ledger.list_pending(intent="create")

    assert len(rows) == 1
    assert rows[0]["correlation_key"] == "key-stays-pending"

    configure_paths()
