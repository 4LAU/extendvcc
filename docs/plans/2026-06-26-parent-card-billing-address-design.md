# Parent Card Billing Address Update — Design

**Date:** 2026-06-26
**Status:** Approved scope, pending implementation
**Author:** AI (brainstormed with L)

## Problem

There is no way to change a parent (source) credit card's billing address after
enrollment. The account owner needs the billing address on a source card to match
reality — primarily so transactions pass the issuer's address verification (AVS).

Extend exposes `PUT /creditcards/{id}` for this. The browser performs a full-object
read-modify-write: it GETs the card, changes the address, and PUTs the entire object
back. We mirror that.

## Scope

**In scope:** Update the billing **address only** (`address1`, `address2`, `city`,
`province`, `postal`, `country`).

**Out of scope (explicitly chosen):** credit limit (`issuedAmountCents`), display
name, company name, card image, currency, timezone, or any other field. No changes
to virtual-card editing.

## Key findings from the captured request

A live `PUT /creditcards/cc_2vtDvRzWDB19myGfM9naDD` payload showed:

- The body carries the address in **two places**: a nested `address` object **and**
  flat top-level fields (`address1`, `city`, `postal`, `province`, `country`).
- In the capture they **disagree**: the nested `address.address1` is the **new**
  value (`357 Dawson Drive`) while the flat top-level `address1` is the **stale old**
  value (`400 Dawson Drive`). This proves the server reads the **nested `address`
  object** and the browser round-trips the flat fields untouched.
- The credit-card object contains **no PAN/CVC** — safe to round-trip and (if needed)
  ledger without leaking secrets.

**Implementation consequence:** override **only** the nested `address` object (merged
over the GET's existing one), and round-trip the stale flat fields untouched — exactly
as the browser did. Do **not** mirror the new address into the flat fields; that would
deviate from the only ground-truth capture.

## Assumptions & kill list

1. **`GET /creditcards/{id}` returns the full card object** (same shape as the PUT
   body). Required for read-modify-write.
   - **Kill:** a single smoke `GET /creditcards/{id}` against a real card. If it
     returns the full object, the assumption holds. If thin, fall back to fetching
     from the `/creditcards` list (but that list is minimal, so this would force a
     redesign — verify first).
2. The server reads the nested `address` object — **confirmed** by the capture.
3. The credit-card object never contains PAN/CVC — **confirmed** by the capture.

## Design

### Library (`src/extendvcc/cards.py`)

**`build_update_credit_card_operation(credit_card_id, overrides, *, fetcher)`**

Generic operation shaper, mirroring `build_update_card_operation(card_id, overrides,
*, fetcher)` at cards.py:516 — `overrides` is a dict of top-level PUT-body keys to set.
Keeping it generic (not address-specific) means broadening scope later is a CLI-only
change, and it removes an address-specific abstraction.

- `fetcher()` performs the read-only `GET /creditcards/{id}`; its result is the
  starting body.
- Take the **full GET object** as the body and round-trip everything unchanged — no
  allowlist. This is faithful to the browser (which PUTs a full object), the object
  carries no secrets, and the server tolerates the extra fields per the capture.
  Trade-off, to be documented in the docstring: a full-object PUT is
  **last-writer-wins for the entire object** — a concurrent UI edit to any field is
  silently reverted. Near-zero risk for single-operator use; the note is for honesty.
- Apply `overrides` on top (e.g. `body["address"] = merged_address`).
- Return `{"method": "PUT", "path": "/creditcards/{id}", "body": ..., "preview_accuracy": "exact"}`.

**No flat-field mirror.** The capture proved the browser changes *only* the nested
`address` object and leaves the flat top-level `address1`/`city`/… **stale**. Writing
the new address into the flat fields would deviate from the only ground-truth we have.
So: override the nested `address` object only; pass the (now-stale) flat fields through
byte-for-byte. This is both more faithful and less code, and it preserves our ability
to detect later which field the server actually honors.

**Merge, never replace, the nested `address`.** The exact key set of the GET's nested
`address` object is unverified (no GET capture). Build the override as
`{**raw["address"], **new_address_fields}` so any nested key we don't know about (e.g.
a `countryCode`) is preserved rather than blanked.

**`update_credit_card_address(credit_card_id, address, *, country=None, client=None) -> CreditCard`**

- `address` is a dict: `address1` (required), `city` (required), `province` (required),
  `postal` (required), `address2` (optional, defaults `""`). Mirrors the `address`
  dict shape already used by `enroll_credit_card`. `postal` stays a **string**
  (leading-zero ZIPs like `02134` must survive).
- `country` optional; if given, sets the top-level `country` key (matching
  `enroll_credit_card`, which carries `country` at top level); if omitted, the existing
  value round-trips untouched.
- Builds `overrides = {"address": {**raw_address, **new_fields}}` (plus `country` when
  passed), calls `build_update_credit_card_operation`, then dispatches through the
  existing `_ledger_flow` for audit consistency:
  - key: `update-cc:{credit_card_id}`
  - intent: `update-cc`
  - `on_success`: a credit-card mapper (reuses the `_parse_credit_card` helper, like
    `enroll_credit_card`'s `_on_success`) returning `(CreditCard, {"credit_card_id": id})`.
- Returns the updated `CreditCard`. The `CreditCard` model is **unchanged** — success
  is the confirmation; the model stays minimal (it is shared with `list_credit_cards`).

Validation: reject an `address` missing any required field with a `ValueError` naming
the missing field(s), before any network call.

**AVS caveat (one line, per operator decision):** this updates the *stored* parent-card
address. Whether that address actually reaches the AVS check at checkout — vs. the
virtual card carrying its own address, or the issuer using its on-file value — is
unverified. The operator will confirm against a live charge. The docstring notes this.

### CLI (`src/extendvcc/cli.py`)

New subcommand **`update-account`** (parent cards are listed by `accounts`, so this
reads naturally):

```
extendvcc update-account <id> \
  --address1 "357 Dawson Drive" \
  --city "Santa Clara" \
  --province CA \
  --postal 95051 \
  [--address2 ""] \
  [--country US] \
  [--dry-run] [--yes]
```

- Required flags: `--address1`, `--city`, `--province`, `--postal`.
- Optional: `--address2`, `--country`, `--dry-run`, `--yes`.
- Confirmation prompt before the mutation (skipped with `--yes`), matching `enroll`/`close`.
- `--dry-run`: performs the read-only GET (non-destructive) and prints the merged PUT
  body to stdout, no mutation — exactly like `update`'s dry-run.
- Honors the global `--json` flag for machine-readable output, like every other
  command (`getattr(args, "json", False)`).
- `--postal` is a plain string argument (no `type=int`) so leading-zero ZIPs survive.
- Handler `_cmd_update_account`; registered in `_COMMANDS` and the parser.

### Public API (`src/extendvcc/__init__.py`)

Export `update_credit_card_address` (under the "mutations"/"enroll" grouping).

### Docs

- `README.md`: add the `update-account` command to the usage/commands section.
- `docs/smoke-testing.md`: add a smoke step for `update-account` (including the GET
  verification of assumption 1).

## Testing (`tests/`)

All offline, faked client at the I/O boundary, every test names an invariant:

1. **RMW overrides nested address** — fake GET returns a full card with old address;
   assert the PUT body's nested `address` object holds the new values.
2. **Nested address is merged, not replaced** — fake GET's nested `address` carries an
   extra unknown key (e.g. `countryCode`); assert it survives into the PUT body.
3. **Flat fields are NOT touched** — assert the stale flat top-level `address1`/`city`/…
   from the GET round-trip unchanged (faithful to the capture; guards against a
   regression that re-adds mirroring).
4. **Untouched fields preserved** — assert a non-address field from the GET (e.g.
   `displayName`, `issuedAmountCents`) round-trips unchanged into the PUT body.
5. **country sets top-level only when passed; preserved when omitted.**
6. **postal stays a string** — a leading-zero ZIP (`02134`) is not coerced to int.
7. **Missing required address field raises ValueError** before any network call.
8. **Dry-run makes no PUT** — only the GET is called; merged body printed.
9. **Ledger row written** — a confirmed `update-cc:{id}` row after success; failed row
   on a 4xx.
10. **CLI `update-account`** — wiring test: flags map to the `address` dict; `--yes`
    skips the prompt; `--dry-run` performs GET only; `--json` emits the card.

## Definition of Done

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/ -v
```

All three clean.

## Post-Implementation

1. **Gate on assumption 1 (do this FIRST, before writing the RMW)** — run a single
   read-only smoke `GET /creditcards/{id}` against a real card (see
   `docs/smoke-testing.md`). Confirm it returns the **full** object (not the thin
   id/last4/status/displayName shape that `list_credit_cards` returns) and note the
   nested `address` object's exact key set. If the GET is thin, **stop** — the
   round-trip RMW is unsafe (it could blank parent-card fields, affecting all child
   virtual cards) and the design must be revisited. No live PUT capture is available,
   so this is the gate that protects the whole approach.
2. **Staff audit** — run `/staffcheck`, fix all findings.
3. **Code cleanup** — run `/simplify` on all changed code.
4. **Manual tasks for L:** none beyond the smoke verification in step 1, which the
   implementer performs. No migrations, secrets, or external config.
