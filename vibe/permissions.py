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
from typing import Any, Callable, Mapping, NoReturn
from urllib.parse import quote, urlsplit

import requests

from config import paths
from config.v2_config import V2Config


CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "permissions_projection.json"
CACHE_ORDER_FILENAME = "permissions_projection.order"
DEFAULT_TIMEOUT_SECONDS = 8.0
_SENSITIVE_KEY_PARTS = ("secret", "token", "credential")
_CACHE_FALLBACK_HTTP_STATUSES = frozenset({408, 425, 429})
_ACCESS_MODES = frozenset({"allowlist", "public"})
_PERMISSION_AUTHORITIES = frozenset({"instance", "cloud"})
_REQUIRED_PERMISSION_CAPABILITY = "instance.permissions.read"
_PRINCIPAL_KINDS = frozenset({"email", "email_domain", "organization_group"})
_ACCESS_ROLES = frozenset({"viewer", "editor"})
_ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
_PROJECT_ACCESS_MODES = frozenset({"inherit", "restricted"})
_PROJECT_SYNC_STATUSES = frozenset({"in_sync", "pending", "offline", "error", "deleted"})
_RESOURCE_KINDS = frozenset({"agent", "vault_secret", "skill", "show_page"})
_RESOURCE_ACCESS_LEVELS = frozenset({"private", "public", "scope"})
_RESOURCE_SYNC_STATUSES = frozenset({"in_sync", "pending", "offline", "error"})
_POLICY_SYNC_STATUSES = frozenset({"none", "in_sync", "applying", "offline", "error"})
_SYNC_COUNT_KEYS = ("active", "error", "offline", "applying", "in_sync")
_PROJECT_ID_PATH_SEPARATORS = frozenset({"/", "\\"})
_PROJECT_ID_DOT_SEGMENTS = frozenset({".", ".."})
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
    cache_order: int = 0

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


def _require_nonempty_string(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        _invalid_response()


def _require_https_public_url(value: Any) -> None:
    _require_nonempty_string(value)
    if value != value.strip():
        _invalid_response()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        _invalid_response()
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        _invalid_response()


def _is_valid_project_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in _PROJECT_ID_DOT_SEGMENTS
        and not any(separator in value for separator in _PROJECT_ID_PATH_SEPARATORS)
    )


def _require_project_id(value: Any) -> None:
    if not _is_valid_project_id(value):
        _invalid_response()


def _require_resource_identity(resource_kind: Any, resource_id: Any) -> tuple[str, str]:
    _require_enum(resource_kind, _RESOURCE_KINDS)
    if (
        not isinstance(resource_id, str)
        or resource_id != resource_id.strip()
        or not resource_id
        or len(resource_id) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in resource_id)
    ):
        raise PermissionsInvalidResponseError("invalid_resource_id")
    return resource_kind, resource_id


def _require_enum(value: Any, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        _invalid_response()


def _principal_identity(kind: str, value: str) -> tuple[str, str]:
    normalized = value.strip()
    if kind == "email_domain":
        normalized = normalized.removeprefix("@")
    if kind != "organization_group":
        normalized = normalized.lower()
    return kind, normalized


def _require_unique_identities(identities: list[object]) -> None:
    if len(identities) != len(set(identities)):
        _invalid_response()


def _require_nonnegative_integer(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _invalid_response()


def _is_backend_unavailable_status(status: int) -> bool:
    return status >= 500 or status in _CACHE_FALLBACK_HTTP_STATUSES


def _validate_access_entries(value: Any) -> list[Any]:
    entries = _require_list(value)
    identities: list[object] = []
    for item in entries:
        entry = _require_mapping(item)
        _require_keys(entry, "kind", "value", "role")
        _require_enum(entry["kind"], _PRINCIPAL_KINDS)
        _require_nonempty_string(entry["value"])
        _require_enum(entry["role"], _ACCESS_ROLES)
        identities.append(_principal_identity(entry["kind"], entry["value"]))
    _require_unique_identities(identities)
    return entries


def _validate_project(value: Any) -> dict[str, Any]:
    project = _require_mapping(value)
    _require_keys(project, "project_id", "organization_id", "display_name", "access", "sync")
    _require_project_id(project["project_id"])
    _require_nonempty_string(project["organization_id"], nullable=True)
    _require_string(project["display_name"])

    access = _require_mapping(project["access"])
    _require_keys(access, "mode", "revision", "bindings")
    _require_enum(access["mode"], _PROJECT_ACCESS_MODES)
    _require_nonnegative_integer(access["revision"])
    binding_identities: list[object] = []
    for item in _require_list(access["bindings"]):
        binding = _require_mapping(item)
        _require_keys(binding, "principal_kind", "principal_value", "access_role")
        _require_enum(binding["principal_kind"], _PRINCIPAL_KINDS)
        _require_nonempty_string(binding["principal_value"])
        _require_enum(binding["access_role"], _ACCESS_ROLES)
        binding_identities.append(
            _principal_identity(binding["principal_kind"], binding["principal_value"])
        )
    _require_unique_identities(binding_identities)

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
    _require_nonempty_string(instance["id"])
    if instance["id"] != instance_id:
        raise PermissionsInvalidResponseError("permissions_instance_mismatch")
    _require_enum(instance["access_mode"], _ACCESS_MODES)
    _require_enum(instance["permission_authority"], _PERMISSION_AUTHORITIES)
    if not isinstance(instance["local_mutation_allowed"], bool):
        _invalid_response()
    _require_nonnegative_integer(instance["authorization_revision"])
    if "name" in instance:
        _require_nonempty_string(instance["name"])
    if "public_url" in instance:
        _require_https_public_url(instance["public_url"])
    if "organization" in instance and instance["organization"] is not None:
        organization = _require_mapping(instance["organization"])
        _require_keys(organization, "id", "name")
        _require_nonempty_string(organization["id"])
        _require_nonempty_string(organization["name"])

    capabilities = _require_list(projection["capabilities"])
    for capability in capabilities:
        if not isinstance(capability, str) or not capability:
            _invalid_response()
    if (
        _REQUIRED_PERMISSION_CAPABILITY not in capabilities
        or len(capabilities) != len(set(capabilities))
    ):
        _invalid_response()

    access = _require_mapping(projection["access"])
    _require_keys(access, "owner", "entries")
    owner = _require_mapping(access["owner"])
    _require_keys(owner, "email", "role")
    _require_nonempty_string(owner["email"], nullable=True)
    if owner["role"] != "owner":
        _invalid_response()
    _validate_access_entries(access["entries"])

    directory = _require_mapping(projection["directory"])
    _require_keys(directory, "members", "groups")
    member_ids: list[object] = []
    for item in _require_list(directory["members"]):
        member = _require_mapping(item)
        _require_keys(member, "id", "email", "organization_role", "group_ids")
        _require_nonempty_string(member["id"])
        _require_nonempty_string(member["email"])
        _require_enum(member["organization_role"], _ORGANIZATION_ROLES)
        member_group_ids = _require_list(member["group_ids"])
        for group_id in member_group_ids:
            _require_nonempty_string(group_id)
        member_ids.append(member["id"])
        _require_unique_identities(member_group_ids)
    _require_unique_identities(member_ids)

    directory_group_ids: list[object] = []
    for item in _require_list(directory["groups"]):
        group = _require_mapping(item)
        _require_keys(group, "id", "name", "archived_at")
        _require_nonempty_string(group["id"])
        _require_string(group["name"])
        _require_string(group["archived_at"], nullable=True)
        directory_group_ids.append(group["id"])
    _require_unique_identities(directory_group_ids)

    project_ids: list[object] = []
    for project_value in _require_list(projection["projects"]):
        project = _validate_project(project_value)
        project_ids.append(project["project_id"])
    _require_unique_identities(project_ids)

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


def _validate_resource(
    value: Any,
    *,
    instance_id: str,
    resource_kind: str,
    resource_id: str,
) -> dict[str, Any]:
    resource = _require_mapping(value)
    _require_keys(
        resource,
        "instance_id",
        "resource_kind",
        "resource_id",
        "display_name",
        "owner_user_id",
        "access",
        "sync",
    )
    if (
        resource["instance_id"] != instance_id
        or resource["resource_kind"] != resource_kind
        or resource["resource_id"] != resource_id
    ):
        raise PermissionsInvalidResponseError("permissions_resource_mismatch")
    _require_string(resource["display_name"])
    _require_nonempty_string(resource["owner_user_id"], nullable=True)

    access = _require_mapping(resource["access"])
    _require_keys(access, "access_level", "group_ids", "revision")
    _require_enum(access["access_level"], _RESOURCE_ACCESS_LEVELS)
    group_ids = _require_list(access["group_ids"])
    for group_id in group_ids:
        _require_nonempty_string(group_id)
    if len(group_ids) != len(set(group_ids)):
        _invalid_response()
    if access["access_level"] == "scope":
        if not group_ids:
            _invalid_response()
    elif group_ids:
        _invalid_response()
    _require_nonnegative_integer(access["revision"])

    sync = _require_mapping(resource["sync"])
    _require_keys(
        sync,
        "status",
        "desired_acl_revision",
        "applied_acl_revision",
        "last_synced_at",
    )
    _require_enum(sync["status"], _RESOURCE_SYNC_STATUSES)
    _require_nonnegative_integer(sync["desired_acl_revision"])
    _require_nonnegative_integer(sync["applied_acl_revision"])
    _require_string(sync["last_synced_at"], nullable=True)
    if "last_sync_error" in sync:
        _require_string(sync["last_sync_error"])
    return resource


def _validated_resource_result(
    payload: Any,
    *,
    instance_id: str,
    resource_kind: str,
    resource_id: str,
    mutation: bool,
    pairing_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    result = _require_mapping(_strip_sensitive(payload))
    _require_keys(result, "resource")
    if mutation:
        _require_keys(result, "ok")
        if result["ok"] is not True:
            _invalid_response()
    result["resource"] = _validate_resource(
        result["resource"],
        instance_id=instance_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    if pairing_guard is not None:
        pairing_guard()
    return result


def _runtime_credentials(config: V2Config) -> tuple[str, str, str]:
    credentials = config.remote_access.vibe_cloud.runtime_credentials()
    if credentials is None:
        raise PermissionsNotPairedError("permissions_not_paired")
    return credentials


def _request_config(
    config: V2Config | None,
) -> tuple[V2Config, Callable[[], V2Config]]:
    return (config if config is not None else V2Config.load()), V2Config.load


def _guard_current_pairing(
    credentials: tuple[str, str, str],
    load_current_config: Callable[[], V2Config],
) -> None:
    try:
        current_credentials = _runtime_credentials(load_current_config())
    except (OSError, TypeError, ValueError, PermissionsNotPairedError) as exc:
        raise PermissionsPairingChangedError("permissions_pairing_changed") from exc
    if current_credentials != credentials:
        raise PermissionsPairingChangedError("permissions_pairing_changed")


def _mutation_payload(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    expected_instance_id = payload.get("if_match_instance_id")
    if not isinstance(expected_instance_id, str) or not expected_instance_id:
        raise PermissionsInvalidResponseError("invalid_request")
    backend_payload = dict(payload)
    del backend_payload["if_match_instance_id"]
    return expected_instance_id, backend_payload


def _cache_path() -> Path:
    return paths.get_state_dir() / CACHE_FILENAME


def _cache_order_path() -> Path:
    return paths.get_state_dir() / CACHE_ORDER_FILENAME


def _cache_file_lock(path: Path):
    """Return the cross-process lock for projection read-compare-write updates."""

    # Import lazily because storage's package initializer imports V2Config.
    from storage.lock import MigrationFileLock

    return MigrationFileLock(path.with_name(f".{path.stem}.lock"))


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        # mkstemp creates the file owner-private without relying on Unix-only APIs.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _write_cache(
    instance_id: str,
    projection: dict[str, Any],
    *,
    cache_order: int = 0,
) -> None:
    _atomic_write_json(
        _cache_path(),
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "instance_id": instance_id,
            "cached_at": int(time.time()),
            "cache_order": cache_order,
            "projection": projection,
        },
    )


def _read_cache(instance_id: str) -> PermissionsProjectionResult | None:
    try:
        envelope = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    cache_order = envelope.get("cache_order", 0)
    if not isinstance(cache_order, int) or isinstance(cache_order, bool) or cache_order < 0:
        return None
    try:
        projection = _validated_projection(envelope.get("projection"), instance_id)
    except PermissionsInvalidResponseError:
        return None
    return PermissionsProjectionResult(
        projection=projection,
        source="cache",
        cached_at=cached_at,
        cache_order=cache_order,
    )


def _read_pairing_bound_cache(
    instance_id: str,
    pairing_guard: Callable[[], None],
) -> PermissionsProjectionResult | None:
    """Read a fallback projection only while the captured pairing remains current."""

    pairing_guard()
    cached = _read_cache(instance_id)
    pairing_guard()
    return cached


def _read_cache_order() -> int:
    try:
        value = json.loads(_cache_order_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def _cache_allocate_order(instance_id: str) -> int:
    """Reserve a request-start order shared by controller and UI processes."""

    with _CACHE_LOCK:
        try:
            cache_path = _cache_path()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with _cache_file_lock(cache_path):
                cached = _read_cache(instance_id)
                current = max(
                    _read_cache_order(),
                    cached.cache_order if cached is not None else 0,
                )
                reserved = current + 1
                _atomic_write_json(_cache_order_path(), reserved)
                return reserved
        except (OSError, TimeoutError):
            logger.warning(
                "Unable to reserve the Permissions cache request order",
                exc_info=True,
            )
    # The written cache envelope will make this fallback visible to later writers.
    return max(time.time_ns(), 1)


def _prefer_mapping(
    preferred: Mapping[str, Any],
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Prefer one mapping while retaining additive fields from the other."""

    merged = dict(fallback)
    for key, value in preferred.items():
        previous = merged.get(key)
        if isinstance(value, Mapping) and isinstance(previous, Mapping):
            merged[key] = _prefer_mapping(value, previous)
        else:
            merged[key] = value
    return merged


def _merge_project_projection(
    preferred: Mapping[str, Any],
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge one Project while preserving the caller-selected snapshot winner.

    Equal-revision snapshots are ordered by their request-start epoch at the
    outer projection boundary. A status ranking here would let an older
    ``applying``/``pending`` response overwrite a newer terminal failure.
    Mutation acknowledgements also use this helper so their authoritative
    Project fields are retained while additive unknown fields survive.
    """

    return _prefer_mapping(preferred, fallback)


def _merge_project_mutation_acknowledgement(
    acknowledged: Mapping[str, Any],
    cached: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge one Project mutation without regressing its policy revision."""

    if cached["access"]["revision"] > acknowledged["access"]["revision"]:
        return _merge_project_projection(cached, acknowledged)
    return _merge_project_projection(acknowledged, cached)


def _merge_projects(
    preferred: list[Any],
    fallback: list[Any],
) -> list[dict[str, Any]]:
    fallback_by_id = {project["project_id"]: project for project in fallback}
    return [
        _merge_project_projection(project, fallback_by_id[project["project_id"]])
        if project["project_id"] in fallback_by_id
        else dict(project)
        for project in preferred
    ]


def _merge_policy_sync(
    preferred: Mapping[str, Any],
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Preserve the request-order winner for aggregate sync state."""

    return _prefer_mapping(preferred, fallback)


def _merge_equal_revision_projection(
    cached: dict[str, Any],
    candidate: dict[str, Any],
    *,
    prefer_candidate: bool,
) -> dict[str, Any]:
    preferred, fallback = (
        (candidate, cached) if prefer_candidate else (cached, candidate)
    )
    merged = _prefer_mapping(preferred, fallback)
    merged["projects"] = _merge_projects(
        preferred["projects"],
        fallback["projects"],
    )
    merged["policy_sync"] = _merge_policy_sync(
        preferred["policy_sync"],
        fallback["policy_sync"],
    )
    return merged


def _merge_mutation_rebase_projection(
    cached: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Apply mutation policy fields while retaining newer cached sync state."""

    merged = _prefer_mapping(candidate, cached)
    cached_projects = {
        project["project_id"]: project for project in cached["projects"]
    }
    projects: list[dict[str, Any]] = []
    for project in candidate["projects"]:
        cached_project = cached_projects.get(project["project_id"])
        if cached_project is None:
            projects.append(dict(project))
            continue
        merged_project = _merge_project_mutation_acknowledgement(
            project,
            cached_project,
        )
        merged_project["sync"] = _prefer_mapping(
            cached_project["sync"],
            project["sync"],
        )
        projects.append(merged_project)
    merged["projects"] = projects
    return merged


def _cache_read_merge_write(
    instance_id: str,
    merge: Callable[[dict[str, Any] | None], Any],
    *,
    request_order: int,
    mutation_rebase: bool = False,
    pairing_guard: Callable[[], None] | None = None,
) -> None:
    """Read, merge, and atomically replace one instance cache under one lock."""

    with _CACHE_LOCK:
        try:
            cache_path = _cache_path()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with _cache_file_lock(cache_path):
                if pairing_guard is not None:
                    pairing_guard()
                cached = _read_cache(instance_id)
                candidate = merge(cached.projection if cached is not None else None)
                if candidate is None:
                    return
                validated = _validated_projection(candidate, instance_id)
                cache_order = request_order
                if cached is not None:
                    cached_revision = cached.projection["instance"][
                        "authorization_revision"
                    ]
                    candidate_revision = validated["instance"][
                        "authorization_revision"
                    ]
                    if cached_revision > candidate_revision:
                        return
                    cache_order = max(cached.cache_order, request_order)
                    if cached_revision == candidate_revision and not mutation_rebase:
                        validated = _merge_equal_revision_projection(
                            cached.projection,
                            validated,
                            prefer_candidate=(
                                request_order >= cached.cache_order
                                if request_order and cached.cache_order
                                else request_order > 0
                            ),
                        )
                        validated = _validated_projection(validated, instance_id)
                    elif (
                        cached_revision == candidate_revision
                        and mutation_rebase
                        and cached.cache_order > request_order
                    ):
                        validated = _merge_mutation_rebase_projection(
                            cached.projection,
                            validated,
                        )
                        validated = _validated_projection(validated, instance_id)
                if pairing_guard is not None:
                    pairing_guard()
                _write_cache(instance_id, validated, cache_order=cache_order)
        except (OSError, TimeoutError):
            logger.warning("Unable to cache the current Permissions projection", exc_info=True)


def _cache_projection(
    instance_id: str,
    projection: Any,
    *,
    request_order: int | None = None,
    pairing_guard: Callable[[], None] | None = None,
) -> None:
    order = _cache_allocate_order(instance_id) if request_order is None else request_order
    _cache_read_merge_write(
        instance_id,
        lambda _cached: projection,
        request_order=order,
        pairing_guard=pairing_guard,
    )


def _cache_mutation_result(
    instance_id: str,
    authorization_revision: int,
    *,
    access_entries: list[Any] | None = None,
    project: Mapping[str, Any] | None = None,
    request_order: int | None = None,
    pairing_guard: Callable[[], None] | None = None,
) -> None:
    def merge(cached: dict[str, Any] | None) -> dict[str, Any] | None:
        if cached is None:
            return None
        current_revision = cached["instance"]["authorization_revision"]
        # A mutation acknowledgement can arrive after a newer global revision
        # was cached by another mutation. Keep that newer fence while replaying
        # the entity that this acknowledgement actually committed.
        effective_revision = max(current_revision, authorization_revision)
        projection = {
            **cached,
            "instance": {
                **cached["instance"],
                "authorization_revision": effective_revision,
            },
        }
        if access_entries is not None:
            projection["access"] = {
                **cached["access"],
                "entries": access_entries,
            }
        if project is not None:
            project_id = project.get("project_id")
            cached_project = next(
                (
                    current
                    for current in cached["projects"]
                    if current.get("project_id") == project_id
                ),
                None,
            )
            merged_project = (
                _merge_project_mutation_acknowledgement(project, cached_project)
                if cached_project is not None
                else project
            )
            projects = [
                merged_project if current.get("project_id") == project_id else current
                for current in cached["projects"]
            ]
            if not any(current.get("project_id") == project_id for current in projects):
                projects.append(merged_project)
            projection["projects"] = projects
        return projection

    order = _cache_allocate_order(instance_id) if request_order is None else request_order
    _cache_read_merge_write(
        instance_id,
        merge,
        request_order=order,
        mutation_rebase=True,
        pairing_guard=pairing_guard,
    )


def _acknowledge_authorization_revision(
    config: V2Config,
    revision: int,
    pairing_guard: Callable[[], None] | None = None,
) -> None:
    from vibe import remote_access

    try:
        remote_access.acknowledge_authorization_revision(
            config,
            revision,
            pairing_guard=pairing_guard,
        )
    except remote_access.AuthorizationRevisionPairingChangedError as exc:
        raise PermissionsPairingChangedError("permissions_pairing_changed") from exc


def _backend_request(
    config: V2Config,
    load_current_config: Callable[[], V2Config],
    method: str,
    suffix: str,
    payload: Mapping[str, Any] | None = None,
    *,
    expected_instance_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    credentials: tuple[str, str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    credentials = credentials or _runtime_credentials(config)
    backend_url, instance_id, instance_secret = credentials
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
        _guard_current_pairing(credentials, load_current_config)
        raise PermissionsUnavailableError("permissions_backend_unavailable") from exc
    _guard_current_pairing(credentials, load_current_config)
    if 300 <= response.status_code < 400:
        raise PermissionsInvalidResponseError("permissions_backend_redirect_blocked")
    try:
        parsed = response.json()
    except ValueError as exc:
        if _is_backend_unavailable_status(response.status_code):
            raise PermissionsUnavailableError("permissions_backend_unavailable") from exc
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response") from exc
    if not isinstance(parsed, dict):
        if _is_backend_unavailable_status(response.status_code):
            raise PermissionsUnavailableError("permissions_backend_unavailable")
        raise PermissionsInvalidResponseError("permissions_backend_invalid_response")
    if not response.ok:
        raise PermissionsBackendError(response.status_code, parsed)
    return parsed, instance_id


def get_current_permissions(config: V2Config | None = None) -> PermissionsProjectionResult:
    config, load_current_config = _request_config(config)
    credentials = _runtime_credentials(config)
    _, instance_id, _ = credentials
    request_order = _cache_allocate_order(instance_id)
    pairing_guard = lambda: _guard_current_pairing(credentials, load_current_config)
    try:
        payload, _ = _backend_request(config, load_current_config, "GET", "")
        projection = _validated_projection(payload, instance_id)
        _cache_projection(
            instance_id,
            projection,
            request_order=request_order,
            pairing_guard=pairing_guard,
        )
        return PermissionsProjectionResult(
            projection=projection,
            source="live",
            cache_order=request_order,
        )
    except PermissionsUnavailableError:
        cached = _read_pairing_bound_cache(instance_id, pairing_guard)
        if cached is not None:
            return cached
        raise
    except PermissionsBackendError as exc:
        if _is_backend_unavailable_status(exc.status):
            cached = _read_pairing_bound_cache(instance_id, pairing_guard)
            if cached is not None:
                return cached
        raise


def replace_authorized_users(
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    config, load_current_config = _request_config(config)
    expected_instance_id, backend_payload = _mutation_payload(payload)
    credentials = _runtime_credentials(config)
    _, request_instance_id, _ = credentials
    request_order = _cache_allocate_order(request_instance_id)
    pairing_guard = lambda: _guard_current_pairing(credentials, load_current_config)
    payload_result, instance_id = _backend_request(
        config,
        load_current_config,
        "PUT",
        "authorized-users",
        backend_payload,
        expected_instance_id=expected_instance_id,
    )
    result = _validated_authorized_users_result(payload_result)
    pairing_guard()
    _acknowledge_authorization_revision(
        config,
        result["authorization_revision"],
        pairing_guard,
    )
    _cache_mutation_result(
        instance_id,
        result["authorization_revision"],
        access_entries=result["entries"],
        request_order=request_order,
        pairing_guard=pairing_guard,
    )
    return {**result, "instance_id": instance_id}


def update_project_access(
    project_id: str,
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    if not _is_valid_project_id(project_id):
        raise PermissionsInvalidResponseError("invalid_project_id")
    config, load_current_config = _request_config(config)
    expected_instance_id, backend_payload = _mutation_payload(payload)
    credentials = _runtime_credentials(config)
    _, request_instance_id, _ = credentials
    request_order = _cache_allocate_order(request_instance_id)
    pairing_guard = lambda: _guard_current_pairing(credentials, load_current_config)
    payload_result, instance_id = _backend_request(
        config,
        load_current_config,
        "PUT",
        f"projects/{quote(project_id, safe='')}/access",
        backend_payload,
        expected_instance_id=expected_instance_id,
    )
    result = _validated_project_result(payload_result, project_id)
    pairing_guard()
    _acknowledge_authorization_revision(
        config,
        result["authorization_revision"],
        pairing_guard,
    )
    _cache_mutation_result(
        instance_id,
        result["authorization_revision"],
        project=result["project"],
        request_order=request_order,
        pairing_guard=pairing_guard,
    )
    return {**result, "instance_id": instance_id}


def get_resource_access(
    resource_kind: str,
    resource_id: str,
    config: V2Config | None = None,
) -> dict[str, Any]:
    resource_kind, resource_id = _require_resource_identity(resource_kind, resource_id)
    config, load_current_config = _request_config(config)
    credentials = _runtime_credentials(config)
    pairing_guard = lambda: _guard_current_pairing(credentials, load_current_config)
    payload_result, instance_id = _backend_request(
        config,
        load_current_config,
        "GET",
        f"resources/{quote(resource_kind, safe='')}/{quote(resource_id, safe='')}/access",
        credentials=credentials,
    )
    return _validated_resource_result(
        payload_result,
        instance_id=instance_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mutation=False,
        pairing_guard=pairing_guard,
    )


def update_resource_access(
    resource_kind: str,
    resource_id: str,
    payload: Mapping[str, Any],
    config: V2Config | None = None,
) -> dict[str, Any]:
    resource_kind, resource_id = _require_resource_identity(resource_kind, resource_id)
    config, load_current_config = _request_config(config)
    credentials = _runtime_credentials(config)
    pairing_guard = lambda: _guard_current_pairing(credentials, load_current_config)
    expected_instance_id, backend_payload = _mutation_payload(payload)
    payload_result, instance_id = _backend_request(
        config,
        load_current_config,
        "PUT",
        f"resources/{quote(resource_kind, safe='')}/{quote(resource_id, safe='')}/access",
        backend_payload,
        expected_instance_id=expected_instance_id,
        credentials=credentials,
    )
    return _validated_resource_result(
        payload_result,
        instance_id=instance_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mutation=True,
        pairing_guard=pairing_guard,
    )


def resolve_current_instance_ownership(
    config: V2Config | None = None,
) -> dict[str, Any]:
    """Resolve exact-instance ownership without trusting browser claims."""

    from storage import resource_access_service

    try:
        config = config or V2Config.load()
        credentials = config.remote_access.vibe_cloud.runtime_credentials()
    except (
        AttributeError,
        FileNotFoundError,
        IndexError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return {
            "mode": resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }
    if credentials is None:
        return {
            "mode": "unmanaged",
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }
    if (
        not isinstance(credentials, (tuple, list))
        or len(credentials) != 3
        or not isinstance(credentials[1], str)
        or not credentials[1].strip()
    ):
        return {
            "mode": resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }

    current = resource_access_service.current_show_page_instance_ownership()
    if current["mode"] in {
        "personal",
        "organization",
        resource_access_service.SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
    }:
        return current
    try:
        result = get_current_permissions(config)
    except (PermissionsError, OSError, TypeError, ValueError):
        return current
    instance = result.projection["instance"]
    organization = instance.get("organization")
    if isinstance(organization, Mapping):
        return {
            "mode": "organization",
            "instance_id": credentials[1],
            "organization_id": organization["id"],
            "source": result.source,
        }
    if "organization" in instance or config.remote_access.vibe_cloud.instance_kind == "personal":
        return {
            "mode": "personal",
            "instance_id": credentials[1],
            "organization_id": None,
            "source": result.source,
        }
    return current


def _local_instance_display(
    config: V2Config,
    instance_id: str,
) -> dict[str, str]:
    cloud = config.remote_access.vibe_cloud
    if str(cloud.instance_id or "").strip() != instance_id:
        return {}
    public_url = str(cloud.public_url or "").strip()
    try:
        _require_https_public_url(public_url)
        parsed = urlsplit(public_url)
    except PermissionsInvalidResponseError:
        return {}
    hostname = parsed.hostname or ""
    name = hostname.split(".", 1)[0]
    if name.endswith("-app"):
        name = name[:-4]
    if not name:
        return {}
    return {
        "name": name,
        "public_url": f"https://{parsed.netloc.lower().rstrip('.')}",
    }


def response_payload(
    result: PermissionsProjectionResult,
    config: V2Config | None = None,
) -> dict[str, Any]:
    projection = result.projection
    instance = projection["instance"]
    if "name" not in instance or "public_url" not in instance:
        try:
            local_display = _local_instance_display(config or V2Config.load(), instance["id"])
        except (FileNotFoundError, OSError, TypeError, ValueError):
            local_display = {}
        if local_display:
            instance = {**local_display, **instance}
            projection = {**projection, "instance": instance}
    return {
        "ok": True,
        "source": result.source,
        "offline": result.offline,
        "cached_at": result.cached_at,
        "projection": projection,
    }
