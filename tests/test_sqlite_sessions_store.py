from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

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


def _commit_competing_bind_after(engine, db_path: Path, *, read: str, values: dict) -> dict:
    """Commit ``values`` onto a row from a REAL second connection, mid-flight.

    Hooks ``after_cursor_execute`` on the engine the code under test uses — the
    ENGINE, never ``bind_agent_session_by_id`` itself — and when the statement
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
                other_conn.execute(
                    agent_sessions.update()
                    .where(agent_sessions.c.id == values["id"])
                    .values(**{key: value for key, value in values.items() if key != "id"})
                )
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

    No backend change here, so the guard has nothing but the native predicate to
    stand on.
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
                session_anchor="slack_C123:race_same_backend",
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
            read=_WRITE_ONCE_SELECT,
            values={
                "id": reserved_id,
                "native_session_id": "native-winner",
                "status": "active",
                "updated_at": "2026-07-28T00:00:01Z",
                "last_active_at": "2026-07-28T00:00:01Z",
            },
        )

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
        f"the losing caller overwrote an already-committed native id with "
        f"{row['native_session_id']!r}; write-once was enforced by a SELECT that "
        "reserved nothing, so the second writer won the column"
    )
    assert row["agent_backend"] == "codex"
    assert row["agent_variant"] == "codex"
    assert row["model"] == "gpt-5.5-codex", (
        f"a same-backend bind reset the session's pinned model to {row['model']!r}"
    )
    assert row["reasoning_effort"] == "xhigh"
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert set(explicit_override_names(stored_metadata)) == {"model", "reasoning_effort"}


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
        # Native-bind UPDATE. Its ``values`` dict carries status / timestamps /
        # native_session_id / agent identity and NONE of the four route columns --
        # not model / reasoning_effort, and not agent_backend / agent_variant
        # either (any backend relabel on this path happens inside
        # ``_find_agent_session_row_id``, which is detected separately). The
        # ``model=None`` it passes goes to ``get_or_create_agent_session_row``,
        # i.e. the INSERT path above. Detected only by the ``**values`` heuristic.
        "bind_agent_session": "native bind: the UPDATE sets none of the route columns",
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
