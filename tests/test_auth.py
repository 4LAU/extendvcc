from __future__ import annotations

import base64
import json
import stat
import time
from typing import Any

import pytest

from extendvcc import auth
from extendvcc._paths import configure as configure_paths
from extendvcc.client import PayWithExtendDisabled


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeExtendClient:
    def __init__(self, authconfig: dict[str, Any] | None = None, user: dict[str, Any] | None = None) -> None:
        self.authconfig = authconfig or {
            "userPoolId": "us-east-1_pool123",
            "clientId": "client123",
        }
        self.user = user or {"id": "user_123", "orgId": "org_123"}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict[str, str], content: bytes, timeout: int) -> FakeResponse:
        self.posts.append({"url": url, "headers": headers, "content": content, "timeout": timeout})
        return FakeResponse(self.authconfig)

    def get(self, url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(self.user, headers={"x-rate-limit-remaining": "499"})


class FakeCognitoClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict[str, str], content: bytes, timeout: int) -> FakeResponse:
        payload = json.loads(content.decode("utf-8"))
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"unexpected Cognito call: {payload}")
        return FakeResponse(self.responses.pop(0))


def make_jwt(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


def password_challenge(session: str = "session-1") -> dict[str, Any]:
    return {
        "ChallengeName": "PASSWORD_VERIFIER",
        "Session": session,
        "ChallengeParameters": {
            "SALT": "deadbeef",
            "SRP_B": "5",
            "SECRET_BLOCK": base64.b64encode(b"secret-block").decode("ascii"),
            "USER_ID_FOR_SRP": "user-sub",
        },
    }


def test_fetch_authconfig_sends_json_email_and_decodes_response() -> None:
    client = FakeExtendClient()

    result = auth.fetch_authconfig("l@example.com", client=client)

    assert result == {"user_pool_id": "us-east-1_pool123", "client_id": "client123"}
    sent = client.posts[0]
    assert sent["url"] == f"{auth.API_BASE}/authconfig"
    assert json.loads(sent["content"].decode("utf-8")) == {"email": "l@example.com"}


def test_save_and_load_session_uses_0600_permissions(tmp_path, monkeypatch) -> None:
    configure_paths(state_dir=tmp_path)
    monkeypatch.delattr("extendvcc._paths._state_dir_override", raising=False)

    # Re-configure after monkeypatch setup
    configure_paths(state_dir=tmp_path)

    auth.save_session({"access_token": "token", "expires_at": 123})

    path = tmp_path / auth.SESSION_FILENAME
    assert auth.load_session() == {"access_token": "token", "expires_at": 123}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Reset paths
    configure_paths()


def test_refresh_tokens_uses_refresh_token_auth_with_device_key(tmp_path, monkeypatch) -> None:
    configure_paths(state_dir=tmp_path)
    old_refresh = "refresh-old"
    access = make_jwt(time.time() + 3600)
    session = {
        "refresh_token": old_refresh,
        "client_id": "client123",
        "user_pool_id": "us-east-1_pool123",
        "email": "l@example.com",
        "device_key": "device123",
        "org_id": "org_123",
    }
    cognito = FakeCognitoClient(
        [
            {
                "AuthenticationResult": {
                    "AccessToken": access,
                    "IdToken": "id-new",
                    "ExpiresIn": 3600,
                }
            }
        ]
    )

    refreshed = auth.refresh_tokens(session, cognito_client=cognito, save=True)

    assert refreshed["access_token"] == access
    assert refreshed["refresh_token"] == old_refresh
    assert refreshed["org_id"] == "org_123"
    payload = cognito.calls[0]["payload"]
    assert payload["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert payload["AuthParameters"] == {
        "REFRESH_TOKEN": old_refresh,
        "DEVICE_KEY": "device123",
    }
    assert auth.load_session()["access_token"] == access

    configure_paths()


def test_authenticate_handles_password_otp_and_device_registration(tmp_path, monkeypatch) -> None:
    configure_paths(state_dir=tmp_path)
    access = make_jwt(time.time() + 3600)
    extend = FakeExtendClient()
    cognito = FakeCognitoClient(
        [
            password_challenge(),
            {
                "ChallengeName": "EMAIL_OTP",
                "Session": "session-2",
                "ChallengeParameters": {"USERNAME": "user-sub"},
            },
            {
                "AuthenticationResult": {
                    "AccessToken": access,
                    "IdToken": "id-token",
                    "RefreshToken": "refresh-token",
                    "ExpiresIn": 3600,
                    "NewDeviceMetadata": {
                        "DeviceKey": "device123",
                        "DeviceGroupKey": "device-group",
                    },
                }
            },
            {},
            {},
        ]
    )

    session = auth.authenticate(
        email="l@example.com",
        password="password",
        otp_callback=lambda _: "123456",
        extend_client=extend,
        cognito_client=cognito,
    )

    assert session["access_token"] == access
    assert session["device_key"] == "device123"
    assert session["device_group_key"] == "device-group"
    assert session["device_password"]
    assert auth.load_session()["refresh_token"] == "refresh-token"
    assert cognito.calls[0]["payload"]["AuthFlow"] == "USER_SRP_AUTH"
    assert cognito.calls[1]["payload"]["ChallengeName"] == "PASSWORD_VERIFIER"
    assert cognito.calls[2]["payload"]["ChallengeResponses"]["EMAIL_OTP_CODE"] == "123456"
    assert cognito.calls[2]["payload"]["ChallengeResponses"]["USERNAME"] == "user-sub"
    assert cognito.calls[3]["headers"]["X-Amz-Target"].endswith(".ConfirmDevice")
    assert cognito.calls[4]["headers"]["X-Amz-Target"].endswith(".UpdateDeviceStatus")

    configure_paths()


def test_device_verifier_pads_negative_salt_and_verifier() -> None:
    """Cognito decodes Salt/PasswordVerifier as signed big-endian ints; a high bit must be
    0x00-padded so neither is negative. Regression for the ConfirmDevice 400
    'Found negative value for salt or password verifier'."""
    salt = b"\x80" + b"\x11" * 15  # high bit set -> negative without padding
    verifier_b64, salt_b64 = auth._generate_device_verifier("grp", "devkey", "pw", salt=salt)
    salt_bytes = base64.b64decode(salt_b64)
    verifier_bytes = base64.b64decode(verifier_b64)
    # High bit clear (first byte < 0x80) keeps Cognito's signed decode non-negative.
    assert salt_bytes[0] < 0x80
    assert verifier_bytes[0] < 0x80
    # The sign-fixed salt is the original 16 bytes with a 0x00 prepended, and that same
    # salt is what gets sent (so it matches the value used in the x-hash).
    assert salt_bytes == b"\x00" + salt


def test_authenticate_handles_remembered_device_srp(tmp_path, monkeypatch) -> None:
    configure_paths(state_dir=tmp_path)
    auth.save_session(
        {
            "access_token": make_jwt(time.time() - 5),
            "id_token": "old-id",
            "refresh_token": "refresh-token",
            "client_id": "client123",
            "user_pool_id": "us-east-1_pool123",
            "email": "l@example.com",
            "device_key": "device123",
            "device_group_key": "device-group",
            "device_password": "device-password",
        }
    )
    access = make_jwt(time.time() + 3600)
    cognito = FakeCognitoClient(
        [
            password_challenge(),
            {
                "ChallengeName": "DEVICE_SRP_AUTH",
                "Session": "session-2",
                "ChallengeParameters": {"USERNAME": "user-sub"},
            },
            {
                "ChallengeName": "DEVICE_PASSWORD_VERIFIER",
                "Session": "session-3",
                "ChallengeParameters": {
                    "SALT": "beadfeed",
                    "SRP_B": "7",
                    "SECRET_BLOCK": base64.b64encode(b"device-secret").decode("ascii"),
                    "USERNAME": "user-sub",
                },
            },
            {
                "AuthenticationResult": {
                    "AccessToken": access,
                    "IdToken": "id-token",
                    "RefreshToken": "refresh-token-new",
                    "ExpiresIn": 3600,
                }
            },
        ]
    )

    session = auth.authenticate(
        email="l@example.com",
        password="password",
        otp_callback=None,
        extend_client=FakeExtendClient(),
        cognito_client=cognito,
    )

    assert session["access_token"] == access
    assert cognito.calls[2]["payload"]["ChallengeName"] == "DEVICE_SRP_AUTH"
    assert cognito.calls[3]["payload"]["ChallengeName"] == "DEVICE_PASSWORD_VERIFIER"
    assert cognito.calls[3]["payload"]["ChallengeResponses"]["DEVICE_KEY"] == "device123"

    configure_paths()


def test_auth_network_paths_refuse_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "_assert_not_disabled",
        lambda: (_ for _ in ()).throw(PayWithExtendDisabled("disabled")),
    )

    with pytest.raises(PayWithExtendDisabled):
        auth.fetch_authconfig("l@example.com", client=FakeExtendClient())

    with pytest.raises(PayWithExtendDisabled):
        auth.refresh_tokens(
            {
                "refresh_token": "refresh",
                "client_id": "client123",
                "user_pool_id": "us-east-1_pool123",
                "email": "l@example.com",
            },
            cognito_client=FakeCognitoClient([]),
        )

    with pytest.raises(PayWithExtendDisabled):
        auth.ensure_valid_token(cognito_client=FakeCognitoClient([]))

    with pytest.raises(PayWithExtendDisabled):
        auth.fetch_current_user("token", client=FakeExtendClient())


def test_fetch_authconfig_trips_kill_switch_on_403(tmp_path, monkeypatch) -> None:
    disabled_path = tmp_path / "paywithextend_disabled.json"
    monkeypatch.setattr("extendvcc.client._disabled_state_path", lambda: disabled_path)

    class ForbiddenExtendClient:
        def post(self, url: str, headers: dict[str, str], content: bytes, timeout: int) -> FakeResponse:
            return FakeResponse({"message": "forbidden"}, status_code=403)

    with pytest.raises(PayWithExtendDisabled, match="403 response"):
        auth.fetch_authconfig("l@example.com", client=ForbiddenExtendClient())

    assert "403 response" in disabled_path.read_text(encoding="utf-8")


def test_fetch_current_user_trips_kill_switch_on_html_challenge(tmp_path, monkeypatch) -> None:
    disabled_path = tmp_path / "paywithextend_disabled.json"
    monkeypatch.setattr("extendvcc.client._disabled_state_path", lambda: disabled_path)

    class HtmlExtendClient:
        def get(self, url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
            return FakeResponse(
                ValueError("no json"),
                status_code=200,
                text="<!doctype html><html>Just a moment...</html>",
                headers={"content-type": "text/html"},
            )

    with pytest.raises(PayWithExtendDisabled, match="HTML/WAF"):
        auth.fetch_current_user("token", client=HtmlExtendClient())

    assert "HTML/WAF" in disabled_path.read_text(encoding="utf-8")
