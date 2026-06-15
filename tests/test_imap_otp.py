from __future__ import annotations

import email.mime.multipart
import email.mime.text
import time

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

    def test_verification_code_pattern(self):
        assert imap_otp.extract_code("Your verification code: 482910") == "482910"

    def test_sign_in_code_pattern(self):
        assert imap_otp.extract_code("Sign-in code: 123456") == "123456"

    def test_security_code_pattern(self):
        assert imap_otp.extract_code("Your security code is 654321") == "654321"

    def test_one_time_code_pattern(self):
        assert imap_otp.extract_code("Your one-time code: 111222") == "111222"

    def test_your_code_is_pattern(self):
        assert imap_otp.extract_code("Your code is: 999888") == "999888"

    def test_generic_code_pattern(self):
        assert imap_otp.extract_code("code: 777666") == "777666"

    def test_fallback_standalone_6_digit(self):
        assert imap_otp.extract_code("Please use 345678 to continue") == "345678"

    def test_no_code_returns_none(self):
        assert imap_otp.extract_code("No code here") is None

    def test_ignores_non_6_digit_numbers(self):
        assert imap_otp.extract_code("Your balance is 12345 dollars") is None
        assert imap_otp.extract_code("Order #1234567 confirmed") is None

    def test_html_entities_stripped(self):
        assert imap_otp.extract_code("code:&nbsp;192837") == "192837"

    def test_enter_this_verification_code_pattern(self):
        text = "Enter this verification code into your app or browser, and you'll be all set: 482019"
        assert imap_otp.extract_code(text) == "482019"


class TestGetBody:
    def test_plain_text_message(self):
        msg = email.mime.text.MIMEText("plain body", "plain")
        assert imap_otp._get_body(msg) == "plain body"

    def test_html_message(self):
        msg = email.mime.text.MIMEText("<b>html</b>", "html")
        assert imap_otp._get_body(msg) == "<b>html</b>"

    def test_multipart_prefers_plain(self):
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText("<b>html</b>", "html"))
        msg.attach(email.mime.text.MIMEText("plain", "plain"))
        assert imap_otp._get_body(msg) == "plain"

    def test_multipart_falls_back_to_html(self):
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText("<b>html</b>", "html"))
        assert imap_otp._get_body(msg) == "<b>html</b>"


class TestReadImapCredentials:
    def test_returns_none_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("EXTENDVCC_IMAP_USER", raising=False)
        monkeypatch.delenv("EXTENDVCC_IMAP_PASSWORD", raising=False)
        assert imap_otp.read_imap_credentials() is None

    def test_returns_credentials_with_default_server(self, monkeypatch):
        monkeypatch.setenv("EXTENDVCC_IMAP_USER", "user@gmail.com")
        monkeypatch.setenv("EXTENDVCC_IMAP_PASSWORD", "app-pass")
        monkeypatch.delenv("EXTENDVCC_IMAP_HOST", raising=False)

        result = imap_otp.read_imap_credentials()
        assert result == ("user@gmail.com", "app-pass", "imap.gmail.com")


class TestMakeOtpCallback:
    def test_returns_input_when_no_credentials(self, monkeypatch):
        monkeypatch.setattr(imap_otp, "read_imap_credentials", lambda: None)
        assert imap_otp.make_otp_callback() is input

    def test_returns_imap_callback_when_credentials_exist(self, monkeypatch):
        monkeypatch.setattr(
            imap_otp,
            "read_imap_credentials",
            lambda: ("user@gmail.com", "pass", "imap.gmail.com"),
        )
        callback = imap_otp.make_otp_callback()
        assert callback is not input
        assert callable(callback)


class TestFetchOtp:
    def test_returns_none_when_no_credentials(self):
        assert imap_otp.fetch_otp(time.time(), _credentials=None, max_wait=0) is None
