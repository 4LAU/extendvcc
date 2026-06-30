# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- `update-account` changes the stored billing address on a parent (SOURCE) credit card, with a matching `update_credit_card_address()` in the public API. It runs a full read-modify-write: GET the card, write the new address into **both** the nested `address` object (merged, so unknown fields survive) and the flat top-level fields (`address1`/`city`/`province`/`postal`/`country`), then PUT the whole object back. The server's `updateAccountRequest` validator reads the flat fields, so a body that set only the nested address is rejected with a 422 (`address1 must not be blank`); flat `country` is always populated, defaulting to the card's existing value when `--country` is omitted. `--dry-run` prints the exact request body from a read-only GET without mutating. `--address2` is preserve-on-omit (pass `""` to clear it), and an omitted `--country` keeps the card's current value. A safety check refuses to PUT when the GET returns a thin list-item shape, which would otherwise blank the parent card that backs your virtual cards. This updates the stored address only; whether it reaches the issuer's address check (AVS) at checkout is unverified.

---

## [0.2.0] - 2026-06-18

### Added

- `card` now shows a spend breakdown — **Limit**, **Spent**, **Held**, and **Available** — instead of only the available balance. "Held" is the pending-authorization amount, derived as `limit - spent - available`. The underlying `limitCents`, `spentCents`, and `lifetimeSpentCents` fields (previously dropped) are mapped onto `VirtualCard` and exposed via `--json`. Lines render only when the data is present, so list-style responses that omit these fields are unaffected.

---

## [0.1.2] - 2026-06-15

### Security

- IMAP OTP retrieval now verifies the mail server's TLS certificate and hostname. Previously the connection accepted any certificate, exposing the IMAP password and login OTP to a man-in-the-middle. A failed or untrusted certificate now falls back to the manual OTP prompt instead of connecting.
- All third-party GitHub Actions are pinned to commit SHAs to close a supply-chain risk from mutable version tags.

### Fixed

- Any IMAP failure (unreachable host, bad app password, mid-session drop) now degrades to the manual OTP prompt instead of crashing login.

---

## [0.1.1] - 2026-06-15

### Fixed

- First-time login failed at device registration with `Found negative value for salt or password verifier`. The device SRP salt and password verifier are now zero-padded so Cognito never reads them as negative.
- Authentication errors now include Cognito's error type and message instead of only a status code, so failures report the real reason.

---

## [0.1.0] - 2026-06-14

### Added

- **Authentication:** Cognito SRP login with device remembering; automatic token refresh; session persistence to disk with `0600` permissions.
- **Email OTP:** IMAP-based OTP retrieval for Cognito `EMAIL_OTP` challenges, configurable via `EXTENDVCC_IMAP_*` env vars.
- **Virtual card lifecycle:** create, list, get, update, cancel, close, and reveal (PAN + CVC + expiry) virtual cards.
- **Reveal:** masked stdout output by default; `--json-path` writes full credentials to a file with `0600` permissions.
- **Parent credit cards:** enroll and activate parent credit cards.
- **Bulk create:** create multiple virtual cards with configurable pacing to avoid rate limits.
- **Recurring cards:** support for `DAILY`, `WEEKLY`, and `MONTHLY` recurrence periods with configurable terminators.
- **JSONL audit ledger:** append-only ledger records every card mutation as pending → confirmed or failed; `reconcile` command flags unconfirmed entries.
- **Kill switch:** HTTP client disables itself on account-risk signals (403, WAF blocks, verification prompts) to avoid account suspension.
- **Chrome TLS fingerprinting:** all HTTP via `impit`; Extend blocks non-browser TLS profiles.
- **CLI:** full lifecycle commands: `login`, `accounts`, `issuers`, `cards`, `card`, `usage`, `enroll`, `activate`, `create`, `bulk`, `update`, `cancel`, `close`, `reveal`, `reconcile`, `status`, `clear-disabled`.
- **Python API:** public re-exports in `extendvcc.__init__` for programmatic use.
- **Typed:** `py.typed` marker (PEP 561); type hints throughout.

[Unreleased]: https://github.com/4LAU/extendvcc/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/4LAU/extendvcc/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/4LAU/extendvcc/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/4LAU/extendvcc/releases/tag/v0.1.0
