"""Vibe-owned Agent catalog and import helpers."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from uuid import uuid4

import yaml
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from config import paths
from storage.agent_session_rows import reserve_write_lock
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.migrations import guard_source_checkout_default_state_migration, run_migrations
from storage.models import (
    agent_runs,
    agent_sessions,
    agents,
    messages,
    run_definitions,
    scope_settings,
    state_meta,
)
from storage.session_reclaim import DEFINITION_AGENT_BINDING_REVISION_KEY
from vibe.authorization import (
    instance_owner_context,
)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAME = "default"
DEFAULT_AGENT_META_KEY = "default_agent_name"
BUILTIN_DEFAULT_AGENT_METADATA = {"builtin": True, "builtin_default": True, "lock_delete": True}
BUILTIN_BACKEND_ENABLED_META_KEY = "backend_enabled"
AGENT_ARCHIVE_METADATA_KEY = "_avibe_archive"
ARCHIVED_AGENT_NAME_PREFIX = "_"
LEGACY_ARCHIVED_AGENT_NAME_PREFIX = "_archived_"
ARCHIVED_AGENT_SLUG_LENGTH = 12
ARCHIVED_AGENT_TOKEN_LENGTH = 4
SUPPORTED_AGENT_BACKENDS = {"codex", "claude", "opencode"}
RECOMMENDED_AGENT_MODELS = {
    "claude": "claude-opus-5",
    "codex": "gpt-5.6-sol",
    "opencode": "openai/gpt-5.6-sol",
}
_UNSET = object()


class AgentUnavailableError(ValueError):
    """The named Agent cannot be used: it does not exist, or it is disabled.

    A TYPED contract for the one condition a caller may legitimately degrade for.
    Resolution can fail two ways that look identical as strings but must not be
    treated identically: the Agent is GONE (the user deleted or disabled it — a
    settled fact, so falling back to scope/default settings is the recovery), or
    the CATALOG could not be read (SQLite contention, a migration failure, a
    filesystem error — transient, and falling back would convert an infrastructure
    fault into a permanent settings change on the definition that retried).

    ``core/scheduled_tasks.py``'s preserved ``create_once`` rebind is the caller
    that made the distinction load-bearing: it catches this type NARROWLY, so an
    operational fault propagates with the definition's route and lifecycle
    untouched instead of being recorded as "settings could not be recovered".

    Subclasses ``ValueError`` so every existing ``except ValueError`` caller — the
    CLI, the UI server, the controller's route resolution — keeps its current
    behaviour and its current message.
    """

    def __init__(self, message: str, *, agent_name: str, reason: str) -> None:
        super().__init__(message)
        #: The name as requested, which for ``missing`` is all there is.
        self.agent_name = str(agent_name)
        #: ``"missing"``, ``"disabled"``, or ``"no_default"``.
        self.reason = str(reason)


class AgentArchiveError(ValueError):
    """A catalog rule refused an otherwise valid archive request."""

    def __init__(self, *, code: str, agent_name: str) -> None:
        super().__init__(code)
        self.code = code
        self.agent_name = str(agent_name)


class AgentArchivedEditError(ValueError):
    """An archived Agent is read-only on public mutation surfaces."""

    code = "agent_archived_read_only"

    def __init__(self, *, agent_name: str) -> None:
        super().__init__(self.code)
        self.agent_name = str(agent_name)


class AgentReferenceRewriteError(ValueError):
    """A durable Agent reference cannot be rewritten without losing data."""

    code = "agent_reference_metadata_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


class AgentNameValidationError(ValueError):
    """A public Agent name violates a catalog namespace rule."""

    def __init__(self, *, code: str, agent_name: str) -> None:
        super().__init__(code)
        self.code = code
        self.agent_name = str(agent_name)


class VibeAgentAccessError(PermissionError):
    """Raised when the caller is not allowed to use a Vibe Agent."""


# Default routing surfaces -- the instance-wide default Agent and a project's
# default Agent -- are resolved on behalf of whoever starts an unpinned session,
# never on behalf of whoever configured them. The invariant both surfaces share:
# a default must be usable by its entire audience, so only an audience-wide
# resource policy qualifies.
DEFAULT_ROUTING_AUDIENCE_ERROR_CODE = "agent_default_audience_restricted"

# Access levels that every principal of a routing surface's audience can resolve.
# Membership is the whole rule: an access level that is not listed here narrows
# the audience somehow, so it cannot back a default. Listing what qualifies
# rather than what does not means a future access level is rejected until it is
# deliberately declared audience-wide.
AUDIENCE_WIDE_ACCESS_LEVELS = frozenset({"public"})


class VibeAgentDefaultAudienceError(VibeAgentAccessError):
    """A default routing target is not usable by the audience it serves."""

    code = DEFAULT_ROUTING_AUDIENCE_ERROR_CODE

    def __init__(self, *, agent_name: str) -> None:
        super().__init__(self.code)
        self.agent_name = str(agent_name)


def get_agent_resource_metadata(
    connection: Connection,
    resource_id: str,
) -> dict[str, str] | None:
    """Return the safe Agent fields allowed in the hosted resource index."""

    row = connection.execute(
        select(agents.c.name, agents.c.updated_at)
        .where(agents.c.id == resource_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    return {
        "display_name": str(row["name"]),
        "updated_at": str(row["updated_at"]),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_agent_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-_")
    if not normalized:
        raise ValueError("agent name is required")
    return normalized


def _normalized_agent_reference_names(reference_names: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_agent_name(name) for name in reference_names)


def _matches_agent_reference(value: Any, reference_names: frozenset[str]) -> bool:
    try:
        return normalize_agent_name(value) in reference_names
    except ValueError:
        return False


def _owns_agent_reference(
    reference_id: Any,
    reference_name: Any,
    *,
    agent_id: str,
    reference_names: frozenset[str],
) -> bool:
    stable_id = str(reference_id or "").strip()
    if stable_id:
        return stable_id == agent_id
    return _matches_agent_reference(reference_name, reference_names)


def _validated_public_agent_name(name: str) -> tuple[str, str]:
    raw_name = str(name or "").strip()
    if raw_name.startswith("_"):
        raise AgentNameValidationError(
            code="agent_name_reserved",
            agent_name=raw_name,
        )
    if "/" in raw_name or "\\" in raw_name:
        raise AgentNameValidationError(
            code="agent_name_path_separator",
            agent_name=raw_name,
        )
    return raw_name, normalize_agent_name(raw_name)


def validate_agent_backend(backend: str) -> str:
    value = str(backend or "").strip().lower()
    if value not in SUPPORTED_AGENT_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_AGENT_BACKENDS))
        raise ValueError(f"unsupported agent backend: {backend}. Supported backends: {supported}")
    return value


def recommended_agent_model(backend: str) -> str:
    return RECOMMENDED_AGENT_MODELS[validate_agent_backend(backend)]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def resolve_resource_access_context(user_context: Any = None):
    """Resolve request ACL context while preserving local service behavior."""

    from storage import resource_access_service

    return resource_access_service.resolve_resource_access_context(user_context)


def ensure_agent_selection_access(
    connection: Connection,
    *,
    agent_name: str | None = None,
    agent_id: str | None = None,
    user_context: Any = None,
    missing_is_error: bool = False,
) -> "VibeAgent | None":
    """Resolve an Agent and require use access to that exact resource."""

    from storage import resource_access_service

    selected_name = str(agent_name or "").strip()
    selected_id = str(agent_id or "").strip()
    if not selected_name and not selected_id:
        return None

    context = resolve_resource_access_context(user_context)
    statement = select(agents)
    if selected_id:
        statement = statement.where(agents.c.id == selected_id)
    if selected_name:
        statement = statement.where(
            agents.c.normalized_name == normalize_agent_name(selected_name)
        )
    row = connection.execute(statement.limit(1)).mappings().first()
    if row is None:
        if missing_is_error:
            raise LookupError("Agent not found")
        # Owners keep the pre-catalog fallback for historical backend names.
        # Everyone else must resolve a real Agent row; a missing selector is
        # not an authorization grant.
        if context.is_instance_owner:
            return None
        raise VibeAgentAccessError("Agent access is not permitted.")

    agent = VibeAgentStore._from_row(row)
    if not resource_access_service.can_use_resource(
        context,
        "agent",
        agent.id,
        connection=connection,
    ):
        raise VibeAgentAccessError("Agent access is not permitted.")
    return agent


def ensure_agent_name_access(
    agent_name: str | None,
    *,
    user_context: Any = None,
) -> None:
    """Reject a remote task/watch binding to an inaccessible named Agent."""

    if not str(agent_name or "").strip():
        return
    context = resolve_resource_access_context(user_context)
    store = VibeAgentStore()
    try:
        try:
            store.require_accessible(str(agent_name), user_context=context)
        except ValueError:
            # Legacy local/Owner task definitions may refer to a backend name
            # that predates the Vibe Agent catalog. Keep those internal flows
            # intact; an Editor or Viewer must still resolve a real ACL row.
            if context.is_instance_owner:
                return
            raise
    finally:
        store.close()


def _require_agent_create_access(user_context: Any) -> None:
    context = resolve_resource_access_context(user_context)
    if context.can_manage_agents:
        return
    raise VibeAgentAccessError("Agent access is not permitted.")


def _require_agent_onboarding_access(user_context: Any):
    """Require instance-management access for Organization Agent publication."""

    context = resolve_resource_access_context(user_context)
    if context.can_manage_agents:
        return context
    raise VibeAgentAccessError("Agent access is not permitted.")


def _agent_onboarding_counts(inventory: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(inventory),
        "system": sum(item.get("source") in {"builtin", "system"} for item in inventory),
        "custom": sum(item.get("source") not in {"builtin", "system"} for item in inventory),
        "not_onboarded": sum(item.get("status") == "not_onboarded" for item in inventory),
        "private": sum(item.get("status") == "private" for item in inventory),
        "published": sum(item.get("status") == "published" for item in inventory),
        "conflicts": sum(item.get("status") == "managed_elsewhere" for item in inventory),
    }


def _rewrite_scope_agent_name(
    raw: str | None,
    reference_names: frozenset[str],
    new_name: str,
) -> tuple[str, bool]:
    payload = _json_loads(raw, {})
    if not isinstance(payload, dict):
        return raw or "{}", False
    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return raw or "{}", False
    changed = False
    for key in ("agent_name", "agent"):
        if _matches_agent_reference(routing.get(key), reference_names):
            routing[key] = new_name
            changed = True
    return (_json_dumps(payload), True) if changed else (raw or "{}", False)


def _rewrite_definition_agent_name(
    raw: str | None,
    reference_names: frozenset[str],
    new_name: str,
    *,
    direct_binding_changed: bool,
    revision: str,
) -> tuple[str, bool]:
    try:
        payload = json.loads("{}" if raw is None else raw)
    except (TypeError, ValueError) as exc:
        if direct_binding_changed:
            raise AgentReferenceRewriteError() from exc
        return raw or "{}", False
    if not isinstance(payload, dict):
        if direct_binding_changed:
            raise AgentReferenceRewriteError()
        return raw or "{}", False
    changed = direct_binding_changed
    snapshot = payload.get("session_settings_snapshot")
    if isinstance(snapshot, dict) and _matches_agent_reference(
        snapshot.get("agent_name"), reference_names
    ):
        snapshot["agent_name"] = new_name
        changed = True
    if not changed:
        return raw or "{}", False
    payload[DEFINITION_AGENT_BINDING_REVISION_KEY] = revision
    return _json_dumps(payload), True


def _rewrite_scheduled_agent_provenance(
    raw: str | None,
    reference_names: frozenset[str],
    new_name: str,
    *,
    agent_id: str,
) -> tuple[str, bool]:
    """Move routing names captured in scheduled submission metadata."""

    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return raw or "{}", False
    if not isinstance(payload, dict):
        return raw or "{}", False
    provenance = payload.get("scheduled_provenance")
    if not isinstance(provenance, dict):
        return raw or "{}", False
    spec = provenance.get("platform_specific")
    if not isinstance(spec, dict):
        return raw or "{}", False

    changed = False
    for key in ("vibe_agent_name", "scheduled_target_agent_name"):
        if _owns_agent_reference(
            spec.get("vibe_agent_id"),
            spec.get(key),
            agent_id=agent_id,
            reference_names=reference_names,
        ):
            spec[key] = new_name
            changed = True
    target = spec.get("agent_session_target")
    if isinstance(target, dict) and _owns_agent_reference(
        target.get("agent_id"),
        target.get("agent_name"),
        agent_id=agent_id,
        reference_names=reference_names,
    ):
        target["agent_name"] = new_name
        changed = True
    return (_json_dumps(payload), True) if changed else (raw or "{}", False)


@dataclass(frozen=True)
class VibeAgent:
    id: str
    name: str
    normalized_name: str
    backend: str
    description: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled: bool = True
    source: str = "user"
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    archived_at: Optional[str] = None
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["archived"] = self.archived_at is not None
        payload["display_name"] = archived_agent_original_name(self) or self.name
        return payload


@dataclass(frozen=True)
class AgentArchiveResult:
    agent: VibeAgent
    original_name: str
    archived_name: str
    references: dict[str, int]
    default_agent_name: Optional[str] = None


def archived_agent_original_name(agent: VibeAgent) -> Optional[str]:
    if agent.archived_at is None:
        return None
    archive_metadata = agent.metadata.get(AGENT_ARCHIVE_METADATA_KEY)
    if not isinstance(archive_metadata, dict):
        return None
    original_name = str(archive_metadata.get("original_name") or "").strip()
    return original_name or None


def agent_reference_is_usable(
    *,
    enabled: bool,
    archived_at: Optional[str],
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return whether an Agent may back an existing durable reference."""

    if enabled:
        return True
    archive_metadata = (metadata or {}).get(AGENT_ARCHIVE_METADATA_KEY)
    archived_was_enabled = (
        archive_metadata.get("was_enabled", True)
        if isinstance(archive_metadata, dict)
        else True
    )
    return archived_at is not None and bool(archived_was_enabled)


def resolve_effective_default_agent(connection, *, enabled_only: bool = True) -> VibeAgent | None:
    """Resolve the same instance-wide default used by runtime dispatch."""

    raw_name = connection.execute(
        select(state_meta.c.value_json).where(state_meta.c.key == DEFAULT_AGENT_META_KEY).limit(1)
    ).scalar_one_or_none()
    configured_name = _json_loads(raw_name, None)
    candidates = [configured_name, DEFAULT_AGENT_NAME]
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            normalized = normalize_agent_name(candidate)
        except ValueError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        statement = select(agents).where(agents.c.normalized_name == normalized)
        if enabled_only:
            statement = statement.where(agents.c.enabled == 1)
        row = connection.execute(statement.limit(1)).mappings().first()
        if row is not None:
            return VibeAgentStore._from_row(row)

    if not enabled_only:
        return None
    row = connection.execute(select(agents).where(agents.c.enabled == 1).order_by(agents.c.name).limit(1)).mappings().first()
    return VibeAgentStore._from_row(row) if row is not None else None


def default_routing_audience_error(
    connection,
    *,
    agent_id: str,
) -> str | None:
    """Return an error code when an Agent cannot serve as a default route.

    A default must be usable by its entire audience; only audience-wide policies
    qualify. Group-subset comparison is intentionally NOT implemented -- defaults
    do not get narrower audiences.

    Shared by the instance-wide default and the per-project default so both
    surfaces enforce one audience rule. Whoever starts an unpinned session
    resolves the default on their own behalf and then passes through
    ``ensure_agent_selection_access``, so any policy narrower than the surface's
    audience locks some of that audience out -- for a project that means it can
    no longer start normal sessions at all. A ``private`` policy is
    single-subject by construction; a ``scope`` policy admits only an
    intersecting group, which is narrower than a project shared with any other
    group and narrower than an instance-wide default by definition.

    Comparing a scoped policy's groups against each surface's audience is
    deliberately out of the model: a project is not an ACL resource
    (``resource_access_service.RESOURCE_KINDS``) and carries no group binding, so
    a project audience is not representable, and an instance-wide default has no
    bounded audience to compare against at all.

    The check is caller-independent. The Instance Owner bypasses ACL checks when
    *using* a resource, but that bypass describes the Owner, not the audience the
    default has to serve, so an Owner assignment is validated the same way; an
    Owner assigning an audience-wide Agent is unaffected.

    An Agent with no ACL row narrows nothing -- it is the local/builtin shape
    that predates the resource catalog, and its use is fenced elsewhere.
    """

    from storage import resource_access_service

    identifier = str(agent_id or "").strip()
    if not identifier:
        return None
    policy = resource_access_service.get_resource_policy(
        "agent",
        identifier,
        connection=connection,
    )
    if policy is None:
        return None
    if str(policy.get("access_level") or "") in AUDIENCE_WIDE_ACCESS_LEVELS:
        return None
    return DEFAULT_ROUTING_AUDIENCE_ERROR_CODE


def ensure_default_routing_audience(
    connection,
    *,
    agent_id: str,
    agent_name: str,
) -> None:
    """Raise when an Agent cannot back a default routing surface."""

    if default_routing_audience_error(connection, agent_id=agent_id) is not None:
        raise VibeAgentDefaultAudienceError(agent_name=agent_name)


def ensure_default_agent_access(
    connection,
    *,
    user_context: Any = None,
    missing_is_error: bool = False,
) -> VibeAgent | None:
    """Resolve and authorize the effective default Agent for a remote caller."""

    from storage import resource_access_service

    context = resolve_resource_access_context(user_context)
    agent = resolve_effective_default_agent(connection)
    if agent is None:
        if missing_is_error:
            raise LookupError("Default Agent not found")
        return None
    if not resource_access_service.can_use_resource(
        context,
        "agent",
        agent.id,
        connection=connection,
    ):
        raise VibeAgentAccessError("Agent access is not permitted.")
    return agent


def ensure_session_agent_access(
    connection,
    session: dict[str, Any],
    *,
    user_context: Any = None,
) -> VibeAgent | None:
    """Revalidate the Agent selected by a persisted session before dispatch."""

    context = resolve_resource_access_context(user_context)
    if session.get("agent_id") or session.get("agent_name"):
        return ensure_agent_selection_access(
            connection,
            agent_name=session.get("agent_name"),
            agent_id=session.get("agent_id"),
            user_context=context,
        )
    if not session.get("agent_backend"):
        return ensure_default_agent_access(connection, user_context=context)
    if not context.is_instance_owner:
        raise VibeAgentAccessError("Agent access is not permitted.")
    return None


@dataclass(frozen=True)
class AgentImportCandidate:
    name: str
    backend: str
    description: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    system_prompt: Optional[str] = None
    source: str = "import"
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentImportResult:
    imported: list[VibeAgent]
    skipped: list[dict[str, Any]]


class VibeAgentStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or paths.get_sqlite_state_path()
        guard_source_checkout_default_state_migration(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None:
            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        else:
            run_migrations(self.db_path)
        self.engine = create_sqlite_engine(self.db_path)
        self.prefill_missing_models()
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def maybe_reload(self) -> bool:
        return self._probe.has_external_write()

    def prefill_missing_models(self) -> int:
        """Materialize release recommendations on legacy model-less Agents."""

        now = _utc_now_iso()
        updated = 0
        with self.engine.begin() as conn:
            for backend, model in RECOMMENDED_AGENT_MODELS.items():
                result = conn.execute(
                    agents.update()
                    .where(agents.c.backend == backend)
                    .where(agents.c.archived_at.is_(None))
                    .where(agents.c.model.is_(None) | (func.trim(agents.c.model) == ""))
                    .values(model=model, updated_at=now)
                )
                updated += int(result.rowcount or 0)
        return updated

    def list_agents(
        self,
        *,
        include_disabled: bool = True,
        include_archived: bool = False,
        user_context: Any = None,
    ) -> list[VibeAgent]:
        from storage import resource_access_service

        context = resolve_resource_access_context(user_context)
        with self.engine.connect() as conn:
            stmt = select(agents).order_by(agents.c.name)
            if not include_disabled:
                if include_archived:
                    stmt = stmt.where(or_(agents.c.enabled == 1, agents.c.archived_at.is_not(None)))
                else:
                    stmt = stmt.where(agents.c.enabled == 1)
            if not include_archived:
                stmt = stmt.where(agents.c.archived_at.is_(None))
            rows = conn.execute(stmt).mappings().all()
            rows = resource_access_service.filter_accessible_resources(
                context,
                "agent",
                rows,
                connection=conn,
            )
            return [self._from_row(row) for row in rows]

    def organization_onboarding_inventory(self, *, user_context: Any = None) -> dict[str, Any]:
        """Return the safe owner inventory for explicit Organization onboarding."""

        from storage import resource_access_service

        context = _require_agent_onboarding_access(user_context)
        organization_id = context.organization_id if context.is_active_organization_member else None
        if not organization_id or not context.subject:
            return {
                "available": False,
                "organization_id": None,
                "agents": [],
                "counts": _agent_onboarding_counts([]),
            }

        with self.engine.connect() as conn:
            rows = conn.execute(select(agents).order_by(agents.c.name)).mappings().all()
            policies = {
                str(policy["resource_id"]): policy
                for policy in resource_access_service.list_resource_policies(
                    resource_kind="agent",
                    connection=conn,
                )
            }

        inventory: list[dict[str, Any]] = []
        for row in rows:
            agent = self._from_row(row)
            policy = policies.get(agent.id)
            visible_policy = policy if policy and policy.get("organization_id") == organization_id else None
            if policy is None:
                status = "not_onboarded"
            elif visible_policy is None:
                status = "managed_elsewhere"
            elif visible_policy.get("access_level") == "private":
                status = "private"
            else:
                status = "published"
            inventory.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "backend": agent.backend,
                    "source": agent.source,
                    "enabled": agent.enabled,
                    "status": status,
                    "access_level": visible_policy.get("access_level") if visible_policy else None,
                    "group_ids": list(visible_policy.get("group_ids") or []) if visible_policy else [],
                    "policy_revision": int(visible_policy.get("policy_revision") or 0) if visible_policy else None,
                    "applied_acl_revision": (
                        int(visible_policy.get("last_applied_control_plane_revision") or 0)
                        if visible_policy
                        else None
                    ),
                }
            )
        return {
            "available": True,
            "organization_id": organization_id,
            "agents": inventory,
            "counts": _agent_onboarding_counts(inventory),
        }

    def onboard_organization_agents(self, *, user_context: Any = None) -> dict[str, Any]:
        """Register every missing Agent as private without replacing any ACL."""

        from storage import resource_access_service

        context = _require_agent_onboarding_access(user_context)
        if not context.is_active_organization_member or not context.organization_id or not context.subject:
            raise VibeAgentAccessError("Agent access is not permitted.")

        created = 0
        unchanged = 0
        conflicts = 0
        with self.engine.begin() as conn:
            rows = conn.execute(select(agents).order_by(agents.c.name)).mappings().all()
            policies = {
                str(policy["resource_id"]): policy
                for policy in resource_access_service.list_resource_policies(
                    resource_kind="agent",
                    connection=conn,
                )
            }
            for row in rows:
                agent = self._from_row(row)
                existing = policies.get(agent.id)
                if existing is not None:
                    if existing.get("organization_id") == context.organization_id:
                        unchanged += 1
                    else:
                        conflicts += 1
                    continue
                resource_access_service.ensure_resource_policy(
                    conn,
                    resource_kind="agent",
                    resource_id=agent.id,
                    organization_id=context.organization_id,
                    owner_user_id=context.subject,
                    owner_email=context.email,
                    access_level="private",
                    created_by_user_id=context.subject,
                    updated_by_user_id=context.subject,
                )
                created += 1

        result = self.organization_onboarding_inventory(user_context=context)
        result.update({"created": created, "unchanged": unchanged, "conflicts": conflicts})
        return result

    def get(self, name: str) -> Optional[VibeAgent]:
        normalized = normalize_agent_name(name)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalized).limit(1)
            ).mappings().first()
            return self._from_row(row) if row else None

    def get_by_id(self, agent_id: str) -> Optional[VibeAgent]:
        cleaned = str(agent_id or "").strip()
        if not cleaned:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                select(agents).where(agents.c.id == cleaned).limit(1)
            ).mappings().first()
            return self._from_row(row) if row else None

    def require(self, name: str) -> VibeAgent:
        agent = self.get(name)
        if agent is None:
            # Same message as before, now with a type: "this Agent is gone" is the
            # only resolution failure a caller may degrade for, and it has to be
            # distinguishable from "the catalog could not be read" (which raises
            # OperationalError from ``get`` and now propagates past the narrow catch).
            raise AgentUnavailableError(f"agent '{name}' not found", agent_name=name, reason="missing")
        return agent

    def require_enabled(self, name: str) -> VibeAgent:
        agent = self.require(name)
        if not agent.enabled or agent.archived_at is not None:
            raise AgentUnavailableError(
                f"agent '{agent.name}' is disabled", agent_name=agent.name, reason="disabled"
            )
        return agent

    def require_reference(self, name: str) -> VibeAgent:
        """Resolve a durable Agent reference, including a disabled archive."""

        return self._require_reference_agent(self.require(name))

    def require_reference_by_id(self, agent_id: str) -> VibeAgent:
        """Resolve a durable Agent identity across rename/archive operations."""

        agent = self.get_by_id(agent_id)
        if agent is None:
            raise AgentUnavailableError(
                f"agent id '{agent_id}' not found",
                agent_name=str(agent_id),
                reason="missing",
            )
        return self._require_reference_agent(agent)

    @staticmethod
    def _require_reference_agent(agent: VibeAgent) -> VibeAgent:
        if not agent_reference_is_usable(
            enabled=agent.enabled,
            archived_at=agent.archived_at,
            metadata=agent.metadata,
        ):
            raise AgentUnavailableError(
                f"agent '{agent.name}' is disabled", agent_name=agent.name, reason="disabled"
            )
        return agent

    def require_accessible(self, name: str, *, user_context: Any = None, enabled_only: bool = False) -> VibeAgent:
        """Return an Agent only when the caller may use its ACL resource."""

        try:
            with self.engine.connect() as conn:
                agent = ensure_agent_selection_access(
                    conn,
                    agent_name=name,
                    user_context=user_context,
                    missing_is_error=True,
                )
        except LookupError as exc:
            raise ValueError(f"agent '{name}' not found") from exc
        assert agent is not None
        if enabled_only and not agent.enabled:
            raise ValueError(f"agent '{agent.name}' is disabled")
        return agent

    def require_manageable(self, name: str, *, user_context: Any = None) -> VibeAgent:
        """Return an Agent only when the caller may change its resource."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalize_agent_name(name)).limit(1)
            ).mappings().first()
            if row is None:
                raise ValueError(f"agent '{name}' not found")
            agent = self._from_row(row)
            return self._require_manageable_agent(
                conn,
                agent,
                user_context=user_context,
            )

    @staticmethod
    def _require_manageable_agent(
        conn: Connection,
        agent: VibeAgent,
        *,
        user_context: Any = None,
    ) -> VibeAgent:
        """Check mutation access using the transaction that owns the Agent row."""

        from storage import resource_access_service

        context = resolve_resource_access_context(user_context)
        if not resource_access_service.can_manage_resource_acl(
            context,
            "agent",
            agent.id,
            connection=conn,
        ):
            raise VibeAgentAccessError("Agent access is not permitted.")
        return agent

    def create(
        self,
        *,
        name: str,
        backend: str,
        description: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        system_prompt: Optional[str] = None,
        source: str = "user",
        source_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        enabled: bool = True,
        user_context: Any = None,
    ) -> VibeAgent:
        from storage import resource_access_service

        raw_name, normalized = _validated_public_agent_name(name)
        normalized_backend = validate_agent_backend(backend)
        context = resolve_resource_access_context(user_context)
        if source != "builtin":
            _require_agent_create_access(context)
        now = _utc_now_iso()
        agent = VibeAgent(
            id=uuid4().hex[:12],
            name=raw_name,
            normalized_name=normalized,
            backend=normalized_backend,
            description=_clean_optional(description),
            model=_clean_optional(model) or recommended_agent_model(normalized_backend),
            reasoning_effort=_clean_optional(reasoning_effort),
            system_prompt=_clean_optional(system_prompt),
            enabled=bool(enabled),
            source=str(source or "user"),
            source_ref=_clean_optional(source_ref),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(agents.insert().values(**self._values(agent)))
                # Register a private ACL for any creating subject, including a
                # Personal/email member. Organization *use* still follows the
                # stored policy; omitting the row hid the Agent from its creator
                # because missing-policy fails closed for non-owners.
                if source != "builtin" and context.subject:
                    resource_access_service.ensure_resource_policy(
                        conn,
                        resource_kind="agent",
                        resource_id=agent.id,
                        organization_id=context.organization_id,
                        owner_user_id=context.subject,
                        owner_email=context.email,
                        access_level="private",
                        created_by_user_id=context.subject,
                        updated_by_user_id=context.subject,
                    )
        except IntegrityError as exc:
            raise ValueError(f"agent '{name}' already exists") from exc
        return agent

    def update(
        self,
        name: str,
        *,
        description: Any = _UNSET,
        model: Any = _UNSET,
        reasoning_effort: Any = _UNSET,
        system_prompt: Any = _UNSET,
        metadata: Any = _UNSET,
        enabled: Any = _UNSET,
        user_context: Any = None,
    ) -> VibeAgent:
        normalized = normalize_agent_name(name)
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalized).limit(1)
            ).mappings().first()
            if row is None:
                raise AgentUnavailableError(
                    f"agent '{name}' not found", agent_name=name, reason="missing"
                )
            existing = self._from_row(row)
            self._require_manageable_agent(
                conn,
                existing,
                user_context=user_context,
            )
            if existing.archived_at is not None:
                raise AgentArchivedEditError(agent_name=name)

            values: dict[str, Any] = {"updated_at": _utc_now_iso()}
            if description is not _UNSET:
                values["description"] = _clean_optional(description)
            if model is not _UNSET:
                values["model"] = _clean_optional(model) or recommended_agent_model(existing.backend)
            if reasoning_effort is not _UNSET:
                values["reasoning_effort"] = _clean_optional(reasoning_effort)
            if system_prompt is not _UNSET:
                values["system_prompt"] = _clean_optional(system_prompt)
            if metadata is not _UNSET:
                values["metadata_json"] = _json_dumps(dict(metadata or {}))
            if enabled is not _UNSET:
                values["enabled"] = 1 if bool(enabled) else 0
            result = conn.execute(
                agents.update()
                .where(agents.c.id == existing.id)
                .where(agents.c.archived_at.is_(None))
                .values(**values)
            )
            if result.rowcount != 1:
                raise AgentArchivedEditError(agent_name=name)
            updated = conn.execute(
                select(agents).where(agents.c.id == existing.id).limit(1)
            ).mappings().one()
            return self._from_row(updated)

    def set_enabled(self, name: str, enabled: bool, *, user_context: Any = None) -> VibeAgent:
        return self.update(name, enabled=enabled, user_context=user_context)

    def rename(
        self,
        name: str,
        new_name: str,
        *,
        user_context: Any = None,
    ) -> VibeAgent:
        raw_new_name, new_normalized = _validated_public_agent_name(new_name)
        old_normalized = normalize_agent_name(name)
        now = _utc_now_iso()
        try:
            with self.engine.begin() as conn:
                reserve_write_lock(conn)
                row = conn.execute(
                    select(agents).where(agents.c.normalized_name == old_normalized).limit(1)
                ).mappings().first()
                if row is None:
                    raise AgentUnavailableError(
                        f"agent '{name}' not found", agent_name=name, reason="missing"
                )
                agent = self._from_row(row)
                self._require_manageable_agent(
                    conn,
                    agent,
                    user_context=user_context,
                )
                if agent.archived_at is not None:
                    raise AgentArchivedEditError(agent_name=name)
                if is_builtin_default_agent(agent):
                    raise ValueError(f"agent '{agent.name}' is built in and cannot be renamed")
                if new_normalized == agent.normalized_name:
                    if raw_new_name == agent.name:
                        return agent
                collision = conn.execute(
                    select(agents.c.id)
                    .where(agents.c.normalized_name == new_normalized)
                    .where(agents.c.id != agent.id)
                    .limit(1)
                ).first()
                if collision is not None:
                    raise ValueError(f"agent '{new_name}' already exists")

                conn.execute(
                    agents.update()
                    .where(agents.c.id == agent.id)
                    .values(name=raw_new_name, normalized_name=new_normalized, updated_at=now)
                )
                self._rewrite_references(
                    conn,
                    agent_id=agent.id,
                    reference_names=frozenset((agent.name, agent.normalized_name)),
                    new_name=raw_new_name,
                    revision=now,
                )
                default_name = self._default_agent_name(conn)
                try:
                    default_matches = (
                        default_name is not None
                        and normalize_agent_name(default_name) == agent.normalized_name
                    )
                except ValueError:
                    default_matches = False
                if default_matches:
                    self._write_default_agent_name(conn, raw_new_name, now=now)
                updated = conn.execute(
                    select(agents).where(agents.c.id == agent.id).limit(1)
                ).mappings().one()
        except IntegrityError as exc:
            raise ValueError(f"agent '{new_name}' already exists") from exc
        return self._from_row(updated)

    def archive(
        self,
        name: str,
        *,
        user_context: Any = None,
    ) -> Optional[AgentArchiveResult]:
        normalized = normalize_agent_name(name)
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalized).limit(1)
            ).mappings().first()
            if row is None:
                legacy = self._legacy_archive_for_original_name(conn, normalized)
                if legacy is None:
                    return None
                self._require_manageable_agent(
                    conn,
                    legacy,
                    user_context=user_context,
                )
                return self._compact_legacy_archive(conn, legacy, now=now)
            agent = self._from_row(row)
            self._require_manageable_agent(
                conn,
                agent,
                user_context=user_context,
            )
            if is_builtin_default_agent(agent):
                raise AgentArchiveError(
                    code="agent_builtin",
                    agent_name=agent.name,
                )
            if agent.archived_at is not None:
                if agent.name.startswith(LEGACY_ARCHIVED_AGENT_NAME_PREFIX):
                    return self._compact_legacy_archive(conn, agent, now=now)
                raise AgentArchiveError(
                    code="agent_already_archived",
                    agent_name=agent.name,
                )

            default_name = self._default_agent_name(conn)
            effective_default = self._effective_default_agent(conn)
            explicit_default_matches = (
                default_name is not None
                and normalize_agent_name(default_name) == agent.normalized_name
            )
            replacement = None
            if effective_default is not None and effective_default.id == agent.id:
                replacement = self._archive_default_replacement(conn, agent)
                if replacement is None:
                    raise AgentArchiveError(
                        code="agent_no_default_replacement",
                        agent_name=agent.name,
                    )
            elif explicit_default_matches:
                replacement = effective_default

            archived_name, archived_normalized = self._available_archive_name(
                conn,
                normalized_name=agent.normalized_name,
            )

            reference_names = frozenset((agent.name, agent.normalized_name))
            references = self._reference_counts(conn, reference_names, agent_id=agent.id)
            archive_metadata = {
                "original_name": agent.name,
                "archived_at": now,
                "was_enabled": agent.enabled,
            }
            metadata = {**agent.metadata, AGENT_ARCHIVE_METADATA_KEY: archive_metadata}
            conn.execute(
                agents.update()
                .where(agents.c.id == agent.id)
                .values(
                    name=archived_name,
                    normalized_name=archived_normalized,
                    enabled=0,
                    metadata_json=_json_dumps(metadata),
                    archived_at=now,
                    updated_at=now,
                )
            )
            self._rewrite_references(
                conn,
                agent_id=agent.id,
                reference_names=reference_names,
                new_name=archived_name,
                revision=now,
            )
            if replacement is not None:
                self._write_default_agent_name(conn, replacement.name, now=now)
                remaining_default_name = replacement.name
            elif explicit_default_matches:
                conn.execute(state_meta.delete().where(state_meta.c.key == DEFAULT_AGENT_META_KEY))
                remaining_default_name = None
            else:
                remaining_default_name = default_name

            archived_agent = VibeAgent(
                **{
                    **asdict(agent),
                    "name": archived_name,
                    "normalized_name": archived_normalized,
                    "enabled": False,
                    "metadata": metadata,
                    "archived_at": now,
                    "updated_at": now,
                }
            )
            return AgentArchiveResult(
                agent=archived_agent,
                original_name=agent.name,
                archived_name=archived_name,
                references=references,
                default_agent_name=remaining_default_name,
            )

    @staticmethod
    def _available_archive_name(conn: Any, *, normalized_name: str) -> tuple[str, str]:
        slug = str(normalized_name or "agent").strip("-_")[:ARCHIVED_AGENT_SLUG_LENGTH] or "agent"
        while True:
            token = uuid4().hex[:ARCHIVED_AGENT_TOKEN_LENGTH]
            archived_name = f"{ARCHIVED_AGENT_NAME_PREFIX}{slug}-{token}"
            archived_normalized = normalize_agent_name(archived_name)
            collision = conn.execute(
                select(agents.c.id).where(agents.c.normalized_name == archived_normalized).limit(1)
            ).first()
            if collision is None:
                return archived_name, archived_normalized

    @staticmethod
    def _legacy_archive_for_original_name(conn: Any, normalized_name: str) -> Optional[VibeAgent]:
        rows = conn.execute(
            select(agents)
            .where(agents.c.archived_at.is_not(None))
            .order_by(agents.c.updated_at.desc())
        ).mappings()
        for row in rows:
            agent = VibeAgentStore._from_row(row)
            if not agent.name.startswith(LEGACY_ARCHIVED_AGENT_NAME_PREFIX):
                continue
            original_name = archived_agent_original_name(agent)
            if original_name and normalize_agent_name(original_name) == normalized_name:
                return agent
        return None

    def _compact_legacy_archive(
        self,
        conn: Any,
        agent: VibeAgent,
        *,
        now: str,
    ) -> AgentArchiveResult:
        original_name = archived_agent_original_name(agent) or agent.name
        archived_name, archived_normalized = self._available_archive_name(
            conn,
            normalized_name=normalize_agent_name(original_name),
        )
        reference_names = frozenset((agent.name, agent.normalized_name))
        references = self._reference_counts(conn, reference_names, agent_id=agent.id)
        conn.execute(
            agents.update()
            .where(agents.c.id == agent.id)
            .values(
                name=archived_name,
                normalized_name=archived_normalized,
                updated_at=now,
            )
        )
        self._rewrite_references(
            conn,
            agent_id=agent.id,
            reference_names=reference_names,
            new_name=archived_name,
            revision=now,
        )
        archived_agent = VibeAgent(
            **{
                **asdict(agent),
                "name": archived_name,
                "normalized_name": archived_normalized,
                "updated_at": now,
            }
        )
        return AgentArchiveResult(
            agent=archived_agent,
            original_name=original_name,
            archived_name=archived_name,
            references=references,
            default_agent_name=self._default_agent_name(conn),
        )

    def remove(self, name: str, *, user_context: Any = None) -> bool:
        from storage import resource_access_service

        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agents)
                .where(agents.c.normalized_name == normalize_agent_name(name))
                .limit(1)
            ).mappings().first()
            if row is None:
                return False
            agent = self._from_row(row)
            self._require_manageable_agent(
                conn,
                agent,
                user_context=user_context,
            )
            if is_builtin_default_agent(agent):
                raise ValueError(f"agent '{agent.name}' is built in and cannot be deleted")
            result = conn.execute(agents.delete().where(agents.c.id == agent.id))
            if result.rowcount:
                resource_access_service.delete_resource_policy(conn, "agent", agent.id)
            return bool(result.rowcount)

    def reference_counts(self, name: str) -> dict[str, int]:
        agent = self.get(name)
        if agent is None:
            return {}
        with self.engine.connect() as conn:
            return self._reference_counts(
                conn,
                frozenset((agent.name, agent.normalized_name)),
                agent_id=agent.id,
            )

    @staticmethod
    def _reference_counts(
        conn: Any,
        reference_names: frozenset[str],
        *,
        agent_id: str,
    ) -> dict[str, int]:
        reference_names = _normalized_agent_reference_names(reference_names)
        scope_names = conn.execute(
            select(scope_settings.c.agent_name).where(scope_settings.c.agent_name.is_not(None))
        ).scalars()
        session_rows = conn.execute(
            select(agent_sessions.c.agent_id, agent_sessions.c.agent_name)
        ).mappings()
        definition_names = conn.execute(
            select(run_definitions.c.agent_name)
            .where(run_definitions.c.agent_name.is_not(None))
            .where(run_definitions.c.deleted_at.is_(None))
        ).scalars()
        return {
            "scopes": sum(_matches_agent_reference(name, reference_names) for name in scope_names),
            "sessions": sum(
                _owns_agent_reference(
                    row["agent_id"],
                    row["agent_name"],
                    agent_id=agent_id,
                    reference_names=reference_names,
                )
                for row in session_rows
            ),
            "definitions": sum(
                _matches_agent_reference(name, reference_names) for name in definition_names
            ),
        }

    @staticmethod
    def _rewrite_references(
        conn: Any,
        *,
        agent_id: str,
        reference_names: frozenset[str],
        new_name: str,
        revision: str,
    ) -> None:
        reference_names = _normalized_agent_reference_names(reference_names)
        session_ids = [
            row["id"]
            for row in conn.execute(
                select(agent_sessions.c.id, agent_sessions.c.agent_id, agent_sessions.c.agent_name)
            ).mappings()
            if _owns_agent_reference(
                row["agent_id"],
                row["agent_name"],
                agent_id=agent_id,
                reference_names=reference_names,
            )
        ]
        if session_ids:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id.in_(session_ids))
                .values(agent_id=agent_id, agent_name=new_name)
            )

        run_ids = [
            row["id"]
            for row in conn.execute(
                select(agent_runs.c.id, agent_runs.c.agent_id, agent_runs.c.agent_name)
                .where(agent_runs.c.status.in_(("pending", "queued", "processing", "running")))
            ).mappings()
            if _owns_agent_reference(
                row["agent_id"],
                row["agent_name"],
                agent_id=agent_id,
                reference_names=reference_names,
            )
        ]
        if run_ids:
            conn.execute(
                agent_runs.update()
                .where(agent_runs.c.id.in_(run_ids))
                .values(agent_id=agent_id, agent_name=new_name)
            )

        scope_rows = conn.execute(
            select(scope_settings.c.scope_id, scope_settings.c.agent_name, scope_settings.c.settings_json)
        ).mappings().all()
        for row in scope_rows:
            values: dict[str, Any] = {}
            if _matches_agent_reference(row["agent_name"], reference_names):
                values["agent_name"] = new_name
            settings, changed = _rewrite_scope_agent_name(
                row["settings_json"], reference_names, new_name
            )
            if changed:
                values["settings_json"] = settings
            if values:
                conn.execute(
                    scope_settings.update()
                    .where(scope_settings.c.scope_id == row["scope_id"])
                    .values(**values)
                )

        definition_rows = conn.execute(
            select(run_definitions.c.id, run_definitions.c.agent_name, run_definitions.c.metadata_json)
            .where(run_definitions.c.deleted_at.is_(None))
        ).mappings().all()
        for row in definition_rows:
            values = {}
            direct_binding_changed = _matches_agent_reference(row["agent_name"], reference_names)
            if direct_binding_changed:
                values["agent_name"] = new_name
            metadata, changed = _rewrite_definition_agent_name(
                row["metadata_json"],
                reference_names,
                new_name,
                direct_binding_changed=direct_binding_changed,
                revision=revision,
            )
            if changed:
                values["metadata_json"] = metadata
            if values:
                conn.execute(
                    run_definitions.update()
                    .where(run_definitions.c.id == row["id"])
                    .values(**values)
                )

    @staticmethod
    def _default_agent_name(conn: Any) -> Optional[str]:
        value = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == DEFAULT_AGENT_META_KEY).limit(1)
        ).scalar_one_or_none()
        payload = _json_loads(value, None)
        return str(payload).strip() if payload else None

    @staticmethod
    def _write_default_agent_name(conn: Any, name: str, *, now: str) -> None:
        conn.execute(state_meta.delete().where(state_meta.c.key == DEFAULT_AGENT_META_KEY))
        conn.execute(
            state_meta.insert().values(
                key=DEFAULT_AGENT_META_KEY,
                value_json=_json_dumps(name),
                updated_at=now,
            )
        )

    @staticmethod
    def _archive_default_replacement(conn: Any, archived: VibeAgent) -> Optional[VibeAgent]:
        rows = conn.execute(
            select(agents)
            .where(agents.c.id != archived.id)
            .where(agents.c.enabled == 1)
            .where(agents.c.archived_at.is_(None))
            .order_by(agents.c.name)
        ).mappings()
        candidates = [VibeAgentStore._from_row(row) for row in rows]
        for candidate in candidates:
            if candidate.backend == archived.backend and is_builtin_default_agent(candidate):
                return candidate
        for candidate in candidates:
            if candidate.backend == archived.backend:
                return candidate
        return candidates[0] if candidates else None

    @staticmethod
    def _effective_default_agent(conn: Any) -> Optional[VibeAgent]:
        explicit_name = VibeAgentStore._default_agent_name(conn)
        if explicit_name:
            explicit = conn.execute(
                select(agents)
                .where(agents.c.normalized_name == normalize_agent_name(explicit_name))
                .where(agents.c.enabled == 1)
                .where(agents.c.archived_at.is_(None))
                .limit(1)
            ).mappings().first()
            if explicit is not None:
                return VibeAgentStore._from_row(explicit)

        fallback = conn.execute(
            select(agents)
            .where(agents.c.normalized_name == normalize_agent_name(DEFAULT_AGENT_NAME))
            .where(agents.c.enabled == 1)
            .where(agents.c.archived_at.is_(None))
            .limit(1)
        ).mappings().first()
        if fallback is not None:
            return VibeAgentStore._from_row(fallback)

        first_enabled = conn.execute(
            select(agents)
            .where(agents.c.enabled == 1)
            .where(agents.c.archived_at.is_(None))
            .order_by(agents.c.name)
            .limit(1)
        ).mappings().first()
        return VibeAgentStore._from_row(first_enabled) if first_enabled is not None else None

    def import_candidates(
        self,
        candidates: Iterable[AgentImportCandidate],
        *,
        user_context: Any = None,
    ) -> AgentImportResult:
        imported: list[VibeAgent] = []
        skipped: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                if self.get(candidate.name):
                    skipped.append({"name": candidate.name, "reason": "name_conflict"})
                    continue
                imported.append(
                    self.create(
                        name=candidate.name,
                        backend=candidate.backend,
                        description=candidate.description,
                        model=candidate.model,
                        reasoning_effort=candidate.reasoning_effort,
                        system_prompt=candidate.system_prompt,
                        source=candidate.source,
                        source_ref=candidate.source_ref,
                        metadata=candidate.metadata,
                        user_context=user_context,
                    )
                )
            except VibeAgentAccessError:
                raise
            except Exception as exc:
                skipped.append({"name": candidate.name, "reason": "invalid", "error": str(exc)})
        return AgentImportResult(imported=imported, skipped=skipped)

    def ensure_default_agent(self, *, backend: str = "claude") -> VibeAgent:
        existing = self.get(DEFAULT_AGENT_NAME)
        if existing:
            self.set_default_agent_name(existing.name, user_context=instance_owner_context())
            return existing
        agent = self.create(
            name=DEFAULT_AGENT_NAME,
            backend=backend,
            description="Default avibe agent.",
            source="builtin",
            metadata={"builtin": True},
            enabled=True,
        )
        self.set_default_agent_name(agent.name, user_context=instance_owner_context())
        return agent

    def ensure_builtin_default_agent(self, *, backend: str, name: str | None = None) -> VibeAgent:
        backend = validate_agent_backend(backend)
        agent_name = str(name or backend).strip()
        metadata = dict(BUILTIN_DEFAULT_AGENT_METADATA)
        metadata["backend"] = backend
        existing = self.get(agent_name)
        if existing:
            if existing.backend != backend:
                raise ValueError(
                    f"agent '{agent_name}' already exists with backend '{existing.backend}', "
                    f"cannot use it as the built-in default for '{backend}'"
                )
            if not is_builtin_default_agent(existing):
                return existing
            merged = {**existing.metadata, **metadata}
            if existing.source != "builtin" or existing.metadata != merged:
                return self.update(
                    existing.name,
                    metadata=merged,
                    user_context=instance_owner_context(),
                )
            return existing
        return self.create(
            name=agent_name,
            backend=backend,
            description=f"Default Agent for the {backend} backend.",
            source="builtin",
            metadata=metadata,
            enabled=True,
        )

    def sync_builtin_default_agent(self, *, backend: str, backend_enabled: bool, name: str | None = None) -> VibeAgent:
        backend = validate_agent_backend(backend)
        agent = self.ensure_builtin_default_agent(backend=backend, name=name)
        if not is_builtin_default_agent(agent):
            return agent

        previous_backend_enabled = agent.metadata.get(BUILTIN_BACKEND_ENABLED_META_KEY)
        metadata = {**agent.metadata, BUILTIN_BACKEND_ENABLED_META_KEY: bool(backend_enabled)}
        should_enable = bool(backend_enabled) and previous_backend_enabled is not True
        should_disable = not bool(backend_enabled) and agent.enabled
        updates: dict[str, Any] = {}
        if metadata != agent.metadata:
            updates["metadata"] = metadata
        if should_enable:
            updates["enabled"] = True
        elif should_disable:
            updates["enabled"] = False
        if updates:
            return self.update(
                agent.name,
                user_context=instance_owner_context(),
                **updates,
            )
        return agent

    def ensure_builtin_default_agents(
        self,
        backends: Iterable[str],
    ) -> list[VibeAgent]:
        ensured: list[VibeAgent] = []
        enabled_backends: list[str] = []
        for backend in backends:
            normalized_backend = validate_agent_backend(backend)
            if normalized_backend not in enabled_backends:
                enabled_backends.append(normalized_backend)
        enabled_backend_set = set(enabled_backends)
        for backend in enabled_backends:
            try:
                ensured.append(self.sync_builtin_default_agent(backend=backend, backend_enabled=True))
            except ValueError as exc:
                logger.warning("Skipping built-in default Agent for backend %s: %s", backend, exc)
        with self.engine.connect() as conn:
            rows = conn.execute(select(agents)).mappings().all()
        for row in rows:
            agent = self._from_row(row)
            if (
                is_builtin_default_agent(agent)
                and agent.backend not in enabled_backend_set
            ):
                self.sync_builtin_default_agent(backend=agent.backend, backend_enabled=False, name=agent.name)
        default_name = self.get_default_agent_name()
        default_agent = self.get(default_name) if default_name else None
        enabled_ensured = [agent for agent in ensured if agent.enabled]
        if (default_agent is None or not default_agent.enabled) and enabled_ensured:
            self.set_default_agent_name(
                enabled_ensured[0].name,
                user_context=instance_owner_context(),
            )
        return ensured

    def get_builtin_default_agent_for_backend(self, backend: str, *, enabled_only: bool = True) -> Optional[VibeAgent]:
        backend = validate_agent_backend(backend)
        for candidate in (backend, DEFAULT_AGENT_NAME):
            agent = self.get(candidate)
            if (
                agent
                and agent.backend == backend
                and is_builtin_default_agent(agent)
                and (agent.enabled or not enabled_only)
            ):
                return agent
        with self.engine.connect() as conn:
            rows = conn.execute(select(agents).where(agents.c.backend == backend).order_by(agents.c.name)).mappings()
            for row in rows:
                agent = self._from_row(row)
                if is_builtin_default_agent(agent) and (agent.enabled or not enabled_only):
                    return agent
        return None

    def get_default_agent_name(self) -> Optional[str]:
        with self.engine.connect() as conn:
            return self._default_agent_name(conn)

    def set_default_agent_name(
        self,
        name: str,
        *,
        user_context: Any = None,
    ) -> None:
        normalized = normalize_agent_name(name)
        context = resolve_resource_access_context(user_context)
        if not context.can_manage_access_members:
            raise VibeAgentAccessError("Agent access is not permitted.")
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalized).limit(1)
            ).mappings().first()
            if row is None:
                raise AgentUnavailableError(
                    f"agent '{name}' not found", agent_name=name, reason="missing"
                )
            agent = self._from_row(row)
            self._require_manageable_agent(
                conn,
                agent,
                user_context=user_context,
            )
            if not agent.enabled or agent.archived_at is not None:
                raise AgentUnavailableError(
                    f"agent '{agent.name}' is disabled",
                    agent_name=agent.name,
                    reason="disabled",
                )
            ensure_default_routing_audience(
                conn,
                agent_id=agent.id,
                agent_name=agent.name,
            )
            self._write_default_agent_name(conn, agent.name, now=now)

    def get_default_agent(self, *, enabled_only: bool = True) -> Optional[VibeAgent]:
        with self.engine.connect() as conn:
            return resolve_effective_default_agent(conn, enabled_only=enabled_only)

    @staticmethod
    def _from_row(row: Any) -> VibeAgent:
        return VibeAgent(
            id=row["id"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            backend=row["backend"],
            description=row["description"],
            model=row["model"],
            reasoning_effort=row["reasoning_effort"],
            system_prompt=row["system_prompt"],
            enabled=bool(row["enabled"]),
            source=row["source"],
            source_ref=row["source_ref"],
            metadata=_json_loads(row["metadata_json"], {}),
            archived_at=row.get("archived_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _values(agent: VibeAgent) -> dict[str, Any]:
        return {
            "id": agent.id,
            "name": agent.name,
            "normalized_name": agent.normalized_name,
            "description": agent.description,
            "backend": agent.backend,
            "model": agent.model,
            "reasoning_effort": agent.reasoning_effort,
            "system_prompt": agent.system_prompt,
            "enabled": 1 if agent.enabled else 0,
            "source": agent.source,
            "source_ref": agent.source_ref,
            "metadata_json": _json_dumps(agent.metadata),
            "archived_at": agent.archived_at,
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
        }


def parse_agent_file(path: Path, *, backend: str) -> AgentImportCandidate:
    backend = validate_agent_backend(backend)
    raw = path.read_text(encoding="utf-8")
    header: dict[str, Any] = {}
    body = raw.strip()
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            header = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
    name = str(header.get("name") or path.stem).strip()
    description = header.get("description")
    model = header.get("model")
    reasoning_effort = header.get("reasoning_effort") or header.get("reasoningEffort")
    metadata = {
        key: value
        for key, value in header.items()
        if key not in {"name", "description", "model", "reasoning_effort", "reasoningEffort"}
    }
    return AgentImportCandidate(
        name=name,
        backend=backend,
        description=str(description).strip() if description else None,
        model=str(model).strip() if model else None,
        reasoning_effort=str(reasoning_effort).strip() if reasoning_effort else None,
        system_prompt=body or None,
        source="file",
        source_ref=str(path),
        metadata=metadata,
    )


def is_builtin_default_agent(agent: VibeAgent) -> bool:
    return bool(agent.metadata.get("builtin_default") or agent.metadata.get("lock_delete"))


def iter_global_agent_files(source: str) -> list[tuple[Path, str]]:
    source_key = str(source or "").strip().lower()
    home = Path.home()
    if source_key == "claude":
        return [(path, "claude") for path in sorted((home / ".claude" / "agents").glob("*.md"))]
    if source_key == "codex":
        search_dirs = [home / ".codex" / "agents"]
        return [(path, "codex") for directory in search_dirs for path in sorted(directory.glob("*.md"))]
    if source_key == "opencode":
        search_dirs = [
            home / ".config" / "opencode" / "agent",
            home / ".config" / "opencode" / "agents",
        ]
        return [(path, "opencode") for directory in search_dirs for path in sorted(directory.glob("*.md"))]
    raise ValueError("--from must be one of: claude, codex, opencode")


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
