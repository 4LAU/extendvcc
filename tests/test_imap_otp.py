from __future__ import annotations

import ssl

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
    """Invariant: a failed TLS handshake or unreachable IMAP server degrades to
    None so login can fall back to the manual OTP prompt, never raising."""

    def _creds(self):
        return ("user@example.com", "app-password", "imap.example.com")

    def test_cert_verification_failure_returns_none(self, monkeypatch):
        def raise_cert_error(*args, **kwargs):
            raise ssl.SSLCertVerificationError("hostname mismatch")

        monkeypatch.setattr(imap_otp.imaplib, "IMAP4_SSL", raise_cert_error)
        assert imap_otp.fetch_otp(0.0, _credentials=self._creds()) is None

    def test_unreachable_server_returns_none(self, monkeypatch):
        def raise_oserror(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(imap_otp.imaplib, "IMAP4_SSL", raise_oserror)
        assert imap_otp.fetch_otp(0.0, _credentials=self._creds()) is None
