# Live smoke-test harness: design

**Date:** 2026-06-14
**Status:** Approved (design), pending implementation plan
**Author:** AI-generated, AI-reviewed

## Problem

Every test in `tests/` runs offline against fakes. That is a deliberate rule: the
suite must never touch the real Extend account. The cost is a blind spot. Nothing
in the project ever talks to the live API, so the suite cannot catch the one class
of bug that matters most in practice: the code disagreeing with what Extend
actually returns.

That blind spot already bit once. The v0.1.0 login bug (device SRP salt and
verifier read as negative by Cognito) passed all 110 unit tests and still failed
on the first real login. The fakes matched the code; the code did not match
reality.

A coverage number does not fix this. Line coverage proves a function *ran* during
a test, not that it behaves correctly against the live service. The missing layer
is a real end-to-end check the maintainer can run on demand.

## Goal

One opt-in script that drives the full card lifecycle against the live Extend API,
proving the tool works end to end, and reports a clear pass/fail per step. It is
run by hand before a release. It is never part of the offline suite and never runs
in CI.

## Non-goals

- **No CI integration.** Running this in GitHub Actions would mean storing the
  maintainer's real Extend credentials and IMAP secrets in a public repository's
  automation, and creating a real card on every run. That is a security and
  account-safety line we do not cross. Local, manual invocation only.
- **No `enroll` / `activate` coverage.** Those commands switch on a real parent
  credit card. That is a hard-to-reverse action against the maintainer's banking
  relationship, not a disposable test artifact. They are documented as excluded.
- **No replacement for the offline suite.** This complements unit tests; it does
  not replace them. The offline suite stays the gate for correctness of logic; the
  smoke harness is the gate for agreement with reality.

## Approach

A single script, `scripts/smoke_test.py`, placed outside `tests/` so pytest never
collects it. It walks a fixed sequence of steps, each a named check that passes or
fails. The created card is tracked from the moment it exists, and a cleanup block
guarantees it is cancelled and closed even if a later step raises.

### Why a script, not a pytest file

If it lived in `tests/`, pytest would collect it by default and a stray
`pytest tests/` would hit the live account. Keeping it as a standalone script under
`scripts/` makes the separation physical, not just conventional. The harness's pure
helpers still get normal offline unit tests (see Testing).

## The lifecycle walk

Each numbered item is one check with a name, a pass/fail result, and a timing.

1. **auth** — Load the saved session. If absent or expired, run a full login
   (Cognito SRP plus IMAP email-OTP). This also exercises the token path that the
   v0.1.0 bug lived in.
2. **accounts** — List accounts; assert the organization is present.
3. **issuers** — List issuers and parent credit cards; select a parent credit card
   for the create step (first active card, or `--parent <id>` to override).
4. **create** — Create one one-time virtual card: `balance_cents = 11001`
   (`$110.01`, a distinctive amount that is easy to spot on a statement if cleanup
   ever fails), `valid_to` set a few days out, display name
   `extendvcc-smoke <ISO-timestamp>`. Capture the returned card id immediately and
   register it for cleanup.
5. **get** — Fetch the card by id; assert the returned fields match what was sent
   (name, balance, status).
6. **list** — List cards; assert the new card id appears.
7. **reveal** — Reveal PAN, CVC, and expiry. Validate without printing: PAN passes
   a Luhn check and is 15-16 digits, CVC is 3-4 digits, expiry parses and is in the
   future. Values are checked and discarded. Only a last-4 mask may be logged.
8. **update** — Change the card (new display name); re-fetch and assert the change
   took.
9. **usage** — Fetch active-card usage; assert the response shape
   (`used` / `remaining` / `limit`).
10. **cancel** — Cancel the card; assert the returned status.
11. **close** — Close the card; assert the terminal status.
12. **ledger** — Assert the local JSONL ledger recorded the create as
    pending then confirmed, plus the later mutations.

### Local-only safe commands

`reconcile`, `status`, and `clear-disabled` act on local state only and are safe to
run against a real session. They each get a dedicated read-only check appended after
the lifecycle. `clear-disabled` runs last and only if the kill switch is set, so a
normal run does not toggle state.

### Optional bulk check

`--bulk K` adds a step that creates `K` cards via the bulk path (each tagged
`extendvcc-smoke`, each registered for cleanup) and then closes them all. Off by
default because it multiplies real account activity.

## Cleanup guarantee

The harness keeps a list of every card id it created. A `finally` block walks that
list and cancels then closes each one, regardless of whether the run succeeded or a
step raised partway through. Cleanup runs even on `KeyboardInterrupt`.

If cleanup itself fails (the close call errors), the harness does not swallow it: it
prints a loud, explicit warning naming the card id and the `$110.01` amount so the
maintainer can close it by hand, and it forces a non-zero exit. A card left open on
the live account is the worst outcome, so it is the loudest one.

## Safety controls

- **Confirmation prompt.** Because it mutates the live account, the harness prints
  what it will do and asks for confirmation before the first write. `--yes` skips
  the prompt for scripted local runs.
- **Minimum footprint.** A single low-limit, short-dated card; closed within the
  same run.
- **Identifiable artifact.** The `extendvcc-smoke <timestamp>` name and the
  `$110.01` amount make any leftover card unmistakable.
- **No secrets printed.** Reads the same env vars as the CLI
  (`EXTENDVCC_EMAIL`, `EXTENDVCC_PASSWORD`, `EXTENDVCC_IMAP_*`). Never prints card
  numbers, CVCs, or tokens; masks to last 4 where logging is needed.

## Output

- A checklist to stderr: one line per step with ✅ / ❌ and elapsed time, then a
  summary line `N/N checks passed`.
- Exit code 0 only if every check passed; non-zero otherwise, reusing the package's
  existing exit-code constants.
- `--json` emits a machine-readable result (step names, pass/fail, durations,
  created/cleaned card ids with PAN/CVC redacted) for record-keeping.

## Coverage map: every function accounted for

The plan ships a table mapping each public CLI command and client card method to
the step that exercises it. This is the literal "audit every function" artifact.

| Command / method | Covered by | Notes |
|---|---|---|
| `login` / auth refresh | step 1 | full login when no session |
| `accounts` | step 2 | |
| `issuers` | step 3 | also selects parent card |
| `create` / `create_card` | step 4 | |
| `card` / `get_card` | step 5 | |
| `cards` / `list_cards` | step 6 | |
| `reveal` / `reveal_card` | step 7 | validated, never printed |
| `update` / `update_card` | step 8 | |
| `usage` | step 9 | |
| `cancel` / `cancel_card` | step 10 | |
| `close` / `close_card` | step 11 | |
| `reconcile` | local-only check | safe, local state |
| `status` | local-only check | safe, local state |
| `clear-disabled` | local-only check | runs only if disabled |
| `bulk` / `create_cards_bulk` | step `--bulk K` | opt-in |
| `enroll`, `activate` | excluded | switch on a real credit card; not reversible |

Anything not hit by a step is listed here with the reason, so "every function" is a
checkable claim, not a vibe.

## Testing the harness

The live walk is manual by nature and is not unit-tested against fakes (that would
recreate the exact blind spot it exists to close). Its pure, side-effect-free
helpers are unit-tested offline in `tests/test_smoke.py`:

- the Luhn / format validators for PAN, CVC, expiry
- the step-runner (records pass/fail, timing, continues to cleanup on error)
- the cleanup-tracking logic (every created id gets cleaned, cleanup runs on raise)
- output formatting and exit-code selection
- the last-4 masking helper

These run with fakes, assert behavior, and keep the harness's own logic honest
without touching the network.

## File layout

```
scripts/
  smoke_test.py        # the harness (not collected by pytest)
tests/
  test_smoke.py        # offline unit tests for the pure helpers
docs/
  smoke-testing.md     # how to run it, what it does, the coverage table
```

`README` gains a short "Release smoke test" pointer to `docs/smoke-testing.md`.
`CONTRIBUTING` notes the harness must be run before tagging a release.
