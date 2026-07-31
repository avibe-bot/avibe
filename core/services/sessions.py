"""Business API for the ``agent_sessions`` table.

This module is the **only** import path that UI server / CLI / IM adapter
should use to read or write agent-session rows. Today it re-exports the
existing storage-level helpers; later phases will fold the per-caller
duplication (``storage.workbench_sessions_service`` vs
``storage.sessions_service.SQLiteSessionsService``) into a single set of
free functions here.

Why the early re-export shim:

* Lets callers move onto ``core.services.sessions`` immediately (UI server
  in C1, CLI in C2) without forcing a behavior-affecting rewrite in the
  same commit.
* Pins the public surface so future internal refactors don't ripple into
  every consumer.
* The contract tests in ``tests/test_core_services_sessions.py`` lock
  the shape so a future internal change cannot silently drift the API.

Conventions (see workbench-dispatch-architecture.md §6):

* Public functions take a SQLAlchemy ``Connection`` as their first
  argument. Never construct engines here.
* Return shapes are plain ``dict[str, Any]`` payloads (matching the
  existing ``workbench_sessions_service`` style).
* Errors raise ``LookupError`` / ``ValueError`` so callers can map them
  to HTTP status codes or CLI exit codes without leaking SQLAlchemy
  exceptions.
* No side effects. SSE publishes, audit logs, etc. belong in the calling
  layer (REST route, CLI command, controller handler).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from config import paths
from storage.workbench_sessions_service import (
    ReservedSessionError,
    SessionArchivedError,
    SessionBackendLockedError,
    archive_session,
    backfill_session_title,
    count_bound_resources,
    create_session,
    derive_backend_for_agent_name,
    get_active_session,
    get_session,
    is_session_archived,
    list_sessions,
    list_sessions_page,
    reset_running_agent_status,
    require_enabled_agent_backend,
    set_agent_status,
    touch_session,
    update_session,
)
from vibe.i18n import t as i18n_t

__all__ = [
    "SESSION_ARCHIVED_I18N_KEY",
    "ReservedSessionError",
    "SessionArchivedError",
    "SessionBackendLockedError",
    "archive_session",
    "backfill_session_title",
    "count_bound_resources",
    "create_session",
    "derive_backend_for_agent_name",
    "get_active_session",
    "get_session",
    "is_session_archived",
    "list_sessions",
    "list_sessions_page",
    "reset_running_agent_status",
    "set_agent_status",
    "session_archived_message",
    "touch_session",
    "update_session",
    "reserve_agent_session",
    "reserve_standalone_agent_session",
]


# --- Localized copy for the terminal-archive refusal ------------------
#
# ``SessionArchivedError`` carries a developer-facing sentence (it names the
# session id), but the ``message`` a route puts in its 409 body is USER-visible:
# direct API/CLI consumers read it verbatim, and a Web UI client that lacks the
# ``errors.session_archived`` translation renders it as the fallback. So it has to
# come from ``vibe/i18n`` rather than an English literal at the route (AGENTS.md
# §6). Shaped exactly like ``core.show_session_events.localized_show_event_error``:
# a module-level key constant beside the error it describes (so the parity guard in
# ``tests/test_i18n_backend_keys.py`` can pin it) plus a factory that resolves the
# user's configured language and degrades to English if the config is unreadable.
SESSION_ARCHIVED_I18N_KEY = "error.sessionArchived"


def session_archived_message(lang: Optional[str] = None) -> str:
    """User-facing copy for a write refused because the session is archived.

    ``lang`` defaults to the configured UI language; an unreadable/missing config
    (a brand-new install, a partially-written file) falls back to English rather
    than failing the response.
    """

    if not lang:
        from config.v2_config import V2Config

        try:
            lang = V2Config.load().language
        except Exception:
            lang = "en"
    return i18n_t(SESSION_ARCHIVED_I18N_KEY, lang)


# --- Reservation helpers (IM-style scope_key + session_anchor) --------
#
# These wrap the legacy ``SQLiteSessionsService`` methods so the CLI and
# scheduled-task / hook flows can move off the storage class without a
# behavior-change in the same commit. Each call opens its own short-lived
# service instance so the caller does not have to manage close().
#
# Later phases will fold the per-instance engine ownership into a shared
# connection lifecycle, but for now the public free-function shape is
# what callers commit to.


def _ensure_cli_sqlite_state() -> None:
    """Make sure the SQLite database exists and is migrated.

    Mirrors ``vibe.cli._ensure_cli_sqlite_state`` so the service can be
    invoked from any process (CLI, controller, future internal RPC)
    without relying on a caller-side bootstrap. Safe to call repeatedly;
    ``ensure_sqlite_state`` is idempotent.
    """

    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config

    ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))


def _open_legacy_service(db_path: Optional[Path] = None):
    """Construct the engine-owning ``SQLiteSessionsService`` exactly once
    per call. Internal — never expose this class to callers.
    """

    from storage.sessions_service import SQLiteSessionsService

    _ensure_cli_sqlite_state()
    return SQLiteSessionsService(db_path or paths.get_sqlite_state_path())


def reserve_agent_session(
    *,
    scope_key: str,
    agent_backend: str,
    session_anchor: str,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    workdir: Optional[str] = None,
    visibility: str = "foreground",
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Reserve a new ``agent_sessions`` row keyed by an IM-style scope.

    Returns the new session id or ``None`` if the underlying service
    refuses (e.g. scope key cannot be resolved). Matches the existing
    ``SQLiteSessionsService.reserve_agent_session`` contract.
    """

    service = _open_legacy_service(db_path)
    try:
        return service.reserve_agent_session(
            scope_key=scope_key,
            agent_backend=agent_backend,
            session_anchor=session_anchor,
            agent_id=agent_id,
            agent_name=agent_name,
            model=model,
            reasoning_effort=reasoning_effort,
            workdir=workdir,
            visibility=visibility,
            metadata=metadata,
        )
    finally:
        service.close()


def reserve_standalone_agent_session(
    *,
    agent_backend: str,
    session_anchor: str,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    workdir: Optional[str] = None,
    visibility: str = "background",
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Reserve a background-capable session with no Scope."""

    service = _open_legacy_service(db_path)
    try:
        return service.reserve_standalone_agent_session(
            agent_backend=agent_backend,
            session_anchor=session_anchor,
            agent_id=agent_id,
            agent_name=agent_name,
            model=model,
            reasoning_effort=reasoning_effort,
            workdir=workdir,
            visibility=visibility,
            metadata=metadata,
        )
    finally:
        service.close()
