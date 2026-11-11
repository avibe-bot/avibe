"""Server-side Web Push primitives for the Workbench PWA."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from config import paths

logger = logging.getLogger(__name__)

_VAPID_FILE = "web_push_vapid.json"
DEFAULT_WEB_PUSH_TIMEOUT_SECONDS = 10.0
DEFAULT_WEB_PUSH_TTL_SECONDS = 12 * 60 * 60
_VAPID_CREATE_LOCK = threading.Lock()
MAX_NOTIFICATION_PLAINTEXT_BYTES = 2800
MAX_NOTIFICATION_TITLE_SCALARS = 80
MAX_NOTIFICATION_BODY_SCALARS = 240
MAX_NOTIFICATION_TAG_BYTES = 64
MAX_BADGE_COUNT = 2_147_483_647
DEFAULT_BADGE_VAPID_EXPIRY_SECONDS = 12 * 60 * 60
NATIVE_PUSH_RELAY_ORIGIN = "https://avibe.bot"
_NATIVE_PUSH_ENDPOINT_RE = re.compile(r"^/api/push/apns/([A-Za-z0-9_-]{43})$")
_NATIVE_BADGE_REQUEST_RE = re.compile(
    r"^/api/push/apns/[A-Za-z0-9_-]{43}/badge$"
)


class _BadgeRelayHttpxLogFilter(logging.Filter):
    _avibe_badge_relay_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name != "httpx" or not isinstance(args, tuple) or len(args) < 2:
            return True
        try:
            parsed = urllib.parse.urlsplit(str(args[1]))
        except ValueError:
            return True
        if (
            parsed.scheme == "https"
            and parsed.netloc == "avibe.bot"
            and _NATIVE_BADGE_REQUEST_RE.fullmatch(parsed.path)
        ):
            redacted = list(args)
            redacted[1] = f"{NATIVE_PUSH_RELAY_ORIGIN}/api/push/apns/<redacted>/badge"
            record.args = tuple(redacted)
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(
    getattr(log_filter, "_avibe_badge_relay_filter", False)
    for log_filter in _httpx_logger.filters
):
    _httpx_logger.addFilter(_BadgeRelayHttpxLogFilter())


class BadgeUpdateProtocolError(RuntimeError):
    """The relay returned a successful HTTP status with a non-contract body."""


@dataclass(frozen=True)
class VapidKeys:
    public_key: str
    private_key_pem: str


def _b64url_no_padding(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _truncate_scalars(value: str, limit: int) -> str:
    return "".join(list(value)[:limit])


def _truncate_utf8_bytes(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def serialize_notification_payload(payload: dict[str, Any]) -> str:
    """Serialize one notification under the shared browser/iOS byte contract."""

    normalized = dict(payload)
    for key, limit in (
        ("title", MAX_NOTIFICATION_TITLE_SCALARS),
        ("body", MAX_NOTIFICATION_BODY_SCALARS),
    ):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = _truncate_scalars(value, limit)

    tag = normalized.get("tag")
    if isinstance(tag, str):
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in tag):
            raise ValueError("notification_tag_contains_control_character")
        normalized["tag"] = _truncate_utf8_bytes(tag, MAX_NOTIFICATION_TAG_BYTES)

    while True:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) <= MAX_NOTIFICATION_PLAINTEXT_BYTES:
            return serialized

        body = normalized.get("body")
        if isinstance(body, str) and body:
            normalized["body"] = body[:-1]
            continue
        title = normalized.get("title")
        if isinstance(title, str) and title:
            normalized["title"] = title[:-1]
            continue
        raise ValueError("notification_payload_too_large")


def vapid_key_path() -> Path:
    return paths.get_state_dir() / _VAPID_FILE


def _read_vapid_keys(key_path: Path) -> VapidKeys:
    payload = json.loads(key_path.read_text(encoding="utf-8"))
    public_key = payload.get("public_key")
    private_key_pem = payload.get("private_key_pem")
    if isinstance(public_key, str) and isinstance(private_key_pem, str):
        return VapidKeys(public_key=public_key, private_key_pem=private_key_pem)
    raise ValueError("invalid_vapid_key_file")


def _generate_vapid_keys() -> VapidKeys:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()
    raw_public = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return VapidKeys(public_key=_b64url_no_padding(raw_public), private_key_pem=private_key_pem)


def _write_vapid_keys_exclusive(key_path: Path, keys: VapidKeys) -> None:
    payload = (
        json.dumps(
            {
                "public_key": keys.public_key,
                "private_key_pem": keys.private_key_pem,
            },
            indent=2,
        )
        + "\n"
    )
    tmp_path: Path | None = None
    with tempfile.NamedTemporaryFile(
        "w",
        dir=key_path.parent,
        encoding="utf-8",
        prefix=f".{key_path.name}.",
        delete=False,
    ) as fp:
        tmp_path = Path(fp.name)
        fp.write(payload)
        fp.flush()
        os.fsync(fp.fileno())
    tmp_path.chmod(0o600)
    try:
        os.link(tmp_path, key_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    try:
        key_path.chmod(0o600)
    except OSError:
        logger.debug("Could not chmod VAPID key file %s", key_path, exc_info=True)


def load_or_create_vapid_keys(path: Path | None = None) -> VapidKeys:
    key_path = path or vapid_key_path()
    if key_path.exists():
        return _read_vapid_keys(key_path)

    with _VAPID_CREATE_LOCK:
        if key_path.exists():
            return _read_vapid_keys(key_path)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        keys = _generate_vapid_keys()
        try:
            _write_vapid_keys_exclusive(key_path, keys)
        except FileExistsError:
            return _read_vapid_keys(key_path)
        return keys


def _badge_vapid_authorization(
    *,
    keys: VapidKeys,
    subject: str,
    expires_at: int,
) -> str:
    """Build deterministic ES256 VAPID bytes for fixture-safe badge requests."""

    private_key = serialization.load_pem_private_key(
        keys.private_key_pem.encode("ascii"),
        password=None,
    )
    if not (
        isinstance(private_key, ec.EllipticCurvePrivateKey)
        and isinstance(private_key.curve, ec.SECP256R1)
    ):
        raise ValueError("badge_vapid_key_must_be_p256")
    header = _b64url_no_padding(b'{"typ":"JWT","alg":"ES256"}')
    claims = _b64url_no_padding(
        json.dumps(
            {
                "aud": NATIVE_PUSH_RELAY_ORIGIN,
                "exp": expires_at,
                "sub": subject,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    unsigned_token = f"{header}.{claims}"
    der_signature = private_key.sign(
        unsigned_token.encode("ascii"),
        ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    )
    r, s = utils.decode_dss_signature(der_signature)
    signature = _b64url_no_padding(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    public_key = _b64url_no_padding(
        private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    return f"vapid t={unsigned_token}.{signature},k={public_key}"


def _native_relay_registration_id(subscription: dict[str, Any]) -> str | None:
    endpoint = subscription.get("endpoint")
    if not isinstance(endpoint, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "avibe.bot"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    match = _NATIVE_PUSH_ENDPOINT_RE.fullmatch(parsed.path)
    return match.group(1) if match is not None else None


def is_native_relay_subscription(subscription: dict[str, Any]) -> bool:
    """Return whether this is the fixed native APNs relay endpoint shape."""

    return _native_relay_registration_id(subscription) is not None


def send_badge_update(
    *,
    subscription: dict[str, Any],
    badge: int,
    vapid_keys: VapidKeys | None = None,
    subject: str = "mailto:notifications@avibe.bot",
    timeout: float = DEFAULT_WEB_PUSH_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> None:
    """Send one unencrypted, registration-scoped badge reconciliation request."""

    if isinstance(badge, bool) or not isinstance(badge, int) or not 0 <= badge <= MAX_BADGE_COUNT:
        raise ValueError("invalid_badge")
    registration_id = _native_relay_registration_id(subscription)
    if registration_id is None:
        raise ValueError("native_relay_subscription_required")

    keys = vapid_keys or load_or_create_vapid_keys()
    authorization = _badge_vapid_authorization(
        keys=keys,
        subject=subject,
        expires_at=int(time.time()) + DEFAULT_BADGE_VAPID_EXPIRY_SECONDS,
    )
    body = json.dumps({"badge": badge}, separators=(",", ":")).encode("utf-8")
    url = f"{NATIVE_PUSH_RELAY_ORIGIN}/api/push/apns/{registration_id}/badge"
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
    }

    def _post(http: httpx.Client) -> httpx.Response:
        return http.post(url, content=body, headers=headers, timeout=timeout)

    if client is None:
        with httpx.Client() as owned_client:
            response = _post(owned_client)
    else:
        response = _post(client)
    response.raise_for_status()
    if response.status_code != 201:
        raise BadgeUpdateProtocolError("badge relay returned an unexpected success status")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise BadgeUpdateProtocolError("badge relay returned an unexpected content type")
    try:
        result = response.json()
    except ValueError as exc:
        raise BadgeUpdateProtocolError("badge relay returned invalid JSON") from exc
    if not (
        isinstance(result, dict)
        and set(result) == {"ok", "registration_id", "badge", "apns_status"}
        and result.get("ok") is True
        and result.get("registration_id") == registration_id
        and isinstance(result.get("badge"), int)
        and not isinstance(result.get("badge"), bool)
        and result.get("badge") == badge
        and result.get("apns_status") == 200
    ):
        raise BadgeUpdateProtocolError("badge relay returned a non-contract response")


def send_web_push(
    *,
    subscription: dict[str, Any],
    payload: dict[str, Any],
    vapid_keys: VapidKeys | None = None,
    subject: str = "mailto:notifications@avibe.bot",
    timeout: float = DEFAULT_WEB_PUSH_TIMEOUT_SECONDS,
    ttl: int = DEFAULT_WEB_PUSH_TTL_SECONDS,
) -> None:
    """Send one Web Push payload.

    The pywebpush dependency is imported lazily so tests that only exercise key
    generation or subscription APIs do not need network-capable setup.
    """

    from py_vapid import Vapid
    from pywebpush import webpush

    keys = vapid_keys or load_or_create_vapid_keys()
    vapid_signer = Vapid.from_pem(keys.private_key_pem.encode("ascii"))
    subscription_info = {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription["p256dh"],
            "auth": subscription["auth"],
        },
    }
    webpush(
        subscription_info=subscription_info,
        data=serialize_notification_payload(payload),
        vapid_private_key=vapid_signer,
        vapid_claims={"sub": subject},
        timeout=timeout,
        ttl=ttl,
    )
