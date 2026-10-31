from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
from pathlib import Path

import pytest

import pytest

from config import paths
from core.system_prompt_injection import build_system_prompt_injection
from modules.im.base import MessageContext
from storage.background import SQLiteBackgroundTaskStore
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]


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
        ("model-requests", "list"),
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


def test_injected_and_builtin_skill_commands_only_use_parser_supported_flags() -> None:
    context = MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-test"},
    )
    prompt = build_system_prompt_injection(context=context)
    guidance = [prompt] + [
        (ROOT / "skills" / name / "SKILL.md").read_text()
        for name in ("use-avibe", "use-avibe-harness", "use-avibe-vault", "use-show-pages")
    ]
    commands = re.findall(r"`(vibe [^`\n]+)`", "\n".join(guidance))
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


def test_session_examples_in_guidance_parse_against_the_cli() -> None:
    """Every `vibe session ...` call the prompt or a built-in Skill teaches must parse.

    The flag check above cannot see a Session named positionally: `vibe session
    get <id>` mentions no flag. The session group names its Session only with
    `--session-id`, so an example that still passes it positionally fails here.
    """

    context = MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-test"},
    )
    guidance = [build_system_prompt_injection(context=context)] + [
        path.read_text() for path in sorted((ROOT / "skills").rglob("*.md"))
    ]
    examples = sorted(set(re.findall(r"`(vibe session [^`\n]+)`", "\n".join(guidance))))
    assert any("queue remove" in example for example in examples), examples

    parser = cli.build_parser()
    for example in examples:
        argv = shlex.split(re.sub(r"<[^>]+>|\.\.\.", "x", example))[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            raise AssertionError(f"guidance teaches an unparseable session command: {example!r}") from exc


@pytest.mark.parametrize(
    ("argv", "expected_limit", "error_code"),
    [
        (["--limit", "500"], 100, None),
        (["--limit", "100"], 100, None),
        (["--limit", "0"], None, "invalid_pagination"),
        (["--page", "0"], None, "invalid_pagination"),
    ],
)
def test_cli_limit_above_the_maximum_is_served_at_the_maximum(
    monkeypatch, tmp_path, capsys, argv, expected_limit, error_code
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    args = cli.build_parser().parse_args(["session", "list", *argv])

    result = cli.cmd_session_list(args)

    captured = capsys.readouterr()
    if error_code is None:
        assert result == 0
        assert json.loads(captured.out)["pagination"]["limit"] == expected_limit
    else:
        assert result == 1
        assert json.loads(captured.err)["code"] == error_code


@pytest.mark.parametrize(
    ("argv", "accepted"),
    [
        (["task", "show", "t1", "--brief"], True),
        (["watch", "show", "w1", "--brief"], True),
        (["runs", "show", "r1", "--brief"], True),
        (["session", "get", "--brief"], True),
        (["agent", "show", "worker", "--brief"], True),
        (["task", "list", "--brief"], False),
        (["watch", "list", "--brief"], False),
        (["runs", "list", "--brief"], False),
        (["agent", "list", "--brief"], False),
    ],
)
def test_brief_selects_the_list_row_on_show_and_is_not_a_list_flag(argv, accepted) -> None:
    """``--brief`` means "print this record's list row"; a list is already rows."""

    parser = cli.build_parser()
    if accepted:
        assert parser.parse_args(argv).brief is True
        return
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
        parser.parse_args(argv)


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

    args = cli.build_parser().parse_args(["runs", "list", "--global"])
    assert cli.cmd_runs_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "agent_runs"
    assert len(payload["runs"]) == 20
    assert payload["runs"][0]["id"] == "run-24"
    assert payload["pagination"]["has_more"] is True
    assert payload["pagination"]["next_command"] == "vibe runs list --global --page 2 --limit 20"
    # The continuation is next_command alone: no prose repeats it.
    assert "message" not in payload


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

    args = cli.build_parser().parse_args(["runs", "list", "--global", "--status", "succeeded", "--q", "weekly"])
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

    args = cli.build_parser().parse_args(["runs", "list", "--global", "--created-after", "2026-05-25T10:00:00+08:00"])
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

    args = cli.build_parser().parse_args(["runs", "list", "--global", "--created-after", "2026-05-25T08:00:00+08:00", "--limit", "10"])
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


def test_runs_show_prints_the_run_text_once_and_brief_prints_the_list_row(monkeypatch, tmp_path, capsys) -> None:
    """#2425: a long delegation brief appears once, as `message`; `--brief` is the status row `runs list` prints."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    delegation_brief = "Review PR #1 and report every finding. " * 100
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-done",
                "request_type": "agent_run",
                "status": "succeeded",
                "agent_name": "helper",
                "agent_backend": "codex",
                "session_id": "ses-alpha",
                "message": delegation_brief,
                "result_text": "No findings.",
                "created_at": "2026-05-25T00:00:00+00:00",
                "updated_at": "2026-05-25T00:00:00+00:00",
            }
        )
    finally:
        store.close()

    assert cli.cmd_runs_show(cli.build_parser().parse_args(["runs", "show", "run-done"])) == 0
    run = json.loads(capsys.readouterr().out)["run"]
    assert "prompt" not in run
    assert (run["message"], run["result_text"]) == (delegation_brief, "No findings.")

    assert cli.cmd_runs_show(cli.build_parser().parse_args(["runs", "show", "run-done", "--brief"])) == 0
    brief = json.loads(capsys.readouterr().out)["run"]
    assert cli.cmd_runs_list(cli.build_parser().parse_args(["runs", "list", "--global"])) == 0
    assert brief == json.loads(capsys.readouterr().out)["runs"][0]
    assert (brief["status"], "message" in brief) == ("succeeded", False)


def test_runs_show_prints_each_fact_once_without_delivery_bookkeeping(monkeypatch, tmp_path, capsys) -> None:
    """The full record keeps what an Agent acts on and drops the reply's ledger and internals."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    provenance = {"turn_id": "trn_1", "run_id": "run-delegate", "detached": False}
    store = SQLiteBackgroundTaskStore()
    try:
        store.enqueue_run(
            {
                "id": "run-delegate",
                "request_type": "agent_run",
                "status": "succeeded",
                "agent_name": "helper",
                "agent_id": "agent-internal-id",
                "agent_backend": "codex",
                "session_id": "ses-delegate",
                "source_kind": "agent",
                "source_actor": "ses-caller",
                "message": "Review PR #1.",
                "result_text": "No findings.",
                "result_payload": {
                    "outputs": [
                        {"id": "msg_1", "text": "Reviewing now.", "provenance": provenance},
                        {"id": "terminal", "text": "No findings.", "provenance": provenance},
                    ]
                },
                "message_ids": ["msg_1"],
                "pid": 4242,
                "created_at": "2026-05-25T00:00:00+00:00",
                "completed_at": "2026-05-25T00:05:00+00:00",
                "updated_at": "2026-05-25T00:05:00+00:00",
                "metadata": {
                    "caller_context": {"session_id": "ses-caller", "channel_id": "ses-caller"},
                    "delivery_intent": "steer",
                    "delivery_outcome": {"intent": "steer", "status": "claimed"},
                    "last_activity_at": "2026-05-25T00:04:00+00:00",
                    "turn_id": "trn_1",
                    "close_after": True,
                },
            }
        )
        # A callback names the Session its result came from only in metadata.
        store.enqueue_run(
            {
                "id": "run-callback",
                "request_type": "agent_run",
                "status": "queued",
                "session_id": "ses-caller",
                "source_kind": "callback",
                "source_actor": "run-delegate",
                "parent_run_id": "run-delegate",
                "message": "No findings.",
                "created_at": "2026-05-25T00:05:00+00:00",
                "metadata": {"source_session_id": "ses-delegate", "callback_parent_run_id": "run-delegate"},
            }
        )
    finally:
        store.close()

    assert cli.cmd_runs_show(cli.build_parser().parse_args(["runs", "show", "run-delegate"])) == 0
    out = capsys.readouterr().out
    run = json.loads(out)["run"]
    assert out.count("No findings.") == 1
    assert (run["result_text"], run["other_outputs"]) == ("No findings.", ["Reviewing now."])
    assert run["source_session_id"] == "ses-caller"
    assert run["last_activity_at"] == "2026-05-25T00:04:00+00:00"
    assert run["metadata"] == {"close_after": True}
    for omitted in (
        "result_payload", "message_ids", "pid", "ok", "cancel_requested",
        "agent_id", "updated_at", "source_actor",
    ):
        assert omitted not in run

    monkeypatch.setattr(cli, "_live_session_runtime_for_run", lambda run: None)
    assert cli.cmd_runs_show(cli.build_parser().parse_args(["runs", "show", "run-callback"])) == 0
    callback = json.loads(capsys.readouterr().out)["run"]
    assert (callback["source_session_id"], callback["parent_run_id"]) == ("ses-delegate", "run-delegate")
    assert "source_actor" not in callback and "metadata" not in callback


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


def test_data_query_text_prints_records_with_values_verbatim_and_pages(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    SQLiteBackgroundTaskStore().close()
    sql = (
        "select 'Line one' || char(10) || 'He said \"hi\"' as body, null as gone, 7 as n "
        "union all select 'second', '', 8 union all select 'third', 'x', 9"
    )

    args = cli.build_parser().parse_args(["data", "query", "--sql", sql, "--limit", "2", "--format", "text"])
    assert cli.cmd_data_query(args) == 0
    *records, next_line = capsys.readouterr().out.splitlines()
    assert records == [
        "-[ RECORD 1 ]",
        "body:",
        "  Line one",
        '  He said "hi"',
        "gone: (null)",
        "n: 7",
        "-[ RECORD 2 ]",
        "body: second",
        "gone:",
        "n: 8",
    ]
    label, next_command = next_line.split(": ", 1)
    assert label == "next_command"

    args = cli.build_parser().parse_args(shlex.split(next_command)[1:])
    assert cli.cmd_data_query(args) == 0
    assert capsys.readouterr().out.splitlines() == ["-[ RECORD 3 ]", "body: third", "gone: x", "n: 9"]


def test_data_query_cuts_long_values_and_says_how_to_read_them_whole(monkeypatch, tmp_path, capsys) -> None:
    """One message body can be hundreds of thousands of characters; a page of them must not fill a context."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    SQLiteBackgroundTaskStore().close()
    sql = "select 'short' as a, printf('%.3000c', 'x') as b union all select 'two', 'y'"

    args = cli.build_parser().parse_args(["data", "query", "--sql", sql])
    assert cli.cmd_data_query(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0] == {"a": "short", "b": "x" * 2000 + "…[+1000 chars]"}
    assert payload["rows"][1] == {"a": "two", "b": "y"}
    assert payload["truncated_values"] == 1
    assert "--max-chars 0" in payload["truncation"]

    args = cli.build_parser().parse_args(["data", "query", "--sql", sql, "--max-chars", "0"])
    assert cli.cmd_data_query(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["b"] == "x" * 3000
    assert "truncated_values" not in payload

    args = cli.build_parser().parse_args(
        ["data", "query", "--sql", sql, "--max-chars", "10", "--limit", "1", "--format", "text"]
    )
    assert cli.cmd_data_query(args) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "b: xxxxxxxxxx…[+2990 chars]" in lines
    assert lines[-2].startswith("1 value(s) cut at 10 characters")
    # The next page is read the same way.
    assert lines[-1].endswith("--format text --max-chars 10 --page 2 --limit 1")


@pytest.mark.parametrize(
    ("sql", "fragments"),
    [
        ("select * from session", ["Did you mean sessions, agent_sessions", "Tables: ", "run_definitions"]),
        ("select * from watches", ["Did you mean run_definitions"]),
        (
            "select m.turn_id from messages m join agent_sessions s on s.id = m.session_id",
            ["No column 'turn_id' on messages.", "is a column of transcript, turns, agent_events", "messages columns: id,"],
        ),
        (
            "select content from agent_events",
            ["Did you mean content_text, content_json?", "agent_events columns: id,"],
        ),
    ],
)
def test_data_query_cli_explains_unknown_tables_and_columns(
    monkeypatch, tmp_path, capsys, sql, fragments
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    SQLiteBackgroundTaskStore().close()

    args = cli.build_parser().parse_args(["data", "query", "--sql", sql])
    assert cli.cmd_data_query(args) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "query_failed"
    for fragment in fragments:
        assert fragment in payload["hint"]


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
