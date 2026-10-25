"""One scope grammar for every CLI list of Session-owned records.

An Agent reads its own Session by default (``AVIBE_SESSION_ID``); ``--session-id``
names another Session and ``--global`` reads every Session. Outside an Agent
shell there is no default, so the command asks for one of the two.
"""

from __future__ import annotations

import json
import sys

import pytest

from config import paths
from core.show_pages import ShowPageStore
from storage.background import SQLiteBackgroundTaskStore
from vibe import cli, internal_client

MINE = "ses-mine"
OTHER = "ses-other"
CREATED_AT = "2026-08-12T00:00:00+00:00"


def _definition_sessions(payload: dict) -> set[str]:
    return {item["session_id"] for item in payload["definitions"]}


def _harness_sessions(payload: dict) -> set[str]:
    return {
        row["session_id"]
        for section in ("runs", "watches", "tasks", "runtime_agents")
        for row in payload[section]
    }


# (argv, sessions in the payload). Each command reads a different store, so each
# one is a separate wiring of the shared scope.
COMMANDS = {
    "task list": (["task", "list"], _definition_sessions),
    "watch list": (["watch", "list"], _definition_sessions),
    "runs list": (["runs", "list"], lambda payload: {run["session_id"] for run in payload["runs"]}),
    "harness status": (["harness", "status"], _harness_sessions),
    "show list": (["show", "list", "--json"], lambda payload: {page["session_id"] for page in payload["pages"]}),
}


@pytest.fixture
def two_sessions(monkeypatch, tmp_path):
    """Each Session-owned record kind, once in this Session and once in another."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        for session_id in (MINE, OTHER):
            store.upsert_watch(
                {
                    "id": f"watch-{session_id}",
                    "name": f"watch {session_id}",
                    "session_id": session_id,
                    "shell_command": "true",
                    "enabled": True,
                    "created_at": CREATED_AT,
                    "updated_at": CREATED_AT,
                }
            )
            store.upsert_scheduled_task(
                {
                    "id": f"task-{session_id}",
                    "name": f"task {session_id}",
                    "session_id": session_id,
                    "schedule_type": "at",
                    "run_at": "2099-01-01T00:00:00+00:00",
                    "timezone": "UTC",
                    "enabled": True,
                    "created_at": CREATED_AT,
                    "updated_at": CREATED_AT,
                }
            )
            store.enqueue_run(
                {
                    "id": f"run-{session_id}",
                    "request_type": "agent_run",
                    "status": "queued",
                    "agent_name": "worker",
                    "agent_backend": "codex",
                    "session_id": session_id,
                    "message": "continue",
                    "created_at": CREATED_AT,
                    "updated_at": CREATED_AT,
                }
            )
    finally:
        store.close()
    show_store = ShowPageStore()
    try:
        show_store.ensure(MINE)
        show_store.ensure(OTHER)
    finally:
        show_store.close()

    async def _controller_snapshot(*, run_ids):
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "agents": [
                    {"backend": "codex", "state": "active", "session_id": session_id, "workdir": f"/work/{session_id}"}
                    for session_id in (MINE, OTHER)
                ],
                "owned_run_ids": [],
                "ownership_available": True,
            },
        }

    monkeypatch.setattr(internal_client, "list_running_agents", _controller_snapshot)


def _run_cli(monkeypatch, capsys, argv: list[str]) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    captured = capsys.readouterr()
    return exc.value.code, json.loads(captured.out or captured.err)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    ("scope_args", "expected_sessions", "expected_scope"),
    (
        ([], {MINE}, {"session_id": MINE, "global": False}),
        (["--session-id", OTHER], {OTHER}, {"session_id": OTHER, "global": False}),
        (["--global"], {MINE, OTHER}, {"session_id": None, "global": True}),
    ),
    ids=("caller", "session-id", "global"),
)
def test_session_owned_lists_default_to_the_caller_session(
    two_sessions, monkeypatch, capsys, command, scope_args, expected_sessions, expected_scope
) -> None:
    argv, sessions_of = COMMANDS[command]
    monkeypatch.setenv("AVIBE_SESSION_ID", MINE)

    code, payload = _run_cli(monkeypatch, capsys, [*argv, *scope_args])

    assert code == 0
    assert sessions_of(payload) == expected_sessions
    assert {key: payload[key] for key in ("session_id", "global")} == expected_scope


@pytest.mark.parametrize("command", COMMANDS)
def test_session_owned_lists_need_a_scope_outside_an_agent_shell(
    two_sessions, monkeypatch, capsys, command
) -> None:
    argv, _ = COMMANDS[command]

    code, payload = _run_cli(monkeypatch, capsys, argv)

    assert code == 1
    assert payload["code"] == "missing_caller_session"
    assert "--session-id <id>" in payload["hint"] and "--global" in payload["hint"]


@pytest.mark.parametrize("command", COMMANDS)
def test_session_id_and_global_are_one_choice(monkeypatch, capsys, command) -> None:
    argv, _ = COMMANDS[command]

    code, payload = _run_cli(monkeypatch, capsys, [*argv, "--session-id", OTHER, "--global"])

    assert code == 2
    assert payload["code"] == "invalid_arguments"


def test_a_session_lists_the_runs_it_dispatched(monkeypatch, tmp_path, capsys) -> None:
    """A delegated Run is the dispatching Session's work, with or without a callback."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        for run_id, fields in (
            ("run-here", {"session_id": MINE}),
            ("run-reporting-back", {"session_id": "ses-child", "callback_session_id": MINE}),
            (
                "run-dispatched-no-callback",
                {"session_id": "ses-child-2", "source_kind": "agent", "source_actor": MINE},
            ),
            ("run-elsewhere", {"session_id": OTHER, "source_kind": "agent", "source_actor": OTHER}),
            # A callback Run's actor is the Run it reports, never a Session.
            ("run-callback-actor", {"session_id": OTHER, "source_kind": "callback", "source_actor": MINE}),
        ):
            store.enqueue_run(
                {
                    "id": run_id,
                    "request_type": "agent_run",
                    "status": "queued",
                    "message": "continue",
                    "created_at": CREATED_AT,
                    "updated_at": CREATED_AT,
                    **fields,
                }
            )
    finally:
        store.close()
    monkeypatch.setenv("AVIBE_SESSION_ID", MINE)

    code, payload = _run_cli(monkeypatch, capsys, ["runs", "list"])

    assert code == 0
    assert {run["id"] for run in payload["runs"]} == {
        "run-here",
        "run-reporting-back",
        "run-dispatched-no-callback",
    }


@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        (["watch", "list", "--limit", "1"], "vibe watch list --page 2 --limit 1"),
        (["watch", "list", "--session-id", OTHER, "--limit", "1"], f"vibe watch list --session-id {OTHER} --page 2 --limit 1"),
        (["runs", "list", "--global", "--limit", "1"], "vibe runs list --global --page 2 --limit 1"),
    ),
)
def test_next_page_repeats_the_scope_the_caller_chose(two_sessions, monkeypatch, capsys, argv, expected) -> None:
    """The next page reads the same Sessions, and an implicit caller scope stays implicit."""

    store = SQLiteBackgroundTaskStore()
    try:
        store.upsert_watch(
            {
                "id": "watch-other-2",
                "name": "second",
                "session_id": OTHER,
                "shell_command": "true",
                "enabled": True,
                "created_at": CREATED_AT,
                "updated_at": CREATED_AT,
            }
        )
        store.upsert_watch(
            {
                "id": "watch-mine-2",
                "name": "second",
                "session_id": MINE,
                "shell_command": "true",
                "enabled": True,
                "created_at": CREATED_AT,
                "updated_at": CREATED_AT,
            }
        )
    finally:
        store.close()
    monkeypatch.setenv("AVIBE_SESSION_ID", MINE)

    code, payload = _run_cli(monkeypatch, capsys, argv)

    assert code == 0
    assert payload["pagination"]["next_command"] == expected
