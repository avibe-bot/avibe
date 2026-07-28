"""Organization-aware authorization for Harness definitions and Runs.

Raw background rows are intentionally not browser projections. This module is
the shared boundary for Task/Watch operations, Run reads, deferred execution,
dynamic resource use, output classification, and revocation.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from core.caller_context import AVIBE_HARNESS_AUTHORIZATION_ENV, AVIBE_RUN_ID_ENV
from storage import project_access_service, resource_access_service
from storage.db import get_cached_sqlite_engine
from storage.models import (
    agent_runs,
    agent_sessions,
    agents,
    harness_definition_dependencies,
    harness_principal_entitlements,
    harness_run_dependencies,
    run_definitions,
)
from vibe.authorization import AuthorizationContext, trusted_local_context


ENTITLEMENT_MAX_AGE_SECONDS = 5 * 60
DEFINITION_RESOURCE_KINDS = {
    "scheduled": "harness_task",
    "watch": "harness_watch",
}
DEPENDENCY_RESOURCE_KINDS = frozenset(
    {"agent", "skill", "vault_secret", "show_page"}
)
_DEFINITION_OPERATION_ROLES = {
    "list": "viewer",
    "detail": "viewer",
    "status": "viewer",
    "output": "viewer",
    "run": "editor",
    "pause": "editor",
    "resume": "editor",
    "cancel": "editor",
    "update": "owner",
    "delete": "owner",
    "raw": "owner",
    "logs": "owner",
}
_RUN_OPERATION_ROLES = {
    "list": "viewer",
    "detail": "viewer",
    "status": "viewer",
    "output": "viewer",
    "cancel": "editor",
    "raw": "owner",
    "logs": "owner",
}
_RAW_RUN_FIELDS = (
    "source_actor",
    "parent_run_id",
    "agent_name",
    "agent_id",
    "agent_backend",
    "model",
    "reasoning_effort",
    "session_policy",
    "session_key",
    "session_id",
    "post_to",
    "deliver_key",
    "prompt",
    "message",
    "message_payload",
    "result_text",
    "result_payload",
    "message_ids",
    "callback_session_id",
    "callback_status",
    "callback_error",
    "callback_run_id",
    "callback_completed_at",
    "pid",
    "exit_code",
    "error",
    "stdout",
    "stderr",
)
_SAFE_DEFINITION_FIELDS = (
    "id",
    "name",
    "enabled",
    "created_at",
    "updated_at",
    "last_run_at",
    "last_started_at",
    "last_finished_at",
    "last_event_at",
    "last_exit_code",
)


class HarnessAuthorizationError(PermissionError):
    """Stable authorization failure with HTTP-safe classification."""

    def __init__(self, code: str, *, hidden: bool = False):
        super().__init__(code)
        self.code = code
        self.hidden = hidden


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clean(value: Any, *, limit: int = 320) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        return None
    return normalized


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    for item in value:
        normalized = _clean(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _context(user_context: AuthorizationContext | Mapping[str, Any] | None) -> AuthorizationContext:
    return resource_access_service.resolve_resource_access_context(user_context)


def _definition_resource_kind(definition_type: Any) -> str:
    kind = DEFINITION_RESOURCE_KINDS.get(str(definition_type or ""))
    if kind is None:
        raise HarnessAuthorizationError("invalid_harness_definition")
    return kind


def _definition_row(
    connection: Connection,
    definition_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    statement = select(run_definitions).where(run_definitions.c.id == definition_id).limit(1)
    if not include_deleted:
        statement = statement.where(run_definitions.c.deleted_at.is_(None))
    row = connection.execute(statement).mappings().first()
    return dict(row) if row else None


def get_definition_resource_metadata(
    connection: Connection,
    resource_kind: str,
    resource_id: str,
) -> dict[str, str] | None:
    """Return the name/state-only definition fields allowed in ACL sync."""

    expected_type = {
        "harness_task": "scheduled",
        "harness_watch": "watch",
    }.get(resource_kind)
    if expected_type is None:
        return None
    row = connection.execute(
        select(
            run_definitions.c.id,
            run_definitions.c.name,
            run_definitions.c.enabled,
            run_definitions.c.authorization_state,
            run_definitions.c.updated_at,
        )
        .where(
            run_definitions.c.id == resource_id,
            run_definitions.c.definition_type == expected_type,
            run_definitions.c.deleted_at.is_(None),
        )
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    state = (
        "suspended_authorization"
        if row["authorization_state"] == "suspended_authorization"
        else "enabled" if row["enabled"] else "disabled"
    )
    return {
        "display_name": str(row["name"] or row["id"]),
        "state": state,
        "updated_at": str(row["updated_at"]),
    }


def _run_row(connection: Connection, run_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
    ).mappings().first()
    return dict(row) if row else None


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("metadata")
    if isinstance(payload, dict):
        return dict(payload)
    parsed = _json_loads(row.get("metadata_json"), {})
    return dict(parsed) if isinstance(parsed, dict) else {}


def _session_project_id(connection: Connection, session_id: Any) -> str | None:
    identifier = _clean(session_id)
    if identifier is None:
        return None
    scope_id = connection.execute(
        select(agent_sessions.c.scope_id)
        .where(agent_sessions.c.id == identifier)
        .limit(1)
    ).scalar_one_or_none()
    return project_access_service.project_id_from_scope_id(scope_id)


def _project_id_from_scope(value: Any) -> str | None:
    scope_id = _clean(value)
    if scope_id is None:
        return None
    return project_access_service.project_id_from_scope_id(scope_id)


def _primary_project_id(
    connection: Connection,
    payload: Mapping[str, Any],
) -> str | None:
    explicit = _clean(payload.get("project_id"))
    if explicit:
        return explicit
    session_project = _session_project_id(connection, payload.get("session_id"))
    if session_project:
        return session_project
    metadata = _metadata(payload)
    for key in ("session_scope_id", "scope_id"):
        project_id = _project_id_from_scope(metadata.get(key))
        if project_id:
            return project_id
    for key in (payload.get("deliver_key"), payload.get("session_key")):
        project_id = _project_id_from_scope(key)
        if project_id:
            return project_id
    return None


def _agent_resource_id(connection: Connection, payload: Mapping[str, Any]) -> str | None:
    agent_id = _clean(payload.get("agent_id"))
    if agent_id:
        return agent_id
    agent_name = _clean(payload.get("agent_name"))
    if agent_name is None:
        return None
    return connection.execute(
        select(agents.c.id)
        .where(agents.c.normalized_name == agent_name.casefold())
        .limit(1)
    ).scalar_one_or_none()


def _declared_dependencies(metadata: Mapping[str, Any]) -> tuple[list[tuple[str, str]], bool]:
    dependencies: list[tuple[str, str]] = []
    complete = True
    key_map = {
        "project_ids": "project",
        "referenced_project_ids": "project",
        "session_ids": "session",
        "referenced_session_ids": "session",
        "skill_ids": "skill",
        "skill_resource_ids": "skill",
        "vault_secret_ids": "vault_secret",
        "vault_secret_resource_ids": "vault_secret",
        "show_page_ids": "show_page",
        "show_page_resource_ids": "show_page",
    }
    for key, resource_kind in key_map.items():
        raw = metadata.get(key)
        if raw is None:
            continue
        values = _strings(raw)
        if not isinstance(raw, (list, tuple, set, frozenset)) or len(values) != len(raw):
            complete = False
        dependencies.extend((resource_kind, value) for value in values)

    raw_resources = metadata.get("harness_resources", metadata.get("resource_dependencies"))
    if raw_resources is not None:
        if not isinstance(raw_resources, list):
            complete = False
        else:
            for raw in raw_resources:
                if not isinstance(raw, Mapping):
                    complete = False
                    continue
                kind = _clean(raw.get("resource_kind", raw.get("kind")))
                identifier = _clean(raw.get("resource_id", raw.get("id")))
                if kind not in DEPENDENCY_RESOURCE_KINDS | {"project", "session"} or not identifier:
                    complete = False
                    continue
                dependencies.append((kind, identifier))
    return list(dict.fromkeys(dependencies)), complete


def _principal_provenance(context: AuthorizationContext) -> dict[str, Any]:
    if context.is_trusted_local:
        return {"principal_type": "trusted_local"}
    if not context.is_remote:
        raise HarnessAuthorizationError("harness_principal_untrusted")
    instance_id = _clean(context.instance_id)
    subject = _clean(context.subject)
    if not instance_id or not subject:
        raise HarnessAuthorizationError("harness_principal_incomplete")
    return {
        "principal_type": "remote",
        "instance_id": instance_id,
        "subject": subject,
        "organization_member_id": _clean(context.organization_member_id),
        "membership_version": _clean(context.membership_version),
    }


def mirror_remote_principal(
    context: AuthorizationContext,
    session_payload: Mapping[str, Any],
    *,
    engine: Engine | None = None,
    now: int | None = None,
) -> None:
    """Persist one fresh remote entitlement record from validated signed claims."""

    if not context.is_remote or context.is_trusted_local:
        return
    instance_id = _clean(context.instance_id)
    subject = _clean(context.subject)
    if not instance_id or not subject or not context.has_role("viewer"):
        raise HarnessAuthorizationError("harness_principal_incomplete")
    revision = session_payload.get("vibe_instance_authorization_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HarnessAuthorizationError("harness_entitlement_revision_missing")
    current = int(time.time()) if now is None else int(now)
    try:
        claims_issued_at = int(
            session_payload.get("claims_issued_at", session_payload.get("iat", current))
        )
    except (TypeError, ValueError) as exc:
        raise HarnessAuthorizationError("harness_entitlement_invalid") from exc
    values = {
        "instance_id": instance_id,
        "subject": subject,
        "organization_member_id": _clean(context.organization_member_id),
        "email": _clean(context.email),
        "organization_id": _clean(context.organization_id),
        "instance_role": context.instance_role,
        "instance_access_source": _clean(context.instance_access_source),
        "organization_role": _clean(context.organization_role),
        "group_ids_json": _json_dumps(sorted(context.group_ids)),
        "membership_version": _clean(context.membership_version),
        "authorization_revision": revision,
        "claims_issued_at": claims_issued_at,
        "fresh_until": current + ENTITLEMENT_MAX_AGE_SECONDS,
        "updated_at": _utc_now_iso(),
    }
    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.begin() as connection:
        statement = sqlite_insert(harness_principal_entitlements).values(**values)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    harness_principal_entitlements.c.instance_id,
                    harness_principal_entitlements.c.subject,
                ],
                set_={key: value for key, value in values.items() if key not in {"instance_id", "subject"}},
            )
        )


def _refresh_entitlement_from_device_revision(
    connection: Connection,
    row: Mapping[str, Any],
    *,
    now: int,
) -> bool:
    try:
        from config.v2_config import V2Config
        from vibe import remote_access

        config = V2Config.load()
        revision = remote_access.current_authorization_revision(config, now=float(now))
    except Exception:
        return False
    if revision is None or revision != int(row.get("authorization_revision") or -1):
        return False
    connection.execute(
        update(harness_principal_entitlements)
        .where(
            harness_principal_entitlements.c.instance_id == row["instance_id"],
            harness_principal_entitlements.c.subject == row["subject"],
        )
        .values(
            fresh_until=now + ENTITLEMENT_MAX_AGE_SECONDS,
            updated_at=_utc_now_iso(),
        )
    )
    return True


def _current_principal_context(
    connection: Connection,
    principal: Mapping[str, Any],
    *,
    now: int,
) -> AuthorizationContext:
    if principal.get("principal_type") == "trusted_local":
        return trusted_local_context()
    if principal.get("principal_type") != "remote":
        raise HarnessAuthorizationError("harness_principal_incomplete")
    instance_id = _clean(principal.get("instance_id"))
    subject = _clean(principal.get("subject"))
    if not instance_id or not subject:
        raise HarnessAuthorizationError("harness_principal_incomplete")
    entitlement = connection.execute(
        select(harness_principal_entitlements)
        .where(
            harness_principal_entitlements.c.instance_id == instance_id,
            harness_principal_entitlements.c.subject == subject,
        )
        .limit(1)
    ).mappings().first()
    if entitlement is None:
        raise HarnessAuthorizationError("harness_entitlement_unavailable")
    if int(entitlement["fresh_until"]) <= now and not _refresh_entitlement_from_device_revision(
        connection,
        entitlement,
        now=now,
    ):
        raise HarnessAuthorizationError("harness_entitlement_stale")
    groups = _json_loads(entitlement["group_ids_json"], [])
    if not isinstance(groups, list):
        raise HarnessAuthorizationError("harness_entitlement_invalid")
    return AuthorizationContext(
        instance_role=_clean(entitlement["instance_role"]),
        subject=subject,
        email=_clean(entitlement["email"]),
        instance_id=instance_id,
        instance_access_source=_clean(entitlement["instance_access_source"]),
        organization_id=_clean(entitlement["organization_id"]),
        organization_member_id=_clean(entitlement["organization_member_id"]),
        organization_role=_clean(entitlement["organization_role"]),
        group_ids=frozenset(_strings(groups)),
        membership_version=_clean(entitlement["membership_version"]),
        claims_issued_at=int(entitlement["claims_issued_at"]),
        is_remote=True,
    )


def execution_context(
    run_id: str,
    *,
    engine: Engine | None = None,
    now: int | None = None,
) -> AuthorizationContext:
    """Rebuild a Run principal from the current entitlement mirror."""

    active_engine = engine or get_cached_sqlite_engine()
    current = int(time.time()) if now is None else int(now)
    with active_engine.begin() as connection:
        row = _run_row(connection, run_id)
        if row is None:
            raise HarnessAuthorizationError("harness_run_not_found", hidden=True)
        provenance = _json_loads(row.get("authorization_provenance_json"), {})
        if not isinstance(provenance, Mapping):
            raise HarnessAuthorizationError("harness_provenance_incomplete")
        principal = provenance.get("execution_principal")
        if not isinstance(principal, Mapping):
            raise HarnessAuthorizationError("harness_principal_incomplete")
        return _current_principal_context(
            connection,
            principal,
            now=current,
        )


def execution_context_for_current_run() -> AuthorizationContext:
    if os.environ.get(AVIBE_HARNESS_AUTHORIZATION_ENV) != "1":
        raise HarnessAuthorizationError("harness_run_context_missing")
    run_id = _clean(os.environ.get(AVIBE_RUN_ID_ENV))
    if run_id is None:
        raise HarnessAuthorizationError("harness_run_context_missing")
    return execution_context(run_id)


def _effective_project_role(
    connection: Connection,
    context: AuthorizationContext,
    project_id: str,
    minimum_role: str,
) -> bool:
    return project_access_service.role_allows(
        project_access_service.get_effective_project_role(connection, context, project_id),
        minimum_role,
    )


def _require_project(
    connection: Connection,
    context: AuthorizationContext,
    project_id: Any,
    minimum_role: str,
) -> None:
    identifier = _clean(project_id)
    if identifier is None:
        if context.is_instance_owner:
            return
        raise HarnessAuthorizationError("harness_project_access_forbidden", hidden=True)
    if not _effective_project_role(connection, context, identifier, minimum_role):
        raise HarnessAuthorizationError("harness_project_access_forbidden", hidden=True)


def _require_session(
    connection: Connection,
    context: AuthorizationContext,
    session_id: str,
    minimum_role: str,
) -> None:
    role = project_access_service.get_effective_session_role(connection, context, session_id)
    if not project_access_service.role_allows(role, minimum_role):
        raise HarnessAuthorizationError("harness_session_access_forbidden", hidden=True)


def _definition_dependencies(
    connection: Connection,
    definition_id: str,
) -> list[tuple[str, str]]:
    return [
        (str(row.resource_kind), str(row.resource_id))
        for row in connection.execute(
            select(
                harness_definition_dependencies.c.resource_kind,
                harness_definition_dependencies.c.resource_id,
            ).where(harness_definition_dependencies.c.definition_id == definition_id)
        )
    ]


def _run_dependencies(
    connection: Connection,
    run_id: str,
) -> list[tuple[str, str]]:
    return [
        (str(row.resource_kind), str(row.resource_id))
        for row in connection.execute(
            select(
                harness_run_dependencies.c.resource_kind,
                harness_run_dependencies.c.resource_id,
            ).where(harness_run_dependencies.c.run_id == run_id)
        )
    ]


def _require_dependencies(
    connection: Connection,
    context: AuthorizationContext,
    dependencies: Iterable[tuple[str, str]],
    minimum_project_role: str,
) -> None:
    for resource_kind, resource_id in dependencies:
        if resource_kind == "project":
            _require_project(connection, context, resource_id, minimum_project_role)
        elif resource_kind == "session":
            _require_session(connection, context, resource_id, minimum_project_role)
        elif resource_kind in DEPENDENCY_RESOURCE_KINDS and not resource_access_service.can_use_resource(
            context,
            resource_kind,
            resource_id,
            connection=connection,
        ):
            raise HarnessAuthorizationError(f"harness_{resource_kind}_access_forbidden")


def authorize_create(
    context: AuthorizationContext,
    *,
    project_id: str | None,
    connection: Connection,
) -> None:
    if not context.has_role("owner"):
        raise HarnessAuthorizationError("harness_owner_required")
    if context.is_remote and not project_id:
        raise HarnessAuthorizationError("harness_project_required")
    if project_id:
        _require_project(connection, context, project_id, "owner")


def authorize_definition(
    context: AuthorizationContext,
    definition: Mapping[str, Any],
    operation: str,
    *,
    connection: Connection,
) -> None:
    minimum_role = _DEFINITION_OPERATION_ROLES.get(operation)
    if minimum_role is None:
        raise HarnessAuthorizationError("invalid_harness_operation")
    if not context.has_role(minimum_role):
        raise HarnessAuthorizationError("harness_operation_forbidden")
    definition_id = _clean(definition.get("id"))
    if definition_id is None:
        raise HarnessAuthorizationError("invalid_harness_definition")
    kind = _definition_resource_kind(definition.get("definition_type"))
    _require_project(connection, context, definition.get("project_id"), minimum_role)

    management = operation in {"update", "delete", "raw", "logs"}
    allowed = (
        resource_access_service.can_manage_resource_acl(
            context,
            kind,
            definition_id,
            connection=connection,
        )
        if management
        else resource_access_service.can_match_resource_acl(
            context,
            kind,
            definition_id,
            connection=connection,
        )
    )
    if not allowed:
        raise HarnessAuthorizationError("harness_definition_access_forbidden", hidden=not management)

    project_session_role = "editor" if operation in {"run", "resume", "cancel"} else "viewer"
    dependencies = _definition_dependencies(connection, definition_id)
    for resource_kind, resource_id in dependencies:
        if resource_kind == "project":
            _require_project(connection, context, resource_id, project_session_role)
        elif resource_kind == "session":
            _require_session(connection, context, resource_id, project_session_role)
    if operation in {"run", "resume"}:
        _require_dependencies(connection, context, dependencies, "editor")


def prepare_definition_payload(
    payload: Mapping[str, Any],
    *,
    definition_type: str,
    user_context: AuthorizationContext | Mapping[str, Any] | None,
    engine: Engine,
) -> dict[str, Any]:
    context = _context(user_context)
    result = dict(payload)
    result["definition_type"] = definition_type
    with engine.connect() as connection:
        project_id = _primary_project_id(connection, result)
        authorize_create(context, project_id=project_id, connection=connection)
    result["project_id"] = project_id
    metadata = _metadata(result)
    metadata["harness_execution_principal"] = _principal_provenance(context)
    result["metadata"] = metadata
    result.setdefault("authorization_state", "active")
    return result


def _replace_definition_dependencies(
    connection: Connection,
    definition: Mapping[str, Any],
) -> bool:
    definition_id = str(definition["id"])
    dependencies: list[tuple[str, str]] = []
    project_id = _clean(definition.get("project_id"))
    if project_id:
        dependencies.append(("project", project_id))
    for key in ("session_id",):
        identifier = _clean(definition.get(key))
        if identifier:
            dependencies.append(("session", identifier))
    agent_id = _agent_resource_id(connection, definition)
    if agent_id:
        dependencies.append(("agent", agent_id))
    declared, complete = _declared_dependencies(_metadata(definition))
    dependencies.extend(declared)
    dependencies = list(dict.fromkeys(dependencies))
    connection.execute(
        delete(harness_definition_dependencies).where(
            harness_definition_dependencies.c.definition_id == definition_id
        )
    )
    if dependencies:
        connection.execute(
            insert(harness_definition_dependencies),
            [
                {
                    "definition_id": definition_id,
                    "resource_kind": kind,
                    "resource_id": identifier,
                    "access_mode": "write" if kind in {"project", "session"} else "use",
                    "created_at": _utc_now_iso(),
                }
                for kind, identifier in dependencies
            ],
        )
    return complete


def register_definition(
    definition_id: str,
    *,
    user_context: AuthorizationContext | Mapping[str, Any] | None,
    engine: Engine,
) -> None:
    context = _context(user_context)
    with engine.begin() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_create(
            context,
            project_id=_clean(definition.get("project_id")),
            connection=connection,
        )
        if context.is_remote:
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind=_definition_resource_kind(definition["definition_type"]),
                resource_id=definition_id,
                organization_id=context.organization_id,
                owner_user_id=context.subject,
                owner_email=context.email,
                access_level="private",
                created_by_user_id=context.subject,
                updated_by_user_id=context.subject,
            )
        complete = _replace_definition_dependencies(connection, definition)
        if not complete:
            connection.execute(
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .values(authorization_state="suspended_authorization")
            )


def authorize_definition_operation(
    definition_id: str,
    operation: str,
    *,
    user_context: AuthorizationContext | Mapping[str, Any] | None,
    engine: Engine,
) -> dict[str, Any]:
    """Authorize a store-layer definition operation and return its raw row."""

    context = _context(user_context)
    with engine.connect() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_definition(context, definition, operation, connection=connection)
        return definition


def refresh_definition_dependencies(
    definition_id: str,
    *,
    engine: Engine,
) -> None:
    """Refresh dependency attribution after an authorized definition update."""

    with engine.begin() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        complete = _replace_definition_dependencies(connection, definition)
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(
                authorization_state=(
                    "active" if complete else "suspended_authorization"
                )
            )
        )


def delete_definition_policy(
    connection: Connection,
    definition: Mapping[str, Any],
) -> None:
    resource_access_service.delete_resource_policy(
        connection,
        _definition_resource_kind(definition.get("definition_type")),
        str(definition["id"]),
    )


def set_definition_enabled(
    context: AuthorizationContext | Mapping[str, Any] | None,
    definition_id: str,
    enabled: bool,
    *,
    engine: Engine,
) -> dict[str, Any]:
    """Authorize and persist an editor pause/resume operation."""

    context = _context(context)
    operation = "resume" if enabled else "pause"
    with engine.begin() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_definition(context, definition, operation, connection=connection)
        values: dict[str, Any] = {
            "enabled": 1 if enabled else 0,
            "updated_at": _utc_now_iso(),
        }
        if enabled:
            metadata = _metadata(definition)
            metadata["harness_execution_principal"] = _principal_provenance(context)
            values.update(
                metadata_json=_json_dumps(metadata),
                authorization_state="active",
            )
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(**values)
        )
        refreshed = _definition_row(connection, definition_id)
        if refreshed is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        return refreshed


def remove_definition(
    context: AuthorizationContext | Mapping[str, Any] | None,
    definition_id: str,
    *,
    engine: Engine,
) -> None:
    """Authorize an owner deletion and quarantine non-terminal derived Runs."""

    context = _context(context)
    with engine.begin() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_definition(context, definition, "delete", connection=connection)
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(
                enabled=0,
                deleted_at=_utc_now_iso(),
                authorization_state="suspended_authorization",
                updated_at=_utc_now_iso(),
            )
        )
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.definition_id == definition_id)
            .where(agent_runs.c.status.in_(["pending", "queued", "processing", "running"]))
            .values(
                status="canceled",
                cancel_requested=1,
                cancel_requested_at=_utc_now_iso(),
                completed_at=_utc_now_iso(),
                output_quarantined=1,
                member_safe_json=None,
                safe_error_code="definition_deleted",
                updated_at=_utc_now_iso(),
            )
        )
        delete_definition_policy(connection, definition)


def authorize_manual_run(
    context: AuthorizationContext,
    definition_id: str,
    *,
    engine: Engine,
) -> dict[str, Any]:
    """Return raw definition data only after editor invocation checks pass."""

    with engine.connect() as connection:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_definition(context, definition, "run", connection=connection)
        result = dict(definition)
        metadata = _metadata(definition)
        metadata["harness_activation_principal"] = _principal_provenance(context)
        result["metadata"] = metadata
        return result


def cancel_run(
    context: AuthorizationContext,
    run_id: str,
    *,
    engine: Engine,
) -> bool:
    """Authorize cancellation and atomically quarantine future output."""

    with engine.begin() as connection:
        run = _run_row(connection, run_id)
        if run is None:
            raise HarnessAuthorizationError("harness_run_not_found", hidden=True)
        authorize_run(context, run, "cancel", connection=connection)
        result = connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .where(agent_runs.c.status.in_(["pending", "queued", "processing", "running"]))
            .values(
                status="canceled",
                cancel_requested=1,
                cancel_requested_at=_utc_now_iso(),
                completed_at=_utc_now_iso(),
                output_quarantined=1,
                member_safe_json=None,
                safe_error_code="canceled",
                updated_at=_utc_now_iso(),
            )
        )
        return bool(result.rowcount)


def serialize_definition(
    context: AuthorizationContext,
    definition: Mapping[str, Any],
    *,
    connection: Connection,
    detail: bool = False,
) -> dict[str, Any]:
    authorize_definition(context, definition, "detail" if detail else "list", connection=connection)
    projected = {
        key: definition.get(key)
        for key in _SAFE_DEFINITION_FIELDS
        if key in definition and definition.get(key) is not None
    }
    projected["definition_type"] = definition.get("definition_type")
    projected["authorization_state"] = definition.get("authorization_state") or "active"
    projected["state"] = (
        "suspended_authorization"
        if projected["authorization_state"] == "suspended_authorization"
        else "enabled" if definition.get("enabled") else "disabled"
    )
    capabilities: dict[str, bool] = {}
    for operation in ("run", "pause", "resume", "update", "delete"):
        try:
            authorize_definition(context, definition, operation, connection=connection)
            capabilities[f"can_{operation}"] = True
        except HarnessAuthorizationError:
            capabilities[f"can_{operation}"] = False
    projected["capabilities"] = capabilities
    projected["redacted"] = True
    if detail:
        try:
            authorize_definition(context, definition, "raw", connection=connection)
        except HarnessAuthorizationError:
            return projected
        raw = dict(definition)
        raw.pop("metadata_json", None)
        metadata = _metadata(definition)
        metadata.pop(resource_access_service.RESOURCE_USER_CONTEXT_METADATA_KEY, None)
        metadata.pop("harness_execution_principal", None)
        raw["metadata"] = metadata
        raw["capabilities"] = capabilities
        raw["redacted"] = False
        return raw
    return projected


def filter_definitions(
    context: AuthorizationContext,
    definitions: Iterable[Mapping[str, Any]],
    *,
    connection: Connection,
    detail: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for definition in definitions:
        try:
            result.append(
                serialize_definition(
                    context,
                    definition,
                    connection=connection,
                    detail=detail,
                )
            )
        except HarnessAuthorizationError:
            continue
    return result


def _forbidden_manifest(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        normalized = _clean(value, limit=16_384)
        if not normalized:
            return
        values.append(normalized)
        values.extend(
            fragment
            for fragment in re.findall(r"[\w./:@+\-]{4,}", normalized)
            if fragment != normalized
        )

    for key in (
        "prompt",
        "message",
        "cwd",
        "shell_command",
        "project_id",
        "session_id",
        "session_key",
        "callback_session_id",
        "deliver_key",
        "post_to",
        "agent_name",
        "agent_id",
        "cron",
        "run_at",
        "timezone",
    ):
        add(payload.get(key))
    command = payload.get("command")
    if isinstance(command, list):
        for item in command:
            add(item)
    for _kind, identifier in _declared_dependencies(_metadata(payload))[0]:
        add(identifier)
    return list(dict.fromkeys(values))


def prepare_run_authorization(
    connection: Connection,
    payload: Mapping[str, Any],
    *,
    activation_context: AuthorizationContext | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    definition_id = _clean(payload.get("definition_id", payload.get("task_id")))
    definition = _definition_row(connection, definition_id) if definition_id else None
    metadata = _metadata(payload)
    if activation_context is not None:
        principal = _principal_provenance(_context(activation_context))
    elif isinstance(metadata.get("harness_activation_principal"), Mapping):
        principal = dict(metadata["harness_activation_principal"])
    elif definition is not None:
        definition_principal = _metadata(definition).get("harness_execution_principal")
        principal = dict(definition_principal) if isinstance(definition_principal, Mapping) else {}
    else:
        principal = _principal_provenance(_context(None))

    project_id = _clean(definition.get("project_id")) if definition else _primary_project_id(connection, payload)
    project_ids: list[str] = [project_id] if project_id else []
    session_ids: list[str] = []
    for key in ("session_id", "callback_session_id"):
        identifier = _clean(payload.get(key))
        if identifier:
            session_ids.append(identifier)
            session_project = _session_project_id(connection, identifier)
            if session_project:
                project_ids.append(session_project)
    source_session = _clean(payload.get("source_actor"))
    if source_session:
        source_project = _session_project_id(connection, source_session)
        if source_project:
            session_ids.append(source_session)
            project_ids.append(source_project)

    dependencies = _definition_dependencies(connection, definition_id) if definition_id else []
    declared, complete = _declared_dependencies(metadata)
    dependencies.extend(declared)
    agent_id = _agent_resource_id(connection, payload)
    if agent_id:
        dependencies.append(("agent", agent_id))
    for kind, identifier in dependencies:
        if kind == "project":
            project_ids.append(identifier)
        elif kind == "session":
            session_ids.append(identifier)
    complete = bool(principal) and complete and (bool(project_id) or principal.get("principal_type") == "trusted_local")
    return {
        "schema_version": 1,
        "definition_kind": definition.get("definition_type") if definition else None,
        "definition_id": definition_id,
        "launch_project_id": project_id,
        "project_ids": list(dict.fromkeys(project_ids)),
        "session_ids": list(dict.fromkeys(session_ids)),
        "execution_principal": principal,
        "dependencies": [
            {"resource_kind": kind, "resource_id": identifier}
            for kind, identifier in dict.fromkeys(dependencies)
        ],
        "dependency_attribution_complete": complete,
        "forbidden_content": _forbidden_manifest({**(definition or {}), **dict(payload)}),
    }


def persist_run_dependencies(
    connection: Connection,
    run_id: str,
    provenance: Mapping[str, Any],
) -> None:
    dependencies: list[tuple[str, str]] = []
    for project_id in _strings(provenance.get("project_ids")):
        dependencies.append(("project", project_id))
    for session_id in _strings(provenance.get("session_ids")):
        dependencies.append(("session", session_id))
    raw_dependencies = provenance.get("dependencies")
    if isinstance(raw_dependencies, list):
        for raw in raw_dependencies:
            if not isinstance(raw, Mapping):
                continue
            kind = _clean(raw.get("resource_kind"))
            identifier = _clean(raw.get("resource_id"))
            if kind and identifier:
                dependencies.append((kind, identifier))
    dependencies = list(dict.fromkeys(dependencies))
    if not dependencies:
        return
    connection.execute(
        insert(harness_run_dependencies),
        [
            {
                "run_id": run_id,
                "resource_kind": kind,
                "resource_id": identifier,
                "access_mode": "write" if kind in {"project", "session"} else "use",
                "used_at": _utc_now_iso(),
            }
            for kind, identifier in dependencies
        ],
    )


def record_dependency(
    run_id: str,
    resource_kind: str,
    resource_id: str,
    *,
    engine: Engine | None = None,
) -> None:
    if resource_kind not in DEPENDENCY_RESOURCE_KINDS | {"project", "session"}:
        raise HarnessAuthorizationError("invalid_harness_dependency")
    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.begin() as connection:
        if _run_row(connection, run_id) is None:
            raise HarnessAuthorizationError("harness_run_not_found")
        statement = sqlite_insert(harness_run_dependencies).values(
            run_id=run_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            access_mode="write" if resource_kind in {"project", "session"} else "use",
            used_at=_utc_now_iso(),
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    harness_run_dependencies.c.run_id,
                    harness_run_dependencies.c.resource_kind,
                    harness_run_dependencies.c.resource_id,
                ],
                set_={"used_at": _utc_now_iso()},
            )
        )
        if resource_kind == "vault_secret":
            connection.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(
                    output_classification="vault_tainted",
                    member_safe_json=None,
                    callback_status="suppressed_authorization",
                    callback_error=None,
                )
            )


def record_dependency_in_connection(
    connection: Connection,
    run_id: str,
    resource_kind: str,
    resource_id: str,
) -> None:
    if resource_kind not in DEPENDENCY_RESOURCE_KINDS | {"project", "session"}:
        raise HarnessAuthorizationError("invalid_harness_dependency")
    if _run_row(connection, run_id) is None:
        return
    statement = sqlite_insert(harness_run_dependencies).values(
        run_id=run_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        access_mode="write" if resource_kind in {"project", "session"} else "use",
        used_at=_utc_now_iso(),
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[
                harness_run_dependencies.c.run_id,
                harness_run_dependencies.c.resource_kind,
                harness_run_dependencies.c.resource_id,
            ],
            set_={"used_at": _utc_now_iso()},
        )
    )
    if resource_kind == "vault_secret":
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(
                output_classification="vault_tainted",
                member_safe_json=None,
                callback_status="suppressed_authorization",
                callback_error=None,
            )
        )


def record_current_run_dependency(
    resource_kind: str,
    resource_id: str,
    *,
    connection: Connection | None = None,
) -> None:
    run_id = _clean(os.environ.get(AVIBE_RUN_ID_ENV))
    if run_id and os.environ.get(AVIBE_HARNESS_AUTHORIZATION_ENV) == "1":
        if connection is not None:
            record_dependency_in_connection(
                connection,
                run_id,
                resource_kind,
                resource_id,
            )
        else:
            record_dependency(run_id, resource_kind, resource_id)


def _provenance(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("authorization_provenance")
    if isinstance(value, dict):
        return dict(value)
    parsed = _json_loads(run.get("authorization_provenance_json"), {})
    return dict(parsed) if isinstance(parsed, dict) else {}


def _run_base_access(
    connection: Connection,
    context: AuthorizationContext,
    run: Mapping[str, Any],
    minimum_role: str,
) -> None:
    provenance = _provenance(run)
    if not provenance.get("dependency_attribution_complete") and not context.is_instance_owner:
        raise HarnessAuthorizationError("harness_provenance_incomplete", hidden=True)
    project_ids = _strings(provenance.get("project_ids"))
    if not project_ids:
        _require_project(connection, context, run.get("project_id"), minimum_role)
    else:
        for project_id in project_ids:
            _require_project(connection, context, project_id, minimum_role)
    for session_id in _strings(provenance.get("session_ids")):
        _require_session(connection, context, session_id, minimum_role)
    for resource_kind, resource_id in _run_dependencies(
        connection,
        str(run["id"]),
    ):
        if resource_kind == "project":
            _require_project(connection, context, resource_id, minimum_role)
        elif resource_kind == "session":
            _require_session(connection, context, resource_id, minimum_role)
    definition_id = _clean(provenance.get("definition_id", run.get("definition_id")))
    if definition_id:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            if context.is_instance_owner:
                return
            raise HarnessAuthorizationError("harness_definition_not_found", hidden=True)
        authorize_definition(context, definition, "cancel" if minimum_role == "editor" else "status", connection=connection)


def authorize_run(
    context: AuthorizationContext,
    run: Mapping[str, Any],
    operation: str,
    *,
    connection: Connection,
) -> None:
    minimum_role = _RUN_OPERATION_ROLES.get(operation)
    if minimum_role is None:
        raise HarnessAuthorizationError("invalid_harness_operation")
    if not context.has_role(minimum_role):
        raise HarnessAuthorizationError("harness_operation_forbidden")
    _run_base_access(connection, context, run, minimum_role)
    if operation in {"raw", "logs"}:
        _require_dependencies(
            connection,
            context,
            _run_dependencies(connection, str(run["id"])),
            minimum_role,
        )
        definition_id = _clean(run.get("definition_id"))
        if definition_id:
            definition = _definition_row(connection, definition_id)
            if definition is None:
                if not context.is_instance_owner:
                    raise HarnessAuthorizationError(
                        "harness_definition_not_found",
                        hidden=True,
                    )
            else:
                authorize_definition(context, definition, operation, connection=connection)
        else:
            agent_id = _clean(run.get("agent_id")) or next(
                (
                    resource_id
                    for kind, resource_id in _run_dependencies(connection, str(run["id"]))
                    if kind == "agent"
                ),
                None,
            )
            if agent_id and not resource_access_service.can_use_resource(
                context, "agent", agent_id, connection=connection
            ):
                raise HarnessAuthorizationError("harness_agent_access_forbidden")


def can_read_run(
    context: AuthorizationContext,
    run: Mapping[str, Any],
    *,
    connection: Connection,
) -> bool:
    """Return whether current entitlements permit safe Run status/history."""

    try:
        authorize_run(context, run, "detail", connection=connection)
    except HarnessAuthorizationError:
        return False
    return True


def _has_vault_dependency(connection: Connection, run_id: str) -> bool:
    return connection.execute(
        select(harness_run_dependencies.c.run_id)
        .where(
            harness_run_dependencies.c.run_id == run_id,
            harness_run_dependencies.c.resource_kind == "vault_secret",
        )
        .limit(1)
    ).first() is not None


def _content_access(
    connection: Connection,
    context: AuthorizationContext,
    run: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    run_id = str(run["id"])
    if bool(run.get("output_quarantined")):
        return None, "authorization_revoked"
    if _has_vault_dependency(connection, run_id) or run.get("output_classification") == "vault_tainted":
        return None, "vault_resource_used"
    provenance = _provenance(run)
    if not provenance.get("dependency_attribution_complete"):
        return None, "dependency_attribution_incomplete"
    try:
        _require_dependencies(
            connection,
            context,
            _run_dependencies(connection, run_id),
            "viewer",
        )
    except HarnessAuthorizationError:
        return None, "dependency_access_revoked"
    member_safe = run.get("member_safe")
    if member_safe is None:
        member_safe = _json_loads(run.get("member_safe_json"), None)
    if run.get("output_classification") != "member_safe" or not isinstance(member_safe, dict):
        return None, "output_unclassified"
    return dict(member_safe), "member_safe"


def _safe_run_envelope(run: Mapping[str, Any]) -> dict[str, Any]:
    envelope = {
        "id": run.get("id"),
        "request_type": run.get("request_type", run.get("run_type")),
        "run_type": run.get("run_type", run.get("request_type")),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "updated_at": run.get("updated_at"),
        "cancel_requested": bool(run.get("cancel_requested")),
        "safe_error_code": run.get("safe_error_code"),
    }
    envelope["ok"] = (
        None
        if not run.get("completed_at")
        else str(run.get("status") or "") in {"completed", "succeeded"}
    )
    return envelope


def serialize_run(
    context: AuthorizationContext,
    run: Mapping[str, Any],
    *,
    connection: Connection,
    operation: str = "detail",
) -> dict[str, Any]:
    authorize_run(context, run, operation, connection=connection)
    projected = _safe_run_envelope(run)
    projected["capabilities"] = {"can_cancel": False, "can_read_logs": False}
    for action, key in (("cancel", "can_cancel"), ("logs", "can_read_logs")):
        try:
            authorize_run(context, run, action, connection=connection)
            projected["capabilities"][key] = True
        except HarnessAuthorizationError:
            pass

    member_safe, reason = _content_access(connection, context, run)
    projected["redaction"] = {
        "redacted": member_safe is None,
        "reason": reason,
    }
    if operation == "list":
        return projected

    vault_tainted = reason == "vault_resource_used"
    raw_allowed = False
    if not vault_tainted:
        try:
            authorize_run(context, run, "raw", connection=connection)
            raw_allowed = True
        except HarnessAuthorizationError:
            pass
    if raw_allowed:
        projected.update({field: run.get(field) for field in _RAW_RUN_FIELDS})
        projected["redaction"] = {"redacted": False, "reason": "owner_raw"}
    elif member_safe is not None:
        projected["member_safe"] = member_safe
        projected["result_text"] = member_safe.get("text")
    return projected


def filter_runs(
    context: AuthorizationContext,
    runs: Iterable[Mapping[str, Any]],
    *,
    connection: Connection,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in runs:
        try:
            result.append(serialize_run(context, run, connection=connection, operation="list"))
        except HarnessAuthorizationError:
            continue
    return result


def record_member_safe_output(
    run_id: str,
    output: Mapping[str, Any],
    *,
    engine: Engine | None = None,
) -> bool:
    """Persist a separately classified output only after forbidden-content scrub."""

    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.begin() as connection:
        run = _run_row(connection, run_id)
        if run is None or _has_vault_dependency(connection, run_id):
            return False
        provenance = _provenance(run)
        if not provenance.get("dependency_attribution_complete"):
            return False
        serialized = _json_dumps(dict(output))
        normalized = serialized.casefold()
        forbidden = _strings(provenance.get("forbidden_content"))
        if any(value.casefold() in normalized for value in forbidden):
            connection.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(
                    member_safe_json=None,
                    output_classification="unsafe",
                )
            )
            return False
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(
                member_safe_json=serialized,
                output_classification="member_safe",
            )
        )
    return True


def quarantine_run(
    run_id: str,
    *,
    reason: str = "authorization_revoked",
    engine: Engine | None = None,
    cancel: bool = True,
) -> bool:
    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.begin() as connection:
        values: dict[str, Any] = {
            "output_quarantined": 1,
            "member_safe_json": None,
            "safe_error_code": reason,
            "callback_status": "suppressed_authorization",
            "callback_error": None,
            "updated_at": _utc_now_iso(),
        }
        row = _run_row(connection, run_id)
        if row is None:
            return False
        if cancel and str(row.get("status")) in {"pending", "queued", "processing", "running"}:
            values.update(
                {
                    "status": "canceled",
                    "cancel_requested": 1,
                    "cancel_requested_at": _utc_now_iso(),
                    "completed_at": _utc_now_iso(),
                }
            )
        result = connection.execute(
            update(agent_runs).where(agent_runs.c.id == run_id).values(**values)
        )
        return bool(result.rowcount)


def suspend_definition(
    definition_id: str,
    *,
    engine: Engine | None = None,
) -> bool:
    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.begin() as connection:
        result = connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .where(run_definitions.c.deleted_at.is_(None))
            .values(
                enabled=0,
                authorization_state="suspended_authorization",
                updated_at=_utc_now_iso(),
            )
        )
        return bool(result.rowcount)


def revalidate_run_for_execution(
    run_id: str,
    *,
    engine: Engine | None = None,
) -> AuthorizationContext:
    active_engine = engine or get_cached_sqlite_engine()
    try:
        context = execution_context(run_id, engine=active_engine)
        with active_engine.connect() as connection:
            run = _run_row(connection, run_id)
            if run is None:
                raise HarnessAuthorizationError("harness_run_not_found")
            definition_id = _clean(run.get("definition_id"))
            if definition_id:
                definition = _definition_row(connection, definition_id)
                if definition is None:
                    provenance = _provenance(run)
                    if not (
                        context.is_trusted_local
                        and provenance.get("definition_kind") is None
                    ):
                        raise HarnessAuthorizationError("harness_definition_not_found")
                else:
                    authorize_definition(context, definition, "run", connection=connection)
            else:
                _run_base_access(connection, context, run, "editor")
            _require_dependencies(
                connection,
                context,
                _run_dependencies(connection, run_id),
                "editor",
            )
        return context
    except Exception:
        quarantine_run(run_id, engine=active_engine)
        raise


def can_emit_run_output(
    run_id: str,
    *,
    engine: Engine | None = None,
) -> bool:
    """Fail closed before any Harness-originated output leaves the runtime."""

    active_engine = engine or get_cached_sqlite_engine()
    try:
        revalidate_run_for_execution(run_id, engine=active_engine)
        with active_engine.connect() as connection:
            run = _run_row(connection, run_id)
            if run is None or bool(run.get("output_quarantined")):
                return False
            if _has_vault_dependency(connection, run_id):
                return False
            return bool(
                run.get("output_classification") == "member_safe"
                and run.get("member_safe_json")
            )
    except Exception:
        return False


def _definition_execution_context(
    connection: Connection,
    definition: Mapping[str, Any],
    *,
    now: int,
) -> AuthorizationContext:
    principal = _metadata(definition).get("harness_execution_principal")
    if not isinstance(principal, Mapping):
        if _clean(definition.get("project_id")):
            raise HarnessAuthorizationError("harness_principal_incomplete")
        context = trusted_local_context()
    else:
        context = _current_principal_context(connection, principal, now=now)
    authorize_definition(context, definition, "run", connection=connection)
    return context


def _run_execution_context(
    connection: Connection,
    run: Mapping[str, Any],
    *,
    now: int,
) -> AuthorizationContext:
    provenance = _provenance(run)
    principal = provenance.get("execution_principal")
    if not isinstance(principal, Mapping):
        raise HarnessAuthorizationError("harness_principal_incomplete")
    context = _current_principal_context(connection, principal, now=now)
    definition_id = _clean(provenance.get("definition_id", run.get("definition_id")))
    if definition_id:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            raise HarnessAuthorizationError("harness_definition_not_found")
        authorize_definition(context, definition, "run", connection=connection)
    else:
        _run_base_access(connection, context, run, "editor")
    _require_dependencies(
        connection,
        context,
        _run_dependencies(connection, str(run["id"])),
        "editor",
    )
    return context


def revalidate_definition_for_execution(
    definition_id: str,
    *,
    engine: Engine | None = None,
) -> AuthorizationContext:
    active_engine = engine or get_cached_sqlite_engine()
    try:
        with active_engine.begin() as connection:
            definition = _definition_row(connection, definition_id)
            if definition is None:
                raise HarnessAuthorizationError("harness_definition_not_found")
            return _definition_execution_context(
                connection,
                definition,
                now=int(time.time()),
            )
    except Exception:
        suspend_definition(definition_id, engine=active_engine)
        raise


def revoke_resource_access(
    resource_kind: str,
    resource_id: str,
    *,
    engine: Engine | None = None,
) -> dict[str, list[str]]:
    """Quarantine affected active work immediately after an ACL narrowing."""

    active_engine = engine or get_cached_sqlite_engine()
    with active_engine.connect() as connection:
        definition_ids = [
            str(row.definition_id)
            for row in connection.execute(
                select(harness_definition_dependencies.c.definition_id).where(
                    harness_definition_dependencies.c.resource_kind == resource_kind,
                    harness_definition_dependencies.c.resource_id == resource_id,
                )
            )
        ]
        run_ids = [
            str(row.run_id)
            for row in connection.execute(
                select(harness_run_dependencies.c.run_id)
                .join(agent_runs, agent_runs.c.id == harness_run_dependencies.c.run_id)
                .where(
                    harness_run_dependencies.c.resource_kind == resource_kind,
                    harness_run_dependencies.c.resource_id == resource_id,
                    agent_runs.c.status.in_(["pending", "queued", "processing", "running"]),
                )
            )
        ]
    for definition_id in definition_ids:
        suspend_definition(definition_id, engine=active_engine)
    for run_id in run_ids:
        quarantine_run(run_id, engine=active_engine)
    return {
        "definition_ids": list(dict.fromkeys(definition_ids)),
        "run_ids": list(dict.fromkeys(run_ids)),
    }


def revoke_resource_access_in_connection(
    connection: Connection,
    resource_kind: str,
    resource_id: str,
) -> dict[str, list[str]]:
    """Suspend affected work inside the transaction that narrowed an ACL."""

    candidate_definition_ids = [
        str(row.definition_id)
        for row in connection.execute(
            select(harness_definition_dependencies.c.definition_id).where(
                harness_definition_dependencies.c.resource_kind == resource_kind,
                harness_definition_dependencies.c.resource_id == resource_id,
            )
        )
    ]
    if resource_kind in {"harness_task", "harness_watch"}:
        candidate_definition_ids.append(resource_id)
    candidate_definition_ids = list(dict.fromkeys(candidate_definition_ids))
    current = int(time.time())
    definition_ids: list[str] = []
    for definition_id in candidate_definition_ids:
        definition = _definition_row(connection, definition_id)
        if definition is None:
            continue
        try:
            _definition_execution_context(
                connection,
                definition,
                now=current,
            )
        except HarnessAuthorizationError:
            definition_ids.append(definition_id)
    if definition_ids:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id.in_(definition_ids))
            .where(run_definitions.c.deleted_at.is_(None))
            .values(
                enabled=0,
                authorization_state="suspended_authorization",
                updated_at=_utc_now_iso(),
            )
        )

    candidate_run_ids = [
        str(row.run_id)
        for row in connection.execute(
            select(harness_run_dependencies.c.run_id)
            .join(agent_runs, agent_runs.c.id == harness_run_dependencies.c.run_id)
            .where(
                harness_run_dependencies.c.resource_kind == resource_kind,
                harness_run_dependencies.c.resource_id == resource_id,
                agent_runs.c.status.in_(["pending", "queued", "processing", "running"]),
            )
        )
    ]
    if resource_kind in {"harness_task", "harness_watch"}:
        candidate_run_ids.extend(
            str(row.id)
            for row in connection.execute(
                select(agent_runs.c.id).where(
                    agent_runs.c.definition_id == resource_id,
                    agent_runs.c.status.in_(["pending", "queued", "processing", "running"]),
                )
            )
        )
    if definition_ids:
        candidate_run_ids.extend(
            str(row.id)
            for row in connection.execute(
                select(agent_runs.c.id).where(
                    agent_runs.c.definition_id.in_(definition_ids),
                    agent_runs.c.status.in_(["pending", "queued", "processing", "running"]),
                )
            )
        )
    run_ids: list[str] = []
    for run_id in dict.fromkeys(candidate_run_ids):
        run = _run_row(connection, run_id)
        if run is None:
            continue
        try:
            _run_execution_context(connection, run, now=current)
        except HarnessAuthorizationError:
            run_ids.append(run_id)
    if run_ids:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id.in_(run_ids))
            .values(
                status="canceled",
                cancel_requested=1,
                cancel_requested_at=_utc_now_iso(),
                completed_at=_utc_now_iso(),
                output_quarantined=1,
                member_safe_json=None,
                safe_error_code="authorization_revoked",
                callback_status="suppressed_authorization",
                callback_error=None,
                updated_at=_utc_now_iso(),
            )
        )
    return {"definition_ids": definition_ids, "run_ids": run_ids}


def project_transcript_messages(
    context: AuthorizationContext,
    messages: Sequence[Mapping[str, Any]],
    *,
    connection: Connection,
) -> list[dict[str, Any]]:
    """Replace Harness-originated transcript content with current Run output."""

    projected: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        metadata = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
        native_id = _clean(message.get("native_message_id"))
        run_id = _clean(metadata.get("harness_run_id"))
        if run_id is None and native_id and native_id.startswith("agent_run:"):
            run_id = _clean(native_id.removeprefix("agent_run:"))
        is_harness = message.get("source") == "harness" or message.get("author") == "harness"
        if run_id is None and not is_harness:
            projected.append(message)
            continue
        if run_id is None:
            message["text"] = ""
            message["content"] = {"kind": "harness", "redacted": True}
            projected.append(message)
            continue
        run = _run_row(connection, run_id)
        if run is None:
            continue
        try:
            safe_run = serialize_run(context, run, connection=connection, operation="detail")
        except HarnessAuthorizationError:
            continue
        member_safe = safe_run.get("member_safe")
        message["text"] = (
            str(member_safe.get("text") or "") if isinstance(member_safe, Mapping) else ""
        )
        message["content"] = {
            "kind": "harness",
            "run_id": run_id,
            "redaction": safe_run.get("redaction"),
        }
        projected.append(message)
    return projected
