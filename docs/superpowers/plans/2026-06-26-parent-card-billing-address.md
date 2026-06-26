# Parent Card Billing Address Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `update_credit_card_address()` and an `update-account` CLI command to change a parent (SOURCE) credit card's billing address via `PUT /creditcards/{id}`.

**Architecture:** Read-modify-write mirroring the existing `update_card` precedent. GET the full card object, override only the nested `address` object (merged, not replaced), round-trip every other field byte-for-byte, PUT it back through the existing `_ledger_flow` audit wrapper. A generic `build_update_credit_card_operation(id, overrides, *, fetcher)` shaper is shared by the real path and the CLI dry-run.

**Tech Stack:** Python 3.11+, impit HTTP client, argparse CLI, pytest (offline fakes), ruff.

**Design source:** `docs/plans/2026-06-26-parent-card-billing-address-design.md`

---

## Key decisions (locked in brainstorming + /challenge)

- **No flat-field mirror.** The captured browser PUT changed only the nested `address` object and left the flat top-level `address1`/`city`/… stale. We do the same — override the nested object, round-trip the stale flat fields untouched.
- **Merge, never replace, the nested `address`.** Build `{**raw["address"], **new}` so unknown nested keys (e.g. `countryCode`) survive.
- **Generic builder** does a one-level dict merge for dict-valued overrides, so `address` merges instead of replacing. No address-specific builder.
- **Thin-GET guard in code.** If `GET /creditcards/{id}` returns only the thin `{id,last4,status,displayName}` keyset (the shape `list_credit_cards` returns), raise rather than risk blanking parent-card fields. This is the challenge's "gate" enforced at runtime.
- **`postal` stays a string** (leading-zero ZIPs).
- **AVS caveat:** this updates the *stored* address; whether it reaches the checkout AVS check is unverified and the operator will confirm live. Noted in the docstring only.

---

## File Structure

- **Modify** `src/extendvcc/cards.py` — add `_THIN_CREDIT_CARD_KEYS`, `build_update_credit_card_operation`, `_credit_card_address_overrides`, `update_credit_card_address`.
- **Modify** `src/extendvcc/__init__.py` — export `update_credit_card_address`.
- **Modify** `src/extendvcc/cli.py` — add `_cmd_update_account`, `_update_account_dry_run`, parser subcommand, `_COMMANDS` entry.
- **Modify** `tests/test_cards.py` — library tests (synthetic raw credit card, builder + function).
- **Modify** `tests/test_cli.py` — CLI wiring + dry-run test.
- **Modify** `README.md` — document the `update-account` command.
- **Modify** `docs/smoke-testing.md` — add the GET-gate + update-account smoke step.

---

## Task 1: Generic credit-card update builder + thin-GET guard

**Files:**
- Modify: `src/extendvcc/cards.py` (add after `build_update_card_operation`, ~line 552)
- Test: `tests/test_cards.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cards.py` (near the other update tests). Place the shared fixture once, above the tests:

```python
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
_CC_PUT_RESP = {
    "creditCard": {"id": "cc_synth1", "last4": "1040", "status": "ACTIVE", "displayName": "Parent Card"}
}


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
```

`tests/test_cards.py` imports `pytest` (line 7) and `from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled` (line 11) but **not** `PayWithExtendError`. Add it to that import line:

```python
from extendvcc.client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cards.py -k "build_update_credit_card" -v`
Expected: FAIL — `AttributeError: module 'extendvcc.cards' has no attribute 'build_update_credit_card_operation'`

- [ ] **Step 3: Write the implementation**

In `src/extendvcc/cards.py`, add after `build_update_card_operation` (after ~line 552):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cards.py -k "build_update_credit_card" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/extendvcc/cards.py tests/test_cards.py
git commit -m "feat(cards): credit-card update builder with merge + thin-GET guard"
```

---

## Task 2: `update_credit_card_address` (overrides helper + ledgered mutation)

**Files:**
- Modify: `src/extendvcc/cards.py` (add after Task 1's builder)
- Test: `tests/test_cards.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cards.py`:

```python
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
    assert body["address1"] == "400 Old St"          # flat untouched
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
    """A successful update resolves the update-cc:{id} pending row to confirmed."""
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
    assert ledger.find_pending("update-cc:cc_synth1") is None  # resolved, not pending
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
```

`PayWithExtendAPIError` is already imported (line 11). `ledger.find_pending` exists (`src/extendvcc/ledger.py:243`). The 4xx test's `PayWithExtendAPIError(...)` passes `status_code` and `path` because both are required keyword args.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cards.py -k "update_credit_card_address" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'update_credit_card_address'`

- [ ] **Step 3: Write the implementation**

In `src/extendvcc/cards.py`, add after the Task 1 builder:

```python
_CREDIT_CARD_ADDRESS_REQUIRED = ("address1", "city", "province", "postal")


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
    missing = [f for f in _CREDIT_CARD_ADDRESS_REQUIRED if not address.get(f)]
    if missing:
        raise ValueError(f"update_credit_card_address: address missing required field(s): {missing}")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cards.py -k "update_credit_card_address" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/extendvcc/cards.py tests/test_cards.py
git commit -m "feat(cards): update_credit_card_address with ledgered RMW"
```

---

## Task 3: Public API export

**Files:**
- Modify: `src/extendvcc/__init__.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cards.py` (or wherever public-API imports are checked — a standalone test is fine):

```python
def test_update_credit_card_address_is_public():
    import extendvcc

    assert hasattr(extendvcc, "update_credit_card_address")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cards.py -k "is_public" -v`
Expected: FAIL — `AttributeError: module 'extendvcc' has no attribute 'update_credit_card_address'`

- [ ] **Step 3: Add the export**

In `src/extendvcc/__init__.py`, add `update_credit_card_address` to the import block from `extendvcc.cards`:

```python
from extendvcc.cards import (
    activate_credit_card,
    cancel_card,
    close_card,
    create_card,
    create_cards_bulk,
    enroll_credit_card,
    get_card,
    list_cards,
    list_credit_cards,
    list_issuers,
    reconcile,
    reveal_card,
    update_card,
    update_credit_card_address,
    usage,
)
```

And add it to `__all__` under the enroll grouping:

```python
    # enroll
    "activate_credit_card",
    "enroll_credit_card",
    "update_credit_card_address",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cards.py -k "is_public" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/extendvcc/__init__.py tests/test_cards.py
git commit -m "feat(api): export update_credit_card_address"
```

---

## Task 4: CLI `update-account` command (with dry-run)

**Files:**
- Modify: `src/extendvcc/cli.py` (handler near `_cmd_update` ~line 509; parser ~line 778; `_COMMANDS` ~line 813)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (reuse the existing `_bang` helper and `main` import):

```python
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
            "update-account", "cc_1",
            "--address1", "1 New Rd", "--city", "Newtown",
            "--province", "CA", "--postal", "95051", "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    body = json.loads(captured.out)
    assert body["address"]["address1"] == "1 New Rd"   # override applied in merged body
    assert body["address1"] == "400 Old St"            # flat field untouched
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
            "update-account", "cc_1",
            "--address1", "1 New Rd", "--city", "Newtown",
            "--province", "CA", "--postal", "02134", "--country", "US", "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert seen["card_id"] == "cc_1"
    assert seen["address"] == {
        "address1": "1 New Rd", "address2": "", "city": "Newtown",
        "province": "CA", "postal": "02134",
    }
    assert seen["country"] == "US"
    assert "Updated: cc_1" in captured.out
```

Confirm `_bang` exists in `tests/test_cli.py` (used by `test_update_dry_run_no_put` at line 361). If `_bang` is a no-arg raiser, the `builtins.input` patch above is correct.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "update_account" -v`
Expected: FAIL — argparse exits 2 (`invalid choice: 'update-account'`)

- [ ] **Step 3: Implement the handlers**

In `src/extendvcc/cli.py`, add after `_update_dry_run` (~line 553):

```python
def _cmd_update_account(args: argparse.Namespace) -> int:
    from .cards import update_credit_card_address

    address = {
        "address1": args.address1,
        "address2": getattr(args, "address2", "") or "",
        "city": args.city,
        "province": args.province,
        "postal": args.postal,
    }
    country = getattr(args, "country", None)

    if getattr(args, "dry_run", False):
        return _update_account_dry_run(args, address, country)

    _info(f"Updating billing address for {args.id}:")
    _info(f"  {address['address1']}, {address['city']}, {address['province']} {address['postal']}")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        _info("Cancelled.")
        return EXIT_ERROR

    card = update_credit_card_address(args.id, address, country=country)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Updated: {card.id} (last4={card.last4}, status={card.status.value})")
    return EXIT_OK


def _update_account_dry_run(args: argparse.Namespace, address: dict[str, Any], country: str | None) -> int:
    """Preview an address update. The read-only GET is allowed (non-destructive) so
    the merged PUT body is accurate; no mutation is performed."""
    from .cards import _credit_card_address_overrides, _default_client, build_update_credit_card_operation

    client = _default_client()
    overrides = _credit_card_address_overrides(address, country)
    operation = build_update_credit_card_operation(
        args.id,
        overrides,
        fetcher=lambda: client.get(f"/creditcards/{args.id}"),
    )
    _info(f"[dry-run] update-account {args.id} — overrides: {overrides}. No mutation made.")
    print(_json_out(operation["body"]))
    return EXIT_OK
```

In `_build_parser`, add after the `update` subparser block (~line 783):

```python
    # update-account
    p = sub.add_parser("update-account", help="Update a parent credit card's billing address")
    p.add_argument("id", help="Credit card ID (cc_...)")
    p.add_argument("--address1", required=True, help="Billing address line 1")
    p.add_argument("--address2", default="", help="Billing address line 2")
    p.add_argument("--city", required=True, help="Billing city")
    p.add_argument("--province", required=True, help="Billing state/province")
    p.add_argument("--postal", required=True, help="Billing postal/ZIP code (string; leading zeros kept)")
    p.add_argument("--country", default=None, help="Country code (defaults to the card's current value)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="Preview the merged PUT body (read-only GET, no mutation)")
```

In `_COMMANDS`, add the entry (after `"update": _cmd_update,`):

```python
    "update-account": _cmd_update_account,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "update_account" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/extendvcc/cli.py tests/test_cli.py
git commit -m "feat(cli): add update-account command for parent-card billing address"
```

---

## Task 5: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/smoke-testing.md`

- [ ] **Step 1: README**

Find the command/usage list in `README.md` (where `enroll`, `update`, `accounts` are documented) and add an entry in the same style as the surrounding ones. Example block to adapt to the existing format:

````markdown
### Update a parent card's billing address

```bash
extendvcc update-account cc_2vtDvRzWDB19myGfM9naDD \
  --address1 "357 Dawson Drive" \
  --city "Santa Clara" \
  --province CA \
  --postal 95051
```

Preview the exact request body without mutating (does a read-only GET):

```bash
extendvcc update-account cc_... --address1 "..." --city "..." --province CA --postal 95051 --dry-run
```

Note: this changes the *stored* billing address on the parent (SOURCE) card. Whether
it affects address verification (AVS) at checkout is issuer-dependent.
````

If `README.md` has a Python API section listing functions, add `update_credit_card_address` there too, matching the existing format.

- [ ] **Step 2: smoke-testing.md**

Add a step to `docs/smoke-testing.md` (matching the existing numbered/bulleted style):

````markdown
### Update parent-card billing address

1. **Gate — confirm the GET is full.** First verify `GET /creditcards/{id}` returns the
   full object (not the thin id/last4/status/displayName shape). The dry-run does this
   read-only GET; if the card object in the printed body has only those four keys, stop:

   ```bash
   extendvcc update-account <cc_id> --address1 "1 Test St" --city "Testville" \
     --province CA --postal 95051 --dry-run
   ```

   The printed JSON should be a full card object with the nested `address` overridden.
   `build_update_credit_card_operation` also raises on a thin GET as a safety net.

2. **Apply for real**, then confirm via `extendvcc accounts` / a live transaction that
   the address took effect.
````

- [ ] **Step 3: Commit**

```bash
git add README.md docs/smoke-testing.md
git commit -m "docs: document update-account command and smoke step"
```

---

## Task 6: Full verification

- [ ] **Step 1: Run the full Definition of Done**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```

Expected: all three clean, all tests pass. If `ruff format --check` fails, run `uv run ruff format src/ tests/` and re-commit.

- [ ] **Step 2: Commit any formatting fixes**

```bash
git add -A
git commit -m "style: ruff format"
```

(Only if there were formatting changes. Stage files individually if mixing with other work.)

---

## Task 7: Staff Audit

- [ ] Run `/staffcheck`
- [ ] Fix all findings

---

## Task 8: Code Cleanup

- [ ] Run `/simplify` on all changed code (`cards.py`, `cli.py`, `__init__.py`)
- [ ] Fix any issues found

---

## Task 9: Manual Tasks for L

- [ ] **Verify the GET-gate against a real card (the one real-world unknown).** Run the
      dry-run in Task 5 / smoke step 1 against an actual `cc_...` id. If the printed body
      is a full card object with the nested `address` overridden, the design holds. If it
      is thin (only id/last4/status/displayName) or the command raises the thin-GET error,
      **stop and report** — the round-trip approach needs revisiting before any live PUT.
- [ ] **Confirm AVS behavior live (optional but recommended).** After a real
      `update-account`, run a small test charge to confirm the new address actually
      satisfies address verification. The library cannot verify this offline.
- [ ] No migrations, secrets, or external configuration are required.

---

## Self-Review Notes

- **Spec coverage:** builder (Task 1), function + overrides helper + ledger + validation (Task 2), export (Task 3), CLI + dry-run + JSON + postal-string (Task 4), README + smoke (Task 5), DoD (Task 6), post-impl staffcheck/simplify/manual (Tasks 7–9). All design sections map to a task.
- **Type consistency:** `build_update_credit_card_operation(id, overrides, *, fetcher)`, `_credit_card_address_overrides(address, country)`, `update_credit_card_address(id, address, *, country, client)`, ledger intent/key `update-cc` / `update-cc:{id}` — names identical across all tasks.
- **No placeholders:** every code/test step shows complete code and exact commands.
