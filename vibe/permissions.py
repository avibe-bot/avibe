"""Current paired-instance Permissions client and offline projection cache."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Mapping, NoReturn
from urllib.parse import quote

import requests

from config import paths
from config.v2_config import V2Config


CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "permissions_projection.json"
DEFAULT_TIMEOUT_SECONDS = 8.0
_SENSITIVE_KEY_PARTS = ("secret", "token", "credential")
_ACCESS_MODES = frozenset({"allowlist", "public"})
_PERMISSION_AUTHORITIES = frozenset({"instance", "cloud"})
_PERMISSION_CAPABILITIES = frozenset(
    {"instance.permissions.read", "instance.permissions.mutate"}
)
_PRINCIPAL_KINDS = frozenset({"email", "email_domain", "organization_group"})
_ACCESS_ROLES = frozenset({"viewer", "editor"})
_ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
_PROJECT_ACCESS_MODES = frozenset({"inherit", "restricted", "owner_only"})
_PROJECT_SYNC_STATUSES = frozenset(
    {"in_sync", "pending", "applying", "offline", "error", "deleted"}
)
_POLICY_SYNC_STATUSES = frozenset({"none", "in_sync", "applying", "offline", "error"})
_SYNC_COUNT_KEYS = ("active", "error", "offline", "applying", "in_sync")
logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.RLock()


class PermissionsError(RuntimeError):
    """Base error for the local Permissions boundary."""


class PermissionsNotPairedError(PermissionsError):
    pass


class PermissionsPairingChangedError(PermissionsError):
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


def _invalid_response() -> NoReturn:
    raise PermissionsInvalidResponseError("permissions_backend_invalid_response")


def _require_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid_response()
    return value


def _require_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        _invalid_response()
    return value


def _require_keys(value: Mapping[str, Any], *keys: str) -> None:
    if any(key not in value for key in keys):
        _invalid_response()


def _require_string(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        _invalid_response()


def _require_enum(value: Any, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        _invalid_response()


def _require_nonnegative_integer(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _invalid_response()


def _validate_access_entries(value: Any) -> list[Any]:
    entries = _require_list(value)
    for item in entries:
        entry = _require_mapping(item)
        _require_keys(entry, "kind", "value", "role")
        _require_enum(entry["kind"], _PRINCIPAL_KINDS)
        _require_string(entry["value"])
        _require_enum(entry["role"], _ACCESS_ROLES)
    return entries


def _validate_project(value: Any) -> dict[str, Any]:
    project = _require_mapping(value)
    _require_keys(project, "project_id", "organization_id", "display_name", "access", "sync")
    _require_string(project["project_id"])
    _require_string(project["organization_id"], nullable=True)
    _require_string(project["display_name"])

    access = _require_mapping(project["access"])
    _require_keys(access, "mode", "revision", "bindings")
    _require_enum(access["mode"], _PROJECT_ACCESS_MODES)
    _require_nonnegative_integer(access["revision"])
    for item in _require_list(access["bindings"]):
        binding = _require_mapping(item)
        _require_keys(binding, "principal_kind", "principal_value", "access_role")
        _require_enum(binding["principal_kind"], _PRINCIPAL_KINDS)
        _require_string(binding["principal_value"])
        _require_enum(binding["access_role"], _ACCESS_ROLES)

    sync = _require_mapping(project["sync"])
    _require_keys(
        sync,
        "status",
        "desired_access_revision",
        "applied_access_revision",
        "last_synced_at",
    )
    _require_enum(sync["status"], _PROJECT_SYNC_STATUSES)
    _require_nonnegative_integer(sync["desired_access_revision"])
    _require_nonnegative_integer(sync["applied_access_revision"])
    _require_string(sync["last_synced_at"], nullable=True)
    if "last_sync_error" in sync:
        _require_string(sync["last_sync_error"])
    return project


def _validate_sync_counts(value: Any) -> None:
    counts = _require_mapping(value)
    _require_keys(counts, *_SYNC_COUNT_KEYS)
    for key in _SYNC_COUNT_KEYS:
        _require_nonnegative_integer(counts[key])


def _validated_projection(payload: Any, instance_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _invalid_response()
    sanitized = _strip_sensitive(payload)
    projection = _require_mapping(sanitized)
    _require_keys(
        projection,
        "schema_version",
        "instance",
        "capabilities",
        "access",
        "directory",
        "projects",
        "policy_sync",
    )
    if (
        not isinstance(projection["schema_version"], int)
        or isinstance(projection["schema_version"], bool)
        or projection["schema_version"] != 1
    ):
        _invalid_response()

    instance = _require_mapping(projection["instance"])
    _require_keys(
        instance,
        "id",
        "access_mode",
        "permission_authority",
        "local_mutation_allowed",
        "authorization_revision",
    )
    if not isinstance(instance["id"], str):
        _invalid_response()
    if instance["id"] != instance_id:
        raise PermissionsInvalidResponseError("permissions_instance_mismatch")
    _require_enum(instance["access_mode"], _ACCESS_MODES)
    _require_enum(instance["permission_authority"], _PERMISSION_AUTHORITIES)
    if not isinstance(instance["local_mutation_allowed"], bool):
        _invalid_response()
    _require_nonnegative_integer(instance["authorization_revision"])

    capabilities = _require_list(projection["capabilities"])
    for capability in capabilities:
        _require_enum(capability, _PERMISSION_CAPABILITIES)
    if (
        "instance.permissions.read" not in capabilities
        or len(capabilities) != len(set(capabilities))
    ):
        _invalid_response()

    access = _require_mapping(projection["access"])
    _require_keys(access, "owner", "entries")
    owner = _require_mapping(access["owner"])
    _require_keys(owner, "email", "role")
    _require_string(owner["email"], nullable=True)
    if owner["role"] != "owner":
        _invalid_response()
    _validate_access_entries(access["entries"])

    directory = _require_mapping(projection["directory"])
    _require_keys(directory, "members", "groups")
    for item in _require_list(directory["members"]):
        member = _require_mapping(item)
        _require_keys(member, "id", "email", "organization_role", "group_ids")
        _require_string(member["id"])
        _require_string(member["email"])
        _require_enum(member["organization_role"], _ORGANIZATION_ROLES)
        for group_id in _require_list(member["group_ids"]):
            _require_string(group_id)
    for item in _require_list(directory["groups"]):
        group = _require_mapping(item)
        _require_keys(group, "id", "name", "archived_at")
        _require_string(group["id"])
        _require_string(group["name"])
        _require_string(group["archived_at"], nullable=True)

    for project in _require_list(projection["projects"]):
        _validate_project(project)

    policy_sync = _require_mapping(projection["policy_sync"])
    _require_keys(policy_sync, "status", "projects", "resources")
    _require_enum(policy_sync["status"], _POLICY_SYNC_STATUSES)
    _validate_sync_counts(policy_sync["projects"])
    _validate_sync_counts(policy_sync["resources"])
    return projection


def _validated_authorized_users_result(payload: Any) -> dict[str, Any]:
    result = _require_mapping(_strip_sensitive(payload))
    _require_keys(result, "ok", "entries", "authorization_revision")
    if result["ok"] is not True:
        _invalid_response()
    _validate_access_entries(result["entries"])
    _require_nonnegative_integer(result["authorization_revision"])
    return result


def _validated_project_result(payload: Any, project_id: str) -> dict[str, Any]:
    result = _require_mapping(_strip_sensitive(payload))
    _require_keys(result, "ok", "project", "authorization_revision")
    if result["ok"] is not True:
        _invalid_response()
    project = _validate_project(result["project"])
    if project["project_id"] != project_id:
        _invalid_response()
    _require_nonnegative_integer(result["authorization_revision"])
    return result


def _runtime_credentials(config: V2Config) -> tuple[str, str, str]:
    credentials = config.remote_access.vibe_cloud.runtime_credentials()
    if credentials is None:
        raise PermissionsNotPairedError("permissions_not_paired")
    return credentials


def _mutation_payload(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    expected_instance_id = payload.get("if_match_instance_id")
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        raise PermissionsInvalidResponseError("invalid_request")
    backend_payload = dict(payload)
    del backend_payload["if_match_instance_id"]
    return expected_instance_id, backend_payload


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


def _cache_projection(instance_id: str, projection: Any) -> None:
    validated = _validated_projection(projection, instance_id)
    with _CACHE_LOCK:
        cached = _read_cache(instance_id)
        if (
            cached is not None
            and cached.projection["instance"]["authorization_revision"]
            > validated["instance"]["authorization_revision"]
        ):
            return
        try:
            _write_cache(instance_id, validated)
        except OSError:
            logger.warning("Unable to cache the current Permissions projection", exc_info=True)


def _cache_mutation_result(
    instance_id: str,
    authorization_revision: int,
    *,
    access_entries: list[Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> None:
    with _CACHE_LOCK:
        cached = _read_cache(instance_id)
        if cached is None:
            return
        projection = cached.projection
        current_revision = projection["instance"]["authorization_revision"]
        if authorization_revision < current_revision:
            return
        projection = {
            **projection,
            "instance": {
                **projection["instance"],
                "authorization_revision": authorization_revision,
            },
        }
        if access_entries is not None:
            projection["access"] = {
                **projection["access"],
                "entries": access_entries,
            }
        if project is not None:
            project_id = project.get("project_id")
            projects = [
                project if current.get("project_id") == project_id else current
                for current in projection["projects"]
            ]
            if not any(current.get("project_id") == project_id for current in projects):
                projects.append(project)
            projection["projects"] = projects
        _cache_projection(instance_id, _validated_projection(projection, instance_id))


def _backend_request(
    config: V2Config,
    method: str,
    suffix: str,
    payload: Mapping[str, Any] | None = None,
    *,
    expected_instance_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], str]:
    backend_url, instance_id, instance_secret = _runtime_credentials(config)
    if expected_instance_id is not None and expected_instance_id != instance_id:
        raise PermissionsPairingChangedError("permissions_pairing_changed")
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
        _cache_projection(instance_id, projection)
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
    expected_instance_id, backend_payload = _mutation_payload(payload)
    payload_result, instance_id = _backend_request(
        config,
        "PUT",
        "authorized-users",
        backend_payload,
        expected_instance_id=expected_instance_id,
    )
    result = _validated_authorized_users_result(payload_result)
    _cache_mutation_result(
        instance_id,
        result["authorization_revision"],
        access_entries=result["entries"],
    )
    return result


def update_project_access(
    project_id: str,
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    if not isinstance(project_id, str) or not project_id or "/" in project_id:
        raise PermissionsInvalidResponseError("invalid_project_id")
    config = config or V2Config.load()
    expected_instance_id, backend_payload = _mutation_payload(payload)
    payload_result, instance_id = _backend_request(
        config,
        "PUT",
        f"projects/{quote(project_id, safe='')}/access",
        backend_payload,
        expected_instance_id=expected_instance_id,
    )
    result = _validated_project_result(payload_result, project_id)
    _cache_mutation_result(
        instance_id,
        result["authorization_revision"],
        project=result["project"],
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
