# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.1.0] — 2026-06-14

### Added

- **Authentication** — Cognito SRP login with device remembering; automatic token refresh; session persistence to disk with `0600` permissions.
- **Email OTP** — IMAP-based OTP retrieval for Cognito `EMAIL_OTP` challenges, configurable via `EXTENDVCC_IMAP_*` env vars.
- **Virtual card lifecycle** — create, list, get, update, cancel, close, and reveal (PAN + CVC + expiry) virtual cards.
- **Reveal** — masked stdout output by default; `--json-path` writes full credentials to a file with `0600` permissions.
- **Parent credit cards** — enroll and activate parent credit cards.
- **Bulk create** — create multiple virtual cards with configurable pacing to avoid rate limits.
- **Recurring cards** — support for `DAILY`, `WEEKLY`, and `MONTHLY` recurrence periods with configurable terminators.
- **JSONL audit ledger** — append-only ledger records every card mutation as pending → confirmed or failed; `reconcile` command flags unconfirmed entries.
- **Kill switch** — HTTP client disables itself on account-risk signals (403, WAF blocks, verification prompts) to avoid account suspension.
- **Chrome TLS fingerprinting** — all HTTP via `impit`; Extend blocks non-browser TLS profiles.
- **CLI** — full lifecycle commands: `login`, `accounts`, `issuers`, `cards`, `card`, `usage`, `enroll`, `activate`, `create`, `bulk`, `update`, `cancel`, `close`, `reveal`, `reconcile`, `status`, `clear-disabled`.
- **Python API** — public re-exports in `extendvcc.__init__` for programmatic use.
- **Typed** — `py.typed` marker (PEP 561); type hints throughout.

[Unreleased]: https://github.com/4LAU/extendvcc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/4LAU/extendvcc/releases/tag/v0.1.0
