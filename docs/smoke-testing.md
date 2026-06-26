# Release smoke test

`scripts/smoke_test.py` drives the full card lifecycle against the **real** Extend
API and cleans up after itself. Run it by hand before tagging a release. It is not
part of the offline `pytest` suite and refuses to run in CI: the script hard-stops
(before any auth or network) if a CI env marker (`CI`, `GITHUB_ACTIONS`, etc.) is
set — `--yes` does not bypass that guard.

## Why it exists

Every test under `tests/` runs offline against fakes, so nothing in the suite ever
talks to Extend. That is deliberate, but it means the suite cannot catch the code
disagreeing with what the live API actually returns. The v0.1.0 login bug passed
all unit tests and still failed on the first real login. This harness is the layer
that catches that class of drift — but note: by default the run **reuses a saved
session** (it refreshes, it does not cold-login). To actually exercise the
first-login/OTP path that the v0.1.0 bug lived in, pass `--login`, which forces a
full `auth.setup(otp_callback=make_otp_callback())` before the lifecycle — the same
IMAP-backed OTP callback the CLI uses, so the email OTP challenge actually
completes (without it, auth raises `OTPRequired` and the path is never tested).

## What it does

It creates one real virtual card at **$110.01** (`balanceCents = 11001`, a
distinctive amount), named `extendvcc-smoke <UTC-timestamp>-<8 hex>` (unique per
run, not just per day), then walks: list accounts, list issuers and parent cards,
create, fetch, list, reveal (validated, never printed), update, usage, cancel,
close. A `finally` block cancels and closes the card even if a step fails. Before
teardown the `finally` block also runs a **prefix-discovery sweep**: it lists live
cards and closes any still-open card whose name carries this run's unique smoke
prefix, so even a card created remotely but lost to a mid-flight error (e.g. a bulk
partial failure) is still cleaned up. Cleanup also verifies the close actually
returned a `CLOSED` status — a 200 with a non-closed status counts as a leftover,
not a success.

Cleanup is best-effort hardened, not an absolute guarantee: if the create call
creates a remote card and then the discovery sweep's own `list_cards()` also fails
(auth/kill-switch/network), or a `Ctrl-C` lands mid-cleanup, a live card can remain.
In that case the harness prints a loud warning naming the `$110.01` amount and the
run prefix and exits non-zero, and the unique prefix is printed at start, so you can
recover by listing the account and closing any card whose name starts with it.

## Run it

```bash
# Requires the same credentials the CLI uses:
#   EXTENDVCC_EMAIL, EXTENDVCC_PASSWORD, EXTENDVCC_IMAP_* (for first-time login)
uv run python scripts/smoke_test.py            # prompts before touching the account
uv run python scripts/smoke_test.py --login    # force a cold first-login (auth.setup + IMAP OTP) — exercises the OTP path (needs EXTENDVCC_IMAP_*)
uv run python scripts/smoke_test.py --yes      # skip the prompt (scripted local run)
uv run python scripts/smoke_test.py --json     # machine-readable report
uv run python scripts/smoke_test.py --parent cc_xxx   # pick a specific parent card
uv run python scripts/smoke_test.py --bulk 3   # also create/close 3 cards via bulk
```

Exit code is `0` only if every check passed and the test card was cleaned up. A
deliberate abort at the confirmation prompt returns a non-zero code (the package's
"aborted confirm" code), so a *skipped* run can never be mistaken for a *passed* one.
A failed step's exit code reflects the cause: disabled kill-switch, auth required,
API error, or generic error — broadly mirroring the CLI's own mapping (one
deliberate difference: an unexpected API *response shape* exits `5`/API-error here,
where the CLI uses the generic code, so release drift is loud).

## Coverage map

| Command / method | Covered by | Notes |
|---|---|---|
| session refresh | `accounts` step (first auth call) | `account_context()` refreshes an existing session; it does NOT cold-login |
| `login` / `setup` (cold first-login + OTP) | `--login` (opt-in) | runs `auth.setup(otp_callback=make_otp_callback())`; only this exercises the first-login/OTP path (needs `EXTENDVCC_IMAP_*`) |
| `accounts` / `account_context` | `accounts` step | |
| `issuers` / `list_issuers` | `issuers` step | |
| `list_credit_cards` | `issuers` step | also selects parent |
| `create` / `create_card` | `create` step | |
| `card` / `get_card` | `get` step | |
| `cards` / `list_cards` | `list` step | |
| `reveal` / `reveal_card` | `reveal` step | validated, never printed |
| `update` / `update_card` | `update` step | |
| `update-account` / `update_credit_card_address` | run manually (see below) | **excluded** — mutates a real parent card's billing address |
| `usage` | `usage` step | |
| `cancel` / `cancel_card` | `cancel` step + cleanup | |
| `close` / `close_card` | `close` step + cleanup | |
| `bulk` / `create_cards_bulk` | `--bulk K` | opt-in; drives the real `create_cards_bulk` helper with pacing disabled |
| `reconcile` | run manually: `extendvcc reconcile` | local state, safe |
| `status` | run manually: `extendvcc status` | local state, safe |
| `clear-disabled` | run manually only | toggles kill-switch state |
| `enroll`, `activate` | **excluded** | switch on a real credit card; not reversible |

`enroll`/`activate` are excluded on purpose: they activate a real credit card,
which is not a disposable test artifact. Verify those manually when the auth or
enrollment flow changes.

### Manual: `update-account` (parent-card billing address)

This mutates a real parent card, so it is not in the automated script. Verify it
manually — and **gate on the GET first**, since the full-object read-modify-write is
only safe if `GET /creditcards/{id}` returns the full card object:

```bash
# 1. Gate — dry-run does the read-only GET and prints the merged PUT body.
extendvcc update-account <cc_id> --address1 "1 Test St" --city "Testville" \
  --province CA --postal 95051 --dry-run
```

The printed JSON should be a **full** card object with the nested `address` overridden —
not the thin `{id,last4,status,displayName}` shape that `list_credit_cards` returns. If
it is thin (or the command raises the thin-GET error), **stop** — the round-trip is
unsafe and the design must be revisited. `build_update_credit_card_operation` also raises
on a thin GET as a runtime safety net.

```bash
# 2. Apply for real, then confirm via `extendvcc accounts` / a live transaction.
extendvcc update-account <cc_id> --address1 "1 Test St" --city "Testville" \
  --province CA --postal 95051 --yes
```
