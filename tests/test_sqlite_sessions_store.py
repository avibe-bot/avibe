from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

import storage.sessions_service as sessions_service_module
from config import paths
from config.v2_sessions import ActivePollInfo, SessionState, SessionsStore
from modules.sessions_facade import SessionsFacade
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, agents, run_definitions
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


def test_save_state_does_not_relabel_existing_anchor_row_to_different_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_171717.123",
                native_session_id="claude-native",
                model="claude-sonnet-4-6",
                reasoning_effort="high",
                workdir="/tmp",
                metadata={
                    "legacy_scope_key": "slack::C123",
                    "explicit_setting_overrides": {
                        "model": True,
                        "reasoning_effort": True,
                    },
                },
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "claude"
        assert row["agent_variant"] == "claude"
        assert row["native_session_id"] == "claude-native"
        assert row["model"] == "claude-sonnet-4-6"
        assert row["reasoning_effort"] == "high"
        assert json.loads(row["metadata_json"])["explicit_setting_overrides"] == {
            "model": True,
            "reasoning_effort": True,
        }
        assert service.load_state().session_mappings["slack::C123"]["claude"]["slack_171717.123"] == "claude-native"
        assert "codex" not in service.load_state().session_mappings["slack::C123"]
    finally:
        service.close()


def test_save_state_skips_import_when_archived_row_owns_anchor(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            archived_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_171717.123",
                native_session_id="archived-native",
                status="archived",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        with service.engine.connect() as conn:
            rows = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.agent_variant,
                    agent_sessions.c.native_session_id,
                    agent_sessions.c.status,
                ).where(agent_sessions.c.scope_id == scope_id)
            ).mappings().all()

        assert rows == [
            {
                "id": archived_id,
                "agent_backend": "claude",
                "agent_variant": "claude",
                "native_session_id": "archived-native",
                "status": "archived",
            }
        ]
    finally:
        service.close()


def test_save_state_allows_legacy_default_anchor_row_to_adopt_imported_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="default",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
        assert row["native_session_id"] == "codex-native"
        assert service.load_state().session_mappings["slack::C123"]["codex"]["slack_171717.123"] == "codex-native"
    finally:
        service.close()


def test_save_state_adopts_unbound_reservation_across_backends(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id="agent-codex",
                agent_name="codex-reviewer",
                model="gpt-5-old",
                reasoning_effort="high",
                metadata={
                    "legacy_scope_key": "slack::C123",
                    "explicit_setting_overrides": ["model", "reasoning_effort"],
                },
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "claude": {
                            "slack_171717.123": "claude-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "claude"
        assert row["agent_variant"] == "claude"
        assert row["agent_id"] is None
        assert row["agent_name"] is None
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        assert "explicit_setting_overrides" not in json.loads(row["metadata_json"] or "{}")
        assert row["native_session_id"] == "claude-native"
    finally:
        service.close()


def test_save_state_adopts_unbound_reservation_when_agent_changes_on_same_backend(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                model="gpt-5-old",
                reasoning_effort="high",
                metadata={
                    "legacy_scope_key": "slack::C123",
                    "explicit_setting_overrides": ["model", "reasoning_effort"],
                },
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        assert "explicit_setting_overrides" not in json.loads(row["metadata_json"] or "{}")
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_clears_route_fields_when_agent_replaces_bound_sentinel_variant(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="legacy-native",
                workdir="/tmp",
                model="gpt-5-old",
                reasoning_effort="high",
                metadata={
                    "legacy_scope_key": "slack::C123",
                    "explicit_setting_overrides": ["model", "reasoning_effort"],
                },
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        assert "explicit_setting_overrides" not in json.loads(row["metadata_json"] or "{}")
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_backend_alias_does_not_override_specific_reserved_owner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id="agent-reviewer",
                agent_name="reviewer",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "claude": {
                            "slack_171717.123": "claude-native",
                        },
                        "codex": {
                            "slack_171717.123": "codex-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "claude"
        assert row["agent_variant"] == "claude"
        assert row["agent_id"] is None
        assert row["agent_name"] is None
        assert row["native_session_id"] == "claude-native"
    finally:
        service.close()


def test_save_state_owner_lookahead_matches_registered_agent_id(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id="agent-reviewer",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "claude": {
                            "slack_171717.123": "claude-native",
                        },
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_keeps_default_sentinel_for_default_import(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="default",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "default": {
                            "slack_171717.123": "legacy-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "default"
        assert row["agent_variant"] == "default"
        assert row["native_session_id"] == "legacy-native"
    finally:
        service.close()


def test_save_state_matches_existing_custom_variant_without_relabeling_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "opencode"
        assert row["agent_variant"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_adopts_registered_custom_agent_into_default_sentinel(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="default",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
        assert service.load_state().session_mappings["slack::C123"]["reviewer"]["slack_171717.123"] == (
            "reviewer-native"
        )
    finally:
        service.close()


def test_save_state_upgrades_unknown_legacy_backend_to_registered_custom_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="unknown",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_clears_route_fields_when_registered_agent_adopts_sentinel(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="unknown",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id="stale-agent",
                agent_name="stale-reviewer",
                model="gpt-5-old",
                reasoning_effort="high",
                metadata={
                    "legacy_scope_key": "slack::C123",
                    "explicit_setting_overrides": ["model", "reasoning_effort"],
                },
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        assert "explicit_setting_overrides" not in json.loads(row["metadata_json"] or "{}")
    finally:
        service.close()


def test_save_state_prefers_catalog_identity_over_backend_name_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-codex-opencode",
                    name="codex",
                    normalized_name="codex",
                    description=None,
                    backend="opencode",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="default",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "opencode"
        assert row["agent_variant"] == "codex"
        assert row["agent_id"] == "agent-codex-opencode"
        assert row["agent_name"] == "codex"
        assert row["native_session_id"] == "codex-native"
    finally:
        service.close()


def test_save_state_preserves_unregistered_custom_variant_on_default_sentinel(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="default",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "default"
        assert row["agent_variant"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
        assert service.load_state().session_mappings["slack::C123"]["reviewer"]["slack_171717.123"] == (
            "reviewer-native"
        )
    finally:
        service.close()


def test_save_state_adopts_unregistered_custom_variant_on_concrete_backend_sentinel(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
        mappings = service.load_state().session_mappings["slack::C123"]
        assert mappings["reviewer"]["slack_171717.123"] == "reviewer-native"
        assert "default" not in mappings
    finally:
        service.close()


def test_save_state_rejects_unresolved_variant_that_contradicts_durable_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id="agent-reviewer-deleted",
                agent_name="Reviewer",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "writer": {
                            "slack_171717.123": "writer-native",
                        },
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer-deleted"
        assert row["agent_name"] == "Reviewer"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


@pytest.mark.parametrize("legacy_backend", ["default", "unknown"])
def test_save_state_preserves_durable_agent_identity_on_legacy_backend(
    tmp_path: Path, legacy_backend: str
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend=legacy_backend,
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="reviewer-native",
                workdir="/tmp",
                agent_id="agent-reviewer-deleted",
                agent_name="reviewer",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == legacy_backend
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer-deleted"
        assert row["agent_name"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


def test_save_state_indexes_legacy_owner_candidates_once_per_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    thread_count = 12
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            for index in range(thread_count):
                create_agent_session_row(
                    conn,
                    scope_id=scope_id,
                    agent_backend="codex",
                    agent_variant="codex",
                    session_anchor=f"slack_{index}",
                    native_session_id="",
                    workdir="/tmp",
                    metadata={"legacy_scope_key": "slack::C123"},
                    require_workdir=False,
                )

        with (
            patch.object(
                sessions_service_module,
                "_resolve_imported_agent_identity",
                wraps=sessions_service_module._resolve_imported_agent_identity,
            ) as resolve_identity,
            patch.object(
                sessions_service_module,
                "_base_session_anchor",
                wraps=sessions_service_module._base_session_anchor,
            ) as normalize_anchor,
        ):
            service.save_state(
                SessionState(
                    session_mappings={
                        "slack::C123": {
                            "codex": {
                                f"slack_{index}": f"codex-native-{index}"
                                for index in range(thread_count)
                            }
                        }
                    }
                )
            )

        assert resolve_identity.call_count == 1
        assert normalize_anchor.call_count == thread_count
    finally:
        service.close()


@pytest.mark.parametrize("existing_variant", ["reviewer", "Reviewer"])
def test_save_state_sets_registered_custom_agent_identity_on_existing_owned_row(
    tmp_path: Path, existing_variant: str
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant=existing_variant,
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == existing_variant
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


_SAVE_STATE_ANCHOR_SNAPSHOT_PREFIX = (
    "SELECT agent_sessions.id, agent_sessions.agent_backend, agent_sessions.agent_variant, "
    "agent_sessions.agent_id, agent_sessions.agent_name, agent_sessions.native_session_id"
)


def _commit_route_claim_after_save_state_snapshot(
    engine,
    db_path: Path,
    *,
    session_id: str,
    values: dict,
) -> dict:
    state = {"fired": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def claim_route_after_snapshot(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if state["fired"] or not " ".join(statement.split()).startswith(
            _SAVE_STATE_ANCHOR_SNAPSHOT_PREFIX
        ):
            return
        state["fired"] += 1
        other = create_sqlite_engine(db_path)
        try:
            with other.begin() as other_conn:
                other_conn.execute(
                    agent_sessions.update().where(agent_sessions.c.id == session_id).values(**values)
                )
        finally:
            other.dispose()

    return state


def test_save_state_identity_backfill_loses_concurrent_route_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        race = _commit_route_claim_after_save_state_snapshot(
            service.engine,
            db_path,
            session_id=session_id,
            values={"agent_backend": "opencode", "agent_variant": "writer"},
        )
        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert race["fired"] == 1
        assert row is not None
        assert row["agent_backend"] == "opencode"
        assert row["agent_variant"] == "writer"
        assert row["agent_id"] is None
        assert row["agent_name"] is None
        assert row["native_session_id"] == ""
    finally:
        service.close()


@pytest.mark.parametrize("winner_backend", ["claude", "codex"])
def test_save_state_final_native_bind_loses_concurrent_claim(
    tmp_path: Path, winner_backend: str
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        race = _commit_route_claim_after_save_state_snapshot(
            service.engine,
            db_path,
            session_id=session_id,
            values={
                "agent_backend": winner_backend,
                "agent_variant": winner_backend,
                "agent_id": f"agent-{winner_backend}",
                "agent_name": f"{winner_backend}-reviewer",
                "native_session_id": f"{winner_backend}-native",
            },
        )
        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert race["fired"] == 1
        assert row is not None
        assert row["agent_backend"] == winner_backend
        assert row["agent_variant"] == winner_backend
        assert row["agent_id"] == f"agent-{winner_backend}"
        assert row["agent_name"] == f"{winner_backend}-reviewer"
        assert row["native_session_id"] == f"{winner_backend}-native"
    finally:
        service.close()


@pytest.mark.parametrize("existing_agent_id", ["agent-reviewer", None])
def test_save_state_accepts_matching_agent_identity_across_variant_aliases(
    tmp_path: Path, existing_agent_id: str | None
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                agent_id=existing_agent_id,
                agent_name="Reviewer",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "Reviewer"
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
        assert row["native_session_id"] == "reviewer-native"
    finally:
        service.close()


@pytest.mark.parametrize("existing_backend", ["codex", "default", "unknown"])
def test_save_state_preserves_existing_agent_identity(tmp_path: Path, existing_backend: str) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer-new",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend=existing_backend,
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="existing-native",
                workdir="/tmp",
                agent_id="agent-reviewer-old",
                agent_name="reviewer-old",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == existing_backend
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] == "agent-reviewer-old"
        assert row["agent_name"] == "reviewer-old"
        assert row["native_session_id"] == "existing-native"
    finally:
        service.close()


def test_save_state_accepts_default_variant_when_backend_already_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
        assert row["native_session_id"] == "codex-native"
        assert service.load_state().session_mappings["slack::C123"]["codex"]["slack_171717.123"] == "codex-native"
    finally:
        service.close()


def test_save_state_compares_retry_guard_with_raw_empty_variant(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    update_attempts = 0

    def reject_repeated_agent_session_update(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        nonlocal update_attempts
        if statement.lstrip().upper().startswith("UPDATE AGENT_SESSIONS SET"):
            update_attempts += 1
            if update_attempts > 1:
                raise AssertionError("save_state retried an unchanged legacy anchor row")

    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )
            conn.execute(
                agent_sessions.update().where(agent_sessions.c.id == session_id).values(agent_variant="")
            )

        event.listen(service.engine, "before_cursor_execute", reject_repeated_agent_session_update)
        try:
            service.save_state(
                SessionState(
                    session_mappings={
                        "slack::C123": {
                            "codex": {
                                "slack_171717.123": "codex-native",
                            }
                        }
                    }
                )
            )
        finally:
            event.remove(service.engine, "before_cursor_execute", reject_repeated_agent_session_update)

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert update_attempts == 1
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "codex"
        assert row["native_session_id"] == "codex-native"
    finally:
        service.close()


def test_save_state_skips_same_variant_when_catalog_backend_disagrees_with_owned_row(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert().values(
                    id="agent-reviewer",
                    name="reviewer",
                    normalized_name="reviewer",
                    description=None,
                    backend="codex",
                    model=None,
                    reasoning_effort=None,
                    system_prompt=None,
                    enabled=1,
                    source="user",
                    source_ref=None,
                    metadata_json="{}",
                    created_at="2026-07-28T00:00:00Z",
                    updated_at="2026-07-28T00:00:00Z",
                )
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="opencode",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="existing-native",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        }
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "opencode"
        assert row["agent_variant"] == "reviewer"
        assert row["agent_id"] is None
        assert row["agent_name"] is None
        assert row["native_session_id"] == "existing-native"
    finally:
        service.close()


def test_save_state_skips_conflicting_backend_and_imports_later_matching_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "codex": {
                            "slack_171717.123": "codex-native",
                        },
                        "claude": {
                            "slack_171717.123": "claude-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "claude"
        assert row["agent_variant"] == "claude"
        assert row["native_session_id"] == "claude-native"
        mappings = service.load_state().session_mappings["slack::C123"]
        assert mappings["claude"]["slack_171717.123"] == "claude-native"
        assert "codex" not in mappings
    finally:
        service.close()


@pytest.mark.parametrize("legacy_backend", ["default", "unknown"])
def test_save_state_preserves_custom_variant_owner_with_legacy_backend(
    tmp_path: Path, legacy_backend: str
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend=legacy_backend,
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="reviewer-native",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "writer": {
                            "slack_171717.123": "writer-native",
                        },
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == legacy_backend
        assert row["agent_variant"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
        mappings = service.load_state().session_mappings["slack::C123"]
        assert mappings["reviewer"]["slack_171717.123"] == "reviewer-native"
        assert "writer" not in mappings
    finally:
        service.close()


def test_save_state_skips_conflicting_custom_variant_with_same_backend_and_imports_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            conn.execute(
                agents.insert(),
                [
                    {
                        "id": "agent-reviewer",
                        "name": "reviewer",
                        "normalized_name": "reviewer",
                        "description": None,
                        "backend": "codex",
                        "model": None,
                        "reasoning_effort": None,
                        "system_prompt": None,
                        "enabled": 1,
                        "source": "user",
                        "source_ref": None,
                        "metadata_json": "{}",
                        "created_at": "2026-07-28T00:00:00Z",
                        "updated_at": "2026-07-28T00:00:00Z",
                    },
                    {
                        "id": "agent-writer",
                        "name": "writer",
                        "normalized_name": "writer",
                        "description": None,
                        "backend": "codex",
                        "model": None,
                        "reasoning_effort": None,
                        "system_prompt": None,
                        "enabled": 1,
                        "source": "user",
                        "source_ref": None,
                        "metadata_json": "{}",
                        "created_at": "2026-07-28T00:00:00Z",
                        "updated_at": "2026-07-28T00:00:00Z",
                    },
                ],
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="reviewer",
                session_anchor="slack_171717.123",
                native_session_id="",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                require_workdir=False,
            )

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C123": {
                        "writer": {
                            "slack_171717.123": "writer-native",
                        },
                        "reviewer": {
                            "slack_171717.123": "reviewer-native",
                        },
                    }
                }
            )
        )

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_id"] == "agent-reviewer"
        assert row["agent_name"] == "reviewer"
        assert row["agent_backend"] == "codex"
        assert row["agent_variant"] == "reviewer"
        assert row["native_session_id"] == "reviewer-native"
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
            reserved_id,
            model="gpt-5.5",
            reasoning_effort="xhigh",
            expected_route={
                "agent_id": row["agent_id"],
                "agent_name": row["agent_name"],
                "agent_backend": row["agent_backend"],
                "agent_variant": row["agent_variant"],
                "model": None,
                "reasoning_effort": None,
                "explicit_overrides": [],
            },
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


def test_materialize_agent_session_route_pins_resolved_agent_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "avibe::project::proj_abc", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="avibe_ses_identity",
                agent_backend="",
                agent_variant="default",
                agent_name=None,
                model=None,
                reasoning_effort=None,
                native_session_id="",
                workdir=str(tmp_path),
                metadata={"created_via": "workbench"},
            )

        assert service.materialize_agent_session_route(
            session_id,
            agent_id="agent-default",
            agent_name="default",
            model="gpt-5.4",
            reasoning_effort="high",
            expected_route={
                "agent_id": None,
                "agent_name": None,
                "agent_backend": None,
                "agent_variant": "default",
                "model": None,
                "reasoning_effort": None,
                "explicit_overrides": [],
            },
        )
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_id"] == "agent-default"
        assert row["agent_name"] == "default"
        assert row["model"] == "gpt-5.4"
        assert row["reasoning_effort"] == "high"
    finally:
        service.close()


def test_materialize_agent_session_route_rejects_stale_agent_route(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "avibe::project::proj_abc", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="avibe_ses1",
                agent_backend="codex",
                agent_variant="codex",
                agent_name="old-agent",
                model=None,
                reasoning_effort=None,
                native_session_id="",
                workdir=str(tmp_path),
                metadata={},
            )
        stale_route = {
            "agent_id": None,
            "agent_name": "old-agent",
            "agent_backend": "codex",
            "agent_variant": "codex",
            "model": None,
            "reasoning_effort": None,
            "explicit_overrides": [],
        }

        with service.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .values(agent_name="new-agent")
            )

        assert not service.materialize_agent_session_route(
            session_id,
            model="gpt-5.5",
            reasoning_effort="xhigh",
            expected_route=stale_route,
        )
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_name"] == "new-agent"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
    finally:
        service.close()


def test_materialize_agent_session_route_rejects_stale_same_agent_settings(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "avibe::project::proj_abc", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="avibe_ses2",
                agent_backend="codex",
                agent_variant="codex",
                agent_name="codex",
                model=None,
                reasoning_effort=None,
                native_session_id="native-1",
                workdir=str(tmp_path),
                metadata={},
            )
        stale_route = {
            "agent_id": None,
            "agent_name": "codex",
            "agent_backend": "codex",
            "agent_variant": "codex",
            "model": None,
            "reasoning_effort": None,
            "explicit_overrides": [],
        }

        from storage.workbench_sessions_service import update_session

        with service.engine.begin() as conn:
            update_session(
                conn,
                session_id,
                model="gpt-5.5",
                reasoning_effort=None,
            )

        assert not service.materialize_agent_session_route(
            session_id,
            model="gpt-5.4",
            reasoning_effort="high",
            expected_route=stale_route,
        )
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["model"] == "gpt-5.5"
        assert row["reasoning_effort"] is None
    finally:
        service.close()


def test_materialize_agent_session_route_rejects_changed_explicit_pin_snapshot(
    tmp_path: Path,
) -> None:
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY
    from storage.workbench_sessions_service import update_session

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "avibe::project::proj_abc", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="avibe_ses_pin",
                agent_backend="codex",
                agent_variant="codex",
                agent_name="codex",
                model=None,
                reasoning_effort=None,
                native_session_id="native-1",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["reasoning_effort"]},
            )
        stale_route = {
            "agent_id": None,
            "agent_name": "codex",
            "agent_backend": "codex",
            "agent_variant": "codex",
            "model": None,
            "reasoning_effort": None,
            "explicit_overrides": ["reasoning_effort"],
        }

        # The picker replaces the explicit-null effort with inherited NULL. The
        # columns are byte-for-byte unchanged; only the route marker proves that
        # the hydrated Turn snapshot is stale.
        with service.engine.begin() as conn:
            update_session(conn, session_id, reasoning_effort=None)

        assert not service.materialize_agent_session_route(
            session_id,
            model="gpt-5.4",
            reasoning_effort="high",
            expected_route=stale_route,
        )
        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["model"] is None
        assert row["reasoning_effort"] is None
    finally:
        service.close()


@pytest.mark.parametrize("placeholder_backend", ["", "default", "unknown"])
def test_first_backend_adoption_preserves_materialized_global_agent_route(
    tmp_path: Path,
    placeholder_backend: str,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "avibe::project::proj_abc", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="avibe_ses3",
                agent_backend=placeholder_backend,
                agent_variant="default",
                agent_name=None,
                model=None,
                reasoning_effort=None,
                native_session_id="",
                workdir=str(tmp_path),
                metadata={"created_via": "workbench"},
            )

        assert service.materialize_agent_session_route(
            session_id,
            model="gpt-5.4",
            reasoning_effort="high",
            expected_route={
                "agent_id": None,
                "agent_name": None,
                "agent_backend": placeholder_backend or None,
                "agent_variant": "default",
                "model": None,
                "reasoning_effort": None,
                "explicit_overrides": [],
            },
        )
        assert service.bind_agent_session_by_id(
            session_id=session_id,
            native_session_id="codex-native-1",
            vibe_agent_id="agent-codex",
            vibe_agent_name="codex",
            vibe_agent_backend="codex",
        ) == session_id

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["model"] == "gpt-5.4"
        assert row["reasoning_effort"] == "high"
    finally:
        service.close()


@pytest.mark.parametrize("placeholder_backend", ["", "default", "unknown"])
def test_placeholder_backend_adoption_clears_non_workbench_route(
    tmp_path: Path,
    placeholder_backend: str,
) -> None:
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_123.456",
                agent_backend=placeholder_backend,
                agent_variant="default",
                agent_name=None,
                model="stale-model",
                reasoning_effort="high",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        assert service.bind_agent_session_by_id(
            session_id=session_id,
            native_session_id="codex-native-im",
            vibe_agent_id="agent-codex",
            vibe_agent_name="codex",
            vibe_agent_backend="codex",
        ) == session_id

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        with service.engine.connect() as conn:
            metadata_json = conn.execute(
                select(agent_sessions.c.metadata_json).where(agent_sessions.c.id == session_id)
            ).scalar_one()
        assert SESSION_SETTINGS_OVERRIDE_KEY not in json.loads(metadata_json or "{}")
    finally:
        service.close()


@pytest.mark.parametrize("raw_metadata_json", ["[]", '"legacy"'])
def test_backend_adoption_normalizes_non_object_route_metadata(
    tmp_path: Path,
    raw_metadata_json: str,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-08-10T00:00:00Z"
            )
            assert scope_id is not None
            session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_123.456",
                agent_backend="default",
                agent_variant="default",
                model="stale-model",
                reasoning_effort="high",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={},
            )
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .values(metadata_json=raw_metadata_json)
            )

        assert service.bind_agent_session_by_id(
            session_id=session_id,
            native_session_id="codex-native-im",
            vibe_agent_id="agent-codex",
            vibe_agent_name="codex",
            vibe_agent_backend="codex",
        ) == session_id

        row = service.get_agent_session_by_id(session_id)
        assert row is not None
        assert row["agent_backend"] == "codex"
        assert row["model"] is None
        assert row["reasoning_effort"] is None
        with service.engine.connect() as conn:
            metadata_json = conn.execute(
                select(agent_sessions.c.metadata_json).where(agent_sessions.c.id == session_id)
            ).scalar_one()
        assert json.loads(metadata_json or "{}") == {}
    finally:
        service.close()


def test_materialize_agent_session_route_never_fills_an_explicitly_pinned_field(
    tmp_path: Path,
) -> None:
    """HFR-249 — a NULL the row pins EXPLICITLY is not an empty column to fill.

    Turn-start materialization exists so a session created on an inherited default
    stops drifting: the first turn writes the resolved model / effort into the NULL
    columns. But a preserved ``create_once`` rebind (HFR-244) and a fork of an
    explicit-null session (HFR-248) both carry NULL columns *on purpose* — the
    session pinned "no model", and the ``explicit_setting_overrides`` marker is the
    only thing distinguishing that from "inherited nothing yet". Materializing
    those NULLs is worse than mis-routing one turn: it PERSISTS the Agent's current
    default as the session's pinned model, which is exactly the outcome the rebind
    was preventing, and no later Agent edit can undo it.

    Both halves are asserted, because "skip pinned fields" is only correct if the
    fill still happens for every ordinary row: an unmarked NULL column must still
    be filled, otherwise the fix would have quietly disabled materialization.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            pinned_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:pinned",
                agent_backend="codex",
                agent_variant="codex",
                agent_name="codex",
                model=None,
                reasoning_effort=None,
                native_session_id="native-pinned",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )
            inherited_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:inherited",
                agent_backend="codex",
                agent_variant="codex",
                agent_name="codex",
                model=None,
                reasoning_effort=None,
                native_session_id="native-inherited",
                workdir=str(tmp_path),
                metadata={},
            )

        # The pinned row: the first turn resolves the Agent's live default and
        # offers it here. Nothing may be written.
        materialized = service.materialize_agent_session_route(
            pinned_id, model="gpt-5.5", reasoning_effort="xhigh"
        )
        row = service.get_agent_session_by_id(pinned_id)
        assert row is not None
        assert row["model"] is None, (
            f"turn start pinned model={row['model']!r} onto a session that explicitly "
            "pins none; the Agent's current default just became this session's model "
            "for every run after it"
        )
        assert row["reasoning_effort"] is None, (
            f"turn start pinned reasoning_effort={row['reasoning_effort']!r} onto a "
            "session that explicitly pins none"
        )
        assert materialized is False, (
            "materialization reported a write on a row that pins both settings "
            "explicitly; every field it was offered was already spoken for"
        )

        # A HALF-pinned row: only the marked field is skipped, so the guard is
        # per-field and not "any marker disables the whole write".
        with service.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == pinned_id)
                .values(
                    metadata_json=json.dumps({SESSION_SETTINGS_OVERRIDE_KEY: ["model"]})
                )
            )
        assert service.materialize_agent_session_route(
            pinned_id, model="gpt-5.5", reasoning_effort="xhigh"
        )
        row = service.get_agent_session_by_id(pinned_id)
        assert row is not None
        assert row["model"] is None, "the still-marked model was filled anyway"
        assert row["reasoning_effort"] == "xhigh", (
            "the UNMARKED reasoning_effort was skipped too; one pinned field must not "
            "freeze the whole route"
        )

        # The negative half: an ordinary inherited-NULL row still fills in, so the
        # skip cannot be passing by having disabled materialization outright.
        assert service.materialize_agent_session_route(
            inherited_id, model="gpt-5.5", reasoning_effort="xhigh"
        )
        row = service.get_agent_session_by_id(inherited_id)
        assert row is not None
        assert row["model"] == "gpt-5.5", (
            "an unmarked NULL column was not filled at turn start; the explicit-pin "
            "skip has disabled ordinary materialization"
        )
        assert row["reasoning_effort"] == "xhigh"
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


def test_replace_agent_session_native_supersedes_binding_without_changing_public_session(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="opencode",
            session_anchor="slack_C123",
            native_session_id="native-wrapped",
        )
        assert session_id is not None

        replaced = service.replace_agent_session_native(
            session_id=session_id,
            expected_native_session_id="native-wrapped",
            replacement_native_session_id="native-repaired",
        )

        assert replaced == session_id
        active = service.get_agent_session_by_id(session_id)
        assert active is not None
        assert active["session_anchor"] == "slack_C123"
        assert active["native_session_id"] == "native-repaired"
        assert service.find_session_for_anchor(
            scope_key="slack::channel::C123",
            session_anchor="slack_C123",
        )["id"] == session_id
        with service.engine.connect() as conn:
            snapshots = conn.execute(
                select(agent_sessions)
                .where(agent_sessions.c.id != session_id)
                .where(agent_sessions.c.session_anchor.like("slack_C123:superseded:%"))
            ).mappings().all()
        assert len(snapshots) == 1
        assert snapshots[0]["native_session_id"] == "native-wrapped"
        assert snapshots[0]["status"] == "archived"
        assert snapshots[0]["visibility"] == "background"
    finally:
        service.close()


def test_replace_agent_session_native_refuses_stale_expected_binding(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="opencode",
            session_anchor="slack_C123",
            native_session_id="native-current",
        )
        assert session_id is not None

        assert service.replace_agent_session_native(
            session_id=session_id,
            expected_native_session_id="native-stale",
            replacement_native_session_id="native-repaired",
        ) is None
        assert service.get_agent_session_by_id(session_id)["native_session_id"] == "native-current"
        with service.engine.connect() as conn:
            assert conn.execute(select(agent_sessions.c.id)).all() == [(session_id,)]
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


def test_new_session_teardown_archives_durable_history_and_releases_anchor(
    tmp_path: Path,
) -> None:
    from storage import message_deliveries
    from storage.models import agent_sessions, messages, session_turns

    service = SQLiteSessionsService(tmp_path / "vibe.sqlite")
    try:
        session_id = service.bind_agent_session(
            scope_key="slack::C_DELIVERY_PURGE",
            agent_name="codex",
            session_anchor="slack_delivery_purge",
            native_session_id="native-delivery-purge",
        )
        assert session_id is not None
        delivery_id = message_deliveries.new_delivery_id()
        turn_id = message_deliveries.new_turn_id()
        attempt_id = message_deliveries.new_attempt_id()
        with service.engine.begin() as conn:
            delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=session_id,
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=None,
                    session_id=session_id,
                    platform="slack",
                    author="user",
                    source="user",
                    text="accepted history",
                ),
                dispatch_text="accepted history",
            )
            message_deliveries.claim_start_batch(
                conn,
                turn_id=turn_id,
                session_id=session_id,
                backend="codex",
                deliveries=[delivery],
                dispatch_text="accepted history",
                attempt_id=attempt_id,
            )
            turn = message_deliveries.get_turn(conn, turn_id)
            assert turn is not None
            assert message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(turn["version"]),
                runtime_key=f"runtime:{turn_id}",
                runtime_turn_id=f"runtime-turn:{turn_id}",
                native_turn_id=f"native:{turn_id}",
            ) is not None
            assert message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=turn_id,
                evidence={"kind": "test"},
            )
            assert message_deliveries.terminalize_turn(
                conn,
                turn_id,
                outcome="completed",
                settled_by="test",
                evidence_kind="test",
            )["changed"]

        assert service.delete_agent_sessions(
            scope_key="slack::C_DELIVERY_PURGE",
            agent_name="codex",
        ) == 1
        with service.engine.connect() as conn:
            archived = conn.execute(
                select(
                    agent_sessions.c.status,
                    agent_sessions.c.session_anchor,
                ).where(agent_sessions.c.id == session_id)
            ).one()
            assert archived[0] == "archived"
            assert ":superseded:" in archived[1]
            assert conn.execute(
                select(session_turns.c.id).where(session_turns.c.session_id == session_id)
            ).all() == [(turn_id,)]
            assert conn.execute(
                select(message_deliveries.message_deliveries.c.id).where(
                    message_deliveries.message_deliveries.c.session_id == session_id
                )
            ).all() == [(delivery_id,)]
            assert conn.execute(
                select(messages.c.id, messages.c.session_id).where(
                    messages.c.id == delivery_id
                )
            ).one() == (delivery_id, session_id)

        replacement = service.bind_agent_session(
            scope_key="slack::C_DELIVERY_PURGE",
            agent_name="codex",
            session_anchor="slack_delivery_purge",
            native_session_id="native-delivery-replacement",
        )
        assert replacement is not None
        assert replacement != session_id
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
        assert second.has_processed_message("C123", "171717.123", "171717.456") is True
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


def test_sessions_store_hot_path_prunes_processed_claim_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        monkeypatch.setattr(
            sessions_service_module,
            "_utc_now_iso",
            lambda: "2026-08-03T00:00:00+00:00",
        )
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


# --- P5 (PR5): the (scope_id, session_anchor) invariant and keyed get-or-create ---
# Scenario IDs: HFR-051 (schema drift) / HFR-052 (foreign-backend adoption)
# / HFR-053 (find-then-create race).


def test_models_declare_scope_anchor_unique_index() -> None:
    """HFR-051 — ``storage/models.py`` must declare the anchor invariant.

    The UNIQUE index on ``(scope_id, session_anchor)`` lived only in the Alembic
    revision, while ``SQLiteSessionsService.__init__`` calls ``metadata.create_all``.
    Any DB born from models-only — including every test that does not run
    ``ensure_sqlite_state`` — silently lacked the invariant, which is why the
    collisions below were never caught.
    """
    from storage.models import agent_sessions as agent_sessions_table

    unique = {
        tuple(column.name for column in index.columns)
        for index in agent_sessions_table.indexes
        if index.unique
    }
    assert ("scope_id", "session_anchor") in unique


def test_ensure_agent_session_id_adopts_row_owned_by_other_backend(monkeypatch, tmp_path: Path) -> None:
    """HFR-052 — lookup key must not be narrower than the constraint key.

    ``_find_agent_session_row_id`` filters on ``agent_backend``; the unique index
    is ``(scope_id, session_anchor)`` alone. A same-anchor row owned by another
    backend is invisible to the finder but visible to the index, so the INSERT
    explodes. A thread is ONE session per (scope, anchor), so the row is adopted.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()

    service = SQLiteSessionsService(db_path)
    try:
        existing = service.ensure_agent_session_id(
            scope_key="slack::channel::C500",
            agent_name="codex",
            session_anchor="slack_C500",
        )
        assert existing is not None

        adopted = service.ensure_agent_session_id(
            scope_key="slack::channel::C500",
            agent_name="claude",
            session_anchor="slack_C500",
        )
        assert adopted == existing
        row = service.get_agent_session_by_id(existing)
        assert row["agent_backend"] == "claude"
    finally:
        service.close()


def test_bind_agent_session_survives_concurrent_insert(monkeypatch, tmp_path: Path) -> None:
    """HFR-053 — the find-then-create window must be closed by the constraint.

    SQLite deferred transactions take no write lock at the SELECT, so two callers
    can both miss and both insert. Simulated by blinding the finder while the row
    exists: the INSERT must lose gracefully and re-read, not raise.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage import sessions_service as sessions_service_module
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()

    service = SQLiteSessionsService(db_path)
    try:
        winner = service.bind_agent_session(
            scope_key="slack::channel::C501",
            agent_name="claude",
            session_anchor="slack_C501",
            native_session_id="native-winner",
        )
        assert winner is not None

        monkeypatch.setattr(
            sessions_service_module,
            "_find_agent_session_row_id",
            lambda *args, **kwargs: None,
        )
        loser = service.bind_agent_session(
            scope_key="slack::channel::C501",
            agent_name="claude",
            session_anchor="slack_C501",
            native_session_id="native-loser",
        )
        assert loser == winner
        # Write-once still holds: the racing caller does not steal the native.
        assert service.get_agent_session_by_id(winner)["native_session_id"] == "native-winner"
    finally:
        service.close()


def test_prefix_clear_does_not_delete_a_superseded_row(tmp_path):
    """HFR-059 — superseding preserves the row; a prefix clear must not undo that.

    ``/new`` reaches ``delete_agent_sessions(session_anchor_prefix=<anchor>)``,
    whose predicate is ``anchor == prefix OR anchor LIKE '<prefix>:%'``, and
    ``_delete_agent_session_rows`` HARD-deletes what it matches. Superseding
    keeps the row on purpose ("Nothing is deleted") -- so the suffix marker that
    preserves thread routing must not also drag the row into that prefix match.
    """
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    scope_key = "telegram::channel::-1001"
    anchor = "telegram_-1001_42"

    service = SQLiteSessionsService(db_path)
    try:
        bound = service.bind_agent_session(
            scope_key=scope_key,
            agent_name="codex",
            session_anchor=anchor,
            native_session_id="codex-native-1",
        )
        assert bound is not None
        # Another backend claims the anchor: the bound row is superseded, not deleted.
        service.ensure_agent_session_id(
            scope_key=scope_key,
            agent_name="claude",
            session_anchor=anchor,
        )
        service.delete_agent_sessions(scope_key=scope_key, session_anchor_prefix=anchor)
        survivors = service.get_agent_session_by_id(bound)
    finally:
        service.close()

    assert survivors is not None, (
        "the superseded row was hard-deleted by a prefix clear; supersede "
        "promises the row is kept"
    )


def test_backend_wide_clear_does_not_delete_a_superseded_row(tmp_path):
    """HFR-240 — /new reaches the backend-wide clear FIRST, so guarding only the
    prefix clear guards nothing.

    ``handle_new`` calls ``agent_service.clear_sessions()`` before
    ``clear_session_base()``. The backend adapters route the first call to
    ``clear_agent_sessions(scope_key, agent_name)`` with no
    ``session_anchor_prefix``, so an exclusion nested inside the prefix branch is
    skipped entirely and the superseded row is hard-deleted before the guarded
    clear ever runs -- losing the transcript superseding promises to keep and
    reclaiming the definitions pinned to it.
    """
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    scope_key = "telegram::channel::-1001"
    anchor = "telegram_-1001_42"

    service = SQLiteSessionsService(db_path)
    try:
        bound = service.bind_agent_session(
            scope_key=scope_key,
            agent_name="codex",
            session_anchor=anchor,
            native_session_id="codex-native-1",
        )
        assert bound is not None
        service.ensure_agent_session_id(
            scope_key=scope_key,
            agent_name="claude",
            session_anchor=anchor,
        )
        # This is /new's FIRST call: no anchor prefix, scoped to the superseded
        # row's own backend.
        service.delete_agent_sessions(scope_key=scope_key, agent_name="codex")
        survivor = service.get_agent_session_by_id(bound)
    finally:
        service.close()

    assert survivor is not None, (
        "the superseded row was hard-deleted by the backend-wide clear that "
        "/new performs before the guarded prefix clear"
    )


def test_backend_wide_clear_still_deletes_a_rows_with_an_empty_anchor(tmp_path):
    """HFR-241 — the superseded exclusion must not preserve everything else.

    Review asked for paired NULL/empty regressions. Only the empty case is
    constructible: ``agent_sessions.session_anchor`` is ``NOT NULL`` in the model
    and has been since the initial migration, so the DB rejects a NULL anchor
    outright (``IntegrityError: NOT NULL constraint failed``). The SQL hazard is
    real — ``NULL NOT LIKE '%x%'`` evaluates to NULL, not true — and the
    predicate is written NULL-safe against it, but it cannot be reached from
    this schema and a test asserting it would be testing the fixture.

    What is reachable is the empty string, which ``NOT LIKE`` handles correctly
    (returns 1), and this pins that only a real superseded marker survives.
    """
    from sqlalchemy import update as sa_update

    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    scope_key = "telegram::channel::-1001"

    service = SQLiteSessionsService(db_path)
    try:
        empty_row = service.bind_agent_session(
            scope_key=scope_key, agent_name="codex",
            session_anchor="telegram_-1001_2", native_session_id="n2",
        )
        assert empty_row
        with service.engine.begin() as conn:
            conn.execute(sa_update(agent_sessions)
                         .where(agent_sessions.c.id == empty_row)
                         .values(session_anchor=""))

        service.delete_agent_sessions(scope_key=scope_key, agent_name="codex")

        assert service.get_agent_session_by_id(empty_row) is None, (
            "an empty-anchor row survived a clear; the superseded exclusion is "
            "preserving rows that carry no marker"
        )
    finally:
        service.close()


def test_legacy_adoption_replaces_the_whole_route_of_an_unbound_row(monkeypatch, tmp_path: Path) -> None:
    """HFR-246 — adopting an unbound row must not leave the old backend's route on it.

    ``_claim_anchor_row`` relabels an UNBOUND row to the incoming backend, but it
    only replaced ``agent_id`` / ``agent_name`` / ``model`` / ``reasoning_effort``
    when the incoming route supplied a non-``None`` value -- and the legacy
    ``ensure_agent_session_id`` / ``bind_agent_session`` callers have no model /
    effort parameters at all, so they pass ``None`` unconditionally. A thread whose
    scope moved from Codex to Claude before its first turn therefore became
    Claude-owned while still carrying the Codex Agent name, a Codex model and a
    Codex reasoning effort (plus the explicit-override marker that pins them). The
    next turn resolved that Codex Agent, so the thread ran on the backend the user
    had just moved away from -- and a Claude backend handed a ``gpt-*`` model
    fails outright.
    """
    import asyncio

    from core.scheduled_tasks import ScheduledTaskStore
    from core.vibe_agents import VibeAgentStore
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY
    from tests.test_scheduled_tasks import _binding_env, _dispatching_binding_service

    db_path = _binding_env(tmp_path, monkeypatch, backends=("claude", "codex"), default="claude")
    anchor = "slack_C123:definition_abc"

    agent_store = VibeAgentStore(db_path)
    try:
        codex_agent = agent_store.create(
            name="nightly-codex", backend="codex", model="gpt-5.5-codex", reasoning_effort="high"
        )
        default_agent = agent_store.get_default_agent()
    finally:
        agent_store.close()
    assert default_agent is not None and default_agent.backend == "claude"

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(
            conn, "slack::channel::C123", now="2026-07-27T00:00:00Z"
        )
        assert scope_id is not None
        # Reserved for a Codex Agent, never bound: no native conversation exists,
        # so nothing here is a committed user choice.
        create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor=anchor,
            agent_backend="codex",
            agent_variant="codex",
            agent_id=codex_agent.id,
            agent_name=codex_agent.name,
            model="gpt-5.5-codex",
            reasoning_effort="high",
            native_session_id="",
            workdir=str(tmp_path),
            metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
        )

    # The scope route moved to Claude; the legacy ensure path adopts the row.
    service = SQLiteSessionsService(db_path)
    try:
        adopted = service.ensure_agent_session_id(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor=anchor,
        )
        assert adopted is not None
        row = service.get_agent_session_by_id(adopted)
    finally:
        service.close()

    assert row["agent_backend"] == "claude"
    assert row["agent_variant"] == "claude"
    for column in ("agent_id", "agent_name", "model", "reasoning_effort"):
        assert row[column] is None, (
            f"the adopted row kept the previous backend's {column}={row[column]!r}; "
            "a Claude-owned session is still routing Codex settings"
        )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert SESSION_SETTINGS_OVERRIDE_KEY not in stored_metadata, (
        "the adoption reset model/reasoning_effort but kept their explicit-override "
        "marker, so dispatch still pins the old backend's settings"
    )

    # The row is only half the story: what the backend is HANDED is the failure the
    # user sees. Fire a definition pinned to this session through the real
    # MessageHandler dispatch path.
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=adopted,
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    dispatch_service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = dispatch_service.controller.agent_service.dispatched
    asyncio.run(
        dispatch_service._execute_task(task, execution_id="exec-1", disable_one_shot=False)
    )

    assert len(dispatched) == 1, "the definition never reached the backend"
    backend_name, request = dispatched[0]
    assert backend_name == "claude", (
        f"the turn was dispatched to the {backend_name!r} backend; the session was "
        "adopted by Claude before it ever bound a native conversation"
    )
    assert request.vibe_agent_name != codex_agent.name, (
        "the turn still identified as the Codex Agent the adopted row kept"
    )
    assert request.vibe_agent_model != "gpt-5.5-codex", (
        f"dispatch handed the Claude backend model={request.vibe_agent_model!r} from "
        "the stale Codex route"
    )
    assert request.vibe_agent_reasoning_effort != "high" or request.vibe_agent_model is None


def test_native_bind_by_id_replaces_the_whole_route_of_an_unbound_row(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-250 — the SECOND adoption entry point must clear the old backend's settings too.

    HFR-246 fixed ``_claim_anchor_row``, which resolves a row by
    ``(scope, anchor)``. ``bind_agent_session_by_id`` is a different door into the
    same state change: the Workbench / fork / subagent callers hand it an
    explicitly targeted reserved row plus the Agent identity they resolved, so the
    anchor lookup — and the complete-route replacement it performs — is never
    involved. It used to set ``agent_backend`` / ``agent_variant`` unconditionally
    while never touching ``model`` / ``reasoning_effort`` or the
    ``explicit_setting_overrides`` marker, so a row reserved for OpenCode and then
    bound under Codex became Codex-owned while still carrying an OpenCode model, an
    OpenCode reasoning effort, and a marker pinning both as deliberate. The next
    turn resolved the Codex backend and handed it that OpenCode model.

    Asserted at BOTH ends: the stored row, and the ``AgentRequest`` the real
    ``MessageHandler`` builds for the next turn on that session — the row is only
    the cause, what the backend is HANDED is the failure the user sees.
    """
    import asyncio

    from core.scheduled_tasks import ScheduledTaskStore
    from core.vibe_agents import VibeAgentStore
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY
    from tests.test_scheduled_tasks import _binding_env, _dispatching_binding_service

    db_path = _binding_env(tmp_path, monkeypatch, backends=("opencode", "codex"), default="codex")
    anchor = "slack_C123:definition_bind_by_id"

    agent_store = VibeAgentStore(db_path)
    try:
        opencode_agent = agent_store.create(
            name="review-opencode",
            backend="opencode",
            model="anthropic/claude-sonnet-4",
            reasoning_effort="medium",
        )
        codex_agent = agent_store.create(
            name="nightly-codex", backend="codex", model="gpt-5.5-codex", reasoning_effort="high"
        )
    finally:
        agent_store.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(
            conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
        )
        assert scope_id is not None
        # Reserved for the OpenCode Agent, never bound: no native conversation
        # exists, so none of these settings is a committed user choice yet.
        reserved_id = create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor=anchor,
            agent_backend="opencode",
            agent_variant="opencode",
            agent_id=opencode_agent.id,
            agent_name=opencode_agent.name,
            model="anthropic/claude-sonnet-4",
            reasoning_effort="medium",
            native_session_id="",
            workdir=str(tmp_path),
            metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
        )

    # The caller resolved a Codex Agent for this row and binds the native id it
    # just got back from the Codex backend.
    service = SQLiteSessionsService(db_path)
    try:
        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="codex-native-250",
            vibe_agent_id=codex_agent.id,
            vibe_agent_name=codex_agent.name,
            vibe_agent_backend="codex",
        )
        assert bound == reserved_id
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert row is not None
    assert row["agent_backend"] == "codex"
    assert row["agent_variant"] == "codex"
    assert row["agent_id"] == codex_agent.id
    assert row["agent_name"] == codex_agent.name
    for column in ("model", "reasoning_effort"):
        assert row[column] is None, (
            f"the bound row kept the previous backend's {column}={row[column]!r}; "
            "a Codex-owned session is still routing OpenCode settings"
        )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert SESSION_SETTINGS_OVERRIDE_KEY not in stored_metadata, (
        "the bind replaced the backend-owned route but kept the previous backend's "
        "explicit-override marker, so dispatch still pins settings the bind cleared"
    )

    # The consuming end: fire a definition pinned to this session through the real
    # MessageHandler dispatch path and inspect the AgentRequest it builds.
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=reserved_id,
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    dispatch_service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = dispatch_service.controller.agent_service.dispatched
    asyncio.run(
        dispatch_service._execute_task(task, execution_id="exec-1", disable_one_shot=False)
    )

    assert len(dispatched) == 1, "the definition never reached the backend"
    backend_name, request = dispatched[0]
    assert backend_name == "codex", (
        f"the turn was dispatched to the {backend_name!r} backend; the bind moved "
        "this session to Codex before it ever produced a turn"
    )
    assert request.vibe_agent_name == codex_agent.name, (
        f"the turn identified as {request.vibe_agent_name!r}; the bind named the "
        "Codex Agent as this session's owner"
    )
    assert request.vibe_agent_model != "anthropic/claude-sonnet-4", (
        f"dispatch handed the Codex backend model={request.vibe_agent_model!r} from "
        "the stale OpenCode route"
    )
    assert request.vibe_agent_reasoning_effort != "medium", (
        "dispatch handed the Codex backend the OpenCode route's reasoning effort"
    )


def test_native_bind_by_id_keeps_the_backend_of_an_already_bound_row(tmp_path: Path) -> None:
    """HFR-250, write-once half — an already-bound row must not switch backend identity.

    The clearing branch above is only safe because it is restricted to rows that
    have produced nothing. Once a row holds a native conversation, WRITE-ONCE
    extends to the backend that produced it: re-labelling the row would leave that
    transcript attributed to a backend that never generated it, and would clear the
    settings the transcript actually ran on. So a later bind under a different
    backend keeps the row's own identity and the native id it already has.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            bound_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:already_bound",
                agent_backend="opencode",
                agent_variant="opencode",
                agent_id="agent-opencode",
                agent_name="review-opencode",
                model="anthropic/claude-sonnet-4",
                reasoning_effort="medium",
                native_session_id="opencode-native-1",
                workdir=str(tmp_path),
                metadata={},
            )

        assert (
            service.bind_agent_session_by_id(
                session_id=bound_id,
                native_session_id="codex-native-2",
                vibe_agent_id="agent-codex",
                vibe_agent_name="nightly-codex",
                vibe_agent_backend="codex",
            )
            == bound_id
        )
        row = service.get_agent_session_by_id(bound_id)
    finally:
        service.close()

    assert row is not None
    assert row["agent_backend"] == "opencode", (
        f"an already-bound session was re-labelled to {row['agent_backend']!r}; its "
        "existing transcript is now attributed to a backend that never produced it"
    )
    assert row["agent_variant"] == "opencode"
    assert row["agent_name"] == "review-opencode", (
        f"the bind switched the bound row's Agent identity to {row['agent_name']!r}"
    )
    assert row["agent_id"] == "agent-opencode"
    assert row["native_session_id"] == "opencode-native-1", (
        "the write-once native guard let a second bind overwrite the existing "
        "native conversation id"
    )


def test_native_bind_by_id_keeps_the_pins_of_a_same_backend_bind(tmp_path: Path) -> None:
    """HFR-250, negative half — an ordinary same-backend bind clears nothing.

    The clearing branch keys off the backend CHANGING. The overwhelmingly common
    call is the same-backend first bind (a reserved row gets the native id its own
    backend just produced), and for it the row's ``model`` / ``reasoning_effort``
    and their explicit-override marker are the user's committed settings. If the
    fix cleared those too it would silently reset every Workbench session's model
    on its first turn, which is a worse regression than the one it fixes.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:same_backend",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                model="gpt-5.5-codex",
                reasoning_effort="xhigh",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        assert (
            service.bind_agent_session_by_id(
                session_id=reserved_id,
                native_session_id="codex-native-3",
                vibe_agent_id="agent-codex",
                vibe_agent_name="nightly-codex",
                vibe_agent_backend="codex",
            )
            == reserved_id
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert row is not None
    assert row["native_session_id"] == "codex-native-3"
    assert row["agent_backend"] == "codex"
    assert row["agent_variant"] == "codex"
    assert row["model"] == "gpt-5.5-codex", (
        f"a same-backend bind reset the session's pinned model to {row['model']!r}; "
        "nothing about the route changed, so the user's settings are still theirs"
    )
    assert row["reasoning_effort"] == "xhigh", (
        f"a same-backend bind reset the session's reasoning effort to "
        f"{row['reasoning_effort']!r}"
    )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        "a same-backend bind dropped the explicit-override marker, so the next turn "
        "treats the user's pinned settings as inherited defaults it may overwrite"
    )


# --- HFR-251: bind_agent_session_by_id decides from a snapshot it does not hold ---
#
# The statements ``bind_agent_session_by_id`` emits, in order (verified against the
# engine): the status SELECT, the route-snapshot SELECT, the ``_set_native_once``
# SELECT, then the UPDATE. SQLite takes the write lock at the UPDATE and pysqlite
# opens no transaction for a bare SELECT, so every one of those reads is released
# before the write is attempted. The two constants below name the exact reads whose
# result the code then acts on; the race is committed immediately AFTER one of them.

#: The snapshot the cross-backend adoption branch is decided from.
_ROUTE_SNAPSHOT_SELECT = (
    "SELECT agent_sessions.agent_backend, agent_sessions.native_session_id, "
    "agent_sessions.metadata_json FROM agent_sessions WHERE agent_sessions.id = ?"
)

#: The read ``_set_native_once`` enforces write-once from.
_WRITE_ONCE_SELECT = (
    "SELECT agent_sessions.native_session_id FROM agent_sessions WHERE agent_sessions.id = ?"
)

#: The archive fast-path read at the very top of the function. Rendered identically
#: by the ``winner_status`` re-read inside the lost-adoption branch, so a listener
#: keyed on it must fire only ONCE (``_commit_competing_bind_after`` does).
_STATUS_FAST_PATH_SELECT = (
    "SELECT agent_sessions.status FROM agent_sessions WHERE agent_sessions.id = ?"
)


def _commit_competing_bind_after(
    engine,
    db_path: Path,
    *,
    read: str,
    values: dict | None = None,
    table=agent_sessions,
    write=None,
) -> dict:
    """Commit a competing write from a REAL second connection, mid-flight.

    ``values`` is any competing write, not only a bind: HFR-252 passes the column
    set ``archive_session`` commits, to land a terminal archive inside the window.
    ``table`` aims that update at another table — HFR-257 repoints a
    ``run_definitions`` row, which is the competing write the reclaim helper loses
    to. ``write`` is the escape hatch for a winner that needs MORE than one
    statement: HFR-258's winner both moves an anchor aside and inserts its
    replacement row, and both must land in ONE commit to be the real interleaving.
    ``values`` and ``write`` compose, in that order, inside a single transaction.

    Hooks ``after_cursor_execute`` on the engine the code under test uses — the
    ENGINE, never the function under test itself — and when the statement
    ``read`` completes, opens a genuinely separate engine/connection, writes, and
    COMMITS. Control then returns to the caller mid-transaction, so its next
    statement runs against a database another writer has already changed.

    Fires ``after`` and not ``before`` the read on purpose: firing before it would
    let the caller's own SELECT observe the winner, which is the serial
    already-bound case HFR-250 covers, not a race. The returned dict records how
    many times the race fired, so a rendered-SQL drift shows up as "never raced"
    instead of a silently trivial pass.
    """
    state = {"fired": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != read:
            return
        state["fired"] += 1
        other = create_sqlite_engine(db_path)
        try:
            with other.begin() as other_conn:
                if values is not None:
                    other_conn.execute(
                        table.update()
                        .where(table.c.id == values["id"])
                        .values(**{key: value for key, value in values.items() if key != "id"})
                    )
                if write is not None:
                    write(other_conn)
        finally:
            other.dispose()

    return state


def test_native_bind_by_id_loses_a_concurrent_cross_backend_adoption(tmp_path: Path) -> None:
    """HFR-251 — the adoption branch reserves nothing between its read and its write.

    THE DEFECT IS THE WINDOW, not the branch. ``bind_agent_session_by_id`` read a
    snapshot (``agent_backend`` / ``native_session_id`` / ``metadata_json``),
    decided from it that this row was UNBOUND and on another backend, and then
    issued an UPDATE guarded only by ``id`` + ``status != 'archived'``. Those reads
    reserve nothing: pysqlite opens no transaction for a SELECT and SQLite takes
    the write lock at the UPDATE, so a second connection can bind the row in
    between. The stale caller still took the adoption branch and applied it to a
    row that was no longer unbound — relabelling the winner's backend, clearing the
    winner's ``model`` / ``reasoning_effort`` and their explicit-override marker,
    and (with a slightly later interleaving) overwriting its write-once native id.

    A SERIAL PRE-BOUND TEST CANNOT OBSERVE THIS. If the row is already bound when
    the call starts, the function's own snapshot sees the native and routes to the
    already-bound branch — that is
    ``test_native_bind_by_id_keeps_the_backend_of_an_already_bound_row`` (HFR-250),
    and it stays green over the unguarded UPDATE. The failure only exists while the
    caller's snapshot is stale, so the competing bind has to land INSIDE the window,
    committed by a real second connection.

    THE INTERLEAVING CHOSEN: the winner binds under the SAME backend the snapshot
    saw (opencode) — an ordinary same-backend first bind of this reserved row —
    while the loser is adopting it for codex. That is the strongest case, because
    the guard's ``agent_backend`` predicate is then satisfied and only its
    ``coalesce(native_session_id,'') = ''`` predicate can reject the write: a fix
    that re-asserted just the backend half would still corrupt the row here.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            # Reserved for the OpenCode Agent with both pinnable settings marked
            # EXPLICIT, and never bound.
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:race_cross_backend",
                agent_backend="opencode",
                agent_variant="opencode",
                agent_id="agent-opencode",
                agent_name="review-opencode",
                model="anthropic/claude-sonnet-4",
                reasoning_effort="medium",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ROUTE_SNAPSHOT_SELECT,
            # The winner: the reserved row's own OpenCode Agent binds the native id
            # OpenCode just handed it. Same backend, same identity, pins untouched —
            # exactly what a same-backend first bind persists.
            values={
                "id": reserved_id,
                "native_session_id": "native-winner",
                "status": "active",
                "updated_at": "2026-07-28T00:00:01Z",
                "last_active_at": "2026-07-28T00:00:01Z",
            },
        )

        # Caller X: resolved a Codex Agent for this row and binds the native id it
        # believes it is the first to write.
        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-x",
            vibe_agent_id="agent-codex",
            vibe_agent_name="nightly-codex",
            vibe_agent_backend="codex",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing bind never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert bound == reserved_id
    assert row is not None
    assert row["native_session_id"] == "native-winner", (
        f"the losing caller overwrote the winner's write-once native id with "
        f"{row['native_session_id']!r}; the row now names a conversation the "
        "backend that produced the transcript never opened"
    )
    assert row["agent_backend"] == "opencode", (
        f"the losing caller relabelled a row that was bound during its window to "
        f"{row['agent_backend']!r}; the winner's transcript is now attributed to a "
        "backend that never produced it"
    )
    assert row["agent_variant"] == "opencode"
    assert row["agent_id"] == "agent-opencode", (
        "the losing caller applied its own Agent identity on top of the winner"
    )
    assert row["agent_name"] == "review-opencode"
    assert row["model"] == "anthropic/claude-sonnet-4", (
        f"the losing caller cleared the winner's model to {row['model']!r}; the "
        "winner bound this row on its own backend, so its pins are still valid"
    )
    assert row["reasoning_effort"] == "medium", (
        f"the losing caller cleared the winner's reasoning effort to "
        f"{row['reasoning_effort']!r}"
    )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        "the losing caller dropped the winner's explicit-override marker, so the "
        "next turn treats the winner's pinned settings as defaults it may overwrite"
    )


#: The whole row a first-bind winner owns, asserted column by column by both
#: same-backend halves below. ``native_session_id`` is only the first entry.
def _assert_first_bind_winner_row_intact(
    row: dict,
    *,
    case: str,
    agent_variant: str,
) -> None:
    """Assert EVERY column the first-bind winner owns is exactly as it committed it.

    Factored out so the two same-backend halves cannot drift into checking
    different subsets of the row. ``agent_variant`` is the only expectation that
    differs between them (the loser that names a backend also rewrites the
    variant; the one that omits it does not name the column at all).
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    assert row["native_session_id"] == "native-winner", (
        f"[{case}] the losing caller overwrote an already-committed native id with "
        f"{row['native_session_id']!r}; write-once was enforced by a SELECT that "
        "reserved nothing, so the second writer won the column"
    )
    assert row["agent_id"] == "agent-winner-codex", (
        f"[{case}] the losing caller wrote its own Agent id {row['agent_id']!r} over "
        "the winner's; the row now attributes the winner's transcript to the Agent "
        "that LOST the race, which is the same corruption as overwriting the native "
        "id — the native id was never the only thing the winner owns"
    )
    assert row["agent_name"] == "winner-codex", (
        f"[{case}] the losing caller wrote its own Agent name {row['agent_name']!r} "
        "over the winner's, so every surface that shows which Agent owns this "
        "session now names the loser"
    )
    assert row["agent_backend"] == "codex", (
        f"[{case}] the winner's backend became {row['agent_backend']!r}"
    )
    assert row["agent_variant"] == agent_variant, (
        f"[{case}] the losing caller reset the winner's Agent variant to "
        f"{row['agent_variant']!r}; the variant is part of the identity the winner "
        "resolved, not a backend-derived default this caller may recompute"
    )
    assert row["model"] == "gpt-5.5-codex", (
        f"[{case}] a same-backend bind reset the session's pinned model to "
        f"{row['model']!r}"
    )
    assert row["reasoning_effort"] == "xhigh", (
        f"[{case}] a same-backend bind reset the session's pinned reasoning effort "
        f"to {row['reasoning_effort']!r}"
    )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        f"[{case}] the losing caller dropped the winner's explicit-override marker "
        f"(now {stored_metadata.get(SESSION_SETTINGS_OVERRIDE_KEY)!r}), so the next "
        "turn treats the winner's pinned settings as defaults it may overwrite"
    )
    assert row["status"] == "active", (
        f"[{case}] the winner's status became {row['status']!r}"
    )
    assert row["updated_at"] == "2026-07-28T00:00:01Z", (
        f"[{case}] the losing caller stamped its own updated_at "
        f"({row['updated_at']!r}) over the winner's; the row now claims it was last "
        "written by the call that changed nothing"
    )
    assert row["last_active_at"] == "2026-07-28T00:00:01Z", (
        f"[{case}] the losing caller stamped its own last_active_at "
        f"({row['last_active_at']!r}) over the winner's, so activity ordering and "
        "any recency-based sweep now date this session from the losing call"
    )


#: What the winning connection commits in both same-backend halves: a DIFFERENT
#: Agent identity from the losing caller's, and timestamps far enough from
#: ``_utc_now_iso()`` (which renders microseconds and ``+00:00``) that a stale
#: caller's stamp can never be mistaken for the winner's.
def _same_backend_winner_values(reserved_id: str, *, agent_variant: str) -> dict:
    return {
        "id": reserved_id,
        "native_session_id": "native-winner",
        "agent_id": "agent-winner-codex",
        "agent_name": "winner-codex",
        "agent_variant": agent_variant,
        "status": "active",
        "updated_at": "2026-07-28T00:00:01Z",
        "last_active_at": "2026-07-28T00:00:01Z",
    }


def _reserve_same_backend_race_row(
    service, tmp_path: Path, *, anchor: str, scope_key: str = "slack::channel::C123"
) -> str:
    """A reserved Codex row with NON-NULL pins and the override marker set.

    The pins and the marker are real values on purpose: asserting them survive is
    vacuous against a fixture that left them ``None`` / empty.

    ``scope_key`` is a parameter because the legacy-scope twin
    (``bind_agent_session``, HFR-254) resolves its row FROM a scope key and so
    needs the 2-part form ``slack::C4``: a 3-part key makes
    ``resolve_scope_from_legacy_key`` upsert the scope, which takes the write lock
    before the window opens. Resolving it here pre-creates the scope so the call
    under test only SELECTs it. The by-id callers (HFR-251) never resolve a key at
    all, so the default is kept for them.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY

    with service.engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(conn, scope_key, now="2026-07-28T00:00:00Z")
        assert scope_id is not None
        return create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor=anchor,
            agent_backend="codex",
            agent_variant="codex",
            agent_id="agent-reserved-codex",
            agent_name="reserved-codex",
            model="gpt-5.5-codex",
            reasoning_effort="xhigh",
            native_session_id="",
            workdir=str(tmp_path),
            metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
        )


def test_native_bind_by_id_loses_a_concurrent_same_backend_first_bind(tmp_path: Path) -> None:
    """HFR-251, write-once half — ``_set_native_once`` is a read, not a reservation.

    Same window, the other branch. The ordinary same-backend first bind enforced
    write-once by SELECTing ``native_session_id`` and, on finding it empty, adding
    the native id to an UPDATE guarded only by ``id`` + ``status != 'archived'``.
    Between that SELECT and that UPDATE the row is unlocked, so a second connection
    can bind it — and the stale caller then overwrites a native id that was already
    committed. A rule enforced by a preceding SELECT is not write-once; the write
    has to be rejected by the statement's own predicate.

    A SERIAL PRE-BOUND TEST CANNOT OBSERVE THIS either: with the native already
    present when the call starts, ``_set_native_once`` returns False and the native
    is simply never added to the UPDATE, which is
    ``test_bind_agent_session_survives_concurrent_insert``'s shape and is green
    without any predicate. The competing bind must commit after that read.

    THE INVARIANT IS "LOSING THE RACE DESTROYS NOTHING THE WINNER OWNS", NOT "THE
    NATIVE ID SURVIVES". The earlier version of this test asserted the winner's
    native id (plus the pins and marker no branch here touches) and stopped —
    which is exactly why a second defect survived a review round. The first fix
    added a predicate to the first-bind statement, so the native id was safe, but
    the rowcount-0 path then FELL THROUGH to the function's final unguarded UPDATE
    and re-applied the losing caller's stale snapshot: the winner's ``agent_id`` /
    ``agent_name`` / ``agent_variant`` / ``status`` / ``updated_at`` /
    ``last_active_at``, overwritten, while the native id was dutifully preserved.
    A row can be corrupted without its native id moving. So this asserts the WHOLE
    row the winner owns, column by column.

    THIS HALF: the loser NAMES the same backend the winner is on (both ``codex``),
    so the guard has nothing but the native predicate to stand on. It is also one
    of the two routes through the old fall-through conditional, which dropped the
    identity columns only when ``requested_backend != winner_backend``: equal
    backends, so it did not fire. The other route — a caller supplying no backend
    at all, where the comparison cannot fire — is
    ``test_native_bind_by_id_loses_a_concurrent_first_bind_without_a_requested_backend``.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = _reserve_same_backend_race_row(
            service, tmp_path, anchor="slack_C123:race_same_backend"
        )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_WRITE_ONCE_SELECT,
            # The winner: a DIFFERENT Codex Agent than the loser resolved, with its
            # own variant and its own timestamps.
            values=_same_backend_winner_values(reserved_id, agent_variant="codex-reviewer"),
        )

        # Caller X: resolved its own Codex Agent for this row and names the backend
        # explicitly — the same one the winner is on.
        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-x",
            vibe_agent_id="agent-loser-codex",
            vibe_agent_name="loser-codex",
            vibe_agent_backend="codex",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing bind never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert bound == reserved_id
    assert row is not None
    _assert_first_bind_winner_row_intact(
        row,
        case="loser names the winner's own backend",
        # The loser named a backend, so its stale snapshot also carried
        # agent_variant = requested_backend ("codex") — the winner's variant is what
        # must still be here.
        agent_variant="codex-reviewer",
    )


def test_native_bind_by_id_loses_a_concurrent_first_bind_without_a_requested_backend(
    tmp_path: Path,
) -> None:
    """HFR-251, the sharp half — no backend named, so no comparison can save the row.

    Same window and same branch as
    ``test_native_bind_by_id_loses_a_concurrent_same_backend_first_bind``, with the
    one difference that makes it the sharpest case: this caller omits
    ``vibe_agent_backend`` entirely. That is an ordinary call — the parameter is
    optional, and a caller that only knows "this Agent id/name produced this native
    id" leaves the row's backend alone.

    WHY IT IS SHARPER. The first fix guarded the first-bind statement with
    ``coalesce(native_session_id,'') = ''``, so the native id was safe, and then let
    the rowcount-0 path FALL THROUGH to the function's final unguarded UPDATE,
    dropping the identity columns only ``if requested_backend and requested_backend
    != winner_backend``. With no backend supplied ``requested_backend`` is ``None``,
    so that comparison cannot fire AT ALL — not "compares equal", but never
    evaluated past the falsy guard — and the losing caller's stale ``agent_id`` /
    ``agent_name`` / ``status`` / ``updated_at`` / ``last_active_at`` landed on top
    of the winner, with the winner's native id preserved underneath.

    THE INVARIANT IS "LOSING THE RACE DESTROYS NOTHING THE WINNER OWNS", NOT "THE
    NATIVE ID SURVIVES". The earlier version of the sibling test asserted only the
    native id, which is precisely how this survived a round: the row was corrupted
    with its write-once column intact. So this asserts the WHOLE row the winner
    owns, column by column, and the columns that move here are the identity and
    timestamp columns — never the native id.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = _reserve_same_backend_race_row(
            service, tmp_path, anchor="slack_C123:race_no_backend"
        )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_WRITE_ONCE_SELECT,
            # The winner keeps the reserved row's variant here: this loser never
            # names the column, so a differing variant would prove nothing about
            # THIS route and would only blur which columns the defect moves.
            values=_same_backend_winner_values(reserved_id, agent_variant="codex"),
        )

        # Caller X: its own Agent identity, and NO backend at all.
        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-x",
            vibe_agent_id="agent-loser-codex",
            vibe_agent_name="loser-codex",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing bind never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert bound == reserved_id
    assert row is not None
    _assert_first_bind_winner_row_intact(
        row,
        case="loser omits vibe_agent_backend",
        agent_variant="codex",
    )


# --- HFR-252: the archive fast-path read is a fast path, and the predicate is the guard ---
#
# GREEN ON THE CURRENT HEAD, AND THAT IS THE POINT. These two are proofs, not
# red-green regressions: the third read in ``bind_agent_session_by_id`` (the status
# SELECT that returns early on ``archived``) has the same read-then-write SHAPE as
# the two windows HFR-251 closed, but unlike those it never decided a write. Every
# UPDATE in the function already re-asserts ``status != 'archived'``, so a late
# archive turns the write into a no-op instead of corrupting the row. What was
# missing was not a fix but EVIDENCE -- nothing failed if a future edit dropped one
# of those predicates and left the SELECT to "guard" the write. These tests are that
# evidence: each was confirmed to fail with the predicate removed from the statement
# it covers.
#
# The competing write is the real one: the exact column set ``archive_session``
# commits (``status`` + ``agent_status`` + the ``archived:<id>`` anchor vacation),
# committed by a genuinely separate connection inside the window.


def _archive_write(session_id: str) -> dict:
    """The columns ``archive_session`` commits, as a competing-write payload.

    Mirrors ``storage/workbench_sessions_service.py::archive_session`` step 1 --
    including the ``archived:<id>`` anchor vacation, so a caller that wrongly wrote
    over the archive would also be seen re-anchoring the row onto the live thread.
    """
    return {
        "id": session_id,
        "status": "archived",
        "agent_status": "idle",
        "session_anchor": f"archived:{session_id}",
        "updated_at": "2026-07-28T00:00:01Z",
    }


def test_native_bind_by_id_cannot_resurrect_a_session_archived_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-252 — a late bind loses to an archive that commits after the status read.

    The hazard: ``bind_agent_session_by_id`` refuses archived rows from a SELECT at
    the top of the function, and that read reserves nothing (pysqlite opens no
    transaction for a bare SELECT; SQLite takes the write lock at the UPDATE). A
    turn that was still finishing when the user archived the session — the cancel is
    best-effort/background — can therefore pass the read and then have the archive
    commit underneath it. Its ``values`` carry ``status='active'``, so an unguarded
    write would resurrect a terminal row, bind a native id into it, and leave it
    re-anchored to the live thread.

    Why it is safe, and why this test is a PROOF rather than a regression: the
    ``status != 'archived'`` predicate on the statement — not the read — is the
    guard. The row is still unbound, so this call exits through the FIRST-BIND
    statement: its ``status != 'archived'`` predicate matches no row, ``rowcount`` is
    0, the branch re-reads the status and returns ``None`` without reaching the final
    UPDATE. Green on the current head; it fails the moment that predicate is dropped
    from the first-bind statement (verified).

    Same-backend interleaving on purpose, so the write-once predicate on that
    statement is SATISFIED (the row has no native) and the status predicate is the
    only thing standing between the caller and the archived row.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:race_archive_status",
                agent_backend="opencode",
                agent_variant="opencode",
                agent_id="agent-opencode",
                agent_name="review-opencode",
                native_session_id="",
                workdir=str(tmp_path),
            )

        # The user archives the session the instant after the fast-path read says
        # "not archived".
        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_STATUS_FAST_PATH_SELECT,
            values=_archive_write(reserved_id),
        )

        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-late",
            workdir=str(tmp_path),
            vibe_agent_id="agent-opencode",
            vibe_agent_name="review-opencode",
            vibe_agent_backend="opencode",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the window, so this test proved nothing "
        "about the read — the keyed read is no longer the SQL the code emits"
    )
    assert bound is None, (
        f"the late bind reported success ({bound!r}) on a session archived during "
        "its window; the caller will now keep polling a terminal session"
    )
    assert row is not None
    assert row["status"] == "archived", (
        f"the late bind flipped a terminal session back to {row['status']!r}; the "
        "archive the user asked for did not stick"
    )
    assert not (row["native_session_id"] or ""), (
        f"the late bind wrote native id {row['native_session_id']!r} into an "
        "archived row, re-attaching a live backend conversation to a dead session"
    )
    assert row["session_anchor"] == f"archived:{reserved_id}", (
        "the late bind undid the archive's anchor vacation, so the next inbound "
        "message on that thread resolves to the archived row"
    )
    assert row["agent_status"] == "idle", (
        "the late bind reopened the archived row's running indicator"
    )


def test_native_bind_by_id_cannot_adopt_a_session_archived_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-252, adoption half — the archive also beats the cross-backend adopt.

    Same window one statement later. The adopt branch is decided from the route
    snapshot (unbound + on another backend) and applies the whole backend identity
    in ONE statement: new ``agent_backend`` / ``agent_variant``, cleared ``model`` /
    ``reasoning_effort`` and their explicit-override marker, plus the native id. If
    the archive commits after that snapshot, an unguarded adopt would relabel and
    strip the route of a terminal row on top of resurrecting it.

    Also a PROOF, not a regression: the adopt statement carries
    ``status != 'archived'`` alongside its write-once and backend predicates, so it
    matches no row; the branch then re-reads the status, sees ``archived``, and
    returns ``None`` instead of falling through to the unconditional update. Green
    on the current head; it fails with the status predicate removed from the adopt
    statement.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:race_archive_adopt",
                agent_backend="opencode",
                agent_variant="opencode",
                agent_id="agent-opencode",
                agent_name="review-opencode",
                model="anthropic/claude-sonnet-4",
                reasoning_effort="medium",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ROUTE_SNAPSHOT_SELECT,
            values=_archive_write(reserved_id),
        )

        bound = service.bind_agent_session_by_id(
            session_id=reserved_id,
            native_session_id="native-late",
            vibe_agent_id="agent-codex",
            vibe_agent_name="nightly-codex",
            vibe_agent_backend="codex",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the adoption window, so this test proved "
        "nothing — the keyed read is no longer the SQL the code emits"
    )
    assert bound is None, (
        f"the adoption reported success ({bound!r}) on a session archived during "
        "its window"
    )
    assert row is not None
    assert row["status"] == "archived", (
        f"the adoption flipped a terminal session back to {row['status']!r}"
    )
    assert not (row["native_session_id"] or ""), (
        f"the adoption wrote native id {row['native_session_id']!r} into an "
        "archived row"
    )
    assert row["agent_backend"] == "opencode", (
        f"the adoption relabelled an archived row to {row['agent_backend']!r}; the "
        "archived transcript is now attributed to a backend that never produced it"
    )
    assert row["agent_variant"] == "opencode"
    assert row["agent_id"] == "agent-opencode", (
        "the adoption applied its own Agent identity to an archived row"
    )
    assert row["agent_name"] == "review-opencode"
    assert row["model"] == "anthropic/claude-sonnet-4", (
        f"the adoption cleared the archived row's pinned model to {row['model']!r}; "
        "a fork or restore of this session would lose the settings it ran with"
    )
    assert row["reasoning_effort"] == "medium"
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        "the adoption dropped the archived row's explicit-override marker"
    )
    assert row["session_anchor"] == f"archived:{reserved_id}", (
        "the adoption undid the archive's anchor vacation"
    )


def test_native_bind_by_id_idempotent_rebind_cannot_resurrect_an_archived_session(
    tmp_path: Path,
) -> None:
    """HFR-252, final-statement half — the harmless re-bind is the one that resurrects.

    The THIRD statement needs its own case, because the two above never reach it: an
    unbound row exits through the first-bind statement, and a lost adoption returns
    early. The final UPDATE is reached when the row already stores the SAME native id
    the caller is re-binding — an idempotent re-bind, the shape a retried or
    duplicated turn-start emits — because ``_set_native_once`` then declines and the
    native is simply left out of the write.

    That is the dangerous shape: with nothing to write-once and no backend change,
    the statement is a bare ``id`` match carrying ``status='active'``. If the archive
    commits after the fast-path read, the most innocuous call in the function is the
    one that resurrects a terminal session. Its ``status != 'archived'`` predicate is
    what stops it.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
            )
            assert scope_id is not None
            bound_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C123:race_archive_rebind",
                agent_backend="opencode",
                agent_variant="opencode",
                agent_id="agent-opencode",
                agent_name="review-opencode",
                native_session_id="native-live",
                workdir=str(tmp_path),
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_STATUS_FAST_PATH_SELECT,
            values=_archive_write(bound_id),
        )

        bound = service.bind_agent_session_by_id(
            session_id=bound_id,
            native_session_id="native-live",
            vibe_agent_id="agent-opencode",
            vibe_agent_name="review-opencode",
            vibe_agent_backend="opencode",
        )
        row = service.get_agent_session_by_id(bound_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the window, so this test proved nothing — "
        "the keyed read is no longer the SQL the code emits"
    )
    assert bound is None, (
        f"the idempotent re-bind reported success ({bound!r}) on a session archived "
        "during its window"
    )
    assert row is not None
    assert row["status"] == "archived", (
        f"an idempotent re-bind flipped a terminal session back to {row['status']!r}; "
        "the archive the user asked for did not stick"
    )
    assert row["session_anchor"] == f"archived:{bound_id}", (
        "the re-bind undid the archive's anchor vacation, so the next inbound "
        "message on that thread resolves to the archived row"
    )
    assert row["agent_status"] == "idle"


# --- HFR-280: update_session is the Workbench PATCH writer with the same shape ---
#
# RED, not a proof. Unlike HFR-252's three statements, ``update_session``'s single
# UPDATE did NOT re-assert ``status != 'archived'``: the guard was a bare
# read-then-write pre-check at the top of the function, which reserves nothing (the
# constant below is that read, and pysqlite opens no transaction for it, so SQLite
# only takes the write lock at the UPDATE). Both cases here fail against the pre-fix
# function -- the first renames an archived row, the second re-routes one -- and the
# competing write is the same real ``_archive_write`` payload HFR-252 uses.

#: The fast-path read at the top of ``update_session``. Keyed on so the archive lands
#: in the window between it and the UPDATE that acts on its answer.
_UPDATE_SESSION_FAST_PATH_SELECT = (
    "SELECT agent_sessions.id, agent_sessions.scope_id, agent_sessions.agent_backend, "
    "agent_sessions.native_session_id, agent_sessions.agent_status, "
    "agent_sessions.workdir, agent_sessions.metadata_json, agent_sessions.status FROM agent_sessions "
    "WHERE agent_sessions.id = ?"
)


def _block_competing_update_after(engine, db_path: Path, *, read: str, values: dict) -> dict:
    state = {"fired": 0, "blocked": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != read:
            return
        state["fired"] += 1
        other = create_sqlite_engine(db_path)
        try:
            try:
                with other.begin() as other_conn:
                    other_conn.exec_driver_sql("PRAGMA busy_timeout = 1")
                    other_conn.execute(
                        agent_sessions.update()
                        .where(agent_sessions.c.id == values["id"])
                        .values(**{key: value for key, value in values.items() if key != "id"})
                    )
            except OperationalError as exc:
                state["blocked"] += int("database is locked" in str(exc))
        finally:
            other.dispose()

    return state


def _seed_update_session_row(service: SQLiteSessionsService, tmp_path: Path, anchor: str) -> str:
    with service.engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(
            conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
        )
        assert scope_id is not None
        return create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor=anchor,
            agent_backend="opencode",
            agent_variant="opencode",
            agent_id="agent-opencode",
            agent_name="review-opencode",
            native_session_id="",
            title="Before",
            workdir=str(tmp_path),
        )


def test_update_session_serializes_rename_before_archive(
    tmp_path: Path,
) -> None:
    """HFR-280 — the writer reservation removes the read/archive/write window."""
    from storage.workbench_sessions_service import update_session

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        session_id = _seed_update_session_row(service, tmp_path, "slack_C123:race_patch_rename")

        archive_values = _archive_write(session_id)
        race = _block_competing_update_after(
            service.engine,
            db_path,
            read=_UPDATE_SESSION_FAST_PATH_SELECT,
            values=archive_values,
        )

        with service.engine.begin() as conn:
            update_session(conn, session_id, title="Renamed before archive")
        with service.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .values(**{key: value for key, value in archive_values.items() if key != "id"})
            )
        row = service.get_agent_session_by_id(session_id)
    finally:
        service.close()

    assert race == {"fired": 1, "blocked": 1}
    assert row is not None
    assert row["title"] == "Renamed before archive"
    assert row["status"] == "archived"
    assert row["session_anchor"] == f"archived:{session_id}"
    assert row["agent_status"] == "idle"


def test_update_session_serializes_route_change_before_archive(tmp_path: Path) -> None:
    """HFR-280, route half — the same reservation serializes both predicates."""
    from storage.workbench_sessions_service import update_session

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        session_id = _seed_update_session_row(service, tmp_path, "slack_C123:race_patch_route")

        archive_values = {**_archive_write(session_id), "native_session_id": "native-late"}
        race = _block_competing_update_after(
            service.engine,
            db_path,
            read=_UPDATE_SESSION_FAST_PATH_SELECT,
            values=archive_values,
        )

        with service.engine.begin() as conn:
            update_session(conn, session_id, agent_backend="codex", agent_name="nightly-codex")
        with service.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .values(**{key: value for key, value in archive_values.items() if key != "id"})
            )
        row = service.get_agent_session_by_id(session_id)
    finally:
        service.close()

    assert race == {"fired": 1, "blocked": 1}
    assert row is not None
    assert row["status"] == "archived"
    assert row["agent_backend"] == "codex"
    assert row["agent_name"] == "nightly-codex"
    assert row["native_session_id"] == "native-late"


# --- HFR-253 / HFR-254: the other two read-then-write session writers ---
#
# HFR-251 closed the two windows in ``bind_agent_session_by_id``. The two writers
# below have the SAME shape and were left untouched by that round: ``_claim_anchor_row``
# (added by the same PR) re-asserted nothing at all, and ``bind_agent_session`` --
# the legacy-scope twin of ``bind_agent_session_by_id`` -- carried neither the
# write-once nor the archive predicate while unconditionally setting
# ``status='active'``.
#
# THE SCOPE KEY IS PART OF THE MECHANISM, not incidental. Both entry points start
# with ``resolve_scope_from_legacy_key``, and for a THREE-part key
# (``slack::channel::C1``) that helper calls ``upsert_scope``, which WRITES -- so the
# caller's transaction already holds SQLite's write lock by the time the decision
# read runs, and a competing connection cannot commit inside the window at all (it
# gets ``database is locked`` after the busy timeout). The window only exists for the
# TWO-part form ``slack::C1`` with the scope already present, where the resolve is a
# pure SELECT. That is the form ``core/message_context.py::build_context_session_key``
# produces for every ordinary channel / thread turn (the three-part form is reserved
# for typed user scopes), so it is the production shape, not a contrived one. Both
# tests below use it, and both were confirmed to raise ``OperationalError: database
# is locked`` instead of corrupting anything when re-run with a three-part key.

#: The decision read of ``_claim_anchor_row``: ``_row_for_scope_anchor``. Everything
#: that branch decides -- "unbound, so relabel it and replace its whole route" --
#: comes from this row.
_ANCHOR_DECISION_SELECT = (
    "SELECT agent_sessions.id, agent_sessions.agent_backend, agent_sessions.agent_variant, "
    "agent_sessions.native_session_id FROM agent_sessions WHERE agent_sessions.scope_id = ? "
    "AND agent_sessions.session_anchor = ? AND agent_sessions.status != ? "
    "ORDER BY agent_sessions.last_active_at DESC, agent_sessions.id DESC LIMIT ? OFFSET ?"
)


def test_ensure_agent_session_id_cannot_relabel_a_row_bound_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-253 — ``_claim_anchor_row``'s relabel UPDATE re-asserted nothing.

    THE PRODUCTION STORY. Thread ``slack_C1`` holds row R, reserved for the Codex
    Agent and not yet bound. The channel's Agent is switched to Claude, so the next
    turn calls ``ensure_agent_session_id(agent_name="claude", ...)``; the finder
    filters on backend, misses R, and ``get_or_create_agent_session_row`` resolves it
    on the CONSTRAINT key instead. ``_claim_anchor_row`` reads R, sees no native id,
    and takes the unbound branch: relabel to Claude and replace the whole route.
    Meanwhile the still-finishing Codex turn commits its native id onto R.

    THE DEFECT IS THE WINDOW. The relabel was ``UPDATE ... WHERE id = ?`` and
    nothing else -- no ``coalesce(native_session_id,'') = ''``, no
    ``agent_backend = <the backend it decided against>``, no ``status != 'archived'``.
    Its sibling ``bind_agent_session_by_id`` got all three in HFR-251. So the stale
    caller relabelled a row that was no longer unbound, and the row came out
    ``agent_backend='claude'`` while holding the Codex native id -- a Claude-owned
    session pointing at a Codex conversation, with the Codex Agent's identity, model
    and reasoning effort wiped and their explicit-override marker cleared.

    A SERIAL TEST CANNOT OBSERVE THIS. Bind R before the call and
    ``_claim_anchor_row``'s own read sees the native, taking the SUPERSEDE branch
    instead (``test_agent_session_anchor_supersede_...``), which is green over the
    unguarded UPDATE. The competing bind has to land INSIDE the window, committed by
    a real second connection.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            # Two-part scope key, created up front, so the resolve inside the call
            # under test is a pure SELECT and takes no write lock. See the note above.
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C1", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C1",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                model="gpt-5.5-codex",
                reasoning_effort="xhigh",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        # The still-finishing Codex turn commits the native id Codex just handed it,
        # the instant after the relabel decision was read.
        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ANCHOR_DECISION_SELECT,
            values={
                "id": reserved_id,
                "native_session_id": "codex-native-uuid",
                "status": "active",
                "updated_at": "2026-07-28T00:00:01Z",
                "last_active_at": "2026-07-28T00:00:01Z",
            },
        )

        # The Claude turn on the same thread, working from the stale snapshot.
        resolved = service.ensure_agent_session_id(
            scope_key="slack::C1",
            agent_name="claude",
            session_anchor="slack_C1",
            workdir=str(tmp_path),
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing bind never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert resolved == reserved_id, (
        f"the loser answered {resolved!r}; a bound LIVE winner is a usable session, so "
        "the row's id is still the right answer here — the caller's turn resolves onto "
        "the winner's row instead of getting no session at all. Only an ARCHIVED "
        "winner (the other half) has no id to give, because the archive is terminal"
    )
    assert row is not None
    assert row["native_session_id"] == "codex-native-uuid", (
        f"the relabel overwrote the winner's write-once native id with "
        f"{row['native_session_id']!r}"
    )
    assert row["agent_backend"] == "codex", (
        f"the relabel moved a row that was bound during its window to "
        f"{row['agent_backend']!r}; the session now claims a backend that never "
        f"produced its conversation, while still holding native id "
        f"{row['native_session_id']!r}"
    )
    assert row["agent_variant"] == "codex"
    assert row["agent_id"] == "agent-codex", (
        "the relabel replaced the winner's Agent identity"
    )
    assert row["agent_name"] == "nightly-codex"
    assert row["model"] == "gpt-5.5-codex", (
        f"the relabel cleared the winner's pinned model to {row['model']!r}; the "
        "winner bound this row on its own backend, so its pins are still valid"
    )
    assert row["reasoning_effort"] == "xhigh", (
        f"the relabel cleared the winner's reasoning effort to "
        f"{row['reasoning_effort']!r}"
    )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        "the relabel dropped the winner's explicit-override marker, so the next turn "
        "treats the winner's pinned settings as inherited defaults it may overwrite"
    )


def test_ensure_agent_session_id_cannot_relabel_a_row_archived_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-253, archive half — the relabel also has to lose to a terminal archive.

    Same window, the other competing write. ``_row_for_scope_anchor`` deliberately
    filters ``status != 'archived'`` because an archive VACATES the anchor, so the
    branch is only ever decided about a live row. The read reserves nothing, though,
    so the archive can commit right after it — and the relabel then rewrote the whole
    route of a terminal row, leaving the archived transcript attributed to a backend
    that never produced it.

    THE ROW IS ONLY HALF THE ANSWER. Guarding the UPDATE stops the relabel, but the
    lost-race path then still RETURNED the row id — the same id whose anchor the
    archive just vacated. An archive is terminal, so that is not a usable session,
    and the return value is what production consumes: nothing re-resolves after this
    call. ``BaseAgent.ensure_agent_session_id`` pins any non-empty answer straight
    into ``context.platform_specific['agent_session_id']``
    (``modules/agents/base.py``, the ``if not agent_session_id: return None`` gate and
    the ``payload["agent_session_id"] = agent_session_id`` write directly under it),
    so a returned archived id becomes the row the whole turn reports against. The two
    sibling bind paths (``bind_agent_session_by_id`` / ``bind_agent_session``) already
    answer ``None`` for an archived winner; this path must too. The end-to-end proof
    that the pin then cannot happen is
    ``test_ensure_agent_session_id_archive_race_never_pins_an_agent_session_id``.
    """
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY, explicit_override_names

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C2", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C2",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                model="gpt-5.5-codex",
                reasoning_effort="xhigh",
                native_session_id="",
                workdir=str(tmp_path),
                metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ANCHOR_DECISION_SELECT,
            values=_archive_write(reserved_id),
        )

        resolved = service.ensure_agent_session_id(
            scope_key="slack::C2",
            agent_name="claude",
            session_anchor="slack_C2",
            workdir=str(tmp_path),
        )
        row = service.get_agent_session_by_id(reserved_id)
        with service.engine.connect() as conn:
            stored_ids = [str(value) for value in conn.execute(select(agent_sessions.c.id)).scalars()]
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the window, so this test proved nothing — "
        "the keyed read is no longer the SQL the code emits"
    )
    assert stored_ids == [reserved_id], (
        f"the lost relabel minted a second row {stored_ids!r}; 'no usable session' "
        "is answered with None, not by creating a fresh row from the same stale "
        "snapshot the relabel was already refused for"
    )
    assert resolved is None, (
        f"the lost relabel returned {resolved!r} for a session that was ARCHIVED "
        "inside its window; an archive is terminal and vacates the anchor, so there "
        "is no usable session to hand back. The caller does not re-resolve: "
        "BaseAgent.ensure_agent_session_id pins any non-empty answer into "
        "context.platform_specific['agent_session_id'], so this id becomes the row "
        "the turn reports against — while every read path (including this "
        "function's own finder) filters the row out as archived. The sibling binds "
        "bind_agent_session / bind_agent_session_by_id already return None here"
    )
    assert resolved != reserved_id, (
        "the archived row's id must never be the answer, not even as a 'the caller "
        "will notice' fallback — nothing downstream re-decides"
    )
    assert row is not None
    assert row["status"] == "archived", (
        f"the relabel flipped a terminal session back to {row['status']!r}"
    )
    assert row["agent_backend"] == "codex", (
        f"the relabel moved an archived row to {row['agent_backend']!r}; the archived "
        "transcript is now attributed to a backend that never produced it"
    )
    assert row["agent_variant"] == "codex"
    assert row["agent_id"] == "agent-codex"
    assert row["agent_name"] == "nightly-codex"
    assert row["model"] == "gpt-5.5-codex", (
        f"the relabel cleared the archived row's pinned model to {row['model']!r}; a "
        "fork or restore of this session would lose the settings it ran with"
    )
    assert row["reasoning_effort"] == "xhigh"
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}, (
        "the relabel dropped the archived row's explicit-override marker"
    )


def test_ensure_agent_session_id_archive_race_never_pins_an_agent_session_id(
    tmp_path: Path,
) -> None:
    """HFR-253, consuming half — the archived id must never reach the turn's context.

    The storage-level assertion (``resolved is None`` in
    ``test_ensure_agent_session_id_cannot_relabel_a_row_archived_inside_its_window``)
    only matters because of what the CALLER does with a non-empty answer, so that
    step is proved here rather than argued: the real production chain
    ``BaseAgent.ensure_agent_session_id`` -> ``SessionsFacade`` ->
    ``SessionsStore`` -> ``SQLiteSessionsService`` is driven in-process, with the
    archive committed by a real second connection inside ``_claim_anchor_row``'s
    window, and the assertion is on ``context.platform_specific``.

    WHY THE PIN IS THE HAZARD. ``BaseAgent.ensure_agent_session_id`` gates on
    ``if not agent_session_id: return None`` and, one line later, writes
    ``payload["agent_session_id"] = agent_session_id`` into the context. There is no
    later re-resolve anywhere on that path — the pinned id is what every mirrored
    reply, terminal notify and Show Page write for the rest of the turn is filed
    under. So "return the winner's id even though the winner archived it" is not a
    stale answer the caller corrects; it is the caller's final answer, naming a
    terminal row that every read path already filters out.

    The context keeps a sentinel ``agent_session_id`` from the ordinary build step,
    so this distinguishes "not pinned" from "pinned to nothing".
    """
    from modules.agents.base import BaseAgent

    class _PinningAgent(BaseAgent):
        """Minimal concrete BaseAgent: real method lookup, no controller."""

        def __init__(self, sessions) -> None:
            self.sessions = sessions
            self.name = "claude"

        async def handle_message(self, request):  # pragma: no cover - abstract stub
            return None

    db_path = tmp_path / "vibe.sqlite"
    store = SessionsStore(tmp_path / "sessions.json")
    try:
        service = store._service
        with service.engine.begin() as conn:
            # Two-part key, scope pre-created: the resolve inside the call under test
            # is a pure SELECT, so the window is open. See the note above this block.
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C5", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C5",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                native_session_id="",
                workdir=str(tmp_path),
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ANCHOR_DECISION_SELECT,
            values=_archive_write(reserved_id),
        )

        agent = _PinningAgent(SessionsFacade(store))
        context = SimpleNamespace(platform_specific={"agent_session_id": "from_build"})
        request = SimpleNamespace(
            context=context,
            session_key="slack::C5",
            base_session_id="slack_C5",
            vibe_agent_id=None,
            vibe_agent_name=None,
        )

        pinned = agent.ensure_agent_session_id(request)
    finally:
        store.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the window, so this test proved nothing — "
        "the keyed read is no longer the SQL the code emits"
    )
    assert pinned is None, (
        f"BaseAgent.ensure_agent_session_id answered {pinned!r} for a session archived "
        "inside the storage window; the whole turn now runs against a terminal row"
    )
    assert context.platform_specific["agent_session_id"] == "from_build", (
        f"the archived row id was pinned into the turn's context as "
        f"{context.platform_specific['agent_session_id']!r}; every mirrored reply and "
        "terminal notify for this turn would be filed under a session that is "
        "archived, and nothing later re-resolves it"
    )
    assert reserved_id not in str(context.platform_specific), (
        f"the archived id {reserved_id} reached the turn context by another key"
    )


def test_bind_agent_session_keeps_the_native_of_a_row_bound_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-254 — ``bind_agent_session``'s final UPDATE broke write-once.

    The legacy-scope twin of ``bind_agent_session_by_id``, and it was missed by
    HFR-251/252. ``_set_native_once`` is a preceding SELECT, and the UPDATE that acted
    on its answer carried neither ``coalesce(native_session_id,'') = ''`` nor
    ``status != 'archived'`` while unconditionally setting ``status='active'``. So a
    single competing commit inside that window did TWO things at once: it lost its
    write-once native id to the stale caller, and it had its terminal archive undone.

    The competing write is the real pair — the native id a concurrent binder commits
    plus the archive column set (see ``_archive_write``) — because that is what makes
    both halves observable in one interleaving: the caller reports SUCCESS and leaves
    ``status='active'`` with ``native_session_id='loser-native'``.

    A SERIAL TEST CANNOT OBSERVE EITHER HALF. Pre-bind the row and
    ``_set_native_once`` returns False, so the native is never added to the write
    (``test_bind_agent_session_survives_concurrent_insert``'s shape); pre-archive it
    and ``_find_agent_session_row_id`` skips the row entirely. Both windows need a
    real second connection committing after the read.

    THE IDENTITY HALF OF THE SAME WINDOW is
    ``test_bind_agent_session_loses_a_concurrent_same_backend_first_bind``. This
    test's winner is ARCHIVED, so the loser's fall-through write is refused
    wholesale by the final UPDATE's ``status != 'archived'`` predicate — which hides
    everything that fall-through does to a LIVE winner. Keep the two together.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            # Two-part key with the scope already present: the resolve is a pure
            # SELECT, so the window is open. See the note above this block.
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C3", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            reserved_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C3",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                native_session_id="",
                workdir=str(tmp_path),
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_WRITE_ONCE_SELECT,
            # One commit, both halves: the winner binds its native id AND the session
            # is archived (the user's ``/archive``, whose cancel is best-effort, so a
            # still-finishing turn can still be mid-bind here).
            values={**_archive_write(reserved_id), "native_session_id": "winner-native"},
        )

        bound = service.bind_agent_session(
            scope_key="slack::C3",
            agent_name="codex",
            session_anchor="slack_C3",
            native_session_id="loser-native",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing write never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert row is not None
    assert row["native_session_id"] == "winner-native", (
        f"the losing caller overwrote an already-committed native id with "
        f"{row['native_session_id']!r}; write-once was enforced by a SELECT that "
        "reserved nothing, so the second writer won the column"
    )
    assert row["status"] == "archived", (
        f"the late bind flipped a terminal session back to {row['status']!r}; the "
        "archive the user asked for did not stick"
    )
    assert bound is None, (
        f"the late bind reported success ({bound!r}) after losing both the native id "
        "and the row itself; the caller will keep polling a terminal session"
    )
    assert row["agent_status"] == "idle", (
        "the late bind reopened the archived row's running indicator"
    )


def test_bind_agent_session_loses_a_concurrent_same_backend_first_bind(
    tmp_path: Path,
) -> None:
    """HFR-254, identity half — the legacy-scope bind fell through onto a LIVE winner.

    THE INVARIANT IS "LOSING THE RACE DESTROYS NOTHING THE WINNER OWNS", NOT "THE
    NATIVE ID SURVIVES". The earlier version of HFR-254's coverage
    (``test_bind_agent_session_keeps_the_native_of_a_row_bound_inside_its_window``)
    asserted the winner's native id, its archived status and the ``None`` return,
    and stopped there — which is why a second defect in this same function survived
    a review round. THIS IS THE SAME CORRECTION THE TWIN ``bind_agent_session_by_id``
    NEEDED (see the note in
    ``test_native_bind_by_id_loses_a_concurrent_same_backend_first_bind``); the hole
    was reintroduced here by HFR-254's own fix and had to be closed twice, once per
    sibling.

    WHAT THE ARCHIVED CASE HIDES. HFR-254 added ``coalesce(native_session_id,'')
    = ''`` to the first-bind statement, so the native id became safe — and then let
    the rowcount-0 path FALL THROUGH to the function's final UPDATE. That UPDATE
    shares the same ``values`` dict, which carries ``status='active'``, both
    timestamps, and this caller's ``agent_id`` / ``agent_name``. With an archived
    winner the whole write is rejected by ``status != 'archived'``, so nothing moves
    and the test stays green. With a LIVE winner — the ordinary case, two callers
    binding the same reserved row — the predicate is satisfied and the loser stamps
    its own Agent identity and its own timestamps over the winner's, with the
    winner's native id dutifully preserved underneath. A row can be corrupted
    without its native id moving.

    THE WINDOW IS THE SAME ONE, and the scope key is part of the mechanism: the
    2-part form ``slack::C4`` makes ``resolve_scope_from_legacy_key`` a pure SELECT,
    while a 3-part key would upsert the scope and take the write lock before the
    window opens. The loser also omits ``workdir`` — ``bind_agent_session`` takes no
    ``vibe_agent_backend`` at all, unlike its twin, so its whole stale snapshot is
    the Agent id / name pair asserted here.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = _reserve_same_backend_race_row(
            service,
            tmp_path,
            anchor="slack_C4",
            scope_key="slack::C4",
        )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_WRITE_ONCE_SELECT,
            # The winner: a DIFFERENT Codex Agent than the loser resolved, with
            # timestamps ``_utc_now_iso()`` cannot render. It stays ACTIVE — no
            # archive — so the final UPDATE's status predicate cannot stand in for
            # the guard that is actually missing.
            values=_same_backend_winner_values(reserved_id, agent_variant="codex"),
        )

        # Caller X: resolved its own Codex Agent for this row and believes it is the
        # first to bind. Same (scope_key, agent_name, session_anchor) triple the
        # winner's row is found by.
        bound = service.bind_agent_session(
            scope_key="slack::C4",
            agent_name="codex",
            session_anchor="slack_C4",
            native_session_id="native-x",
            vibe_agent_id="agent-loser-codex",
            vibe_agent_name="loser-codex",
        )
        row = service.get_agent_session_by_id(reserved_id)
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing bind never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert bound == reserved_id, (
        f"the row is live and bound, so the loser's answer is still the session id, "
        f"not {bound!r}"
    )
    assert row is not None
    _assert_first_bind_winner_row_intact(
        row,
        case="legacy-scope bind loses to a live same-backend winner",
        # ``bind_agent_session`` never names ``agent_variant``, so the winner keeps
        # the reserved variant; a differing one would only blur which columns the
        # defect moves.
        agent_variant="codex",
    )


# --- HFR-257..260: the same shape in the TEARDOWN writers ---
#
# HFR-251..254 closed the four read-then-write windows in the BIND writers. The four
# below are the rest of the set, found by enumerating every statement in
# ``storage/session_reclaim.py`` / ``storage/agent_session_rows.py`` and on their call
# path that acts on a decision a preceding SELECT made:
#
# * HFR-257 ``reclaim_bound_definitions`` -- matched the definition by id alone.
# * HFR-258 ``_claim_anchor_row``'s SUPERSEDE branch -- matched the session by id
#   alone; HFR-253 guarded only its relabel sibling.
# * HFR-259 ``_find_agent_session_row_id``'s placeholder relabel -- matched by id
#   alone AND returned the id it had just relabelled.
# * HFR-260 ``_delete_agent_session_rows`` -- applied the whole teardown query's
#   predicates in the id READ and then hard-deleted by id list.
#
# The window is the same one throughout, and the 2-part scope key is again part of
# the mechanism (see the HFR-253/254 note above): the teardown paths reach these
# statements after nothing but SELECTs, so SQLite's write lock is still free.


def _bind_definition(
    conn,  # noqa: ANN001
    *,
    definition_id: str,
    session_id: str | None,
    metadata: dict | None = None,
    enabled: int = 1,
) -> None:
    """A scheduled task pinned to ``session_id``, in the shape reclaim reads."""
    conn.execute(
        run_definitions.insert().values(
            id=definition_id,
            definition_type="scheduled",
            name="nightly",
            agent_name=None,
            session_policy="existing",
            session_id=session_id,
            mode="create_once",
            message="run the nightly check",
            prompt="run the nightly check",
            schedule_type="cron",
            cron="0 3 * * *",
            enabled=enabled,
            created_at="2026-07-28T00:00:00Z",
            updated_at="2026-07-28T00:00:00Z",
            metadata_json=json.dumps(metadata or {"origin": "cli"}),
        )
    )


#: The decision read of ``reclaim_bound_definitions``: every live definition that
#: either belongs to the session or targets it for execution. Everything the loop then
#: writes -- pause / soft-delete, the settings snapshot, the summary counters and the
#: teardown ledger -- is decided from this one row set.
_RECLAIM_DECISION_SELECT = (
    "SELECT run_definitions.id, run_definitions.definition_type, run_definitions.enabled, "
    "run_definitions.session_id, run_definitions.metadata_json FROM run_definitions WHERE ("
    "run_definitions.definition_type = ? AND run_definitions.session_id = ? OR "
    "run_definitions.definition_type = ? AND (CASE WHEN (json_valid(run_definitions.metadata_json) = ?) "
    "THEN CASE WHEN (json_type(run_definitions.metadata_json, ?) = ?) THEN "
    "nullif(trim(json_extract(run_definitions.metadata_json, ?), ?), ?) END END = ? OR "
    "run_definitions.session_id = ?)) AND run_definitions.deleted_at IS NULL"
)


def test_reclaim_leaves_a_definition_repointed_inside_its_window_alone(tmp_path: Path) -> None:
    """HFR-257 — the reclaim UPDATE matched the definition by id and nothing else.

    THE PRODUCTION STORY. A ``create_once`` task is pinned to session S. The user
    repoints it to session S2 (``vibe task update --session``, an
    ``upsert_scheduled_task`` full-row write) at the moment another surface tears S
    down -- ``/new`` in the same thread, or the archive dialog. The teardown reads the
    definitions bound to S, finds this one, and decides to pause it.

    THE DEFECT IS THE WINDOW. That read reserves nothing: pysqlite emits no ``BEGIN``
    for a bare SELECT, and the hard-delete path reaches the reclaim helper after only
    reads, so SQLite's write lock is still free. The UPDATE was
    ``WHERE run_definitions.id = ?`` alone, so it landed on the definition's NEW,
    LIVE binding: the task was disabled with a pause reason naming a session it no
    longer belongs to, and its ``session_settings_snapshot`` was overwritten with the
    dead session's model / agent -- which is what a later ``create_once`` rebind reads,
    so the repointed task would come back on the wrong route.

    AND THE ACCOUNTING IS PART OF THE INVARIANT. Refusing the write is not enough
    while the counters and the ledger still credit it: ``summary`` is what the archive
    confirm dialog reports, and the ledger is what ``/new`` counts in its reply. A
    lost write reported as "1 task paused" is the same lie as the write itself, just
    told to the user instead of the database. All three are asserted below.

    A SERIAL TEST CANNOT OBSERVE THIS. Repoint the definition BEFORE the call and the
    reclaim's own SELECT (``session_id = S``) never returns it. The repoint has to land
    INSIDE the window, committed by a real second connection.
    """
    from storage.session_reclaim import (
        RECLAIM_PAUSE,
        SESSION_SETTINGS_SNAPSHOT_KEY,
        reclaim_bound_definitions,
        session_teardown_context,
    )

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C7", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            dying_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C7",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                model="gpt-5.5-codex",
                native_session_id="codex-native",
                workdir=str(tmp_path),
            )
            keeper_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C7:keeper",
                agent_backend="claude",
                agent_variant="claude",
                agent_id="agent-claude",
                agent_name="review-claude",
                model="claude-opus-4",
                native_session_id="claude-native",
                workdir=str(tmp_path),
            )
            _bind_definition(conn, definition_id="def-repointed", session_id=dying_id)

        # The user's repoint, committed the instant after the teardown read the
        # bindings: same statement shape ``upsert_scheduled_task`` emits (a full-row
        # UPDATE keyed on the definition id) and it moves the definition to a LIVE
        # session.
        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_RECLAIM_DECISION_SELECT,
            table=run_definitions,
            values={
                "id": "def-repointed",
                "session_id": keeper_id,
                "updated_at": "2026-07-28T00:00:01Z",
            },
        )

        # The teardown, working from the stale snapshot. ``engine.begin()`` with no
        # write before the call is exactly how ``_delete_agent_session_rows`` reaches
        # this helper: a resolved 2-part scope key and an id SELECT, both reads.
        with session_teardown_context(reason="the bound agent session was cleared") as ledger:
            with service.engine.begin() as conn:
                summary = reclaim_bound_definitions(conn, dying_id, mode=RECLAIM_PAUSE)
            ledger_entries = list(ledger)

        with service.engine.begin() as conn:
            definition = (
                conn.execute(
                    select(run_definitions).where(run_definitions.c.id == "def-repointed")
                )
                .mappings()
                .first()
            )
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing repoint never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert definition is not None
    assert definition["session_id"] == keeper_id, (
        "the repoint itself did not survive, so the rest of this test is meaningless"
    )
    assert definition["enabled"] == 1, (
        "the teardown paused a definition that had been repointed to a LIVE session "
        "inside its window; the user's task is disabled and the session it now names "
        "is perfectly usable"
    )
    assert definition["deleted_at"] is None
    assert definition["last_error"] is None, (
        f"the teardown stamped {definition['last_error']!r} on a definition bound to "
        "another session, so the task now explains its state by a teardown that never "
        "touched it"
    )
    stored_metadata = json.loads(definition["metadata_json"] or "{}")
    assert SESSION_SETTINGS_SNAPSHOT_KEY not in stored_metadata, (
        "the teardown overwrote the repointed definition's metadata with the DYING "
        "session's settings snapshot; a later create_once rebind reads that snapshot, "
        "so the task would silently come back on the dead session's model and agent"
    )
    assert stored_metadata == {"origin": "cli"}, (
        f"the definition's metadata was rewritten to {stored_metadata!r}"
    )

    # What the function RETURNS is half the invariant: a lost write must be reported
    # to nobody.
    assert summary == {"paused": 0, "deleted": 0, "snapshotted": 0}, (
        f"the returned summary {summary!r} credits a reclaim that did not happen; this "
        "is the count the archive confirm dialog shows the user"
    )
    assert ledger_entries == [], (
        f"the teardown ledger records {ledger_entries!r}; the ledger is what /new "
        "counts in its reply, so the user is told a task was paused while it is still "
        "enabled and pointing at a live session"
    )


def test_new_teardown_path_reports_only_the_definitions_it_reclaimed(tmp_path: Path) -> None:
    """HFR-257, ``/new`` half — the same window, reached the way production reaches it.

    The primary test calls the shared helper directly to assert its returned summary.
    This one drives the real IM ``/new`` teardown: ``delete_agent_sessions`` ->
    ``_delete_agent_session_rows`` -> ``reclaim_bound_definitions``, inside the
    ``session_teardown_context`` the command handler opens, so the ledger the reply
    counts is the production one. Both definitions are bound to the dying session; one
    is repointed inside the window and one is not, so the ledger has to name exactly
    the second.
    """
    from storage.session_reclaim import session_teardown_context

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C5", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            dying_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C5",
                agent_backend="codex",
                agent_variant="codex",
                native_session_id="codex-native",
                workdir=str(tmp_path),
            )
            keeper_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C5:keeper",
                agent_backend="claude",
                agent_variant="claude",
                native_session_id="claude-native",
                workdir=str(tmp_path),
            )
            _bind_definition(conn, definition_id="def-repointed", session_id=dying_id)
            _bind_definition(conn, definition_id="def-staying", session_id=dying_id)

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_RECLAIM_DECISION_SELECT,
            table=run_definitions,
            values={
                "id": "def-repointed",
                "session_id": keeper_id,
                "updated_at": "2026-07-28T00:00:01Z",
            },
        )

        with session_teardown_context(reason="the bound agent session was cleared") as ledger:
            removed = service.delete_agent_sessions(scope_key="slack::C5", agent_name="codex")
            ledger_entries = list(ledger)

        with service.engine.begin() as conn:
            states = {
                str(row["id"]): row
                for row in conn.execute(
                    select(run_definitions.c.id, run_definitions.c.enabled, run_definitions.c.session_id)
                ).mappings()
            }
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing repoint never landed inside the window, so this test proved "
        "nothing about the race"
    )
    assert removed == 1, f"the dying session should still be hard-deleted, got {removed!r}"
    assert states["def-repointed"]["enabled"] == 1, (
        "/new paused a task that had just been repointed to another live session"
    )
    assert states["def-staying"]["enabled"] == 0, (
        "the definition still bound to the dying session was NOT paused; the guard "
        "must refuse only the write that lost, never the ordinary reclaim"
    )
    assert [entry["definition_id"] for entry in ledger_entries] == ["def-staying"], (
        f"the /new reply counts {ledger_entries!r}; it must name exactly the tasks it "
        "actually paused"
    )


def test_anchor_supersede_returns_the_winner_of_a_concurrent_supersede(tmp_path: Path) -> None:
    """HFR-258 — the supersede branch matched the session by id and then INSERTed.

    THE PRODUCTION STORY. Thread ``slack_C8`` holds row R: bound, with a Codex native
    id. The channel's Agent is switched to Claude and TWO turns arrive on that thread
    at once. Both call ``ensure_agent_session_id(agent_name="claude", ...)``, both miss
    R in the backend-filtered finder, and both resolve it on the constraint key, where
    ``_claim_anchor_row`` sees a bound row on another backend and takes the SUPERSEDE
    branch: move R's anchor aside, then create a fresh row on the freed slot.

    THE DEFECT IS THE WINDOW. ``_row_for_scope_anchor`` reserves nothing, so the winner
    can move the anchor and insert its replacement before the loser's UPDATE. That
    UPDATE was ``WHERE id = ?`` alone: it "succeeded" (rowcount 1) re-superseding an
    already-superseded row, reported that as freeing the slot, and fell straight into
    ``create_agent_session_row`` -- whose INSERT collided with the winner's replacement
    on the UNIQUE ``(scope_id, session_anchor)`` index. So the loser got an
    ``IntegrityError`` out of ``get_or_create_agent_session_row``, a function whose
    entire contract is "resolve the one session row for this anchor, creating it once",
    and the turn died where it should have joined the winner's session.

    A SERIAL TEST CANNOT OBSERVE THIS. Supersede R first and the second caller's read
    finds the winner's replacement row on the anchor, takes the same-backend fast path
    and returns its id -- green over the unguarded UPDATE. The competing supersede has
    to land INSIDE the window, and as ONE commit (anchor move + replacement insert),
    which is what the winner's transaction really is.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    winner_replacement_id = "seswinner001"
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C8", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            superseded_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C8",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                native_session_id="codex-native-uuid",
                workdir=str(tmp_path),
            )

        def _winner_supersedes(other_conn) -> None:  # noqa: ANN001
            """Exactly what ``_claim_anchor_row``'s supersede branch commits."""
            other_conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == superseded_id)
                .values(
                    session_anchor=f"slack_C8:superseded:{superseded_id}",
                    updated_at="2026-07-28T00:00:01Z",
                )
            )
            create_agent_session_row(
                other_conn,
                scope_id=scope_id,
                session_id=winner_replacement_id,
                session_anchor="slack_C8",
                agent_backend="claude",
                agent_variant="claude",
                agent_id="agent-claude",
                agent_name="review-claude",
                native_session_id="",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:01Z",
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ANCHOR_DECISION_SELECT,
            write=_winner_supersedes,
        )

        # The losing turn, working from the stale snapshot. No IntegrityError may
        # escape: an exception here fails the test on its own.
        resolved = service.ensure_agent_session_id(
            scope_key="slack::C8",
            agent_name="claude",
            session_anchor="slack_C8",
            workdir=str(tmp_path),
        )

        with service.engine.begin() as conn:
            anchor_rows = [
                dict(row)
                for row in conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.agent_backend)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .where(agent_sessions.c.session_anchor == "slack_C8")
                ).mappings()
            ]
            loser_row = service.get_agent_session_by_id(superseded_id)
            total_rows = conn.execute(
                select(agent_sessions.c.id).where(agent_sessions.c.scope_id == scope_id)
            ).scalars().all()
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing supersede never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert len(anchor_rows) == 1, (
        f"the anchor slack_C8 is held by {anchor_rows!r}; exactly ONE replacement row "
        "may exist for it — the UNIQUE (scope_id, session_anchor) index is the only "
        "reason a second one is an IntegrityError instead of a silent duplicate"
    )
    assert anchor_rows[0]["id"] == winner_replacement_id, (
        f"the anchor is held by {anchor_rows[0]['id']!r} rather than the winner's "
        "replacement row"
    )
    assert resolved == winner_replacement_id, (
        f"the loser answered {resolved!r}; a lost supersede must hand back the session "
        "that HOLDS the anchor now, so the turn joins the winner's session instead of "
        "inserting a second row on a slot it did not free"
    )
    assert loser_row is not None
    assert loser_row["session_anchor"] == f"slack_C8:superseded:{superseded_id}", (
        f"the loser re-superseded an already-superseded row to "
        f"{loser_row['session_anchor']!r}; the marker is appended once, and a second "
        "pass breaks the thread id every definition pinned to this row derives from it"
    )
    assert loser_row["native_session_id"] == "codex-native-uuid"
    assert loser_row["agent_backend"] == "codex"
    assert len(total_rows) == 2, (
        f"the scope holds {len(total_rows)} session rows; the loser must not create a "
        "third — the original, plus the winner's one replacement"
    )


#: The decision read of ``_find_agent_session_row_id``'s legacy branch: the newest
#: live row at this (scope, anchor) whose backend / variant are still the blank or
#: ``"default"`` placeholder. "It is a placeholder, so relabelling it in place is
#: free" is decided entirely from this row.
_PLACEHOLDER_DECISION_SELECT = (
    "SELECT agent_sessions.id FROM agent_sessions WHERE agent_sessions.scope_id = ? "
    "AND agent_sessions.session_anchor = ? AND agent_sessions.status != ? "
    "AND agent_sessions.agent_backend IN (?, ?) AND agent_sessions.agent_variant IN (?, ?) "
    "ORDER BY agent_sessions.last_active_at DESC, agent_sessions.id DESC LIMIT ? OFFSET ?"
)


def test_ensure_agent_session_id_never_returns_a_placeholder_archived_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-259 — the placeholder relabel matched by id and returned what it relabelled.

    THE FOURTH writer with the HFR-251 shape, and the one the earlier rounds walked
    past twice: ``_find_agent_session_row_id`` runs BEFORE
    ``get_or_create_agent_session_row`` on both bind entry points, and its legacy
    branch is itself a read-then-write. It selects a row whose ``agent_backend`` /
    ``agent_variant`` are still the blank / ``"default"`` placeholder (and, via
    ``base_query``, not archived), then relabels it to the concrete backend with
    ``UPDATE ... WHERE id = ?`` and RETURNS that id.

    THE RETURN VALUE IS THE DAMAGE HERE, which is HFR-253's lesson applied to a
    different function. ``ensure_agent_session_id`` passes this id straight back to its
    caller, and ``BaseAgent.ensure_agent_session_id`` pins any non-empty answer into
    ``context.platform_specific['agent_session_id']`` without ever re-resolving. So an
    archive committed inside the window -- terminal, and filtered out by every read on
    this path, ``base_query`` included -- was relabelled and then handed to the turn as
    its session.

    THE ANSWER IS "NO ROW", not the archived id and not a second decision from the
    refused snapshot: ``None`` is what this finder returns when it sees nothing usable,
    and the caller then resolves the anchor through ``get_or_create_agent_session_row``,
    whose reads exclude archived rows and whose writes are individually guarded. The
    outcome converges with the serial order (archive first, then the turn): the thread
    gets a FRESH session, because the archive vacated the anchor.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            # Two-part scope key, created up front: the resolve inside the call under
            # test is then a pure SELECT and takes no write lock.
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C9", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            placeholder_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C9",
                # The legacy placeholder shape: a row that reserved the thread's
                # identity before backends were recorded on it.
                agent_backend="",
                agent_variant="default",
                native_session_id="",
                workdir=str(tmp_path),
                require_workdir=False,
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_PLACEHOLDER_DECISION_SELECT,
            values=_archive_write(placeholder_id),
        )

        resolved = service.ensure_agent_session_id(
            scope_key="slack::C9",
            agent_name="claude",
            session_anchor="slack_C9",
            workdir=str(tmp_path),
        )
        placeholder_row = service.get_agent_session_by_id(placeholder_id)
        resolved_row = service.get_agent_session_by_id(resolved) if resolved else None
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the archive never landed inside the window, so this test proved nothing about "
        "the race — the keyed read is no longer the SQL the code emits"
    )
    assert resolved != placeholder_id, (
        f"the finder relabelled and returned {resolved!r}, a session archived during "
        "its window; an archive is terminal and vacates the anchor, and nothing "
        "re-resolves after this call — BaseAgent pins the answer into the turn context "
        "as-is, so the whole turn would report against an archived row"
    )
    assert placeholder_row is not None
    assert placeholder_row["status"] == "archived", (
        f"the relabel undid the archive; status is {placeholder_row['status']!r}"
    )
    assert placeholder_row["agent_backend"] == "", (
        f"the relabel rewrote the route of a terminal row to "
        f"{placeholder_row['agent_backend']!r}, leaving the archived session labelled "
        "with a backend it never ran"
    )
    assert placeholder_row["agent_variant"] == "default"
    assert placeholder_row["session_anchor"] == f"archived:{placeholder_id}", (
        "the relabel undid the archive's anchor vacation"
    )
    # The thread keeps working: a fresh session on the vacated anchor, which is
    # exactly what the serial order (archive, then turn) produces.
    assert resolved_row is not None, "the turn was left with no session at all"
    assert resolved_row["status"] == "active"
    assert resolved_row["agent_backend"] == "claude"
    assert resolved_row["session_anchor"] == "slack_C9"


def test_placeholder_relabel_cannot_overwrite_a_backend_claimed_inside_its_window(
    tmp_path: Path,
) -> None:
    """HFR-259, identity half — the other competing write for the same window.

    Same statement, a live winner instead of an archive: another turn fills the
    placeholder's blank backend with a CONCRETE one and binds its native id. The
    relabel's whole justification is that a blank / ``"default"`` label names no
    previous backend, so nothing on the row can belong to a different one -- once the
    winner has filled it, that justification is gone, and a bare ``id`` match relabelled
    a Codex-owned, Codex-bound row to ``claude`` while its native id stood.

    Losing sends the caller back through ``get_or_create_agent_session_row``, which
    reads the row fresh, sees a bound row on another backend, and supersedes it -- the
    branch that exists for exactly this state. So the winner keeps its route and its
    transcript, and the caller still gets a usable Claude session.
    """
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::CA", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            placeholder_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_CA",
                agent_backend="",
                agent_variant="default",
                native_session_id="",
                workdir=str(tmp_path),
                require_workdir=False,
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_PLACEHOLDER_DECISION_SELECT,
            values={
                "id": placeholder_id,
                "agent_backend": "codex",
                "agent_variant": "codex",
                "agent_id": "agent-codex",
                "agent_name": "nightly-codex",
                "native_session_id": "codex-native-uuid",
                "status": "active",
                "updated_at": "2026-07-28T00:00:01Z",
                "last_active_at": "2026-07-28T00:00:01Z",
            },
        )

        resolved = service.ensure_agent_session_id(
            scope_key="slack::CA",
            agent_name="claude",
            session_anchor="slack_CA",
            workdir=str(tmp_path),
        )
        winner_row = service.get_agent_session_by_id(placeholder_id)
        resolved_row = service.get_agent_session_by_id(resolved) if resolved else None
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing claim never landed inside the window, so this test proved "
        "nothing about the race"
    )
    assert resolved != placeholder_id, (
        f"the finder relabelled and handed back {resolved!r}, a row another backend "
        "had just claimed and bound"
    )
    assert winner_row is not None
    assert winner_row["agent_backend"] == "codex", (
        f"the placeholder relabel moved a row claimed during its window to "
        f"{winner_row['agent_backend']!r}; it now claims a backend that never produced "
        f"its conversation while holding native id {winner_row['native_session_id']!r}"
    )
    assert winner_row["agent_variant"] == "codex"
    assert winner_row["agent_name"] == "nightly-codex"
    assert winner_row["native_session_id"] == "codex-native-uuid"
    assert resolved_row is not None, "the Claude turn was left with no session at all"
    assert resolved_row["agent_backend"] == "claude"
    assert resolved_row["session_anchor"] == "slack_CA"


#: The id read of ``_delete_agent_session_rows`` for the ``/new`` clear shape
#: (``agent_name``, no anchor prefix). Every predicate the teardown decides from --
#: the scope, the agent-name match, and the ``include_superseded=False`` guard -- is
#: evaluated HERE, before any write lock exists.
_TEARDOWN_ID_SELECT = (
    "SELECT agent_sessions.id FROM agent_sessions WHERE agent_sessions.scope_id = ? "
    "AND (agent_sessions.agent_backend = ? OR agent_sessions.agent_variant = ?) "
    "AND (agent_sessions.session_anchor IS NULL OR agent_sessions.session_anchor "
    "NOT LIKE ? ESCAPE '\\')"
)


def test_new_teardown_keeps_a_session_superseded_inside_its_window(tmp_path: Path) -> None:
    """HFR-260 — the hard delete applied the teardown query in the READ only.

    THE PRODUCTION STORY. ``/new`` in a thread runs ``clear_sessions()`` through every
    backend adapter, which reach ``delete_agent_sessions(scope_key, agent_name=...)``.
    At the same moment a claim for another backend supersedes the thread's session R:
    R's anchor is moved aside and a replacement row takes the slot.

    THE DEFECT IS THE WINDOW. ``delete_agent_sessions`` excludes superseded rows from
    EVERY deletion path on purpose -- superseding PROMISES the row is kept, because its
    native id is write-once and its transcript is not recoverable -- but that guard
    lived only in the id SELECT, which runs before any write lock exists. R's id was
    read while its anchor was still bare, so the ``DELETE ... WHERE id IN (...)`` that
    followed hard-deleted exactly the row the guard exists to protect, and returned it
    in the count.

    The fix re-asserts the WHOLE teardown query inside the DELETE, so every predicate is
    re-evaluated with the write lock held, whichever caller built them.

    WHAT THIS TEST DELIBERATELY DOES NOT ASSERT: that the reclaim is undone. The reclaim
    must run before the delete (it needs both rows visible for the settings snapshot), and
    rolling it back needs a SAVEPOINT around both -- which under WAL pins a read snapshot
    at the reclaim's own SELECT and makes its UPDATE fail with ``SQLITE_BUSY_SNAPSHOT``
    on this very interleaving (measured while writing this test). So the pause stands and
    is honestly reported; the kept row is a superseded one the thread has moved off, and
    ``pause`` is the recoverable mode.
    """
    from storage.session_reclaim import session_teardown_context

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C6", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            superseded_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_C6",
                agent_backend="codex",
                agent_variant="codex",
                native_session_id="codex-native-uuid",
                workdir=str(tmp_path),
            )
            _bind_definition(conn, definition_id="def-pinned", session_id=superseded_id)
            _bind_definition(
                conn,
                definition_id="def-owner-only",
                session_id=None,
                enabled=0,
                metadata={
                    "created_by": {"caller": {"session_id": superseded_id}},
                    "origin": "cli",
                },
            )

        def _winner_supersedes(other_conn) -> None:  # noqa: ANN001
            other_conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == superseded_id)
                .values(
                    session_anchor=f"slack_C6:superseded:{superseded_id}",
                    updated_at="2026-07-28T00:00:01Z",
                )
            )
            create_agent_session_row(
                other_conn,
                scope_id=scope_id,
                session_id="seswinner002",
                session_anchor="slack_C6",
                agent_backend="claude",
                agent_variant="claude",
                native_session_id="",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:01Z",
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_TEARDOWN_ID_SELECT,
            write=_winner_supersedes,
        )

        with session_teardown_context(reason="the bound agent session was cleared") as ledger:
            removed = service.delete_agent_sessions(scope_key="slack::C6", agent_name="codex")
            ledger_entries = list(ledger)

        kept_row = service.get_agent_session_by_id(superseded_id)
        with service.engine.begin() as conn:
            definition = (
                conn.execute(select(run_definitions).where(run_definitions.c.id == "def-pinned"))
                .mappings()
                .first()
            )
            owner_only = (
                conn.execute(select(run_definitions).where(run_definitions.c.id == "def-owner-only"))
                .mappings()
                .one()
            )
        from storage.background import SQLiteBackgroundTaskStore, task_resume_block

        metadata = json.loads(owner_only["metadata_json"])
        assert task_resume_block(metadata, owner_only["session_id"]) is None
        task_store = SQLiteBackgroundTaskStore(db_path)
        try:
            assert task_store.set_definition_enabled(
                "def-owner-only",
                True,
                definition_type="scheduled",
            )
        finally:
            task_store.close()
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing supersede never landed inside the window, so this test proved "
        "nothing about the race — the keyed read is no longer the SQL the code emits"
    )
    assert removed == 0, (
        f"the teardown reported {removed!r} sessions deleted; the only candidate it "
        "read had become a SUPERSEDED row by the time the delete ran, and the "
        "include_superseded=False guard exists to keep exactly that row"
    )
    assert kept_row is not None, (
        "the /new teardown hard-deleted a row that was superseded inside its window; "
        "superseding promises the row is kept, its native id is write-once and its "
        "transcript is not recoverable"
    )
    assert kept_row["session_anchor"] == f"slack_C6:superseded:{superseded_id}"
    assert kept_row["native_session_id"] == "codex-native-uuid"
    # The reclaim stands (see the docstring) and the ledger says exactly that, so the
    # user's reply is still true about what happened to their tasks.
    assert definition is not None
    assert definition["enabled"] == 0
    assert [entry["definition_id"] for entry in ledger_entries] == ["def-pinned"], (
        f"the /new reply counts {ledger_entries!r}, which is not what the teardown did"
    )


_DEFINITION_RESUME_SELECT = (
    "SELECT run_definitions.definition_type, run_definitions.mode, "
    "run_definitions.schedule_type, run_definitions.retired_at, run_definitions.enabled, "
    "run_definitions.session_id, run_definitions.metadata_json FROM run_definitions "
    "WHERE run_definitions.id = ? AND run_definitions.deleted_at IS NULL"
)


# --- Meta-guard: every writer of the session ROUTE must stay marker-aware ---
#
# Deliberately NOT catalogued as a product scenario: it asserts nothing a user can
# observe, it converts "the next writer must remember the override marker" from a
# review habit into a CI failure. The user-visible regressions it backs are
# HFR-245/246/247/250.

#: Modules allowed to write the backend-owned session route -- ``model`` /
#: ``reasoning_effort`` (the pinnable settings) or ``agent_backend`` /
#: ``agent_variant`` (what makes those settings stale). Each one must ALSO
#: reconcile the row's ``explicit_setting_overrides`` marker, which is re-checked
#: below -- a stale marker keeps forcing dispatch to honour a value the writer
#: just invalidated.
_MARKER_AWARE_SESSION_ROUTE_WRITERS = {
    # create_agent_session_row (insert: the caller hands in its own metadata) and
    # _claim_anchor_row, which resets the whole route of an unbound row on a
    # cross-backend adoption and clears the marker with it.
    "storage/agent_session_rows.py",
    # update_session: a PRESENT model / reasoning_effort (including null, the Chat
    # header's "Default") drops that field's marker entry.
    "storage/workbench_sessions_service.py",
    # materialize_agent_session_route skips any field the row pins explicitly, and
    # bind_agent_session_by_id clears both pinnable columns (marker included) when
    # its bind moves an unbound row to another backend; the remaining sites here
    # write the route only as part of creating / importing / relabelling a row.
    "storage/sessions_service.py",
    # First-turn binding materializes an inherited backend on an agentless row and
    # normalizes its override marker without changing explicitly pinned settings.
    "core/session_turns.py",
}

#: The reconciliation API a marker-aware writer has to reach for.
_MARKER_RECONCILERS = ("reconcile_explicit_overrides", "explicit_override_names")

#: Per-write-site escape hatch: ``module -> {enclosing function name: why}``.
#:
#: The module-level check below is satisfied by a module that reconciles the marker
#: ANYWHERE in the file, so a brand-new direct writer dropped into an already
#: allowlisted module passes it for free. The per-function check closes that: the
#: function containing each write site must reach the reconciliation API ITSELF,
#: unless it is listed here. Keep this small -- every entry is a function that
#: provably does not need to reconcile, not a function that has not got round to it.
_MARKER_EXEMPT_ROUTE_WRITE_SITES = {
    "storage/agent_session_rows.py": {
        # INSERT path: the row does not exist yet, so there is no prior marker to
        # reconcile against. ``metadata`` (marker included) is supplied by the
        # caller, which is the layer that knows whether the new row PINS its
        # model / effort or merely inherits them.
        "create_agent_session_row": "INSERT path: metadata comes from the caller",
    },
    "storage/sessions_service.py": {
        # Native-bind UPDATEs -- TWO detected sites since HFR-254 split the write
        # into a write-once-guarded first bind and the remaining update. Both share
        # one ``values`` dict, which carries status / timestamps /
        # native_session_id / agent identity and NONE of the four route columns --
        # not model / reasoning_effort, and not agent_backend / agent_variant
        # either (any backend relabel on this path happens inside
        # ``_find_agent_session_row_id``, which is detected separately). The
        # ``model=None`` it passes goes to ``get_or_create_agent_session_row``,
        # i.e. the INSERT path above. Detected only by the ``**values`` heuristic.
        "bind_agent_session": "native bind: neither UPDATE sets a route column",
        # Legacy-placeholder relabel: the only route column write here fills an
        # agent_backend / agent_variant that is BLANK or ``"default"`` (the branch is
        # guarded on both being in ``("", "default")``, and is reached only after the
        # concrete-backend lookup for the same (scope, anchor) found nothing) with
        # the concrete backend. A placeholder label names no previous backend, so no
        # setting on that row can be a *different* backend's -- there is nothing for
        # the marker to have gone stale about -- and the statement writes neither
        # pinnable column. Contrast HFR-250, which moved a row between two CONCRETE
        # backends.
        "_find_agent_session_row_id": (
            "fills a blank / 'default' placeholder backend label with the concrete "
            "backend; not a move between backends, and writes neither pinnable column"
        ),
        # Legacy sessions.json import. The DETECTED site is the INSERT ``.values()``,
        # with metadata built from the legacy state being imported, so there is no
        # prior marker to reconcile. Its ``ON CONFLICT DO UPDATE`` ``set_`` excludes
        # model / reasoning_effort, so an existing row's pinnable columns and its
        # marker are left exactly as they were. NOTE: that ``set_`` DOES list
        # agent_backend / agent_variant, and the conflict row is resolved by
        # ``(scope, anchor)`` regardless of backend -- so an import CAN relabel an
        # existing row's backend without reconciling. Recorded here rather than
        # silently exempted; the detector cannot express it either way, because it
        # matches the ``.values()`` and not the ``set_``.
        "save_state": (
            "legacy import: the detected site is the INSERT path, and the upsert's "
            "set_ excludes both pinnable columns (see the note on its backend relabel)"
        ),
        # Native repair snapshot: the INSERT copies the old binding's complete
        # route and metadata marker unchanged into an inert superseded row. The
        # active-row UPDATE changes only native_session_id and timestamps.
        "replace_agent_session_native": (
            "native repair snapshot: route and marker are copied together unchanged"
        ),
    },
}


#: The columns whose write makes a statement a ROUTE write.
#:
#: ``model`` / ``reasoning_effort`` are the pinnable settings the marker names.
#: ``agent_backend`` / ``agent_variant`` are here because they are what makes
#: those settings STALE: a statement that moves the row to another backend and
#: touches neither pinnable column still invalidates both, and the marker it
#: leaves behind keeps telling dispatch the old backend's model is a deliberate
#: pin. That exact shape is HFR-250, and the old detector could not see it.
_ROUTE_COLUMNS = ("model", "reasoning_effort", "agent_backend", "agent_variant")


def _agent_session_route_writers() -> dict[str, list[int]]:
    """Repo-relative module -> line numbers writing any ``_ROUTE_COLUMNS`` column.

    Matches both the ORM shape (``agent_sessions.update()`` /
    ``update(agent_sessions)`` followed by ``.values(...)``) and raw
    ``UPDATE agent_sessions`` SQL. A ``**kwargs`` values dict counts: that is how
    every dynamic writer in this repo sets these columns.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    skip_parts = {".git", "tests", "ui", "node_modules", ".venv", "__pycache__", ".runtime", "docs"}
    orm_stmt = re.compile(r"agent_sessions\.(?:update|insert)\(\)|(?:update|insert)\(\s*agent_sessions\s*\)")
    raw_stmt = re.compile(r"(?:UPDATE|INSERT\s+INTO)\s+agent_sessions", re.IGNORECASE)
    settings = re.compile(
        "|".join(rf"\b{name}\s*=" for name in _ROUTE_COLUMNS) + r"|\*\*"
    )

    def balanced(text: str, start: int) -> str:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return text[start:]

    found: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        if any(part in skip_parts for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for match in orm_stmt.finditer(text):
            values_at = text.find(".values(", match.end())
            if values_at < 0:
                continue
            if settings.search(balanced(text, values_at + len(".values"))):
                found.setdefault(rel, []).append(text[:values_at].count("\n") + 1)
        raw_columns = re.compile("|".join(rf"\b{name}\b" for name in _ROUTE_COLUMNS))
        for match in raw_stmt.finditer(text):
            statement = text[match.start() : match.start() + 800]
            if raw_columns.search(statement):
                found.setdefault(rel, []).append(text[: match.start()].count("\n") + 1)
    return found


def _enclosing_functions(
    source: str, lines: list[int]
) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef | None]:
    """line number -> the INNERMOST function definition containing it, or None.

    Parsed with ``ast`` rather than matched textually so "which function owns this
    write" is decided by the same structure Python sees; a nested helper wins over
    the method it lives in.
    """
    tree = ast.parse(source)
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    owners: dict[int, ast.FunctionDef | ast.AsyncFunctionDef | None] = {}
    for line in lines:
        containing = [
            node
            for node in definitions
            if node.lineno <= line <= (node.end_lineno or node.lineno)
        ]
        # Innermost = the one that starts last among those that contain the line.
        owners[line] = max(containing, key=lambda node: node.lineno) if containing else None
    return owners


def test_only_marker_aware_modules_write_the_pinnable_session_columns() -> None:
    """A new writer of the session ROUTE must reconcile the override marker.

    ``agent_sessions.metadata_json.explicit_setting_overrides`` is a claim about
    what the ROW currently pins, so it is only correct while every writer that can
    invalidate that claim maintains it. It shipped maintained by exactly one
    writer, and three of the others were already stale (HFR-245/246/247). This test
    is the standing reminder: add the writer to an allowlist only after it
    reconciles the marker.

    THE QUESTION IT ASKS, widened by HFR-250. It used to be "does this statement
    write ``model`` / ``reasoning_effort``, and if so does its function reconcile?"
    -- which has a blind spot exactly the shape of the defect HFR-250 fixes: a
    statement that rewrites the BACKEND-OWNED ROUTE (``agent_backend`` /
    ``agent_variant``) while touching NEITHER pinnable column is invisible to it,
    even though moving a row to another backend invalidates both of those columns
    and the marker that pins them. The old allowlist even EXEMPTED
    ``bind_agent_session_by_id`` on the grounds that its UPDATE "never sets model /
    reasoning_effort" -- true, and precisely why the bug survived. So the question
    is now: does this statement write ANY of ``_ROUTE_COLUMNS``?

    WHAT IT PROVES, exactly:

    1. every file outside ``_MARKER_AWARE_SESSION_ROUTE_WRITERS`` is free of
       detectable ``INSERT``/``UPDATE`` statements on ``agent_sessions`` that set
       any of ``_ROUTE_COLUMNS``;
    2. each allowlisted module mentions the reconciliation API somewhere;
    3. for each DETECTED write site in an allowlisted module, the function that
       lexically encloses it mentions the reconciliation API in its OWN body, or
       that function is named in ``_MARKER_EXEMPT_ROUTE_WRITE_SITES`` with a
       reason;
    4. both allowlists are free of stale entries.

    (3) is the point of the AST pass. The module-level check (2) is satisfied by a
    reconciliation ANYWHERE in the file, so a brand-new direct writer added to an
    already allowlisted module used to pass for free -- that specific hole is what
    the per-function check closes.

    WHAT IT DOES NOT PROVE. This is a static text/AST inspection over a hand-kept
    allowlist, not enforcement by construction. It cannot see:

    - a write that reaches the columns through an ALIAS or a dynamically built
      table reference (``t = agent_sessions``, ``metadata.tables["agent_sessions"]``,
      an f-string / ``text()`` SQL string, an ORM mapper, a raw ``cursor.execute``
      with the table name assembled at runtime);
    - a write whose column names are computed rather than written literally, since
      the detector's own column test is a regex over the statement text;
    - migrations, fixtures, or any path under the skipped directories
      (``tests/``, ``ui/``, ``docs/``, ...);
    - route columns written by an ``ON CONFLICT DO UPDATE`` ``set_=`` clause rather
      than the statement's own ``.values(...)``. The ORM branch matches the FIRST
      ``.values(`` after the statement and stops there, so an upsert can relabel an
      existing row's backend from ``set_`` and be judged on its INSERT values
      instead -- ``save_state`` is exactly that case, recorded in its exemption
      reason;
    - WHETHER the reconciliation a function performs is CORRECT, or even reached on
      the branch that does the write. Presence of the call is all that is checked --
      HFR-248 is a case where the call was present and reconciled exactly backwards,
      and this test was green throughout that inversion.

    So it is a tripwire for the ordinary case (someone adds a normal UPDATE), not a
    guarantee. The behavioural guarantees live in HFR-244/245/246/247/248/249/250.
    """
    writers = _agent_session_route_writers()
    unexpected = {
        module: lines
        for module, lines in writers.items()
        if module not in _MARKER_AWARE_SESSION_ROUTE_WRITERS
    }
    assert not unexpected, (
        "these modules write the agent_sessions route ("
        + " / ".join(_ROUTE_COLUMNS)
        + ") without being listed as marker-aware: "
        + "; ".join(f"{module}:{lines}" for module, lines in sorted(unexpected.items()))
        + " -- reconcile the row's explicit_setting_overrides marker in the same "
        "statement (storage.session_reclaim.reconcile_explicit_overrides), then add "
        "the module to _MARKER_AWARE_SESSION_ROUTE_WRITERS with the reason"
    )

    root = Path(__file__).resolve().parents[1]
    for module in sorted(_MARKER_AWARE_SESSION_ROUTE_WRITERS):
        assert module in writers, (
            f"stale allowlist entry: {module} no longer writes any route column"
        )
        source = (root / module).read_text(encoding="utf-8")
        # Module level: kept as the outer ring. Cheap, and it catches an allowlist
        # entry whose reconciliation was deleted wholesale.
        assert any(name in source for name in _MARKER_RECONCILERS), (
            f"{module} is allowlisted as marker-aware but never reaches the "
            f"reconciliation API ({' / '.join(_MARKER_RECONCILERS)})"
        )

        # Per-write-site: the function doing the write must reconcile, itself.
        exempt = _MARKER_EXEMPT_ROUTE_WRITE_SITES.get(module, {})
        owners = _enclosing_functions(source, writers[module])
        seen_exempt: set[str] = set()
        for line, owner in sorted(owners.items()):
            assert owner is not None, (
                f"{module}:{line} writes an agent_sessions route column at module "
                "level, outside any function -- there is nowhere to reconcile the "
                "explicit_setting_overrides marker from"
            )
            name = owner.name
            if name in exempt:
                seen_exempt.add(name)
                continue
            body = ast.get_source_segment(source, owner) or ""
            assert any(reconciler in body for reconciler in _MARKER_RECONCILERS), (
                f"{module}:{line} writes an agent_sessions route column ("
                + " / ".join(_ROUTE_COLUMNS)
                + f") inside {name}(), which never reaches the reconciliation API "
                f"({' / '.join(_MARKER_RECONCILERS)}). Its module is allowlisted, but "
                "the allowlist is per module -- a stale marker keeps forcing dispatch "
                "to honour the value this write just changed. Reconcile it in the same "
                "statement (storage.session_reclaim.reconcile_explicit_overrides), or, "
                "if this write provably needs no reconciliation, add "
                f"{name!r} to _MARKER_EXEMPT_ROUTE_WRITE_SITES[{module!r}] with the reason"
            )
        stale_exempt = sorted(set(exempt) - seen_exempt)
        assert not stale_exempt, (
            f"stale per-function exemptions in {module}: {stale_exempt} no longer "
            "enclose a detected route-column write"
        )

    stale_modules = sorted(set(_MARKER_EXEMPT_ROUTE_WRITE_SITES) - _MARKER_AWARE_SESSION_ROUTE_WRITERS)
    assert not stale_modules, (
        f"per-function exemptions for modules that are not allowlisted writers: {stale_modules}"
    )



#: The LAST read the first-turn INSERT decides from: ``new_session_id``'s scan of every
#: existing session id. Proving the write lock is already held when this statement
#: completes proves it was taken before every read the create rests on, because it is the
#: read closest to the INSERT.
_SESSION_ID_SCAN_SELECT = "SELECT agent_sessions.id FROM agent_sessions"

#: The two spellings ``reserve_write_lock`` uses to become the writer: ``BEGIN IMMEDIATE``
#: when the connection is in autocommit, and a never-matching UPDATE when a transaction is
#: already open and ``BEGIN IMMEDIATE`` would be illegal. Asserting on the SEQUENCE keeps
#: the guarantee observable even where a competing writer is hard to schedule.
_WRITE_LOCK_RESERVATIONS = ("BEGIN IMMEDIATE", "UPDATE agent_sessions SET id = id WHERE 1 = 0")


def _refuse_a_competing_writer_at(engine, db_path: Path, *, read: str, write) -> dict:
    """Prove the code under test HOLDS SQLite's write lock when ``read`` completes.

    The mirror image of ``_commit_competing_bind_after``: that helper lands a competing
    commit inside an UNLOCKED window, and this one proves there is no window left for one
    to land in. The competing connection is given ``busy_timeout = 0``, so rather than
    waiting out the engine's 5s pragma it is refused the instant the writer slot is taken
    -- positive, deterministic evidence that another transaction owns it, with no sleeping,
    no second thread, and no dependence on ``sqlite3.Error.sqlite_errorcode`` (Python
    3.11+), so the observation is identical on every interpreter this project supports.

    Records all three outcomes separately, because they mean different things: ``fired``
    at 0 says the keyed read is no longer the SQL the code emits and the test proved
    nothing; ``committed`` says the lock was NOT held, which is the defect; ``refused`` is
    the pass.
    """

    state: dict = {"fired": 0, "refused": [], "committed": 0}

    @event.listens_for(engine, "after_cursor_execute")
    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != read:
            return
        state["fired"] += 1
        other = create_sqlite_engine(db_path)
        try:
            with other.connect() as other_conn:
                other_conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                try:
                    write(other_conn)
                    other_conn.commit()
                except OperationalError as exc:
                    state["refused"].append(str(exc))
                else:
                    state["committed"] += 1
        finally:
            other.dispose()

    return state


def test_direct_task_resume_reserves_write_lock_before_resumability_read(tmp_path: Path) -> None:
    """A Harness resume cannot overtake an orphan marker from Session teardown."""

    from storage.background import SQLiteBackgroundTaskStore

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            _bind_definition(
                conn,
                definition_id="def-resume-race",
                session_id=None,
                enabled=0,
                metadata={
                    "created_by": {"caller": {"session_id": "ses-owner"}},
                    "origin": "cli",
                },
            )
    finally:
        service.close()

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        def _stamp_orphan_marker(other_conn) -> None:  # noqa: ANN001
            other_conn.execute(
                run_definitions.update()
                .where(run_definitions.c.id == "def-resume-race")
                .values(
                    enabled=0,
                    metadata_json=json.dumps(
                        {
                            "created_by": {"caller": {"session_id": "ses-owner"}},
                            "origin": "cli",
                            "orphaned_task_owner": {
                                "reason_code": "task_owner_session_unavailable",
                                "owner_session_id": "ses-owner",
                            },
                        }
                    ),
                    updated_at="2026-08-11T00:00:01Z",
                )
            )

        race = _refuse_a_competing_writer_at(
            store.engine,
            db_path,
            read=_DEFINITION_RESUME_SELECT,
            write=_stamp_orphan_marker,
        )

        assert store.set_definition_enabled(
            "def-resume-race",
            True,
            definition_type="scheduled",
        )
        saved = store.get_scheduled_task("def-resume-race")
    finally:
        store.close()

    assert race["fired"] == 1, "the test did not observe the resumability decision read"
    assert race["committed"] == 0, "Session teardown wrote inside the resume window"
    assert race["refused"], "the resume path did not hold SQLite's writer slot"
    assert saved is not None and saved["enabled"] is True


def _record_statements(engine) -> list[str]:
    """Collect the normalised SQL the engine emits, in order."""

    seen: list[str] = []

    @event.listens_for(engine, "after_cursor_execute")
    def _collect(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(" ".join(statement.split()))

    return seen


def test_first_turn_insert_reserves_the_write_lock_before_the_reads_it_decides_from(
    tmp_path: Path,
) -> None:
    """HFR-262 — the first-turn INSERT decided from a snapshot it could not write from.

    THE PRODUCTION STORY. Two turns arrive on a brand-new thread at once. Neither finds a
    session row, so both fall into ``get_or_create_agent_session_row`` and try to INSERT
    the first one.

    THE DEFECT WAS *WHEN* THE WINNER COMMITS. SQLite takes the write lock at a
    transaction's first WRITE, never at its reads. The old code opened a ``SAVEPOINT`` and
    then READ -- ``new_session_id``'s scan of every session id -- which under WAL pins a
    read snapshot with no lock behind it. A winner committing between that scan and the
    INSERT left the loser unable to become a writer AT ALL: SQLite answers
    ``SQLITE_BUSY_SNAPSHOT``, ``busy_timeout`` never retries it because waiting cannot make
    a snapshot newer, and it arrives as a PLAIN ``OperationalError`` carrying the same
    "database is locked" text every unrelated busy error carries. The ``IntegrityError``
    catch did not cover it, so ``ensure_agent_session_id`` -- and every other caller of
    this shared get-or-create -- surfaced a database error out of an ordinary first turn.

    WHY THIS TEST DOES NOT WATCH FOR THAT ERROR. RECOGNISING it needs
    ``sqlite3.Error.sqlite_errorcode``, which exists from Python 3.11, while this project
    supports 3.10 (``requires-python >= 3.10``): a fix built on detection is not a fix on a
    supported runtime. ``reserve_write_lock`` removes the window instead -- the create path
    becomes the writer BEFORE its reads -- so the interleaving cannot occur and there is no
    error left to classify on any interpreter.

    SO THE ASSERTION IS THE MECHANISM, NOT THE SYMPTOM: a competing writer given zero
    patience must be REFUSED at the last read before the INSERT. That observation is the
    same on 3.10 and on 3.13, and it fails against the pre-fix code, where the competitor
    commits inside the savepoint instead.
    """

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    competitor_id = "seswinner001"
    raised: list[BaseException] = []
    try:
        with service.engine.begin() as conn:
            # A TWO-part scope key with the scope already present: the resolve is then a
            # pure SELECT, so the caller's transaction holds NO write lock of its own when
            # the create path is entered. A three-part key upserts the scope and would
            # close the window by accident, which is why the sibling test below covers
            # both shapes rather than only this one.
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C9", now="2026-07-28T00:00:00Z")
            assert scope_id is not None

        def _competitor_creates_the_first_row(other_conn) -> None:  # noqa: ANN001
            """The other turn trying to win the same brand-new anchor."""
            create_agent_session_row(
                other_conn,
                scope_id=scope_id,
                session_id=competitor_id,
                session_anchor="slack_C9",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                native_session_id="",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:01Z",
            )

        race = _refuse_a_competing_writer_at(
            service.engine,
            db_path,
            read=_SESSION_ID_SCAN_SELECT,
            write=_competitor_creates_the_first_row,
        )

        try:
            resolved = service.ensure_agent_session_id(
                scope_key="slack::C9",
                agent_name="claude",
                session_anchor="slack_C9",
                workdir=str(tmp_path),
                vibe_agent_id="agent-claude",
                vibe_agent_name="review-claude",
            )
        except Exception as exc:  # noqa: BLE001 - the original defect IS the escape
            raised.append(exc)
            resolved = None
    finally:
        service.close()

    # Read the committed state through a FRESH engine: only a real commit puts it here.
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            anchor_rows = [
                dict(row)
                for row in conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.agent_backend)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .where(agent_sessions.c.session_anchor == "slack_C9")
                ).mappings()
            ]
            all_ids = list(
                conn.execute(select(agent_sessions.c.id).where(agent_sessions.c.scope_id == scope_id))
                .scalars()
                .all()
            )
    finally:
        engine.dispose()

    assert race["fired"] == 1, (
        "the competing insert never ran inside the window, so this test proved nothing "
        "about the race — the keyed read is no longer the SQL the code emits"
    )
    assert race["committed"] == 0, (
        "a competing connection took the write lock while the first-turn create was "
        "reading the state its INSERT decides from; that window is exactly what produces "
        "SQLITE_BUSY_SNAPSHOT, and no interleaving may be able to enter it"
    )
    assert len(race["refused"]) == 1, (
        f"the competing writer neither committed nor was refused: {race!r}"
    )
    assert not raised, (
        f"a first turn raised {raised[0]!r} out of get_or_create_agent_session_row; the "
        "create path must hold the write lock across the reads its INSERT decides from, "
        "so a competing turn cannot make its snapshot stale"
    )
    assert [row["id"] for row in anchor_rows] == [resolved], (
        f"the anchor slack_C9 is held by {anchor_rows!r}, not by the session {resolved!r} "
        "this turn was handed"
    )
    assert anchor_rows[0]["agent_backend"] == "claude"
    assert all_ids == [resolved], (
        f"scope {scope_id} holds {all_ids!r}; exactly ONE session row may exist for a "
        "thread whose first turn was contested"
    )
    assert competitor_id not in all_ids, (
        f"the refused competitor's row {competitor_id!r} was committed anyway"
    )


def test_first_turn_insert_joins_a_winner_that_committed_before_the_reservation(
    tmp_path: Path,
) -> None:
    """HFR-262 — the UNLOCKED first read is a hint, and the reserved re-read is the answer.

    THE WINDOW THAT REMAINS, and must not matter. ``get_or_create_agent_session_row``
    deliberately keeps its FIRST anchor read unlocked: it is the hot path every inbound
    message takes and a session row almost always already exists, so serialising it would
    queue unrelated threads behind each other for no gain. The consequence is that a
    winner can still commit between that read and the reservation -- which under the old
    code was the ``IntegrityError`` half of this race, and is why the create path must
    take its decision AGAIN once the write lock is held.

    THE DEFECT THIS PINS is a create that trusts the unlocked read: it would INSERT a
    second row for a thread that already has one, and the UNIQUE ``(scope_id,
    session_anchor)`` index is the only reason that surfaces as an error rather than a
    silent duplicate. With the reserved re-read the loser sees the winner and joins it
    through ``_claim_anchor_row`` instead, which is the answer the contract promises.

    A SERIAL TEST CANNOT OBSERVE THIS: commit the winner first and the unlocked read finds
    it, which is the ordinary already-exists path. The commit has to land between the two
    reads.
    """

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    winner_id = "seswinner001"
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::CB", now="2026-07-28T00:00:00Z")
            assert scope_id is not None

        def _winner_creates_the_first_row(other_conn) -> None:  # noqa: ANN001
            """On the OTHER backend, so the loser must reconcile through the relabel.

            That relabel is a WRITE, and it runs after the reservation, so a green test
            also proves the reserved transaction stays usable and commits.
            """
            create_agent_session_row(
                other_conn,
                scope_id=scope_id,
                session_id=winner_id,
                session_anchor="slack_CB",
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-codex",
                agent_name="nightly-codex",
                native_session_id="",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:01Z",
            )

        race = _commit_competing_bind_after(
            service.engine,
            db_path,
            read=_ANCHOR_DECISION_SELECT,
            write=_winner_creates_the_first_row,
        )

        resolved = service.ensure_agent_session_id(
            scope_key="slack::CB",
            agent_name="claude",
            session_anchor="slack_CB",
            workdir=str(tmp_path),
            vibe_agent_id="agent-claude",
            vibe_agent_name="review-claude",
        )
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            anchor_rows = [
                dict(row)
                for row in conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.agent_backend)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .where(agent_sessions.c.session_anchor == "slack_CB")
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert race["fired"] == 1, (
        "the competing insert never landed between the unlocked read and the reservation, "
        "so this test proved nothing about the race"
    )
    assert [row["id"] for row in anchor_rows] == [winner_id], (
        f"the anchor slack_CB is held by {anchor_rows!r}; a create that trusted its "
        "unlocked read inserted a second row for a thread that already had one"
    )
    assert resolved == winner_id, (
        f"the loser answered {resolved!r}; it must hand back the session that holds the "
        "anchor now, so the turn runs in the session the thread actually has"
    )
    assert anchor_rows[0]["agent_backend"] == "claude", (
        f"the loser joined the winner's row but its relabel did not survive (backend is "
        f"{anchor_rows[0]['agent_backend']!r}): the reserved transaction must stay usable, "
        "so the writes it makes afterwards still commit"
    )


def _ensure_first_turn(service: SQLiteSessionsService, *, scope_key: str, anchor: str, workdir: str) -> str | None:
    return service.ensure_agent_session_id(
        scope_key=scope_key,
        agent_name="claude",
        session_anchor=anchor,
        workdir=workdir,
    )


def _bind_first_turn(service: SQLiteSessionsService, *, scope_key: str, anchor: str, workdir: str) -> str | None:
    return service.bind_agent_session(
        scope_key=scope_key,
        agent_name="claude",
        session_anchor=anchor,
        native_session_id="claude-native-uuid",
        workdir=workdir,
    )


@pytest.mark.parametrize(
    ("scope_key", "anchor", "call"),
    [
        # Two-part key: the scope resolve is a pure SELECT, so the create path enters in
        # autocommit and the reservation is ``BEGIN IMMEDIATE``.
        ("slack::CC1", "slack_CC1", _ensure_first_turn),
        # Three-part key: ``resolve_scope_from_legacy_key`` upserts the scope, so a
        # transaction is already open and the reservation is the never-matching UPDATE.
        ("slack::channel::CC2", "slack_CC2", _ensure_first_turn),
        # The second writer that reaches the same get-or-create on a first turn.
        ("slack::CC3", "slack_CC3", _bind_first_turn),
    ],
    ids=["ensure_two_part_scope_key", "ensure_three_part_scope_key", "bind_two_part_scope_key"],
)
def test_every_first_turn_entry_point_reserves_the_write_lock(
    tmp_path: Path,
    scope_key: str,
    anchor: str,
    call,
) -> None:
    """HFR-262 — the guarantee has to hold for EVERY path into the contested INSERT.

    One path holding the write lock is not the fix; the INSERT is shared, so a caller that
    reaches it without the lock reopens the window on its own. These are the production
    entry points into the create branch of ``get_or_create_agent_session_row`` that start
    from a ``SQLiteSessionsService`` method, and they differ in the ONE way that matters:
    whether the caller's transaction has already written by the time the create path is
    entered. A three-part scope key upserts the scope (so a transaction is open and
    ``BEGIN IMMEDIATE`` is illegal); a two-part key resolves with a pure SELECT (so it is
    not). ``reserve_write_lock`` has a spelling for each, and both must end with the lock
    actually held.

    ``resolve_agent_run_target`` is the third entry point and is pinned the same way in
    ``tests/test_agent_run_target.py``.

    Checked TWO ways, because each catches what the other cannot: the statement sequence
    shows a reservation ahead of the last read (so the mechanism is present and ordered),
    and a competing writer with zero patience is refused at that read (so the reservation
    really took the lock, rather than merely being emitted).
    """

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    competitor_id = "seswinner001"
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, scope_key, now="2026-07-28T00:00:00Z")
            assert scope_id is not None

        def _competitor_creates_the_first_row(other_conn) -> None:  # noqa: ANN001
            create_agent_session_row(
                other_conn,
                scope_id=scope_id,
                session_id=competitor_id,
                session_anchor=anchor,
                agent_backend="codex",
                agent_variant="codex",
                native_session_id="",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:01Z",
            )

        statements = _record_statements(service.engine)
        race = _refuse_a_competing_writer_at(
            service.engine,
            db_path,
            read=_SESSION_ID_SCAN_SELECT,
            write=_competitor_creates_the_first_row,
        )

        resolved = call(service, scope_key=scope_key, anchor=anchor, workdir=str(tmp_path))
    finally:
        service.close()

    assert resolved, f"{scope_key} produced no session at all"
    assert _SESSION_ID_SCAN_SELECT in statements, (
        "the id scan the INSERT decides from is no longer emitted, so neither check below "
        "means anything"
    )
    scan_at = statements.index(_SESSION_ID_SCAN_SELECT)
    reserved_at = [index for index, sql in enumerate(statements) if sql in _WRITE_LOCK_RESERVATIONS]
    assert reserved_at and min(reserved_at) < scan_at, (
        f"no write-lock reservation precedes the id scan for {scope_key}: {statements!r}"
    )
    assert race["fired"] == 1, (
        "the competing insert never ran inside the window, so this test proved nothing"
    )
    assert race["committed"] == 0, (
        f"for {scope_key} a competing connection took the write lock while the create path "
        f"was reading the state its INSERT decides from: {race!r}"
    )


def test_write_lock_reservation_takes_the_lock_without_touching_a_row(tmp_path: Path) -> None:
    """HFR-262 — the reservation used when a transaction is ALREADY open.

    ``BEGIN IMMEDIATE`` is illegal inside an open transaction, so ``reserve_write_lock``
    falls back to a write that changes nothing. That works because SQLite takes the write
    lock at the START of an UPDATE program, before its WHERE loop runs -- an implementation
    property rather than a documented promise, and the only reason this statement is here,
    so it is pinned rather than trusted. Both halves matter: it must take the lock, and it
    must leave the data alone.
    """

    from storage.agent_session_rows import reserve_write_lock

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    refused: list[str] = []
    committed: list[str] = []
    try:
        with service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::CD", now="2026-07-28T00:00:00Z")
            assert scope_id is not None
            existing_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_CD",
                agent_backend="codex",
                agent_variant="codex",
                native_session_id="codex-native-uuid",
                workdir=str(tmp_path),
                now="2026-07-28T00:00:00Z",
            )

        with service.engine.begin() as conn:
            # Force the "transaction already open" state WITHOUT writing: pysqlite emits no
            # BEGIN for a bare SELECT, so a raw one is the only way to reach the branch.
            conn.exec_driver_sql("BEGIN")
            assert conn.connection.dbapi_connection.in_transaction is True
            reserve_write_lock(conn)

            other = create_sqlite_engine(db_path)
            try:
                with other.connect() as other_conn:
                    other_conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                    try:
                        other_conn.execute(
                            agent_sessions.update()
                            .where(agent_sessions.c.id == existing_id)
                            .values(agent_backend="claude")
                        )
                        other_conn.commit()
                    except OperationalError as exc:
                        refused.append(str(exc))
                    else:
                        committed.append("wrote")
            finally:
                other.dispose()
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.agent_backend, agent_sessions.c.updated_at)
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert not committed, (
        "a competing writer took the write lock after the reservation ran, so the "
        "never-matching UPDATE did not reserve the writer slot and the create path it "
        "guards is still racing"
    )
    assert len(refused) == 1, f"the competing writer was neither refused nor committed: {refused!r}"
    assert rows == [
        {"id": existing_id, "agent_backend": "codex", "updated_at": "2026-07-28T00:00:00Z"}
    ], (
        f"the reservation changed data: {rows!r}. It exists only to become the writer, so "
        "it must never match a row"
    )


#: The read ``release_reserved_agent_session`` decides from: the reserved row, still
#: unbound and still referenced by nothing (``_delete_agent_session_rows``'s ``id_query``).
#: Everything the release then does -- the reclaim, the DELETE, the workspace removal --
#: rests on this one row set.
_RESERVED_RELEASE_DECISION_SELECT = (
    "SELECT agent_sessions.id FROM agent_sessions WHERE agent_sessions.id = ? AND "
    "(agent_sessions.native_session_id IS NULL OR agent_sessions.native_session_id = ?) "
    "AND NOT (EXISTS (SELECT run_definitions.id FROM run_definitions "
    "WHERE run_definitions.session_id = ?))"
)


def _adopt_the_reservation_at_the_last_unlocked_instant(
    engine,
    db_path: Path,
    *,
    definition_id: str,
    session_id: str,
    updated_at: str,
) -> dict:
    """Commit a competing ADOPTION of the reserved session, from a real second connection.

    Fires at the last instant another transaction is ABLE to commit before the release
    treats the reserved row as unreferenced, which is a different statement in each
    version of the code and is the whole point of the fix:

    * BEFORE ``reserve_write_lock``'s reservation (the fixed order). The lock is not held
      yet, so the adoption commits, and the decision read that follows it SEES the
      adoption -- there is no window left between the read and the reclaim.
    * AFTER the decision read (the unfixed order, which emits no reservation at all). The
      read has already happened and reserved nothing, so this is the window: the reclaim
      that follows looks at the winner's definition.

    Fires ONCE either way, so the fixed order never reaches the second hook. The competing
    connection uses ``busy_timeout = 0`` and records its outcome instead of waiting, so a
    hook that ever lands after the write lock is taken shows up as ``refused`` rather than
    as a five-second stall -- and ``committed`` at 1 is what makes the interleaving real in
    BOTH versions, so the winner-untouched assertions mean the same thing in each.
    """

    state: dict = {"fired": 0, "committed": 0, "refused": [], "at": None}

    def _adopt(where: str) -> None:
        state["fired"] += 1
        state["at"] = where
        other = create_sqlite_engine(db_path)
        try:
            with other.connect() as other_conn:
                other_conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                try:
                    other_conn.execute(
                        run_definitions.update()
                        .where(run_definitions.c.id == definition_id)
                        .values(session_id=session_id, updated_at=updated_at)
                    )
                    other_conn.commit()
                except OperationalError as exc:
                    state["refused"].append(str(exc))
                else:
                    state["committed"] += 1
        finally:
            other.dispose()

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) not in _WRITE_LOCK_RESERVATIONS:
            return
        _adopt("before the write-lock reservation")

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != _RESERVED_RELEASE_DECISION_SELECT:
            return
        _adopt("after the decision read")

    return state


def test_releasing_a_reservation_cannot_damage_a_definition_that_adopted_it(
    tmp_path: Path,
) -> None:
    """HFR-278 — the reservation cleanup damaged the winner before deciding it may not delete.

    THE PRODUCTION STORY, one layer inside our own HFR-270 fix. A fire reserves a session,
    its guarded rebind is refused because another surface won the race, and HFR-270 gives
    the reservation back. Meanwhile the WINNER -- another fire, another process -- adopts
    that same reserved session id.

    THE DEFECT IS THE WINDOW, and the final DELETE is exactly why it went unnoticed.
    ``_delete_agent_session_rows`` runs its ``id_query`` first and
    ``reclaim_bound_definitions`` second, and that read reserves nothing (pysqlite opens no
    transaction for a bare SELECT). An adoption committing between the two left the reclaim
    reading the WINNER's definition: it PAUSED it, stamped its ``last_error`` with the
    release's reason, and overwrote its ``session_settings_snapshot``. The DELETE then
    re-evaluated ``NOT EXISTS``, correctly refused, and the function returned ``False`` --
    so the session row survived, the log said "not releasing", and the definition that had
    just adopted it was disabled anyway, with a pause reason naming a release that had
    decided to keep its session. The reclaim is deliberately never rolled back
    (``_delete_agent_session_rows`` documents why), so nothing corrected it.

    THE FIX IS ORDER, NOT DETECTION: ``reserve_write_lock`` before the read the decision
    rests on. The adoption can then only land BEFORE the release looks -- where the
    predicates see it and the release backs off untouched -- or after the release has
    committed. Re-asserting the predicates in the DELETE was never enough: it protects the
    ROW, and the damage was to a definition.

    ASSERTED ON THE WHOLE ROW THE WINNER OWNS, the HFR-251 lesson: not merely "the session
    survived" but its Session row, its workspace, its route/anchor, its pins and explicit
    override markers, its definition's enabled state, pause reason, settings snapshot AND
    every timestamp. A test that checked only ``enabled`` would have gone green over a
    reclaim that still rewrote the snapshot a later ``create_once`` rebind reads.
    """
    from storage.session_reclaim import (
        SESSION_SETTINGS_OVERRIDE_KEY,
        SESSION_SETTINGS_SNAPSHOT_KEY,
        session_teardown_context,
    )

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        # A standalone reservation: no Scope, and its own lazy Show workspace, which is
        # the one workdir ``_remove_reserved_workspace`` is entitled to delete -- so the
        # release really can destroy something on disk here.
        reserved_id = service.reserve_standalone_agent_session(
            agent_backend="codex",
            session_anchor="slack_C9:reserved",
            agent_id="agent-codex",
            agent_name="nightly-codex",
            model="gpt-5.5-codex",
            reasoning_effort="high",
            metadata={
                # The route the reservation carries, and the pins the winner inherits
                # with it.
                "legacy_scope_key": "slack::channel::C9",
                SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"],
            },
        )
        workspace = paths.get_show_page_dir(reserved_id)
        assert workspace.is_dir(), "the reservation did not create the workspace it owns"

        with service.engine.begin() as conn:
            # The winner's definition, not yet bound to anything: a create_once task
            # between reserving its session and committing the binding.
            _bind_definition(conn, definition_id="def-winner", session_id=None)
            definition_before = dict(
                conn.execute(
                    select(run_definitions).where(run_definitions.c.id == "def-winner")
                )
                .mappings()
                .one()
            )
            session_before = dict(
                conn.execute(select(agent_sessions).where(agent_sessions.c.id == reserved_id))
                .mappings()
                .one()
            )

        race = _adopt_the_reservation_at_the_last_unlocked_instant(
            service.engine,
            db_path,
            definition_id="def-winner",
            session_id=reserved_id,
            updated_at="2026-07-28T00:00:05Z",
        )

        with session_teardown_context(reason="reserved session released") as ledger:
            released = service.release_reserved_agent_session(
                reserved_id, reason="the rebind was refused"
            )
            ledger_entries = list(ledger)

        with service.engine.begin() as conn:
            definition_after = (
                conn.execute(
                    select(run_definitions).where(run_definitions.c.id == "def-winner")
                )
                .mappings()
                .first()
            )
            session_after = (
                conn.execute(select(agent_sessions).where(agent_sessions.c.id == reserved_id))
                .mappings()
                .first()
            )
    finally:
        service.close()

    assert race["fired"] == 1, (
        "the competing adoption never ran, so this test proved nothing about the race -- "
        "neither keyed statement is the SQL the code emits any more"
    )
    assert race["committed"] == 1, (
        f"the adoption could not commit at all ({race!r}), so there is no winner and the "
        "assertions below would hold for a reservation nobody wanted"
    )

    assert released is False, (
        "the release reported that it gave the reservation back, while another "
        "definition had adopted it: that is the dangling binding the whole reclaim "
        "machinery exists to prevent"
    )
    assert session_after is not None, (
        f"the release DELETED session {reserved_id} after a definition adopted it"
    )
    assert dict(session_after) == session_before, (
        "the release rewrote the Session the winner adopted. Differing fields: "
        + repr(
            {
                key: (value, session_before.get(key))
                for key, value in dict(session_after).items()
                if session_before.get(key) != value
            }
        )
    )
    assert workspace.is_dir(), (
        f"the release removed the workspace {workspace} of a session the winner is now "
        "bound to; its first turn has no cwd"
    )

    assert definition_after is not None
    # The ONLY change the winner's own adoption makes: its binding and the timestamp it
    # stamps. Everything else in the row must be exactly what it was.
    expected_definition = {
        **definition_before,
        "session_id": reserved_id,
        "updated_at": "2026-07-28T00:00:05Z",
    }
    assert dict(definition_after) == expected_definition, (
        "the release damaged the definition that adopted the reserved session before "
        "deciding it was not allowed to delete it. Differing fields: "
        + repr(
            {
                key: (value, expected_definition.get(key))
                for key, value in dict(definition_after).items()
                if expected_definition.get(key) != value
            }
        )
    )
    assert definition_after["enabled"] == 1, (
        "the winner's definition was PAUSED by a release that then kept its session: it "
        "is disabled, and nothing will re-enable it"
    )
    assert definition_after["last_error"] is None, (
        f"the winner's definition explains itself with {definition_after['last_error']!r}, "
        "a reason from a release that touched nothing it owns"
    )
    assert SESSION_SETTINGS_SNAPSHOT_KEY not in json.loads(
        definition_after["metadata_json"] or "{}"
    ), (
        "the release wrote a teardown settings snapshot onto a LIVE binding; a later "
        "create_once rebind reads that snapshot and would come back on the wrong route"
    )

    # The accounting half: a reclaim that must not happen must not be reported either.
    assert ledger_entries == [], (
        f"the release credited a reclaim to the teardown ledger: {ledger_entries!r}"
    )


def test_releasing_a_reservation_holds_the_write_lock_at_its_decision_read(
    tmp_path: Path,
) -> None:
    """HFR-278 — the mechanism, pinned where the previous test can only infer it.

    The test above proves the OUTCOME (a winner that adopted the reservation is left
    exactly as it was). This proves the PROPERTY that outcome now rests on: when the
    release's decision read completes, this transaction already owns SQLite's writer slot,
    so there is no instant at which an adoption can be committed between that read and the
    reclaim it authorises. Checked the two complementary ways the HFR-262 tests use: the
    statement sequence shows a reservation ahead of the read, and a competing writer with
    zero patience is REFUSED at it -- which is the half that cannot be faked by emitting a
    reservation that does not take the lock.
    """

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        reserved_id = service.reserve_standalone_agent_session(
            agent_backend="codex",
            session_anchor="slack_CA:reserved",
            workdir=str(tmp_path / "reserved-ws"),
        )
        with service.engine.begin() as conn:
            _bind_definition(conn, definition_id="def-late", session_id=None)

        def _adopt(other_conn) -> None:  # noqa: ANN001
            other_conn.execute(
                run_definitions.update()
                .where(run_definitions.c.id == "def-late")
                .values(session_id=reserved_id, updated_at="2026-07-28T00:00:09Z")
            )

        statements = _record_statements(service.engine)
        race = _refuse_a_competing_writer_at(
            service.engine,
            db_path,
            read=_RESERVED_RELEASE_DECISION_SELECT,
            write=_adopt,
        )
        released = service.release_reserved_agent_session(reserved_id, reason="lost the race")
    finally:
        service.close()

    assert _RESERVED_RELEASE_DECISION_SELECT in statements, (
        f"the release no longer emits the read it decides from: {statements!r}"
    )
    decision_at = statements.index(_RESERVED_RELEASE_DECISION_SELECT)
    reserved_at = [index for index, sql in enumerate(statements) if sql in _WRITE_LOCK_RESERVATIONS]
    assert reserved_at and min(reserved_at) < decision_at, (
        f"no write-lock reservation precedes the release's decision read: {statements!r}"
    )
    assert race["fired"] == 1, (
        "the competing adoption never ran inside the window, so this test proved nothing"
    )
    assert race["committed"] == 0, (
        f"a competing connection ADOPTED the reserved session while the release was "
        f"deciding it was unreferenced: {race!r}. The reclaim that follows that read then "
        "pauses the winner's definition"
    )
    assert released is True, (
        "nothing adopted the reservation, so the release must still remove it: a fix that "
        "makes the cleanup stop working is not a fix"
    )


def _activity_stamps(service: SQLiteSessionsService) -> dict[str, tuple[str, str]]:
    """Every row's ranking-relevant timestamps, keyed by session id."""

    with service.engine.connect() as conn:
        return {
            str(row["id"]): (str(row["last_active_at"]), str(row["updated_at"]))
            for row in conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.last_active_at,
                    agent_sessions.c.updated_at,
                )
            ).mappings()
        }


def test_save_state_does_not_move_last_active_at_of_any_existing_row(tmp_path: Path) -> None:
    """``save_state`` imports legacy mappings; it is not session activity.

    ``now`` is computed once per call, so a row it restamps is not merely wrong
    by a few microseconds -- every row it touches ends up sharing one identical
    value, which collapses the session list's ``last_active_at DESC`` ordering
    onto its tiebreakers. The assertion is therefore that *no* pre-existing row
    moved, seeded with one row of every shape this loop can reach rather than a
    list of the shapes it is expected to skip.
    """

    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        with service.engine.begin() as conn:
            adopted_scope = resolve_scope_from_legacy_key(conn, "slack::C_ADOPT", now="2026-07-01T00:00:00Z")
            archived_scope = resolve_scope_from_legacy_key(conn, "slack::C_ARCHIVED", now="2026-07-01T00:00:00Z")
            routed_scope = resolve_scope_from_legacy_key(conn, "slack::C_ROUTED", now="2026-07-01T00:00:00Z")

            # Shape 1: the imported mapping matches this row and its backend.
            adopted_id = create_agent_session_row(
                conn,
                scope_id=adopted_scope,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="slack_100.001",
                native_session_id="codex-native",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C_ADOPT"},
                now="2026-07-10T00:00:00+00:00",
                require_workdir=False,
            )
            # Shape 2: an archived row owns the anchor, so the import is skipped.
            archived_id = create_agent_session_row(
                conn,
                scope_id=archived_scope,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_200.002",
                native_session_id="archived-native",
                status="archived",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C_ARCHIVED"},
                now="2026-07-11T00:00:00+00:00",
                require_workdir=False,
            )
            # Shape 3: a backend-owned route the import must not relabel.
            routed_id = create_agent_session_row(
                conn,
                scope_id=routed_scope,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_300.003",
                native_session_id="claude-native",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C_ROUTED"},
                now="2026-07-12T00:00:00+00:00",
                require_workdir=False,
            )
            # Shape 4: a Session with no Scope at all.
            scopeless_id = create_agent_session_row(
                conn,
                scope_id=None,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="standalone_400.004",
                native_session_id="scopeless-native",
                workdir="/tmp",
                metadata={},
                now="2026-07-13T00:00:00+00:00",
                require_workdir=False,
            )

        before = _activity_stamps(service)
        assert set(before) == {adopted_id, archived_id, routed_id, scopeless_id}

        service.save_state(
            SessionState(
                session_mappings={
                    "slack::C_ADOPT": {"codex": {"slack_100.001": "codex-native"}},
                    "slack::C_ARCHIVED": {"codex": {"slack_200.002": "codex-native"}},
                    "slack::C_ROUTED": {"codex": {"slack_300.003": "codex-native"}},
                    "": {"codex": {"standalone_400.004": "scopeless-native"}},
                    # A mapping with no row yet: this one must still be stamped.
                    "slack::C_NEW": {"codex": {"slack_500.005": "new-native"}},
                }
            )
        )

        after = _activity_stamps(service)
        for session_id, stamps in before.items():
            assert after[session_id][0] == stamps[0], (
                f"save_state moved last_active_at of {session_id}: "
                f"{stamps[0]} -> {after[session_id][0]}"
            )

        created = set(after) - set(before)
        assert created, "save_state imported no new row, so the insert path proved nothing"
        for session_id in created:
            assert after[session_id][0], f"newly imported row {session_id} has no last_active_at"
    finally:
        service.close()


def test_startup_mapping_migration_does_not_restamp_sessions_without_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Session with no Scope must not make every startup re-save the state.

    ``_legacy_scope_key`` collapses a row with no Scope and no recorded legacy
    scope key onto the empty key. Treating that as a legacy raw key made the
    startup migration "migrate" it on every boot -- and each of those saves
    rewrote the whole state, so the fix has to hold across restarts, not just
    once.
    """

    sessions_path = tmp_path / "sessions.json"
    store = SessionsStore(sessions_path)
    try:
        with store._service.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, "slack::C123", now="2026-07-01T00:00:00Z")
            create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="claude",
                session_anchor="slack_171717.123",
                native_session_id="claude-native",
                workdir="/tmp",
                metadata={"legacy_scope_key": "slack::C123"},
                now="2026-07-20T00:00:00+00:00",
                require_workdir=False,
            )
            create_agent_session_row(
                conn,
                scope_id=None,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="archived:seed",
                native_session_id="codex-native",
                status="archived",
                workdir="/tmp",
                metadata={},
                now="2026-07-21T00:00:00+00:00",
                require_workdir=False,
            )
        before = _activity_stamps(store._service)
    finally:
        store.close()

    save_calls: list[object] = []
    original_save_state = SQLiteSessionsService.save_state

    def _spy(self: SQLiteSessionsService, state: SessionState) -> None:
        save_calls.append(state)
        return original_save_state(self, state)

    monkeypatch.setattr(SQLiteSessionsService, "save_state", _spy)

    for _ in range(2):
        restarted = SessionsStore(sessions_path)
        try:
            assert "archived:seed" in restarted.state.session_mappings.get("", {}).get("codex", {}), (
                "the scope-less row no longer loads under the empty key, so this test "
                f"no longer reproduces the trigger: {restarted.state.session_mappings!r}"
            )
            restarted.migrate_session_mappings("slack")
            assert "slack::" not in restarted.state.session_mappings, (
                "the empty key was prefixed onto a platform it has no relation to"
            )
            assert "archived:seed" in restarted.state.session_mappings.get("", {}).get("codex", {}), (
                "the migration dropped the scope-less Session's mapping"
            )
        finally:
            restarted.close()

    assert save_calls == [], (
        f"the startup migration still re-saved the whole session state {len(save_calls)} time(s)"
    )

    verify = SQLiteSessionsService(sessions_path.with_name("vibe.sqlite"))
    try:
        assert _activity_stamps(verify) == before
    finally:
        verify.close()
