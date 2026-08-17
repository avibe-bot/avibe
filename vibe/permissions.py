"""Current paired-instance Permissions client and offline projection cache."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import quote

import requests

from config import paths
from config.v2_config import V2Config


CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "permissions_projection.json"
DEFAULT_TIMEOUT_SECONDS = 8.0
_SENSITIVE_KEY_PARTS = ("secret", "token", "credential")
logger = logging.getLogger(__name__)


class PermissionsError(RuntimeError):
    """Base error for the local Permissions boundary."""


class PermissionsNotPairedError(PermissionsError):
    pass


class PermissionsUnavailableError(PermissionsError):
    pass


class PermissionsInvalidResponseError(PermissionsError):
    pass


class PermissionsBackendError(PermissionsError):
    def __init__(self, status: int, payload: Mapping[str, Any] | None = None):
        self.status = status
        self.payload = _error_payload(payload)
        super().__init__(str(self.payload["error"]))


@dataclass(frozen=True)
class PermissionsProjectionResult:
    projection: dict[str, Any]
    source: str
    cached_at: int | None = None

    @property
    def offline(self) -> bool:
        return self.source == "cache"


def _error_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    code = payload.get("error") if isinstance(payload, Mapping) else None
    result: dict[str, Any] = {
        "error": code if isinstance(code, str) and code else "permissions_backend_rejected"
    }
    revision = payload.get("current_revision") if isinstance(payload, Mapping) else None
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        result["current_revision"] = revision
    return result


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_sensitive(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PermissionsInvalidResponseError("permissions_backend_invalid_response")


def _validated_projection(payload: Any, instance_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    sanitized = _strip_sensitive(payload)
    if not isinstance(sanitized, dict) or sanitized.get("schema_version") != 1:
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    instance = sanitized.get("instance")
    if not isinstance(instance, dict) or instance.get("id") != instance_id:
        raise PermissionsInvalidResponseError("permissions_instance_mismatch")
    if instance.get("permission_authority") not in {"instance", "cloud"}:
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    if not isinstance(instance.get("local_mutation_allowed"), bool):
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    revision = instance.get("authorization_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    for key in ("capabilities", "access", "directory", "projects", "policy_sync"):
        if key not in sanitized:
            raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    return sanitized


def _runtime_credentials(config: V2Config) -> tuple[str, str, str]:
    credentials = config.remote_access.vibe_cloud.runtime_credentials()
    if credentials is None:
        raise PermissionsNotPairedError("permissions_not_paired")
    return credentials


def _cache_path() -> Path:
    return paths.get_state_dir() / CACHE_FILENAME


def _write_cache(instance_id: str, projection: dict[str, Any]) -> None:
    cache_path = _cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "instance_id": instance_id,
        "cached_at": int(time.time()),
        "projection": projection,
    }
    fd, temporary_name = tempfile.mkstemp(
        dir=cache_path.parent,
        prefix=f".{cache_path.name}.",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(envelope, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, cache_path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _read_cache(instance_id: str) -> PermissionsProjectionResult | None:
    try:
        envelope = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if envelope.get("instance_id") != instance_id:
        return None
    cached_at = envelope.get("cached_at")
    if not isinstance(cached_at, int) or isinstance(cached_at, bool) or cached_at < 0:
        return None
    try:
        projection = _validated_projection(envelope.get("projection"), instance_id)
    except PermissionsInvalidResponseError:
        return None
    return PermissionsProjectionResult(
        projection=projection,
        source="cache",
        cached_at=cached_at,
    )


def _backend_request(
    config: V2Config,
    method: str,
    suffix: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], str]:
    backend_url, instance_id, instance_secret = _runtime_credentials(config)
    endpoint = (
        f"{backend_url}/api/v1/instances/{quote(instance_id, safe='')}/permissions"
        f"/{suffix.lstrip('/')}"
        if suffix
        else f"{backend_url}/api/v1/instances/{quote(instance_id, safe='')}/permissions"
    )
    try:
        response = requests.request(
            method,
            endpoint,
            json=dict(payload) if payload is not None else None,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "avibe/dev",
                "X-Vibe-Device-Secret": instance_secret,
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise PermissionsUnavailableError("permissions_backend_unavailable") from exc
    if 300 <= response.status_code < 400:
        raise PermissionsInvalidResponseError("permissions_backend_redirect_blocked")
    try:
        parsed = response.json()
    except ValueError as exc:
        if response.status_code >= 500:
            raise PermissionsUnavailableError("permissions_backend_unavailable") from exc
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response") from exc
    if not isinstance(parsed, dict):
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    if not response.ok:
        raise PermissionsBackendError(response.status_code, parsed)
    return parsed, instance_id


def get_current_permissions(config: V2Config | None = None) -> PermissionsProjectionResult:
    config = config or V2Config.load()
    _, instance_id, _ = _runtime_credentials(config)
    try:
        payload, _ = _backend_request(config, "GET", "")
        projection = _validated_projection(payload, instance_id)
        try:
            _write_cache(instance_id, projection)
        except OSError:
            logger.warning("Unable to cache the current Permissions projection", exc_info=True)
        return PermissionsProjectionResult(projection=projection, source="live")
    except PermissionsUnavailableError:
        cached = _read_cache(instance_id)
        if cached is not None:
            return cached
        raise
    except PermissionsBackendError as exc:
        if exc.status >= 500:
            cached = _read_cache(instance_id)
            if cached is not None:
                return cached
        raise


def replace_authorized_users(
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    config = config or V2Config.load()
    result, _ = _backend_request(config, "PUT", "authorized-users", payload)
    return result


def update_project_access(
    project_id: str,
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    if not isinstance(project_id, str) or not project_id or "/" in project_id:
        raise PermissionsInvalidResponseError("invalid_project_id")
    config = config or V2Config.load()
    result, _ = _backend_request(
        config,
        "PUT",
        f"projects/{quote(project_id, safe='')}/access",
        payload,
    )
    return result


def response_payload(result: PermissionsProjectionResult) -> dict[str, Any]:
    return {
        "ok": True,
        "source": result.source,
        "offline": result.offline,
        "cached_at": result.cached_at,
        "projection": result.projection,
    }
