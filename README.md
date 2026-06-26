# extendvcc

[![CI](https://github.com/4LAU/extendvcc/actions/workflows/ci.yml/badge.svg)](https://github.com/4LAU/extendvcc/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/extendvcc-cli.svg)](https://pypi.org/project/extendvcc-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue.svg)](https://pypi.org/project/extendvcc-cli/)

Unofficial CLI and Python client for the Extend virtual card API.

> **Disclaimer**
>
> This is an unofficial, independent client for Extend's private browser
> API (`api.paywithextend.com`). It is not affiliated with, endorsed by, or
> supported by Extend, Inc. Use at your own risk. Your Extend account may be
> suspended for running automation against their private API.

## Install

```bash
pip install extendvcc-cli
# or
pipx install extendvcc-cli
```

Standalone binary (no Python required): download from [GitHub Releases](../../releases).

## Quick Start

```bash
# Log in: prompts for email, password, and the emailed OTP. Device is remembered,
# so you won't be asked again on this machine for a while. No setup required.
extendvcc login

# List parent cards
extendvcc accounts

# List virtual cards
extendvcc cards
```

## Enroll a Parent Card

Enrolling a parent card is a three-step lifecycle:

```bash
# 1. Register the card (prompts for card number + CVC; triggers an issuer
#    verification email). Card details are typed at a prompt, never passed as flags.
extendvcc enroll \
  --display-name "My Amex" \
  --cardholder-name "Jane Doe" \
  --issuer-id iss_xxx \
  --expires 2028-12-31 \
  --address1 "123 Main St" \
  --city "Springfield" \
  --province "IL" \
  --postal "62701"

# 2. Your card issuer (e.g. Amex) emails you a verification request. Open it and
#    approve manually before activation will succeed.

# 3. Pull the card from PENDING to ACTIVE. Re-run if it still reports PENDING.
extendvcc activate <card-id>
```

Run `extendvcc issuers` to find the `--issuer-id` for your card.

## Update a Parent Card's Billing Address

Change the stored billing address on a parent (SOURCE) credit card:

```bash
extendvcc update-account cc_2vtDvRzWDB19myGfM9naDD \
  --address1 "357 Dawson Drive" \
  --city "Santa Clara" \
  --province CA \
  --postal 95051
```

`--address2` and `--country` are optional; an omitted `--country` keeps the card's
current value. Preview the exact request body without mutating (does a read-only GET):

```bash
extendvcc update-account cc_... --address1 "..." --city "..." --province CA --postal 95051 --dry-run
```

Note: this changes the *stored* billing address on the parent card. Whether it affects
address verification (AVS) at checkout is issuer-dependent and unverified — confirm
against a live transaction before relying on it.

## Create a One-Time Card

```bash
extendvcc create \
  --credit-card-id cc_xxx \
  --name "My Card" \
  --balance-cents 5000 \
  --valid-to 2026-12-31
```

## Create a Recurring Card

```bash
extendvcc create \
  --credit-card-id cc_xxx \
  --name "Monthly" \
  --balance-cents 10000 \
  --period MONTHLY \
  --by-month-day 1 \
  --terminator NONE
```

## Reveal Credentials

```bash
# Show masked card number and CVC on stdout
extendvcc reveal <card-id>

# Write full credentials to a file with 0600 permissions (owner-only)
extendvcc reveal <card-id> --json-path creds.json
```

`--json-path` writes the full PAN, CVC, and expiry to a file. Without it, the card number is masked on stdout. `--json` is a separate global flag that controls JSON output format; it does not write a file.

## Dry Run

Destructive commands accept `--dry-run` to preview the exact operation without
touching the network or mutating anything:

```bash
extendvcc create --credit-card-id cc_x --name "Test" --balance-cents 5000 --valid-to 2026-12-31 --dry-run
extendvcc bulk cards.csv --credit-card-id cc_x --dry-run
extendvcc cancel <card-id> --dry-run
extendvcc close <card-id> --dry-run
extendvcc update <card-id> --balance-cents 9000 --dry-run
extendvcc update-account <cc-id> --address1 "1 New Rd" --city "Newtown" --province CA --postal 95051 --dry-run
```

The would-be request body (or operation descriptor) is printed as JSON to **stdout**;
the human-readable plan goes to **stderr**. So `extendvcc create ... --dry-run > body.json`
captures just the JSON. `create`/`bulk` resolve the recipient locally (from `--recipient`,
else the saved session) and never call the API; the preview is labelled `approximate`
when the recipient falls back to a placeholder. `update --dry-run` performs only the
read-only GET needed to show the accurate merged PUT body, with no mutation.

## Exit Codes

The CLI uses stable exit codes so scripts and CI can branch on the outcome:

| Code | Name           | Meaning                                                   |
|------|----------------|-----------------------------------------------------------|
| 0    | OK             | Success.                                                  |
| 1    | ERROR          | Generic failure (library error, or an aborted confirmation). |
| 2    | USAGE          | Bad flags or CLI input validation (e.g. missing `--valid-to`). |
| 3    | AUTH_REQUIRED  | Login, OTP, or a saved session is required or failed.     |
| 4    | DISABLED       | Kill switch tripped (account-risk); run `clear-disabled --manual`. |
| 5    | API_ERROR      | Extend returned an error response.                        |

## Unattended / Automation Use

**You can skip this entire section for normal use.** `extendvcc login` prompts for
everything it needs. The variables below only matter when you want to run the CLI with
no human present (a cron job, CI pipeline, or script), where there's nothing to type
into a prompt.

To log in non-interactively, supply the credentials as environment variables. Extend
still requires a one-time code (OTP) emailed on login; set the IMAP variables so the CLI
can read that code from your inbox automatically. If IMAP retrieval times out, it falls
back to a manual prompt.

| Variable | Purpose |
|---|---|
| `EXTENDVCC_EMAIL` | Extend account email (replaces the email prompt) |
| `EXTENDVCC_PASSWORD` | Extend account password (replaces the password prompt) |
| `EXTENDVCC_IMAP_USER` | Inbox address to read the login OTP from |
| `EXTENDVCC_IMAP_PASSWORD` | IMAP app password for that inbox |
| `EXTENDVCC_IMAP_HOST` | IMAP server (default: `imap.gmail.com`) |

Inject these from your secrets manager or CI vault at runtime. Don't commit them to a file.

### Advanced overrides

Rarely needed. Change only if the defaults don't fit your setup.

| Variable | Purpose |
|---|---|
| `EXTENDVCC_STATE_DIR` | Override session/state directory |
| `EXTENDVCC_LEDGER_PATH` | Override ledger file path |
| `EXTENDVCC_BRAND_ID` | Override Extend brand ID |

## Python API

```python
from extendvcc import list_cards, get_card, create_card, reveal_card

# List all virtual cards
cards = list_cards()

# Get a single card by ID
card = get_card("card_id_here")

# Reveal card credentials (PAN, CVC, expiry)
creds = reveal_card("card_id_here")

# Update a parent card's billing address
from extendvcc import update_credit_card_address

update_credit_card_address(
    "cc_id_here",
    {"address1": "357 Dawson Drive", "city": "Santa Clara", "province": "CA", "postal": "95051"},
)
```

See `extendvcc.__init__` for the full list of exported functions and models.

## Testing

The `pytest` suite runs entirely offline against fakes. It never touches the network.

## Security Notes

- The ledger never stores PAN or CVC data.
- `reveal` saves credentials with `0600` file permissions.
- Session tokens are stored locally with restricted permissions.
- All HTTP uses Chrome TLS fingerprinting via `impit`.

## License

[MIT](LICENSE)
