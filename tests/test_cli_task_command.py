from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import cli


def _configured_v2(platforms: set[str]):
    return SimpleNamespace(
        slack=SimpleNamespace(
            bot_token="x" if "slack" in platforms else "",
            app_token="y" if "slack" in platforms else "",
        ),
        discord=SimpleNamespace(bot_token="x" if "discord" in platforms else ""),
        lark=SimpleNamespace(
            app_id="x" if "lark" in platforms else "",
            app_secret="y" if "lark" in platforms else "",
        ),
        wechat=SimpleNamespace(enable="wechat" in platforms),
        enabled_platforms=lambda: list(platforms),
    )


def _parse_task_add(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["task", "add", *argv])


def _parse_hook_send(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["hook", "send", *argv])


def _parse_agent_run(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["agent", "run", *argv])


def _parse_agent(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["agent", *argv])


def _capture_stderr_json(func, *args):
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        result = func(*args)
    return result, json.loads(stderr.getvalue())


def test_agent_enable_disable_cli_toggles_enabled_state(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")

    with patch("vibe.cli._agent_store", return_value=agent_store):
        assert cli.cmd_agent_set_enabled(_parse_agent(["disable", "worker"]), enabled=False) == 0
        disabled_payload = json.loads(capsys.readouterr().out)
        assert disabled_payload["agent"]["enabled"] is False

        assert cli.cmd_agent_list(_parse_agent(["list", "--brief"])) == 0
        assert json.loads(capsys.readouterr().out)["agents"] == []

        assert cli.cmd_agent_list(_parse_agent(["list", "--all", "--brief"])) == 0
        all_payload = json.loads(capsys.readouterr().out)
        assert all_payload["agents"][0]["name"] == "worker"
        assert all_payload["agents"][0]["enabled"] is False

        assert cli.cmd_agent_set_enabled(_parse_agent(["enable", "worker"]), enabled=True) == 0
        enabled_payload = json.loads(capsys.readouterr().out)
        assert enabled_payload["agent"]["enabled"] is True


def test_disabled_agent_cannot_run(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex", enabled=False)
    args = _parse_agent_run(["--agent", "worker", "--async", "--message", "hello"])

    with patch("vibe.cli._agent_store", return_value=agent_store):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["error"] == "agent 'worker' is disabled"


def test_task_add_rejects_unsupported_platform() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "foo::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack", "discord"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "unsupported_platform"
    assert payload["details"]["requested_platform"] == "foo"
    assert payload["help_command"] == "vibe task add --help"


def test_task_add_rejects_disabled_platform_even_with_credentials_present() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "discord::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    config = _configured_v2({"slack"})
    config.discord.bot_token = "configured-but-disabled"

    with patch("vibe.cli._ensure_config", return_value=config):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "unsupported_platform"
    # ``avibe`` (the web workbench) is always an available task platform; the
    # disabled discord platform is still correctly rejected.
    assert payload["details"]["configured_platforms"] == ["avibe", "slack"]


def test_supported_task_platforms_always_includes_avibe() -> None:
    # The web workbench (avibe) is always available, even when only IM platforms
    # are configured — so a scheduled task created from a workbench session isn't
    # rejected as "unsupported platform".
    config = _configured_v2({"slack"})
    with patch("vibe.cli._ensure_config", return_value=config):
        assert "avibe" in cli._supported_task_platforms()


def test_task_add_rejects_avibe_session_key() -> None:
    # avibe passes the platform gate but a bare session KEY has no agent session
    # id, so the reply couldn't attach to a workbench session — must be rejected
    # (target workbench sessions by --session-id instead).
    args = _parse_task_add(
        [
            "--session-key",
            "avibe::channel::ses3chKBjP5hy",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )
    config = _configured_v2({"slack"})
    with patch("vibe.cli._ensure_config", return_value=config):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "avibe_requires_session_id"


def test_task_help_describes_session_id_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Create, inspect, and control scheduled Agent messages for Avibe." in captured.out
    assert "vibe task add --session-id sesk8m4q2p7x" in captured.out
    assert "{add,update,list,show,pause,resume,run,remove}" in captured.out
    assert "rm (remove)" not in captured.out
    assert "\n    ls" not in captured.out


def test_task_add_help_includes_examples_and_threadless_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "add", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "If this is your first time using this command, read this whole help entry before creating a task." in captured.out
    assert "`--session-id` chooses which Agent Session Avibe will continue using when the task runs." in captured.out
    assert "--post-to" in captured.out
    assert "--deliver-key" in captured.out
    assert "Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid." in captured.out
    assert "Prefer weekday names such as mon, tue, or sun when scheduling by day of week." in captured.out


def test_hook_send_help_describes_runtime_effects(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["hook", "send", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "`vibe hook send` is a compatibility entrypoint." in captured.out
    assert "New automation should use `vibe agent run --async`." in captured.out
    assert "`vibe hook send` queues one deprecated asynchronous compatibility turn" in captured.out
    assert "`--post-to channel` changes where the message is posted, not which session is continued." in captured.out
    assert "`--message` and `--message-file` provide the one-shot async user message that will be queued immediately." in captured.out
    assert "--session-id" in captured.out
    assert "vibe agent run --async --session-id sesk8m4q2p7x" in captured.out


def test_task_list_help_mentions_completed_one_shots_hidden_by_default(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "list", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Completed one-shot tasks are hidden unless --all is used." in captured.out
    assert "--all" in captured.out
    assert "--brief" in captured.out


def test_task_update_help_includes_partial_update_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "update", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "keeping its task ID" in captured.out
    assert "--reset-delivery" in captured.out
    assert "Unspecified fields keep their existing values." in captured.out
    assert "Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid." in captured.out
    assert "Prefer weekday names such as mon, tue, or sun when scheduling by day of week." in captured.out


def test_hook_send_help_includes_examples_and_threadless_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["hook", "send", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "`vibe hook send` queues one deprecated asynchronous compatibility turn" in captured.out
    assert "--post-to" in captured.out
    assert "--deliver-key" in captured.out
    assert "--session-id" in captured.out
    assert "vibe agent run --async --session-id sesk8m4q2p7x" in captured.out


def test_agent_run_help_includes_fork_session_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["agent", "run", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--fork-session FORK_SESSION" in captured.out
    assert "Use --fork-session to create a new Session by forking an existing Session's native backend context." in captured.out
    assert "Forks keep the same backend as the source Session." in captured.out
    assert "vibe agent run --async --fork-session sesk8m4q2p7x" in captured.out
    assert "Do not combine --fork-session with --session-id, --create-session, --create-session-per-run, --deliver-key, or --post-to." in captured.out


def test_task_add_parse_error_is_structured_json(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "add", "--session-key", "slack::channel::C123"])

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert payload["help_command"] == "vibe task add --help"
    assert "--session-key SESSION_KEY" in payload["usage"]


def test_task_remove_alias_parse_error_keeps_structured_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["task", "rm"])

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert payload["help_command"] == "vibe task remove --help"
    assert "task_id" in payload["error"]


def test_task_add_rejects_invalid_session_key_with_hint() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::thread::123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "invalid_session_key"
    assert payload["example"] == "slack::channel::C123"


def test_task_add_rejects_conflicting_delivery_target_flags(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "task",
                "add",
                "--session-key",
                "slack::channel::C123",
                "--post-to",
                "channel",
                "--deliver-key",
                "slack::channel::C999",
                "--cron",
                "0 * * * *",
                "--message",
                "hello",
            ]
        )

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert "not allowed with argument --post-to" in payload["error"]
    assert payload["help_command"] == "vibe task add --help"


def test_task_add_rejects_post_to_thread_without_thread_session_key() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--post-to",
            "thread",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "invalid_delivery_target"


def test_task_add_rejects_cross_platform_deliver_key() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--deliver-key",
            "discord::channel::C999",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack", "discord"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "invalid_delivery_target"
    assert payload["details"] == {
        "session_platform": "slack",
        "delivery_platform": "discord",
    }


def test_task_add_rejects_invalid_cron_with_example() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "bad cron",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "invalid_cron"
    assert payload["example"] == "0 * * * *"


def test_task_add_rejects_invalid_timezone() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
            "--timezone",
            "Mars/Base",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "invalid_timezone"
    assert payload["details"]["timezone"] == "Mars/Base"


def test_task_show_missing_id_returns_guidance(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"

    with patch("vibe.cli._task_store", return_value=cli.ScheduledTaskStore(store_path)):
        result, payload = _capture_stderr_json(cli.cmd_task_show, "missing")

    assert result == 1
    assert payload["code"] == "task_not_found"
    assert payload["help_command"] == "vibe task list"


def test_task_update_missing_id_returns_guidance(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"

    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", "missing", "--name", "Updated"])

    with patch("vibe.cli._task_store", return_value=cli.ScheduledTaskStore(store_path)):
        result, payload = _capture_stderr_json(cli.cmd_task_update, args)

    assert result == 1
    assert payload["code"] == "task_not_found"
    assert payload["help_command"] == "vibe task list"


def test_task_run_missing_id_returns_guidance(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"

    with patch("vibe.cli._task_store", return_value=cli.ScheduledTaskStore(store_path)):
        result, payload = _capture_stderr_json(cli.cmd_task_run, "missing")

    assert result == 1
    assert payload["code"] == "task_not_found"
    assert payload["help_command"] == "vibe task list"


def test_task_list_hides_completed_one_shots_by_default(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    store.add_task(
        session_key="slack::channel::C123",
        prompt="recurring",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    done = store.add_task(
        session_key="slack::channel::C123",
        prompt="one-shot",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    store.mark_task_result(done.id, error=None)

    with patch("vibe.cli._task_store", return_value=store):
        result = cli.cmd_task_list()

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [item["id"] for item in payload["tasks"]]
    assert done.id not in ids


def test_task_list_brief_returns_scheduling_focused_view(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        name="Hourly summary",
        session_key="slack::channel::C123",
        prompt="recurring summary prompt",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    with patch("vibe.cli._task_store", return_value=store):
        result = cli.cmd_task_list(brief=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["tasks"][0]
    assert entry["id"] == task.id
    assert entry["display_name"] == "Hourly summary"
    assert "prompt" not in entry
    assert entry["next_run_at"] is not None
    assert entry["state"] == "active"


def test_task_list_sorts_by_next_run_instant_across_timezones(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    later = store.add_task(
        name="Later run",
        session_key="slack::channel::C123",
        prompt="later",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    earlier = store.add_task(
        name="Earlier run",
        session_key="slack::channel::C123",
        prompt="earlier",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    next_runs = {
        later.id: "2026-04-01T09:30:00+08:00",
        earlier.id: "2026-04-01T01:00:00+00:00",
    }
    monkeypatch.setattr(cli, "_task_next_run_at", lambda task: next_runs[task.id])

    with patch("vibe.cli._task_store", return_value=store):
        result = cli.cmd_task_list(brief=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    ordered_ids = [item["id"] for item in payload["tasks"]]
    assert ordered_ids == [earlier.id, later.id]


def test_task_show_includes_derived_schedule_fields(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        name="Hourly summary",
        session_key="slack::channel::C123",
        prompt="recurring summary prompt",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    with patch("vibe.cli._task_store", return_value=store):
        result = cli.cmd_task_show(task.id)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["display_name"] == "Hourly summary"
    assert payload["task"]["message_preview"] == "recurring summary prompt"
    assert payload["task"]["next_run_at"] is not None
    assert payload["task"]["state"] == "active"
    assert payload["task"]["last_status"] == "never_run"


def test_task_list_all_includes_completed_one_shots(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    done = store.add_task(
        session_key="slack::channel::C123",
        prompt="one-shot",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    store.mark_task_result(done.id, error=None)

    with patch("vibe.cli._task_store", return_value=store):
        result = cli.cmd_task_list(include_all=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [item["id"] for item in payload["tasks"]]
    assert done.id in ids


def test_task_run_enqueues_request(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    request_root = tmp_path / "task_requests"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    with (
        patch("vibe.cli._task_store", return_value=store),
        patch("vibe.cli._task_request_store", return_value=cli.TaskExecutionStore(request_root)),
    ):
        result = cli.cmd_task_run(task.id)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["task_id"] == task.id
    assert (request_root / "pending" / f"{payload['execution_id']}.json").exists()


def test_task_update_requires_at_least_one_change(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result, payload = _capture_stderr_json(cli.cmd_task_update, args)

    assert result == 1
    assert payload["code"] == "no_task_changes"


def test_task_update_modifies_existing_task_without_changing_id(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        ["task", "update", task.id, "--name", "Morning summary", "--cron", "*/30 * * * *", "--message", "updated"]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["id"] == task.id
    assert payload["task"]["name"] == "Morning summary"
    assert payload["task"]["cron"] == "*/30 * * * *"
    assert payload["task"]["prompt"] == "updated"


def test_task_update_session_key_clears_previous_session_id(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="",
        session_id="sesk8m4q2p7x",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--session-key", "slack::channel::C456"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["session_id"] is None
    assert payload["task"]["session_key"] == "slack::channel::C456"


def test_task_update_replaces_post_to_with_deliver_key(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="slack::channel::C123::thread::171717.123",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
        post_to="channel",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--deliver-key", "slack::channel::C999"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["id"] == task.id
    assert payload["task"]["post_to"] is None
    assert payload["task"]["deliver_key"] == "slack::channel::C999"


def test_task_add_returns_reachability_warning_for_unbound_lark_dm(tmp_path: Path, capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["task", "add", "--session-key", "lark::user::ou_123", "--cron", "0 * * * *", "--message", "hello"]
    )
    fake_store = SimpleNamespace(get_user=lambda *args, **kwargs: None)

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"lark"})),
        patch("vibe.cli._task_store", return_value=cli.ScheduledTaskStore(tmp_path / "scheduled_tasks.json")),
        patch("vibe.cli.SettingsStore.get_instance", return_value=fake_store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"][0]["code"] == "lark_user_not_bound"


def test_hook_send_rejects_invalid_session_key_with_hint() -> None:
    args = _parse_hook_send(["--session-key", "slack::thread::123", "--message", "hello"])

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_hook_send, args)

    assert result == 1
    assert payload["code"] == "invalid_session_key"
    assert payload["help_command"] == "vibe hook send --help"


def test_hook_send_rejects_conflicting_delivery_target_flags(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "hook",
                "send",
                "--session-key",
                "slack::channel::C123",
                "--post-to",
                "channel",
                "--deliver-key",
                "slack::channel::C999",
                "--message",
                "hello",
            ]
        )

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert "not allowed with argument --post-to" in payload["error"]
    assert payload["help_command"] == "vibe hook send --help"


def test_hook_send_rejects_cross_platform_deliver_key() -> None:
    args = _parse_hook_send(
        [
            "--session-key",
            "slack::channel::C123",
            "--deliver-key",
            "discord::channel::C999",
            "--message",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack", "discord"})):
        result, payload = _capture_stderr_json(cli.cmd_hook_send, args)

    assert result == 1
    assert payload["code"] == "invalid_delivery_target"
    assert payload["details"] == {
        "session_platform": "slack",
        "delivery_platform": "discord",
    }


def test_hook_send_enqueues_request(tmp_path: Path, capsys) -> None:
    args = _parse_hook_send(
        [
            "--session-key",
            "slack::channel::C123::thread::171717.123",
            "--post-to",
            "channel",
            "--message",
            "hello",
        ]
    )
    request_root = tmp_path / "task_requests"

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_request_store", return_value=cli.TaskExecutionStore(request_root)),
    ):
        result = cli.cmd_hook_send(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["session_key"] == "slack::channel::C123::thread::171717.123"
    assert payload["post_to"] == "channel"
    assert (request_root / "pending" / f"{payload['execution_id']}.json").exists()


def test_hook_send_allows_unresolved_legacy_scope_backend(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    default_agent = agent_store.ensure_default_agent(backend="claude")
    agent_store.create(name="codex", backend="opencode")
    agent_store.close()
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )
    args = _parse_hook_send(["--session-key", "slack::channel::C123", "--message", "hello"])

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_request_store", return_value=request_store),
    ):
        result = cli.cmd_hook_send(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    queued = json.loads((request_store.pending_dir / f"{payload['run_id']}.json").read_text())
    assert queued["session_key"] == "slack::channel::C123"
    assert queued["agent_name"] == default_agent.name


def test_hook_send_returns_reachability_warning_for_unbound_lark_dm(tmp_path: Path, capsys) -> None:
    args = _parse_hook_send(
        [
            "--session-key",
            "lark::user::ou_123",
            "--message",
            "hello",
        ]
    )
    request_root = tmp_path / "task_requests"
    fake_store = SimpleNamespace(get_user=lambda *args, **kwargs: None)

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"lark"})),
        patch("vibe.cli._task_request_store", return_value=cli.TaskExecutionStore(request_root)),
        patch("vibe.cli.SettingsStore.get_instance", return_value=fake_store),
    ):
        result = cli.cmd_hook_send(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"][0]["code"] == "lark_user_not_bound"


def test_agent_run_private_async_reserves_session_and_queues_request(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent = agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="slack"),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"].startswith("ses")
    assert payload["session_policy"] == "none"
    assert payload["agent"] == agent.name
    queued = json.loads((request_store.pending_dir / f"{payload['run_id']}.json").read_text())
    assert queued["request_type"] == "agent_run"
    assert queued["session_id"] == payload["session_id"]
    assert queued["agent_name"] == "worker"


def test_agent_run_create_session_uses_scope_anchor_for_channel_deliver_key(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(
        [
            "--agent",
            "worker",
            "--async",
            "--create-session",
            "--deliver-key",
            "slack::channel::C123",
            "--message",
            "hello",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    target = cli.resolve_session_id_target(payload["session_id"], db_path=db_path)
    assert target.session_key.to_key() == "slack::channel::C123"
    assert target.session_key.thread_id is None
    assert target.session_anchor == "slack_C123"


def test_agent_run_private_async_uses_no_delivery_channel_scope_for_lark(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._primary_platform", return_value="lark"),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            select scopes.platform, scopes.scope_type, scopes.native_type, scopes.metadata_json, agent_sessions.metadata_json
            from agent_sessions
            join scopes on scopes.id = agent_sessions.scope_id
            where agent_sessions.id = ?
            """,
            (payload["session_id"],),
        ).fetchone()

    assert row is not None
    assert row[0] == "lark"
    assert row[1] == "channel"
    assert row[2] == "private_agent_run"
    assert json.loads(row[3])["no_delivery"] is True
    assert json.loads(row[4])["no_delivery"] is True


def test_agent_run_rejects_deprecated_prompt_argument(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    args = _parse_agent_run(["--agent", "worker", "--async", "--prompt", "hello"])

    with patch("vibe.cli._agent_store", return_value=agent_store):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "deprecated_prompt_argument"


def test_agent_run_rejects_per_run_for_direct_invocation() -> None:
    args = _parse_agent_run(["--agent", "worker", "--create-session-per-run", "--message", "hello"])

    result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "invalid_session_policy"


def test_agent_run_rejects_backend_mismatch_for_existing_session(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="codex-worker", backend="codex")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_private_agent_session(
            platform="slack",
            agent_backend="claude",
            session_anchor="slack_private-agent-test",
        )
    finally:
        service.close()
    args = _parse_agent_run(["--agent", "codex-worker", "--session-id", session_id, "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "agent_session_backend_mismatch"


def test_agent_run_rejects_post_to_thread_for_threadless_session_before_enqueue(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_agent_session(
            scope_key="slack::C123",
            agent_backend="codex",
            session_anchor="slack_C123",
            agent_name="worker",
        )
    finally:
        service.close()
    args = _parse_agent_run(["--async", "--session-id", session_id, "--post-to", "thread", "--message", "hello"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "invalid_delivery_target"
    assert request_store.list_pending() == []


def test_agent_run_rejects_cross_platform_deliver_key_before_enqueue(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_agent_session(
            scope_key="slack::C123",
            agent_backend="codex",
            session_anchor="slack_C123",
            agent_name="worker",
        )
    finally:
        service.close()
    args = _parse_agent_run(
        [
            "--async",
            "--session-id",
            session_id,
            "--deliver-key",
            "discord::channel::C999",
            "--message",
            "hello",
        ]
    )

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack", "discord"})),
    ):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "invalid_delivery_target"
    assert payload["details"] == {
        "session_platform": "slack",
        "delivery_platform": "discord",
    }
    assert request_store.list_pending() == []


def test_agent_run_rejects_delivery_options_without_session_policy() -> None:
    args = _parse_agent_run(["--agent", "worker", "--async", "--post-to", "channel", "--message", "hello"])

    result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "delivery_target_without_session_policy"


def test_agent_run_existing_session_uses_session_agent_when_agent_omitted(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_private_agent_session(
            platform="slack",
            agent_backend="codex",
            agent_name="worker",
            session_anchor="slack_private-agent-test",
        )
    finally:
        service.close()
    args = _parse_agent_run(["--async", "--session-id", session_id, "--message", "hello"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "worker"
    queued = json.loads((request_store.pending_dir / f"{payload['run_id']}.json").read_text())
    assert queued["agent_name"] == "worker"


def test_agent_run_rejects_async_wait_timeout_combo() -> None:
    args = _parse_agent_run(["--agent", "worker", "--async", "--wait-timeout", "5", "--message", "hello"])

    result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "conflicting_wait_policy"


def test_agent_create_accepts_effort_alias(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    args = _parse_agent(["create", "worker", "--backend", "codex", "--effort", "high"])

    with patch("vibe.cli._agent_store", return_value=agent_store):
        result = cli.cmd_agent_create(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"]["reasoning_effort"] == "high"


def test_agent_default_cli_sets_default_agent(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.ensure_builtin_default_agents(["opencode", "codex"])
    args = _parse_agent(["default", "codex"])

    with patch("vibe.cli._agent_store", return_value=agent_store):
        result = cli.cmd_agent_default(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_agent_name"] == "codex"
    assert agent_store.get_default_agent_name() == "codex"


def test_agent_default_cli_bootstraps_builtin_backend_agent(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    args = _parse_agent(["default", "codex"])

    with patch("vibe.cli._agent_store", return_value=agent_store):
        result = cli.cmd_agent_default(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_agent_name"] == "codex"
    agent = agent_store.get("codex")
    assert agent is not None
    assert agent.backend == "codex"
    assert agent.enabled is True
    assert agent_store.get_default_agent_name() == "codex"


def test_agent_import_name_filters_global_candidates(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    keep = tmp_path / "reviewer.md"
    skip = tmp_path / "builder.md"
    keep.write_text("---\nname: reviewer\n---\nReview carefully.", encoding="utf-8")
    skip.write_text("---\nname: builder\n---\nBuild things.", encoding="utf-8")
    args = _parse_agent(["import", "--from", "codex", "--name", "reviewer"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli.iter_global_agent_files", return_value=[(keep, "codex"), (skip, "codex")]),
    ):
        result = cli.cmd_agent_import(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert [agent["name"] for agent in payload["imported"]] == ["reviewer"]
    assert agent_store.get("builder") is None


def test_agent_import_skips_malformed_global_candidates(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    valid = tmp_path / "reviewer.md"
    broken = tmp_path / "broken.md"
    valid.write_text("---\nname: reviewer\n---\nReview carefully.", encoding="utf-8")
    broken.write_text("---\nname: [broken\n---\n", encoding="utf-8")
    args = _parse_agent(["import", "--from", "codex", "--all"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli.iter_global_agent_files", return_value=[(broken, "codex"), (valid, "codex")]),
    ):
        result = cli.cmd_agent_import(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert [agent["name"] for agent in payload["imported"]] == ["reviewer"]
    assert payload["skipped"][0]["source_ref"] == str(broken)
    assert payload["skipped"][0]["reason"] == "invalid"


def test_default_agent_pointer_is_created(tmp_path: Path) -> None:
    agent_store = cli.VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    agent = agent_store.ensure_default_agent(backend="codex")

    assert agent.name == "default"
    assert agent_store.get_default_agent_name() == "default"
    assert agent_store.get_default_agent().backend == "codex"


def test_resolve_agent_for_target_bootstraps_sqlite_before_scope_lookup(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-state" / "vibe.sqlite"
    default_agent = SimpleNamespace(name="default", backend="codex")
    fake_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_default_agent=lambda: default_agent,
        close=lambda: None,
    )

    with (
        patch("vibe.cli._agent_store", return_value=fake_store),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        agent = cli._resolve_agent_for_target(
            agent_name=None,
            session_id=None,
            session_key="slack::channel::C123",
            help_command="vibe task add --help",
        )

    assert agent is default_agent
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from scope_settings").fetchone()[0] == 0


def test_resolve_agent_for_target_ignores_deprecated_scope_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    default_agent = cli.VibeAgentStore(db_path).ensure_default_agent(backend="claude")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import make_scope_id, upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )
        assert scope_id == make_scope_id("slack", "channel", "C123")

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        agent = cli._resolve_agent_for_target(
            agent_name=None,
            session_id=None,
            session_key="slack::channel::C123",
            help_command="vibe task add --help",
        )

    assert agent is not None
    assert agent.name == default_agent.name
    assert agent.backend == default_agent.backend
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select agent_name, agent_backend, settings_json from scope_settings where scope_id = ?",
            ("slack::channel::C123",),
        ).fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] == "codex"
    assert "agent_name" not in json.loads(row[2])["routing"]


def test_resolve_agent_for_target_allows_unresolved_legacy_scope_backend_without_session_creation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    default_agent = agent_store.ensure_default_agent(backend="claude")
    agent_store.create(name="codex", backend="opencode")
    agent_store.close()
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        agent = cli._resolve_agent_for_target(
            agent_name=None,
            session_id=None,
            session_key="slack::channel::C123",
            help_command="vibe task add --help",
        )

    assert agent is not None
    assert agent.name == default_agent.name


def test_resolve_agent_for_target_ignores_unresolved_legacy_scope_backend_for_session_creation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    default_agent = agent_store.ensure_default_agent(backend="claude")
    agent_store.create(name="codex", backend="opencode")
    agent_store.close()
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        agent = cli._resolve_agent_for_target(
            agent_name=None,
            session_id=None,
            session_key="slack::channel::C123",
            help_command="vibe task add --help",
        )

    assert agent is not None
    assert agent.name == default_agent.name


def test_reserve_definition_session_ignores_deprecated_scope_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    default_agent = cli.VibeAgentStore(db_path).ensure_default_agent(backend="claude")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        session_id = cli._reserve_definition_session(
            agent_name=None,
            deliver_key="slack::channel::C123",
            help_command="vibe task add --help",
        )
        target = cli.resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == default_agent.backend
    assert target.agent_name == default_agent.name
    assert target.agent_id


def test_reserve_definition_session_ignores_unresolved_legacy_scope_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    default_agent = agent_store.ensure_default_agent(backend="claude")
    agent_store.create(name="codex", backend="opencode")
    agent_store.close()
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        session_id = cli._reserve_definition_session(
            agent_name=None,
            deliver_key="slack::channel::C123",
            help_command="vibe task add --help",
        )
        target = cli.resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == default_agent.backend
    assert target.agent_name == default_agent.name


def test_task_add_create_per_run_ignores_unresolved_legacy_scope_backend(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    default_agent = agent_store.ensure_default_agent(backend="claude")
    agent_store.create(name="codex", backend="opencode")
    agent_store.close()
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )
    args = _parse_task_add(
        [
            "--create-session-per-run",
            "--deliver-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["task"]["agent_name"] == default_agent.name


def test_task_add_rejects_deprecated_prompt_argument() -> None:
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--prompt",
            "hello",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "deprecated_prompt_argument"
    assert "--message" in payload["hint"]


def test_hook_send_rejects_deprecated_prompt_argument() -> None:
    args = _parse_hook_send(["--session-key", "slack::channel::C123", "--prompt", "hello"])

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_hook_send, args)

    assert result == 1
    assert payload["code"] == "deprecated_prompt_argument"
    assert "--message" in payload["hint"]


def test_task_remove_alias_parses_to_remove_command() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["task", "remove", "task-123"])

    assert args.command == "task"
    assert args.task_command == "remove"
    assert args.task_id == "task-123"


def test_task_hidden_aliases_still_parse() -> None:
    parser = cli.build_parser()
    list_args = parser.parse_args(["task", "ls"])
    remove_args = parser.parse_args(["task", "rm", "task-123"])

    assert list_args.task_command == "ls"
    assert remove_args.task_command == "rm"
