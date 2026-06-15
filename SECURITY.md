# Security Policy

## Scope

The following are in scope for vulnerability reports:

- Authentication and session handling (Cognito SRP, token storage, device remembering)
- Credential exposure — API tokens, IMAP passwords, session files
- Card data exposure — PAN, CVC, expiry leaking outside of the `reveal` code path
- Permission issues — session or state files created with overly permissive modes
- Dependency vulnerabilities with a realistic exploit path against this package

Out of scope: Extend's own API security, issues requiring physical access to the machine, and theoretical issues with no practical impact.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via [GitHub Security Advisories](https://github.com/4LAU/extendvcc/security/advisories/new). This keeps details confidential until a fix is available.

Include:

- A description of the vulnerability and its impact
- Steps to reproduce or a proof-of-concept
- The version(s) affected

You will receive an acknowledgment within 72 hours. Fixes for confirmed vulnerabilities are typically released within 14 days.

## What This Package Protects

**Credentials** — Extend email/password and IMAP credentials are accepted via env vars or interactive prompt. They are never written to disk.

**Session tokens** — Cognito tokens and device credentials are persisted to the state directory with `0600` file permissions (owner read/write only).

**Card data** — The JSONL audit ledger never stores PAN or CVC. The `reveal` command writes full credentials to disk only when `--json-path` is explicitly passed, and the file is created with `0600` permissions.

**Kill switch** — The HTTP client monitors for account-risk signals (403 responses, WAF blocks, verification prompts) and disables itself to reduce the risk of account suspension from runaway automation.

## Responsible Disclosure

Please give us a reasonable window to fix and release a patch before disclosing publicly. We will credit reporters in the changelog unless anonymity is requested.
