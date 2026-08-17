from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from config.v2_config import V2Config
from core.show_pages import normalize_show_access_email, validate_share_id


CALLBACK_PATH = "/auth/show-identity/callback"
STATE_TTL_SECONDS = 10 * 60
ASSERTION_CLOCK_LEEWAY_SECONDS = 60
ASSERTION_MAX_TTL_SECONDS = 10 * 60
MAX_STATE_BYTES = 4096
MAX_ASSERTION_BYTES = 16 * 1024
MAX_CALLBACK_BODY_BYTES = 24 * 1024
MAX_CONSUMED_ASSERTIONS = 4096
_STATE_PREFIX = "vsi1"
_LEASE_PREFIX = "vsl1"
_STATE_PROCESS_SECRET = secrets.token_bytes(32)
_CONSUMED_ASSERTIONS_LOCK = threading.Lock()
_CONSUMED_ASSERTIONS: dict[str, int] = {}


class ShowIdentityError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ShowIdentityState:
    share_id: str
    return_target: str
    nonce: str
    callback_origin: str


@dataclass(frozen=True)
class VerifiedShowIdentity:
    subject: str
    normalized_email: str
    assertion_id: str
    expires_at: int


@dataclass(frozen=True)
class ShowGuestLease:
    page_id: str
    share_id: str
    normalized_email: str


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value
    ):
        raise ShowIdentityError("invalid_token")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ShowIdentityError("invalid_token") from exc


def _signature(secret: str, prefix: str, payload: str) -> str:
    return _b64url_encode(
        hmac.new(
            secret.encode("utf-8"),
            f"{prefix}.{payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def _state_secret(secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        _STATE_PROCESS_SECRET,
        hashlib.sha256,
    ).hexdigest()


def _encode_signed_payload(secret: str, prefix: str, payload: dict[str, Any]) -> str:
    encoded = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{prefix}.{encoded}.{_signature(secret, prefix, encoded)}"


def _decode_signed_payload(
    secret: str,
    prefix: str,
    token: str | None,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    if not isinstance(token, str) or len(token.encode("utf-8")) > max_bytes:
        raise ShowIdentityError("invalid_token")
    try:
        token_prefix, encoded, signature = token.split(".", 2)
    except ValueError as exc:
        raise ShowIdentityError("invalid_token") from exc
    expected = _signature(secret, prefix, encoded)
    if token_prefix != prefix or not hmac.compare_digest(signature, expected):
        raise ShowIdentityError("invalid_token")
    try:
        payload = json.loads(_b64url_decode(encoded))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowIdentityError("invalid_token") from exc
    if not isinstance(payload, dict):
        raise ShowIdentityError("invalid_token")
    return payload


def _cloud(config: V2Config):
    cloud = config.remote_access.vibe_cloud
    required = (
        cloud.enabled,
        cloud.backend_url,
        cloud.instance_id,
        cloud.client_id,
        cloud.issuer,
        cloud.jwks_uri,
        cloud.session_secret,
    )
    if not all(required):
        raise ShowIdentityError("identity_unavailable")
    return cloud


def _lease_secret(config: V2Config) -> str:
    secret = config.remote_access.vibe_cloud.session_secret
    if not isinstance(secret, str) or not secret:
        raise ShowIdentityError("identity_unavailable")
    return secret


def _normalize_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise ShowIdentityError("invalid_callback_origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ShowIdentityError("invalid_callback_origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}{f':{port}' if port is not None and port != 443 else ''}"


def _validate_return_target(share_id: str, return_target: str) -> str:
    if not isinstance(return_target, str) or len(return_target) > 2048:
        raise ShowIdentityError("invalid_return_target")
    parsed = urlsplit(return_target)
    prefix = f"/p/{quote(share_id, safe='')}/"
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not (parsed.path == prefix or parsed.path.startswith(prefix))
    ):
        raise ShowIdentityError("invalid_return_target")
    return return_target


def begin_show_identity_authorization(
    config: V2Config,
    *,
    callback_origin: str,
    share_id: str,
    return_target: str,
    now: int | None = None,
) -> str:
    cloud = _cloud(config)
    share_id = validate_share_id(share_id)
    callback_origin = _normalize_origin(callback_origin)
    return_target = _validate_return_target(share_id, return_target)
    issued_at = int(time.time()) if now is None else int(now)
    nonce = secrets.token_urlsafe(32)
    state = _encode_signed_payload(
        _state_secret(cloud.session_secret),
        _STATE_PREFIX,
        {
            "v": 1,
            "share_id": share_id,
            "return_target": return_target,
            "nonce": nonce,
            "callback_origin": callback_origin,
            "exp": issued_at + STATE_TTL_SECONDS,
        },
    )
    backend = cloud.backend_url.rstrip("/")
    endpoint = f"{backend}/api/v1/instances/{quote(cloud.instance_id, safe='')}/show-identity/authorize"
    query = urlencode(
        {
            "state": state,
            "nonce": nonce,
            "redirect_uri": f"{callback_origin}{CALLBACK_PATH}",
        }
    )
    return f"{endpoint}?{query}"


def read_show_identity_state(
    config: V2Config,
    token: str | None,
    *,
    callback_origin: str,
    now: int | None = None,
) -> ShowIdentityState:
    cloud = _cloud(config)
    payload = _decode_signed_payload(
        _state_secret(cloud.session_secret),
        _STATE_PREFIX,
        token,
        max_bytes=MAX_STATE_BYTES,
    )
    if set(payload) != {
        "v",
        "share_id",
        "return_target",
        "nonce",
        "callback_origin",
        "exp",
    }:
        raise ShowIdentityError("invalid_state")
    if payload.get("v") != 1 or type(payload.get("exp")) is not int:
        raise ShowIdentityError("invalid_state")
    current_time = int(time.time()) if now is None else int(now)
    if payload["exp"] <= current_time:
        raise ShowIdentityError("expired_state")
    raw_share_id = payload.get("share_id")
    if not isinstance(raw_share_id, str):
        raise ShowIdentityError("invalid_state")
    try:
        share_id = validate_share_id(raw_share_id)
    except Exception as exc:
        raise ShowIdentityError("invalid_state") from exc
    nonce = payload.get("nonce")
    signed_origin = payload.get("callback_origin")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        raise ShowIdentityError("invalid_state")
    if not isinstance(signed_origin, str) or not hmac.compare_digest(
        signed_origin,
        _normalize_origin(callback_origin),
    ):
        raise ShowIdentityError("invalid_callback_origin")
    return ShowIdentityState(
        share_id=share_id,
        return_target=_validate_return_target(share_id, payload.get("return_target")),
        nonce=nonce,
        callback_origin=signed_origin,
    )


def verify_show_identity_assertion(
    config: V2Config,
    token: str | None,
    *,
    expected_nonce: str,
) -> VerifiedShowIdentity:
    cloud = _cloud(config)
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_ASSERTION_BYTES:
        raise ShowIdentityError("invalid_assertion")
    try:
        header = jwt.get_unverified_header(token)
        if (
            header.get("alg") != "RS256"
            or header.get("typ") != "JWT"
            or not isinstance(header.get("kid"), str)
            or not header["kid"]
        ):
            raise ShowIdentityError("invalid_assertion")
        signing_key = PyJWKClient(cloud.jwks_uri, timeout=5).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=f"avibe-show-identity:{cloud.client_id}",
            issuer=cloud.issuer,
            leeway=ASSERTION_CLOCK_LEEWAY_SECONDS,
            options={
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "iat",
                    "exp",
                    "jti",
                    "nonce",
                    "instance_id",
                    "verified_email",
                ]
            },
        )
    except ShowIdentityError:
        raise
    except PyJWKClientConnectionError as exc:
        raise ShowIdentityError("identity_unavailable") from exc
    except Exception as exc:
        raise ShowIdentityError("invalid_assertion") from exc

    string_claims = ("sub", "jti", "nonce", "instance_id", "verified_email")
    if any(not isinstance(claims.get(name), str) or not claims[name] for name in string_claims):
        raise ShowIdentityError("invalid_assertion")
    if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
        raise ShowIdentityError("invalid_assertion")
    if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > ASSERTION_MAX_TTL_SECONDS:
        raise ShowIdentityError("invalid_assertion")
    if claims["nonce"] != expected_nonce or claims["instance_id"] != cloud.instance_id:
        raise ShowIdentityError("invalid_assertion")
    try:
        normalized_email = normalize_show_access_email(claims["verified_email"])
    except Exception as exc:
        raise ShowIdentityError("invalid_assertion") from exc
    return VerifiedShowIdentity(
        subject=claims["sub"],
        normalized_email=normalized_email,
        assertion_id=claims["jti"],
        expires_at=claims["exp"],
    )


def consume_verified_show_identity(
    identity: VerifiedShowIdentity,
    *,
    now: int | None = None,
) -> None:
    current_time = int(time.time()) if now is None else int(now)
    retain_until = identity.expires_at + ASSERTION_CLOCK_LEEWAY_SECONDS
    with _CONSUMED_ASSERTIONS_LOCK:
        expired = [
            assertion_id for assertion_id, expires_at in _CONSUMED_ASSERTIONS.items() if expires_at <= current_time
        ]
        for assertion_id in expired:
            _CONSUMED_ASSERTIONS.pop(assertion_id, None)
        if identity.assertion_id in _CONSUMED_ASSERTIONS:
            raise ShowIdentityError("replayed_assertion")
        if len(_CONSUMED_ASSERTIONS) >= MAX_CONSUMED_ASSERTIONS:
            raise ShowIdentityError("identity_unavailable")
        _CONSUMED_ASSERTIONS[identity.assertion_id] = retain_until


def show_guest_cookie_name(share_id: str) -> str:
    share_id = validate_share_id(share_id)
    suffix = hashlib.sha256(share_id.encode("utf-8")).hexdigest()[:20]
    return f"__Secure-vibe_show_guest_{suffix}"


def show_guest_cookie_path(share_id: str) -> str:
    return f"/p/{quote(validate_share_id(share_id), safe='')}"


def make_show_guest_lease(
    config: V2Config,
    *,
    page_id: str,
    share_id: str,
    normalized_email: str,
) -> str:
    # Deliberately a browser-session lease: access changes govern new
    # admissions without interrupting a page the user already opened.
    return _encode_signed_payload(
        _lease_secret(config),
        _LEASE_PREFIX,
        {
            "v": 1,
            "page_id": page_id,
            "share_id": validate_share_id(share_id),
            "normalized_email": normalize_show_access_email(normalized_email),
            "lease_id": secrets.token_urlsafe(18),
        },
    )


def read_show_guest_lease(
    config: V2Config,
    token: str | None,
    *,
    expected_share_id: str,
) -> ShowGuestLease:
    payload = _decode_signed_payload(
        _lease_secret(config),
        _LEASE_PREFIX,
        token,
        max_bytes=MAX_STATE_BYTES,
    )
    if set(payload) != {
        "v",
        "page_id",
        "share_id",
        "normalized_email",
        "lease_id",
    }:
        raise ShowIdentityError("invalid_lease")
    if payload.get("v") != 1:
        raise ShowIdentityError("invalid_lease")
    page_id = payload.get("page_id")
    share_id = payload.get("share_id")
    lease_id = payload.get("lease_id")
    try:
        expected_share_id = validate_share_id(expected_share_id)
    except Exception as exc:
        raise ShowIdentityError("invalid_lease") from exc
    if (
        not isinstance(page_id, str)
        or not page_id
        or not isinstance(share_id, str)
        or share_id != expected_share_id
        or not isinstance(lease_id, str)
        or not lease_id
    ):
        raise ShowIdentityError("invalid_lease")
    try:
        normalized_email = normalize_show_access_email(payload.get("normalized_email"))
    except Exception as exc:
        raise ShowIdentityError("invalid_lease") from exc
    return ShowGuestLease(
        page_id=page_id,
        share_id=share_id,
        normalized_email=normalized_email,
    )
