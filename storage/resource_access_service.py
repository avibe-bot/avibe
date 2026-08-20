"""Local organization resource-policy state and evaluation helpers.

The control plane owns desired ACL intents. This module owns the local applied
policy used by future resource services, and deliberately stores no resource
content, prompts, paths, outputs, or secret values.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import select, update
from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.engine import Connection

from storage.db import get_cached_sqlite_engine
from storage.models import (
    agent_runs,
    message_deliveries,
    resource_access_groups,
    resource_access_policies,
    run_definitions,
    state_meta,
)
from vibe.authorization import (
    AuthorizationContext,
    context_from_session_payload,
    instance_owner_context,
)


RESOURCE_KINDS = frozenset({"agent", "vault_secret", "skill"})
ACCESS_LEVELS = frozenset({"public", "scope", "private"})
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
RESOURCE_USER_CONTEXT_METADATA_KEY = "resource_user_context"
LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY = "migrations.legacy_deferred_resource_contexts.v1"
RESOURCE_ORGANIZATIONS_META_KEY = "resource_access_organizations"
HARNESS_ACCESS_FORBIDDEN_CODE = "harness_access_forbidden"
RESOURCE_BINDING_STATE_UNAVAILABLE = "unavailable"
RESOURCE_BINDING_STATE_UNPAIRED = "unpaired"
RESOURCE_BINDING_STATE_PARTIAL = "partial"
RESOURCE_BINDING_STATE_READY = "ready"
RESOURCE_BINDING_STATE_SEALED = "sealed"
RESOURCE_BINDING_STATUS_KEY = "binding_status"
_CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE = object()


@dataclass(frozen=True)
class ConfiguredResourceState:
    """One coherent read of the pairing identity used by deferred resources."""

    status: str
    instance_id: str | None
    instance_kind: str | None
    runtime_ready: bool


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
        "vibe_instance_id": context.instance_id,
        "vibe_instance_role": context.instance_role,
        "vibe_instance_access_source": context.instance_access_source,
        "vibe_instance_kind": context.instance_kind,
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
    context = current_resource_context(snapshot, is_remote=True)

    # A persisted context is durable across re-pairing, so a Personal snapshot
    # must not authorize work after this installation moves to another
    # instance. Compare the server-owned binding whenever the current pairing
    # exposes one; legacy snapshots without an instance id remain unbound.
    configured = _configured_resource_instance()
    if context.instance_kind is not None and (
        configured is _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE or configured is None
    ):
        return None
    if configured is not _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE and configured is not None:
        paired_instance_id, paired_kind = configured
        from storage import remote_access_authorization_service

        ready = remote_access_authorization_service.binding_is_ready_for_pairing(
            instance_id=paired_instance_id,
            instance_kind=paired_kind,
            ensure=False,
            # Never bootstrap from this call: it can run while a SQLite writer
            # lock is already held (queued-delivery recheck). Opening a second
            # write connection here deadlocks; the interactive/UI path hoists
            # bootstrap via remote_access.binding_is_ready(..., bootstrap=True).
            bootstrap=False,
        )
        # An absent durable row is an upgrade artifact, not evidence the
        # pairing is wrong. Identity matching below still applies; the
        # authorization consumers (binding_is_ready) fail closed / bootstrap
        # on their own path. Only an explicit reconciling/mismatched row
        # means "do not project a Personal bypass from this snapshot".
        state = remote_access_authorization_service.load_instance_binding_state(
            ensure=False
        )
        if not ready and state is not None:
            return None
        if context.instance_kind is not None and (
            not paired_instance_id or not paired_kind
        ):
            return None
        snapshot_instance_id = context.instance_id
        if (
            snapshot_instance_id
            and paired_instance_id
            and snapshot_instance_id != paired_instance_id
        ):
            return None
        if context.instance_kind and paired_kind and context.instance_kind != paired_kind:
            return None
    if context.instance_kind is not None or snapshot.get("vibe_instance_kind") is not None:
        return context

    # Released snapshots predate the instance-kind field. Recover their kind
    # only when the snapshot still names the current server-owned instance. An
    # unbound legacy snapshot cannot be attributed to a re-paired Personal
    # instance, because doing so would turn old Organization work into a
    # Personal ACL bypass.
    paired_kind = (
        configured[1]
        if configured is not _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
        and configured is not None
        else None
    )
    if paired_kind not in {"personal", "organization"}:
        return context
    if not context.instance_id:
        return None
    return replace(context, instance_kind=paired_kind)


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


def _configured_resource_state() -> ConfiguredResourceState:
    """Return one typed, fail-closed read of the configured pairing state.

    ``UNAVAILABLE`` is intentionally distinct from a successfully read
    unpaired configuration. Migration provenance must never be sealed because
    a transient config read failed.
    """

    try:
        from config.v2_config import V2Config

        loaded = V2Config.load()
        # A recovered/defaulted load is not an authoritative unpaired state:
        # the file is broken and the user will repair it. Treat it as a
        # transient UNAVAILABLE so we never seal snapshots against empty
        # recovery defaults.
        if getattr(loaded, "load_warnings", ()):
            return ConfiguredResourceState(
                status=RESOURCE_BINDING_STATE_UNAVAILABLE,
                instance_id=None,
                instance_kind=None,
                runtime_ready=False,
            )
        cloud = loaded.remote_access.vibe_cloud
        raw_instance_id = cloud.instance_id
        instance_id = _clean_optional_string(raw_instance_id)
        if raw_instance_id not in (None, "") and instance_id is None:
            return ConfiguredResourceState(
                status=RESOURCE_BINDING_STATE_UNAVAILABLE,
                instance_id=None,
                instance_kind=None,
                runtime_ready=False,
            )
        instance_kind = (
            cloud.instance_kind if cloud.instance_kind in {"personal", "organization"} else None
        )
        credentials = cloud.runtime_credentials()
    except FileNotFoundError:
        # A missing config file is an authoritative unpaired install, not a
        # transient read failure. pair() must be allowed to redeem.
        return ConfiguredResourceState(
            status=RESOURCE_BINDING_STATE_UNPAIRED,
            instance_id=None,
            instance_kind=None,
            runtime_ready=False,
        )
    except Exception:
        return ConfiguredResourceState(
            status=RESOURCE_BINDING_STATE_UNAVAILABLE,
            instance_id=None,
            instance_kind=None,
            runtime_ready=False,
        )

    runtime_ready = False
    if credentials is not None:
        if not isinstance(credentials, (tuple, list)) or len(credentials) != 3:
            return ConfiguredResourceState(
                status=RESOURCE_BINDING_STATE_UNAVAILABLE,
                instance_id=instance_id,
                instance_kind=instance_kind,
                runtime_ready=False,
            )
        credential_instance_id = _clean_optional_string(credentials[1])
        runtime_ready = instance_id is not None and credential_instance_id == instance_id

    if instance_id is None:
        return ConfiguredResourceState(
            status=RESOURCE_BINDING_STATE_UNPAIRED,
            instance_id=None,
            instance_kind=instance_kind,
            runtime_ready=False,
        )
    if runtime_ready and instance_kind in {"personal", "organization"}:
        status = RESOURCE_BINDING_STATE_READY
    else:
        status = RESOURCE_BINDING_STATE_PARTIAL
    return ConfiguredResourceState(
        status=status,
        instance_id=instance_id,
        instance_kind=instance_kind,
        runtime_ready=runtime_ready,
    )


def _configured_resource_instance() -> tuple[str | None, str | None] | None | object:
    """Return the current complete pairing identity used to fence deferred resources."""

    configured = _configured_resource_state()
    if configured.status == RESOURCE_BINDING_STATE_UNAVAILABLE:
        return _CONFIGURED_SHOW_PAGE_INSTANCE_UNAVAILABLE
    if configured.status != RESOURCE_BINDING_STATE_READY:
        return None
    return configured.instance_id, configured.instance_kind


def _legacy_snapshot_has_organization_context(
    context: ResourceUserContext,
    snapshot: Mapping[str, Any],
) -> bool:
    """Detect Organization semantics retained by pre-binding snapshots."""

    return bool(
        context.organization_id
        or context.organization_member_id
        or context.organization_role
        or context.instance_access_source == "organization_group"
        or context.group_ids
        or any(
            snapshot.get(key)
            for key in (
                "vibe_organization_id",
                "vibe_organization_member_id",
                "vibe_organization_role",
                "vibe_group_ids",
            )
        )
    )


def _legacy_deferred_context_binding(
    snapshot: Mapping[str, Any],
    *,
    paired_instance_id: str,
    paired_kind: str,
) -> dict[str, str] | None:
    """Return a safe current binding for one pre-instance metadata snapshot."""

    context = current_resource_context(snapshot, is_remote=True)
    if context.instance_role not in {"owner", "editor", "viewer"}:
        return None

    raw_instance_id = snapshot.get("vibe_instance_id")
    snapshot_instance_id = _clean_optional_string(raw_instance_id)
    if raw_instance_id is not None and snapshot_instance_id is None:
        return None
    if snapshot_instance_id and snapshot_instance_id != paired_instance_id:
        return None

    raw_kind = snapshot.get("vibe_instance_kind")
    if raw_kind is not None and raw_kind not in {"personal", "organization"}:
        return None
    if raw_kind is not None and raw_kind != paired_kind:
        return None

    # The pairing kind is the only authoritative evidence for a legacy
    # snapshot that predates both binding fields. Explicit Organization claims
    # still contradict a Personal pairing, but access sources such as ``email``
    # are shared by Personal and Organization instances and cannot choose a
    # kind on their own.
    if paired_kind == "personal" and _legacy_snapshot_has_organization_context(context, snapshot):
        return None
    inferred_kind = raw_kind or paired_kind
    if inferred_kind != paired_kind:
        return None
    return {
        "vibe_instance_id": paired_instance_id,
        "vibe_instance_kind": paired_kind,
    }


def _connection_has_table(connection: Connection, table_name: str) -> bool:
    """True when the table exists on this connection's schema.

    The deferred-context migration also runs while an unversioned legacy
    schema is being stamped, where newer tables may not exist yet. A missing
    table simply has no deferred records to migrate.
    """

    return (
        connection.execute(
            sqlalchemy_text(
                "select 1 from sqlite_master where type = 'table' and name = :name"
            ),
            {"name": table_name},
        ).scalar_one_or_none()
        is not None
    )


def _metadata_snapshot_lacks_binding(metadata: Any) -> bool:
    """True when a deferred snapshot exists but carries no complete binding."""

    if not isinstance(metadata, dict):
        return False
    snapshot = metadata.get(RESOURCE_USER_CONTEXT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return False
    instance_id = _clean_optional_string(snapshot.get("vibe_instance_id"))
    kind = snapshot.get("vibe_instance_kind")
    return instance_id is None or kind not in {"personal", "organization"}


def _has_unbound_deferred_snapshots(connection: Connection) -> bool:
    """True when any released deferred record still lacks an instance binding.

    A fresh install has nothing to protect: sealing or pinning a marker there
    would only block the first real pairing. Only actual legacy snapshots make
    the first migration opportunity meaningful.
    """

    if _connection_has_table(connection, "run_definitions"):
        definition_rows = connection.execute(
            select(run_definitions.c.metadata_json).where(
                run_definitions.c.definition_type.in_(("scheduled", "watch"))
            )
        )
        for (metadata_json,) in definition_rows:
            try:
                metadata = json.loads(metadata_json or "{}")
            except (TypeError, ValueError):
                continue
            if _metadata_snapshot_lacks_binding(metadata):
                return True
    if _connection_has_table(connection, "agent_runs"):
        run_rows = connection.execute(
            select(agent_runs.c.metadata_json).where(
                agent_runs.c.status.in_(("pending", "queued", "processing", "running"))
            )
        )
        for (metadata_json,) in run_rows:
            try:
                metadata = json.loads(metadata_json or "{}")
            except (TypeError, ValueError):
                continue
            if _metadata_snapshot_lacks_binding(metadata):
                return True
    if _connection_has_table(connection, "message_deliveries"):
        delivery_rows = connection.execute(
            select(message_deliveries.c.snapshot_json).where(
                message_deliveries.c.snapshot_json.is_not(None)
            )
        )
        for (snapshot_json,) in delivery_rows:
            try:
                snapshot = json.loads(snapshot_json or "{}")
                metadata = json.loads(snapshot.get("metadata_json") or "{}")
            except (AttributeError, TypeError, ValueError):
                continue
            if _metadata_snapshot_lacks_binding(metadata):
                return True
    return False


def _migrate_deferred_metadata_value(
    metadata: Any,
    *,
    paired_instance_id: str,
    paired_kind: str,
) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    snapshot = metadata.get(RESOURCE_USER_CONTEXT_METADATA_KEY)
    if not isinstance(snapshot, Mapping):
        return None
    binding = _legacy_deferred_context_binding(
        snapshot,
        paired_instance_id=paired_instance_id,
        paired_kind=paired_kind,
    )
    if binding is None:
        return None
    if all(snapshot.get(key) == value for key, value in binding.items()):
        return None
    migrated = dict(metadata)
    migrated_snapshot = dict(snapshot)
    migrated_snapshot.update(binding)
    migrated[RESOURCE_USER_CONTEXT_METADATA_KEY] = migrated_snapshot
    return migrated


def migrate_legacy_deferred_resource_contexts(connection: Connection) -> dict[str, int]:
    """Bind released deferred metadata to the still-current runtime pairing.

    The migration is deliberately conservative and one-shot. It needs complete
    runtime credentials and a known server-owned instance kind, and it only
    updates a legacy snapshot when its existing instance evidence or explicit
    Organization claims agree with that pairing. A transient configuration read
    is not an unpaired state: it leaves the marker untouched so the same
    pairing can retry later. A successfully observed unpaired state is sealed,
    while a known instance with an unknown kind remains pending until that same
    instance is authoritatively classified.
    """

    def _counts(
        *,
        binding_status: str,
        definitions: int = 0,
        runs: int = 0,
        deliveries: int = 0,
    ) -> dict[str, int | str]:
        return {
            "legacy_deferred_definitions": definitions,
            "legacy_deferred_runs": runs,
            "legacy_deferred_deliveries": deliveries,
            RESOURCE_BINDING_STATUS_KEY: binding_status,
        }

    empty_unavailable = _counts(binding_status=RESOURCE_BINDING_STATE_UNAVAILABLE)
    marker_value = connection.execute(
        select(state_meta.c.value_json).where(
            state_meta.c.key == LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
        )
    ).scalar_one_or_none()
    marker: dict[str, Any] | None = None
    if marker_value is not None:
        try:
            parsed_marker = json.loads(marker_value)
        except (TypeError, ValueError):
            parsed_marker = None
        if not isinstance(parsed_marker, dict):
            # A corrupt marker cannot prove which pairing created the rows.
            # Preserve fail-closed behavior instead of trying to insert a
            # duplicate key or guessing a new binding.
            return _counts(binding_status=RESOURCE_BINDING_STATE_UNAVAILABLE)
        marker = parsed_marker
        marker_state = marker.get("state")
        if marker_state not in {"pending", "completed", "sealed_unattributed"}:
            marker_state = "completed" if marker.get("completed_at") else "pending"
        # Markers written by e64/d667 used ``completed`` with no instance ID
        # for an unpaired first opportunity. Preserve that terminal meaning;
        # never reinterpret it as a fresh migration opportunity.
        if marker_state == "completed" and marker.get("instance_id") is None:
            marker_state = "sealed_unattributed"
        marker = {**marker, "state": marker_state}

    configured = _configured_resource_state()
    if configured.status == RESOURCE_BINDING_STATE_UNAVAILABLE:
        # A read failure must not create ``pending(instance_id=None)`` or
        # rewrite a known marker. Startup/heartbeat will retry this operation.
        return empty_unavailable
    current_instance_id = configured.instance_id
    current_kind = (
        configured.instance_kind
        if configured.status == RESOURCE_BINDING_STATE_READY
        else None
    )
    # PARTIAL still carries a known instance ID; heartbeat/backfill uses it to
    # keep snapshots pending instead of sealing them as unattributed.

    def write_marker(
        *,
        state: str,
        instance_id: str | None,
        instance_kind: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        payload: dict[str, Any] = {
            "schema_version": 2,
            "state": state,
            "instance_id": instance_id,
            "updated_at": now,
        }
        if instance_kind in {"personal", "organization"}:
            payload["instance_kind"] = instance_kind
        if state == "completed":
            payload["completed_at"] = now
        values = {
            "value_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "updated_at": now,
        }
        if marker is None:
            connection.execute(
                state_meta.insert().values(
                    key=LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
                    **values,
                )
            )
        else:
            connection.execute(
                update(state_meta)
                .where(state_meta.c.key == LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY)
                .values(**values)
            )

    if marker is not None and marker.get("state") in {"completed", "sealed_unattributed"}:
        return _counts(binding_status=RESOURCE_BINDING_STATE_SEALED)

    raw_marker_instance_id = marker.get("instance_id") if marker else None
    marker_instance_id = (
        _clean_optional_string(raw_marker_instance_id) if marker else None
    )
    if marker and raw_marker_instance_id is not None and marker_instance_id is None:
        # A malformed marker cannot prove ownership. Keep the existing value
        # intact and fail closed rather than guessing a new pairing.
        return _counts(binding_status=RESOURCE_BINDING_STATE_UNAVAILABLE)
    if marker is not None:
        if marker_instance_id is None:
            # No first pairing was available to prove ownership. A later
            # pairing must not be allowed to claim these records.
            write_marker(state="sealed_unattributed", instance_id=None)
            return _counts(binding_status=RESOURCE_BINDING_STATE_SEALED)
        if current_instance_id is None:
            if configured.status == RESOURCE_BINDING_STATE_UNPAIRED:
                write_marker(state="sealed_unattributed", instance_id=marker_instance_id)
                return _counts(binding_status=RESOURCE_BINDING_STATE_SEALED)
            return _counts(binding_status=configured.status)
        if current_instance_id != marker_instance_id:
            write_marker(state="sealed_unattributed", instance_id=marker_instance_id)
            return _counts(binding_status=RESOURCE_BINDING_STATE_SEALED)

    if configured.status == RESOURCE_BINDING_STATE_UNPAIRED:
        if marker is None and not _has_unbound_deferred_snapshots(connection):
            # A fresh install has no legacy snapshots to protect. Writing a
            # sealed marker here would only block the first real pairing.
            return _counts(binding_status=RESOURCE_BINDING_STATE_UNPAIRED)
        write_marker(state="sealed_unattributed", instance_id=current_instance_id)
        return _counts(binding_status=RESOURCE_BINDING_STATE_SEALED)
    if configured.status != RESOURCE_BINDING_STATE_READY:
        # A PARTIAL pairing still records its identity: the pending marker is
        # what lets the SAME instance complete after credential repair while a
        # DIFFERENT instance stays sealed out.
        write_marker(state="pending", instance_id=current_instance_id)
        return _counts(binding_status=configured.status)
    if current_instance_id is None or current_kind not in {"personal", "organization"}:
        # Defensive guard for a future state-reader change. This branch is
        # never authoritative enough to complete a migration.
        return _counts(binding_status=RESOURCE_BINDING_STATE_PARTIAL)
    # C2 fence: this migration can run from a bare ensure_sqlite_state()
    # without config_file_lock (taking it here would invert the canonical
    # lock order against a peer holding config lock -> migration lock).
    # Instead, refuse to bind snapshots when the durable binding row exists
    # and disagrees with the pairing snapshot just read: a peer is mid-
    # reclassification and owns this migration through its own transition.
    from storage.remote_access_authorization_service import (
        INSTANCE_BINDING_STATE_RECONCILING as _BINDING_RECONCILING,
        _load_binding_state_from_connection as _load_binding_row,
    )

    try:
        binding_row = _load_binding_row(connection)
    except Exception:
        binding_row = None
    if binding_row is not None:
        row_id = binding_row.get("instance_id")
        row_kind = binding_row.get("instance_kind")
        row_state = binding_row.get("state")
        identity_matches = row_id == current_instance_id and (
            row_kind == current_kind
            or (row_state == _BINDING_RECONCILING and row_kind in {None, current_kind})
        )
        if not identity_matches:
            write_marker(state="pending", instance_id=current_instance_id)
            return _counts(binding_status=RESOURCE_BINDING_STATE_PARTIAL)
    paired_instance_id = current_instance_id
    paired_kind = current_kind

    counts = {
        "legacy_deferred_definitions": 0,
        "legacy_deferred_runs": 0,
        "legacy_deferred_deliveries": 0,
        RESOURCE_BINDING_STATUS_KEY: RESOURCE_BINDING_STATE_READY,
    }
    definition_rows = (
        connection.execute(
            select(run_definitions.c.id, run_definitions.c.metadata_json).where(
                run_definitions.c.definition_type.in_(("scheduled", "watch"))
            )
        ).mappings()
        if _connection_has_table(connection, "run_definitions")
        else ()
    )
    for row in definition_rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        migrated = _migrate_deferred_metadata_value(
            metadata,
            paired_instance_id=paired_instance_id,
            paired_kind=paired_kind,
        )
        if migrated is None:
            continue
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == row["id"])
            .values(
                metadata_json=json.dumps(
                    migrated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        )
        counts["legacy_deferred_definitions"] += 1

    run_rows = (
        connection.execute(
            select(agent_runs.c.id, agent_runs.c.metadata_json)
            .where(agent_runs.c.status.in_(("pending", "queued", "processing", "running")))
        ).mappings()
        if _connection_has_table(connection, "agent_runs")
        else ()
    )
    for row in run_rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        migrated = _migrate_deferred_metadata_value(
            metadata,
            paired_instance_id=paired_instance_id,
            paired_kind=paired_kind,
        )
        if migrated is None:
            continue
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == row["id"])
            .values(
                metadata_json=json.dumps(
                    migrated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                updated_at=_utc_now_iso(),
            )
        )
        counts["legacy_deferred_runs"] += 1

    delivery_rows = (
        connection.execute(
            select(
                message_deliveries.c.id,
                message_deliveries.c.snapshot_json,
            ).where(message_deliveries.c.snapshot_json.is_not(None))
        ).mappings()
        if _connection_has_table(connection, "message_deliveries")
        else ()
    )
    for row in delivery_rows:
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
            metadata = json.loads(snapshot.get("metadata_json") or "{}")
        except (AttributeError, TypeError, ValueError):
            continue
        migrated_metadata = _migrate_deferred_metadata_value(
            metadata,
            paired_instance_id=paired_instance_id,
            paired_kind=paired_kind,
        )
        if migrated_metadata is None:
            continue
        snapshot["metadata_json"] = json.dumps(
            migrated_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        connection.execute(
            update(message_deliveries)
            .where(message_deliveries.c.id == row["id"])
            .values(
                snapshot_json=snapshot_json,
                snapshot_sha256=snapshot_sha256,
                updated_at=_utc_now_iso(),
            )
        )
        counts["legacy_deferred_deliveries"] += 1
    write_marker(
        state="completed",
        instance_id=paired_instance_id,
        instance_kind=paired_kind,
    )
    return counts

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
                .where(resource_access_policies.c.resource_kind.in_(RESOURCE_KINDS))
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
    else:
        # Unscoped listings exclude retired resource kinds so preserved legacy
        # rows (e.g. show_page policies written by older releases) degrade
        # silently instead of surfacing through sync or onboarding queries.
        statement = statement.where(resource_access_policies.c.resource_kind.in_(RESOURCE_KINDS))
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
) -> bool:
    if context.is_instance_owner:
        return True
    if resource_kind == "agent" and context.is_personal_instance:
        return context.can_use_resource(resource_kind)
    # Skill and Vault ACL rows remain persisted for compatibility, but Editor
    # runtime access intentionally ignores them for validated remote sessions.
    # Direct non-remote contexts still use the stored policy so service-level
    # ACL checks cannot be bypassed by a caller-supplied role alone.
    if resource_kind in {"skill", "vault_secret"}:
        if context.is_remote and context.is_active_organization_member and context.has_role("editor"):
            return True
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
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
        groups = _policy_groups(conn, kind, identifier) if policy else []
        return _policy_allows(
            context,
            kind,
            identifier,
            policy,
            groups,
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
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
    return _policy_allows_management(context, policy)


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
    with _connection(connection) as conn:
        policy = _policy_row(conn, kind, identifier)
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
        and _policy_allows(
            context,
            kind,
            identifier,
            policies.get(identifier),
            groups.get(identifier, []),
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
