# Testing Policy

Write a test only when a wrong result would be **silent**. If a failure produces a
crash, traceback, or non-zero exit, the program is already the test — don't add one.

## Principles

- **Offline only.** Every test runs without network. No real Extend API calls, no
  real IMAP connections. Mock only at the outermost I/O boundary — the HTTP client
  (impit), the filesystem, the IMAP connection. Never mock internal modules or the
  math; feed real numbers and assert real numbers.
- **Prefer running the tool over writing a test.** For most changes the fastest
  proof is to exercise the real code path:
  - `scripts/smoke_test.py` runs a live card lifecycle end to end.
  - `--dry-run` on every destructive command previews the exact request body with
    no API call.
  - the account-risk kill switch halts automation before a bad request goes out.
- **A test earns its place by naming the invariant it protects.** If it can't state
  what silent wrong result it catches, delete it.

## Behaviors that earn a test

Wrong values here look plausible and reach production unnoticed:

- **`cards.py` amount math** — `held_cents`, and the recurrence-balance trap in
  `build_update_card_operation`: a `balanceCents` override on a recurring card must
  also rewrite the nested `recurrence.balanceCents`, or the two limits silently
  disagree.
- **`cards.py` body assembly and wire parsing** — `create_card` / `bulk_create`
  request bodies, and `_map_virtual_card` parsing the Extend response (a dropped or
  misparsed field surfaces as a plausible-but-wrong card).
- **Credential masking** — `cli.py` `_mask_card_number` / `_cmd_reveal` and
  `client.py` `_scrub_payload`. A regression here leaks a PAN or CVC into logs or
  serialized output with no visible error.
- **`ledger.py`** — the sensitive-data guard (never persist PAN/CVC-shaped keys)
  and the atomic write (concurrent writes must not corrupt the JSONL file).
- **`auth.py`** — the SRP signature math. Wrong output fails login opaquely.

## Property-based testing (hypothesis)

Use property tests where random inputs can expose silent numeric or serialization
corruption: cent/dollar conversions (reversible, never negative, no precision
loss), the ledger sensitive-data guard (never permits a PAN/CVC-shaped key), and
session round-trips (save then load preserves every field).
