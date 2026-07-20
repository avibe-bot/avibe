"""Local organization resource-policy state and evaluation helpers.

The control plane owns desired ACL intents. This module owns the local applied
policy used by future resource services, and deliberately stores no resource
content, prompts, paths, outputs, or secret values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage.db import get_cached_sqlite_engine
from storage.models import resource_access_groups, resource_access_policies


RESOURCE_KINDS = frozenset({"agent", "vault_secret", "skill", "show_page"})
ACCESS_LEVELS = frozenset({"public", "scope", "private"})
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})


class ResourceAccessError(ValueError):
    """A stable, non-sensitive policy or intent validation error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResourceUserContext:
    subject: str | None = None
    email: str | None = None
    organization_id: str | None = None
    organization_member_id: str | None = None
    organization_role: str | None = None
    group_ids: frozenset[str] | None = None
    membership_version: str | None = None
    instance_access_source: str | None = None
    is_remote: bool = False
    is_trusted_local: bool = False

    @property
    def is_active_organization_member(self) -> bool:
        return bool(
            self.organization_id
            and self.organization_member_id
            and self.organization_role in ORGANIZATION_ROLES
        )

    @property
    def is_instance_owner(self) -> bool:
        return self.instance_access_source == "owner" and bool(self.subject)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_optional_string(value: Any, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit or any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return None
    return cleaned


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
    data = payload or {}
    organization_id = _clean_optional_string(data.get("vibe_organization_id", data.get("organization_id")))
    raw_groups = data.get("vibe_group_ids", data.get("group_ids"))
    group_ids: frozenset[str] | None = None
    if organization_id and isinstance(raw_groups, (list, tuple, set, frozenset)):
        cleaned_groups = [_clean_optional_string(value) for value in raw_groups]
        if all(value is not None for value in cleaned_groups):
            group_ids = frozenset(value for value in cleaned_groups if value is not None)
    role = _clean_optional_string(data.get("vibe_organization_role", data.get("organization_role")))
    if role not in ORGANIZATION_ROLES:
        role = None
    return ResourceUserContext(
        subject=_clean_optional_string(data.get("sub")),
        email=_clean_optional_string(data.get("email"), limit=320),
        organization_id=organization_id,
        organization_member_id=_clean_optional_string(
            data.get("vibe_organization_member_id", data.get("organization_member_id"))
        ),
        organization_role=role,
        group_ids=group_ids,
        membership_version=_clean_optional_string(
            data.get("vibe_membership_version", data.get("membership_version"))
        ),
        instance_access_source=_clean_optional_string(
            data.get("vibe_instance_access_source", data.get("instance_access_source"))
        ),
        is_remote=is_remote,
        is_trusted_local=is_trusted_local,
    )


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

    if session_payload is not None or is_remote is not None or is_trusted_local is not None:
        return _context_from_mapping(
            session_payload,
            is_remote=bool(is_remote if is_remote is not None else session_payload is not None),
            is_trusted_local=bool(is_trusted_local),
        )

    try:
        from vibe import remote_access, ui_server

        config = ui_server._load_remote_access_config()
        remote_request = bool(
            config is not None
            and ui_server._is_remote_access_request(config)
            and not ui_server._is_local_request(config)
        )
        if remote_request and config is not None:
            payload = remote_access.parse_session_cookie(
                config,
                ui_server.request.cookies.get(remote_access.SESSION_COOKIE_NAME),
            )
            return _context_from_mapping(payload, is_remote=True, is_trusted_local=False)
        return _context_from_mapping(
            None,
            is_remote=False,
            is_trusted_local=bool(ui_server._is_local_request(config)),
        )
    except Exception:
        return ResourceUserContext()


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
        return _serialize_policy(connection, existing)
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


def _policy_allows(context: ResourceUserContext, policy: Mapping[str, Any] | None, group_ids: Sequence[str]) -> bool:
    if context.is_trusted_local:
        return True
    if policy is None:
        # A legacy no-policy resource is local-private. The paired instance's
        # owner retains legacy access, while remote organization members do not.
        return context.is_instance_owner

    owner_user_id = _clean_optional_string(policy.get("owner_user_id"))
    if policy.get("access_level") == "private":
        return bool(owner_user_id and context.subject and owner_user_id == context.subject)

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
        policy = _policy_row(conn, kind, identifier)
        groups = _policy_groups(conn, kind, identifier) if policy else []
        return _policy_allows(context, policy, groups)


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
    if policy is None:
        return context.is_instance_owner
    owner_user_id = _clean_optional_string(policy.get("owner_user_id"))
    if owner_user_id and context.subject and owner_user_id == context.subject:
        return True
    return bool(
        context.is_active_organization_member
        and context.organization_id == _clean_optional_string(policy.get("organization_id"))
        and context.organization_role in {"owner", "admin"}
    )


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
        if identifier is not None and _policy_allows(context, policies.get(identifier), groups.get(identifier, []))
    ]


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
