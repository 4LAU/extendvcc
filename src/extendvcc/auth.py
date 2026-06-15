"""PayWithExtend Cognito SRP authentication helpers."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import impit

from extendvcc._paths import state_dir

API_BASE = "https://api.paywithextend.com"
COGNITO_ENDPOINT = "https://cognito-idp.us-east-1.amazonaws.com/"
SESSION_FILENAME = "paywithextend_session.json"
TOKEN_SAFETY_MARGIN_SECONDS = 300

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
EXTEND_ACCEPT = "application/vnd.paywithextend.v2021-03-12+json"
EXTEND_BRAND = os.environ.get("EXTENDVCC_BRAND_ID", "br_2F0trP1UmE59x1ZkNIAqsg")

COGNITO_HEADERS = {
    "Content-Type": "application/x-amz-json-1.1",
    "User-Agent": USER_AGENT,
}

INFO_BITS = b"Caldera Derived Key"


class PayWithExtendAuthError(RuntimeError):
    """Raised when PayWithExtend authentication cannot complete."""


class OTPRequired(PayWithExtendAuthError):
    """Raised when Cognito requests an email OTP but no callback is available."""


class UnexpectedChallenge(PayWithExtendAuthError):
    """Raised when Cognito returns a challenge this module does not support."""


class SessionNotFound(PayWithExtendAuthError):
    """Raised when a token operation needs a saved session and none exists."""


def _session_path() -> Path:
    return state_dir() / SESSION_FILENAME


def _assert_not_disabled() -> None:
    from .client import assert_not_disabled

    assert_not_disabled()


def _secure_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_session(path: Path | None = None) -> dict[str, Any] | None:
    session_path = path or _session_path()
    if not session_path.exists():
        return None
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def save_session(session: Mapping[str, Any], path: Path | None = None) -> None:
    _secure_write_json(path or _session_path(), dict(session))


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _b64_json(payload: Mapping[str, Any]) -> str:
    return base64.b64encode(_json_bytes(payload)).decode("ascii")


def _decode_b64_json(value: str) -> dict[str, Any]:
    normalized = value.strip().strip('"')
    padded = normalized + "=" * (-len(normalized) % 4)
    decoded = base64.b64decode(padded)
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise PayWithExtendAuthError("PayWithExtend authconfig response was not an object")
    return payload


def _response_json(resp: Any) -> Any:
    if hasattr(resp, "json"):
        return resp.json()
    text = getattr(resp, "text", "")
    return json.loads(text)


def _raise_for_status(resp: Any, *, kind: str = "auth", path: str | None = None) -> None:
    """Turn a non-2xx response into a typed PROJECT exception.

    The impit/httpx-native ``raise_for_status`` raises library exceptions that
    escape the project's catch chain (auth errors map to exit codes via these
    types). So we inspect ``status_code`` ourselves and raise:

    - ``kind="auth"`` (Cognito calls) -> ``PayWithExtendAuthError``
    - ``kind="api"`` (Extend API calls) -> ``PayWithExtendAPIError`` (with status)

    Fakes that lack ``status_code`` are tolerated (treated as success), preserving
    offline test fixtures whose default status is 200.
    """
    status_code = int(getattr(resp, "status_code", 0) or 0)
    if status_code < 400:
        return
    if kind == "api":
        from .client import PayWithExtendAPIError

        raise PayWithExtendAPIError(
            f"PayWithExtend API request failed: {status_code} {path or ''}".rstrip(),
            status_code=status_code,
            path=path or "",
        )
    # Cognito error responses carry {"__type": "...", "message": "..."} — surface
    # it so a 400 distinguishes a wrong/expired code from a real flow bug. The body
    # holds only Cognito's own error type/message, no secrets.
    detail = ""
    try:
        body = _response_json(resp)
        if isinstance(body, dict):
            err_type = str(body.get("__type", "")).rsplit("#", 1)[-1]
            message = body.get("message") or body.get("Message") or ""
            detail = " - ".join(part for part in (err_type, message) if part)
    except Exception:
        detail = ""
    suffix = f" ({detail})" if detail else ""
    raise PayWithExtendAuthError(f"PayWithExtend Cognito request failed with status {status_code}{suffix}")


def _inspect_account_risk(resp: Any, path: str) -> None:
    from .client import inspect_account_risk

    inspect_account_risk(resp, path)


def _post_json(client: Any, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
    return client.post(url, headers=dict(headers), content=_json_bytes(payload), timeout=30)


def read_credentials() -> tuple[str, str]:
    email = os.environ.get("EXTENDVCC_EMAIL", "")
    password = os.environ.get("EXTENDVCC_PASSWORD", "")
    if not email or not password:
        raise PayWithExtendAuthError(
            "Credentials required: set EXTENDVCC_EMAIL and EXTENDVCC_PASSWORD env vars, "
            "or pass email/password directly to authenticate()"
        )
    return email, password


def _extend_headers(access_token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": EXTEND_ACCEPT,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "x-extend-app-id": "app.paywithextend.com",
        "x-extend-brand": EXTEND_BRAND,
        "x-extend-platform": "web",
        "x-extend-platform-version": USER_AGENT,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _default_extend_client() -> impit.Client:
    return impit.Client(browser="chrome", follow_redirects=True)


def _default_cognito_client() -> impit.Client:
    return impit.Client(follow_redirects=True)


def impit_supports_async() -> bool:
    return hasattr(impit, "AsyncClient")


def fetch_authconfig(email: str, client: Any | None = None) -> dict[str, str]:
    _assert_not_disabled()
    http = client or _default_extend_client()
    resp = http.post(
        f"{API_BASE}/authconfig",
        headers=_extend_headers(),
        content=_json_bytes({"email": email}),
        timeout=30,
    )
    _inspect_account_risk(resp, "/authconfig")
    _raise_for_status(resp, kind="api", path="/authconfig")

    raw_payload: Any
    try:
        raw_payload = _response_json(resp)
    except (json.JSONDecodeError, TypeError, ValueError):
        raw_payload = getattr(resp, "text", "")

    if isinstance(raw_payload, str):
        payload = _decode_b64_json(raw_payload)
    elif isinstance(raw_payload, dict) and isinstance(raw_payload.get("data"), str):
        payload = _decode_b64_json(raw_payload["data"])
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise PayWithExtendAuthError("PayWithExtend authconfig returned an unsupported payload")

    user_pool_id = payload.get("userPoolId") or payload.get("user_pool_id")
    client_id = payload.get("clientId") or payload.get("client_id")
    if not isinstance(user_pool_id, str) or not isinstance(client_id, str):
        raise PayWithExtendAuthError("PayWithExtend authconfig did not include pool/client IDs")
    return {"user_pool_id": user_pool_id, "client_id": client_id}


def _pool_name(user_pool_id: str) -> str:
    if "_" not in user_pool_id:
        raise PayWithExtendAuthError(f"Unexpected Cognito user pool ID: {user_pool_id}")
    return user_pool_id.split("_", 1)[1]


COGNITO_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200CB"
    "BE117577A615D6C770988C0BAD946E208E24FA074E5AB3143"
    "DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)

N = int(COGNITO_N_HEX, 16)
G = 2
K = int(
    hashlib.sha256(bytes.fromhex("00" + COGNITO_N_HEX + "0" + f"{G:x}")).hexdigest(),
    16,
)


def _pad_hex(value: int | str) -> str:
    if isinstance(value, int):
        hex_value = f"{value:x}"
    else:
        hex_value = value.lower().removeprefix("0x")
    if len(hex_value) % 2 == 1:
        hex_value = "0" + hex_value
    if hex_value and hex_value[0] in "89abcdef":
        hex_value = "00" + hex_value
    return hex_value


def _hex_to_int(value: str) -> int:
    return int(value, 16)


def _hex_to_bytes(value: int | str) -> bytes:
    return bytes.fromhex(_pad_hex(value))


def _hash_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _calculate_u(big_a: int, big_b: int) -> int:
    return _hex_to_int(_hash_hex(_hex_to_bytes(big_a) + _hex_to_bytes(big_b)))


def _calculate_x(salt_hex: str, username: str, password: str) -> int:
    user_pass = f"{username}:{password}".encode("utf-8")
    user_pass_hash = hashlib.sha256(user_pass).digest()
    return _hex_to_int(_hash_hex(_hex_to_bytes(salt_hex) + user_pass_hash))


def _hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    info = INFO_BITS + b"\x01"
    return hmac.new(prk, info, hashlib.sha256).digest()[:16]


def _utc_cognito_timestamp(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.UTC)
    return f"{current:%a %b} {current.day} {current:%H:%M:%S UTC %Y}"


class _SrpContext:
    def __init__(self, username: str, password: str, *, bytes_a: bytes | None = None) -> None:
        self.username = username
        self.password = password
        self.small_a = int.from_bytes(bytes_a or secrets.token_bytes(128), "big")
        self.large_a = pow(G, self.small_a, N)
        if self.large_a % N == 0:
            raise PayWithExtendAuthError("Generated invalid SRP_A value")

    @property
    def public_a_hex(self) -> str:
        return f"{self.large_a:x}"

    def password_claim_signature(
        self,
        *,
        pool_name: str,
        username_for_srp: str,
        username_for_signature: str,
        password: str,
        salt_hex: str,
        srp_b_hex: str,
        secret_block_b64: str,
        timestamp: str,
    ) -> str:
        big_b = _hex_to_int(srp_b_hex)
        if big_b % N == 0:
            raise PayWithExtendAuthError("Cognito returned an invalid SRP_B value")
        u_value = _calculate_u(self.large_a, big_b)
        if u_value == 0:
            raise PayWithExtendAuthError("Cognito returned an invalid SRP scrambling parameter")

        x_value = _calculate_x(salt_hex, f"{pool_name}{username_for_srp}", password)
        g_mod_pow_x = pow(G, x_value, N)
        s_value = pow(big_b - K * g_mod_pow_x, self.small_a + u_value * x_value, N)
        key = _hkdf(_hex_to_bytes(s_value), _hex_to_bytes(u_value))

        secret_block = base64.b64decode(secret_block_b64)
        message = (
            pool_name.encode("utf-8")
            + username_for_signature.encode("utf-8")
            + secret_block
            + timestamp.encode("utf-8")
        )
        digest = hmac.new(key, message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")


def _challenge_parameters(challenge: Mapping[str, Any]) -> dict[str, str]:
    params = challenge.get("ChallengeParameters", {})
    if not isinstance(params, dict):
        raise PayWithExtendAuthError("Cognito challenge did not include parameters")
    return {str(key): str(value) for key, value in params.items()}


def _cognito_target(action: str) -> dict[str, str]:
    return {
        **COGNITO_HEADERS,
        "X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}",
    }


def _initiate_auth(client: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    _assert_not_disabled()
    resp = _post_json(client, COGNITO_ENDPOINT, payload, _cognito_target("InitiateAuth"))
    _raise_for_status(resp)
    data = _response_json(resp)
    if not isinstance(data, dict):
        raise PayWithExtendAuthError("Cognito InitiateAuth returned a non-object response")
    return data


def _respond_to_auth_challenge(client: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    _assert_not_disabled()
    resp = _post_json(
        client,
        COGNITO_ENDPOINT,
        payload,
        _cognito_target("RespondToAuthChallenge"),
    )
    _raise_for_status(resp)
    data = _response_json(resp)
    if not isinstance(data, dict):
        raise PayWithExtendAuthError("Cognito challenge response returned a non-object response")
    return data


def _call_cognito(client: Any, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    _assert_not_disabled()
    resp = _post_json(client, COGNITO_ENDPOINT, payload, _cognito_target(action))
    _raise_for_status(resp)
    data = _response_json(resp)
    return data if isinstance(data, dict) else {}


def _auth_result_to_session(
    auth_result: Mapping[str, Any],
    *,
    email: str,
    user_pool_id: str,
    client_id: str,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    access_token = auth_result.get("AccessToken")
    id_token = auth_result.get("IdToken")
    if not isinstance(access_token, str) or not isinstance(id_token, str):
        raise PayWithExtendAuthError("Cognito did not return access/id tokens")

    session = dict(existing or {})
    session.update(
        {
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": auth_result.get("RefreshToken") or session.get("refresh_token"),
            "expires_at": _jwt_exp(access_token) or (time.time() + float(auth_result.get("ExpiresIn", 3600))),
            "email": email,
            "user_pool_id": user_pool_id,
            "client_id": client_id,
        }
    )
    return session


def _generate_device_password() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(40)).decode("ascii").rstrip("=")


def _generate_device_verifier(
    device_group_key: str,
    device_key: str,
    device_password: str,
    *,
    salt: bytes | None = None,
) -> tuple[str, str]:
    salt_bytes = salt or secrets.token_bytes(16)
    # Cognito decodes Salt and PasswordVerifier as signed big-endian integers, so a
    # leading high bit is read as negative ("Found negative value for salt or password
    # verifier") and ConfirmDevice 400s. Prepend a 0x00 byte when needed via
    # _hex_to_bytes/_pad_hex, and use the SAME sign-fixed salt for both the x-hash and
    # the wire value so they stay consistent.
    salt_bytes = _hex_to_bytes(salt_bytes.hex())
    device_username = f"{device_group_key}{device_key}"
    x_value = _calculate_x(salt_bytes.hex(), device_username, device_password)
    verifier = pow(G, x_value, N)
    return (
        base64.b64encode(_hex_to_bytes(verifier)).decode("ascii"),
        base64.b64encode(salt_bytes).decode("ascii"),
    )


def _remember_device(
    client: Any,
    *,
    access_token: str,
    new_device_metadata: Mapping[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    device_key = new_device_metadata.get("DeviceKey")
    device_group_key = new_device_metadata.get("DeviceGroupKey")
    if not isinstance(device_key, str) or not isinstance(device_group_key, str):
        return session

    device_password = _generate_device_password()
    verifier, salt = _generate_device_verifier(device_group_key, device_key, device_password)
    confirm_payload = {
        "AccessToken": access_token,
        "DeviceKey": device_key,
        "DeviceName": "extendvcc",
        "DeviceSecretVerifierConfig": {
            "PasswordVerifier": verifier,
            "Salt": salt,
        },
    }
    _call_cognito(client, "ConfirmDevice", confirm_payload)
    _call_cognito(
        client,
        "UpdateDeviceStatus",
        {
            "AccessToken": access_token,
            "DeviceKey": device_key,
            "DeviceRememberedStatus": "remembered",
        },
    )
    session.update(
        {
            "device_key": device_key,
            "device_group_key": device_group_key,
            "device_password": device_password,
        }
    )
    return session


def _password_verifier_response(
    *,
    challenge: Mapping[str, Any],
    srp_context: _SrpContext,
    client_id: str,
    user_pool_id: str,
    password: str,
    device_key: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    params = _challenge_parameters(challenge)
    timestamp = _utc_cognito_timestamp()
    username_for_srp = params["USER_ID_FOR_SRP"]
    signature = srp_context.password_claim_signature(
        pool_name=_pool_name(user_pool_id),
        username_for_srp=username_for_srp,
        username_for_signature=username_for_srp,
        password=password,
        salt_hex=params["SALT"],
        srp_b_hex=params["SRP_B"],
        secret_block_b64=params["SECRET_BLOCK"],
        timestamp=timestamp,
    )
    responses = {
        "USERNAME": username_for_srp,
        "PASSWORD_CLAIM_SECRET_BLOCK": params["SECRET_BLOCK"],
        "PASSWORD_CLAIM_SIGNATURE": signature,
        "TIMESTAMP": timestamp,
    }
    if device_key:
        responses["DEVICE_KEY"] = device_key
    payload: dict[str, Any] = {
        "ChallengeName": "PASSWORD_VERIFIER",
        "ClientId": client_id,
        "ChallengeResponses": responses,
    }
    if session:
        payload["Session"] = session
    return payload


def _device_password_verifier_response(
    *,
    challenge: Mapping[str, Any],
    srp_context: _SrpContext,
    client_id: str,
    device_group_key: str,
    device_key: str,
    device_password: str,
    session: str | None = None,
) -> dict[str, Any]:
    params = _challenge_parameters(challenge)
    timestamp = _utc_cognito_timestamp()
    signature = srp_context.password_claim_signature(
        pool_name=device_group_key,
        username_for_srp=device_key,
        username_for_signature=device_key,
        password=device_password,
        salt_hex=params["SALT"],
        srp_b_hex=params["SRP_B"],
        secret_block_b64=params["SECRET_BLOCK"],
        timestamp=timestamp,
    )
    payload: dict[str, Any] = {
        "ChallengeName": "DEVICE_PASSWORD_VERIFIER",
        "ClientId": client_id,
        "ChallengeResponses": {
            "USERNAME": params.get("USERNAME", ""),
            "DEVICE_KEY": device_key,
            "PASSWORD_CLAIM_SECRET_BLOCK": params["SECRET_BLOCK"],
            "PASSWORD_CLAIM_SIGNATURE": signature,
            "TIMESTAMP": timestamp,
        },
    }
    if session:
        payload["Session"] = session
    return payload


def _email_otp_response(
    *,
    challenge: Mapping[str, Any],
    client_id: str,
    username: str,
    otp_callback: Callable[[str], str] | None,
    session: str | None = None,
) -> dict[str, Any]:
    if otp_callback is None:
        raise OTPRequired("PayWithExtend requires an email OTP. Run setup interactively.")
    code = otp_callback("Enter the PayWithExtend email OTP: ").strip()
    payload: dict[str, Any] = {
        "ChallengeName": "EMAIL_OTP",
        "ClientId": client_id,
        "ChallengeResponses": {
            "USERNAME": username,
            "EMAIL_OTP_CODE": code,
        },
    }
    if session:
        payload["Session"] = session
    return payload


def _extract_auth_result(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    auth_result = response.get("AuthenticationResult")
    return auth_result if isinstance(auth_result, Mapping) else None


def authenticate(
    *,
    email: str | None = None,
    password: str | None = None,
    otp_callback: Callable[[str], str] | None = None,
    extend_client: Any | None = None,
    cognito_client: Any | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Run Cognito SRP auth and return the saved session payload."""

    if email is None or password is None:
        stored_email, stored_password = read_credentials()
        email = email or stored_email
        password = password or stored_password

    authconfig = fetch_authconfig(email, client=extend_client)
    user_pool_id = authconfig["user_pool_id"]
    client_id = authconfig["client_id"]
    existing = load_session() or {}
    device_key = existing.get("device_key")
    cognito = cognito_client or _default_cognito_client()
    user_srp = _SrpContext(email, password)

    auth_parameters = {"USERNAME": email, "SRP_A": user_srp.public_a_hex}
    if isinstance(device_key, str):
        auth_parameters["DEVICE_KEY"] = device_key
    response = _initiate_auth(
        cognito,
        {
            "AuthFlow": "USER_SRP_AUTH",
            "ClientId": client_id,
            "AuthParameters": auth_parameters,
        },
    )

    cognito_username = email
    while True:
        auth_result = _extract_auth_result(response)
        if auth_result is not None:
            session = _auth_result_to_session(
                auth_result,
                email=email,
                user_pool_id=user_pool_id,
                client_id=client_id,
                existing=existing,
            )
            new_device = auth_result.get("NewDeviceMetadata")
            if isinstance(new_device, Mapping):
                session = _remember_device(
                    cognito,
                    access_token=session["access_token"],
                    new_device_metadata=new_device,
                    session=session,
                )
            if save:
                save_session(session)
            return session

        challenge_name = response.get("ChallengeName")
        session_token = response.get("Session") if isinstance(response.get("Session"), str) else None
        params = _challenge_parameters(response)
        srp_username = params.get("USER_ID_FOR_SRP") or params.get("USERNAME")
        if srp_username:
            cognito_username = srp_username
        if challenge_name == "PASSWORD_VERIFIER":
            response = _respond_to_auth_challenge(
                cognito,
                _password_verifier_response(
                    challenge=response,
                    srp_context=user_srp,
                    client_id=client_id,
                    user_pool_id=user_pool_id,
                    password=password,
                    device_key=device_key if isinstance(device_key, str) else None,
                    session=session_token,
                ),
            )
        elif challenge_name == "EMAIL_OTP":
            response = _respond_to_auth_challenge(
                cognito,
                _email_otp_response(
                    challenge=response,
                    client_id=client_id,
                    username=cognito_username,
                    otp_callback=otp_callback,
                    session=session_token,
                ),
            )
        elif challenge_name == "DEVICE_SRP_AUTH":
            device_group_key = existing.get("device_group_key")
            device_password = existing.get("device_password")
            if not all(isinstance(v, str) for v in (device_key, device_group_key, device_password)):
                raise PayWithExtendAuthError("Cognito requested device SRP without saved device credentials")
            device_srp = _SrpContext(f"{device_group_key}{device_key}", device_password)
            response = _respond_to_auth_challenge(
                cognito,
                {
                    "ChallengeName": "DEVICE_SRP_AUTH",
                    "ClientId": client_id,
                    "ChallengeResponses": {
                        "USERNAME": _challenge_parameters(response).get("USERNAME", email),
                        "DEVICE_KEY": device_key,
                        "SRP_A": device_srp.public_a_hex,
                    },
                    **({"Session": session_token} if session_token else {}),
                },
            )
            user_srp = device_srp
        elif challenge_name == "DEVICE_PASSWORD_VERIFIER":
            device_group_key = existing.get("device_group_key")
            device_password = existing.get("device_password")
            if not all(isinstance(v, str) for v in (device_key, device_group_key, device_password)):
                raise PayWithExtendAuthError("Cognito requested device verifier without saved device credentials")
            response = _respond_to_auth_challenge(
                cognito,
                _device_password_verifier_response(
                    challenge=response,
                    srp_context=user_srp,
                    client_id=client_id,
                    device_group_key=device_group_key,
                    device_key=device_key,
                    device_password=device_password,
                    session=session_token,
                ),
            )
        else:
            raise UnexpectedChallenge(f"Unsupported PayWithExtend Cognito challenge: {challenge_name}")


def refresh_tokens(
    session: Mapping[str, Any] | None = None,
    *,
    cognito_client: Any | None = None,
    save: bool = True,
) -> dict[str, Any]:
    _assert_not_disabled()
    current = dict(session or load_session() or {})
    refresh_token = current.get("refresh_token")
    client_id = current.get("client_id")
    if not isinstance(refresh_token, str) or not isinstance(client_id, str):
        raise SessionNotFound("PayWithExtend refresh needs a saved refresh token and client ID")

    auth_parameters = {"REFRESH_TOKEN": refresh_token}
    device_key = current.get("device_key")
    if isinstance(device_key, str):
        auth_parameters["DEVICE_KEY"] = device_key

    cognito = cognito_client or _default_cognito_client()
    response = _initiate_auth(
        cognito,
        {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "ClientId": client_id,
            "AuthParameters": auth_parameters,
        },
    )
    auth_result = _extract_auth_result(response)
    if auth_result is None:
        raise PayWithExtendAuthError("Cognito refresh did not return AuthenticationResult")

    refreshed = _auth_result_to_session(
        auth_result,
        email=str(current.get("email", "")),
        user_pool_id=str(current.get("user_pool_id", "")),
        client_id=client_id,
        existing=current,
    )
    if save:
        save_session(refreshed)
    return refreshed


def _jwt_exp(token: str) -> float | None:
    try:
        payload_b64 = token.split(".")[1]
    except IndexError:
        return None
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


def ensure_valid_token(
    *,
    margin_seconds: int = TOKEN_SAFETY_MARGIN_SECONDS,
    cognito_client: Any | None = None,
) -> str:
    _assert_not_disabled()
    session = load_session()
    if session is None:
        session = authenticate()
    access_token = session.get("access_token")
    expires_at = _jwt_exp(access_token) if isinstance(access_token, str) else None
    if expires_at is None:
        # Fall back to the persisted expiry when the JWT carries no usable exp,
        # so an opaque token does not force a refresh on every single call.
        stored = session.get("expires_at")
        expires_at = float(stored) if isinstance(stored, (int, float)) else None
    if isinstance(access_token, str) and expires_at and time.time() + margin_seconds < expires_at:
        return access_token
    refreshed = refresh_tokens(session, cognito_client=cognito_client)
    return str(refreshed["access_token"])


def fetch_current_user(access_token: str, client: Any | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    _assert_not_disabled()
    http = client or _default_extend_client()
    resp = http.get(
        f"{API_BASE}/users/me",
        headers=_extend_headers(access_token),
        timeout=30,
    )
    _inspect_account_risk(resp, "/users/me")
    _raise_for_status(resp, kind="api", path="/users/me")
    payload = _response_json(resp)
    if not isinstance(payload, dict):
        raise PayWithExtendAuthError("PayWithExtend /users/me returned a non-object response")
    headers = {str(key).lower(): str(value) for key, value in getattr(resp, "headers", {}).items()}
    return payload, headers


def extract_org_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("org_id", "orgId", "organization_id", "organizationId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("organization", "org"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            found = extract_org_id(value)
            if found:
                return found
    for key in ("organizations", "orgs"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    found = extract_org_id(item)
                    if found:
                        return found
    return None


def _redact_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    visible = name[:2] if len(name) > 2 else name[:1]
    return f"{visible}***@{domain}"


def _setup_report(session: Mapping[str, Any], user: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    rate_limit_headers = {
        key: value for key, value in headers.items() if key.startswith("x-rate") or key.startswith("ratelimit")
    }
    return {
        "success": True,
        "email": _redact_email(session.get("email") if isinstance(session.get("email"), str) else None),
        "org_id": session.get("org_id"),
        "user_id": user.get("id") or user.get("userId"),
        "rate_limits": rate_limit_headers,
        "impit_async_supported": impit_supports_async(),
        "session_path": str(_session_path()),
    }


def setup(
    *,
    email: str | None = None,
    password: str | None = None,
    otp_callback: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    session = authenticate(email=email, password=password, otp_callback=otp_callback, save=False)
    user, headers = fetch_current_user(session["access_token"])
    org_id = extract_org_id(user)
    if org_id:
        session["org_id"] = org_id
    save_session(session)
    return _setup_report(session, user, headers)
