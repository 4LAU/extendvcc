# extendvcc

Unofficial CLI and Python client for the Extend virtual card API.

> **Disclaimer**
>
> This is an unofficial, reverse-engineered client for Extend's private browser
> API (`api.paywithextend.com`). It is not affiliated with, endorsed by, or
> supported by Extend, Inc. Use at your own risk. Your Extend account may be
> suspended for using automation against their private API.

## Install

```bash
pip install extendvcc
# or
pipx install extendvcc
```

Standalone binary (no Python required): download from [GitHub Releases](../../releases).

## Quick Start

```bash
# Log in (interactive email + password, device remembered)
extendvcc login

# List parent cards
extendvcc accounts

# List virtual cards
extendvcc cards
```

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
# Show masked card number and CVC
extendvcc reveal <card-id>

# Save full credentials to a file (0600 permissions)
extendvcc reveal <card-id> --json creds.json
```

## Environment Variables

| Variable | Purpose |
|---|---|
| `EXTENDVCC_EMAIL` | Extend account email (overrides interactive prompt) |
| `EXTENDVCC_PASSWORD` | Extend account password (overrides interactive prompt) |
| `EXTENDVCC_IMAP_USER` | IMAP email for automatic OTP retrieval |
| `EXTENDVCC_IMAP_PASSWORD` | IMAP app password |
| `EXTENDVCC_IMAP_HOST` | IMAP server (default: `imap.gmail.com`) |
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
```

See `extendvcc.__init__` for the full list of exported functions and models.

## Security Notes

- The ledger never stores PAN or CVC data.
- `reveal` saves credentials with `0600` file permissions.
- Session tokens are stored locally with restricted permissions.
- All HTTP uses Chrome TLS fingerprinting via `impit`.

## License

[MIT](LICENSE)
