# Gold-Standard Open Source Preparation

**Date:** 2026-06-14
**Status:** Design
**Scope:** Documentation, packaging, and lightweight code polish to bring extendvcc to professional-grade open-source quality before flipping the repo to public.

## Problem

extendvcc is functionally complete (78 tests, CI on 3 Python versions, binary release pipeline) but not packaged for credible public distribution. Missing: author metadata, changelog, security policy, LLM contributor guide, stable exit codes, and PyPI publishing. The tool handles financial API keys and credit card data — sloppy packaging signals "hobby project."

## Benchmark

Patterns drawn from:
- **gogcli** (steipete, 7.7k stars): AGENTS.md, stable exit codes, --dry-run, stdout/stderr discipline, SECURITY.md
- **codex-profile-switcher** (own project, A-): badges, changelog, issue templates, GitHub topics
- **apisniff** (own project, B+): SECURITY.md, multi-format release pipeline

## Design

### 1. Scrub CLAUDE.md for public consumption

Remove:
- Line 3: `"Extracted from argus/lib/paywithextend/"` — internal origin reference
- Line 72: `"Refer to user as **L**..."` — personal communication directives
- All references to "L" throughout

Keep everything else — build commands, module map, critical rules, testing policy reference. CLAUDE.md is tracked in git and useful for AI contributors using Claude Code.

### 2. Create AGENTS.md

The gogcli pattern: a tool-agnostic guide for any AI agent (or human) contributing to the project. Covers:
- Project overview (1 paragraph)
- Build, lint, test commands
- Architecture overview (module responsibilities)
- Coding style rules (impit for HTTP, lazy path resolution, no bare httpx)
- Commit conventions
- What needs approval vs. what doesn't
- Security rules (no PAN/CVC logging, no real API calls in tests)

CLAUDE.md becomes the Claude-specific supplement; AGENTS.md is the universal contributor guide.

### 3. Fill pyproject.toml metadata

Add under `[project]` (before `[project.optional-dependencies]`):
```toml
authors = [{name = "4LAU"}]
classifiers = [
    "Development Status :: 4 - Beta",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Office/Business :: Financial",
    "Typing :: Typed",  # Requires adding src/extendvcc/py.typed marker (PEP 561)
]
keywords = ["extend", "virtual-card", "fintech", "cli"]
```

Then add a separate `[project.urls]` table (after `[project.optional-dependencies]` or after `[project.scripts]`):
```toml
[project.urls]
Homepage = "https://github.com/4LAU/extendvcc"
Repository = "https://github.com/4LAU/extendvcc"
Issues = "https://github.com/4LAU/extendvcc/issues"
Changelog = "https://github.com/4LAU/extendvcc/blob/main/CHANGELOG.md"
```

**TOML ordering matters:** `classifiers` and `keywords` are `[project]` keys. Placing them after a `[project.urls]` header would make them URL entries, breaking `python -m build`. Keep scalar/array keys under `[project]` before any sub-table headers.

### 4. Create CHANGELOG.md

Keep-a-Changelog format. Single entry for 0.1.0 covering initial feature set.

### 5. Create SECURITY.md

Essential for a tool that handles:
- Extend API credentials (email/password)
- IMAP credentials (for OTP retrieval)
- Card numbers and CVCs (via reveal)
- Session tokens

Cover: scope, reporting process, what's protected (ledger PAN rejection, 0600 file permissions, kill switch), responsible disclosure.

### 6. Fix .gitignore

Add: `.venv/`, `.env`, `.env.*`

### 6a. Add py.typed marker

Create an empty `src/extendvcc/py.typed` file (PEP 561). Without this, the `Typing :: Typed` PyPI classifier is misleading and type checkers won't treat the installed package as typed. Ensure hatchling includes it in the wheel/sdist (verify with `hatch build` + inspect).

### 7. README polish

Add badges (CI status, license, Python versions). Fix the `reveal` example: change `extendvcc reveal <card-id> --json creds.json` to `extendvcc reveal <card-id> --json-path creds.json` (the CLI parser uses `--json-path PATH` for file output, not `--json` which is a global boolean flag). Add examples distinguishing masked stdout and `--json-path` secure file write.

**Code-level reveal security:** The global `--json` flag must NOT emit raw PAN/CVC to stdout for `reveal`. Make `reveal` ignore global `--json` and instead return masked JSON (same masking as the non-JSON path). Full credentials only via `--json-path` (file output with 0600 permissions). Document this as a deliberate security boundary: stdout is routinely captured by shell logs, CI logs, agent transcripts, and command wrappers — documentation warnings do not prevent leakage. If a future scripting use case requires stdout credentials, gate it behind an explicit opt-in flag like `--unsafe-reveal-stdout` that cannot be triggered by the global `--json` habit.

### 7a. Add `activate` CLI subcommand

The `enroll` command output (cli.py:125) tells users to run `extendvcc activate <id>`, and `cards.py` exports `activate_credit_card()`, but the CLI parser has no `activate` subcommand and `_COMMANDS` has no entry for it. This is a broken lifecycle for any user who enrolls a credit card via the CLI.

Fix: add `activate` subparser (`id` positional, `--json` inherited) mapped to `activate_credit_card()`. Output: card ID, status (PENDING or ACTIVE), and a hint if still pending. Include in Group C and add test coverage in Group D.

### 8. GitHub setup

- Set 8-10 topics: `python`, `cli`, `virtual-card`, `fintech`, `extend`, `card-management`, `automation`, `api-client`
- Add issue templates: bug report + feature request (YAML format in `.github/ISSUE_TEMPLATE/`)

### 9. PyPI publish workflow

Add a `publish` job to the existing `release.yml` (not a separate file — trusted publisher config matches on workflow filename). The job:
- Needs the existing `build` job (so binaries build first)
- Runs in a `release` GitHub environment (must match PyPI trusted publisher config)
- Has `permissions: { id-token: write, contents: read }` at the job level (job-level `permissions` override unspecified scopes to `none`, so `contents: read` is needed for checkout; the workflow-level `contents: write` stays for the release job)
- Installs the `build` frontend explicitly (`pip install build` — it is not in project dependencies), then builds the sdist/wheel with `python -m build` and publishes with `pypa/gh-action-pypi-publish`
- Adds a `test` job (or reusable test workflow call) that runs before any publishing. **This test job must gate both the `release` (binary) and `publish` (PyPI) jobs** — the current `release` job depends only on `build`, so a tag with failing tests would still publish binaries even if PyPI is gated. Add `needs: [build, test]` to both `release` and `publish`.

Single tag push runs tests, then builds binaries AND publishes to PyPI. No artifacts ship if tests fail.

### 9a. Fix login credential pass-through

`_cmd_login()` in `cli.py` writes the interactive password into `os.environ["EXTENDVCC_PASSWORD"]` (line 55-56) solely because `auth.setup()` does not accept credentials — even though `auth.authenticate()` already does. This makes the plaintext password available for the process lifetime and inheritable by any child process. For a tool whose OSS credibility rests on security hygiene, this must be fixed before shipping SECURITY.md.

Fix: add `email` and `password` parameters to `auth.setup()`, forwarding them to `authenticate(email=email, password=password, ...)`. Update `_cmd_login()` to pass credentials directly and remove the `os.environ` writes. This belongs in Group C (code polish).

### 10. Stable exit codes

Define in a new `_exit_codes.py` module:

| Code | Name | Meaning |
|------|------|---------|
| 0 | OK | Success |
| 1 | ERROR | General/unexpected error |
| 2 | USAGE | Bad arguments, missing required flags |
| 3 | AUTH_REQUIRED | No session file, expired token, login needed |
| 4 | DISABLED | Kill switch tripped (403, WAF, verification prompt) |
| 5 | API_ERROR | Extend API returned a non-2xx response |

Update `cli.py` exception handlers and command returns to use these codes. Document in README under a new "Exit Codes" section.

**Exception mapping detail:** `PayWithExtendAuthError` (and subclasses `SessionNotFound`, `OTPRequired`, `UnexpectedChallenge`) inherits from `RuntimeError`, NOT from `PayWithExtendError`. The CLI error handler must catch auth exceptions explicitly and map them to exit code 3. The full catch chain in `main()`:
1. `PayWithExtendDisabled` / `AccountRiskDetected` → exit 4
2. `PayWithExtendAuthError` / `SessionNotFound` / `OTPRequired` → exit 3
3. `PayWithExtendAPIError` → exit 5
4. `PayWithExtendError` → exit 1
5. `ValueError` → **do not catch broadly as USAGE.** Library-internal `ValueError` raises (e.g., `org_id` missing in `usage()`, absolute URL refusal in `client.py`) are not CLI user-input errors. Either: (a) introduce a typed `CLIInputError(ValueError)` for CLI-layer validation and map only that to exit 2, or (b) convert CLI-owned validation `print()+return 1` to `sys.exit(EXIT_USAGE)` directly in each handler, leaving library `ValueError` to fall through to exit 1 (ERROR)
6. `argparse` bad-input: override `parser.error()` to call `sys.exit(2)` instead of the default `sys.exit(2)` (accidental match, but make it explicit via the exit code constant)
7. No-subcommand path: `main()` currently returns `1` when no command is provided (line 607). This is a usage error, not a runtime error — return `EXIT_USAGE` (2). Similarly, all CLI-owned validation failures (mutually exclusive `create` flags, empty bulk CSV, missing update fields, missing `clear-disabled --manual`) should use `EXIT_USAGE` consistently, not `return 1`.
8. **Auth HTTP exceptions escape the catch chain.** `auth._raise_for_status()` calls `resp.raise_for_status()`, which raises impit/httpx-native exceptions (e.g., `httpx.HTTPStatusError`), NOT project exceptions. This affects Cognito calls (`_initiate_auth`, `_respond_to_auth_challenge`, `_call_cognito`) and `/users/me` (`fetch_current_user`). These are the most common failure paths (bad credentials, Cognito rejection, expired refresh). Fix: wrap `_raise_for_status()` to convert non-2xx HTTP responses into `PayWithExtendAuthError` (for Cognito paths) or `PayWithExtendAPIError` (for Extend API paths like `/authconfig`, `/users/me`). Without this, "stable exit codes" are not stable on auth failures. Add CLI tests for authconfig 400/500, Cognito 400, and `/users/me` 500 to Group D.

### 11. --dry-run on destructive commands

Add `--dry-run` flag to: `create`, `bulk`, `cancel`, `close`, `update`.

**Prerequisite: extract operation builders (not just payload builders) in `cards.py`.** The request body for `create_card()` is currently assembled inline (UUID correlation suffix, `account_context()` recipient resolution, recurrence payload) immediately before dispatch. `update_card()` builds its PUT body after a read-modify-write GET. If dry-run logic lives in `cli.py`, it will duplicate this shaping and inevitably diverge. Extract `build_create_card_operation(...)`, `build_update_card_operation(raw, overrides)`, etc. that return an operation descriptor: `{path, method, body, correlation_key, preview_accuracy}`. Both dry-run and real mutations call the same builders. The correlation key (UUID suffix in `displayName`) must be part of the operation object — a body-only builder would generate a different UUID on dry-run vs. real run, making the preview misleading. Accept injected `recipient_resolver` and `token_factory` so dry-run can substitute non-network paths.

Behavior varies by command:
- **create / bulk:** call the shared builder to produce the full request body without dispatch. Print the operation plan (card name, amount, target card ID) to stderr, print the would-be request body to stdout as JSON, exit 0. No API call made.
- **cancel / close:** these are bodyless PUTs (`PUT /virtualcards/{id}/cancel` and `/close`) — there is no request body to build. Dry-run should emit an operation descriptor to stdout as JSON: `{"method": "PUT", "path": "/virtualcards/{id}/cancel", "card_id": "...", "reversible": true|false, "body": null}`. Print a human summary to stderr. No payload builder needed.
- **update:** the builder performs the read-only GET to show the accurate merged payload (GET is non-destructive). Print the current state and the would-be PUT body to stdout as JSON. If the GET is not acceptable, fall back to showing only the override fields as a "semantic patch" and document that the full body requires the GET. Either way, no mutation is made.

`create_card()` derives `recipient` via `account_context()` (which loads the session). **Dry-run builders must never make network calls.** `account_context()` may call `/users/me` and refresh tokens — this violates the "no API call" contract. Dry-run create must resolve recipient from: (a) an explicit `--recipient` flag, (b) the email from `auth.load_session()` (local file read, no network), or (c) `"<session-email>"` placeholder if no session exists. The payload builder's `account_context()` dependency must be injected, not hardcoded, so dry-run can substitute a non-network path. Label the output as an approximate semantic preview, not "full request body," when no active session or explicit recipient is provided.

### 12. Stdout/stderr discipline

Current state: errors already go to stderr. Data (--json and tables) goes to stdout. But human-oriented messages like "Logged in as X" and "No enrolled credit cards" also go to stdout.

Fix: when `--json` is passed, ONLY structured JSON goes to stdout. All human messages (progress, confirmations, hints) go to stderr. When `--json` is NOT passed, current behavior is fine (humans read stdout).

Implementation: introduce a `_info(msg)` helper that prints to stderr, replace bare `print()` calls for non-data output in --json code paths. **Also fix `_confirm()`**: Python's `input(prompt)` writes the prompt to stdout; change to `print(prompt, end="", file=sys.stderr); input()` so confirmation prompts don't pollute JSON output. Similarly, route all pre-result prints (operation summaries in `create`, warnings in `close`) through `_info()` unconditionally — they are human-oriented and belong on stderr regardless of `--json`.

**Also fix login and OTP prompts:** `_cmd_login()` uses `input("Email: ")` which writes the prompt to stdout. `make_otp_callback()` returns `input` or `input("IMAP retrieval timed out...")` — both write prompts to stdout. These contaminate JSON output on the most common first-run path (`login`). Use the same `_info()` + bare `input()` pattern for all interactive prompts: email, password (already via `getpass`), OTP fallback, and confirmation. Add CLI tests for `extendvcc --json login` verifying no prompt text appears on stdout.

**Also fix parser help output:** `parser.print_help()` in the no-subcommand path (and the unknown-handler fallback) writes to stdout by default. Under `--json`, this violates the JSON-only-on-stdout contract. Fix: when `--json` is set, either redirect help to stderr (`parser.print_help(sys.stderr)`) or call `parser.error("no command specified")` which writes to stderr and exits. Add a CLI test for `main(["--json"])` asserting stdout is empty and exit code is `EXIT_USAGE`.

## Non-goals

- Safety profiles or compile-time constraints (gogcli-scale, not warranted)
- MCP server or schema introspection command
- `--wrap-untrusted` flag
- `--no-input` flag (can add later if agent usage grows)
- ~~New tests~~ **Revised: add CLI tests.** The existing 78 tests cover `cards`, `auth`, `client`, `ledger`, `imap_otp`, and `models` but have zero coverage for `cli.py`. Since this plan changes exit codes, stdout/stderr contract, and adds `--dry-run`, add `tests/test_cli.py` covering: exit code mapping for each exception type, `--json` stdout isolation, `--dry-run` output for each command, argparse bad-input behavior, and confirmation prompt cancellation. Keep tests offline (monkeypatch card/auth functions, use `capsys`/subprocess).
- Code of Conduct (premature for project size)
- DCO/CLA (MIT license, unnecessary overhead)

## Task grouping

**Group A — Documentation and config (no runtime code changes):**
Tasks 1-6, 6a, 7 (partial), 8 (CLAUDE.md scrub, AGENTS.md, pyproject.toml, CHANGELOG, SECURITY.md, .gitignore, py.typed, README badges, GitHub setup). README content for CLI commands added in Group C (activate, exit codes, dry-run) should be written in Group C to avoid documenting behavior before it exists.

**Group B — CI/CD:**
Task 9 (PyPI publish job + test gating for all release artifacts in release.yml)

**Group C — Code polish:**
Tasks 7a, 9a, 10-12 (activate subcommand, login credential pass-through fix, exit codes with full exception mapping, --dry-run with correct cancel/close semantics, stdout/stderr, reveal security boundary). Also owns README sections that document new CLI behavior (exit codes table, dry-run usage, activate lifecycle).

**Group D — CLI tests:**
New `tests/test_cli.py` covering exit codes, --json isolation, --dry-run output, activate lifecycle, argparse errors. Depends on Group C.

Groups A and B are independent and can run in parallel. Group C depends on nothing but should be reviewed as a unit. Group D depends on Group C.

## Post-implementation

1. Run `/staffcheck` on all changed code
2. Run `/simplify` on cli.py and _exit_codes.py
3. **Pre-public security gate (BLOCKING — do before flipping visibility):**
   - Run `gitleaks detect --source . -v --redact` locally with `fetch-depth: 0` (full history scan). CI already runs this on main pushes, but a local scan ensures nothing was missed on branches or force-pushed history.
   - Inspect all historically ignored paths (`*.json`, `*.jsonl`, session files) for accidentally committed secrets.
   - Confirm no API credentials, IMAP passwords, session tokens, or card data were ever committed. If found, rotate immediately and consider `git filter-repo` to remove from history before going public.
   - Review `git log --diff-filter=D --name-only` for deleted files that contained credentials.
4. **Manual tasks for L:**
   - Configure PyPI trusted publisher: since `extendvcc` does not yet exist on PyPI, use the **pending publisher** flow at pypi.org/manage/account/publishing/ (account-level, not project-level). Add a pending publisher (owner: 4LAU, repo: extendvcc, workflow: release.yml, environment: release). The pending publisher does not reserve the package name — verify `extendvcc` is unclaimed on PyPI immediately before tagging v0.1.0. After first successful publish, the trusted publisher moves to project settings automatically.
   - Flip repo visibility: `gh repo edit 4LAU/extendvcc --visibility public`
   - Verify first public CI run passes
   - Tag v0.1.0 to trigger first PyPI publish + binary release
