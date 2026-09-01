from __future__ import annotations

import argparse
import json
import re
import shlex

from config import paths
from core.system_prompt_injection import build_system_prompt_injection
from modules.im.base import MessageContext
from storage.background import SQLiteBackgroundTaskStore
from vibe import cli


def _command_parser(path: tuple[str, ...]):
    parser = cli.build_parser()
    for segment in path:
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        parser = subparsers.choices[segment]
    return parser


def _parser_flags(parser) -> set[str]:
    return {flag for action in parser._actions for flag in action.option_strings}


def _iter_command_parsers(parser, path=()):
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            child_path = (*path, name)
            yield child_path, child
            yield from _iter_command_parsers(child, child_path)


def _prompt_command_parser(command: str):
    tokens = shlex.split(command)
    assert tokens[0] == "vibe"
    parser = cli.build_parser()
    path: list[str] = []
    for token in tokens[1:]:
        subparsers = next(
            (
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if subparsers is None or token not in subparsers.choices:
            break
        parser = subparsers.choices[token]
        path.append(token)
    return parser, tuple(path)


def test_all_collection_commands_share_bounded_pagination_contract() -> None:
    collection_commands = {
        ("agent", "list"),
        ("agent", "models"),
        ("runs", "list"),
        ("memory", "list"),
        ("session", "list"),
        ("session", "queue", "list"),
        ("vault", "list"),
        ("vault", "find"),
        ("vault", "tags"),
        ("show", "list"),
        ("show", "marks"),
        ("data", "query"),
        ("task", "list"),
        ("watch", "list"),
    }
    fixed_page_commands = {("skill", "list")}
    parser = cli.build_parser()
    discovered_list_commands = {
        path
        for path, _ in _iter_command_parsers(parser)
        if path[-1] == "list" and path[-1] != "ls"
    }

    assert discovered_list_commands <= collection_commands | fixed_page_commands
    for path in collection_commands:
        flags = _parser_flags(_command_parser(path))
        assert "--page" in flags, path
        assert "--limit" in flags, path
        assert "--all" not in flags, path
    for path in fixed_page_commands:
        flags = _parser_flags(_command_parser(path))
        assert "--page" in flags, path
        assert "--limit" not in flags, path
        assert "--all" not in flags, path


def test_injected_vibe_commands_only_use_parser_supported_flags() -> None:
    context = MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-test"},
    )
    prompt = build_system_prompt_injection(context=context)
    commands = re.findall(r"`(vibe [^`\n]+)`", prompt)
    checked_commands = 0

    for command in commands:
        mentioned_flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", command))
        if not mentioned_flags:
            continue
        parser, path = _prompt_command_parser(command)
        unsupported_flags = mentioned_flags - _parser_flags(parser)
        assert not unsupported_flags, (
            f"Injected command {command!r} uses flags unsupported by "
            f"{' '.join(('vibe', *path))}: {sorted(unsupported_flags)}"
        )
        checked_commands += 1

    assert checked_commands >= 20


def test_injected_session_queue_commands_name_real_subcommands() -> None:
    context = MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-test"},
    )
    prompt = build_system_prompt_injection(context=context)

    for command, expected_path in (
        (
            "vibe session queue list <session-id>",
            ("session", "queue", "list"),
        ),
        (
            "vibe session queue remove <session-id> <message-id>",
            ("session", "queue", "remove"),
        ),
    ):
        assert f"`{command}`" in prompt
        _, path = _prompt_command_parser(command)
        assert path == expected_path


def test_runs_list_cli_defaults_to_first_page(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(25):
            store.enqueue_run(
                {
                    "id": f"run-{index:02d}",
                    "request_type": "agent_run",
                    "status": "succeeded",
                    "agent_name": "helper",
                    "agent_backend": "codex",
                    "session_id": "ses-alpha",
                    "message": f"message {index}",
                    "created_at": f"2026-05-25T00:{index:02d}:00+00:00",
                    "updated_at": f"2026-05-25T00:{index:02d}:00+00:00",
                }
            )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["runs", "list", "--brief"])
    assert cli.cmd_runs_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "agent_runs"
    assert len(payload["runs"]) == 20
    assert payload["runs"][0]["id"] == "run-24"
    assert payload["pagination"]["has_more"] is True
    assert payload["pagination"]["next_command"] == "vibe runs list --brief --page 2 --limit 20"
    assert "More records are available" in payload["message"]


def test_runs_list_cli_filters_status_and_query(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-success",
                "request_type": "agent_run",
                "status": "succeeded",
                "agent_name": "helper",
                "agent_backend": "codex",
                "session_id": "ses-alpha",
                "message": "weekly report",
                "created_at": "2026-05-25T00:00:00+00:00",
                "updated_at": "2026-05-25T00:00:00+00:00",
            }
        )
        store.enqueue_run(
            {
                "id": "run-failed",
                "request_type": "agent_run",
                "status": "failed",
                "agent_name": "helper",
                "agent_backend": "codex",
                "session_id": "ses-alpha",
                "message": "weekly report",
                "created_at": "2026-05-25T00:01:00+00:00",
                "updated_at": "2026-05-25T00:01:00+00:00",
            }
        )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["runs", "list", "--status", "succeeded", "--q", "weekly", "--brief"])
    assert cli.cmd_runs_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["runs"]] == ["run-success"]
    assert payload["pagination"]["has_more"] is False


def test_runs_list_cli_normalizes_offset_time_filters(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-before",
                "request_type": "agent_run",
                "status": "succeeded",
                "created_at": "2026-05-25T01:59:59+00:00",
                "updated_at": "2026-05-25T01:59:59+00:00",
            }
        )
        store.enqueue_run(
            {
                "id": "run-after",
                "request_type": "agent_run",
                "status": "succeeded",
                "created_at": "2026-05-25T02:00:00+00:00",
                "updated_at": "2026-05-25T02:00:00+00:00",
            }
        )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["runs", "list", "--created-after", "2026-05-25T10:00:00+08:00", "--brief"])
    assert cli.cmd_runs_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["runs"]] == ["run-after"]


def test_runs_list_cli_next_command_uses_absolute_time_filters(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(25):
            store.enqueue_run(
                {
                    "id": f"run-{index:02d}",
                    "request_type": "agent_run",
                    "status": "succeeded",
                    "created_at": f"2026-05-25T00:{index:02d}:00+00:00",
                    "updated_at": f"2026-05-25T00:{index:02d}:00+00:00",
                }
            )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["runs", "list", "--created-after", "2026-05-25T08:00:00+08:00", "--limit", "10"])
    assert cli.cmd_runs_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert "--created-after 2026-05-25T00:00:00+00:00" in payload["pagination"]["next_command"]
    assert "--created-after 2026-05-25T08:00:00+08:00" not in payload["pagination"]["next_command"]
    assert payload["pagination"]["next_command"].endswith("--page 2 --limit 10")
    assert "message" not in payload["runs"][0]


def test_runs_show_defaults_to_caller_run(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-alpha")
    monkeypatch.setenv("AVIBE_RUN_ID", "run-current")
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-current",
                "request_type": "agent_run",
                "status": "succeeded",
                "agent_name": "helper",
                "agent_backend": "codex",
                "session_id": "ses-alpha",
                "message": "current run",
                "created_at": "2026-05-25T00:00:00+00:00",
                "updated_at": "2026-05-25T00:00:00+00:00",
            }
        )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["runs", "show"])
    assert cli.cmd_runs_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == "run-current"
    assert payload["run_default_notice"] == {
        "code": "run_defaulted_to_caller",
        "message": "Run defaulted to the caller Run from AVIBE_RUN_ID.",
        "run_id": "run-current",
    }


def test_runs_show_attaches_live_session_owner_for_queued_run(monkeypatch, tmp_path, capsys) -> None:
    """HFR-002: CLI diagnosis names the authoritative live Session owner."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-queued",
                "request_type": "agent_run",
                "status": "queued",
                "agent_name": "helper",
                "agent_backend": "codex",
                "session_id": "ses-alpha",
                "message": "queued successor",
                "created_at": "2026-07-18T04:31:27+00:00",
                "updated_at": "2026-07-18T04:31:27+00:00",
            }
        )
    finally:
        store.close()

    async def _turn_state(session_id):
        assert session_id == "ses-alpha"
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "session_id": session_id,
                "in_flight": True,
                "owner": {
                    "source": "agent_run",
                    "run_id": "run-owner",
                    "acquired_at": "2026-07-18T04:31:26+00:00",
                    "native_turn_started": True,
                    "backend_alive": True,
                },
            },
        }

    monkeypatch.setattr("vibe.internal_client.turn_state", _turn_state)
    args = cli.build_parser().parse_args(["runs", "show", "run-queued"])

    assert cli.cmd_runs_show(args) == 0
    payload = json.loads(capsys.readouterr().out)
    runtime = payload["run"]["session_runtime"]
    assert runtime["available"] is True
    assert runtime["owner"]["run_id"] == "run-owner"
    assert runtime["owner"]["backend_alive"] is True


def test_runs_show_requires_run_without_caller(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    paths.ensure_data_dirs()

    args = cli.build_parser().parse_args(["runs", "show"])
    assert cli.cmd_runs_show(args) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "missing_run_target"
    assert payload["help_command"] == "vibe runs show --help"


def test_data_query_cli_runs_read_only_sql(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-1",
                "request_type": "agent_run",
                "status": "succeeded",
                "created_at": "2026-05-25T00:00:00+00:00",
                "updated_at": "2026-05-25T00:00:00+00:00",
            }
        )
    finally:
        store.close()

    args = cli.build_parser().parse_args(["data", "query", "--sql", "select id, status from agent_runs"])
    assert cli.cmd_data_query(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "data_query"
    assert payload["columns"] == ["id", "status"]
    assert payload["rows"] == [{"id": "run-1", "status": "succeeded"}]


def test_data_query_cli_omits_next_command_for_stdin_sql(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(25):
            store.enqueue_run(
                {
                    "id": f"run-{index:02d}",
                    "request_type": "agent_run",
                    "status": "succeeded",
                    "created_at": f"2026-05-25T00:{index:02d}:00+00:00",
                    "updated_at": f"2026-05-25T00:{index:02d}:00+00:00",
                }
            )
    finally:
        store.close()

    monkeypatch.setattr("sys.stdin", type("FakeStdin", (), {"read": lambda self: "select id from agent_runs order by id"})())
    args = cli.build_parser().parse_args(["data", "query", "--sql-file", "-", "--limit", "10"])
    assert cli.cmd_data_query(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["pagination"]["has_more"] is True
    assert "next_command" not in payload["pagination"]
    assert payload["message"] == "More records are available. Add --page to continue."


def test_data_query_cli_rejects_writes(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    store = SQLiteBackgroundTaskStore()
    store.close()

    args = cli.build_parser().parse_args(["data", "query", "--sql", "delete from agent_runs"])
    assert cli.cmd_data_query(args) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["help_command"] == "vibe data query --help"
