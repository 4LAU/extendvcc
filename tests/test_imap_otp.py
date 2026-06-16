from __future__ import annotations

import imaplib
import ssl

import pytest

from extendvcc import imap_otp

EXTEND_HTML_BODY = """
<html>
<body>
<p>It looks like you're trying to log in to your account. Enter this verification
code into your app or browser, and you'll be all set:</p>
<p><b>291827</b></p>
<p>This code will expire in 10 minutes.</p>
</body>
</html>
"""

EXTEND_PLAIN_BODY = (
    "It looks like you're trying to log in to your account. "
    "Enter this verification code into your app or browser, "
    "and you'll be all set:\n\n291827\n\nThis code will expire in 10 minutes."
)


class TestExtractCode:
    def test_bold_html_code(self):
        assert imap_otp.extract_code(EXTEND_HTML_BODY) == "291827"

    def test_plain_text_code(self):
        assert imap_otp.extract_code(EXTEND_PLAIN_BODY) == "291827"

    def test_enter_this_verification_code_pattern(self):
        text = "Enter this verification code into your app or browser, and you'll be all set: 482019"
        assert imap_otp.extract_code(text) == "482019"

    def test_no_code_returns_none(self):
        assert imap_otp.extract_code("No code here") is None

    def test_ignores_non_6_digit_numbers(self):
        assert imap_otp.extract_code("Your balance is 12345 dollars") is None
        assert imap_otp.extract_code("Order #1234567 confirmed") is None


class TestFetchOtpConnectionFailure:
    """Invariant: any IMAP failure (bad cert, unreachable host, bad password,
    mid-session drop) degrades to None so login can fall back to the manual OTP
    prompt, never raising."""

    CREDS = ("user@example.com", "app-password", "imap.example.com")

    @pytest.mark.parametrize(
        "exc",
        [ssl.SSLCertVerificationError("hostname mismatch"), OSError("connection refused")],
    )
    def test_constructor_failure_returns_none(self, monkeypatch, exc):
        def raise_exc(*args, **kwargs):
            raise exc

        monkeypatch.setattr(imap_otp.imaplib, "IMAP4_SSL", raise_exc)
        assert imap_otp.fetch_otp(0.0, _credentials=self.CREDS) is None

    def test_login_failure_returns_none_and_logs_out(self, monkeypatch):
        logged_out = []

        class FakeConn:
            def login(self, *args):
                raise imaplib.IMAP4.error("authentication failed")

            def logout(self):
                logged_out.append(True)

        monkeypatch.setattr(imap_otp.imaplib, "IMAP4_SSL", lambda *a, **k: FakeConn())
        assert imap_otp.fetch_otp(0.0, _credentials=self.CREDS) is None
        assert logged_out == [True]
