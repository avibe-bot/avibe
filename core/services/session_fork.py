"""Fork Avibe Agent Sessions.

This module owns the Avibe-level fork contract: validate the source Session,
copy its row into a new pending Session, and carry enough metadata for backend
adapters to call their native fork API on the first turn.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config import paths
from core.backend_failure import BACKEND_FAILURE_EVENT, is_backend_failure_notification
from vibe.i18n import t
from vibe.message_identity import INPUT_TURN_AUTHOR_TYPES, is_input_turn

TRIM_LATEST_RUNNING_TURN_BACKENDS = {"codex", "opencode"}
# ``silent`` is the invisible completion marker (messages_service.SILENT_TYPE): a turn
# that finished with no user-visible reply is still TERMINAL, so a fork created after
# it must not trim/roll back the completed turn as if it were still running.
TERMINAL_AGENT_OUTPUT_TYPES = {"result", "error", "silent"}
SOURCE_PROGRESS_AGENT_OUTPUT_TYPES = {"assistant", *TERMINAL_AGENT_OUTPUT_TYPES}
ACTIVE_SOURCE_RUN_STATUSES = ("pending", "queued", "processing", "running")
INPUT_TURN_MESSAGE_TYPES = tuple(message_type for _, message_type in INPUT_TURN_AUTHOR_TYPES)


class SessionForkError(ValueError):
    """Raised when a Session cannot be forked."""


@dataclass(frozen=True)
class SessionForkSpec:
    source_session_id: str
    source_native_session_id: str
    source_backend: str
    source_message_id: Optional[str] = None
    trim_latest_running_turn: bool = False
    native_turn_started: bool = False
    opencode_fork_message_id: Optional[str] = None
    opencode_fork_empty_history: bool = False
    opencode_boundary_from_active_run: bool = False

    def to_metadata(self) -> dict[str, Any]:
        metadata = {
            "source_session_id": self.source_session_id,
            "source_native_session_id": self.source_native_session_id,
            "source_backend": self.source_backend,
        }
        if self.source_message_id:
            metadata["source_message_id"] = self.source_message_id
        if self.trim_latest_running_turn:
            metadata["trim_latest_running_turn"] = True
        if self.native_turn_started:
            metadata["native_turn_started"] = True
        if self.opencode_fork_message_id:
            metadata["opencode_fork_message_id"] = self.opencode_fork_message_id
        if self.opencode_fork_empty_history:
            metadata["opencode_fork_empty_history"] = True
        if self.opencode_boundary_from_active_run:
            metadata["opencode_boundary_from_active_run"] = True
        return metadata


@dataclass(frozen=True)
class SessionForkResult:
    session_id: str
    agent_name: Optional[str]
    agent_id: Optional[str]
    agent_backend: str
    model: Optional[str]
    reasoning_effort: Optional[str]
    fork: SessionForkSpec


@dataclass(frozen=True)
class ForkSourceState:
    anchor_author: Optional[str] = None
    anchor_type: Optional[str] = None
    latest_after_anchor_author: Optional[str] = None
    latest_after_anchor_type: Optional[str] = None
    has_messages_after_anchor: bool = False
    has_terminal_agent_output_after_anchor: bool = False
    has_input_turn_after_anchor: bool = False
    anchor_is_backend_failure: bool = False

    @property
    def anchor_is_terminal_agent_output(self) -> bool:
        return self.anchor_author == "agent" and (
            self.anchor_type in TERMINAL_AGENT_OUTPUT_TYPES or self.anchor_is_backend_failure
        )


@dataclass(frozen=True)
class SourceMessageAnchor:
    message_id: Optional[str] = None
    author: Optional[str] = None
    message_type: Optional[str] = None

    @property
    def is_running_input_turn(self) -> bool:
        return is_input_turn(self.author, self.message_type)


def reserve_forked_session(
    *,
    source_session_id: str,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    scope_id: Optional[str] = None,
    visibility: str = "foreground",
    trim_latest_running_turn: bool = False,
    native_turn_started: bool = False,
    db_path: Optional[Path] = None,
    title_lang: str = "en",
) -> SessionForkResult:
    """Copy an existing Agent Session row into a new pending fork target.

    ``agent_name`` may switch to another enabled Avibe Agent only when that
    Agent uses the same backend as the source. ``model`` and
    ``reasoning_effort`` are simple per-session overrides. The new row's native
    id stays empty until the backend adapter successfully forks the native
    session.
    """

    from sqlalchemy import select

    from core.vibe_agents import VibeAgentStore
    from storage.agent_session_rows import create_agent_session_row, utc_now_iso
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
    from storage.models import agent_sessions

    path = db_path or paths.get_sqlite_state_path()
    if db_path is None:
        ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    engine = create_sqlite_engine(path)
    agent_store = VibeAgentStore(path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == str(source_session_id)).limit(1)
            ).mappings().first()
            if row is None:
                raise SessionForkError(f"agent session id not found: {source_session_id}")
            if str(row["status"] or "") == "archived":
                raise SessionForkError(f"agent session is archived: {source_session_id}")

            source_backend = str(row["agent_backend"] or "").strip()
            if source_backend not in {"codex", "claude", "opencode"}:
                raise SessionForkError(f"session backend cannot be forked: {source_backend or 'unknown'}")
            source_native = str(row["native_session_id"] or "").strip()
            if not source_native:
                raise SessionForkError(
                    f"agent session has no native session id to fork: {source_session_id}"
                )
            source_anchor = _latest_source_message_anchor(conn, str(row["id"]))
            source_has_active_run = _source_has_active_agent_run(conn, str(row["id"]))
            inferred_running_turn = source_anchor.is_running_input_turn or (
                source_backend == "opencode"
                and source_has_active_run
            )
            effective_trim_latest_running_turn = bool(
                source_backend in TRIM_LATEST_RUNNING_TURN_BACKENDS
                and (trim_latest_running_turn or inferred_running_turn)
            )
            effective_native_turn_started = bool(
                effective_trim_latest_running_turn and native_turn_started
            )
            opencode_fork_message_id: Optional[str] = None
            opencode_fork_empty_history = False
            opencode_boundary_from_active_run = False
            if effective_trim_latest_running_turn and source_backend == "opencode":
                fork_point = _opencode_running_fork_point(source_native)
                if fork_point is not None:
                    opencode_fork_message_id, opencode_fork_empty_history = fork_point
                    opencode_boundary_from_active_run = bool(
                        source_has_active_run and not source_anchor.is_running_input_turn
                    )
                    effective_native_turn_started = True
            source_message_id = source_anchor.message_id
            override_agent = agent_store.require_enabled(agent_name) if agent_name else None
            if override_agent is not None and override_agent.backend != source_backend:
                raise SessionForkError(
                    "agent backend does not match the source session backend"
                )

            target_agent_id = override_agent.id if override_agent else row["agent_id"]
            target_agent_name = override_agent.name if override_agent else row["agent_name"]
            target_backend = override_agent.backend if override_agent else source_backend
            target_variant = target_backend if override_agent else str(row["agent_variant"] or target_backend)
            now = utc_now_iso()
            target_model = _clean_optional(model) if model is not None else row["model"]
            target_effort = (
                _clean_optional(reasoning_effort)
                if reasoning_effort is not None
                else row["reasoning_effort"]
            )
            explicit_scope_id = _clean_optional(scope_id) if scope_id is not None else None
            target_scope_id = explicit_scope_id or row["scope_id"]
            if explicit_scope_id or target_scope_id != row["scope_id"]:
                target_anchor = _fork_session_anchor(
                    _anchor_for_scope_id(target_scope_id),
                    source_session_id=str(row["id"]),
                    now=now,
                )
            else:
                target_anchor = _fork_session_anchor(row["session_anchor"], source_session_id=str(row["id"]), now=now)
            source_title = str(row["title"] or "").strip()
            target_title = _forked_session_title(source_title, title_lang)

            metadata = _load_metadata(row["metadata_json"])
            metadata.pop("fork_opencode_message_id", None)
            metadata.pop("fork_opencode_fork_empty_history", None)
            metadata.pop("fork_opencode_boundary_from_active_run", None)
            metadata.update(
                {
                    "created_via": "session_fork",
                    "fork_source_session_id": str(row["id"]),
                    "fork_source_session_title": source_title,
                    "fork_source_message_id": source_message_id,
                    "fork_source_native_session_id": source_native,
                    "fork_source_backend": source_backend,
                    "fork_trim_latest_running_turn": effective_trim_latest_running_turn,
                    "fork_native_turn_started": effective_native_turn_started,
                    "fork_created_at": now,
                }
            )
            if opencode_fork_message_id:
                metadata["fork_opencode_message_id"] = opencode_fork_message_id
            if opencode_fork_empty_history:
                metadata["fork_opencode_fork_empty_history"] = True
            if opencode_boundary_from_active_run:
                metadata["fork_opencode_boundary_from_active_run"] = True
            if target_scope_id != row["scope_id"]:
                metadata["fork_target_scope_id"] = target_scope_id
                metadata["legacy_scope_key"] = target_scope_id
            session_id = create_agent_session_row(
                conn,
                scope_id=target_scope_id,
                # Keep IM delivery stable while satisfying the per-scope anchor
                # uniqueness invariant. resolve_session_id_target strips the
                # suffix after ":" when deriving the IM thread id.
                session_anchor=target_anchor,
                agent_id=target_agent_id,
                agent_name=target_agent_name,
                agent_backend=target_backend,
                agent_variant=target_variant,
                model=target_model,
                reasoning_effort=target_effort,
                workdir=row["workdir"],
                native_session_id="",
                title=target_title,
                visibility=visibility,
                metadata=metadata,
                now=now,
                require_workdir=False,
            )

        fork = SessionForkSpec(
            source_session_id=str(source_session_id),
            source_native_session_id=source_native,
            source_backend=source_backend,
            source_message_id=source_message_id,
            trim_latest_running_turn=effective_trim_latest_running_turn,
            native_turn_started=effective_native_turn_started,
            opencode_fork_message_id=opencode_fork_message_id,
            opencode_fork_empty_history=opencode_fork_empty_history,
            opencode_boundary_from_active_run=opencode_boundary_from_active_run,
        )
        return SessionForkResult(
            session_id=session_id,
            agent_name=str(target_agent_name).strip() if target_agent_name else None,
            agent_id=str(target_agent_id).strip() if target_agent_id else None,
            agent_backend=target_backend,
            model=target_model,
            reasoning_effort=target_effort,
            fork=fork,
        )
    finally:
        agent_store.close()
        engine.dispose()


def fork_metadata_from_request(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return normalized pending fork metadata from an Agent Run record."""

    if not isinstance(metadata, dict):
        return None
    fork = metadata.get("session_fork")
    if not isinstance(fork, dict):
        return None
    source_backend = _clean_optional(fork.get("source_backend"))
    source_native = _clean_optional(fork.get("source_native_session_id"))
    source_session = _clean_optional(fork.get("source_session_id"))
    if not (source_backend and source_native and source_session):
        return None
    result = {
        "source_session_id": source_session,
        "source_native_session_id": source_native,
        "source_backend": source_backend,
    }
    if bool(fork.get("trim_latest_running_turn")):
        result["trim_latest_running_turn"] = True
    if bool(fork.get("native_turn_started")):
        result["native_turn_started"] = True
    opencode_message = _clean_optional(fork.get("opencode_fork_message_id"))
    if opencode_message:
        result["opencode_fork_message_id"] = opencode_message
    if bool(fork.get("opencode_fork_empty_history")):
        result["opencode_fork_empty_history"] = True
    if bool(fork.get("opencode_boundary_from_active_run")):
        result["opencode_boundary_from_active_run"] = True
    source_message = _clean_optional(fork.get("source_message_id"))
    if source_message:
        result["source_message_id"] = source_message
    return result


def fork_metadata_from_session_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return pending fork metadata persisted on a fork target Session row."""

    if not isinstance(metadata, dict):
        return None
    source_backend = _clean_optional(metadata.get("fork_source_backend"))
    source_native = _clean_optional(metadata.get("fork_source_native_session_id"))
    source_session = _clean_optional(metadata.get("fork_source_session_id"))
    if not (source_backend and source_native and source_session):
        return None
    result = {
        "source_session_id": source_session,
        "source_native_session_id": source_native,
        "source_backend": source_backend,
    }
    if bool(metadata.get("fork_trim_latest_running_turn")):
        result["trim_latest_running_turn"] = True
    if bool(metadata.get("fork_native_turn_started")):
        result["native_turn_started"] = True
    opencode_message = _clean_optional(metadata.get("fork_opencode_message_id"))
    if opencode_message:
        result["opencode_fork_message_id"] = opencode_message
    if bool(metadata.get("fork_opencode_fork_empty_history")):
        result["opencode_fork_empty_history"] = True
    if bool(metadata.get("fork_opencode_boundary_from_active_run")):
        result["opencode_boundary_from_active_run"] = True
    source_message = _clean_optional(metadata.get("fork_source_message_id"))
    if source_message:
        result["source_message_id"] = source_message
    return result


def pending_native_fork(context: Any, backend: str) -> Optional[dict[str, Any]]:
    """Native fork spec if this turn should fork instead of start fresh."""

    payload = getattr(context, "platform_specific", None) or {}
    target = payload.get("agent_session_target")
    if not isinstance(target, dict):
        return None
    target_backend = str(target.get("agent_backend") or "").strip()
    if target_backend and target_backend != backend:
        return None
    if str(target.get("native_session_id") or "").strip():
        return None
    fork = target.get("native_session_fork")
    if not isinstance(fork, dict):
        fork = fork_metadata_from_session_metadata(target.get("metadata"))
    if not isinstance(fork, dict):
        return None
    if str(fork.get("source_backend") or "").strip() != backend:
        return None
    source_native = str(fork.get("source_native_session_id") or "").strip()
    if not source_native:
        return None
    return {**fork, "source_native_session_id": source_native}


def pending_native_fork_source(context: Any, backend: str) -> Optional[str]:
    """Native source id if this turn should fork instead of start fresh."""

    fork = pending_native_fork(context, backend)
    if not fork:
        return None
    return str(fork.get("source_native_session_id") or "").strip() or None


def fork_source_state(fork: dict[str, Any] | None) -> ForkSourceState:
    """Return source transcript state relative to the fork reservation anchor."""

    if not isinstance(fork, dict):
        return ForkSourceState()
    source_session_id = _clean_optional(fork.get("source_session_id"))
    source_message_id = _clean_optional(fork.get("source_message_id"))
    if not source_session_id or not source_message_id:
        return ForkSourceState()

    from sqlalchemy import and_, func, or_, select

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
    from storage.models import messages

    ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            anchor = conn.execute(
                select(
                    messages.c.created_at,
                    messages.c.id,
                    messages.c.author,
                    messages.c.type,
                    messages.c.metadata_json,
                )
                .where(messages.c.session_id == source_session_id, messages.c.id == source_message_id)
                .limit(1)
            ).mappings().first()
            if anchor is None:
                return ForkSourceState()
            anchor_created_at = anchor["created_at"]
            anchor_id = anchor["id"]
            after_anchor = (
                (messages.c.created_at > anchor_created_at)
                | ((messages.c.created_at == anchor_created_at) & (messages.c.id > anchor_id))
            )
            latest_after_anchor = conn.execute(
                select(messages.c.author, messages.c.type, messages.c.metadata_json)
                .where(
                    messages.c.session_id == source_session_id,
                    or_(
                        messages.c.type.in_(
                            [*INPUT_TURN_MESSAGE_TYPES, *list(SOURCE_PROGRESS_AGENT_OUTPUT_TYPES)]
                        ),
                        and_(
                            messages.c.type == "notify",
                            func.json_extract(messages.c.metadata_json, "$.event")
                            == BACKEND_FAILURE_EVENT,
                        ),
                    ),
                    after_anchor,
                )
                .order_by(messages.c.created_at.desc(), messages.c.id.desc())
                .limit(1)
            ).mappings().first()
            latest_after_anchor_author = (
                str(latest_after_anchor["author"] or "").strip() if latest_after_anchor else ""
            )
            latest_after_anchor_type = (
                str(latest_after_anchor["type"] or "").strip() if latest_after_anchor else ""
            )
            latest_after_anchor_metadata = _load_metadata(
                latest_after_anchor["metadata_json"] if latest_after_anchor else None
            )
            has_terminal_agent_output_after_anchor = (
                latest_after_anchor_author == "agent"
                and (
                    latest_after_anchor_type in TERMINAL_AGENT_OUTPUT_TYPES
                    or is_backend_failure_notification(
                        latest_after_anchor_type,
                        latest_after_anchor_metadata,
                    )
                )
            )
            has_input_turn_after_anchor = (
                conn.execute(
                    select(messages.c.id)
                    .where(
                        messages.c.session_id == source_session_id,
                        or_(
                            *(
                                and_(messages.c.author == author, messages.c.type == message_type)
                                for author, message_type in INPUT_TURN_AUTHOR_TYPES
                            )
                        ),
                        after_anchor,
                    )
                    .limit(1)
                ).first()
                is not None
            )
            return ForkSourceState(
                anchor_author=str(anchor["author"] or "").strip() or None,
                anchor_type=str(anchor["type"] or "").strip() or None,
                latest_after_anchor_author=latest_after_anchor_author or None,
                latest_after_anchor_type=latest_after_anchor_type or None,
                has_messages_after_anchor=latest_after_anchor is not None,
                has_terminal_agent_output_after_anchor=has_terminal_agent_output_after_anchor,
                has_input_turn_after_anchor=has_input_turn_after_anchor,
                anchor_is_backend_failure=is_backend_failure_notification(
                    anchor["type"],
                    _load_metadata(anchor["metadata_json"]),
                ),
            )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to inspect fork source state for %s after %s: %s",
            source_session_id,
            source_message_id,
            exc,
        )
        return ForkSourceState()
    finally:
        engine.dispose()


def fork_source_has_agent_output_after_anchor(fork: dict[str, Any] | None) -> bool:
    """Whether the source produced terminal agent output after the fork anchor."""

    return fork_source_state(fork).has_terminal_agent_output_after_anchor


def fork_anchor_is_terminal_agent_output(fork: dict[str, Any] | None) -> bool:
    """Whether the reservation anchor already points at a completed agent row."""

    return fork_source_state(fork).anchor_is_terminal_agent_output


def _load_metadata(value: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _forked_session_title(source_title: str, lang: str = "en") -> str:
    return t("fork.title", lang, title=source_title) if source_title else t("fork.titleUntitled", lang)


def _latest_source_message_anchor(conn: Any, source_session_id: str) -> SourceMessageAnchor:
    from sqlalchemy import func, or_, select

    from storage.messages_service import SILENT_TYPE, TRANSCRIPT_TYPES
    from storage.models import messages

    row = conn.execute(
        select(messages.c.id, messages.c.author, messages.c.type)
        .where(
            messages.c.session_id == source_session_id,
            or_(
                # Include the invisible ``silent`` completion marker so a turn that
                # finished silently is the anchor (a terminal, NOT a running input),
                # otherwise the anchor falls back to the input row and the fork treats
                # the completed turn as still running and trims/rolls it back.
                messages.c.type.in_([*TRANSCRIPT_TYPES, SILENT_TYPE]),
                func.json_extract(messages.c.metadata_json, "$.source") == "show_page",
            ),
        )
        .order_by(messages.c.created_at.desc(), messages.c.id.desc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return SourceMessageAnchor()
    return SourceMessageAnchor(
        message_id=str(row["id"]) if row["id"] else None,
        author=str(row["author"] or "").strip() or None,
        message_type=str(row["type"] or "").strip() or None,
    )


def _source_has_active_agent_run(conn: Any, source_session_id: str) -> bool:
    from sqlalchemy import select

    from storage.models import agent_runs

    return (
        conn.execute(
            select(agent_runs.c.id)
            .where(
                agent_runs.c.session_id == source_session_id,
                agent_runs.c.status.in_(ACTIVE_SOURCE_RUN_STATUSES),
            )
            .order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _opencode_running_fork_point(source_native_session_id: str) -> Optional[tuple[Optional[str], bool]]:
    from modules.agents.native_sessions.opencode import OpenCodeNativeSessionProvider

    point = OpenCodeNativeSessionProvider().running_fork_point_before_latest_user(source_native_session_id)
    if not point.available:
        return None
    return point.message_id, point.empty_history


def _fork_session_anchor(value: Any, *, source_session_id: str, now: str) -> Optional[str]:
    anchor = _clean_optional(value)
    if not anchor or anchor == source_session_id:
        return None
    suffix = "".join(ch for ch in f"{source_session_id}_{now}" if ch.isalnum())
    return f"{anchor}:fork_{suffix}_{secrets.token_hex(3)}"


def _anchor_for_scope_id(scope_id: Any) -> Optional[str]:
    value = _clean_optional(scope_id)
    if not value:
        return None
    try:
        platform, scope_type, native_id = value.split("::", 2)
    except ValueError:
        return None
    if not (platform and scope_type and native_id):
        return None
    return f"{platform}_{native_id}"
