from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def _parse_runs_cancel(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["runs", "cancel", *argv])


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
    args = _parse_agent_run(["--agent", "worker", "--async", "--no-callback", "--message", "hello"])

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
    assert "vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message" in captured.out
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
    assert "tasks continue this conversation by default" in captured.out
    assert "--post-to" not in captured.out
    assert "--same-scope" in captured.out
    assert "--scope-id" in captured.out
    assert "--deliver-key" not in captured.out
    assert "Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid." in captured.out
    assert "Prefer weekday names such as mon, tue, or sun when scheduling by day of week." in captured.out


def test_hook_send_help_describes_runtime_effects(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["hook", "send", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "`vibe hook send` is a compatibility entrypoint." in captured.out
    assert "New automation should use `vibe agent run`." in captured.out
    assert "`vibe hook send` queues one deprecated asynchronous compatibility turn" in captured.out
    assert "--post-to" not in captured.out
    assert "`--message` and `--message-file` provide the one-shot async user message that will be queued immediately." in captured.out
    assert "--session-id" in captured.out
    assert "vibe agent run --session-id sesk8m4q2p7x --no-callback --message" in captured.out


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
    assert "--post-to" not in captured.out
    assert "--deliver-key" not in captured.out
    assert "--session-id" in captured.out
    assert "vibe agent run --session-id sesk8m4q2p7x --no-callback" in captured.out


def test_agent_run_help_includes_fork_session_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["agent", "run", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--fork-session FORK_SESSION" in captured.out
    assert "--fork-self" in captured.out
    assert "--sync" in captured.out
    assert "--same-scope" in captured.out
    assert "--scope-id" in captured.out
    assert "--visible" in captured.out
    assert "--deliver-key" not in captured.out
    assert "Avibe Agent shell examples:" in captured.out
    assert "Normal terminal examples:" in captured.out
    assert "--fork-self forks this current Session." in captured.out
    assert "Forks keep the same backend, scope, and cwd as the source Session." in captured.out
    assert "vibe agent run --fork-self --message" in captured.out
    assert "Agent runs are async by default. From an Avibe Agent shell, they return their final result to this conversation by default." in captured.out
    assert "vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'" in captured.out
    assert (
        "vibe agent run --agent release-reviewer --visible --message 'Review this project in a visible sibling Session.'"
        in captured.out
    )
    assert (
        "vibe agent run --agent release-reviewer --no-callback --message 'Review the latest deployment result.'"
        not in captured.out
    )
    assert "vibe agent run --agent release-reviewer --same-scope --no-callback --message" not in captured.out
    assert "From a normal terminal, pass --callback-session-id or --no-callback for async runs." in captured.out
    assert "Review the latest CI result and print it here." in captured.out
    assert "Review the latest CI result and report back." in captured.out
    assert "--callback-session-id sescaller456 --message 'Review the latest CI result and report back.'" in captured.out
    assert "--no-callback --message 'Review the latest CI result and report back.'" not in captured.out
    assert "Do not combine fork flags with --session-id or --create-session." in captured.out
    assert "--create-session-per-run" not in captured.out


def test_agent_run_rejects_async_and_sync_together(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["agent", "run", "--async", "--sync", "--message", "hello"])

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert "not allowed with argument --async" in payload["error"]


def test_agent_run_visible_sugar_sets_foreground_visibility() -> None:
    args = _parse_agent_run(["--agent", "worker", "--visible", "--message", "hello"])

    assert args.visibility == "foreground"


def test_agent_run_visible_conflicts_with_explicit_visibility(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "agent",
                "run",
                "--agent",
                "worker",
                "--visible",
                "--visibility",
                "background",
                "--message",
                "hello",
            ]
        )

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert "--visibility" in payload["error"]
    assert "--visible" in payload["error"]


def test_agent_run_runtime_rejects_async_and_sync_together() -> None:
    args = SimpleNamespace(
        agent="worker",
        session_id=None,
        fork_session=None,
        fork_self=False,
        create_session=False,
        create_session_per_run=False,
        same_scope=False,
        scope_id=None,
        deliver_key=None,
        model=None,
        reasoning_effort=None,
        cwd=None,
        post_to=None,
        callback_session_id=None,
        no_callback=True,
        async_run=True,
        sync_run=True,
        wait_timeout=None,
        message="hello",
        message_file=None,
        prompt=None,
        prompt_file=None,
    )

    result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "conflicting_wait_policy"


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


def test_task_add_records_caller_context_metadata(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    args = _parse_task_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )
    caller_env = {
        "AVIBE_SESSION_ID": "sesCaller",
        "AVIBE_RUN_ID": "runCaller",
        "AVIBE_CALLER_SOURCE": "agent_turn",
        "AVIBE_CALLER_BACKEND": "codex",
        "AVIBE_NATIVE_SESSION_ID": "native-codex-1",
    }

    with (
        patch.dict(os.environ, caller_env, clear=False),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    expected = {
        "kind": "caller_context",
        "caller": {
            "session_id": "sesCaller",
            "run_id": "runCaller",
            "source": "agent_turn",
            "backend": "codex",
            "native_session_id": "native-codex-1",
        },
    }
    assert payload["task"]["metadata"]["created_by"] == expected
    stored = cli.ScheduledTaskStore(store_path).get_task(payload["task"]["id"])
    assert stored is not None
    assert stored.metadata["created_by"] == expected


def test_task_add_create_per_run_scope_id_records_session_scope_metadata(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="project-agent", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-29T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-scope-task", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(tmp_path),
                agent_name="project-agent",
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_name": "project-agent"}}),
                created_at=now,
                updated_at=now,
            )
        )

    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    args = _parse_task_add(
        [
            "--create-session-per-run",
            "--scope-id",
            "avibe::project::proj-scope-task",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with (
        patch("os.getcwd", return_value=str(invoke_dir)),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["session_policy"] == "create_per_run"
    assert payload["task"]["deliver_key"] is None
    assert payload["task"]["cwd"] is None
    assert payload["task"]["metadata"]["session_scope_id"] == "avibe::project::proj-scope-task"
    assert "session_workdir" not in payload["task"]["metadata"]
    assert payload["task"]["agent_name"] == "project-agent"


def test_task_add_create_per_run_without_scope_records_standalone_definition(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    store = cli.ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    args = _parse_task_add(
        [
            "--agent",
            "worker",
            "--create-session-per-run",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    with (
        patch.dict(os.environ, {"AVIBE_SESSION_ID": ""}, clear=False),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    task = json.loads(capsys.readouterr().out)["task"]
    assert task["session_policy"] == "create_per_run"
    assert task["session_id"] is None
    assert task["deliver_key"] is None
    assert task["cwd"] is None
    assert "session_scope_id" not in task["metadata"]
    assert "session_workdir" not in task["metadata"]


def test_task_add_create_session_scope_id_supports_project_scope(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="project-agent", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-29T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-once-task", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(tmp_path),
                agent_name="project-agent",
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_name": "project-agent"}}),
                created_at=now,
                updated_at=now,
            )
        )

    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    args = _parse_task_add(
        [
            "--create-session",
            "--scope-id",
            "avibe::project::proj-once-task",
            "--at",
            "2026-06-30T00:00:00+00:00",
            "--message",
            "hello",
        ]
    )

    with (
        patch("os.getcwd", return_value=str(invoke_dir)),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["session_policy"] == "create_once"
    target = cli.resolve_session_id_target(payload["task"]["session_id"], db_path=db_path)
    assert target.session_key.session_scope == "avibe::project::proj-once-task"
    assert target.visibility == "foreground"
    assert target.suppress_delivery is False
    assert target.workdir == str(tmp_path)
    assert payload["task"]["cwd"] is None
    assert payload["task"]["metadata"]["session_scope_id"] == "avibe::project::proj-once-task"
    assert "session_workdir" not in payload["task"]["metadata"]


def test_task_add_create_session_scope_id_uses_unique_definition_anchors(tmp_path: Path, capsys) -> None:
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, "avibe", "project", "proj-once-unique", now="2026-06-16T00:00:00Z")
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
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        store = cli.ScheduledTaskStore(tmp_path / "scheduled_tasks.json")

        payloads = []
        for cron in ("0 * * * *", "30 * * * *"):
            args = _parse_task_add(
                [
                    "--agent",
                    "worker",
                    "--create-session",
                    "--scope-id",
                    scope_id,
                    "--cron",
                    cron,
                    "--message",
                    "hello",
                ]
            )
            with (
                patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
                patch("vibe.cli._agent_store", return_value=agent_store),
                patch("vibe.cli._task_store", return_value=store),
                patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
                patch("os.getcwd", return_value=str(invoke_dir)),
            ):
                assert cli.cmd_task_add(args) == 0
            payloads.append(json.loads(capsys.readouterr().out))

        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.session_anchor)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .order_by(agent_sessions.c.created_at, agent_sessions.c.id)
                ).mappings()
            )

    assert {payload["task"]["session_id"] for payload in payloads} == {row["id"] for row in rows}
    anchors = {row["session_anchor"] for row in rows}
    assert len(anchors) == 2
    assert all(anchor.startswith("avibe_proj-once-unique:definition_") for anchor in anchors)


def test_task_add_defaults_target_to_caller_session(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="codex", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-28T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-cli-defaults", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="sesCaller",
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="codex",
                agent_variant="default",
                session_anchor="anchor_sesCaller",
                native_session_id="native-caller",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    args = _parse_task_add(["--cron", "0 * * * *", "--message", "hello"])

    with (
        patch.dict(os.environ, {"AVIBE_SESSION_ID": "sesCaller"}, clear=False),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["session_id"] == "sesCaller"
    assert payload["task"]["session_policy"] == "existing"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Task target Session defaulted to this Agent Session.",
        "session_id": "sesCaller",
    }


def test_task_add_rejects_scope_without_session_creation() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "task",
            "add",
            "--session-id",
            "sesExisting",
            "--scope-id",
            "avibe::project::proj-ignored",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )

    result, payload = _capture_stderr_json(cli.cmd_task_add, args)

    assert result == 1
    assert payload["code"] == "scope_without_session_creation"


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


def test_task_update_rejects_scope_without_session_creation(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_id="sesExisting",
        session_key="",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--scope-id", "avibe::project::proj-ignored"])

    with patch("vibe.cli._task_store", return_value=store):
        result, payload = _capture_stderr_json(cli.cmd_task_update, args)

    assert result == 1
    assert payload["code"] == "scope_without_session_creation"


def test_task_update_rejects_cwd_for_already_reserved_create_once_task(tmp_path: Path) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_id="sesExisting",
        session_key="",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
        agent_name="worker",
        session_policy="create_once",
        cwd=str(tmp_path / "old"),
        metadata={"session_scope_id": "avibe::project::proj-existing"},
    )
    new_cwd = tmp_path / "new"
    new_cwd.mkdir()
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--cwd", str(new_cwd)])

    with patch("vibe.cli._task_store", return_value=store):
        result, payload = _capture_stderr_json(cli.cmd_task_update, args)

    assert result == 1
    assert payload["code"] == "cwd_with_existing_session"


def test_task_update_create_session_preserves_existing_cwd_without_cwd_flag(tmp_path: Path, capsys) -> None:
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
    from storage.settings_service import upsert_scope

    state_home = tmp_path / "home"
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    saved_cwd = tmp_path / "saved"
    saved_cwd.mkdir()
    with patch.dict("os.environ", {"AVIBE_HOME": str(state_home)}):
        ensure_sqlite_state()
        db_path = state_home / "state" / "vibe.sqlite"
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, "avibe", "project", "proj-replace-cwd", now="2026-06-16T00:00:00Z")
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
            conn.execute(
                agent_sessions.insert().values(
                    id="sesOld",
                    scope_id=scope_id,
                    agent_backend="codex",
                    agent_name="worker",
                    agent_variant="codex",
                    session_anchor="avibe_proj-replace-cwd:definition_old",
                    native_session_id="native-old",
                    status="active",
                    metadata_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                    last_active_at="2026-06-16T00:00:00Z",
                    workdir=str(saved_cwd),
                )
            )
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        store = cli.ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
        task = store.add_task(
            session_id="sesOld",
            session_key="",
            prompt="hello",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="Asia/Shanghai",
            agent_name="worker",
            session_policy="create_once",
            cwd=str(saved_cwd),
            metadata={"session_scope_id": scope_id, "session_workdir": str(saved_cwd)},
        )
        parser = cli.build_parser()
        args = parser.parse_args(["task", "update", task.id, "--create-session"])

        with (
            patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_store", return_value=store),
            patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            patch("os.getcwd", return_value=str(invoke_dir)),
        ):
            result = cli.cmd_task_update(args)

        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions.c.workdir).where(agent_sessions.c.id == store.get_task(task.id).session_id).limit(1)
            ).mappings().one()

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["cwd"] == str(saved_cwd)
    assert payload["task"]["metadata"]["session_workdir"] == str(saved_cwd)
    assert row["workdir"] == str(saved_cwd)
    assert payload["task"]["session_id"] != "sesOld"


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


def test_task_update_reset_delivery_preserves_creation_scope_metadata(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "scheduled_tasks.json"
    store = cli.ScheduledTaskStore(store_path)
    task = store.add_task(
        session_key="",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
        agent_name="worker",
        session_policy="create_per_run",
        post_to="channel",
        metadata={
            "session_scope_id": "avibe::project::proj-reset-task",
            "session_workdir": str(tmp_path),
        },
    )
    agent_store = cli.VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    agent_store.create(name="worker", backend="codex")
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--reset-delivery"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_store", return_value=store),
    ):
        result = cli.cmd_task_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["post_to"] is None
    assert payload["task"]["deliver_key"] is None
    assert payload["task"]["metadata"]["session_scope_id"] == "avibe::project::proj-reset-task"


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


def test_hook_send_deprecation_warning_names_callback_policy(tmp_path: Path, capsys) -> None:
    args = _parse_hook_send(["--session-key", "slack::channel::C123", "--message", "hello"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._task_request_store", return_value=cli.TaskExecutionStore(tmp_path / "task_requests")),
    ):
        result = cli.cmd_hook_send(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert "vibe hook send is deprecated" in payload["deprecation_warning"]
    assert "--no-callback" in payload["deprecation_warning"]
    assert "--callback-session-id <session-id>" in payload["deprecation_warning"]


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


def test_runs_cancel_running_agent_run_stops_live_turn_and_marks_canceled(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_live_cancel",
        message="keep working",
        agent_name="worker",
        callback_session_id="ses_callback",
    )
    assert request_store.claim(request.id) is not None
    cancel_dispatch = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"ok": True, "session_id": "ses_live_cancel", "status": "cancel_requested"},
        }
    )

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_awaited_once_with("ses_live_cancel")
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["completed_at"] is not None
    assert saved["cancel_requested"] is True
    assert saved["callback_status"] == "pending"
    pending_callbacks = request_store.list_pending_callbacks()
    assert [item["id"] for item in pending_callbacks] == [request.id]
    assert pending_callbacks[0]["status"] == "canceled"
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "live_cancel_confirmed"
    assert payload["cancel_result"]["live_cancel_confirmed"] is True
    assert payload["cancel_result"]["run_terminalized"] is True
    assert payload["run"]["status"] == "canceled"


def test_runs_cancel_running_agent_run_reports_recorded_only_when_controller_unavailable(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from vibe import internal_client

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_controller_down",
        message="keep working",
        agent_name="worker",
    )
    assert request_store.claim(request.id) is not None
    cancel_dispatch = AsyncMock(side_effect=internal_client.InternalServerUnavailable("missing socket"))

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_awaited_once_with("ses_controller_down")
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["completed_at"] is None
    assert saved["cancel_requested"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "cancel_request_recorded_only"
    assert payload["cancel_result"]["reason_code"] == "internal_unavailable"
    assert payload["cancel_result"]["live_cancel_confirmed"] is False
    assert payload["run"]["status"] == "running"


def test_runs_cancel_running_agent_run_reports_recorded_only_when_backend_refuses_stop(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_stop_failed",
        message="keep working",
        agent_name="worker",
    )
    assert request_store.claim(request.id) is not None
    cancel_dispatch = AsyncMock(
        return_value={
            "status_code": 409,
            "body": {"ok": False, "code": "stop_failed", "reason": "interrupt_failed"},
        }
    )

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_awaited_once_with("ses_stop_failed")
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["cancel_requested"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "cancel_request_recorded_only"
    assert payload["cancel_result"]["reason_code"] == "stop_failed"
    assert payload["cancel_result"]["detail"]["controller_status_code"] == 409


def test_runs_cancel_running_agent_run_reports_recorded_only_when_no_live_turn(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_no_live_turn",
        message="keep working",
        agent_name="worker",
    )
    assert request_store.claim(request.id) is not None
    cancel_dispatch = AsyncMock(
        return_value={
            "status_code": 404,
            "body": {"ok": False, "code": "not_in_flight", "session_id": "ses_no_live_turn"},
        }
    )

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_awaited_once_with("ses_no_live_turn")
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["cancel_requested"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "cancel_request_recorded_only"
    assert payload["cancel_result"]["reason_code"] == "not_in_flight"
    assert payload["cancel_result"]["detail"]["controller_status_code"] == 404


def test_runs_cancel_running_agent_run_does_not_overwrite_already_finished_turn(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_already_finished",
        message="keep working",
        agent_name="worker",
    )
    assert request_store.claim(request.id) is not None
    cancel_dispatch = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"ok": True, "session_id": "ses_already_finished", "status": "already_finished"},
        }
    )

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_awaited_once_with("ses_already_finished")
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["completed_at"] is None
    assert saved["cancel_requested"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "cancel_request_recorded_only"
    assert payload["cancel_result"]["reason_code"] == "already_finished"
    assert payload["cancel_result"]["live_cancel_confirmed"] is False
    assert payload["run"]["status"] == "running"


def test_runs_cancel_queued_agent_run_does_not_call_live_controller(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = cli.TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="ses_queued_cancel",
        message="queued work",
        agent_name="worker",
    )
    cancel_dispatch = AsyncMock()

    with (
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.internal_client.cancel_dispatch", cancel_dispatch),
    ):
        result = cli.cmd_runs_cancel(_parse_runs_cancel([request.id]))

    assert result == 0
    cancel_dispatch.assert_not_awaited()
    saved = request_store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["cancel_requested"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_code"] == "queued_canceled"
    assert payload["run"]["status"] == "canceled"


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


def test_agent_run_standalone_async_reserves_background_session(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent = agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--no-callback", "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch(
            "storage.sessions_service.paths.get_show_page_dir",
            side_effect=lambda session_id: tmp_path / "show" / session_id,
        ),
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
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select scope_id, visibility, workdir from agent_sessions where id = ?",
            (payload["session_id"],),
        ).fetchone()
    assert row == (None, "background", str(tmp_path / "show" / payload["session_id"]))
    assert (tmp_path / "show" / payload["session_id"]).is_dir()


def test_agent_run_caller_scope_default_keeps_caller_cwd_and_same_scope_uses_scope_cwd(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    from core.services import sessions as sessions_service
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
    from storage.settings_service import upsert_scope

    home = tmp_path / "home"
    invocation_cwd = tmp_path / "caller-cwd"
    scope_cwd = tmp_path / "scope-cwd"
    invocation_cwd.mkdir()
    scope_cwd.mkdir()
    monkeypatch.setenv("AVIBE_HOME", str(home))
    ensure_sqlite_state()
    db_path = home / "state" / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_caller",
            now="2026-07-23T00:00:00Z",
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(scope_cwd),
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json="{}",
                created_at="2026-07-23T00:00:00Z",
                updated_at="2026-07-23T00:00:00Z",
            )
        )
        caller = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="caller",
        )

    monkeypatch.setenv("AVIBE_SESSION_ID", caller["id"])
    monkeypatch.chdir(invocation_cwd)
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")

    def run(extra: list[str]) -> dict:
        args = _parse_agent_run(
            ["--agent", "worker", "--no-callback", *extra, "--message", "hello"]
        )
        with (
            patch("vibe.cli._agent_store", return_value=agent_store),
            patch("vibe.cli._task_request_store", return_value=request_store),
        ):
            assert cli.cmd_agent_run(args) == 0
        return json.loads(capsys.readouterr().out)

    implicit = run([])
    explicit = run(["--same-scope"])
    visible = run(["--visibility", "foreground"])

    with engine.connect() as conn:
        rows = {
            row.id: row
            for row in conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.scope_id,
                    agent_sessions.c.visibility,
                    agent_sessions.c.workdir,
                ).where(
                    agent_sessions.c.id.in_(
                        [implicit["session_id"], explicit["session_id"], visible["session_id"]]
                    )
                )
            )
        }

    assert rows[implicit["session_id"]].scope_id == scope_id
    assert rows[implicit["session_id"]].visibility == "background"
    assert rows[implicit["session_id"]].workdir == str(invocation_cwd)
    assert rows[explicit["session_id"]].scope_id == scope_id
    assert rows[explicit["session_id"]].visibility == "background"
    assert rows[explicit["session_id"]].workdir == str(scope_cwd)
    assert rows[visible["session_id"]].scope_id == scope_id
    assert rows[visible["session_id"]].visibility == "foreground"
    assert rows[visible["session_id"]].workdir == str(invocation_cwd)


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
            "--no-callback",
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
    assert target.session_anchor.startswith("slack_C123:run_")


def test_agent_run_create_session_preserves_legacy_thread_deliver_key(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(
        [
            "--agent",
            "worker",
            "--async",
            "--no-callback",
            "--create-session",
            "--deliver-key",
            "slack::channel::C123::thread::171717.123",
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
    assert payload["deliver_key"] == "slack::channel::C123::thread::171717.123"
    target = cli.resolve_session_id_target(payload["session_id"], db_path=db_path)
    assert target.session_key.to_key() == "slack::channel::C123::thread::171717.123"
    assert target.session_key.thread_id == "171717.123"
    assert target.session_anchor.startswith("slack_171717.123:run_")
    queued = request_store.get_run(payload["run_id"])
    assert queued is not None
    assert queued["deliver_key"] == "slack::channel::C123::thread::171717.123"


def test_agent_run_create_session_scope_id_uses_unique_project_anchors(tmp_path: Path, capsys) -> None:
    from sqlalchemy import select
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
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
                native_id="proj_unique",
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
        agent_store = cli.VibeAgentStore(db_path)
        agent_store.create(name="worker", backend="codex")
        request_store = cli.TaskExecutionStore(tmp_path / "task_requests")

        payloads = []
        for message in ("one", "two"):
            args = _parse_agent_run(
                [
                    "--agent",
                    "worker",
                    "--async",
                    "--no-callback",
                    "--create-session",
                    "--scope-id",
                    scope_id,
                    "--message",
                    message,
                ]
            )
            with (
                patch("vibe.cli._agent_store", return_value=agent_store),
                patch("vibe.cli._task_request_store", return_value=request_store),
                patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
            ):
                assert cli.cmd_agent_run(args) == 0
            payloads.append(json.loads(capsys.readouterr().out))

        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_sessions.c.id, agent_sessions.c.session_anchor)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .order_by(agent_sessions.c.created_at, agent_sessions.c.id)
                ).mappings()
            )

    assert {payload["session_id"] for payload in payloads} == {row["id"] for row in rows}
    anchors = {row["session_anchor"] for row in rows}
    assert len(anchors) == 2
    assert all(anchor.startswith("avibe_proj_unique:run_") for anchor in anchors)


def test_agent_run_standalone_does_not_create_platform_pseudo_scope(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    args = _parse_agent_run(["--agent", "worker", "--async", "--no-callback", "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._task_request_store", return_value=request_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch(
            "storage.sessions_service.paths.get_show_page_dir",
            side_effect=lambda session_id: tmp_path / "show" / session_id,
        ),
    ):
        result = cli.cmd_agent_run(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select scope_id, visibility from agent_sessions where id = ?",
            (payload["session_id"],),
        ).fetchone()

    assert row == (None, "background")


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


def test_agent_run_rejects_cross_backend_agent_for_existing_session(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="codex-worker", backend="codex")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_standalone_agent_session(
            agent_backend="claude",
            session_anchor="slack_private-agent-test",
            workdir=str(tmp_path),
        )
    finally:
        service.close()
    args = _parse_agent_run(["--agent", "codex-worker", "--sync", "--session-id", session_id, "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "agent_session_backend_mismatch"


def test_agent_run_existing_session_allows_matching_agent_hint(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="codex-worker", backend="codex")
    request_store = cli.TaskExecutionStore(tmp_path / "task_requests")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_standalone_agent_session(
            agent_backend="codex",
            agent_name="codex-worker",
            session_anchor="slack_private-agent-test",
            workdir=str(tmp_path),
        )
    finally:
        service.close()
    args = _parse_agent_run(
        [
            "--agent",
            "codex-worker",
            "--async",
            "--no-callback",
            "--session-id",
            session_id,
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
    assert payload["session_id"] == session_id
    assert payload["agent"] == "codex-worker"


def test_agent_run_rejects_different_same_backend_agent_for_existing_session(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="session-worker", backend="codex")
    agent_store.create(name="other-worker", backend="codex")
    from storage.sessions_service import SQLiteSessionsService

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_standalone_agent_session(
            agent_backend="codex",
            agent_name="session-worker",
            session_anchor="slack_private-agent-test",
            workdir=str(tmp_path),
        )
    finally:
        service.close()
    args = _parse_agent_run(["--agent", "other-worker", "--sync", "--session-id", session_id, "--message", "hello"])

    with (
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "agent_session_agent_mismatch"


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
        session_id = service.reserve_standalone_agent_session(
            agent_backend="codex",
            agent_name="worker",
            session_anchor="slack_private-agent-test",
            workdir=str(tmp_path),
        )
    finally:
        service.close()
    args = _parse_agent_run(["--async", "--no-callback", "--session-id", session_id, "--message", "hello"])

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


def test_agent_run_rejects_default_async_wait_timeout_combo() -> None:
    args = _parse_agent_run(["--agent", "worker", "--wait-timeout", "5", "--message", "hello"])

    result, payload = _capture_stderr_json(cli.cmd_agent_run, args)

    assert result == 1
    assert payload["code"] == "conflicting_wait_policy"
    assert "--sync" in payload["hint"]


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
