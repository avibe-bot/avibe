"""Local organization resource-policy state and evaluation helpers.

The control plane owns desired ACL intents. This module owns the local applied
policy used by future resource services, and deliberately stores no resource
content, prompts, paths, outputs, or secret values.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.engine import Connection

from config.v2_config import config_file_lock
from storage.db import get_cached_sqlite_engine
from storage.models import resource_access_groups, resource_access_policies, state_meta
from vibe.authorization import (
    AuthorizationContext,
    context_from_session_payload,
    instance_owner_context,
)


RESOURCE_KINDS = frozenset({"agent", "vault_secret", "skill", "show_page"})
ACCESS_LEVELS = frozenset({"public", "scope", "private"})
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
RESOURCE_USER_CONTEXT_METADATA_KEY = "resource_user_context"
RESOURCE_ORGANIZATIONS_META_KEY = "resource_access_organizations"
SHOW_PAGE_INSTANCE_OWNERSHIP_META_KEY = "show_page_instance_ownership"
HARNESS_ACCESS_FORBIDDEN_CODE = "harness_access_forbidden"
SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE = "configuration_unavailable"

_SHOW_PAGE_OWNERSHIP_MODES = frozenset(
    {
        "unmanaged",
        "personal",
        "organization",
        "organization_pending",
        SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
    }
)
_CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE = object()


class ResourceAccessError(ValueError):
    """A stable, non-sensitive policy or intent validation error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# Compatibility name for callers that predate the shared authorization
# context. Resource services and transports now pass the same immutable object.
ResourceUserContext = AuthorizationContext


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_optional_string(value: Any, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit or any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return None
    return cleaned


def _clean_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _required_identifier(value: Any, *, code: str) -> str:
    cleaned = _clean_optional_string(value)
    if cleaned is None:
        raise ResourceAccessError(code)
    return cleaned


def _assert_show_page_pairing_current(ownership: Mapping[str, Any]) -> None:
    """Reject a reconciliation snapshot that no longer names the current pairing."""

    configured = _configured_show_page_instance()
    instance_id = _clean_optional_string(ownership.get("instance_id"))
    if (
        configured is _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
        or configured is None
        or instance_id is None
        or configured[0] != instance_id
        or ownership.get("mode") not in {"personal", "organization"}
        or configured[1] != ownership.get("mode")
    ):
        raise ResourceAccessError("show_page_pairing_changed")


def _validate_resource_kind(resource_kind: Any) -> str:
    if resource_kind not in RESOURCE_KINDS:
        raise ResourceAccessError("invalid_resource_kind")
    return str(resource_kind)


def _validate_access_level(access_level: Any) -> str:
    if access_level not in ACCESS_LEVELS:
        raise ResourceAccessError("invalid_resource_acl_intent")
    return str(access_level)


def _normalize_group_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ResourceAccessError("invalid_resource_acl_intent")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        group_id = _clean_optional_string(item)
        if group_id is None:
            raise ResourceAccessError("invalid_resource_acl_intent")
        if group_id not in seen:
            seen.add(group_id)
            result.append(group_id)
    return result


def _validate_policy_values(access_level: Any, group_ids: Any, organization_id: str | None) -> tuple[str, list[str]]:
    normalized_level = _validate_access_level(access_level)
    normalized_groups = _normalize_group_ids(group_ids)
    if normalized_level in {"public", "scope"} and not organization_id:
        # Public and scoped policies are organization semantics. A personal
        # resource stays private rather than accepting a policy no context can
        # safely authorize.
        raise ResourceAccessError("invalid_resource_acl_intent")
    if normalized_level == "scope":
        if not organization_id or not normalized_groups:
            raise ResourceAccessError("invalid_resource_acl_intent")
    elif normalized_groups:
        raise ResourceAccessError("invalid_resource_acl_intent")
    return normalized_level, normalized_groups


def normalize_policy_request(
    access_level: Any,
    group_ids: Any,
    organization_id: str | None,
) -> tuple[str, list[str]]:
    """Validate one policy request and normalize ignored non-scope groups."""

    if access_level in {"private", "public"}:
        group_ids = []
    return _validate_policy_values(access_level, group_ids, _clean_optional_string(organization_id))


def _context_from_mapping(
    payload: Mapping[str, Any] | None,
    *,
    is_remote: bool,
) -> ResourceUserContext:
    if not is_remote:
        return instance_owner_context()
    return context_from_session_payload(payload or {})


def current_resource_context(
    session_payload: Mapping[str, Any] | None = None,
    *,
    is_remote: bool | None = None,
) -> ResourceUserContext:
    """Return the request's local resource-access context.

    Callers that already parsed the signed session should pass it explicitly.
    The no-argument form is intentionally best-effort for future service-layer
    callers running inside the UI request context; outside a request it returns
    an untrusted anonymous context rather than guessing at an identity.
    """

    if isinstance(session_payload, AuthorizationContext):
        return session_payload
    if session_payload is not None or is_remote is not None:
        return _context_from_mapping(
            session_payload,
            is_remote=bool(is_remote if is_remote is not None else session_payload is not None),
        )

    try:
        from vibe.ui_compat import g, has_request_context

        if has_request_context():
            context = getattr(g, "authorization_context", None)
            return context if isinstance(context, AuthorizationContext) else AuthorizationContext(is_remote=True)
    except Exception:
        pass
    return AuthorizationContext()


def resolve_resource_access_context(
    user_context: ResourceUserContext | Mapping[str, Any] | None = None,
) -> ResourceUserContext:
    """Resolve ACL context, defaulting non-HTTP service calls to Instance Owner."""

    if isinstance(user_context, AuthorizationContext):
        return user_context
    if isinstance(user_context, Mapping):
        return current_resource_context(user_context, is_remote=True)

    context = current_resource_context()
    if context.is_remote:
        return context

    from vibe.ui_compat import has_request_context

    if has_request_context():
        return context
    return instance_owner_context()


def ensure_harness_definition_write(
    user_context: ResourceUserContext | Mapping[str, Any] | None = None,
) -> ResourceUserContext:
    """Require Editor role for Harness definitions, independent of request origin."""

    context = resolve_resource_access_context(user_context)
    if not context.has_role("editor"):
        raise ResourceAccessError(HARNESS_ACCESS_FORBIDDEN_CODE)
    return context


def metadata_has_remote_resource_context(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether persisted metadata records a remote-origin principal."""

    return isinstance(metadata, Mapping) and isinstance(
        metadata.get(RESOURCE_USER_CONTEXT_METADATA_KEY),
        Mapping,
    )


def metadata_allows_harness_runtime(
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return whether persisted user provenance has Editor runtime access."""

    if not metadata_has_remote_resource_context(metadata):
        return True
    try:
        context = resource_user_context_from_metadata(metadata)
    except ResourceAccessError:
        return False
    return bool(context is not None and context.has_role("editor"))


def metadata_with_resource_user_context(
    metadata: Mapping[str, Any] | None,
    user_context: ResourceUserContext | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Store the minimum remote identity needed to recheck deferred work."""

    result = dict(metadata or {})
    result.pop(RESOURCE_USER_CONTEXT_METADATA_KEY, None)
    context = resolve_resource_access_context(user_context)
    if not context.is_remote:
        return result

    result[RESOURCE_USER_CONTEXT_METADATA_KEY] = {
        "sub": context.subject,
        "vibe_organization_id": context.organization_id,
        "vibe_organization_member_id": context.organization_member_id,
        "vibe_organization_role": context.organization_role,
        "vibe_group_ids": sorted(context.group_ids) if context.group_ids is not None else None,
        "vibe_membership_version": context.membership_version,
        "vibe_instance_role": context.instance_role,
        "vibe_instance_access_source": context.instance_access_source,
        "vibe_show_page_id": context.show_page_id,
        "claims_issued_at": context.claims_issued_at,
        "vibe_instance_authorization_revision": context.authorization_revision,
        "authorization_expires_at": _resource_context_expires_at(context),
    }
    return result


def _resource_context_expires_at(context: ResourceUserContext) -> int:
    from vibe.remote_access import SESSION_AUTHORIZATION_REFRESH_SECONDS

    issued_at = context.claims_issued_at or int(time.time())
    return issued_at + SESSION_AUTHORIZATION_REFRESH_SECONDS


def resource_user_context_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    now: int | None = None,
) -> ResourceUserContext | None:
    """Restore remote provenance for deferred authorization checks.

    The returned context retains the initiating role and ACL attributes.
    Deferred runtime executors still apply ``metadata_allows_harness_runtime``
    before doing work, so malformed or Viewer snapshots fail closed.

    The ``authorization_expires_at`` snapshot is recorded for audit but is no
    longer a hard execution cutoff: durable automation (Harness tasks/watches)
    must not be permanently suspended just because the creating browser session's
    refresh window elapsed. Active Organization membership is re-derived from the
    signed claims at execution time instead (avibe#1343 P1).
    """

    if not isinstance(metadata, Mapping):
        return None
    snapshot = metadata.get(RESOURCE_USER_CONTEXT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return None
    return current_resource_context(snapshot, is_remote=True)


def _as_context(user_context: ResourceUserContext | Mapping[str, Any] | None) -> ResourceUserContext:
    if isinstance(user_context, ResourceUserContext):
        return user_context
    if isinstance(user_context, Mapping):
        return _context_from_mapping(user_context, is_remote=True)
    return ResourceUserContext()


@contextmanager
def _connection(connection: Connection | None) -> Iterator[Connection]:
    if connection is not None:
        yield connection
        return
    engine = get_cached_sqlite_engine()
    with engine.begin() as active_connection:
        yield active_connection


def _configured_show_page_instance() -> tuple[str, str] | None | object:
    try:
        from config.v2_config import V2Config

        cloud = V2Config.load().remote_access.vibe_cloud
        credentials = cloud.runtime_credentials()
        if credentials is None:
            return None
        if not isinstance(credentials, (tuple, list)) or len(credentials) != 3:
            return _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
        instance_id = _clean_optional_string(credentials[1])
        if instance_id is None or cloud.instance_kind not in {"personal", "organization"}:
            return _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
    except (
        AttributeError,
        FileNotFoundError,
        IndexError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
    return instance_id, cloud.instance_kind


def _configuration_unavailable_ownership() -> dict[str, Any]:
    return {
        "mode": SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
        "instance_id": None,
        "organization_id": None,
        "source": "config",
    }


def _stored_show_page_instance_ownership(connection: Connection) -> dict[str, Any] | None:
    raw_value = connection.execute(
        select(state_meta.c.value_json).where(
            state_meta.c.key == SHOW_PAGE_INSTANCE_OWNERSHIP_META_KEY
        )
    ).scalar_one_or_none()
    try:
        payload = json.loads(raw_value) if raw_value else None
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    instance_id = _clean_optional_string(payload.get("instance_id"))
    mode = payload.get("mode")
    organization_id = _clean_optional_string(payload.get("organization_id"))
    if instance_id is None or mode not in {"personal", "organization"}:
        return None
    if mode == "organization" and organization_id is None:
        return None
    if mode == "personal" and organization_id is not None:
        return None
    return {
        "mode": mode,
        "instance_id": instance_id,
        "organization_id": organization_id,
        "source": "stored",
    }


def current_show_page_instance_ownership(
    *,
    connection: Connection | None = None,
) -> dict[str, Any]:
    """Return the persisted ownership fence for the exact current pairing."""

    configured = _configured_show_page_instance()
    if configured is _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE:
        return _configuration_unavailable_ownership()
    if configured is None:
        return {
            "mode": "unmanaged",
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }
    instance_id, instance_kind = configured
    with _connection(connection) as conn:
        stored = _stored_show_page_instance_ownership(conn)
    exact_stored = stored if stored and stored["instance_id"] == instance_id else None
    if instance_kind == "personal":
        return {
            "mode": "personal",
            "instance_id": instance_id,
            "organization_id": None,
            "source": "config",
        }
    if instance_kind == "organization":
        if exact_stored and exact_stored["mode"] == "organization":
            return exact_stored
        return {
            "mode": "organization_pending",
            "instance_id": instance_id,
            "organization_id": None,
            "source": "config",
        }
    if exact_stored is not None:
        return exact_stored
    return {
        "mode": "unmanaged",
        "instance_id": instance_id,
        "organization_id": None,
        "source": "config",
    }


def remember_show_page_instance_ownership(
    connection: Connection,
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist an authoritative binding only while its exact pairing is current."""

    with config_file_lock():
        return _remember_show_page_instance_ownership_locked(connection, ownership)


def _remember_show_page_instance_ownership_locked(
    connection: Connection,
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    """Implement ownership persistence while the pairing lock is held."""

    configured = _configured_show_page_instance()
    instance_id = _clean_optional_string(ownership.get("instance_id"))
    mode = ownership.get("mode")
    organization_id = _clean_optional_string(ownership.get("organization_id"))
    if (
        configured is None
        or configured is _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
        or instance_id != configured[0]
        or mode not in {"personal", "organization"}
        or configured[1] != mode
        or (mode == "organization") != bool(organization_id)
    ):
        return current_show_page_instance_ownership(connection=connection)
    payload = {
        "schema_version": 1,
        "instance_id": instance_id,
        "mode": mode,
        "organization_id": organization_id,
    }
    connection.execute(
        state_meta.delete().where(state_meta.c.key == SHOW_PAGE_INSTANCE_OWNERSHIP_META_KEY)
    )
    connection.execute(
        state_meta.insert().values(
            key=SHOW_PAGE_INSTANCE_OWNERSHIP_META_KEY,
            value_json=json.dumps(payload, separators=(",", ":")),
            updated_at=_utc_now_iso(),
        )
    )
    return {**payload, "source": str(ownership.get("source") or "live")}


def reconcile_show_page_resource_policy(
    connection: Connection,
    *,
    resource_id: str,
    ownership: Mapping[str, Any],
    owner_user_id: str | None,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Reconcile one Show Page policy under the persisted pairing lock."""

    with config_file_lock():
        return _reconcile_show_page_resource_policy_locked(
            connection,
            resource_id=resource_id,
            ownership=ownership,
            owner_user_id=owner_user_id,
            owner_email=owner_email,
        )


def _reconcile_show_page_resource_policy_locked(
    connection: Connection,
    *,
    resource_id: str,
    ownership: Mapping[str, Any],
    owner_user_id: str | None,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Reconcile one Show Page policy without changing either access axis."""

    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    mode = ownership.get("mode")
    if mode not in _SHOW_PAGE_OWNERSHIP_MODES:
        raise ResourceAccessError("invalid_show_page_ownership")
    if mode in {"personal", "organization"}:
        _assert_show_page_pairing_current(ownership)
        fence = _remember_show_page_instance_ownership_locked(connection, ownership)
    else:
        fence = current_show_page_instance_ownership(connection=connection)
    mode = fence["mode"]
    policy = _policy_row(connection, "show_page", identifier)
    policy_organization = _clean_optional_string(
        policy.get("organization_id") if policy else None
    )

    if mode == "unmanaged":
        status = "unmanaged"
    elif mode == "organization_pending":
        status = "pending"
    elif mode == SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE:
        status = SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
    elif mode == "personal":
        if policy is None:
            _assert_show_page_pairing_current(fence)
            policy = ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id=identifier,
                organization_id=None,
                owner_user_id=owner_user_id,
                owner_email=owner_email,
                access_level="private",
                created_by_user_id=owner_user_id,
                updated_by_user_id=owner_user_id,
            )
            status = "created"
        else:
            status = "unchanged" if policy_organization is None else "conflict"
    else:
        organization_id = _required_identifier(
            fence.get("organization_id"), code="invalid_organization_id"
        )
        if policy is None:
            _assert_show_page_pairing_current(fence)
            policy = ensure_resource_policy(
                connection,
                resource_kind="show_page",
                resource_id=identifier,
                organization_id=organization_id,
                owner_user_id=owner_user_id,
                owner_email=owner_email,
                access_level="private",
                created_by_user_id=owner_user_id,
                updated_by_user_id=owner_user_id,
            )
            status = "created"
        elif policy_organization is None:
            _assert_show_page_pairing_current(fence)
            connection.execute(
                resource_access_policies.update()
                .where(resource_access_policies.c.resource_kind == "show_page")
                .where(resource_access_policies.c.resource_id == identifier)
                .values(organization_id=organization_id)
            )
            connection.execute(
                resource_access_groups.update()
                .where(resource_access_groups.c.resource_kind == "show_page")
                .where(resource_access_groups.c.resource_id == identifier)
                .values(organization_id=organization_id)
            )
            remember_resource_organization(connection, organization_id)
            policy = _policy_row(connection, "show_page", identifier)
            status = "adopted"
        else:
            status = (
                "unchanged" if policy_organization == organization_id else "conflict"
            )
    serialized = _serialize_policy(connection, policy) if policy else None
    return {"status": status, "ownership": fence, "policy": serialized}


def _show_page_policy_matches_instance_ownership(
    ownership: Mapping[str, Any],
    policy: Mapping[str, Any] | None,
) -> bool:
    mode = ownership.get("mode")
    if mode == "unmanaged":
        return True
    if mode in {
        "organization_pending",
        SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE,
    }:
        return False
    policy_organization = _clean_optional_string(
        policy.get("organization_id") if policy else None
    )
    if mode == "personal":
        return policy_organization is None
    return bool(
        mode == "organization"
        and policy_organization
        and policy_organization == _clean_optional_string(ownership.get("organization_id"))
    )


def _resolve_authoritative_show_page_ownership(
    ownership: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a pending organization fence using the paired Permissions projection."""

    if ownership.get("mode") != "organization_pending":
        return dict(ownership)
    try:
        from vibe import permissions

        resolved = permissions.resolve_current_instance_ownership()
    except (
        AttributeError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return dict(ownership)
    if resolved.get("mode") == "organization":
        return dict(resolved)
    return dict(ownership)


def _show_page_authorization_ownership(
    connection: Connection | None,
    ownership: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the pairing fence before the authorization transaction reads state."""

    if ownership is not None:
        return _resolve_authoritative_show_page_ownership(dict(ownership))

    # With no caller-owned connection, finish the small persisted-fence read
    # before resolving the potentially slow authoritative organization lookup.
    if connection is None:
        return _resolve_authoritative_show_page_ownership(
            current_show_page_instance_ownership()
        )

    configured = _configured_show_page_instance()
    if configured is _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE:
        return _configuration_unavailable_ownership()
    if configured is None:
        return {
            "mode": "unmanaged",
            "instance_id": None,
            "organization_id": None,
            "source": "config",
        }
    instance_id, instance_kind = configured
    if instance_kind == "personal":
        return {
            "mode": "personal",
            "instance_id": instance_id,
            "organization_id": None,
            "source": "config",
        }

    # A supplied Connection may contain an uncommitted legacy policy. Resolve
    # the organization fence before its first SELECT so reconciliation can
    # safely write through that same transaction.
    provisional = {
        "mode": "organization_pending",
        "instance_id": instance_id,
        "organization_id": None,
        "source": "config",
    }
    resolved = _resolve_authoritative_show_page_ownership(provisional)
    if resolved.get("mode") == "organization":
        return resolved
    return current_show_page_instance_ownership(connection=connection)


def _show_page_authorization_snapshot(
    connection: Connection,
    resource_id: str,
    *,
    ownership: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """Load one policy after adopting a legacy null-organization shape when safe."""

    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    current_ownership = dict(
        ownership
        if ownership is not None
        else _show_page_authorization_ownership(connection)
    )
    policy = _policy_row(connection, "show_page", identifier)
    policy_organization = _clean_optional_string(
        policy.get("organization_id") if policy else None
    )
    if (
        policy is not None
        and policy_organization is None
        and current_ownership.get("mode") in {"organization", "organization_pending"}
    ):
        try:
            reconciliation = reconcile_show_page_resource_policy(
                connection,
                resource_id=identifier,
                ownership=current_ownership,
                owner_user_id=_clean_optional_string(policy.get("owner_user_id")),
                owner_email=_clean_optional_string(policy.get("owner_email"), limit=320),
            )
        except ResourceAccessError:
            return _configuration_unavailable_ownership(), policy, []
        current_ownership = dict(reconciliation["ownership"])
        policy = reconciliation["policy"]
    groups = _policy_groups(connection, "show_page", identifier) if policy else []
    return current_ownership, policy, groups


def _stored_resource_organizations(connection: Connection) -> set[str]:
    raw_value = connection.execute(
        select(state_meta.c.value_json).where(state_meta.c.key == RESOURCE_ORGANIZATIONS_META_KEY)
    ).scalar_one_or_none()
    try:
        values = json.loads(raw_value) if raw_value else []
    except (TypeError, ValueError):
        return set()
    if not isinstance(values, list):
        return set()
    return {
        organization
        for value in values
        if (organization := _clean_optional_string(value)) is not None
    }


def _store_resource_organizations(connection: Connection, organizations: set[str]) -> None:
    connection.execute(state_meta.delete().where(state_meta.c.key == RESOURCE_ORGANIZATIONS_META_KEY))
    if not organizations:
        return
    connection.execute(
        state_meta.insert().values(
            key=RESOURCE_ORGANIZATIONS_META_KEY,
            value_json=json.dumps(sorted(organizations), separators=(",", ":")),
            updated_at=_utc_now_iso(),
        )
    )


def remember_resource_organization(connection: Connection, organization_id: str | None) -> None:
    organization = _clean_optional_string(organization_id)
    if organization is None:
        return
    organizations = _stored_resource_organizations(connection)
    if organization in organizations:
        return
    organizations.add(organization)
    _store_resource_organizations(connection, organizations)


def forget_resource_organization(connection: Connection, organization_id: str) -> None:
    organization = _required_identifier(organization_id, code="invalid_organization_id")
    organizations = _stored_resource_organizations(connection)
    if organization not in organizations:
        return
    organizations.remove(organization)
    _store_resource_organizations(connection, organizations)


def list_resource_organization_ids(*, connection: Connection | None = None) -> list[str]:
    """List current and pending-empty-index organizations for device sync."""

    with _connection(connection) as conn:
        organizations = _stored_resource_organizations(conn)
        organizations.update(
            str(row.organization_id)
            for row in conn.execute(
                select(resource_access_policies.c.organization_id)
                .where(resource_access_policies.c.organization_id.is_not(None))
                .distinct()
            )
            if row.organization_id
        )
    return sorted(organizations)


def _policy_row(connection: Connection, resource_kind: str, resource_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        select(resource_access_policies)
        .where(resource_access_policies.c.resource_kind == resource_kind)
        .where(resource_access_policies.c.resource_id == resource_id)
        .limit(1)
    ).mappings().first()
    return dict(row) if row else None


def _policy_groups(connection: Connection, resource_kind: str, resource_id: str) -> list[str]:
    return [
        str(row["group_id"])
        for row in connection.execute(
            select(resource_access_groups.c.group_id)
            .where(resource_access_groups.c.resource_kind == resource_kind)
            .where(resource_access_groups.c.resource_id == resource_id)
            .order_by(resource_access_groups.c.group_id)
        ).mappings()
    ]


def _serialize_policy(connection: Connection, policy: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(policy)
    data["group_ids"] = _policy_groups(connection, str(data["resource_kind"]), str(data["resource_id"]))
    return data


def get_resource_policy(
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> dict[str, Any] | None:
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
        return _serialize_policy(conn, policy) if policy else None


def list_resource_policies(
    *,
    resource_kind: str | None = None,
    organization_id: str | None = None,
    owner_user_id: str | None = None,
    connection: Connection | None = None,
) -> list[dict[str, Any]]:
    kind = _validate_resource_kind(resource_kind) if resource_kind is not None else None
    organization = _clean_optional_string(organization_id) if organization_id is not None else None
    owner = _clean_optional_string(owner_user_id) if owner_user_id is not None else None
    statement = select(resource_access_policies).order_by(
        resource_access_policies.c.resource_kind,
        resource_access_policies.c.resource_id,
    )
    if kind is not None:
        statement = statement.where(resource_access_policies.c.resource_kind == kind)
    if organization is not None:
        statement = statement.where(resource_access_policies.c.organization_id == organization)
    if owner is not None:
        statement = statement.where(resource_access_policies.c.owner_user_id == owner)
    with _connection(connection) as conn:
        return [_serialize_policy(conn, row) for row in conn.execute(statement).mappings()]


def ensure_resource_policy(
    connection: Connection,
    *,
    resource_kind: str,
    resource_id: str,
    organization_id: str | None,
    owner_user_id: str | None,
    owner_email: str | None = None,
    access_level: str = "private",
    group_ids: Sequence[str] | None = None,
    created_by_user_id: str | None = None,
    updated_by_user_id: str | None = None,
    policy_revision: int = 0,
    last_applied_control_plane_revision: int | None = None,
) -> dict[str, Any]:
    """Create a resource policy if it does not already exist.

    Resource-specific services should use this at creation time. It never
    overwrites an existing policy, which prevents a late resource registration
    from replacing a control-plane intent already applied locally.
    """

    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    organization = _clean_optional_string(organization_id)
    owner = _clean_optional_string(owner_user_id)
    normalized_level, normalized_groups = _validate_policy_values(access_level, group_ids, organization)
    if policy_revision < 0 or last_applied_control_plane_revision is not None and last_applied_control_plane_revision < 0:
        raise ResourceAccessError("invalid_resource_acl_intent")
    existing = _policy_row(connection, kind, identifier)
    if existing:
        remember_resource_organization(connection, existing.get("organization_id"))
        return _serialize_policy(connection, existing)
    remember_resource_organization(connection, organization)
    now = _utc_now_iso()
    connection.execute(
        resource_access_policies.insert().values(
            resource_kind=kind,
            resource_id=identifier,
            organization_id=organization,
            owner_user_id=owner,
            owner_email=_clean_optional_string(owner_email, limit=320),
            access_level=normalized_level,
            created_by_user_id=_clean_optional_string(created_by_user_id),
            updated_by_user_id=_clean_optional_string(updated_by_user_id),
            policy_revision=policy_revision,
            last_applied_control_plane_revision=last_applied_control_plane_revision,
            created_at=now,
            updated_at=now,
        )
    )
    _replace_policy_groups(connection, kind, identifier, organization, normalized_groups, now)
    policy = _policy_row(connection, kind, identifier)
    assert policy is not None
    return _serialize_policy(connection, policy)


def delete_resource_policy(connection: Connection, resource_kind: str, resource_id: str) -> bool:
    """Delete local policy state when its local resource is permanently removed."""

    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    policy = _policy_row(connection, kind, identifier)
    if policy is not None:
        remember_resource_organization(connection, policy.get("organization_id"))
    connection.execute(
        resource_access_groups.delete()
        .where(resource_access_groups.c.resource_kind == kind)
        .where(resource_access_groups.c.resource_id == identifier)
    )
    result = connection.execute(
        resource_access_policies.delete()
        .where(resource_access_policies.c.resource_kind == kind)
        .where(resource_access_policies.c.resource_id == identifier)
    )
    return bool(result.rowcount)


def _replace_policy_groups(
    connection: Connection,
    resource_kind: str,
    resource_id: str,
    organization_id: str | None,
    group_ids: Sequence[str],
    now: str,
) -> None:
    connection.execute(
        resource_access_groups.delete()
        .where(resource_access_groups.c.resource_kind == resource_kind)
        .where(resource_access_groups.c.resource_id == resource_id)
    )
    if not group_ids:
        return
    if organization_id is None:
        raise ResourceAccessError("invalid_resource_acl_intent")
    connection.execute(
        resource_access_groups.insert(),
        [
            {
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "group_id": group_id,
                "organization_id": organization_id,
                "created_at": now,
            }
            for group_id in group_ids
        ],
    )


def _policy_allows(
    context: ResourceUserContext,
    resource_kind: str,
    resource_id: str,
    policy: Mapping[str, Any] | None,
    group_ids: Sequence[str],
    *,
    show_page_ownership: Mapping[str, Any] | None = None,
) -> bool:
    if context.is_instance_owner:
        return True
    # Skill and Vault ACL rows remain persisted for compatibility, but Editor
    # runtime access intentionally ignores them for validated remote sessions.
    # Direct non-remote contexts still use the stored policy so service-level
    # ACL checks cannot be bypassed by a caller-supplied role alone.
    if resource_kind in {"skill", "vault_secret"}:
        if context.is_remote and context.is_active_organization_member and context.has_role("editor"):
            return True
    # A signed Show Page email session carries an exact resource entitlement.
    # It remains valid even when the local instance pairing cannot be reloaded;
    # the broader instance/organization policy fence below must not revoke that
    # independent Limited-link flow.
    if resource_kind == "show_page" and context.can_use_show_page(resource_id):
        return True
    if (
        resource_kind == "show_page"
        and show_page_ownership is not None
        and show_page_ownership.get("mode")
        == SHOW_PAGE_OWNERSHIP_CONFIGURATION_UNAVAILABLE
    ):
        return False
    if (
        resource_kind == "show_page"
        and show_page_ownership is not None
        and not _show_page_policy_matches_instance_ownership(
            show_page_ownership,
            policy,
        )
    ):
        return False
    if context.instance_access_source == "show_page_email":
        return False
    if not context.can_use_resource(resource_kind):
        return False
    if policy is None:
        return False

    owner_user_id = _clean_optional_string(policy.get("owner_user_id"))
    if policy.get("access_level") == "private":
        if not (owner_user_id and context.subject and owner_user_id == context.subject):
            return False
        organization_id = _clean_optional_string(policy.get("organization_id"))
        return bool(
            not organization_id
            or context.is_active_organization_member and context.organization_id == organization_id
        )

    organization_id = _clean_optional_string(policy.get("organization_id"))
    if not organization_id or context.organization_id != organization_id or not context.is_active_organization_member:
        return False
    if policy.get("access_level") == "public":
        return True
    if policy.get("access_level") == "scope":
        # Missing and empty are intentionally distinct: a missing group claim
        # must fail closed, and an active member with no matching group also
        # cannot use a scoped resource.
        return bool(context.group_ids and set(group_ids).intersection(context.group_ids))
    return False


def _policy_allows_management(context: ResourceUserContext, policy: Mapping[str, Any] | None) -> bool:
    if context.is_instance_owner:
        return True
    if policy is None:
        return False
    # Skills and Vault ACL rows are retained for compatibility, but MVP runtime
    # access and management follow the Instance Editor capability directly.
    if policy.get("resource_kind") in {"skill", "vault_secret"}:
        return context.has_role("editor")
    return bool(
        context.has_role("editor")
        and context.subject
        and context.subject == _clean_optional_string(policy.get("owner_user_id"))
    )


def _policy_allows_owner_control(
    context: ResourceUserContext,
    policy: Mapping[str, Any] | None,
) -> bool:
    """Allow high-impact sharing actions only to the resource owner."""

    if context.is_instance_owner:
        return True
    if policy is None:
        return context.is_instance_owner
    if not context.can_use_resource(str(policy.get("resource_kind") or "")):
        return False
    owner_user_id = _clean_optional_string(policy.get("owner_user_id"))
    if not (owner_user_id and context.subject and owner_user_id == context.subject):
        return False
    organization_id = _clean_optional_string(policy.get("organization_id"))
    return bool(
        not organization_id
        or context.is_active_organization_member
        and context.organization_id == organization_id
    )


def _policy_allows_show_page_access_management(
    context: ResourceUserContext,
    policy: Mapping[str, Any] | None,
) -> bool:
    """Allow the Instance Owner or page owner represented by the ACL."""
    if _policy_allows_owner_control(context, policy):
        return True
    if policy is None or policy.get("resource_kind") != "show_page":
        return False
    return _policy_allows_management(context, policy)


def can_use_resource_policy_snapshot(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None,
) -> bool:
    """Evaluate a server-owned policy snapshot for immutable history."""

    context = _as_context(user_context)
    if policy is not None and not isinstance(policy, Mapping):
        return False
    resource_kind = policy.get("resource_kind") if policy is not None else None
    if resource_kind not in RESOURCE_KINDS:
        return False
    group_ids = policy.get("group_ids") if policy is not None else []
    if not isinstance(group_ids, (list, tuple, set, frozenset)):
        return False
    return _policy_allows(
        context,
        str(resource_kind),
        str(policy.get("resource_id") or ""),
        policy,
        [str(group_id) for group_id in group_ids],
    )


def can_manage_resource_policy_snapshot(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None,
) -> bool:
    """Evaluate management access from a server-owned policy snapshot."""

    if policy is not None and not isinstance(policy, Mapping):
        return False
    return _policy_allows_management(_as_context(user_context), policy)


def can_use_resource(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> bool:
    context = _as_context(user_context)
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    show_page_ownership = (
        _show_page_authorization_ownership(connection)
        if kind == "show_page"
        else None
    )
    with _connection(connection) as conn:
        if kind == "show_page":
            ownership, policy, groups = _show_page_authorization_snapshot(
                conn,
                identifier,
                ownership=show_page_ownership,
            )
        else:
            policy = _policy_row(conn, kind, identifier)
            groups = _policy_groups(conn, kind, identifier) if policy else []
            ownership = None
        return _policy_allows(
            context,
            kind,
            identifier,
            policy,
            groups,
            show_page_ownership=ownership,
        )


def can_manage_resource_acl(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> bool:
    context = _as_context(user_context)
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    show_page_ownership = (
        _show_page_authorization_ownership(connection)
        if kind == "show_page"
        else None
    )
    with _connection(connection) as conn:
        if kind == "show_page":
            ownership, policy, _groups = _show_page_authorization_snapshot(
                conn,
                identifier,
                ownership=show_page_ownership,
            )
        else:
            ownership = None
            policy = _policy_row(conn, kind, identifier)
        if (
            kind == "show_page"
            and not context.is_instance_owner
            and not _show_page_policy_matches_instance_ownership(
                ownership or {},
                policy,
            )
        ):
            return False
    return _policy_allows_management(context, policy)


def can_manage_show_page_access(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> bool:
    """Return whether the caller may manage a Show Page audience or narrow sharing."""

    context = _as_context(user_context)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    show_page_ownership = _show_page_authorization_ownership(connection)
    with _connection(connection) as conn:
        ownership, policy, _groups = _show_page_authorization_snapshot(
            conn,
            identifier,
            ownership=show_page_ownership,
        )
        if (
            not context.is_instance_owner
            and not _show_page_policy_matches_instance_ownership(
                ownership,
                policy,
            )
        ):
            return False
    return _policy_allows_show_page_access_management(context, policy)


def can_control_resource_sharing(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> bool:
    """Return whether the resource owner may widen anonymous sharing."""

    context = _as_context(user_context)
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    show_page_ownership = (
        _show_page_authorization_ownership(connection)
        if kind == "show_page"
        else None
    )
    with _connection(connection) as conn:
        if kind == "show_page":
            ownership, policy, _groups = _show_page_authorization_snapshot(
                conn,
                identifier,
                ownership=show_page_ownership,
            )
        else:
            ownership = None
            policy = _policy_row(conn, kind, identifier)
        if (
            kind == "show_page"
            and not context.is_instance_owner
            and not _show_page_policy_matches_instance_ownership(
                ownership or {},
                policy,
            )
        ):
            return False
    return _policy_allows_owner_control(context, policy)


def _row_resource_id(row: Any) -> str | None:
    keys = ("resource_id", "id", "session_id", "name", "key")
    if isinstance(row, Mapping):
        for key in keys:
            value = _clean_optional_string(row.get(key))
            if value is not None:
                return value
        return None
    for key in keys:
        value = _clean_optional_string(getattr(row, key, None))
        if value is not None:
            return value
    return None


def filter_accessible_resources(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_kind: str,
    rows: Sequence[Any],
    *,
    connection: Connection | None = None,
) -> list[Any]:
    """Filter generic resource rows without coupling to any resource domain.

    Rows may be mappings or objects and should expose one of `resource_id`,
    `id`, `session_id`, `name`, or `key`. Unknown rows are excluded rather than
    accidentally exposed.
    """

    context = _as_context(user_context)
    kind = _validate_resource_kind(resource_kind)
    candidates = [(row, _row_resource_id(row)) for row in rows]
    identifiers = [identifier for _, identifier in candidates if identifier is not None]
    if not identifiers:
        return []
    ownership = (
        _show_page_authorization_ownership(connection)
        if kind == "show_page"
        else None
    )
    with _connection(connection) as conn:
        policy_rows = conn.execute(
            select(resource_access_policies)
            .where(resource_access_policies.c.resource_kind == kind)
            .where(resource_access_policies.c.resource_id.in_(identifiers))
        ).mappings()
        policies = {str(row["resource_id"]): dict(row) for row in policy_rows}
        groups = {
            identifier: _policy_groups(conn, kind, identifier)
            for identifier in policies
        }
        if kind == "show_page":
            if ownership and ownership.get("mode") == "organization":
                for identifier, policy in list(policies.items()):
                    if _clean_optional_string(policy.get("organization_id")) is not None:
                        continue
                    try:
                        reconciliation = reconcile_show_page_resource_policy(
                            conn,
                            resource_id=identifier,
                            ownership=ownership,
                            owner_user_id=_clean_optional_string(policy.get("owner_user_id")),
                            owner_email=_clean_optional_string(policy.get("owner_email"), limit=320),
                        )
                    except ResourceAccessError:
                        ownership = _configuration_unavailable_ownership()
                        break
                    policies[identifier] = reconciliation["policy"] or policy
                    groups[identifier] = _policy_groups(conn, kind, identifier)
    return [
        row
        for row, identifier in candidates
        if identifier is not None
        and _policy_allows(
            context,
            kind,
            identifier,
            policies.get(identifier),
            groups.get(identifier, []),
            show_page_ownership=ownership,
        )
    ]


def resource_policy_narrowed(
    previous: Mapping[str, Any] | None,
    updated: Mapping[str, Any] | None,
) -> bool:
    """Return whether an applied policy removes any previously granted use."""

    if not previous or not updated:
        return False
    previous_level = str(previous.get("access_level") or "")
    updated_level = str(updated.get("access_level") or "")
    if previous_level == "public":
        return updated_level in {"scope", "private"}
    if previous_level == "private":
        return updated_level == "scope"
    if previous_level != "scope":
        return False
    if updated_level == "private":
        return True
    if updated_level != "scope":
        return False
    previous_groups = {
        str(group_id)
        for group_id in previous.get("group_ids") or []
        if isinstance(group_id, str)
    }
    updated_groups = {
        str(group_id)
        for group_id in updated.get("group_ids") or []
        if isinstance(group_id, str)
    }
    return not previous_groups.issubset(updated_groups)


def apply_control_plane_intent(
    connection: Connection,
    *,
    organization_id: str,
    resource_kind: str,
    resource_id: str,
    revision: int,
    access_level: str,
    group_ids: Sequence[str],
    updated_by_user_id: str = "control_plane",
) -> dict[str, Any]:
    """Atomically apply a newer hosted ACL intent to one local policy.

    The caller owns the surrounding transaction. A stale or already-applied
    revision never rewrites the current policy, which makes retrying a device
    poll safe.
    """

    organization = _required_identifier(organization_id, code="invalid_organization_id")
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ResourceAccessError("invalid_resource_acl_intent")
    normalized_level, normalized_groups = _validate_policy_values(access_level, group_ids, organization)
    policy = _policy_row(connection, kind, identifier)
    if policy is None:
        raise ResourceAccessError("resource_not_found")
    if _clean_optional_string(policy.get("organization_id")) != organization:
        raise ResourceAccessError("resource_organization_mismatch")

    last_applied = policy.get("last_applied_control_plane_revision")
    last_revision = int(last_applied) if isinstance(last_applied, int) else -1
    policy_revision = int(policy.get("policy_revision") or 0)
    if revision < last_revision or revision < policy_revision:
        return {"status": "stale", "policy": _serialize_policy(connection, policy)}
    if revision == last_revision:
        return {"status": "already_applied", "policy": _serialize_policy(connection, policy)}

    if normalized_groups:
        conflicting_group = connection.execute(
            select(resource_access_groups.c.group_id)
            .where(resource_access_groups.c.group_id.in_(normalized_groups))
            .where(resource_access_groups.c.organization_id != organization)
            .limit(1)
        ).first()
        if conflicting_group is not None:
            raise ResourceAccessError("resource_group_organization_mismatch")

    now = _utc_now_iso()
    connection.execute(
        resource_access_policies.update()
        .where(resource_access_policies.c.resource_kind == kind)
        .where(resource_access_policies.c.resource_id == identifier)
        .values(
            access_level=normalized_level,
            policy_revision=revision,
            last_applied_control_plane_revision=revision,
            updated_by_user_id=_clean_optional_string(updated_by_user_id) or "control_plane",
            updated_at=now,
        )
    )
    _replace_policy_groups(connection, kind, identifier, organization, normalized_groups, now)
    updated = _policy_row(connection, kind, identifier)
    assert updated is not None
    return {"status": "applied", "policy": _serialize_policy(connection, updated)}


def update_local_non_organization_policy(
    connection: Connection,
    *,
    resource_kind: str,
    resource_id: str,
    access_level: str,
    group_ids: Sequence[str] | None,
    updated_by_user_id: str | None,
) -> dict[str, Any]:
    """Update a personal/local policy without touching control-plane revisions."""

    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    policy = _policy_row(connection, kind, identifier)
    if policy is None:
        raise ResourceAccessError("resource_not_found")
    organization = _clean_optional_string(policy.get("organization_id"))
    if organization is not None:
        raise ResourceAccessError("resource_acl_control_plane_required")
    normalized_level, normalized_groups = _validate_policy_values(access_level, group_ids, None)
    now = _utc_now_iso()
    next_revision = int(policy.get("policy_revision") or 0) + 1
    connection.execute(
        resource_access_policies.update()
        .where(resource_access_policies.c.resource_kind == kind)
        .where(resource_access_policies.c.resource_id == identifier)
        .values(
            access_level=normalized_level,
            policy_revision=next_revision,
            updated_by_user_id=_clean_optional_string(updated_by_user_id),
            updated_at=now,
        )
    )
    _replace_policy_groups(connection, kind, identifier, None, normalized_groups, now)
    updated = _policy_row(connection, kind, identifier)
    assert updated is not None
    return _serialize_policy(connection, updated)
