"""Browser-bound Avibe Cloud Organization management grants and proxying.

The browser never receives the Cloud Bearer token. A short-lived grant is held
in this process and addressed by two HttpOnly cookies: an opaque grant handle
and a stable browser binding. All Cloud requests are constrained to the paired,
SSRF-validated backend and an explicit management API allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import jwt

from config.v2_config import V2Config
from vibe import remote_access


MANAGEMENT_AUDIENCE = "avibe-organization-api"
MANAGEMENT_SCOPES = (
    "organization:read",
    "organization:manage",
    "instance:read",
    "instance:manage",
    "project:read",
    "project:manage",
    "resource:read",
    "resource:manage",
)
MANAGEMENT_SCOPE = " ".join(MANAGEMENT_SCOPES)
MANAGEMENT_TOKEN_MAX_TTL_SECONDS = 600
HANDSHAKE_TTL_SECONDS = 300
MAX_HANDSHAKES = 512
MAX_GRANTS = 2048
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024

HANDLE_COOKIE_NAME = "vibe_cloud_management"
BROWSER_COOKIE_NAME = "vibe_cloud_management_browser"
MANUAL_COOKIE_NAME = "vibe_cloud_management_manual"


@dataclass(frozen=True)
class ManagementHandshake:
    state: str
    browser_id: str
    expected_subject: str | None
    code_verifier: str
    nonce: str
    redirect_uri: str
    next_path: str
    silent: bool
    expires_at: float


@dataclass(frozen=True)
class ManagementGrant:
    handle: str
    browser_id: str
    subject: str
    email: str
    token: str
    expires_at: float


class CloudManagementError(RuntimeError):
    def __init__(self, code: str, *, status: int = 503, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


class CloudResponseError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error") or "cloud_management_upstream_error"))
        self.status = status
        self.payload = payload


_lock = threading.RLock()
_handshakes: OrderedDict[str, ManagementHandshake] = OrderedDict()
_grants: OrderedDict[str, ManagementGrant] = OrderedDict()
_browser_subjects: OrderedDict[str, str] = OrderedDict()


def _token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def _b64url_digest(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _prune_locked(now: float) -> None:
    for state, handshake in list(_handshakes.items()):
        if handshake.expires_at <= now:
            _handshakes.pop(state, None)
    for handle, grant in list(_grants.items()):
        if grant.expires_at <= now:
            _grants.pop(handle, None)
    while len(_handshakes) > MAX_HANDSHAKES:
        _handshakes.popitem(last=False)
    while len(_grants) > MAX_GRANTS:
        _grants.popitem(last=False)
    while len(_browser_subjects) > MAX_GRANTS:
        _browser_subjects.popitem(last=False)


def reset_for_tests() -> None:
    """Clear process-owned state; tests must never share grants or handshakes."""
    with _lock:
        _handshakes.clear()
        _grants.clear()
        _browser_subjects.clear()


def cloud_is_configured(config: V2Config | None) -> bool:
    if config is None:
        return False
    cloud = config.remote_access.vibe_cloud
    return bool(
        cloud.enabled
        and cloud.backend_url
        and cloud.instance_id
        and cloud.client_id
        and cloud.issuer
        and cloud.jwks_uri
        and cloud.public_url
    )


def validate_next_path(value: object) -> str:
    path = str(value or "/admin/organization/overview")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/admin/organization"):
        return "/admin/organization/overview"
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def new_browser_id() -> str:
    return _token(24)


def _bound_subject(browser_id: str) -> str | None:
    with _lock:
        subject = _browser_subjects.get(browser_id)
        if subject is not None:
            _browser_subjects.move_to_end(browser_id)
        return subject


def authorization_subject(
    browser_id: str | None,
    remote_subject: str | None,
) -> str | None:
    """Return the subject that can safely bind a management authorization."""
    return remote_subject or (_bound_subject(browser_id) if browser_id else None)


def can_silent_reauthorize(
    browser_id: str | None,
    remote_subject: str | None,
) -> bool:
    """Silent authorization is safe only when it cannot switch Cloud users."""
    return authorization_subject(browser_id, remote_subject) is not None


def begin_authorization(
    config: V2Config,
    *,
    browser_id: str,
    remote_subject: str | None,
    callback_origin: str,
    next_path: str,
    silent: bool,
) -> tuple[str, str]:
    if not cloud_is_configured(config):
        raise CloudManagementError("cloud_management_not_connected", status=409)
    backend = _validated_backend(config)
    verifier = _token(48)
    state = _token(32)
    nonce = _token(24)
    redirect_uri = f"{callback_origin.rstrip('/')}/auth/organization/callback"
    expected_subject = authorization_subject(browser_id, remote_subject)
    if silent and expected_subject is None:
        raise CloudManagementError(
            "cloud_management_authorization_required",
            status=401,
        )
    handshake = ManagementHandshake(
        state=state,
        browser_id=browser_id,
        expected_subject=expected_subject,
        code_verifier=verifier,
        nonce=nonce,
        redirect_uri=redirect_uri,
        next_path=validate_next_path(next_path),
        silent=silent,
        expires_at=time.time() + HANDSHAKE_TTL_SECONDS,
    )
    with _lock:
        _prune_locked(time.time())
        _handshakes[state] = handshake
        _handshakes.move_to_end(state)
        _prune_locked(time.time())
    return _authorization_url(config, backend.base_url, handshake), state


def _authorization_url(config: V2Config, backend_url: str, handshake: ManagementHandshake) -> str:
    params = {
        "client_id": config.remote_access.vibe_cloud.client_id,
        "redirect_uri": handshake.redirect_uri,
        "response_type": "code",
        "scope": MANAGEMENT_SCOPE,
        "state": handshake.state,
        "nonce": handshake.nonce,
        "code_challenge": _b64url_digest(handshake.code_verifier),
        "code_challenge_method": "S256",
    }
    if handshake.silent:
        params["prompt"] = "none"
    return f"{backend_url}/oauth/management/authorize?{urllib.parse.urlencode(params)}"


def handshake_for_handoff(state: str) -> ManagementHandshake | None:
    with _lock:
        _prune_locked(time.time())
        handshake = _handshakes.get(state)
        if handshake is not None:
            _handshakes.move_to_end(state)
        return handshake


def authorization_url_for_handoff(
    config: V2Config, state: str, browser_id: str | None
) -> tuple[str, str] | None:
    handshake = handshake_for_handoff(state)
    if handshake is None or (
        browser_id is not None and not secrets.compare_digest(handshake.browser_id, browser_id)
    ):
        return None
    return _authorization_url(config, _validated_backend(config).base_url, handshake), handshake.browser_id


def pop_handshake(state: str) -> ManagementHandshake | None:
    with _lock:
        _prune_locked(time.time())
        return _handshakes.pop(state, None)


def fail_handshake(state: str | None) -> str:
    if not state:
        return "/admin/organization/overview"
    handshake = pop_handshake(state)
    return handshake.next_path if handshake is not None else "/admin/organization/overview"


def complete_authorization(
    config: V2Config,
    *,
    state: str,
    code: str,
    browser_id: str,
    remote_subject: str | None,
) -> tuple[ManagementGrant, str]:
    handshake = pop_handshake(state)
    if handshake is None or not secrets.compare_digest(handshake.browser_id, browser_id):
        raise CloudManagementError("invalid_cloud_management_state", status=400)
    if not code:
        raise CloudManagementError("invalid_cloud_management_code", status=400)
    token_status, payload = _backend_request(
        config,
        "POST",
        "/oauth/management/token",
        form_body={
            "grant_type": "authorization_code",
            "client_id": config.remote_access.vibe_cloud.client_id,
            "redirect_uri": handshake.redirect_uri,
            "code": code,
            "code_verifier": handshake.code_verifier,
        },
    )
    if token_status < 200 or token_status >= 300:
        raise CloudManagementError("invalid_cloud_management_code", status=400)
    token = str(payload.get("access_token") or "")
    claims = _validate_management_token(config, token)
    subject = str(claims.get("sub") or "")
    email = str(claims.get("email") or "")
    response_subject = str(payload.get("subject") or "")
    response_instance = str(payload.get("vibe_instance_id") or "")
    subject_mismatch = any(
        expected is not None and not secrets.compare_digest(expected, subject)
        for expected in (handshake.expected_subject, remote_subject)
    )
    if (
        payload.get("token_type") != "Bearer"
        or response_subject != subject
        or response_instance != config.remote_access.vibe_cloud.instance_id
        or str(claims.get("vibe_instance_id") or "") != config.remote_access.vibe_cloud.instance_id
        or subject_mismatch
    ):
        if subject_mismatch and subject:
            raise CloudManagementError("cloud_management_subject_mismatch", status=409)
        raise CloudManagementError("invalid_cloud_management_token", status=400)
    expires_at = float(claims["exp"])
    grant = ManagementGrant(
        handle=_token(32),
        browser_id=browser_id,
        subject=subject,
        email=email,
        token=token,
        expires_at=expires_at,
    )
    with _lock:
        _prune_locked(time.time())
        for handle, existing in list(_grants.items()):
            if existing.browser_id == browser_id:
                _grants.pop(handle, None)
        _grants[grant.handle] = grant
        _browser_subjects[browser_id] = subject
        _browser_subjects.move_to_end(browser_id)
        _prune_locked(time.time())
    return grant, handshake.next_path


def resolve_grant(
    handle: str | None,
    browser_id: str | None,
    remote_subject: str | None,
) -> tuple[ManagementGrant | None, str | None]:
    if not handle or not browser_id:
        return None, None
    with _lock:
        _prune_locked(time.time())
        grant = _grants.get(handle)
        if grant is None or not secrets.compare_digest(grant.browser_id, browser_id):
            return None, None
        if remote_subject is not None and not secrets.compare_digest(grant.subject, remote_subject):
            _grants.pop(handle, None)
            return None, "cloud_management_subject_mismatch"
        _grants.move_to_end(handle)
        return grant, None


def invalidate_grant(
    handle: str | None,
    *,
    browser_id: str | None = None,
    clear_subject: bool = False,
) -> None:
    with _lock:
        grant = _grants.pop(handle, None) if handle else None
        if clear_subject:
            bound_browser_id = grant.browser_id if grant is not None else browser_id
            if bound_browser_id:
                _browser_subjects.pop(bound_browser_id, None)


def proxy_request(
    config: V2Config,
    *,
    grant: ManagementGrant,
    method: str,
    upstream_path: str,
    query: Sequence[tuple[str, str]],
    json_body: Any | None,
) -> tuple[int, dict[str, Any]]:
    if not proxy_path_allowed(method, upstream_path):
        raise CloudManagementError("cloud_management_route_not_allowed", status=404)
    return _backend_request(
        config,
        method,
        upstream_path,
        query=query,
        json_body=json_body,
        bearer_token=grant.token,
    )


_PROXY_RULES: tuple[tuple[str, str], ...] = (
    ("GET", r"/api/organizations"),
    ("GET|PATCH", r"/api/organizations/[^/]+"),
    ("GET|POST", r"/api/organizations/[^/]+/members"),
    ("GET", r"/api/organizations/[^/]+/members/export"),
    ("PATCH", r"/api/organizations/[^/]+/members/[^/]+"),
    ("POST", r"/api/organizations/[^/]+/members/[^/]+/resend-invite"),
    ("GET|POST", r"/api/organizations/[^/]+/groups"),
    ("GET|PATCH|DELETE", r"/api/organizations/[^/]+/groups/[^/]+"),
    ("PUT", r"/api/organizations/[^/]+/groups/[^/]+/members"),
    ("GET", r"/api/organizations/[^/]+/instances"),
    ("GET", r"/api/organizations/[^/]+/resources"),
    ("PATCH", r"/api/organizations/[^/]+/resources/[^/]+/[^/]+/[^/]+/access"),
    ("GET|POST|PUT|DELETE", r"/api/instances/[^/]+/authorized-users"),
    ("GET", r"/api/instances/[^/]+/projects"),
    ("GET|PUT", r"/api/instances/[^/]+/projects/[^/]+/access"),
)


def proxy_path_allowed(method: str, path: str) -> bool:
    import re

    if "%" in path or "//" in path or ".." in path:
        return False
    return any(re.fullmatch(methods, method.upper()) and re.fullmatch(pattern, path) for methods, pattern in _PROXY_RULES)


def _validated_backend(config: V2Config) -> Any:
    backend, error = remote_access._normalize_pairing_backend_url(  # noqa: SLF001
        config.remote_access.vibe_cloud.backend_url
    )
    if error or backend is None:
        raise CloudManagementError("cloud_management_backend_invalid", status=503)
    return backend


def _backend_request(
    config: V2Config,
    method: str,
    path: str,
    *,
    query: Sequence[tuple[str, str]] = (),
    json_body: Any | None = None,
    form_body: Mapping[str, str] | None = None,
    bearer_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    backend = _validated_backend(config)
    if not path.startswith("/") or "#" in path:
        raise CloudManagementError("cloud_management_route_not_allowed", status=404)
    encoded_query = urllib.parse.urlencode(list(query), doseq=True)
    url = f"{backend.base_url}{path}"
    if encoded_query:
        url = f"{url}?{encoded_query}"
    parsed = urllib.parse.urlsplit(url)
    target_path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
    headers = {
        "Accept": "application/json",
        "Host": backend.host_header,
        "User-Agent": "avibe/dev",
    }
    body: bytes | None = None
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        body = urllib.parse.urlencode(form_body).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise CloudManagementError("cloud_management_request_too_large", status=413)
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    proxy_url = remote_access._validated_backend_proxy_url(url)  # noqa: SLF001
    if backend.requires_proxy and not proxy_url:
        raise CloudManagementError("cloud_management_unavailable", retryable=True)
    last_error: Exception | None = None
    for connect_host in backend.connect_hosts:
        connection = remote_access._validated_backend_connection(  # noqa: SLF001
            connect_host, backend, 20.0, proxy_url
        )
        try:
            connection.request(method.upper(), target_path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise CloudManagementError("cloud_management_response_too_large")
            if 300 <= response.status < 400:
                raise CloudManagementError("cloud_management_redirect_blocked")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise CloudManagementError("cloud_management_invalid_response")
            try:
                payload = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise CloudManagementError("cloud_management_invalid_response") from exc
            if not isinstance(payload, dict):
                raise CloudManagementError("cloud_management_invalid_response")
            return response.status, payload
        except CloudManagementError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise CloudManagementError("cloud_management_unavailable", retryable=True) from last_error


def _validate_management_token(config: V2Config, token: str) -> dict[str, Any]:
    if not token:
        raise CloudManagementError("invalid_cloud_management_token", status=400)
    cloud = config.remote_access.vibe_cloud
    try:
        header = jwt.get_unverified_header(token)
        kid = str(header.get("kid") or "")
        jwks_path = _same_backend_path(config, cloud.jwks_uri)
        _, jwks = _backend_request(config, "GET", jwks_path)
        key_data = next(
            key
            for key in jwks.get("keys", [])
            if isinstance(key, dict) and str(key.get("kid") or "") == kid
        )
        signing_key = jwt.PyJWK.from_dict(key_data).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=MANAGEMENT_AUDIENCE,
            issuer=cloud.issuer,
            leeway=5,
        )
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        scopes = set(str(claims.get("scope") or "").split())
        if (
            not str(claims.get("sub") or "")
            or not str(claims.get("email") or "")
            or not str(claims.get("jti") or "")
            or expires_at <= time.time()
            or expires_at - issued_at > MANAGEMENT_TOKEN_MAX_TTL_SECONDS
            or not set(MANAGEMENT_SCOPES).issubset(scopes)
        ):
            raise ValueError("invalid management claims")
        return claims
    except CloudManagementError:
        raise
    except Exception as exc:
        raise CloudManagementError("invalid_cloud_management_token", status=400) from exc


def _same_backend_path(config: V2Config, absolute_url: str) -> str:
    backend = _validated_backend(config)
    parsed = urllib.parse.urlsplit(absolute_url)
    expected = urllib.parse.urlsplit(backend.base_url)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").rstrip(".").lower() != backend.hostname
        or (parsed.port or 443) != backend.port
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise CloudManagementError("invalid_cloud_management_token", status=400)
    base_path = expected.path.rstrip("/")
    if base_path and parsed.path != base_path and not parsed.path.startswith(f"{base_path}/"):
        raise CloudManagementError("invalid_cloud_management_token", status=400)
    relative_path = parsed.path[len(base_path) :] if base_path else parsed.path
    relative_path = relative_path or "/"
    if parsed.query:
        relative_path = f"{relative_path}?{parsed.query}"
    return relative_path
