from __future__ import annotations

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
