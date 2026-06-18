"""extendvcc CLI — full lifecycle management of Extend virtual cards."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ._exit_codes import (
    EXIT_API_ERROR,
    EXIT_AUTH_REQUIRED,
    EXIT_DISABLED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
)

DISCLAIMER = (
    "Unofficial and unaffiliated: extendvcc is an independent client for "
    "Extend's private API (api.paywithextend.com). It is not affiliated with, endorsed "
    "by, or supported by Extend, Inc. Automating their private API may get your account "
    "suspended. Use at your own risk."
)


class CLIInputError(ValueError):
    """Raised for CLI-layer input/usage errors (maps to EXIT_USAGE).

    Distinct from library ``ValueError`` (e.g. ``usage()`` missing org_id) so that
    only CLI-owned validation maps to exit 2; library errors fall through to 1.
    """


def _info(msg: str = "") -> None:
    """Print a human-oriented message to stderr.

    Under ``--json``, stdout must carry only structured JSON, so every progress
    line, confirmation, summary, and warning routes through here to stderr.
    """
    print(msg, file=sys.stderr)


def _json_out(data: Any) -> str:
    """Serialize data for --json output."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _confirm(prompt: str, *, yes: bool = False) -> bool:
    """Ask for confirmation unless --yes was passed."""
    if yes:
        return True
    # Prompt to stderr so stdout stays clean (shell pipes, --json capture).
    print(prompt, end="", file=sys.stderr, flush=True)
    answer = input()
    return answer.strip().lower() in ("y", "yes")


def _mask_card_number(number: str) -> str:
    """Show first 4 and last 4 digits, mask the rest."""
    if len(number) <= 8:
        return number
    return number[:4] + "*" * (len(number) - 8) + number[-4:]


def _card_to_dict(card: Any) -> dict[str, Any]:
    """Convert a dataclass card to a JSON-safe dict."""
    return json.loads(json.dumps(asdict(card), default=str))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _prompt(prompt: str) -> str:
    """Read a line from stdin with the prompt written to stderr (keeps stdout clean)."""
    print(prompt, end="", file=sys.stderr, flush=True)
    return input()


def _cmd_login(args: argparse.Namespace) -> int:
    from . import auth
    from .imap_otp import make_otp_callback

    # Surface the unofficial/at-your-own-risk notice at the start of real usage.
    _info(DISCLAIMER)
    _info()

    email = args.email or os.environ.get("EXTENDVCC_EMAIL") or _prompt("Email: ")
    # getpass writes its prompt to stderr, so stdout stays clean.
    password = os.environ.get("EXTENDVCC_PASSWORD") or getpass.getpass("Password: ")

    # Pass credentials directly — never write the plaintext password into the
    # process environment, where it would leak into every child process.
    otp_callback = make_otp_callback()
    result = auth.setup(email=email, password=password, otp_callback=otp_callback)

    if getattr(args, "json", False):
        print(_json_out(result))
    else:
        _info(f"Logged in as {result.get('email', '?')}")
        if result.get("org_id"):
            _info(f"Organization: {result['org_id']}")
        _info(f"Session saved to {result.get('session_path', '?')}")
    return EXIT_OK


def _cmd_accounts(args: argparse.Namespace) -> int:
    from .cards import list_credit_cards

    cards = list_credit_cards()
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(c) for c in cards]))
    else:
        if not cards:
            _info("No enrolled credit cards.")
            return EXIT_OK
        print(f"{'ID':<40} {'Last 4':<8} {'Status':<16} {'Name'}")
        print("-" * 90)
        for c in cards:
            print(f"{c.id:<40} {c.last4:<8} {c.status.value:<16} {c.display_name}")
    return EXIT_OK


def _cmd_enroll(args: argparse.Namespace) -> int:
    from .cards import enroll_credit_card

    card_number = getpass.getpass("Card number (PAN): ")
    cvc = getpass.getpass("CVC: ")

    _info(f"\nEnrolling card ending in ...{card_number[-4:]}")
    _info(f"  Name: {args.display_name}")
    _info(f"  Cardholder: {args.cardholder_name}")
    _info(f"  Issuer ID: {args.issuer_id}")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        _info("Cancelled.")
        return EXIT_ERROR

    address = {
        "address1": args.address1,
        "address2": getattr(args, "address2", "") or "",
        "city": args.city,
        "province": args.province,
        "postal": args.postal,
    }

    result = enroll_credit_card(
        display_name=args.display_name,
        card_number=card_number,
        expires=args.expires,
        cvc=cvc,
        cardholder_name=args.cardholder_name,
        issuer_id=args.issuer_id,
        address=address,
        company_name=getattr(args, "company_name", None),
        country=getattr(args, "country", "US") or "US",
    )
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(result)))
    else:
        print(f"Enrolled: {result.id} (last4={result.last4}, status={result.status.value})")
        _info("Check your email for issuer verification, then run: extendvcc activate <id>")
    return EXIT_OK


def _cmd_activate(args: argparse.Namespace) -> int:
    from .cards import activate_credit_card
    from .models import CardStatus

    card = activate_credit_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Card:   {card.id}")
        print(f"Status: {card.status.value}")
        if card.status == CardStatus.PENDING:
            _info("Still PENDING — verify via the issuer email, then re-run: extendvcc activate <id>")
    return EXIT_OK


def _cmd_issuers(args: argparse.Namespace) -> int:
    from .cards import list_issuers

    issuers = list_issuers()
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(i) for i in issuers]))
    else:
        if not issuers:
            _info("No issuers found.")
            return EXIT_OK
        print(f"{'ID':<40} {'Code':<12} {'Name'}")
        print("-" * 70)
        for i in issuers:
            print(f"{i.id:<40} {i.code:<12} {i.name}")
    return EXIT_OK


def _cmd_cards(args: argparse.Namespace) -> int:
    from .cards import list_cards
    from .models import CardStatus

    status = None
    if args.status:
        status = CardStatus(args.status.upper())

    cards = list_cards(status=status)
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(c) for c in cards]))
    else:
        if not cards:
            _info("No virtual cards found.")
            return EXIT_OK
        print(f"{'ID':<40} {'Last 4':<8} {'Status':<14} {'Balance':<12} {'Name'}")
        print("-" * 120)
        for c in cards:
            balance = f"${c.balance_cents / 100:.2f}"
            print(f"{c.id:<40} {c.last4:<8} {c.status.value:<14} {balance:<12} {c.name}")
    return EXIT_OK


def _cmd_card(args: argparse.Namespace) -> int:
    from .cards import get_card, held_cents

    card = get_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        held = held_cents(card)
        print(f"ID:            {card.id}")
        print(f"Name:          {card.name}")
        print(f"Last 4:        {card.last4}")
        print(f"Status:        {card.status.value}")
        # Spend breakdown: limit and settled spend are only present on the GET
        # response; show them (plus derived holds) when available, else fall
        # back to the available-balance line alone.
        if card.limit_cents is not None:
            print(f"Limit:         ${card.limit_cents / 100:.2f}")
            print(f"Spent:         ${(card.spent_cents or 0) / 100:.2f}")
        if held is not None:
            print(f"Held:          ${held / 100:.2f}")
        print(f"Available:     ${card.balance_cents / 100:.2f}")
        print(f"Credit Card:   {card.credit_card_id}")
        print(f"Valid From:    {card.valid_from or 'N/A'}")
        print(f"Valid To:      {card.valid_to or 'N/A'}")
        print(f"Created:       {card.created_at or 'N/A'}")
        if card.notes:
            print(f"Notes:         {card.notes}")
    return EXIT_OK


def _cmd_usage(args: argparse.Namespace) -> int:
    from .cards import usage

    result = usage()
    if getattr(args, "json", False):
        print(_json_out(result))
    else:
        print(f"Active cards:  {result['used']} / {result['limit']}")
        print(f"Remaining:     {result['remaining']}")
    return EXIT_OK


def _create_recurrence(args: argparse.Namespace) -> Any:
    from .models import Recurrence

    if not args.period:
        return None
    return Recurrence(
        period=args.period.upper(),
        interval=getattr(args, "interval", 1) or 1,
        terminator=(getattr(args, "terminator", None) or "NONE").upper(),
        by_month_day=getattr(args, "by_month_day", None),
        by_week_day=getattr(args, "by_week_day", None),
        until=getattr(args, "until", None),
        count=getattr(args, "count", None),
    )


def _create_summary(args: argparse.Namespace, recurrence: Any) -> None:
    kind = "recurring" if recurrence else "one-time"
    balance_dollars = args.balance_cents / 100
    _info(f"Creating {kind} virtual card:")
    _info(f"  Name:           {args.name}")
    _info(f"  Balance:        ${balance_dollars:.2f}")
    _info(f"  Credit Card ID: {args.credit_card_id}")
    if recurrence:
        _info(f"  Period:         {recurrence.period} (every {recurrence.interval})")
        _info(f"  Terminator:     {recurrence.terminator}")
    else:
        _info(f"  Valid To:       {args.valid_to}")


def _local_recipient(recipient_flag: str | None) -> tuple[str, bool]:
    """Resolve a recipient WITHOUT any network call for dry-run preview.

    Returns ``(email, exact)``. Prefers an explicit ``--recipient`` flag, else the
    email from the locally-saved session file, else a placeholder. ``exact`` is
    True only when we have a concrete value (flag or session); a placeholder means
    the preview is approximate.
    """
    if recipient_flag:
        return recipient_flag, True
    from . import auth

    session = auth.load_session()
    if session and session.get("email"):
        return str(session["email"]), True
    return "<session-email>", False


def _cmd_create(args: argparse.Namespace) -> int:
    from .cards import build_create_card_operation, create_card

    if args.valid_to and args.period:
        raise CLIInputError("--valid-to and --period are mutually exclusive.")
    if not args.valid_to and not args.period:
        raise CLIInputError("provide either --valid-to (one-time) or --period (recurring).")

    recurrence = _create_recurrence(args)

    if getattr(args, "dry_run", False):
        return _create_dry_run(args, recurrence, build_create_card_operation)

    _create_summary(args, recurrence)
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        _info("Cancelled.")
        return EXIT_ERROR

    card = create_card(
        credit_card_id=args.credit_card_id,
        name=args.name,
        balance_cents=args.balance_cents,
        valid_to=args.valid_to if not recurrence else None,
        recurrence=recurrence,
        recipient=getattr(args, "recipient", None),
    )
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Created: {card.id} (last4={card.last4}, status={card.status.value})")
    return EXIT_OK


def _create_dry_run(args: argparse.Namespace, recurrence: Any, builder: Any) -> int:
    """Preview a create without any network call: resolve recipient locally,
    print the plan to stderr and the would-be request body (JSON) to stdout."""
    import uuid

    recipient, exact = _local_recipient(getattr(args, "recipient", None))
    operation = builder(
        args.credit_card_id,
        args.name,
        args.balance_cents,
        args.valid_to if not recurrence else None,
        recurrence=recurrence,
        recipient_resolver=lambda: recipient,
        token_factory=lambda: uuid.uuid4().hex[:8],
    )
    if not exact:
        operation["preview_accuracy"] = "approximate"

    _create_summary(args, recurrence)
    _info(f"  Recipient:      {recipient}")
    _info(f"[dry-run] No API call made (preview: {operation['preview_accuracy']}).")
    print(_json_out(operation["body"]))
    return EXIT_OK


def _read_bulk_rows(csv_path: Path) -> list[dict[str, Any]]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, Any]] = []
        for row in reader:
            parsed: dict[str, Any] = {
                "name": row["name"],
                "balance_cents": int(row["balance_cents"]),
                "valid_to": row["valid_to"],
            }
            if "recipient" in row and row["recipient"].strip():
                parsed["recipient"] = row["recipient"].strip()
            rows.append(parsed)
    return rows


def _cmd_bulk(args: argparse.Namespace) -> int:
    from .cards import build_create_card_operation, create_cards_bulk

    csv_path = Path(args.file)
    if not csv_path.exists():
        raise CLIInputError(f"file not found: {csv_path}")

    rows = _read_bulk_rows(csv_path)
    if not rows:
        raise CLIInputError("CSV file is empty.")

    total_cents = sum(r["balance_cents"] for r in rows)
    _info(f"Bulk create: {len(rows)} cards, total ${total_cents / 100:.2f}")
    _info(f"  Credit Card ID: {args.credit_card_id}")

    if getattr(args, "dry_run", False):
        return _bulk_dry_run(args, rows, build_create_card_operation)

    _info(f"  Delay: {args.delay}s (jitter {args.jitter}s, min {args.min_delay}s)")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        _info("Cancelled.")
        return EXIT_ERROR

    cards = create_cards_bulk(
        credit_card_id=args.credit_card_id,
        rows=rows,
        delay_seconds=args.delay,
        jitter_seconds=args.jitter,
        min_delay_seconds=args.min_delay,
    )
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(c) for c in cards]))
    else:
        for c in cards:
            print(f"  Created: {c.id} (last4={c.last4}, {c.name})")
        print(f"\n{len(cards)} cards created.")
    return EXIT_OK


def _bulk_dry_run(args: argparse.Namespace, rows: list[dict[str, Any]], builder: Any) -> int:
    """Preview a bulk create with no network calls and no pacing sleeps.

    Each row's recipient is resolved locally (flag/session/placeholder); the plan
    goes to stderr and the list of would-be request bodies (JSON) to stdout."""
    import uuid

    bodies: list[dict[str, Any]] = []
    approximate = False
    for row in rows:
        recipient, exact = _local_recipient(row.get("recipient"))
        approximate = approximate or not exact
        operation = builder(
            args.credit_card_id,
            row["name"],
            row["balance_cents"],
            row["valid_to"],
            recurrence=None,
            recipient_resolver=lambda r=recipient: r,
            token_factory=lambda: uuid.uuid4().hex[:8],
        )
        bodies.append(operation["body"])

    accuracy = "approximate" if approximate else "exact"
    _info(f"[dry-run] {len(bodies)} cards, no API calls (preview: {accuracy}).")
    print(_json_out(bodies))
    return EXIT_OK


def _cmd_reveal(args: argparse.Namespace) -> int:
    from .cards import reveal_card

    _info("WARNING: Card credentials are highly sensitive.")
    _info("Do not share, log, or transmit them insecurely.")

    creds = reveal_card(args.id)
    json_path = getattr(args, "json_path", None)

    # Security boundary: full PAN/CVC reaches stdout via NO path. stdout is
    # captured by shell history, CI logs, and agent transcripts, so raw
    # credentials only ever go to a 0600 file (--json-path). The global --json
    # flag emits a MASKED structure; human mode masks too.
    if json_path:
        path = Path(json_path)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
        try:
            os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(creds, indent=2, sort_keys=True))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(path))
            os.chmod(str(path), 0o600)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        _info(f"Credentials written to {path} (mode 0600)")
    elif getattr(args, "json", False):
        masked = {
            "last4": creds.get("last4"),
            "expires": creds.get("expires"),
            "number": _mask_card_number(creds.get("number", "")),
            "cvc": "****",
        }
        print(_json_out(masked))
    else:
        number = creds.get("number", "")
        print(f"Card Number: {_mask_card_number(number)}")
        print(f"Last 4:      {creds.get('last4', 'N/A')}")
        print(f"Expires:     {creds.get('expires', 'N/A')}")
        print("CVC:         ****")
    return EXIT_OK


def _cmd_update(args: argparse.Namespace) -> int:
    from .cards import update_card

    kwargs: dict[str, Any] = {}
    if args.balance_cents is not None:
        kwargs["balance_cents"] = args.balance_cents
    if args.name is not None:
        kwargs["name"] = args.name
    if args.valid_to is not None:
        kwargs["valid_to"] = args.valid_to

    if not kwargs:
        raise CLIInputError("no fields to update. Use --balance-cents, --name, or --valid-to.")

    if getattr(args, "dry_run", False):
        return _update_dry_run(args, kwargs)

    card = update_card(args.id, **kwargs)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Updated: {card.id} (last4={card.last4}, status={card.status.value})")
    return EXIT_OK


def _update_dry_run(args: argparse.Namespace, kwargs: dict[str, Any]) -> int:
    """Preview an update. The read-only GET is allowed (non-destructive) so the
    merged PUT body is accurate; no mutation is performed."""
    from .cards import _default_client, _update_overrides, build_update_card_operation

    client = _default_client()
    overrides = _update_overrides(
        balance_cents=kwargs.get("balance_cents"),
        name=kwargs.get("name"),
        valid_to=kwargs.get("valid_to"),
        recurs=None,
    )
    operation = build_update_card_operation(
        args.id,
        overrides,
        fetcher=lambda: client.get(f"/virtualcards/{args.id}"),
    )
    _info(f"[dry-run] update {args.id} — overrides: {overrides}. No mutation made.")
    print(_json_out(operation["body"]))
    return EXIT_OK


def _bodyless_descriptor(card_id: str, *, action: str, reversible: bool) -> dict[str, Any]:
    """Build a dry-run descriptor for a bodyless PUT (cancel/close)."""
    return {
        "method": "PUT",
        "path": f"/virtualcards/{card_id}/{action}",
        "card_id": card_id,
        "reversible": reversible,
        "body": None,
    }


def _cmd_cancel(args: argparse.Namespace) -> int:
    from .cards import cancel_card

    if getattr(args, "dry_run", False):
        _info(f"[dry-run] would cancel {args.id} (reversible). No API call made.")
        print(_json_out(_bodyless_descriptor(args.id, action="cancel", reversible=True)))
        return EXIT_OK

    card = cancel_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Cancelled: {card.id} (last4={card.last4}, status={card.status.value})")
    return EXIT_OK


def _cmd_close(args: argparse.Namespace) -> int:
    from .cards import close_card

    if getattr(args, "dry_run", False):
        _info(f"[dry-run] would close {args.id} (PERMANENT, not reversible). No API call made.")
        print(_json_out(_bodyless_descriptor(args.id, action="close", reversible=False)))
        return EXIT_OK

    _info(f"WARNING: Closing card {args.id} is permanent and cannot be undone.")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        _info("Cancelled.")
        return EXIT_ERROR

    card = close_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Closed: {card.id} (last4={card.last4}, status={card.status.value})")
    return EXIT_OK


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from .cards import reconcile

    result = reconcile()
    if getattr(args, "json", False):
        print(_json_out(result))
    else:
        adopted = result.get("adopted", [])
        failed = result.get("failed", [])
        if adopted:
            print(f"Adopted {len(adopted)} card(s):")
            for card_id in adopted:
                print(f"  {card_id}")
        if failed:
            print(f"Failed {len(failed)} pending row(s):")
            for key in failed:
                print(f"  {key}")
        if not adopted and not failed:
            _info("No pending rows to reconcile.")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    from .client import disabled_status

    status = disabled_status()
    if getattr(args, "json", False):
        if status is None:
            print(_json_out({"disabled": False}))
        else:
            print(_json_out(status))
    else:
        if status is None:
            print("Status: ENABLED")
        else:
            print("Status: DISABLED")
            print(f"Reason: {status.get('reason', 'unknown')}")
            if "timestamp" in status:
                print(f"Since:  {status['timestamp']}")
            if "path" in status:
                print(f"File:   {status['path']}")
            _info("\nTo re-enable: extendvcc clear-disabled --manual")
    return EXIT_OK


def _cmd_clear_disabled(args: argparse.Namespace) -> int:
    from .client import clear_disabled

    manual = getattr(args, "manual", False)
    if not manual:
        raise CLIInputError(
            "--manual flag is required to confirm re-enabling. Usage: extendvcc clear-disabled --manual"
        )

    removed = clear_disabled(manual=True)
    if getattr(args, "json", False):
        print(_json_out({"cleared": removed}))
    else:
        if removed:
            _info("Kill switch cleared. Client re-enabled.")
        else:
            _info("No disabled-state file found. Client was already enabled.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that exits with the stable EXIT_USAGE code on bad input.

    argparse already exits 2, but we route it through the constant so the usage
    contract is explicit and stays correct if the value ever changes.
    """

    def error(self, message: str) -> Any:  # noqa: D102
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="extendvcc",
        description="Unofficial CLI for the Extend virtual card API.",
        epilog=DISCLAIMER,
    )
    parser.add_argument("--state-dir", default=None, help="Override state directory")
    parser.add_argument("--ledger", default=None, help="Override ledger file path")
    parser.add_argument("--json", action="store_true", default=False, help="Machine-readable JSON output")

    sub = parser.add_subparsers(dest="command")

    # login
    p = sub.add_parser("login", help="Authenticate with Extend (SRP + email OTP)")
    p.add_argument("--email", default=None, help="Email (or EXTENDVCC_EMAIL env var)")

    # accounts
    sub.add_parser("accounts", help="List enrolled parent credit cards")

    # enroll
    p = sub.add_parser("enroll", help="Enroll a new parent credit card")
    p.add_argument("--display-name", required=True, help="Display name for the card")
    p.add_argument("--cardholder-name", required=True, help="Name on the card")
    p.add_argument("--issuer-id", required=True, help="Issuer ID (see 'issuers' command)")
    p.add_argument("--expires", required=True, help="Card expiration date (YYYY-MM-DD)")
    p.add_argument("--address1", required=True, help="Billing address line 1")
    p.add_argument("--address2", default="", help="Billing address line 2")
    p.add_argument("--city", required=True, help="Billing city")
    p.add_argument("--province", required=True, help="Billing state/province")
    p.add_argument("--postal", required=True, help="Billing postal/ZIP code")
    p.add_argument("--company-name", default=None, help="Company name (defaults to cardholder name)")
    p.add_argument("--country", default="US", help="Country code (default: US)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # activate
    p = sub.add_parser("activate", help="Activate a verified parent credit card (PENDING -> ACTIVE)")
    p.add_argument("id", help="Credit card ID")

    # issuers
    sub.add_parser("issuers", help="List card issuers")

    # cards
    p = sub.add_parser("cards", help="List virtual cards")
    p.add_argument("--status", default=None, help="Filter by status (ACTIVE, CANCELLED, PENDING, etc.)")

    # card
    p = sub.add_parser("card", help="Get a single virtual card")
    p.add_argument("id", help="Virtual card ID")

    # usage
    sub.add_parser("usage", help="Show active virtual card usage vs. limit")

    # create
    p = sub.add_parser("create", help="Create a virtual card (one-time or recurring)")
    p.add_argument("--credit-card-id", required=True, help="Parent credit card ID")
    p.add_argument("--name", required=True, help="Card display name")
    p.add_argument("--balance-cents", required=True, type=int, help="Spending limit in cents")
    p.add_argument("--recipient", default=None, help="Recipient email (defaults to account email)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    # One-time
    p.add_argument("--valid-to", default=None, help="Expiration date YYYY-MM-DD (one-time card)")
    # Recurring (mutually exclusive with --valid-to conceptually)
    p.add_argument("--period", default=None, help="Recurrence period: DAILY, WEEKLY, or MONTHLY")
    p.add_argument("--interval", type=int, default=1, help="Reset every N periods (default: 1)")
    p.add_argument("--by-month-day", type=int, default=None, help="Day of month (1-31, MONTHLY only)")
    p.add_argument("--by-week-day", type=int, default=None, help="Day of week (0-6, WEEKLY only)")
    p.add_argument("--terminator", default=None, help="NONE, DATE, or COUNT")
    p.add_argument("--until", default=None, help="End date YYYY-MM-DD (DATE terminator)")
    p.add_argument("--count", type=int, default=None, help="Number of resets (COUNT terminator)")
    p.add_argument("--dry-run", action="store_true", help="Preview the request body (no API call)")

    # bulk
    p = sub.add_parser("bulk", help="Bulk-create virtual cards from a CSV file")
    p.add_argument("file", help="CSV file (columns: name, balance_cents, valid_to, [recipient])")
    p.add_argument("--credit-card-id", required=True, help="Parent credit card ID")
    p.add_argument("--delay", type=float, default=2.0, help="Mean delay between cards in seconds (default: 2.0)")
    p.add_argument("--jitter", type=float, default=0.75, help="Delay jitter std-dev in seconds (default: 0.75)")
    p.add_argument("--min-delay", type=float, default=0.5, help="Minimum delay between cards in seconds (default: 0.5)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="Preview the request bodies (no API calls)")

    # reveal
    p = sub.add_parser("reveal", help="Reveal live card credentials (PAN, CVC, expiry)")
    p.add_argument("id", help="Virtual card ID")
    p.add_argument(
        "--json-path",
        default=None,
        metavar="PATH",
        help="Save full credentials to file (0600 perms) instead of printing masked",
    )

    # update
    p = sub.add_parser("update", help="Update a virtual card (read-modify-write)")
    p.add_argument("id", help="Virtual card ID")
    p.add_argument("--balance-cents", type=int, default=None, help="New spending limit in cents")
    p.add_argument("--name", default=None, help="New display name")
    p.add_argument("--valid-to", default=None, help="New expiration date YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="Preview the merged PUT body (read-only GET, no mutation)")

    # cancel
    p = sub.add_parser("cancel", help="Cancel a virtual card (reversible)")
    p.add_argument("id", help="Virtual card ID")
    p.add_argument("--dry-run", action="store_true", help="Preview the operation descriptor (no API call)")

    # close
    p = sub.add_parser("close", help="Close a virtual card permanently")
    p.add_argument("id", help="Virtual card ID")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="Preview the operation descriptor (no API call)")

    # reconcile
    sub.add_parser("reconcile", help="Resolve pending ledger rows against remote cards")

    # status
    sub.add_parser("status", help="Show kill-switch state")

    # clear-disabled
    p = sub.add_parser("clear-disabled", help="Re-enable client after kill-switch trip")
    p.add_argument("--manual", action="store_true", help="Confirm manual override (required)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_COMMANDS: dict[str, Any] = {
    "login": _cmd_login,
    "accounts": _cmd_accounts,
    "enroll": _cmd_enroll,
    "activate": _cmd_activate,
    "issuers": _cmd_issuers,
    "cards": _cmd_cards,
    "card": _cmd_card,
    "usage": _cmd_usage,
    "create": _cmd_create,
    "bulk": _cmd_bulk,
    "reveal": _cmd_reveal,
    "update": _cmd_update,
    "cancel": _cmd_cancel,
    "close": _cmd_close,
    "reconcile": _cmd_reconcile,
    "status": _cmd_status,
    "clear-disabled": _cmd_clear_disabled,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No subcommand: help to stderr (keeps stdout JSON-only under --json), exit 2.
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    # Configure paths before dispatching
    from . import _paths

    path_kwargs: dict[str, Any] = {}
    if args.state_dir:
        path_kwargs["state_dir"] = args.state_dir
    if args.ledger:
        path_kwargs["ledger_path"] = args.ledger
    if path_kwargs:
        _paths.configure(**path_kwargs)

    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    from .auth import PayWithExtendAuthError
    from .client import PayWithExtendAPIError, PayWithExtendDisabled, PayWithExtendError

    # Catch order is most-specific-first. Note auth errors are RuntimeError, NOT
    # PayWithExtendError, so they are caught on their own branch.
    try:
        return handler(args)
    except PayWithExtendDisabled as exc:  # covers AccountRiskDetected
        print(f"Error: {exc}", file=sys.stderr)
        print("Hint: run 'extendvcc clear-disabled --manual' to re-enable.", file=sys.stderr)
        return EXIT_DISABLED
    except PayWithExtendAuthError as exc:  # covers SessionNotFound, OTPRequired, UnexpectedChallenge
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_AUTH_REQUIRED
    except PayWithExtendAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_API_ERROR
    except PayWithExtendError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except CLIInputError as exc:  # CLI-owned validation -> usage error
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:  # library-internal ValueError -> generic error, NOT usage
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
