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


def _json_out(data: Any) -> str:
    """Serialize data for --json output."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _confirm(prompt: str, *, yes: bool = False) -> bool:
    """Ask for confirmation unless --yes was passed."""
    if yes:
        return True
    answer = input(prompt)
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


def _cmd_login(args: argparse.Namespace) -> int:
    from . import auth
    from .imap_otp import make_otp_callback

    email = args.email or os.environ.get("EXTENDVCC_EMAIL") or input("Email: ")
    password = os.environ.get("EXTENDVCC_PASSWORD") or getpass.getpass("Password: ")

    # setup() -> authenticate() -> read_credentials() reads these env vars
    os.environ["EXTENDVCC_EMAIL"] = email
    os.environ["EXTENDVCC_PASSWORD"] = password

    otp_callback = make_otp_callback()
    result = auth.setup(otp_callback=otp_callback)

    if getattr(args, "json", False):
        print(_json_out(result))
    else:
        print(f"Logged in as {result.get('email', '?')}")
        if result.get("org_id"):
            print(f"Organization: {result['org_id']}")
        print(f"Session saved to {result.get('session_path', '?')}")
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    from .cards import list_credit_cards

    cards = list_credit_cards()
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(c) for c in cards]))
    else:
        if not cards:
            print("No enrolled credit cards.")
            return 0
        print(f"{'ID':<40} {'Last 4':<8} {'Status':<16} {'Name'}")
        print("-" * 90)
        for c in cards:
            print(f"{c.id:<40} {c.last4:<8} {c.status.value:<16} {c.display_name}")
    return 0


def _cmd_enroll(args: argparse.Namespace) -> int:
    from .cards import enroll_credit_card

    card_number = getpass.getpass("Card number (PAN): ")
    cvc = getpass.getpass("CVC: ")

    print(f"\nEnrolling card ending in ...{card_number[-4:]}")
    print(f"  Name: {args.display_name}")
    print(f"  Cardholder: {args.cardholder_name}")
    print(f"  Issuer ID: {args.issuer_id}")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        print("Cancelled.")
        return 1

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
        print("Check your email for issuer verification, then run: extendvcc activate <id>")
    return 0


def _cmd_issuers(args: argparse.Namespace) -> int:
    from .cards import list_issuers

    issuers = list_issuers()
    if getattr(args, "json", False):
        print(_json_out([_card_to_dict(i) for i in issuers]))
    else:
        if not issuers:
            print("No issuers found.")
            return 0
        print(f"{'ID':<40} {'Code':<12} {'Name'}")
        print("-" * 70)
        for i in issuers:
            print(f"{i.id:<40} {i.code:<12} {i.name}")
    return 0


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
            print("No virtual cards found.")
            return 0
        print(f"{'ID':<40} {'Last 4':<8} {'Status':<14} {'Balance':<12} {'Name'}")
        print("-" * 120)
        for c in cards:
            balance = f"${c.balance_cents / 100:.2f}"
            print(f"{c.id:<40} {c.last4:<8} {c.status.value:<14} {balance:<12} {c.name}")
    return 0


def _cmd_card(args: argparse.Namespace) -> int:
    from .cards import get_card

    card = get_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"ID:            {card.id}")
        print(f"Name:          {card.name}")
        print(f"Last 4:        {card.last4}")
        print(f"Status:        {card.status.value}")
        print(f"Balance:       ${card.balance_cents / 100:.2f}")
        print(f"Credit Card:   {card.credit_card_id}")
        print(f"Valid From:    {card.valid_from or 'N/A'}")
        print(f"Valid To:      {card.valid_to or 'N/A'}")
        print(f"Created:       {card.created_at or 'N/A'}")
        if card.notes:
            print(f"Notes:         {card.notes}")
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    from .cards import usage

    result = usage()
    if getattr(args, "json", False):
        print(_json_out(result))
    else:
        print(f"Active cards:  {result['used']} / {result['limit']}")
        print(f"Remaining:     {result['remaining']}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from .cards import create_card
    from .models import Recurrence

    if args.valid_to and args.period:
        print("Error: --valid-to and --period are mutually exclusive.", file=sys.stderr)
        return 1
    if not args.valid_to and not args.period:
        print("Error: provide either --valid-to (one-time) or --period (recurring).", file=sys.stderr)
        return 1

    recurrence = None
    if args.period:
        recurrence = Recurrence(
            period=args.period.upper(),
            interval=getattr(args, "interval", 1) or 1,
            terminator=(getattr(args, "terminator", None) or "NONE").upper(),
            by_month_day=getattr(args, "by_month_day", None),
            by_week_day=getattr(args, "by_week_day", None),
            until=getattr(args, "until", None),
            count=getattr(args, "count", None),
        )

    kind = "recurring" if recurrence else "one-time"
    balance_dollars = args.balance_cents / 100
    print(f"Creating {kind} virtual card:")
    print(f"  Name:           {args.name}")
    print(f"  Balance:        ${balance_dollars:.2f}")
    print(f"  Credit Card ID: {args.credit_card_id}")
    if recurrence:
        print(f"  Period:         {recurrence.period} (every {recurrence.interval})")
        print(f"  Terminator:     {recurrence.terminator}")
    else:
        print(f"  Valid To:       {args.valid_to}")

    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        print("Cancelled.")
        return 1

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
    return 0


def _cmd_bulk(args: argparse.Namespace) -> int:
    from .cards import create_cards_bulk

    csv_path = Path(args.file)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        return 1

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

    if not rows:
        print("Error: CSV file is empty.", file=sys.stderr)
        return 1

    total_cents = sum(r["balance_cents"] for r in rows)
    print(f"Bulk create: {len(rows)} cards, total ${total_cents / 100:.2f}")
    print(f"  Credit Card ID: {args.credit_card_id}")
    print(f"  Delay: {args.delay}s (jitter {args.jitter}s, min {args.min_delay}s)")

    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        print("Cancelled.")
        return 1

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
    return 0


def _cmd_reveal(args: argparse.Namespace) -> int:
    from .cards import reveal_card

    print("WARNING: Card credentials are highly sensitive.", file=sys.stderr)
    print("Do not share, log, or transmit them insecurely.", file=sys.stderr)

    creds = reveal_card(args.id)
    json_path = getattr(args, "json_path", None)

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
        print(f"Credentials written to {path} (mode 0600)")
    elif getattr(args, "json", False):
        print(_json_out(creds))
    else:
        number = creds.get("number", "")
        print(f"Card Number: {_mask_card_number(number)}")
        print(f"Last 4:      {creds.get('last4', 'N/A')}")
        print(f"Expires:     {creds.get('expires', 'N/A')}")
        print("CVC:         ****")
    return 0


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
        print("Error: no fields to update. Use --balance-cents, --name, or --valid-to.", file=sys.stderr)
        return 1

    card = update_card(args.id, **kwargs)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Updated: {card.id} (last4={card.last4}, status={card.status.value})")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    from .cards import cancel_card

    card = cancel_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Cancelled: {card.id} (last4={card.last4}, status={card.status.value})")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    from .cards import close_card

    print(f"WARNING: Closing card {args.id} is permanent and cannot be undone.")
    if not _confirm("Proceed? [y/N] ", yes=getattr(args, "yes", False)):
        print("Cancelled.")
        return 1

    card = close_card(args.id)
    if getattr(args, "json", False):
        print(_json_out(_card_to_dict(card)))
    else:
        print(f"Closed: {card.id} (last4={card.last4}, status={card.status.value})")
    return 0


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
            print("No pending rows to reconcile.")
    return 0


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
            print("\nTo re-enable: extendvcc clear-disabled --manual")
    return 0


def _cmd_clear_disabled(args: argparse.Namespace) -> int:
    from .client import clear_disabled

    manual = getattr(args, "manual", False)
    if not manual:
        print("Error: --manual flag is required to confirm re-enabling.", file=sys.stderr)
        print("Usage: extendvcc clear-disabled --manual", file=sys.stderr)
        return 1

    removed = clear_disabled(manual=True)
    if getattr(args, "json", False):
        print(_json_out({"cleared": removed}))
    else:
        if removed:
            print("Kill switch cleared. Client re-enabled.")
        else:
            print("No disabled-state file found. Client was already enabled.")
    return 0


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extendvcc",
        description="Unofficial CLI for the Extend virtual card API.",
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

    # bulk
    p = sub.add_parser("bulk", help="Bulk-create virtual cards from a CSV file")
    p.add_argument("file", help="CSV file (columns: name, balance_cents, valid_to, [recipient])")
    p.add_argument("--credit-card-id", required=True, help="Parent credit card ID")
    p.add_argument("--delay", type=float, default=2.0, help="Mean delay between cards in seconds (default: 2.0)")
    p.add_argument("--jitter", type=float, default=0.75, help="Delay jitter std-dev in seconds (default: 0.75)")
    p.add_argument("--min-delay", type=float, default=0.5, help="Minimum delay between cards in seconds (default: 0.5)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

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

    # cancel
    p = sub.add_parser("cancel", help="Cancel a virtual card (reversible)")
    p.add_argument("id", help="Virtual card ID")

    # close
    p = sub.add_parser("close", help="Close a virtual card permanently")
    p.add_argument("id", help="Virtual card ID")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

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

    if not args.command:
        parser.print_help()
        return 1

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
        parser.print_help()
        return 1

    from .client import PayWithExtendDisabled, PayWithExtendError

    try:
        return handler(args)
    except PayWithExtendDisabled as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Hint: run 'extendvcc clear-disabled --manual' to re-enable.", file=sys.stderr)
        return 1
    except PayWithExtendError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
