from __future__ import annotations

import json

import httpx
import pytest

from extendvcc import client as client_module
from extendvcc.client import (
    VAULT_BASE_URL,
    AccountRiskDetected,
    PayWithExtendAPIError,
    PayWithExtendClient,
    PayWithExtendDisabled,
    clear_disabled,
    vault_client,
)


def _client(
    handler,
    *,
    token_getter=lambda: "token",
    token_refresher=lambda: None,
    disable_writer=lambda reason: None,
    disabled_checker=lambda: None,
    sleeper=lambda delay: None,
    rate_limit_low_watermark=10,
) -> PayWithExtendClient:
    return PayWithExtendClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_getter=token_getter,
        token_refresher=token_refresher,
        disabled_checker=disabled_checker,
        disable_writer=disable_writer,
        sleeper=sleeper,
        rate_limit_low_watermark=rate_limit_low_watermark,
    )


def test_request_sets_extend_and_authorization_headers() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    result = _client(handler, token_getter=lambda: "access-token").get("/creditcards")

    assert result == {"ok": True}
    assert seen_headers["authorization"] == "Bearer access-token"
    assert seen_headers["accept"] == "application/vnd.paywithextend.v2021-03-12+json"
    assert seen_headers["x-extend-app-id"] == "app.paywithextend.com"
    assert seen_headers["x-extend-brand"] == "br_2F0trP1UmE59x1ZkNIAqsg"
    assert seen_headers["x-extend-platform"] == "web"
    assert "Mozilla/5.0" in seen_headers["user-agent"]
    assert seen_headers["x-extend-platform-version"] == seen_headers["user-agent"]


def test_absolute_url_must_stay_on_extend_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should fail before transport")

    client = _client(handler)

    with pytest.raises(ValueError, match="outside the Extend API host"):
        client.get("https://example.invalid/steal")


def test_json_error_raises_api_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    with pytest.raises(PayWithExtendAPIError) as exc_info:
        _client(handler).get("/creditcards")

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload == {"message": "bad request"}


def test_403_trips_kill_switch_and_future_calls_are_refused(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    disabled_path = tmp_path / "paywithextend_disabled.json"
    monkeypatch.setattr(client_module, "_disabled_state_path", lambda: disabled_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(AccountRiskDetected, match="403 response"):
        PayWithExtendClient(
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            token_getter=lambda: "token",
        ).get("/virtualcards")

    disabled_payload = json.loads(disabled_path.read_text())
    assert disabled_payload["disabled"] is True
    assert "403 response" in disabled_payload["reason"]
    assert disabled_payload["timestamp"].endswith("Z")
    assert calls == 1

    def token_getter() -> str:
        raise AssertionError("disabled client should fail before token lookup")

    with pytest.raises(PayWithExtendDisabled, match="403 response"):
        PayWithExtendClient(
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            token_getter=token_getter,
        ).get("/virtualcards")

    assert calls == 1


def test_html_challenge_trips_kill_switch() -> None:
    disabled_reasons: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<!doctype html><html><title>Just a moment...</title></html>",
            headers={"content-type": "text/html"},
        )

    with pytest.raises(AccountRiskDetected, match="HTML/WAF"):
        _client(handler, disable_writer=disabled_reasons.append).get("/virtualcards")

    assert disabled_reasons == ["HTML/WAF challenge response from Extend for /virtualcards"]


def test_verification_prompt_trips_kill_switch() -> None:
    disabled_reasons: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ChallengeName": "EMAIL_OTP"})

    with pytest.raises(AccountRiskDetected, match="verification prompt"):
        _client(handler, disable_writer=disabled_reasons.append).get("/users/me")

    assert disabled_reasons == ["Unexpected verification prompt from Extend for /users/me"]


def test_clear_disabled_requires_manual_flag(tmp_path) -> None:
    disabled_path = tmp_path / "paywithextend_disabled.json"
    disabled_path.write_text('{"disabled":true}', encoding="utf-8")

    with pytest.raises(ValueError, match="manual=True"):
        clear_disabled(disabled_path=disabled_path)

    assert disabled_path.exists()
    assert clear_disabled(manual=True, disabled_path=disabled_path) is True
    assert not disabled_path.exists()
    assert clear_disabled(manual=True, disabled_path=disabled_path) is False


def test_vault_client_accepts_vault_host_paths() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    c = vault_client(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_getter=lambda: "t",
        disabled_checker=lambda: None,
        disable_writer=lambda r: None,
        sleeper=lambda d: None,
    )
    assert c.get("https://v.paywithextend.com/virtualcards/vc_1") == {"ok": True}
    assert seen["url"] == "https://v.paywithextend.com/virtualcards/vc_1"
    assert VAULT_BASE_URL == "https://v.paywithextend.com"


def test_vault_client_rejects_api_host_absolute_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should fail before transport")

    c = vault_client(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        token_getter=lambda: "t",
        disabled_checker=lambda: None,
        disable_writer=lambda r: None,
        sleeper=lambda d: None,
    )
    with pytest.raises(ValueError, match="outside the Extend API host"):
        c.get("https://api.paywithextend.com/virtualcards")
