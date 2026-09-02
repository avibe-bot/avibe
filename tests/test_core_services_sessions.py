"""Contract tests for ``core.services.sessions``.

This module is the public business API for the ``agent_sessions`` table.
The tests here pin the shape so callers (UI server, CLI, IM adapter)
can rely on it across refactors. Any change that breaks the row payload
shape or the public function set must update this file in lock-step.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.services import sessions as sessions_service
from storage import workbench_sessions_service as storage_sessions
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, scope_settings
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_avibe_scope(conn, workdir: str | None = None) -> str:
    scope_id = upsert_scope(
        conn,
        platform="avibe",
        scope_type="project",
        native_id="proj_contract",
        now="2026-05-26T13:00:00Z",
    )
    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=workdir or "/tmp/vibe-remote-contract-project",
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json="{}",
            created_at="2026-05-26T13:00:00Z",
            updated_at="2026-05-26T13:00:00Z",
        )
    )
    return scope_id


# --- Public surface ---------------------------------------------------


def test_public_surface_is_stable():
    """The service module's ``__all__`` is the locked public API."""
    expected = {
        # Modern workbench CRUD (takes ``conn``):
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
        "set_agent_status",
        "touch_session",
        "touch_session_agent_activity",
        "update_session",
        # Legacy IM-style reservation helpers added in C2 for the CLI:
        "reserve_agent_session",
            "reserve_standalone_agent_session",
        # Backend-pin guard raised by update_session on a cross-backend switch:
        "SessionBackendLockedError",
        # Terminal-archive guard raised by update_session on an archived row:
        "SessionArchivedError",
        # Localized user-facing copy for that guard's 409 body:
        "session_archived_message",
        # Reserved-row guard raised by archive_session for the workspace-notifications
        # session (D5 rung (5)'s home): archive is terminal, and archiving that row
        # would silently swallow every later caller-less failure notice.
        "ReservedSessionError",
        # Stable authorization denial translated by API callers:
        "ProjectAccessDeniedError",
    }
    assert set(sessions_service.__all__) == expected | {"SESSION_ARCHIVED_I18N_KEY"}
    for name in expected:
        assert callable(getattr(sessions_service, name))
    # The one non-callable member: the i18n key constant the parity guard pins.
    assert isinstance(sessions_service.SESSION_ARCHIVED_I18N_KEY, str)


def test_each_workbench_function_delegates_to_storage():
    """The conn-based workbench CRUD functions are thin re-exports of the
    storage module. The C2 reservation helpers wrap a different storage
    class (engine-owning) so they are not part of this delegation check.
    """
    for name in (
        "archive_session",
        "backfill_session_title",
        "count_bound_resources",
        "create_session",
        "get_session",
        "is_session_archived",
        "list_sessions",
        "set_agent_status",
        "touch_session",
        "touch_session_agent_activity",
        "update_session",
    ):
        assert getattr(sessions_service, name) is getattr(storage_sessions, name)


# --- Round-trip via the public API ------------------------------------


def test_create_and_get_round_trip(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        created = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="contract-bot",
        )

    assert created["scope_id"] == scope_id
    assert created["agent_backend"] == "claude"
    assert created["agent_name"] == "contract-bot"
    assert created["agent_variant"] == "claude"

    with engine.connect() as conn:
        fetched = sessions_service.get_session(conn, created["id"])
    assert fetched["id"] == created["id"]
    assert fetched["agent_name"] == "contract-bot"
    assert fetched["agent_variant"] == "claude"


def test_create_session_without_title_persists_null(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        missing = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="")
        blank = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="", title="   ")

    assert missing["title"] is None
    assert blank["title"] is None


def test_update_then_list_reflects_changes(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
        )
        sessions_service.update_session(
            conn,
            session["id"],
            title="renamed",
            model="claude-sonnet-4-6",
        )

    with engine.connect() as conn:
        page = sessions_service.list_sessions(conn, scope_id=scope_id)
    assert len(page["sessions"]) == 1
    assert page["sessions"][0]["title"] == "renamed"
    assert page["sessions"][0]["model"] == "claude-sonnet-4-6"


def test_project_session_pins_sort_first_and_paginate_across_groups(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)

        def create(title: str, last_active_at: str, *, pinned: bool = False) -> dict:
            row = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                title=title,
            )
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == row["id"])
                .values(last_active_at=last_active_at)
            )
            if pinned:
                row = sessions_service.update_session(conn, row["id"], pinned=True)
            return row

        create("unpinned-new", "2026-07-24T05:00:00Z")
        create("pinned-old", "2026-07-24T01:00:00Z", pinned=True)
        create("pinned-new", "2026-07-24T04:00:00Z", pinned=True)
        create("unpinned-old", "2026-07-24T02:00:00Z")
        create("pinned-mid", "2026-07-24T03:00:00Z", pinned=True)

    with engine.connect() as conn:
        first = sessions_service.list_sessions(conn, scope_id=scope_id, limit=2)
        second = sessions_service.list_sessions(
            conn,
            scope_id=scope_id,
            limit=2,
            before_id=first["next_before_id"],
        )
        third = sessions_service.list_sessions(
            conn,
            scope_id=scope_id,
            limit=2,
            before_id=second["next_before_id"],
        )

    assert [(row["title"], row["pinned"]) for row in first["sessions"]] == [
        ("pinned-new", True),
        ("pinned-mid", True),
    ]
    assert [(row["title"], row["pinned"]) for row in second["sessions"]] == [
        ("pinned-old", True),
        ("unpinned-new", False),
    ]
    assert [(row["title"], row["pinned"]) for row in third["sessions"]] == [
        ("unpinned-old", False),
    ]


def test_update_session_requires_boolean_pin_state(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")
        with pytest.raises(ValueError, match="pinned must be a boolean"):
            sessions_service.update_session(conn, session["id"], pinned="true")


def test_session_lists_only_include_foreground_sessions(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        foreground = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            title="Foreground",
        )
        background = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            title="Background",
            visibility="background",
        )

    with engine.connect() as conn:
        workbench = sessions_service.list_sessions(conn, scope_id=scope_id)
        cli_page = sessions_service.list_sessions_page(conn)
        direct = sessions_service.get_session(conn, background["id"])

    assert [row["id"] for row in workbench["sessions"]] == [foreground["id"]]
    assert [row["id"] for row in cli_page.items] == [foreground["id"]]
    assert direct["visibility"] == "background"


def test_list_sessions_title_query_filters_by_title(isolated_state):
    """``#``-mention search: case-insensitive title LIKE, escaping LIKE metachars."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", title="Review auth module"
        )
        sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", title="Deploy pipeline"
        )
        sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", title="100% coverage push"
        )

    with engine.connect() as conn:
        hit = sessions_service.list_sessions(conn, title_query="AUTH")
        miss = sessions_service.list_sessions(conn, title_query="nonexistent")
        literal = sessions_service.list_sessions(conn, title_query="100%")

    assert [s["title"] for s in hit["sessions"]] == ["Review auth module"]
    assert miss["sessions"] == []
    # The ``%`` is escaped, so it matches the literal "100%" title, not every row.
    assert [s["title"] for s in literal["sessions"]] == ["100% coverage push"]


def test_archive_marks_session(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
        )
        archived = sessions_service.archive_session(conn, session["id"])

    assert archived["status"] == "archived"

    with engine.connect() as conn:
        page = sessions_service.list_sessions(conn, scope_id=scope_id, status="active")
    assert page["sessions"] == [], "archived sessions should not appear in the active list"


def test_update_session_rejects_archived_session(isolated_state):
    """Archive is terminal: an archived row can never be renamed or re-routed.

    ``archive_session`` itself writes the row directly, so archiving does not trip
    the guard — only a later mutation attempt does. The archived payload stays
    fully readable via ``get_session`` (search + the read-only chat depend on it)."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude", title="Before"
        )["id"]
        sessions_service.archive_session(conn, sid)

        with pytest.raises(sessions_service.SessionArchivedError):
            sessions_service.update_session(conn, sid, title="Renamed")
        with pytest.raises(sessions_service.SessionArchivedError):
            sessions_service.update_session(conn, sid, agent_name="codex", agent_backend="codex")
        # Nothing was written, and the transcript row stays readable.
        after = sessions_service.get_session(conn, sid)
        assert after["title"] == "Before"
        assert after["agent_backend"] == "claude"
        assert after["status"] == "archived"


def test_update_session_present_null_clears_model_and_effort(isolated_state):
    """Switching to an agent with no default model/effort sends present nulls;
    update_session must CLEAR the columns (drop the prior agent's override),
    while omitting the fields leaves them untouched (Codex P2)."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="codex", model="gpt-5-codex", reasoning_effort="high"
        )
        sid = session["id"]
        # Present null → clear both.
        sessions_service.update_session(conn, sid, model=None, reasoning_effort=None)
    with engine.connect() as conn:
        cleared = sessions_service.get_session(conn, sid)
    assert cleared["model"] is None
    assert cleared["reasoning_effort"] is None

    # Omitting the fields leaves the (re-set) values untouched.
    with engine.begin() as conn:
        sessions_service.update_session(conn, sid, model="claude-sonnet-4-6", reasoning_effort="low")
        sessions_service.update_session(conn, sid, title="renamed")  # model/effort omitted
    with engine.connect() as conn:
        kept = sessions_service.get_session(conn, sid)
    assert kept["model"] == "claude-sonnet-4-6"
    assert kept["reasoning_effort"] == "low"
    assert kept["title"] == "renamed"


def test_update_session_scope_move_drops_stale_legacy_mapping(isolated_state):
    from config import paths
    from core.scheduled_tasks import resolve_session_id_target
    from storage.sessions_service import SQLiteSessionsService

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        original_scope_id = _seed_avibe_scope(conn)
        target_scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_moved",
            now="2026-05-26T13:00:00Z",
        )
        session = sessions_service.create_session(
            conn,
            scope_id=original_scope_id,
            agent_backend="claude",
            metadata={"legacy_scope_key": original_scope_id, "kept": True},
        )
        moved = sessions_service.update_session(conn, session["id"], scope_id=target_scope_id)

    assert moved["scope_id"] == target_scope_id
    assert moved["metadata"]["kept"] is True
    assert "legacy_scope_key" not in moved["metadata"]
    assert moved["session_anchor"] == f"avibe_proj_moved:session_{session['id']}"

    target = resolve_session_id_target(session["id"])
    assert target.scope_id == target_scope_id
    assert target.session_key.thread_id is None

    legacy = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        mappings = legacy.load_state().session_mappings
    finally:
        legacy.close()
    assert "avibe::proj_moved" in mappings
    assert original_scope_id not in mappings


def test_update_session_present_null_clears_agent_route(isolated_state):
    """The Chat header's "Default" item sends present nulls; update_session must
    clear an unpinned route instead of treating null as "field omitted"."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="",
            agent_name="codex",
            agent_id="agent-1",
            agent_variant="codex",
            model="gpt-5.5",
            reasoning_effort="high",
        )
        cleared = sessions_service.update_session(
            conn,
            session["id"],
            agent_id=None,
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
        )

    assert cleared["agent_id"] is None
    assert cleared["agent_name"] is None
    assert cleared["agent_backend"] == ""
    assert cleared["agent_variant"] == "default"
    assert cleared["model"] is None
    assert cleared["reasoning_effort"] is None


def test_update_session_marks_user_title_ownership(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        updated = sessions_service.update_session(conn, sid, title="  renamed  ")

    assert updated["title"] == "renamed"
    assert updated["metadata"]["title_source"] == "user"
    assert updated["metadata"]["title_user_modified_at"]


def test_update_session_empty_title_is_user_owned_clear(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude", title="Old")["id"]
        updated = sessions_service.update_session(conn, sid, title="")

    assert updated["title"] is None
    assert updated["metadata"]["title_source"] == "user"


def test_backfill_session_title_only_fills_empty_non_user_title(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="opencode")["id"]
        filled = sessions_service.backfill_session_title(
            conn,
            sid,
            title="Plan backend title",
            backend="opencode",
            source="backend",
            confidence="high",
            native_session_id="oc-1",
        )
        skipped = sessions_service.backfill_session_title(
            conn,
            sid,
            title="Should not replace",
            backend="opencode",
            source="backend",
        )

    assert filled is not None
    assert filled["title"] == "Plan backend title"
    assert filled["metadata"]["title_source"] == "backend"
    assert filled["metadata"]["title_backend"] == "opencode"
    assert filled["metadata"]["title_native_session_id"] == "oc-1"
    assert filled["metadata"]["title_confidence"] == "high"
    assert skipped is None

    with engine.connect() as conn:
        assert sessions_service.get_session(conn, sid)["title"] == "Plan backend title"


def test_backfill_session_title_does_not_override_user_owned_clear(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        sessions_service.update_session(conn, sid, title="")
        skipped = sessions_service.backfill_session_title(
            conn,
            sid,
            title="Derived",
            backend="claude",
            source="derived_first_prompt",
        )

    assert skipped is None
    with engine.connect() as conn:
        session = sessions_service.get_session(conn, sid)
    assert session["title"] is None
    assert session["metadata"]["title_source"] == "user"


# --- Live agent-runtime status (sidebar dot) --------------------------


def test_new_session_agent_status_defaults_idle(isolated_state):
    """A freshly created session starts idle, and the payload exposes it."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        created = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")
    assert created["agent_status"] == "idle"
    with engine.connect() as conn:
        page = sessions_service.list_sessions(conn, scope_id=scope_id)
    assert page["sessions"][0]["agent_status"] == "idle"


def test_set_agent_status_changes_and_reports_delta(isolated_state):
    """set_agent_status persists the value and returns True only on a real change."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        assert sessions_service.set_agent_status(conn, sid, "running") is True
        # Idempotent: same value reports no change (so the caller skips the broadcast).
        assert sessions_service.set_agent_status(conn, sid, "running") is False
        assert sessions_service.set_agent_status(conn, sid, "failed") is True
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, sid)["agent_status"] == "failed"


def test_set_agent_status_rejects_unknown_value(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        assert sessions_service.set_agent_status(conn, sid, "bogus") is False
        assert sessions_service.set_agent_status(conn, "ses-missing", "running") is False
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, sid)["agent_status"] == "idle"


def _bind_native(conn, session_id: str, native_id: str = "native-1") -> None:
    """Simulate the first turn's native bind (``bind_agent_session_by_id``)."""
    conn.execute(
        agent_sessions.update()
        .where(agent_sessions.c.id == session_id)
        .values(native_session_id=native_id)
    )


def test_update_session_backend_is_free_until_native_bind(isolated_state):
    """A concrete backend at creation (e.g. inherited from the project's default
    Agent) is a SOFT default: until a native conversation exists the session can
    be re-routed to a DIFFERENT backend, or cleared back to the default."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude"
        )["id"]
        # No native yet → cross-backend re-route is allowed.
        sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        assert sessions_service.get_session(conn, sid)["agent_backend"] == "codex"
        # ... and so is clearing back to the inherited default.
        sessions_service.update_session(
            conn,
            sid,
            agent_backend=None,
            agent_name=None,
            agent_id=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
        )
        assert not sessions_service.get_session(conn, sid)["agent_backend"]


def test_update_session_pending_fork_locks_backend_until_native_bind(isolated_state):
    """A fork target has no native id yet, but its pending fork metadata points
    at a source native session owned by one backend. Cross-backend changes would
    make the first turn fall back to a fresh native session, so the backend is
    locked until the fork binds. Same-backend agent/model overrides stay allowed.
    """

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="claude",
            metadata={
                "created_via": "session_fork",
                "fork_source_session_id": "source-session",
                "fork_source_native_session_id": "source-native",
                "fork_source_backend": "claude",
            },
        )["id"]

        sessions_service.update_session(conn, sid, agent_backend="claude", agent_name="claude-pro", model="opus")
        assert sessions_service.get_session(conn, sid)["agent_name"] == "claude-pro"

        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend=None, agent_name=None)


def test_update_session_locks_backend_once_native_exists(isolated_state):
    """Once the first turn bound a native conversation the backend is pinned for
    life — the native can only be resumed by the backend that created it.
    Same-backend agent/model changes stay allowed; a cross-backend switch or a
    clear back to default raises SessionBackendLockedError."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude"
        )["id"]
        _bind_native(conn, sid)
        # Same-backend change (different agent / model) is still allowed.
        sessions_service.update_session(conn, sid, agent_backend="claude", agent_name="claude-pro", model="opus")
        # Cross-backend switch is rejected.
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        # Clearing back to the inherited default is rejected too: a future
        # default switch could route the old session through another backend.
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend=None, agent_name=None)


def test_update_session_running_turn_locks_backend(isolated_state):
    """A RUNNING turn locks the backend even before the native is bound: the
    in-flight first turn is already executing on the current route and will bind
    its native shortly, so a mid-turn switch would be silently overwritten by
    the bind-time backfill or route queued follow-ups inconsistently. Same-
    backend changes stay allowed; a settled turn without a native (failed first
    turn) unlocks again so the user can re-route to recover."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude"
        )["id"]
        sessions_service.set_agent_status(conn, sid, "running")
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend=None, agent_name=None)
        # Same-backend agent/model change stays allowed mid-turn.
        sessions_service.update_session(conn, sid, agent_backend="claude", model="opus")
        # First turn failed before binding a native → switchable again to recover.
        sessions_service.set_agent_status(conn, sid, "failed")
        sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        assert sessions_service.get_session(conn, sid)["agent_backend"] == "codex"


def test_update_session_running_turn_locks_agent_less_session_too(isolated_state):
    """An agent-less session's first (global-default) turn also locks while
    running: a concrete pick would race the bind-time backend backfill the same
    way. Once settled without a native, the pick is allowed again."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="")["id"]
        sessions_service.set_agent_status(conn, sid, "running")
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        sessions_service.set_agent_status(conn, sid, "idle")
        sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        assert sessions_service.get_session(conn, sid)["agent_backend"] == "codex"


def test_update_session_backend_switch_reserves_writer_before_decision(isolated_state):
    """A route edit and first native bind are serialized before the decision read."""

    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude"
        )["id"]

    bind_engine = create_sqlite_engine()
    race = {"fired": False, "blocked": False}

    def bind_native_after_read(_conn, _cursor, statement, _params, _context, _executemany):
        if race["fired"] or not statement.lstrip().upper().startswith("SELECT AGENT_SESSIONS.ID"):
            return
        race["fired"] = True
        try:
            with bind_engine.begin() as bind_conn:
                bind_conn.exec_driver_sql("PRAGMA busy_timeout = 1")
                _bind_native(bind_conn, sid)
        except OperationalError as exc:
            race["blocked"] = "database is locked" in str(exc)

    event.listen(engine, "after_cursor_execute", bind_native_after_read)
    try:
        with engine.begin() as conn:
            sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
    finally:
        event.remove(engine, "after_cursor_execute", bind_native_after_read)

    assert race == {"fired": True, "blocked": True}
    with bind_engine.begin() as bind_conn:
        _bind_native(bind_conn, sid)
    with engine.connect() as conn:
        session = sessions_service.get_session(conn, sid)
        assert session["agent_backend"] == "codex"
        assert session["native_session_id"] == "native-1"


def test_update_session_legacy_blank_backend_keeps_initial_pin_escape(isolated_state):
    """Legacy agent-less rows whose native predates the bind-time backend
    backfill don't know which backend owns their native; the empty -> concrete
    "initial pin" stays allowed so their picker isn't permanently stuck. The pin
    then locks the session like any other."""

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="")["id"]
        _bind_native(conn, sid)
        # Empty -> concrete is the initial pin, even with a native bound.
        sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
        assert sessions_service.get_session(conn, sid)["agent_backend"] == "codex"
        # Now pinned: a different backend is rejected.
        with pytest.raises(sessions_service.SessionBackendLockedError):
            sessions_service.update_session(conn, sid, agent_backend="claude", agent_name="claude")


# --- HFR-247: the override marker must not outlive the columns it describes ----


def test_workbench_default_action_drops_the_explicit_override_marker(isolated_state, tmp_path):
    """HFR-247 — the Chat header's "Default" must actually restore the defaults.

    ``agent_sessions.metadata_json.explicit_setting_overrides`` tells dispatch
    that this session's NULL ``model`` / ``reasoning_effort`` are a deliberate pin
    ("inherit nothing"), not the ordinary "inherit from the Agent". Only the
    ``create_once`` rebind wrote it -- but any writer of those columns replaces
    what the marker describes. ``update_session`` only rewrote ``metadata_json``
    on a title change or a scope move, so the Workbench "Default" action (present
    nulls for the whole route) left the marker in place: the session then showed a
    cleared route, and every turn still ran with NO model and NO reasoning effort
    while claiming to run as the default Agent. The control looked like it worked
    and routed nothing.
    """

    import asyncio

    from config import paths
    from core.handlers.message_handler import MessageHandler
    from core.internal_server import _build_session_context
    from core.vibe_agents import VibeAgentStore
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY
    from tests.test_scheduled_tasks import _DispatchController, _DispatchSessionHandler

    workdir = tmp_path / "workdir"
    workdir.mkdir()

    agent_store = VibeAgentStore()
    try:
        agent_store.ensure_builtin_default_agents(["codex"])
        agent_store.set_default_agent_name("codex")
        # The Agent the cleared session must fall back to. Distinctive values so
        # "inherited the Agent's defaults" cannot pass on a None == None compare.
        default_agent = agent_store.update(
            "codex", model="gpt-5.5-default", reasoning_effort="xhigh"
        )
    finally:
        agent_store.close()
    assert default_agent.model == "gpt-5.5-default"

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn, workdir=str(workdir))
        # A session that pins NOTHING on purpose -- the shape a preserved
        # ``create_once`` rebind reserves (D3) and the user then opens in Chat.
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="codex",
            agent_variant="codex",
            model=None,
            reasoning_effort=None,
            metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
        )
        sid = session["id"]

        # An UNRELATED edit must not touch a marker entry it knows nothing about.
        renamed = sessions_service.update_session(conn, sid, title="Nightly digest")
        assert renamed["metadata"][SESSION_SETTINGS_OVERRIDE_KEY] == [
            "model",
            "reasoning_effort",
        ], "a title-only edit dropped an override marker it never wrote"

        # The Chat header's "Default" item: present nulls for the whole route.
        cleared = sessions_service.update_session(
            conn,
            sid,
            agent_id=None,
            agent_name=None,
            agent_backend=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
        )

    assert cleared["model"] is None
    assert cleared["reasoning_effort"] is None
    assert SESSION_SETTINGS_OVERRIDE_KEY not in cleared["metadata"], (
        "'Default' replaced model/reasoning_effort but left the explicit-override "
        "marker, so dispatch keeps honouring a pin the user just removed"
    )
    # The title metadata the earlier edit wrote is still there: reconciling the
    # marker must compose with the other metadata writers, not replace them.
    assert cleared["metadata"]["title_source"] == "user"

    # The deliverable is the turn, not the row. Build the REAL workbench dispatch
    # context from the stored row and run the REAL MessageHandler.
    context = _build_session_context(sid, message_id="m1")
    controller = _DispatchController(paths.get_sqlite_state_path(), workdir)
    handler = MessageHandler(controller)
    handler.set_session_handler(_DispatchSessionHandler(str(workdir)))
    controller.message_handler = handler
    controller.session_handler = handler.session_handler
    try:
        asyncio.run(handler.handle_user_message(context, "hello"))
    finally:
        controller.sessions.close()
        controller.vibe_agent_store.close()

    assert len(controller.agent_service.dispatched) == 1
    backend_name, request = controller.agent_service.dispatched[0]
    assert backend_name == default_agent.backend
    assert request.vibe_agent_name == default_agent.name
    assert request.vibe_agent_model == "gpt-5.5-default", (
        f"dispatch handed the backend model={request.vibe_agent_model!r}: the stale "
        "marker still forces the cleared session's NULL, so 'Default' changed the "
        "stored row and nothing else"
    )
    assert request.vibe_agent_reasoning_effort == "xhigh", (
        f"dispatch handed the backend reasoning_effort="
        f"{request.vibe_agent_reasoning_effort!r} instead of the default Agent's"
    )


# --- Agent-activity rank (session-list ordering) ----------------------
#
# ``touch_session`` records input; ``touch_session_agent_activity`` records the
# session's own agent producing output, so a session working unattended stops
# sinking below sessions idle since their last user message. It carries its own
# rate limit because it is called once per agent message and tool-call event.


def _rank_stamp_shapes(instant: datetime) -> dict[str, str]:
    """One stored ``last_active_at`` text per shape the column holds, each
    naming the same ``instant``.

    Seeded by shape rather than by case so a writer added later is covered by
    construction. The shapes come from the writers:
    ``storage.workbench_sessions_service`` and ``storage.agent_session_rows``
    write ``strftime("%Y-%m-%dT%H:%M:%SZ")``; ``storage.sessions_service``
    writes ``datetime.isoformat()``, which omits the fractional part when it is
    exactly zero — one writer, two shapes. Naive rows predate the tz-aware
    writers. Text ordering disagrees with instant ordering across these, which
    is why the throttle compares parsed times instead of strings.
    """

    return {
        "z_second": instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "z_microsecond": instant.isoformat().replace("+00:00", "Z"),
        "offset_second": instant.replace(microsecond=0).isoformat(),
        "offset_microsecond": instant.isoformat(),
        "naive_second": instant.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _seed_session_with_stamp(conn, scope_id: str, title: str, stamp) -> str:
    session_id = sessions_service.create_session(
        conn, scope_id=scope_id, agent_backend="claude", title=title
    )["id"]
    conn.execute(
        agent_sessions.update()
        .where(agent_sessions.c.id == session_id)
        .values(last_active_at=stamp)
    )
    return session_id


def _freeze_rank_clock(monkeypatch, instant: datetime) -> None:
    """Pin the helper's own clock so the assertions describe the interval
    rather than the wall clock the test happens to run on."""

    monkeypatch.setattr(
        storage_sessions,
        "_utc_now_iso",
        lambda: instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def test_agent_activity_rank_is_throttled_to_its_interval(isolated_state, monkeypatch):
    """Agent output arrives at tool-call rate, so the rank must move at most
    once per interval however many messages land inside it — and must move
    again on the first call past it.
    """
    interval = storage_sessions.AGENT_ACTIVITY_RANK_INTERVAL_SECONDS
    start = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        session_id = _seed_session_with_stamp(
            conn, scope_id, "working", start.strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def bumps_over(offsets: list[int]) -> list[bool]:
        moved = []
        for offset in offsets:
            _freeze_rank_clock(monkeypatch, start + timedelta(seconds=offset))
            with engine.begin() as conn:
                moved.append(sessions_service.touch_session_agent_activity(conn, session_id))
        return moved

    # A burst inside one interval: the stamp is already that recent, so no write
    # lands however many messages the agent emits.
    assert bumps_over([1, 2, 5, interval - 1]) == [False, False, False, False]
    # First call at/after the interval moves it, and re-arms the window.
    assert bumps_over([interval, interval + 1, 2 * interval - 1]) == [True, False, False]
    assert bumps_over([2 * interval]) == [True]

    with engine.connect() as conn:
        stored = sessions_service.get_session(conn, session_id)
    assert stored["last_active_at"] == (start + timedelta(seconds=2 * interval)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_agent_activity_rank_decides_by_instant_for_every_stored_shape(
    isolated_state, monkeypatch
):
    """The throttle decision follows the instant a stamp names, in every shape
    the column stores — not the text, which sorts differently across shapes.
    """
    interval = storage_sessions.AGENT_ACTIVITY_RANK_INTERVAL_SECONDS
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(seconds=5, microseconds=-123456)
    stale = now - timedelta(seconds=interval * 2, microseconds=-123456)

    engine = create_sqlite_engine()
    seeded: dict[tuple[str, str], str] = {}
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        for age, instant in (("fresh", fresh), ("stale", stale)):
            for shape, stamp in _rank_stamp_shapes(instant).items():
                seeded[(age, shape)] = _seed_session_with_stamp(
                    conn, scope_id, f"{age}-{shape}", stamp
                )

    _freeze_rank_clock(monkeypatch, now)
    moved = {}
    with engine.begin() as conn:
        for key, session_id in seeded.items():
            moved[key] = sessions_service.touch_session_agent_activity(conn, session_id)

    # Every shape, one property: inside the interval the rank holds; past it, it moves.
    assert moved == {key: key[0] == "stale" for key in seeded}


def test_agent_activity_rank_bumps_a_row_it_cannot_date(isolated_state, monkeypatch):
    """A stamp the database cannot parse must fail open. Holding the rank would
    freeze such a row at the bottom of the list forever; moving it costs one
    write per interval, the same as any other row.
    """
    engine = create_sqlite_engine()
    undatable = {"null": None, "empty": "", "garbage": "not-a-timestamp"}
    seeded = {}
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        for name, stamp in undatable.items():
            seeded[name] = _seed_session_with_stamp(conn, scope_id, name, stamp)

    _freeze_rank_clock(monkeypatch, datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc))
    with engine.begin() as conn:
        moved = {
            name: sessions_service.touch_session_agent_activity(conn, session_id)
            for name, session_id in seeded.items()
        }
    assert moved == dict.fromkeys(undatable, True)

    with engine.connect() as conn:
        stamps = {
            name: sessions_service.get_session(conn, session_id)["last_active_at"]
            for name, session_id in seeded.items()
        }
    assert stamps == dict.fromkeys(undatable, "2026-09-02T12:00:00Z")


def test_agent_activity_rank_lifts_a_session_working_unattended(isolated_state, monkeypatch):
    """The user-facing property this exists for: a session whose agent is still
    working outranks one whose user spoke more recently but has since gone
    quiet. Ordering is read through the same list the sidebar renders.
    """
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        working = _seed_session_with_stamp(conn, scope_id, "agent-working", "2026-09-02T11:00:00Z")
        replied = _seed_session_with_stamp(conn, scope_id, "user-replied", "2026-09-02T11:30:00Z")

    with engine.connect() as conn:
        before = [row["title"] for row in sessions_service.list_sessions(conn, scope_id=scope_id)["sessions"]]
    assert before == ["user-replied", "agent-working"]

    # The unattended session's agent emits one message: no user input at all.
    _freeze_rank_clock(monkeypatch, datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc))
    with engine.begin() as conn:
        assert sessions_service.touch_session_agent_activity(conn, working) is True

    with engine.connect() as conn:
        after = [row["title"] for row in sessions_service.list_sessions(conn, scope_id=scope_id)["sessions"]]
    assert after == ["agent-working", "user-replied"]
    # The session nobody touched kept its rank — the bump is not a global re-sort.
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, replied)["last_active_at"] == "2026-09-02T11:30:00Z"


# Every value the session lifecycle ``status`` column holds. The rank is one more
# write against a row whose archived state is terminal, and the repository
# enforces that with a ``status != 'archived'`` predicate on every write rather
# than a per-caller check, so the property is stated over the whole domain
# instead of over the one case a reviewer happened to name.
SESSION_LIFECYCLE_STATUSES = ("active", "archived")


def test_agent_activity_rank_never_writes_an_archived_row(isolated_state, monkeypatch):
    """Archive is terminal, including for late output.

    ``archive_session`` commits the archive first and cancels the in-flight turn
    best-effort afterwards, so a turn can still be emitting into an
    already-archived session. Every seeded row carries the same stale stamp, so
    the throttle would allow all of them and the status predicate is the only
    thing deciding — and the expectation is derived from the rule, so a lifecycle
    status added later is covered rather than silently skipped.
    """
    stale = "2020-01-01T00:00:00Z"
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        seeded = {
            status: _seed_session_with_stamp(conn, scope_id, f"s-{status}", stale)
            for status in SESSION_LIFECYCLE_STATUSES
        }
        for status, session_id in seeded.items():
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .values(status=status)
            )

    def archived_row() -> dict:
        with engine.connect() as conn:
            return dict(
                conn.execute(
                    agent_sessions.select().where(agent_sessions.c.id == seeded["archived"])
                )
                .mappings()
                .one()
            )

    before = archived_row()

    _freeze_rank_clock(monkeypatch, datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc))
    with engine.begin() as conn:
        moved = {
            status: sessions_service.touch_session_agent_activity(conn, session_id)
            for status, session_id in seeded.items()
        }
    assert moved == {status: status != "archived" for status in SESSION_LIFECYCLE_STATUSES}

    # Nothing on the archived row moved, not merely its rank: the return value is
    # a claim about the write, and ``updated_at`` shifting would still be a
    # mutation of terminal metadata. Compared against the row as it was rather
    # than against a chosen date, so the assertion holds whatever day it runs on.
    assert archived_row() == before
    assert before["last_active_at"] == stale
