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

from storage.db import get_cached_sqlite_engine
from storage.models import resource_access_groups, resource_access_policies, state_meta
from vibe.authorization import (
    AuthorizationContext,
    context_from_session_payload,
    trusted_local_context,
)


RESOURCE_KINDS = frozenset(
    {"agent", "vault_secret", "skill", "show_page", "harness_task", "harness_watch"}
)
ACCESS_LEVELS = frozenset({"public", "scope", "private"})
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
RESOURCE_USER_CONTEXT_METADATA_KEY = "resource_user_context"
RESOURCE_ORGANIZATIONS_META_KEY = "resource_access_organizations"


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
    is_trusted_local: bool,
) -> ResourceUserContext:
    if is_trusted_local:
        return trusted_local_context()
    if not is_remote:
        return AuthorizationContext()
    return context_from_session_payload(payload or {})


def current_resource_context(
    session_payload: Mapping[str, Any] | None = None,
    *,
    is_remote: bool | None = None,
    is_trusted_local: bool | None = None,
) -> ResourceUserContext:
    """Return the request's local resource-access context.

    Callers that already parsed the signed session should pass it explicitly.
    The no-argument form is intentionally best-effort for future service-layer
    callers running inside the UI request context; outside a request it returns
    an untrusted anonymous context rather than guessing at an identity.
    """

    if isinstance(session_payload, AuthorizationContext):
        return session_payload
    if session_payload is not None or is_remote is not None or is_trusted_local is not None:
        return _context_from_mapping(
            session_payload,
            is_remote=bool(is_remote if is_remote is not None else session_payload is not None),
            is_trusted_local=bool(is_trusted_local),
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
    """Resolve ACL context, trusting implicit local use only outside HTTP."""

    if isinstance(user_context, AuthorizationContext):
        return user_context
    if isinstance(user_context, Mapping):
        return current_resource_context(user_context, is_remote=True)

    context = current_resource_context()
    if context.is_remote or context.is_trusted_local:
        return context

    from vibe.ui_compat import has_request_context

    if has_request_context():
        return context
    try:
        from core.caller_context import (
            AVIBE_HARNESS_AUTHORIZATION_ENV,
            AVIBE_RUN_ID_ENV,
            authorization_principal_from_env,
            caller_context_environment,
        )

        from storage import harness_authorization_service

        caller_env = caller_context_environment()
        principal = authorization_principal_from_env(caller_env)
        if (
            str(caller_env.get(AVIBE_RUN_ID_ENV) or "").strip()
            and caller_env.get(AVIBE_HARNESS_AUTHORIZATION_ENV) == "1"
        ):
            return harness_authorization_service.execution_context(
                str(caller_env[AVIBE_RUN_ID_ENV])
            )
        if principal is not None:
            return harness_authorization_service.current_principal_context(principal)
    except Exception:
        # A malformed, stale, or revoked execution principal must not turn an
        # Agent subprocess into a trusted-local resource caller.
        return AuthorizationContext(is_remote=True)
    return trusted_local_context()


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
    """Restore a remote definition creator context, or None for local legacy work."""

    if not isinstance(metadata, Mapping):
        return None
    snapshot = metadata.get(RESOURCE_USER_CONTEXT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return None
    expires_at = _clean_positive_int(snapshot.get("authorization_expires_at"))
    if expires_at is None or expires_at <= (now if now is not None else int(time.time())):
        raise ResourceAccessError("resource_authorization_expired")
    return current_resource_context(snapshot, is_remote=True, is_trusted_local=False)


def _as_context(user_context: ResourceUserContext | Mapping[str, Any] | None) -> ResourceUserContext:
    if isinstance(user_context, ResourceUserContext):
        return user_context
    if isinstance(user_context, Mapping):
        return _context_from_mapping(user_context, is_remote=True, is_trusted_local=False)
    return ResourceUserContext()


@contextmanager
def _connection(connection: Connection | None) -> Iterator[Connection]:
    if connection is not None:
        yield connection
        return
    engine = get_cached_sqlite_engine()
    with engine.connect() as active_connection:
        yield active_connection


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
    from storage import harness_authorization_service

    harness_authorization_service.revoke_resource_access_in_connection(
        connection,
        kind,
        identifier,
        unconditional=True,
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


def _policy_matches(
    context: ResourceUserContext,
    policy: Mapping[str, Any] | None,
    group_ids: Sequence[str],
) -> bool:
    if context.is_trusted_local:
        return True
    if policy is None:
        # A legacy no-policy resource is local-private. The paired instance's
        # owner retains legacy access, while remote organization members do not.
        return context.is_instance_owner

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


def _policy_allows(
    context: ResourceUserContext,
    resource_kind: str,
    policy: Mapping[str, Any] | None,
    group_ids: Sequence[str],
) -> bool:
    if not context.can_use_resource(resource_kind):
        return context.is_trusted_local
    return _policy_matches(context, policy, group_ids)


def _policy_allows_management(context: ResourceUserContext, policy: Mapping[str, Any] | None) -> bool:
    if context.is_trusted_local:
        return True
    if not context.has_role("owner"):
        return False
    if policy is None:
        return True
    owner_user_id = _clean_optional_string(policy.get("owner_user_id"))
    if owner_user_id and context.subject and owner_user_id == context.subject:
        organization_id = _clean_optional_string(policy.get("organization_id"))
        return bool(
            not organization_id
            or context.is_active_organization_member
            and context.organization_id == organization_id
        )
    return bool(
        context.is_active_organization_member
        and context.organization_id == _clean_optional_string(policy.get("organization_id"))
        and context.organization_role in {"owner", "admin"}
    )


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
        return context.is_trusted_local
    group_ids = policy.get("group_ids") if policy is not None else []
    if not isinstance(group_ids, (list, tuple, set, frozenset)):
        return False
    return _policy_allows(
        context,
        str(resource_kind),
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
    with _connection(connection) as conn:
        from storage import harness_authorization_service

        policy = _policy_row(conn, kind, identifier)
        groups = _policy_groups(conn, kind, identifier) if policy else []
        allowed = _policy_allows(context, kind, policy, groups)
        if allowed:
            harness_authorization_service.record_current_run_dependency(
                kind,
                identifier,
                connection=conn,
            )
        return allowed


def can_match_resource_acl(
    user_context: ResourceUserContext | Mapping[str, Any] | None,
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> bool:
    """Evaluate ACL membership without assigning a resource-use capability.

    Harness applies its own viewer/editor operation matrix to Task and Watch
    definitions. Agent, Skill, Vault, and Show Page use must continue through
    :func:`can_use_resource`, which also consumes the #1055 capability split.
    """

    context = _as_context(user_context)
    kind = _validate_resource_kind(resource_kind)
    identifier = _required_identifier(resource_id, code="invalid_resource_id")
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
        groups = _policy_groups(conn, kind, identifier) if policy else []
        return _policy_matches(context, policy, groups)


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
    if context.is_trusted_local:
        return True
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
    return _policy_allows_management(context, policy)


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
    return [
        row
        for row, identifier in candidates
        if identifier is not None
        and _policy_allows(context, kind, policies.get(identifier), groups.get(identifier, []))
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
    previous_policy = _serialize_policy(connection, policy)

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
    serialized = _serialize_policy(connection, updated)
    if resource_policy_narrowed(previous_policy, serialized):
        from storage import harness_authorization_service

        harness_authorization_service.revoke_resource_access_in_connection(
            connection,
            kind,
            identifier,
        )
    return {"status": "applied", "policy": serialized}


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
    previous_policy = _serialize_policy(connection, policy)
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
    serialized = _serialize_policy(connection, updated)
    if resource_policy_narrowed(previous_policy, serialized):
        from storage import harness_authorization_service

        harness_authorization_service.revoke_resource_access_in_connection(
            connection,
            kind,
            identifier,
        )
    return serialized
