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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vibe.cli as cli


def _parse_agent_run(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["agent", "run", *argv])


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
    "callback_session_id",
    "async",
    "run",
}

_EXPECTED_RUN_KEYS_QUEUED = {"id", "status", "run_type", "agent_name", "session_id", "callback_session_id"}


def test_agent_run_async_envelope_schema(tmp_path: Path, capsys) -> None:
    """Locks the top-level keys + nested ``run`` keys for the async path
    (the synchronous path adds the resolved result fields after
    ``_wait_for_run_result``, so they're tested via existing wait-flow
    coverage).
    """

    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--message", "hi"])

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

    run = payload["run"]
    assert set(run.keys()) == _EXPECTED_RUN_KEYS_QUEUED, (
        f"run sub-payload keys drifted: {set(run.keys()) ^ _EXPECTED_RUN_KEYS_QUEUED}"
    )
    assert run["status"] == "queued"
    assert run["run_type"] == "agent_run"
    assert run["agent_name"] == "worker"
    assert run["id"] == payload["run_id"]


def test_agent_run_async_accepts_callback_session_id(tmp_path: Path, capsys) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"VIBE_REMOTE_HOME": str(state_home)}):
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
                "--async",
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


def test_agent_run_callback_session_requires_async(capsys) -> None:
    args = _parse_agent_run(["--agent", "worker", "--callback-session-id", "ses1", "--message", "hi"])

    result = cli.cmd_agent_run(args)

    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out or captured.err)
    assert payload["code"] == "callback_requires_async"


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
    with patch.dict("os.environ", {"VIBE_REMOTE_HOME": str(state_home)}):
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
    assert row["session_anchor"] == payload["session_id"]


def test_agent_run_fork_rejects_cross_backend_agent(tmp_path: Path, capsys) -> None:
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    with patch.dict("os.environ", {"VIBE_REMOTE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        source_session_id = _seed_bound_session(db_path, tmp_path)
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="claude-worker", backend="claude")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            ["--fork-session", source_session_id, "--agent", "claude-worker", "--async", "--message", "hi"]
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


def test_agent_run_private_session_workdir_follows_invocation_cwd(tmp_path: Path, capsys, monkeypatch) -> None:
    """A private (no --deliver-key) reservation snapshots the CLI invocation's
    cwd as the new session's workdir — like every other CLI tool — instead of
    leaving it blank and falling to the global default cwd at dispatch."""

    import os

    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "repo"
    invoke_dir.mkdir()
    with patch.dict("os.environ", {"VIBE_REMOTE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(["--agent", "worker", "--async", "--message", "hi"])
        monkeypatch.chdir(invoke_dir)
        expected = os.getcwd()

        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("vibe.cli._primary_platform", return_value="slack"),
        ):
            result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert _read_session_workdir(db_path, payload["session_id"]) == expected


def test_agent_run_explicit_cwd_wins(tmp_path: Path, capsys, monkeypatch) -> None:
    from storage.importer import ensure_sqlite_state

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "repo"
    invoke_dir.mkdir()
    picked_dir = tmp_path / "elsewhere"
    picked_dir.mkdir()
    with patch.dict("os.environ", {"VIBE_REMOTE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
        args = _parse_agent_run(
            ["--agent", "worker", "--cwd", str(picked_dir), "--async", "--message", "hi"]
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


def test_resolve_run_cwd_deliver_key_defers_to_scope(monkeypatch, tmp_path: Path) -> None:
    """Without --cwd, a --deliver-key reservation returns None so the target
    scope's configured workdir snapshot stays authoritative; an explicit --cwd
    still wins."""

    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(cwd=None, deliver_key="slack::channel::C123")
    assert cli._resolve_run_cwd(args, session_policy="create", help_command="x") is None
    args = SimpleNamespace(cwd=str(tmp_path), deliver_key="slack::channel::C123")
    assert cli._resolve_run_cwd(args, session_policy="create", help_command="x") == str(tmp_path)
