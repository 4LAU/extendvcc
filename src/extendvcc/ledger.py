"""JSONL ledger for PayWithExtend virtual cards.

The Extend API is the source of truth, but this ledger is the local audit trail
for created cards and in-flight mutations. It intentionally never stores PAN or
CVC data.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import re
import tempfile
import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from filelock import FileLock

from extendvcc._jsonl import append_jsonl

logger = logging.getLogger(__name__)


def _ledger_path() -> Path:
    from extendvcc._paths import ledger_path
    return ledger_path()


CARD_RECORD_TYPE = "card"
OPERATION_RECORD_TYPE = "operation"
PENDING_STATUS = "pending"
RESOLVED_STATUSES = {"confirmed", "failed"}

_SENSITIVE_FIELD_NAMES = {
    "account_number",
    "card_number",
    "card_identification_number",
    "card_security_number",
    "card_security_value",
    "card_validation_code",
    "card_validation_value",
    "card_verification_code",
    "card_verification_value",
    "cid",
    "cc_number",
    "credit_card_number",
    "csc",
    "cvc",
    "cvv",
    "cvn",
    "cvv2",
    "full_card_number",
    "vcn",
    "number",
    "pan",
    "primary_account_number",
    "security_code",
    "verification_code",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "_cvc",
    "cvc_",
    "_cvv",
    "cvv_",
    "security_code",
    "verification_code",
)
_SENSITIVE_COMPACT_KEY_FRAGMENTS = (
    "accountnumber",
    "cardnumber",
    "cardidentificationnumber",
    "cardsecuritynumber",
    "cardsecurityvalue",
    "cardvalidationcode",
    "cardvalidationvalue",
    "cardverificationvalue",
    "ccnumber",
    "creditcardnumber",
    "cid",
    "csc",
    "cvc",
    "cvv",
    "cvn",
    "pan",
    "primaryaccountnumber",
    "securitycode",
    "securitynumber",
    "securityvalue",
    "validationcode",
    "validationvalue",
    "verificationcode",
    "verificationvalue",
)
_PAN_CANDIDATE_RE = re.compile(r"(?:\d[ -]?){13,19}")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")


def append(card_record: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Append a new card row to the ledger.

    Raises:
        ValueError: if the row has no card id, duplicates an existing card id,
            or contains PAN/CVC-like data.
    """
    record = _normalize_card_record(card_record)
    card_id = record["card_id"]

    with _ledger_lock():
        records = _read_records_unlocked()
        if _find_card_index(records, card_id) is not None:
            raise ValueError(f"card already exists in ledger: {card_id}")
        append_jsonl(_ledger_path(), [record], fsync=True)
    return record


def record_pending(intent: str, correlation_key: str) -> dict[str, Any]:
    """Record a pending mutation before dispatching it to Extend."""
    if not intent:
        raise ValueError("intent is required")
    if not correlation_key:
        raise ValueError("correlation_key is required")

    record = {
        "record_type": OPERATION_RECORD_TYPE,
        "status": PENDING_STATUS,
        "intent": str(intent),
        "correlation_key": str(correlation_key),
        "created_at": _now_iso(),
    }

    with _ledger_lock():
        records = _read_records_unlocked()
        if _find_pending_index(records, str(correlation_key)) is not None:
            raise ValueError(f"pending operation already exists: {correlation_key}")
        append_jsonl(_ledger_path(), [record], fsync=True)
    return record


def resolve_pending(
    correlation_key: str,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    """Resolve a pending operation as ``confirmed`` or ``failed``.

    If ``card_record=...`` is supplied while confirming, the card row is added
    in the same locked atomic rewrite as the operation resolution.
    """
    if not correlation_key:
        raise ValueError("correlation_key is required")
    resolved_status = str(status).lower()
    if resolved_status not in RESOLVED_STATUSES:
        raise ValueError(f"status must be one of {sorted(RESOLVED_STATUSES)}")
    _assert_no_sensitive_data(fields)

    card_record = fields.pop("card_record", None)
    normalized_card = None
    if card_record is not None:
        if resolved_status != "confirmed":
            raise ValueError("card_record can only be stored for confirmed operations")
        normalized_card = _normalize_card_record(card_record)

    with _ledger_lock():
        records = _read_records_unlocked()
        pending_index = _find_pending_index(records, str(correlation_key))
        if pending_index is None:
            raise KeyError(f"no pending operation found: {correlation_key}")

        operation = dict(records[pending_index])
        operation.update(fields)
        operation["status"] = resolved_status
        operation["resolved_at"] = _now_iso()
        records[pending_index] = operation

        if normalized_card is not None:
            card_id = normalized_card["card_id"]
            existing_card_index = _find_card_index(records, card_id)
            if existing_card_index is None:
                records.append(normalized_card)
            else:
                records[existing_card_index] = {
                    **records[existing_card_index],
                    **normalized_card,
                }

        _atomic_write_records_unlocked(records)
    return operation


def update(card_id: str, **fields: Any) -> dict[str, Any]:
    """Update a card row by ``card_id`` using lock + atomic rewrite."""
    if not card_id:
        raise ValueError("card_id is required")
    if "card_id" in fields:
        raise ValueError("card_id cannot be changed")
    _assert_no_sensitive_data(fields)

    with _ledger_lock():
        records = _read_records_unlocked()
        card_index = _find_card_index(records, str(card_id))
        if card_index is None:
            raise KeyError(f"card not found in ledger: {card_id}")

        card = dict(records[card_index])
        card.update(fields)
        records[card_index] = card
        _atomic_write_records_unlocked(records)
    return card


def query(
    status: str | None = None,
    name_pattern: str | None = None,
) -> list[dict[str, Any]]:
    """Return card rows matching the optional status and name filters."""
    status_filter = _status_value(status)
    name_re = re.compile(name_pattern, re.IGNORECASE) if name_pattern else None

    with _ledger_lock():
        records = _read_records_unlocked()

    matches: list[dict[str, Any]] = []
    for record in records:
        if not _is_card_record(record):
            continue
        if status_filter is not None and _status_value(record.get("status")) != status_filter:
            continue
        if name_re is not None and not name_re.search(str(record.get("name", ""))):
            continue
        matches.append(dict(record))
    return matches


def find_pending(correlation_key: str) -> dict[str, Any] | None:
    """Return a copy of the pending operation row for ``correlation_key``, or None."""
    if not correlation_key:
        return None
    with _ledger_lock():
        records = _read_records_unlocked()
        index = _find_pending_index(records, correlation_key)
        if index is None:
            return None
        return dict(records[index])


def list_pending(intent: str | None = None) -> list[dict[str, Any]]:
    """Return copies of all pending operation rows, optionally filtered by intent."""
    with _ledger_lock():
        records = _read_records_unlocked()
    result: list[dict[str, Any]] = []
    for record in records:
        if record.get("record_type") != OPERATION_RECORD_TYPE:
            continue
        if record.get("status") != PENDING_STATUS:
            continue
        if intent is not None and record.get("intent") != intent:
            continue
        result.append(dict(record))
    return result


def sync(fetcher: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Fetch all Extend cards and reconcile them into the local ledger.

    ``fetcher`` is injectable for tests and future card-operation code. If it
    is omitted, this performs the read-only ``GET /virtualcards`` path directly.
    """
    from .client import assert_not_disabled

    assert_not_disabled()
    fetched_cards = _normalize_cards_response(_fetch_cards(fetcher))

    added: list[str] = []
    updated: dict[str, dict[str, Any]] = {}
    unchanged: list[str] = []

    with _ledger_lock():
        records = _read_records_unlocked()

        for fetched in fetched_cards:
            card = _normalize_card_record(fetched)
            card_id = card["card_id"]
            card_index = _find_card_index(records, card_id)

            if card_index is None:
                records.append(card)
                added.append(card_id)
                continue

            current = records[card_index]
            changed_fields = {
                key: value
                for key, value in card.items()
                if current.get(key) != value
            }
            if changed_fields:
                records[card_index] = {**current, **changed_fields}
                updated[card_id] = changed_fields
            else:
                unchanged.append(card_id)

        _atomic_write_records_unlocked(records)

    return {
        "fetched": len(fetched_cards),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
    }


@contextmanager
def _ledger_lock() -> Iterable[None]:
    """Hold an advisory lock for all ledger writes and consistent reads."""
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock = FileLock(str(lock_path))
    with lock:
        yield


def _read_records_unlocked() -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            # Skip (don't raise) so one torn line — e.g. a crash mid-append or a
            # Syncthing conflict — cannot brick every ledger read and all card ops.
            logger.error("skipping malformed ledger JSON at %s:%d: %s", path, lineno, exc)
            continue
        if not isinstance(record, dict):
            logger.error("skipping non-object ledger row at %s:%d", path, lineno)
            continue
        records.append(record)
    return records


def _atomic_write_records_unlocked(records: list[dict[str, Any]]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            for record in records:
                tmp_file.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
                tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _fetch_cards(fetcher: Callable[[], Any] | None) -> Any:
    if fetcher is None:
        # Default to the page-based list_cards paginator (the scheme the live
        # Extend API actually uses). Lazy import avoids a cards<->ledger cycle.
        from . import cards

        fetcher = cards.list_cards

    result = fetcher()
    if inspect.isawaitable(result):
        return _run_awaitable(result)
    return result


def _run_awaitable(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("ledger.sync cannot run an async fetcher inside an active event loop")


def _normalize_cards_response(response: Any) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, Mapping):
        for key in ("cards", "items", "data", "results", "virtual_cards", "virtualCards"):
            value = response.get(key)
            if isinstance(value, list):
                return value
        return [response]
    if isinstance(response, list):
        return response
    return list(response)


def _normalize_card_record(card_record: Mapping[str, Any] | Any) -> dict[str, Any]:
    record = _object_to_dict(card_record)
    record = {_snake_key(str(key)): value for key, value in record.items()}
    if "card_id" not in record and "id" in record:
        record["card_id"] = record.pop("id")
    if "name" not in record and "display_name" in record:
        record["name"] = record["display_name"]
    record["record_type"] = CARD_RECORD_TYPE

    card_id = record.get("card_id")
    if not card_id:
        raise ValueError("card_record requires card_id or id")
    record["card_id"] = str(card_id)

    # Coerce date/datetime values to ISO strings so json.dumps does not crash.
    record = {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in record.items()
    }

    _assert_no_sensitive_data(record)
    return record


def _object_to_dict(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "dict"):
        return dict(value.dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError("card record must be a mapping or dataclass-like object")


def _find_card_index(records: list[dict[str, Any]], card_id: str) -> int | None:
    for index, record in enumerate(records):
        if _is_card_record(record) and str(record.get("card_id")) == card_id:
            return index
    return None


def _find_pending_index(records: list[dict[str, Any]], correlation_key: str) -> int | None:
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if (
            record.get("record_type") == OPERATION_RECORD_TYPE
            and record.get("correlation_key") == correlation_key
            and record.get("status") == PENDING_STATUS
        ):
            return index
    return None


def _is_card_record(record: Mapping[str, Any]) -> bool:
    record_type = record.get("record_type")
    return record_type == CARD_RECORD_TYPE or (record_type is None and "card_id" in record)


def _assert_no_sensitive_data(value: Any, path: str = "record") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            normalized_key = _snake_key(key_text).lower()
            compact_key = normalized_key.replace("_", "")
            if normalized_key in _SENSITIVE_FIELD_NAMES or any(
                fragment in f"_{normalized_key}_" for fragment in _SENSITIVE_KEY_FRAGMENTS
            ) or any(
                fragment in compact_key for fragment in _SENSITIVE_COMPACT_KEY_FRAGMENTS
            ) or _looks_like_credential_key(compact_key):
                raise ValueError(f"refusing to store sensitive card field: {path}.{key_text}")
            _assert_no_sensitive_data(item, f"{path}.{key_text}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_no_sensitive_data(item, f"{path}[{index}]")
        return
    if isinstance(value, (str, int, float, Decimal)) and _contains_luhn_pan(str(value)):
        raise ValueError(f"refusing to store possible PAN value at {path}")


def _looks_like_credential_key(compact_key: str) -> bool:
    if compact_key in {"cid", "csc", "cvc", "cvv", "cvn", "cvv2"}:
        return True
    if any(marker in compact_key for marker in ("verification", "validation", "security")):
        return any(suffix in compact_key for suffix in ("code", "value", "number"))
    if any(marker in compact_key for marker in ("card", "creditcard", "account", "primaryaccount")):
        return "number" in compact_key or "pan" in compact_key
    if "identification" in compact_key:
        return "number" in compact_key
    return False


def _contains_luhn_pan(value: str) -> bool:
    for match in _PAN_CANDIDATE_RE.finditer(value):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 13 and len(set(digits)) > 1 and _luhn_valid(digits):
            return True
    return False


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _snake_key(key: str) -> str:
    return _CAMEL_BOUNDARY_RE.sub("_", key).replace("-", "_").lower()


def _status_value(status: Any) -> str | None:
    if status is None:
        return None
    value = getattr(status, "value", status)
    return str(value).lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
