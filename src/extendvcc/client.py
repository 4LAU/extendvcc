"""HTTP client for Extend's private web API.

This module intentionally treats anti-bot, WAF, and verification responses as
account-risk events. A detected risk writes the disabled-state file and all
subsequent network paths fail closed before requesting or refreshing tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import impit

BASE_URL = "https://api.paywithextend.com"
VAULT_BASE_URL = "https://v.paywithextend.com"

RATE_LIMIT_REMAINING_HEADER = "x-rate-limit-remaining"
RATE_LIMIT_BACKOFF_SECONDS = 1.0
RATE_LIMIT_LOW_WATERMARK = 10
RATE_LIMIT_MAX_BACKOFF_SECONDS = 30.0

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

EXTEND_BRAND = os.environ.get("EXTENDVCC_BRAND_ID", "br_2F0trP1UmE59x1ZkNIAqsg")

EXTEND_HEADERS = {
    "Accept": "application/vnd.paywithextend.v2021-03-12+json",
    "x-extend-app-id": "app.paywithextend.com",
    "x-extend-brand": EXTEND_BRAND,
    "x-extend-platform": "web",
    "x-extend-platform-version": USER_AGENT,
    "User-Agent": USER_AGENT,
}

_JSON_CONTENT_TYPE = "application/json"
# PAN-shaped digit runs (13-19) and obvious secret key names — scrubbed from any
# error payload so a card number / CVC echoed in a 4xx body can never reach a log.
_PAN_RUN_RE = re.compile(r"\d(?:[ -]?\d){12,18}")
_SENSITIVE_PAYLOAD_KEYS = ("cardnumber", "cvc", "cvv", "cvn", "securitycode", "pan", "vcn")


def _scrub_payload(value: Any) -> Any:
    """Recursively mask PAN-shaped runs and sensitive-keyed values in an error payload."""
    if isinstance(value, dict):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            compact = str(key).replace("_", "").replace("-", "").lower()
            if any(marker in compact for marker in _SENSITIVE_PAYLOAD_KEYS):
                scrubbed[key] = "[redacted]"
            else:
                scrubbed[key] = _scrub_payload(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_payload(item) for item in value]
    if isinstance(value, str):
        return _PAN_RUN_RE.sub("[redacted]", value)
    return value


_HTML_MARKERS = (
    "<!doctype html",
    "<html",
    "cloudflare",
    "cf-chl",
    "attention required",
    "just a moment",
    "checking your browser",
)
_VERIFICATION_MARKERS = (
    "email_otp",
    "otp required",
    "verification required",
    "verify your",
    "verify identity",
    "confirm your identity",
    "multi-factor",
    "mfa",
    "captcha",
    "recaptcha",
    "challenge required",
)


def _disabled_state_path() -> Path:
    from extendvcc._paths import state_dir

    return state_dir() / "paywithextend_disabled.json"


class PayWithExtendError(RuntimeError):
    """Base PayWithExtend client error."""


class PayWithExtendDisabled(PayWithExtendError):
    """Raised when automation is disabled by the account-risk kill switch."""


class AccountRiskDetected(PayWithExtendDisabled):
    """Raised when a response trips the account-risk kill switch."""


class PayWithExtendAPIError(PayWithExtendError):
    """Raised when Extend returns a JSON error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        path: str,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.payload = payload


class PayWithExtendNonJSONError(PayWithExtendAPIError):
    """Raised when Extend returns a response that cannot be decoded as JSON."""


def disabled_status(disabled_path: Path | None = None) -> dict[str, Any] | None:
    path = disabled_path or _disabled_state_path()
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "disabled": True,
            "reason": "disabled-state file exists but could not be decoded",
            "path": str(path),
        }
    if isinstance(loaded, dict):
        return loaded
    return {
        "disabled": True,
        "reason": "disabled-state file did not contain an object",
        "path": str(path),
    }


def assert_not_disabled(disabled_path: Path | None = None) -> None:
    status = disabled_status(disabled_path)
    if status is None:
        return
    reason = status.get("reason", "PayWithExtend automation is disabled")
    raise PayWithExtendDisabled(str(reason))


def disable_paywithextend(
    reason: str,
    *,
    disabled_path: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    path = disabled_path or _disabled_state_path()
    timestamp = (now or _utc_now)().isoformat().replace("+00:00", "Z")
    payload = {
        "disabled": True,
        "reason": reason,
        "timestamp": timestamp,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)
    return payload


def clear_disabled(*, manual: bool = False, disabled_path: Path | None = None) -> bool:
    if not manual:
        raise ValueError("clear_disabled requires manual=True")
    path = disabled_path or _disabled_state_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_valid_token() -> str:
    assert_not_disabled()
    from .auth import ensure_valid_token

    return ensure_valid_token()


def _refresh_tokens() -> Any:
    assert_not_disabled()
    from .auth import refresh_tokens

    return refresh_tokens()


def _headers_get(headers: Any, name: str, default: str | None = None) -> str | None:
    if hasattr(headers, "get"):
        return headers.get(name, default)
    return default


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


def _json_or_none(response: Any) -> Any:
    try:
        return response.json()
    except (TypeError, ValueError):
        return None


def _contains_marker(value: Any, markers: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        return any(_contains_marker(item, markers) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, markers) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in markers)
    return False


def _is_html_response(response: Any) -> bool:
    content_type = (_headers_get(response.headers, "content-type", "") or "").lower()
    if "text/html" in content_type:
        return True
    lowered = _response_text(response).lstrip().lower()
    return any(marker in lowered for marker in _HTML_MARKERS)


def _risk_reason(response: Any, path: str) -> str | None:
    status_code = int(getattr(response, "status_code", 0))
    if status_code == 403:
        return f"403 response from Extend for {path}"
    if _is_html_response(response):
        return f"HTML/WAF challenge response from Extend for {path}"
    payload = _json_or_none(response)
    if payload is not None and _contains_marker(payload, _VERIFICATION_MARKERS):
        return f"Unexpected verification prompt from Extend for {path}"
    return None


def inspect_account_risk(
    response: Any,
    path: str,
    *,
    disable_writer: Callable[[str], Any] = disable_paywithextend,
) -> None:
    reason = _risk_reason(response, path)
    if reason is None:
        return
    disable_writer(reason)
    raise AccountRiskDetected(reason)


class PayWithExtendClient:
    def __init__(
        self,
        *,
        http_client: Any | None = None,
        base_url: str = BASE_URL,
        token_getter: Callable[[], str] = _ensure_valid_token,
        token_refresher: Callable[[], Any] = _refresh_tokens,
        disabled_checker: Callable[[], None] = assert_not_disabled,
        disable_writer: Callable[[str], Any] = disable_paywithextend,
        sleeper: Callable[[float], None] = time.sleep,
        rate_limit_low_watermark: int = RATE_LIMIT_LOW_WATERMARK,
        rate_limit_backoff_seconds: float = RATE_LIMIT_BACKOFF_SECONDS,
        rate_limit_max_backoff_seconds: float = RATE_LIMIT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self._client = http_client or impit.Client(browser="chrome")
        self._base_url = base_url.rstrip("/")
        self._token_getter = token_getter
        self._token_refresher = token_refresher
        self._disabled_checker = disabled_checker
        self._disable_writer = disable_writer
        self._sleeper = sleeper
        self._rate_limit_low_watermark = rate_limit_low_watermark
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self._rate_limit_max_backoff_seconds = rate_limit_max_backoff_seconds
        self._rate_limit_hits = 0

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        return self.request("GET", path, params=params, headers=headers, timeout=timeout)

    def post(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        return self.request("POST", path, json_body=json_body, headers=headers, timeout=timeout)

    def put(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        return self.request("PUT", path, json_body=json_body, headers=headers, timeout=timeout)

    def patch(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        return self.request("PATCH", path, json_body=json_body, headers=headers, timeout=timeout)

    def delete(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        return self.request("DELETE", path, headers=headers, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> Any:
        response = self._request_once(
            method,
            path,
            params=params,
            json_body=json_body,
            content=content,
            headers=headers,
            timeout=timeout,
        )
        # A routine 401 (expired token) must get one refresh-and-retry BEFORE the
        # account-risk inspection runs — otherwise a 401 body that happens to
        # contain a verification phrase would permanently trip the kill switch
        # instead of simply refreshing. Only the final response is inspected.
        if int(getattr(response, "status_code", 0)) == 401:
            self._disabled_checker()
            self._token_refresher()
            response = self._request_once(
                method,
                path,
                params=params,
                json_body=json_body,
                content=content,
                headers=headers,
                timeout=timeout,
            )
        self._inspect_response(response, path)
        return self._decode_response(response, path)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: Any | None,
        content: bytes | str | None,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> Any:
        self._disabled_checker()
        request_headers = self._auth_headers(headers)
        request_content = content
        if json_body is not None:
            request_headers.setdefault("Content-Type", _JSON_CONTENT_TYPE)
            request_content = json.dumps(json_body, separators=(",", ":")).encode()
        url = self._url(path, params)
        return self._client.request(
            method,
            url,
            headers=request_headers,
            content=request_content,
            timeout=timeout,
        )

    def _auth_headers(self, extra_headers: dict[str, str] | None) -> dict[str, str]:
        self._disabled_checker()
        token = self._token_getter()
        headers = {**EXTEND_HEADERS, "Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        # Re-assert the freshly-minted token so caller headers cannot override it.
        headers["Authorization"] = f"Bearer {token}"
        return headers

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            parsed_url = urlparse(path)
            parsed_base = urlparse(self._base_url)
            if parsed_url.scheme != parsed_base.scheme or parsed_url.netloc != parsed_base.netloc:
                raise ValueError("PayWithExtendClient refuses absolute URLs outside the Extend API host")
            url = path
        else:
            url = f"{self._base_url}/{path.lstrip('/')}"
        if not params:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode(params, doseq=True)}"

    def _inspect_response(self, response: Any, path: str) -> None:
        inspect_account_risk(response, path, disable_writer=self._disable_writer)
        self._backoff_for_rate_limit(response)

    def _backoff_for_rate_limit(self, response: Any) -> None:
        remaining_header = _headers_get(response.headers, RATE_LIMIT_REMAINING_HEADER)
        remaining: int | None = None
        if remaining_header is not None:
            try:
                remaining = int(remaining_header)
            except ValueError:
                remaining = None

        status_code = int(getattr(response, "status_code", 0))
        should_backoff = status_code == 429 or (remaining is not None and remaining <= self._rate_limit_low_watermark)
        if not should_backoff:
            self._rate_limit_hits = 0
            return

        self._rate_limit_hits += 1
        delay = min(
            self._rate_limit_backoff_seconds * (2 ** (self._rate_limit_hits - 1)),
            self._rate_limit_max_backoff_seconds,
        )
        self._sleeper(delay)

    def _decode_response(self, response: Any, path: str) -> Any:
        status_code = int(getattr(response, "status_code", 0))
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayWithExtendNonJSONError(
                f"Extend API returned a non-JSON response: {status_code} {path}",
                status_code=status_code,
                path=path,
            ) from exc
        if status_code >= 400:
            raise PayWithExtendAPIError(
                f"Extend API request failed: {status_code} {path}",
                status_code=status_code,
                path=path,
                payload=_scrub_payload(payload),
            )
        return payload


def vault_client(**kwargs: Any) -> PayWithExtendClient:
    """Return a PayWithExtendClient configured for the vault host (v.paywithextend.com)."""
    return PayWithExtendClient(base_url=VAULT_BASE_URL, **kwargs)
