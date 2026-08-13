"""Applied Project access policy storage and centralized role evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from storage.models import (
    agent_sessions,
    project_access_bindings,
    project_access_policies,
    scope_settings,
    scopes,
)
from vibe.authorization import AuthorizationContext

PROJECT_SCOPE_PREFIX = "avibe::project::"
PROJECT_ACCESS_MODES = frozenset({"inherit", "restricted"})
PROJECT_ACCESS_ROLES = frozenset({"editor", "viewer"})
PROJECT_PRINCIPAL_KINDS = frozenset({"email", "email_domain", "organization_group"})
MAX_CONTROL_PLANE_REVISION = (1 << 53) - 1
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
_EMAIL_RE = re.compile(
    r"^[a-z0-9._%+-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$"
)
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$"
)


class ProjectAccessIntentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProjectAccessIntentResult:
    project_id: str
    revision: int
    outcome: str
    error_code: str | None = None
    changed: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_scope_id(project_id: str) -> str:
    return f"{PROJECT_SCOPE_PREFIX}{project_id}"


def project_id_from_scope_id(scope_id: Any) -> str | None:
    if not isinstance(scope_id, str) or not scope_id.startswith(PROJECT_SCOPE_PREFIX):
        return None
    project_id = scope_id[len(PROJECT_SCOPE_PREFIX) :].strip()
    return project_id or None


def _safe_string(value: Any, *, code: str, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise ProjectAccessIntentError(code)
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)
    ):
        raise ProjectAccessIntentError(code)
    return cleaned


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectAccessIntentError("invalid_project_access_revision")
    if value < 0 or value > MAX_CONTROL_PLANE_REVISION:
        raise ProjectAccessIntentError("invalid_project_access_revision")
    return value


def _normalize_binding(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ProjectAccessIntentError("invalid_project_access_binding")
    if set(raw) != {"principal_kind", "principal_value", "access_role"}:
        raise ProjectAccessIntentError("invalid_project_access_binding")
    kind = _safe_string(raw.get("principal_kind"), code="invalid_project_access_binding")
    if kind not in PROJECT_PRINCIPAL_KINDS:
        raise ProjectAccessIntentError("invalid_project_access_binding")
    role = _safe_string(raw.get("access_role"), code="invalid_project_access_binding")
    if role not in PROJECT_ACCESS_ROLES:
        raise ProjectAccessIntentError("invalid_project_access_binding")
    value = _safe_string(
        raw.get("principal_value"),
        code="invalid_project_access_binding",
        limit=320,
    )
    if kind != "organization_group":
        value = value.lower()
    if kind == "email" and not _EMAIL_RE.fullmatch(value):
        raise ProjectAccessIntentError("invalid_project_access_email")
    if kind == "email_domain" and not _DOMAIN_RE.fullmatch(value):
        raise ProjectAccessIntentError("invalid_project_access_domain")
    if kind == "organization_group" and any(char in value for char in ("/", "\\")):
        raise ProjectAccessIntentError("invalid_project_access_group")
    return {"principal_kind": kind, "principal_value": value, "access_role": role}


def validate_project_access_intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, Mapping):
        raise ProjectAccessIntentError("invalid_project_access_intent")
    if set(intent) - {"project_id", "revision", "mode", "bindings", "organization_id"}:
        raise ProjectAccessIntentError("invalid_project_access_intent")
    project_id = _safe_string(intent.get("project_id"), code="invalid_project_id", limit=200)
    if any(char in project_id for char in ("/", "\\")):
        raise ProjectAccessIntentError("invalid_project_id")
    revision = _revision(intent.get("revision"))
    mode = _safe_string(intent.get("mode"), code="invalid_project_access_mode")
    if mode not in PROJECT_ACCESS_MODES:
        raise ProjectAccessIntentError("invalid_project_access_mode")
    raw_bindings = intent.get("bindings")
    if not isinstance(raw_bindings, list) or len(raw_bindings) > 512:
        raise ProjectAccessIntentError("invalid_project_access_bindings")
    bindings = [_normalize_binding(binding) for binding in raw_bindings]
    if mode == "inherit" and bindings:
        raise ProjectAccessIntentError("invalid_project_access_bindings")
    keys = {(item["principal_kind"], item["principal_value"]) for item in bindings}
    if len(keys) != len(bindings):
        raise ProjectAccessIntentError("duplicate_project_access_principal")
    organization_id = intent.get("organization_id")
    if organization_id is not None:
        organization_id = _safe_string(
            organization_id,
            code="invalid_project_access_organization",
            limit=200,
        )
    return {
        "project_id": project_id,
        "revision": revision,
        "mode": mode,
        "bindings": bindings,
        "organization_id": organization_id,
    }


def _active_project_scope(conn: Connection, project_id: str) -> str | None:
    scope_id = project_scope_id(project_id)
    row = conn.execute(
        select(scopes.c.id, scope_settings.c.enabled)
        .select_from(scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id))
        .where(
            scopes.c.id == scope_id,
            scopes.c.platform == "avibe",
            scopes.c.scope_type == "project",
        )
        .limit(1)
    ).first()
    if row is None or row.enabled == 0:
        return None
    return str(row.id)


def is_active_project(conn: Connection, project_id: str) -> bool:
    return _active_project_scope(conn, project_id) is not None


def apply_project_access_intent(
    conn: Connection,
    intent: Any,
    *,
    now: str | None = None,
) -> ProjectAccessIntentResult:
    """Atomically apply a newer validated intent or classify a replay/stale intent."""

    try:
        normalized = validate_project_access_intent(intent)
    except ProjectAccessIntentError as error:
        project_id = str(intent.get("project_id") or "") if isinstance(intent, Mapping) else ""
        revision = intent.get("revision", 0) if isinstance(intent, Mapping) else 0
        return ProjectAccessIntentResult(
            project_id=project_id,
            revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else 0,
            outcome="rejected",
            error_code=error.code,
        )

    project_id = normalized["project_id"]
    revision = normalized["revision"]
    scope_id = _active_project_scope(conn, project_id)
    if scope_id is None:
        return ProjectAccessIntentResult(
            project_id=project_id,
            revision=revision,
            outcome="rejected",
            error_code="project_not_found",
        )

    current = conn.execute(
        select(project_access_policies).where(project_access_policies.c.project_id == project_id)
    ).mappings().first()
    applied_revision = int(current["last_applied_control_plane_revision"] or 0) if current else 0
    if revision < applied_revision:
        return ProjectAccessIntentResult(
            project_id=project_id,
            revision=revision,
            outcome="stale",
        )
    if revision == applied_revision and current is not None:
        return ProjectAccessIntentResult(
            project_id=project_id,
            revision=revision,
            outcome="applied",
        )

    timestamp = now or _utc_now_iso()
    policy_revision = (int(current["policy_revision"] or 0) if current else 0) + 1
    statement = sqlite_insert(project_access_policies).values(
        project_id=project_id,
        scope_id=scope_id,
        organization_id=normalized["organization_id"],
        mode=normalized["mode"],
        policy_revision=policy_revision,
        last_applied_control_plane_revision=revision,
        created_at=current["created_at"] if current else timestamp,
        updated_at=timestamp,
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[project_access_policies.c.project_id],
            set_={
                "scope_id": scope_id,
                "organization_id": normalized["organization_id"],
                "mode": normalized["mode"],
                "policy_revision": policy_revision,
                "last_applied_control_plane_revision": revision,
                "updated_at": timestamp,
            },
        )
    )
    conn.execute(
        delete(project_access_bindings).where(project_access_bindings.c.project_id == project_id)
    )
    if normalized["bindings"]:
        conn.execute(
            insert(project_access_bindings),
            [
                {"project_id": project_id, **binding, "created_at": timestamp}
                for binding in normalized["bindings"]
            ],
        )
    return ProjectAccessIntentResult(
        project_id=project_id,
        revision=revision,
        outcome="applied",
        changed=True,
    )


def get_project_policy(conn: Connection, project_id: str) -> dict[str, Any] | None:
    policy = conn.execute(
        select(project_access_policies).where(project_access_policies.c.project_id == project_id)
    ).mappings().first()
    if policy is None:
        return None
    bindings = conn.execute(
        select(project_access_bindings)
        .where(project_access_bindings.c.project_id == project_id)
        .order_by(
            project_access_bindings.c.principal_kind,
            project_access_bindings.c.principal_value,
        )
    ).mappings().all()
    return {**dict(policy), "bindings": [dict(binding) for binding in bindings]}


def _matching_binding_role(
    context: AuthorizationContext,
    policy: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]],
) -> str | None:
    email = (context.email or "").strip().lower()
    domain = email.rsplit("@", 1)[1] if "@" in email else ""
    policy_organization_id = str(policy.get("organization_id") or "").strip()
    roles: list[str] = []
    for binding in bindings:
        kind = str(binding.get("principal_kind") or "")
        value = str(binding.get("principal_value") or "")
        if kind == "email" and email and value == email:
            roles.append(str(binding.get("access_role") or ""))
        elif kind == "email_domain" and domain and value == domain:
            roles.append(str(binding.get("access_role") or ""))
        elif (
            kind == "organization_group"
            and value in context.group_ids
            and (not policy_organization_id or policy_organization_id == context.organization_id)
        ):
            roles.append(str(binding.get("access_role") or ""))
    valid_roles = [role for role in roles if role in PROJECT_ACCESS_ROLES]
    return max(valid_roles, key=_ROLE_RANK.__getitem__) if valid_roles else None


def get_effective_project_role(
    conn: Connection,
    context: AuthorizationContext,
    project_id: str,
) -> str | None:
    if context.is_instance_owner:
        return "owner"
    if not is_active_project(conn, project_id):
        return None
    instance_role = context.instance_role
    if instance_role not in _ROLE_RANK:
        return None
    policy = get_project_policy(conn, project_id)
    if policy is None or policy["mode"] == "inherit":
        return instance_role
    binding_role = _matching_binding_role(context, policy, policy["bindings"])
    if binding_role is None:
        return None
    return min((instance_role, binding_role), key=_ROLE_RANK.__getitem__)


def role_allows(role: str | None, minimum_role: str) -> bool:
    return _ROLE_RANK.get(role or "", 0) >= _ROLE_RANK.get(minimum_role, 1 << 30)


def can_read_project(conn: Connection, context: AuthorizationContext, project_id: str) -> bool:
    return role_allows(get_effective_project_role(conn, context, project_id), "viewer")


def can_chat_project(conn: Connection, context: AuthorizationContext, project_id: str) -> bool:
    return role_allows(get_effective_project_role(conn, context, project_id), "editor")


def can_manage_project(conn: Connection, context: AuthorizationContext, project_id: str) -> bool:
    return get_effective_project_role(conn, context, project_id) == "owner"


def filter_accessible_projects(
    conn: Connection,
    context: AuthorizationContext,
    projects: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(project)
        for project in projects
        if can_read_project(conn, context, str(project.get("id") or ""))
    ]


def get_effective_session_role(
    conn: Connection,
    context: AuthorizationContext,
    session_id: str,
) -> str | None:
    scope_id = conn.execute(
        select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id).limit(1)
    ).scalar_one_or_none()
    project_id = project_id_from_scope_id(scope_id)
    if project_id is None:
        return "owner" if context.is_instance_owner else None
    return get_effective_project_role(conn, context, project_id)


def accessible_project_ids(
    conn: Connection,
    context: AuthorizationContext,
    *,
    include_archived: bool = False,
) -> set[str]:
    query = (
        select(scopes.c.native_id, scope_settings.c.enabled)
        .select_from(scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id))
        .where(scopes.c.platform == "avibe", scopes.c.scope_type == "project")
    )
    return {
        str(row.native_id)
        for row in conn.execute(query)
        if (include_archived or row.enabled is None or row.enabled != 0)
        and can_read_project(conn, context, str(row.native_id))
    }
