from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from config import paths
from config.v2_sessions import ActivePollInfo, SessionState, SessionsStore
from modules.sessions_facade import SessionsFacade
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.models import agent_sessions
from storage.sessions_service import SQLiteSessionsService, resolve_scope_from_legacy_key
from storage.settings_service import upsert_scope


def test_sessions_store_uses_sqlite_without_rewriting_legacy_json(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    original = json.dumps(
        {
            "session_mappings": {
                "C123": {
                    "opencode": {
                        "slack_123.456:/repo": "session-old",
                    }
                }
            },
            "active_polls": {
                "oc-1": {
                    "opencode_session_id": "oc-1",
                    "base_session_id": "base-1",
                    "channel_id": "C123",
                    "thread_id": "123.456",
                    "settings_key": "slack::C123",
                    "working_path": "/repo",
                }
            },
        },
        indent=2,
    )
    sessions_path.write_text(original, encoding="utf-8")

    store = SessionsStore(sessions_path)
    try:
        store.migrate_active_polls("slack")
        store.migrate_session_mappings("slack")
        store.add_active_poll(
            ActivePollInfo(
                opencode_session_id="oc-2",
                base_session_id="base-2",
                channel_id="C999",
                thread_id="999.000",
                settings_key="C999",
                working_path="/repo",
                platform="slack",
            )
        )
    finally:
        store.close()

    reloaded = SessionsStore(sessions_path)
    try:
        # Legacy OpenCode ``base:/cwd`` composite is normalised to the bare anchor
        # on import; the native id is preserved, but cwd is not inferred from the
        # anchor suffix.
        assert reloaded.state.session_mappings["slack::C123"]["opencode"]["slack_123.456"] == "session-old"
        engine = create_sqlite_engine(reloaded.db_path)
        with engine.connect() as conn:
            workdir = conn.execute(
                select(agent_sessions.c.workdir).where(agent_sessions.c.session_anchor == "slack_123.456")
            ).scalar_one()
        engine.dispose()
        assert workdir is None
        assert reloaded.state.active_polls["oc-1"]["settings_key"] == "C123"
        assert reloaded.state.active_polls["oc-1"]["platform"] == "slack"
        assert reloaded.get_active_poll("oc-2") is not None
        assert sessions_path.read_text(encoding="utf-8") == original
    finally:
        reloaded.close()


def test_sessions_store_reloads_external_sqlite_writes(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    external = SQLiteSessionsService(tmp_path / "vibe.sqlite")
    try:
        assert store.get_active_poll("oc-external") is None

        external.save_state(
            SessionState(
                active_polls={
                    "oc-external": ActivePollInfo(
                        opencode_session_id="oc-external",
                        base_session_id="base",
                        channel_id="C1",
                        thread_id="t1",
                        settings_key="C1",
                        working_path="/repo",
                        platform="slack",
                    ).to_dict()
                }
            )
        )

        store.maybe_reload()

        poll = store.get_active_poll("oc-external")
        assert poll is not None
        assert poll.platform == "slack"
        assert poll.channel_id == "C1"
    finally:
        external.close()
        store.close()


def test_sessions_facade_preserves_active_poll_session_key(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    facade = SessionsFacade(store)
    try:
        facade.add_active_poll(
            opencode_session_id="oc-typed",
            base_session_id="slack_171717.123",
            channel_id="C1",
            thread_id="171717.123",
            settings_key="C1",
            working_path="/repo",
            baseline_message_ids=[],
            platform="slack",
            session_key="slack::channel::C1",
        )

        reloaded = SessionsStore(sessions_path)
        try:
            poll = reloaded.get_active_poll("oc-typed")
            assert poll is not None
            assert poll.session_key == "slack::channel::C1"
        finally:
            reloaded.close()
    finally:
        store.close()


def test_sqlite_sessions_service_preserves_agent_session_ids_on_save(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        state = SessionState(
            session_mappings={
                "slack::C123": {
                    "codex": {
                        "slack_171717.123": "thread-native-1",
                    }
                }
            }
        )
        service.save_state(state)

        engine = create_sqlite_engine(db_path)
        try:
            with engine.connect() as conn:
                original_id = conn.execute(select(agent_sessions.c.id)).scalar_one()
        finally:
            engine.dispose()

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "thread-native-1",
                        }
                    }
                },
                active_polls={
                    "oc-1": ActivePollInfo(
                        opencode_session_id="oc-1",
                        base_session_id="base",
                        channel_id="C123",
                        thread_id="171717.123",
                        settings_key="C123",
                        working_path="/repo",
                        platform="slack",
                    ).to_dict()
                },
            )
        )

        engine = create_sqlite_engine(db_path)
        try:
            with engine.connect() as conn:
                saved_id = conn.execute(select(agent_sessions.c.id)).scalar_one()
        finally:
            engine.dispose()

        assert saved_id == original_id
    finally:
        service.close()


def test_sqlite_sessions_service_updates_logical_agent_session_on_save(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "thread-native-1",
                        }
                    }
                }
            )
        )
        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "thread-native-2",
                        }
                    }
                }
            )
        )

        engine = create_sqlite_engine(db_path)
        try:
            with engine.connect() as conn:
                rows = conn.execute(select(agent_sessions.c.native_session_id)).scalars().all()
        finally:
            engine.dispose()

        assert rows == ["thread-native-2"]
        assert service.load_state().session_mappings["slack::C123"]["codex"]["slack_171717.123"] == "thread-native-2"
    finally:
        service.close()


def test_sqlite_sessions_service_reserves_then_binds_agent_session_id(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        from storage.models import scope_settings

        with service.engine.begin() as conn:
            scope_id = upsert_scope(conn, "slack", "channel", "C123", now="2026-06-04T05:00:00Z")
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path / "repo"),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-04T05:00:00Z",
                    updated_at="2026-06-04T05:00:00Z",
                )
            )
        reserved_id = service.ensure_agent_session_id(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
        )
        assert reserved_id is not None
        assert service.load_state().session_mappings["slack::channel::C123"]["codex"]["slack_171717.123"] == ""

        bound_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
            native_session_id="thread-native-1",
        )

        assert bound_id == reserved_id
        assert service.get_agent_session_row_id(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
        ) == reserved_id
        assert (
            service.load_state().session_mappings["slack::channel::C123"]["codex"]["slack_171717.123"]
            == "thread-native-1"
        )
    finally:
        service.close()


@pytest.mark.parametrize("legacy_backend", ["", "default"])
def test_bind_agent_session_upgrades_legacy_default_anchor_row(tmp_path: Path, legacy_backend: str) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        from storage.agent_session_rows import create_agent_session_row
        from storage.models import scope_settings

        with service.engine.begin() as conn:
            scope_id = upsert_scope(conn, "slack", "channel", "C123", now="2026-06-04T05:00:00Z")
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path / "repo"),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-04T05:00:00Z",
                    updated_at="2026-06-04T05:00:00Z",
                )
            )
            legacy_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_171717.123",
                agent_backend=legacy_backend,
                agent_variant="default",
                workdir=str(tmp_path / "repo"),
                native_session_id="",
                require_workdir=False,
            )

        bound_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
            native_session_id="thread-native-1",
        )

        assert bound_id == legacy_id
        with service.engine.connect() as conn:
            rows = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.agent_variant,
                    agent_sessions.c.native_session_id,
                )
            ).all()
        assert rows == [(legacy_id, "codex", "codex", "thread-native-1")]
    finally:
        service.close()


def test_sqlite_sessions_service_binds_reserved_agent_session_by_id(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    default_workdir = tmp_path / "runtime-default"
    service = SQLiteSessionsService(db_path)
    try:
        with patch(
            "storage.sessions_service.V2Config.load",
            return_value=SimpleNamespace(runtime=SimpleNamespace(default_cwd=str(default_workdir))),
        ):
            reserved_id = service.reserve_agent_session(
                scope_key="slack::channel::C123",
                agent_backend="opencode",
                session_anchor="slack_private-agent",
                agent_name="opencode",
            )
        assert reserved_id is not None

        bound_id = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="oc-session-1",
            workdir="/repo",
            vibe_agent_id="agent-codex",
            vibe_agent_name="codex",
            vibe_agent_backend="codex",
        )

        assert bound_id == reserved_id
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["native_session_id"] == "oc-session-1"
        assert row["workdir"] == str(default_workdir)
        assert row["agent_id"] == "agent-codex"
        assert row["agent_name"] == "codex"
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
    finally:
        service.close()


def test_materialize_agent_session_route_fills_empty_columns_only(tmp_path: Path) -> None:
    """Turn-start materialization pins the resolved model/effort into empty
    columns, never overwrites an existing value, and — because it runs at
    dispatch time — a later explicit clear (update_session storing NULL) is a
    fact the NEXT turn re-pins from its own resolution, not something a stale
    value from this turn may undo."""
    db_path = tmp_path / "vibe.sqlite"
    default_workdir = tmp_path / "runtime-default"
    service = SQLiteSessionsService(db_path)
    try:
        with patch(
            "storage.sessions_service.V2Config.load",
            return_value=SimpleNamespace(runtime=SimpleNamespace(default_cwd=str(default_workdir))),
        ):
            reserved_id = service.reserve_agent_session(
                scope_key="avibe::project::proj_abc",
                agent_backend="codex",
                session_anchor="avibe_ses1",
                agent_name="codex",
            )
        assert reserved_id is not None
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["model"] is None
        assert row["reasoning_effort"] is None

        # First turn resolves the Agent default → empty columns fill in.
        assert service.materialize_agent_session_route(
            reserved_id, model="gpt-5.5", reasoning_effort="xhigh"
        )
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["model"] == "gpt-5.5"
        assert row["reasoning_effort"] == "xhigh"

        # A later turn resolving a different route must NOT overwrite the pin.
        service.materialize_agent_session_route(reserved_id, model="gpt-5.4", reasoning_effort="low")
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["model"] == "gpt-5.5"
        assert row["reasoning_effort"] == "xhigh"

        # Explicit clear back to inherited (the chat-header "Default" pick →
        # update_session stores NULL): the cleared state persists —
        # materialization happens only at the START of a turn, so nothing later
        # in the old turn refills it.
        from storage.workbench_sessions_service import update_session

        with service.engine.begin() as conn:
            update_session(conn, reserved_id, model=None, reasoning_effort=None)
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["model"] is None
        assert row["reasoning_effort"] is None

        # No-op call shapes: nothing to pin → False, row untouched.
        assert not service.materialize_agent_session_route(reserved_id)
        assert not service.materialize_agent_session_route("ses_missing", model="gpt-5.5")
    finally:
        service.close()


def test_reserve_agent_session_uses_runtime_default_when_scope_workdir_missing(tmp_path: Path) -> None:
    from storage.models import scope_settings

    db_path = tmp_path / "vibe.sqlite"
    default_workdir = tmp_path / "runtime-default"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = upsert_scope(conn, "slack", "channel", "C123", now="2026-07-01T00:00:00Z")
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=None,
                    agent_name="codex",
                    agent_backend="codex",
                    agent_variant="codex",
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-07-01T00:00:00Z",
                    updated_at="2026-07-01T00:00:00Z",
                )
            )

        with patch(
            "storage.sessions_service.V2Config.load",
            return_value=SimpleNamespace(runtime=SimpleNamespace(default_cwd=str(default_workdir))),
        ):
            session_id = service.reserve_agent_session(
                scope_key="slack::channel::C123",
                agent_backend="codex",
                session_anchor="slack_171717.123:definition_test",
                agent_name="codex",
            )

        assert session_id is not None
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["workdir"] == str(default_workdir)
    finally:
        service.close()


def test_bind_agent_session_by_id_does_not_overwrite_existing_workdir(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = service.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_private-agent",
            agent_name="codex",
        )
        assert reserved_id is not None
        with service.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == reserved_id)
                .values(workdir="/repo/right")
            )
        service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="codex-native-1",
            workdir="/repo/right",
        )

        service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="codex-native-1",
            workdir="/tmp/test",
        )

        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["workdir"] == "/repo/right"
    finally:
        service.close()


def test_bind_agent_session_by_id_does_not_use_anchor_suffix_as_workdir(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    default_workdir = tmp_path / "runtime-default"
    service = SQLiteSessionsService(db_path)
    try:
        with patch(
            "storage.sessions_service.V2Config.load",
            return_value=SimpleNamespace(runtime=SimpleNamespace(default_cwd=str(default_workdir))),
        ):
            reserved_id = service.reserve_agent_session(
                scope_key="slack::channel::C123",
                agent_backend="codex",
                session_anchor="slack_scheduled:/tmp/test",
                agent_name="codex",
            )
        assert reserved_id is not None
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["workdir"] == str(default_workdir)

        service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="codex-native-1",
            workdir="/repo/right",
        )

        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["workdir"] == str(default_workdir)
    finally:
        service.close()


def test_bind_agent_session_by_id_does_not_derive_variant_from_vibe_agent_name(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = service.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="claude",
            session_anchor="slack_private-agent",
            agent_name="claude",
        )
        assert reserved_id is not None

        service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-1",
            vibe_agent_name="contract-bot",
        )

        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["agent_name"] == "contract-bot"
        assert row["agent_backend"] == "claude"
        assert row["agent_variant"] == "claude"
    finally:
        service.close()


def test_bind_agent_session_snapshots_workdir_without_anchor_suffix(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
            native_session_id="codex-native-1",
            workdir="/repo/original",
        )
        assert session_id is not None
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["workdir"] == "/repo/original"

        service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123",
            native_session_id="codex-native-1",
            workdir="/repo/changed",
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["workdir"] == "/repo/original"
    finally:
        service.close()


def test_find_session_for_anchor_returns_latest_regardless_of_backend(tmp_path: Path) -> None:
    """The new session model resolves a thread to ONE session by (scope, anchor),
    independent of backend. With legacy multi-backend rows for one anchor, the
    most-recently-active wins. Read-only — an unknown scope is never created."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        service.bind_agent_session(
            scope_key="slack::C123",
            agent_name="claude",
            session_anchor="slack_T1",
            native_session_id="claude-native",
        )
        service.bind_agent_session(
            scope_key="slack::C123",
            agent_name="codex",
            session_anchor="slack_T1",
            native_session_id="codex-native",
        )
        row = service.find_session_for_anchor(scope_key="slack::C123", session_anchor="slack_T1")
        assert row is not None
        # Most-recently-active row (codex, bound last) wins, regardless of backend.
        assert row["agent_backend"] == "codex"
        assert row["native_session_id"] == "codex-native"
        # Read-only: an unknown scope is never created.
        assert service.find_session_for_anchor(scope_key="slack::CNONE", session_anchor="slack_T1") is None
    finally:
        service.close()


def test_native_session_id_is_write_once_by_id(tmp_path: Path) -> None:
    """Once a row's native_session_id is bound, a second bind (fork / recapture /
    subagent / any fallback) must NOT overwrite it — the table is write-once."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = service.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="claude",
            session_anchor="slack_C123",
            agent_name="claude",
        )
        assert reserved_id is not None
        assert service.bind_agent_session_by_id(session_id=reserved_id, native_session_id="native-1") == reserved_id
        # A second bind with a DIFFERENT native must be ignored (kept = native-1).
        service.bind_agent_session_by_id(session_id=reserved_id, native_session_id="native-2")
        assert service.get_agent_session_by_id(reserved_id)["native_session_id"] == "native-1"
    finally:
        service.close()


def test_native_session_id_is_write_once_by_anchor(tmp_path: Path) -> None:
    """bind_agent_session (scope+anchor path) is also write-once."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        first = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123",
            native_session_id="native-1",
        )
        assert first is not None
        # Re-bind a different native on the same row → ignored.
        service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123",
            native_session_id="native-2",
        )
        assert service.get_agent_session_by_id(first)["native_session_id"] == "native-1"
    finally:
        service.close()


def test_sqlite_sessions_service_delete_agent_sessions_escapes_anchor_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        service.bind_agent_session(
            scope_key="slack::C123",
            agent_name="codex",
            session_anchor="slack_1_2%3",
            native_session_id="target-base",
        )
        service.bind_agent_session(
            scope_key="slack::C123",
            agent_name="codex",
            session_anchor="slack_1_2%3:/repo",
            native_session_id="target-child",
        )
        service.bind_agent_session(
            scope_key="slack::C123",
            agent_name="codex",
            session_anchor="slack_1A2X3:/repo",
            native_session_id="unrelated",
        )

        removed = service.delete_agent_sessions(
            scope_key="slack::C123",
            session_anchor_prefix="slack_1_2%3",
        )

        assert removed == 2
        mappings = service.load_state().session_mappings["slack::C123"]["codex"]
        assert mappings == {"slack_1A2X3:/repo": "unrelated"}
    finally:
        service.close()


def test_delete_agent_sessions_by_backend_removes_custom_variant_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "telegram::-100123", now="2026-06-18T07:30:00Z")
            assert scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="telegram_-100123",
                native_session_id="oc-native",
                workdir="/tmp",
                require_workdir=False,
            )
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="worker",
                session_anchor="telegram_-100123:claude",
                native_session_id="claude-native",
                workdir="/tmp",
                require_workdir=False,
            )
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="helper",
                session_anchor="telegram_-100123:codex",
                native_session_id="codex-native",
                workdir="/tmp",
                require_workdir=False,
            )

        removed = service.delete_agent_sessions(scope_key="telegram::-100123", agent_name="opencode")

        assert removed == 1
        assert service.find_session_for_anchor(scope_key="telegram::-100123", session_anchor="telegram_-100123") is None
        assert (
            service.find_session_for_anchor(scope_key="telegram::-100123", session_anchor="telegram_-100123:claude")
            is not None
        )
        assert (
            service.find_session_for_anchor(scope_key="telegram::-100123", session_anchor="telegram_-100123:codex")
            is not None
        )
    finally:
        service.close()


def test_delete_agent_session_by_backend_removes_custom_variant_row(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "telegram::-100123", now="2026-06-18T07:30:00Z")
            assert scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="telegram_-100123",
                native_session_id="oc-native",
                workdir="/tmp",
                require_workdir=False,
            )

        removed = service.delete_agent_session(
            scope_key="telegram::-100123",
            agent_name="opencode",
            session_anchor="telegram_-100123",
        )

        assert removed is True
        assert service.find_session_for_anchor(scope_key="telegram::-100123", session_anchor="telegram_-100123") is None
    finally:
        service.close()


def test_sessions_store_clear_backend_prunes_cached_custom_variant_rows(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "telegram::-100123", now="2026-06-18T07:30:00Z")
            assert scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="telegram_-100123",
                native_session_id="oc-native",
                workdir="/tmp",
                require_workdir=False,
            )

        store.load()
        assert store.state.session_mappings["telegram::-100123"]["reviewer"]["telegram_-100123"] == "oc-native"

        removed = store.clear_agent_sessions("telegram::-100123", "opencode")
        store.save()

        assert removed == 1
        assert "reviewer" not in store.state.session_mappings["telegram::-100123"]
        assert (
            store.find_session_for_anchor("telegram::-100123", "telegram_-100123")
            is None
        )
    finally:
        store.close()


def test_clear_session_base_can_target_typed_user_and_channel_scopes(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            user_scope_id = resolve_scope_from_legacy_key(
                conn, "telegram::user::58181121", now="2026-06-19T07:30:00Z"
            )
            channel_scope_id = resolve_scope_from_legacy_key(
                conn, "telegram::channel::58181121", now="2026-06-19T07:30:00Z"
            )
            assert user_scope_id is not None
            assert channel_scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=user_scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="telegram_58181121",
                native_session_id="claude-native",
                workdir="/tmp",
                require_workdir=False,
            )
            create_agent_session_row(
                conn,
                scope_id=channel_scope_id,
                agent_backend="opencode",
                agent_variant="opencode",
                session_anchor="telegram_58181121",
                native_session_id="oc-native",
                workdir="/tmp",
                require_workdir=False,
            )
        store.load()

        assert store.clear_session_base("telegram::user::58181121", "telegram_58181121") == 1
        assert store.find_session_for_anchor("telegram::user::58181121", "telegram_58181121") is None
        assert store.find_session_for_anchor("telegram::channel::58181121", "telegram_58181121") is not None

        assert store.clear_session_base("telegram::channel::58181121", "telegram_58181121") == 1
        assert store.find_session_for_anchor("telegram::channel::58181121", "telegram_58181121") is None
    finally:
        store.close()


def test_typed_user_scope_session_mapping_survives_reload_without_legacy_metadata(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            user_scope_id = resolve_scope_from_legacy_key(
                conn, "telegram::user::58181121", now="2026-06-19T07:30:00Z"
            )
            assert user_scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=user_scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="telegram_58181121",
                native_session_id="claude-native",
                workdir="/tmp",
                require_workdir=False,
            )

        store.load()

        assert store.state.session_mappings["telegram::user::58181121"]["claude"]["telegram_58181121"] == (
            "claude-native"
        )
        assert "telegram::58181121" not in store.state.session_mappings
    finally:
        store.close()


def test_slack_user_scope_session_mapping_keeps_legacy_untyped_key_on_reload(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            user_scope_id = resolve_scope_from_legacy_key(conn, "slack::user::U123", now="2026-06-19T07:30:00Z")
            assert user_scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=user_scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_171717.123",
                native_session_id="claude-native",
                workdir="/tmp",
                require_workdir=False,
            )

        store.load()

        assert store.state.session_mappings["slack::U123"]["claude"]["slack_171717.123"] == "claude-native"
        assert "slack::user::U123" not in store.state.session_mappings
    finally:
        store.close()


def test_sessions_store_remove_backend_session_prunes_cached_custom_variant_row(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "telegram::-100123", now="2026-06-18T07:30:00Z")
            assert scope_id is not None
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="telegram_-100123",
                native_session_id="oc-native",
                workdir="/tmp",
                require_workdir=False,
            )

        store.load()
        assert store.state.session_mappings["telegram::-100123"]["reviewer"]["telegram_-100123"] == "oc-native"

        removed = store.remove_agent_session("telegram::-100123", "opencode", "telegram_-100123")
        store.save()

        assert removed is True
        assert "reviewer" not in store.state.session_mappings["telegram::-100123"]
        assert (
            store.find_session_for_anchor("telegram::-100123", "telegram_-100123")
            is None
        )
    finally:
        store.close()


def test_sessions_store_lifecycle_updates_in_memory_state(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        reserved_id = store.ensure_agent_session_id("slack::C123", "codex", "slack_171717.123")
        assert reserved_id is not None
        assert store.state.session_mappings["slack::C123"]["codex"]["slack_171717.123"] == ""

        bound_id = store.bind_agent_session("slack::C123", "codex", "slack_171717.123", "thread-native-1")

        assert bound_id == reserved_id
        assert store.state.session_mappings["slack::C123"]["codex"]["slack_171717.123"] == "thread-native-1"
        assert store.get_agent_session_row_id("slack::C123", "codex", "slack_171717.123") == reserved_id
    finally:
        store.close()


def test_sessions_store_ensure_snapshots_workdir(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        reserved_id = store.ensure_agent_session_id(
            "slack::C123",
            "codex",
            "slack_171717.123",
            workdir="/repo/original",
        )
        assert reserved_id is not None
        with create_sqlite_engine(db_path=tmp_path / "vibe.sqlite").connect() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == reserved_id)).mappings().one()
        assert row["workdir"] == "/repo/original"
    finally:
        store.close()


def test_bind_agent_session_does_not_use_anchor_suffix_as_workdir(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = service.ensure_agent_session_id(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123:/tmp/test",
        )
        assert reserved_id is not None
        assert service.get_agent_session_by_id(reserved_id)["workdir"] is None

        bound_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_171717.123:/tmp/test",
            native_session_id="codex-native-1",
            workdir="/repo/original",
        )

        assert bound_id == reserved_id
        row = service.get_agent_session_by_id(reserved_id)
        assert row is not None
        assert row["workdir"] is None
    finally:
        service.close()


def test_sessions_store_bind_by_id_accepts_vibe_agent_backend(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        reserved_id = store.ensure_agent_session_id("slack::C123", "opencode", "slack_171717.123")
        assert reserved_id is not None

        bound_id = store.bind_agent_session_by_id(
            reserved_id,
            "oc-session-1",
            workdir="/repo",
            vibe_agent_id="agent-codex",
            vibe_agent_name="codex",
            vibe_agent_backend="codex",
        )

        assert bound_id == reserved_id
        with create_sqlite_engine(db_path=tmp_path / "vibe.sqlite").connect() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == reserved_id)).mappings().one()
        assert row["native_session_id"] == "oc-session-1"
        assert row["workdir"] is None
        assert row["agent_id"] == "agent-codex"
        assert row["agent_name"] == "codex"
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
    finally:
        store.close()


def test_sessions_store_lifecycle_survives_followup_save(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        reserved_id = store.ensure_agent_session_id("slack::C123", "opencode", "slack_171717.123:/repo")
        bound_id = store.bind_agent_session("slack::C123", "opencode", "slack_171717.123:/repo", "oc-session-1")
        store.add_active_poll(
            ActivePollInfo(
                opencode_session_id="oc-session-1",
                base_session_id="slack_171717.123",
                channel_id="C123",
                thread_id="171717.123",
                settings_key="C123",
                working_path="/repo",
                platform="slack",
            )
        )

        assert bound_id == reserved_id
        assert store.state.session_mappings["slack::C123"]["opencode"]["slack_171717.123:/repo"] == "oc-session-1"
    finally:
        store.close()

    reloaded = SessionsStore(sessions_path)
    try:
        assert (
            reloaded.state.session_mappings["slack::C123"]["opencode"]["slack_171717.123:/repo"] == "oc-session-1"
        )
        assert (
            reloaded.get_agent_session_row_id("slack::C123", "opencode", "slack_171717.123:/repo") == reserved_id
        )
    finally:
        reloaded.close()


def test_sessions_facade_cross_scope_alias_persists_after_reload_during_target_map_read(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    facade = SessionsFacade(store)
    try:
        store.bind_agent_session("slack::C123", "codex", "slack_source-1", "native-base")
        store.bind_agent_session("slack::C123", "codex", "slack_source-1:/repo", "native-workdir")
        external = SQLiteSessionsService(tmp_path / "vibe.sqlite")
        try:
            external.try_record_runtime_event("test_external_write", "reload-marker")
        finally:
            external.close()

        assert facade.alias_session_base_across_scopes(
            "slack::C123",
            "slack::C999",
            "slack_source-1",
            "slack_target-1",
        )

        reloaded = SessionsStore(sessions_path)
        try:
            target_map = reloaded.state.session_mappings["slack::C999"]["codex"]
            assert target_map["slack_target-1"] == "native-base"
            assert target_map["slack_target-1:/repo"] == "native-workdir"
        finally:
            reloaded.close()
    finally:
        store.close()


def test_sessions_store_atomically_claims_processed_messages_across_instances(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    first = SessionsStore(sessions_path)
    second = SessionsStore(sessions_path)
    try:
        assert first.try_add_to_processed_set("C123", "171717.123", "171717.456") is True
        assert second.try_add_to_processed_set("C123", "171717.123", "171717.456") is False
        assert second.is_message_in_processed_set("C123", "171717.123", "171717.456") is True
    finally:
        first.close()
        second.close()


def test_sessions_store_atomically_claims_runtime_events_across_instances(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    first = SessionsStore(sessions_path)
    second = SessionsStore(sessions_path)
    try:
        assert first.try_record_runtime_event("slack_event", "T1:Ev123", {"event_id": "Ev123"}) is True
        assert second.try_record_runtime_event("slack_event", "T1:Ev123", {"event_id": "Ev123"}) is False
        assert second.try_record_runtime_event("slack_event", "T1:Ev124", {"event_id": "Ev124"}) is True
    finally:
        first.close()
        second.close()


def test_sessions_store_save_preserves_external_processed_claims(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    stale = SessionsStore(sessions_path)
    external = SessionsStore(sessions_path)
    try:
        assert external.try_add_to_processed_set("C123", "171717.123", "171717.456") is True

        stale.add_active_poll(
            ActivePollInfo(
                opencode_session_id="oc-stale",
                base_session_id="base",
                channel_id="C123",
                thread_id="171717.123",
                settings_key="C123",
                working_path="/repo",
                platform="slack",
            )
        )

        reloaded = SessionsStore(sessions_path)
        try:
            assert reloaded.is_message_in_processed_set("C123", "171717.123", "171717.456") is True
        finally:
            reloaded.close()
    finally:
        stale.close()
        external.close()


def test_sessions_store_save_keeps_newest_external_processed_claims(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    stale = SQLiteSessionsService(db_path)
    external = SQLiteSessionsService(db_path)
    try:
        stale_state = SessionState(
            processed_message_ts={
                "C123": {
                    "171717.123": [f"old-{index:03d}" for index in range(200)],
                }
            }
        )
        for index in range(5):
            assert external.try_record_processed_message("C123", "171717.123", f"new-{index:03d}") is True

        stale.save_state(stale_state)

        processed = stale.load_state().processed_message_ts["C123"]["171717.123"]
        assert len(processed) == 200
        assert processed[-5:] == [f"new-{index:03d}" for index in range(5)]
        assert "old-000" not in processed
    finally:
        stale.close()
        external.close()


def test_sessions_store_save_prunes_stale_processed_claim_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        service.save_state(
            SessionState(
                processed_message_ts={
                    "C123": {
                        "171717.123": [f"msg-{index:03d}" for index in range(205)],
                    }
                }
            )
        )

        engine = create_sqlite_engine(db_path)
        try:
            with engine.connect() as conn:
                count = conn.execute(
                    select(agent_sessions.c.id)
                ).all()
                runtime_count = conn.exec_driver_sql(
                    "select count(*) from runtime_records where record_type = 'processed_message'"
                ).scalar_one()
        finally:
            engine.dispose()

        assert count == []
        assert runtime_count == 200
        processed = service.load_state().processed_message_ts["C123"]["171717.123"]
        assert processed[0] == "msg-005"
        assert processed[-1] == "msg-204"
    finally:
        service.close()


def test_sessions_store_hot_path_prunes_processed_claim_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        for index in range(205):
            assert service.try_record_processed_message("C123", "171717.123", f"msg-{index:03d}") is True

        engine = create_sqlite_engine(db_path)
        try:
            with engine.connect() as conn:
                runtime_count = conn.exec_driver_sql(
                    "select count(*) from runtime_records where record_type = 'processed_message'"
                ).scalar_one()
        finally:
            engine.dispose()

        assert runtime_count == 200
        processed = service.load_state().processed_message_ts["C123"]["171717.123"]
        assert processed[0] == "msg-005"
        assert processed[-1] == "msg-204"
    finally:
        service.close()


def test_sessions_store_prunes_processed_claims_with_escaped_like_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        for index in range(205):
            assert service.try_record_processed_message("C_1", "thread%1", f"msg-{index:03d}") is True
        assert service.try_record_processed_message("CA1", "threadX1", "other-thread-message") is True

        processed = service.load_state().processed_message_ts
        assert processed["C_1"]["thread%1"][0] == "msg-005"
        assert processed["C_1"]["thread%1"][-1] == "msg-204"
        assert processed["CA1"]["threadX1"] == ["other-thread-message"]
    finally:
        service.close()


def test_sessions_store_runtime_updates_do_not_flush_stale_snapshots(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    stale = SessionsStore(sessions_path)
    external = SessionsStore(sessions_path)
    try:
        stale.state.processed_message_ts = {
            "C123": {
                "171717.123": ["stale-message"],
            }
        }
        assert external.try_add_to_processed_set("C123", "171717.123", "external-message") is True

        stale.add_active_poll(
            ActivePollInfo(
                opencode_session_id="oc-stale",
                base_session_id="base",
                channel_id="C123",
                thread_id="171717.123",
                settings_key="C123",
                working_path="/repo",
                platform="slack",
            )
        )

        reloaded = SessionsStore(sessions_path)
        try:
            processed = reloaded._get_processed_set("C123", "171717.123")
            assert processed == ["external-message"]
            assert reloaded.get_active_poll("oc-stale") is not None
        finally:
            reloaded.close()
    finally:
        stale.close()
        external.close()


def test_sessions_store_bootstrap_uses_config_primary_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_config_path().write_text(
        json.dumps({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}}),
        encoding="utf-8",
    )
    paths.get_sessions_path().write_text(
        json.dumps(
            {
                "session_mappings": {"chat-1": {"codex": {"1774074591.762089:/repo": "session-1"}}},
                "active_polls": {
                    "oc-1": {
                        "opencode_session_id": "oc-1",
                        "base_session_id": "base-1",
                        "channel_id": "chat-1",
                        "thread_id": "1774074591.762089",
                        "settings_key": "chat-1",
                        "working_path": "/repo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = SessionsStore(paths.get_sessions_path())
    try:
        assert "lark::chat-1" in store.state.session_mappings
        assert store.state.active_polls["oc-1"]["platform"] == "lark"
    finally:
        store.close()


def test_sessions_store_custom_path_uses_sibling_config_primary_platform(tmp_path: Path) -> None:
    root = tmp_path / "custom-home"
    state_dir = root / "state"
    config_dir = root / "config"
    state_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}}),
        encoding="utf-8",
    )
    sessions_path = state_dir / "sessions.json"
    sessions_path.write_text(
        json.dumps(
            {
                "session_mappings": {"chat-2": {"codex": {"1774074591.762089:/repo": "session-2"}}},
                "active_polls": {
                    "oc-2": {
                        "opencode_session_id": "oc-2",
                        "base_session_id": "base-2",
                        "channel_id": "chat-2",
                        "thread_id": "1774074591.762089",
                        "settings_key": "chat-2",
                        "working_path": "/repo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = SessionsStore(sessions_path)
    try:
        assert "lark::chat-2" in store.state.session_mappings
        assert store.state.active_polls["oc-2"]["platform"] == "lark"
    finally:
        store.close()


def test_sessions_store_preserves_legacy_non_string_session_values(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        store.state.session_mappings = {"U1": {"claude": {"base": {"/repo": "session-1"}}}}
        store.save()
    finally:
        store.close()

    reloaded = SessionsStore(sessions_path)
    try:
        assert reloaded.state.session_mappings["U1"]["claude"]["base"]["/repo"] == "session-1"
    finally:
        reloaded.close()
