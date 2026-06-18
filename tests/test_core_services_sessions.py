"""Contract tests for ``core.services.sessions``.

This module is the public business API for the ``agent_sessions`` table.
The tests here pin the shape so callers (UI server, CLI, IM adapter)
can rely on it across refactors. Any change that breaks the row payload
shape or the public function set must update this file in lock-step.
"""

from __future__ import annotations

import sys
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
        "reset_running_agent_status",
        "set_agent_status",
        "touch_session",
        "update_session",
        # Legacy IM-style reservation helpers added in C2 for the CLI:
        "reserve_agent_session",
        "reserve_private_agent_session",
        # Backend-pin guard raised by update_session on a cross-backend switch:
        "SessionBackendLockedError",
    }
    assert set(sessions_service.__all__) == expected
    for name in expected:
        assert callable(getattr(sessions_service, name))


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
        "reset_running_agent_status",
        "set_agent_status",
        "touch_session",
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


def test_reset_running_agent_status_clears_only_running(isolated_state):
    """Startup recovery: stale ``running`` → ``idle``; failed/idle untouched."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        running = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        failed = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="claude")["id"]
        sessions_service.set_agent_status(conn, running, "running")
        sessions_service.set_agent_status(conn, failed, "failed")
        reset = sessions_service.reset_running_agent_status(conn)
    assert reset == 1
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, running)["agent_status"] == "idle"
        assert sessions_service.get_session(conn, failed)["agent_status"] == "failed"


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


def test_update_session_backend_switch_loses_race_with_native_bind(isolated_state):
    """The lock guard is read-then-write and the first turn's native bind can
    commit in between (the SELECT runs before the UPDATE takes the write lock).
    The UPDATE re-asserts the lock in its WHERE predicate, so a cross-backend
    switch that loses the race raises instead of stamping a backend that
    mismatches the backend owning the just-bound native."""

    from sqlalchemy import event

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_avibe_scope(conn)
        sid = sessions_service.create_session(
            conn, scope_id=scope_id, agent_backend="claude", agent_name="claude"
        )["id"]

    bind_engine = create_sqlite_engine()
    fired = {"done": False}

    def bind_native_before_update(_conn, _cursor, statement, _params, _context, _executemany):
        # Right before update_session's UPDATE executes — i.e. AFTER its lock
        # check read "no native yet" — land the first turn's native bind on a
        # separate connection, exactly the racing interleave.
        if fired["done"] or not statement.lstrip().upper().startswith("UPDATE AGENT_SESSIONS"):
            return
        fired["done"] = True
        with bind_engine.begin() as bind_conn:
            _bind_native(bind_conn, sid)

    event.listen(engine, "before_cursor_execute", bind_native_before_update)
    try:
        with engine.begin() as conn:
            with pytest.raises(sessions_service.SessionBackendLockedError):
                sessions_service.update_session(conn, sid, agent_backend="codex", agent_name="codex")
    finally:
        event.remove(engine, "before_cursor_execute", bind_native_before_update)

    assert fired["done"], "race interleave never triggered — test setup is broken"
    with engine.connect() as conn:
        assert sessions_service.get_session(conn, sid)["agent_backend"] == "claude"


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
