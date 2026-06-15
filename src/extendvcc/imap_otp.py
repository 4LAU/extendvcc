"""IMAP-based OTP retrieval for PayWithExtend Cognito EMAIL_OTP challenges."""

from __future__ import annotations

import calendar
import email
import imaplib
import os
import re
import sys
import time
from collections.abc import Callable
from email.header import decode_header

DEFAULT_IMAP_SERVER = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

EXTEND_SENDER = "paywithextend.com"

MAX_WAIT_SECONDS = 60
POLL_INTERVAL_SECONDS = 2


def read_imap_credentials() -> tuple[str, str, str] | None:
    """Read IMAP credentials from env vars. Returns (email, app_password, server) or None."""
    imap_email = os.environ.get("EXTENDVCC_IMAP_USER", "")
    imap_password = os.environ.get("EXTENDVCC_IMAP_PASSWORD", "")
    if not imap_email or not imap_password:
        return None
    imap_server = os.environ.get("EXTENDVCC_IMAP_HOST", DEFAULT_IMAP_SERVER)
    return imap_email, imap_password, imap_server


def extract_code(text: str) -> str | None:
    """Extract a 6-digit verification code from email body text."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&\w+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean)

    patterns = [
        r"verification\s+code[:\s]*(\d{6})",
        r"Enter\s+this\s+verification\s+code[^:]*:\s*(\d{6})",
        r"sign[- ]?in\s+code[:\s]*(\d{6})",
        r"security\s+code[:\s]*(\d{6})",
        r"one[- ]?time\s+(?:code|password)[:\s]*(\d{6})",
        r"your\s+code\s+is[:\s]*(\d{6})",
        r"code[:\s]+(\d{6})",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            return match.group(1)

    # Fallback: bold standalone 6-digit number (the Extend email format)
    bold_match = re.search(r"<b>\s*(\d{6})\s*</b>", text, re.IGNORECASE)
    if bold_match:
        return bold_match.group(1)

    # Last resort: any standalone 6-digit number
    fallback = re.search(r"\b(\d{6})\b", clean)
    return fallback.group(1) if fallback else None


def _get_body(msg: email.message.Message) -> str:
    """Extract text body from an email message."""
    if msg.is_multipart():
        html_body = ""
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(errors="replace")
            if ct == "text/plain":
                return decoded
            if ct == "text/html" and not html_body:
                html_body = decoded
        return html_body
    payload = msg.get_payload(decode=True)
    return payload.decode(errors="replace") if payload else ""


def fetch_otp(
    since_timestamp: float,
    *,
    max_wait: int = MAX_WAIT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
    _credentials: tuple[str, str, str] | None = None,
) -> str | None:
    """Poll IMAP inbox for an Extend OTP email and return the 6-digit code."""
    creds = _credentials or read_imap_credentials()
    if creds is None:
        return None

    imap_email, imap_password, imap_server = creds
    deadline = time.monotonic() + max_wait

    conn = imaplib.IMAP4_SSL(imap_server, DEFAULT_IMAP_PORT)
    try:
        conn.login(imap_email, imap_password)
        conn.select("INBOX")

        since_date = time.strftime("%d-%b-%Y", time.localtime(since_timestamp - 86400))

        while time.monotonic() < deadline:
            conn.noop()
            conn.select("INBOX")
            _, data = conn.search(None, f'(SINCE {since_date} FROM "{EXTEND_SENDER}")')
            msg_ids = data[0].split() if data[0] else []

            for msg_id in reversed(msg_ids):
                _, date_data = conn.fetch(msg_id, "(INTERNALDATE)")
                internal_date = imaplib.Internaldate2tuple(date_data[0])
                if internal_date is not None:
                    msg_epoch = calendar.timegm(internal_date)
                    if msg_epoch < since_timestamp - 5:
                        continue

                _, msg_data = conn.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject_raw = decode_header(msg.get("Subject", ""))[0][0]
                if isinstance(subject_raw, bytes):
                    subject_raw = subject_raw.decode(errors="replace")
                subject = str(subject_raw).lower()

                if "code" not in subject and "verification" not in subject:
                    continue

                body = _get_body(msg)
                code = extract_code(body)
                if code:
                    conn.store(msg_id, "+FLAGS", "\\Seen")
                    return code

            time.sleep(poll_interval)

        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _prompt_stdin(prompt: str) -> str:
    """Read a line from stdin after writing the prompt to stderr.

    Keeps stdout clean: under ``--json`` only structured data may reach stdout,
    so interactive prompt text must go to stderr.
    """
    print(prompt, end="", file=sys.stderr, flush=True)
    return input()


def make_otp_callback() -> Callable[[str], str]:
    """Return an OTP callback: IMAP auto-retrieval if configured, stdin prompt if not."""
    creds = read_imap_credentials()
    if creds is None:
        return _prompt_stdin

    def _imap_callback(_prompt: str) -> str:
        since = time.time() - 300
        code = fetch_otp(since, _credentials=creds)
        if code is None:
            return _prompt_stdin("IMAP retrieval timed out. Enter OTP manually: ")
        return code

    return _imap_callback
