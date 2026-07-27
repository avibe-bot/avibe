"""Snapshot test for ``vibe agent run --json`` output schema.

C2 of the services-layer refactor moves CLI's session reservation off
``storage.sessions_service.SQLiteSessionsService`` and onto
``core.services.sessions``. The CLI's JSON output shape is the public
contract callers (and scheduled tasks, watch hooks, downstream tools)
depend on — Q8 in the design doc commits to keeping it byte-stable
through the refactor.

This test pins the **keys** in the ``agent_run`` envelope. Values like
run ids and session ids are non-deterministic so we check key presence /
types instead of exact strings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vibe.cli as cli


def _parse_agent_run(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["agent", "run", *argv])


@pytest.fixture(autouse=True)
def _clear_caller_env(monkeypatch):
    for key in (
        "AVIBE_SESSION_ID",
        "AVIBE_RUN_ID",
        "AVIBE_CALLER_SOURCE",
        "AVIBE_CALLER_BACKEND",
        "AVIBE_NATIVE_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)


_EXPECTED_KEYS = {
    "schema_version",
    "ok",
    "kind",
    "accepted",
    "request_type",
    "run_id",
    "execution_id",
    "agent",
    "session_policy",
    "session_id",
    "deliver_key",
    "scope_id",
    "visibility",
    "callback_session_id",
    "caller_context",
    "callback_notice",
    "async",
    "run",
}

_EXPECTED_RUN_KEYS_QUEUED = {
    "id",
    "status",
    "run_type",
    "agent_name",
    "session_id",
    "scope_id",
    "visibility",
    "callback_session_id",
    "source_kind",
    "source_actor",
    "parent_run_id",
}


def test_agent_run_default_async_envelope_schema(tmp_path: Path, capsys) -> None:
    """Locks the top-level keys + nested ``run`` keys for the default async path
    (the synchronous path adds the resolved result fields after
    ``_wait_for_run_result``, so they're tested via existing wait-flow
    coverage).
    """

    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--no-callback", "--message", "hi"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="slack"),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    # Envelope shape — these are the public keys that downstream tooling
    # parses; any rename / drop is a breaking change.
    assert set(payload.keys()) == _EXPECTED_KEYS, (
        f"agent_run envelope keys drifted: {set(payload.keys()) ^ _EXPECTED_KEYS}"
    )
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["kind"] == "agent_run"
    assert payload["async"] is True
    assert payload["request_type"] == "agent_run"
    assert payload["callback_session_id"] is None
    assert payload["caller_context"] is None
    assert payload["callback_notice"]["code"] == "async_run_without_callback"

    run = payload["run"]
    assert set(run.keys()) == _EXPECTED_RUN_KEYS_QUEUED, (
        f"run sub-payload keys drifted: {set(run.keys()) ^ _EXPECTED_RUN_KEYS_QUEUED}"
    )
    assert run["status"] == "queued"
    assert run["run_type"] == "agent_run"
    assert run["agent_name"] == "worker"
    assert run["id"] == payload["run_id"]
    assert run["source_kind"] == "cli"
    assert run["source_actor"] is None
    assert run["parent_run_id"] is None


def test_agent_run_explicit_async_flag_remains_compatible(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--no-callback", "--message", "hi"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="slack"),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["async"] is True
    assert payload["run"]["status"] == "queued"


def test_agent_run_async_accepts_callback_session_id(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_callback",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            callback_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="worker",
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--agent",
                "worker",
                "--callback-session-id",
                callback_session["id"],
                "--message",
                "hi",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["callback_session_id"] == callback_session["id"]
    assert payload["run"]["callback_session_id"] == callback_session["id"]
    stored = request_store.get_run(payload["run_id"])
    assert stored is not None
    assert stored["callback_session_id"] == callback_session["id"]
    assert stored["callback_status"] == "pending"


def test_agent_run_async_requires_callback_or_no_callback_without_caller(tmp_path: Path, capsys) -> None:
    from sqlalchemy import func, select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions

    db_path = tmp_path / "state" / "vibe.sqlite"
    with patch.dict("os.environ", {"AVIBE_HOME": str(tmp_path)}):
        ensure_sqlite_state()
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--message", "hi"])

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

        engine = create_sqlite_engine(db_path)
        with engine.connect() as conn:
            session_count = conn.execute(select(func.count()).select_from(agent_sessions)).scalar_one()

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    assert payload["code"] == "missing_async_callback"
    assert "--callback-session-id" in payload["hint"]
    assert "--no-callback" in payload["hint"]
    assert session_count == 0


def test_agent_run_async_defaults_callback_from_caller_env(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_caller",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            caller_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="caller",
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--message", "hi"])

        with (
            patch.dict(
                "os.environ",
                {
                    "AVIBE_SESSION_ID": caller_session["id"],
                    "AVIBE_RUN_ID": "run-parent",
                    "AVIBE_CALLER_SOURCE": "agent_run",
                    "AVIBE_CALLER_BACKEND": "codex",
                    "AVIBE_NATIVE_SESSION_ID": "thread-caller",
                },
            ),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["callback_session_id"] == caller_session["id"]
    assert payload["caller_context"] == {
        "session_id": caller_session["id"],
        "run_id": "run-parent",
        "source": "agent_run",
        "backend": "codex",
        "native_session_id": "thread-caller",
    }
    assert payload["callback_notice"]["code"] == "callback_defaulted_to_caller_session"
    assert payload["run"]["source_kind"] == "agent"
    assert payload["run"]["source_actor"] == caller_session["id"]
    assert payload["run"]["parent_run_id"] == "run-parent"
    stored = request_store.get_run(payload["run_id"])
    assert stored is not None
    assert stored["callback_session_id"] == caller_session["id"]
    assert stored["source_kind"] == "agent"
    assert stored["source_actor"] == caller_session["id"]
    assert stored["parent_run_id"] == "run-parent"
    assert stored["metadata"]["caller_context"]["session_id"] == caller_session["id"]


def test_agent_run_async_self_target_defaults_to_no_callback(
    tmp_path: Path, capsys
) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_caller",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            caller_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="caller",
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="caller", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--session-id", caller_session["id"], "--message", "hi"])

        with (
            patch.dict(
                "os.environ",
                {
                    "AVIBE_SESSION_ID": caller_session["id"],
                    "AVIBE_RUN_ID": "run-parent",
                    "AVIBE_CALLER_SOURCE": "agent_run",
                    "AVIBE_CALLER_BACKEND": "codex",
                },
            ),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["callback_session_id"] is None
    assert payload["callback_notice"]["code"] == "async_self_run_without_callback"
    stored = request_store.get_run(payload["run_id"])
    assert stored is not None
    assert stored["callback_session_id"] is None
    assert stored["callback_status"] is None


def test_agent_run_sync_self_target_detach_defaults_to_no_callback(
    tmp_path: Path, capsys
) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_caller",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            caller_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="caller",
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="caller", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--session-id", caller_session["id"], "--sync", "--message", "hi"])

        with (
            patch.dict(
                "os.environ",
                {
                    "AVIBE_SESSION_ID": caller_session["id"],
                    "AVIBE_RUN_ID": "run-parent",
                    "AVIBE_CALLER_SOURCE": "agent_run",
                    "AVIBE_CALLER_BACKEND": "codex",
                },
            ),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch(
                "vibe.cli._wait_for_run_result",
                return_value={
                    "id": "still-running",
                    "status": "running",
                    "wait_state": "detached",
                    "handoff_reason": "wait_limit_reached",
                },
            ),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["callback_session_id"] is None
    assert payload["callback_notice"]["code"] == "async_self_run_without_callback"
    assert payload["run"].get("callback_status") is None
    stored = request_store.get_run(payload["run_id"])
    assert stored is not None
    assert stored["callback_session_id"] is None
    assert stored["callback_status"] is None


def test_agent_run_callback_session_records_sync_route(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_callback_sync",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            callback_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="worker",
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--sync", "--callback-session-id", callback_session["id"], "--message", "hi"])

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
            patch("vibe.cli._wait_for_run_result", return_value={"id": "done", "status": "succeeded"}),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["callback_session_id"] == callback_session["id"]
    stored = request_store.get_run(payload["run_id"])
    assert stored is not None
    assert stored["callback_session_id"] == callback_session["id"]
    assert stored["callback_status"] is None


def test_agent_run_sync_waits_for_run_result(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--sync", "--no-callback", "--message", "hi"])

    wait_calls: list[tuple[str, float | None]] = []

    def _wait_for_result(_store, run_id: str, *, wait_timeout: float | None) -> dict:
        wait_calls.append((run_id, wait_timeout))
        return {"id": "done", "status": "succeeded"}

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="slack"),
        patch("vibe.cli._wait_for_run_result", side_effect=_wait_for_result),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["async"] is False
    assert payload["run"] == {"id": "done", "status": "succeeded"}
    assert wait_calls == [(payload["run_id"], None)]


def test_agent_run_sync_wait_timeout_is_passed_to_waiter(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(
        ["--agent", "worker", "--sync", "--wait-timeout", "2.5", "--no-callback", "--message", "hi"]
    )
    wait_timeouts: list[float | None] = []

    def _wait_for_result(_store, _run_id: str, *, wait_timeout: float | None) -> dict:
        wait_timeouts.append(wait_timeout)
        return {"id": "done", "status": "succeeded"}

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="slack"),
        patch("vibe.cli._wait_for_run_result", side_effect=_wait_for_result),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["async"] is False
    assert wait_timeouts == [2.5]


def test_agent_run_callback_session_records_sync_route_in_sqlite_store(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_runs, scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        with create_sqlite_engine(db_path).begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C123",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            callback_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="worker",
            )

        args = _parse_agent_run(
            ["--agent", "worker", "--sync", "--callback-session-id", callback_session["id"], "--message", "hi"]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli.paths.get_state_dir", return_value=state_home / "state"),
            patch("vibe.cli._primary_platform", return_value="slack"),
            patch("vibe.cli._wait_for_run_result", return_value={"id": "done", "status": "succeeded"}),
        ):
            result = cli.cmd_agent_run(args)

        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        with create_sqlite_engine(db_path).connect() as conn:
            row = conn.execute(
                select(agent_runs.c.callback_session_id, agent_runs.c.callback_status)
                .where(agent_runs.c.id == payload["run_id"])
                .limit(1)
            ).mappings().one()
        assert row["callback_session_id"] == callback_session["id"]
        assert row["callback_status"] is None
        assert cli.TaskExecutionStore().list_pending_callbacks() == []


def test_agent_run_sync_detach_marks_callback_pending(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_runs, scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        with create_sqlite_engine(db_path).begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C123",
                now="2026-06-10T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-10T00:00:00Z",
                    updated_at="2026-06-10T00:00:00Z",
                )
            )
            callback_session = sessions_service.create_session(
                conn,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="worker",
            )

        args = _parse_agent_run(
            ["--agent", "worker", "--sync", "--callback-session-id", callback_session["id"], "--message", "hi"]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli.paths.get_state_dir", return_value=state_home / "state"),
            patch("vibe.cli._primary_platform", return_value="slack"),
            patch(
                "vibe.cli._wait_for_run_result",
                return_value={
                    "id": "still-running",
                    "status": "running",
                    "wait_state": "detached",
                    "handoff_reason": "wait_limit_reached",
                },
            ),
        ):
            result = cli.cmd_agent_run(args)

        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["run"]["callback_status"] == "pending"
        with create_sqlite_engine(db_path).connect() as conn:
            row = conn.execute(
                select(agent_runs.c.callback_status, agent_runs.c.callback_completed_at)
                .where(agent_runs.c.id == payload["run_id"])
                .limit(1)
            ).mappings().one()
        assert row["callback_status"] == "pending"
        assert row["callback_completed_at"] is None


def test_agent_run_callback_conflict_does_not_reserve_session(tmp_path: Path, capsys) -> None:
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from sqlalchemy import select, func

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        args = _parse_agent_run(
            [
                "--agent",
                "worker",
                "--async",
                "--callback-session-id",
                "ses-caller",
                "--no-callback",
                "--message",
                "hi",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

        engine = create_sqlite_engine(db_path)
        with engine.connect() as conn:
            session_count = conn.execute(select(func.count()).select_from(agent_sessions)).scalar_one()

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "conflicting_callback_policy"
    assert session_count == 0


def _read_session_workdir(db_path: Path, session_id: str):
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    engine = create_sqlite_engine(db_path)
    with engine.connect() as conn:
        return conn.execute(
            select(agent_sessions.c.workdir).where(agent_sessions.c.id == session_id)
        ).scalar_one()


def _seed_bound_session(db_path: Path, tmp_path: Path) -> str:
    from storage.agent_session_rows import create_agent_session_row
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_fork_cli",
                now="2026-06-16T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name="worker",
                    agent_backend="codex",
                    agent_variant="codex",
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                )
            )
            return create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor=None,
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-worker",
                agent_name="worker",
                model="gpt-5",
                reasoning_effort="medium",
                workdir=str(tmp_path),
                native_session_id="thread-source",
            )
    finally:
        engine.dispose()


def test_agent_run_fork_session_reserves_new_session_and_persists_metadata(tmp_path: Path, capsys) -> None:
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        source_session_id = _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="reviewer", backend="codex", model="gpt-5.1", reasoning_effort="high")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--fork-session",
                source_session_id,
                "--agent",
                "reviewer",
                "--model",
                "gpt-5.2",
                "--reasoning-effort",
                "low",
                "--async",
                "--no-callback",
                "--message",
                "continue from here",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_policy"] == "fork"
    assert payload["forked_from_session_id"] == source_session_id
    assert payload["session_id"] != source_session_id

    run = request_store.get_run(payload["run_id"])
    assert run is not None
    assert run["metadata"]["session_fork"]["source_native_session_id"] == "thread-source"
    assert run["model"] == "gpt-5.2"
    assert run["reasoning_effort"] == "low"

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == payload["session_id"])
            ).mappings().one()
    finally:
        engine.dispose()
    assert row["agent_name"] == "reviewer"
    assert row["agent_variant"] == "codex"
    assert row["native_session_id"] == ""
    assert row["model"] == "gpt-5.2"
    assert row["reasoning_effort"] == "low"
    assert row["scope_id"] == "avibe::project::proj_fork_cli"
    assert row["session_anchor"] == payload["session_id"]


def test_agent_run_fork_self_uses_caller_session_and_inherits_scope(tmp_path: Path, capsys) -> None:
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        source_session_id = _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="reviewer", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--fork-self",
                "--agent",
                "reviewer",
                "--async",
                "--no-callback",
                "--message",
                "continue from here",
            ]
        )

        with (
            patch.dict("os.environ", {"AVIBE_SESSION_ID": source_session_id}),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["forked_from_session_id"] == source_session_id
    assert payload["scope_id"] == "avibe::project::proj_fork_cli"

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == payload["session_id"])
            ).mappings().one()
    finally:
        engine.dispose()
    assert row["scope_id"] == "avibe::project::proj_fork_cli"
    assert row["workdir"] == str(tmp_path)


def test_agent_run_create_same_scope_snapshots_scope_workdir(tmp_path: Path, capsys, monkeypatch) -> None:
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        caller_session_id = _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--same-scope", "--async", "--no-callback", "--message", "hi"])
        monkeypatch.chdir(invoke_dir)

        with (
            patch.dict("os.environ", {"AVIBE_SESSION_ID": caller_session_id}),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope_id"] == "avibe::project::proj_fork_cli"

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == payload["session_id"])
            ).mappings().one()
    finally:
        engine.dispose()
    assert row["scope_id"] == "avibe::project::proj_fork_cli"
    assert row["workdir"] == str(tmp_path)


def test_agent_run_same_scope_rejects_standalone_caller(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        with create_sqlite_engine(db_path).begin() as conn:
            caller_session_id = sessions_service.create_session(
                conn,
                scope_id=None,
                agent_backend="codex",
                agent_name="worker",
            )["id"]
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            ["--agent", "worker", "--same-scope", "--async", "--no-callback", "--message", "hi"]
        )

        with (
            patch.dict("os.environ", {"AVIBE_SESSION_ID": caller_session_id}),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "standalone_session_has_no_scope"
    assert payload["details"]["session_id"] == caller_session_id


def test_agent_run_same_scope_rejects_standalone_fork_source(monkeypatch) -> None:
    args = _parse_agent_run(
        [
            "--agent",
            "worker",
            "--fork-session",
            "ses-standalone",
            "--same-scope",
            "--async",
            "--no-callback",
            "--message",
            "hi",
        ]
    )
    monkeypatch.setattr(cli, "_scope_id_from_session_id", lambda *_args, **_kwargs: None)

    with pytest.raises(cli.TaskCliError) as exc_info:
        cli._resolve_agent_run_scope_key(
            args,
            caller_context=None,
            source_session_id="ses-standalone",
        )

    assert exc_info.value.code == "standalone_session_has_no_scope"


def test_agent_run_fork_defaults_to_source_scope_not_caller_scope(monkeypatch) -> None:
    args = _parse_agent_run(
        [
            "--agent",
            "worker",
            "--fork-session",
            "ses-source",
            "--async",
            "--no-callback",
            "--message",
            "hi",
        ]
    )
    resolved: list[str] = []
    monkeypatch.setattr(
        cli,
        "_scope_id_from_session_id",
        lambda session_id, **_kwargs: resolved.append(session_id) or "avibe::project::proj_caller",
    )

    scope_id = cli._resolve_agent_run_scope_key(
        args,
        caller_context=SimpleNamespace(session_id="ses-caller"),
        source_session_id="ses-source",
    )

    # None is intentional: reserve_forked_session interprets it as inherit the
    # source scope. Reaching the caller resolver here would override placement.
    assert scope_id is None
    assert resolved == []


def test_agent_run_create_scope_id_snapshots_scope_workdir(tmp_path: Path, capsys, monkeypatch) -> None:
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--agent",
                "worker",
                "--scope-id",
                "avibe::project::proj_fork_cli",
                "--async",
                "--no-callback",
                "--message",
                "hi",
            ]
        )
        monkeypatch.chdir(invoke_dir)

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope_id"] == "avibe::project::proj_fork_cli"

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == payload["session_id"])
            ).mappings().one()
    finally:
        engine.dispose()
    assert row["scope_id"] == "avibe::project::proj_fork_cli"
    assert row["workdir"] == str(tmp_path)


def test_agent_run_create_scope_id_requires_existing_scope(tmp_path: Path, capsys) -> None:
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--agent",
                "worker",
                "--scope-id",
                "avibe::project::missing",
                "--async",
                "--no-callback",
                "--message",
                "hi",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "scope_not_found"


def test_agent_run_create_scope_id_rejects_archived_project(tmp_path: Path, capsys) -> None:
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        with create_sqlite_engine(db_path).begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_archived",
                now="2026-06-16T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=0,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name="worker",
                    agent_backend="codex",
                    agent_variant="codex",
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                )
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--agent",
                "worker",
                "--scope-id",
                scope_id,
                "--async",
                "--no-callback",
                "--message",
                "hi",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "scope_archived"


def test_agent_run_fork_rejects_cross_backend_agent(tmp_path: Path, capsys) -> None:
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        source_session_id = _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="claude-worker", backend="claude")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            [
                "--fork-session",
                source_session_id,
                "--agent",
                "claude-worker",
                "--async",
                "--no-callback",
                "--message",
                "hi",
            ]
        )

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    assert payload["code"] == "session_fork_failed"


def test_agent_run_callerless_session_workdir_uses_show_workspace(tmp_path: Path, capsys, monkeypatch) -> None:
    """A caller-less run reserves a standalone Session in its Show workspace."""
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "repo"
    invoke_dir.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--async", "--no-callback", "--message", "hi"])
        monkeypatch.chdir(invoke_dir)

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    expected = state_home / "show" / payload["session_id"]
    assert _read_session_workdir(db_path, payload["session_id"]) == str(expected)
    assert expected.is_dir()


def test_agent_run_explicit_cwd_wins(tmp_path: Path, capsys, monkeypatch) -> None:
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "repo"
    invoke_dir.mkdir()
    picked_dir = tmp_path / "elsewhere"
    picked_dir.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            ["--agent", "worker", "--cwd", str(picked_dir), "--async", "--no-callback", "--message", "hi"]
        )
        monkeypatch.chdir(invoke_dir)

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert _read_session_workdir(db_path, payload["session_id"]) == str(picked_dir)


def test_agent_run_cwd_must_exist(tmp_path: Path, capsys) -> None:
    args = _parse_agent_run(
        ["--agent", "worker", "--create-session", "--cwd", str(tmp_path / "missing"), "--async", "--message", "hi"]
    )

    result = cli.cmd_agent_run(args)

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    assert payload["code"] == "cwd_not_found"


def test_agent_run_cwd_rejected_with_existing_session(capsys) -> None:
    """An existing session keeps its own workdir — --cwd cannot re-route it."""

    args = _parse_agent_run(["--session-id", "ses123", "--cwd", "/tmp", "--async", "--message", "hi"])

    result = cli.cmd_agent_run(args)

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    assert payload["code"] == "cwd_with_existing_session"


def test_resolve_run_cwd_defaults_by_session_target(monkeypatch, tmp_path: Path) -> None:
    """Caller placement uses its cwd; standalone/scoped reservations derive theirs."""

    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(cwd=None)
    assert cli._resolve_run_cwd(args, session_policy="create", help_command="x") is None
    assert (
        cli._resolve_run_cwd(
            args,
            session_policy="create",
            invocation_cwd_default=True,
            help_command="x",
        )
        == str(tmp_path)
    )
    assert cli._resolve_run_cwd(args, session_policy="create", scoped_session=True, help_command="x") is None

    args = SimpleNamespace(cwd=str(tmp_path), deliver_key="slack::channel::C123")
    assert cli._resolve_run_cwd(args, session_policy="create", scoped_session=True, help_command="x") == str(tmp_path)


def test_runs_list_current_session_filters_from_caller_env(tmp_path: Path, capsys) -> None:
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    request_store.enqueue_agent_run(
        message="current",
        agent_name="worker",
        session_id="ses-current",
    )
    request_store.enqueue_agent_run(
        message="other",
        agent_name="worker",
        session_id="ses-other",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["runs", "list", "--current-session"])

    with (
        patch.dict("os.environ", {"AVIBE_SESSION_ID": "ses-current"}),
        patch("vibe.cli._task_request_store", return_value=request_store),
    ):
        result = cli.cmd_runs_list(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert [run["session_id"] for run in payload["runs"]] == ["ses-current"]


def test_runs_list_current_session_conflicts_with_session_id(capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["runs", "list", "--current-session", "--session-id", "ses-explicit"])

    result = cli.cmd_runs_list(args)

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "conflicting_session_filter"
