from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.watches import ManagedWatchStore, WatchRuntimeStateStore
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


def _parse_watch_add(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["watch", "add", *argv])


def _parse_watch_update(argv: list[str]):
    parser = cli.build_parser()
    return parser.parse_args(["watch", "update", *argv])


def _capture_stderr_json(func, *args):
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        result = func(*args)
    return result, json.loads(stderr.getvalue())


def _startup_ok(store: ManagedWatchStore, runtime_store: WatchRuntimeStateStore, watch_id: str):
    return store.get_watch(watch_id), runtime_store.load().get("watches", {}).get(watch_id)


def test_watch_help_describes_session_id_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["watch", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "managed background watchers" in captured.out
    assert "vibe watch add --session-id sesk8m4q2p7x" in captured.out
    assert "{add,update,list,show,pause,resume,remove}" in captured.out


def test_watch_add_help_mentions_shell_and_lifetime_timeout(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["watch", "add", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Pass either --shell '<command>' or a command after '--'." in captured.out
    assert "--lifetime-timeout" in captured.out
    assert "vibe watch add --session-id sesk8m4q2p7x" in captured.out
    assert "`--prefix` becomes the instruction text of the follow-up hook." in captured.out
    assert "Terminal failures also send a follow-up and disable the watch." in captured.out
    assert "If this is your first time using this command, read this whole help entry before creating a watch." in captured.out


def test_watch_add_parser_keeps_top_level_command_name() -> None:
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--",
            "python3",
            "wait.py",
        ]
    )

    assert args.command == "watch"
    assert args.watch_command == "add"
    assert args.waiter_command == ["--", "python3", "wait.py"]


def test_watch_update_parser_accepts_argv_command_replacement() -> None:
    args = _parse_watch_update(["watch-1", "--", "python3", "wait.py", "--flag", "value"])

    assert args.command == "watch"
    assert args.watch_command == "update"
    assert args.waiter_command == ["--", "python3", "wait.py", "--flag", "value"]


def test_watch_add_missing_command_is_structured_json() -> None:
    args = _parse_watch_add(["--session-key", "slack::channel::C123"])

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert payload["code"] == "missing_watch_command"
    assert payload["help_command"] == "vibe watch add --help"


def test_watch_add_rejects_lifetime_timeout_without_forever() -> None:
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--lifetime-timeout",
            "10",
            "--shell",
            "echo done",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert payload["code"] == "invalid_watch_lifetime_timeout"


def test_watch_add_rejects_missing_cwd() -> None:
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cwd",
            "/tmp/definitely-missing-watch-dir",
            "--shell",
            "echo done",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert payload["code"] == "invalid_watch_cwd"


def test_watch_add_create_per_run_ignores_unresolved_legacy_scope_backend(tmp_path: Path, capsys) -> None:
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
    args = _parse_watch_add(
        [
            "--create-session-per-run",
            "--deliver-key",
            "slack::channel::C123",
            "--shell",
            "echo done",
        ]
    )

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(args[0], args[1], args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["watch"]["agent_name"] == default_agent.name


def test_watch_add_creates_shell_watch(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--name",
            "Wait for export",
            "--prefix",
            "Export finished.",
            "--shell",
            "python3 scripts/wait.py",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["watch"]["name"] == "Wait for export"
    assert payload["watch"]["shell_command"] == "python3 scripts/wait.py"
    assert payload["watch"]["command"] == []
    assert payload["watch"]["mode"] == "once"
    assert payload["watch"]["retry_exit_codes"] == [75]


def test_watch_add_records_caller_context_metadata(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "watches.json"
    runtime_path = tmp_path / "watch_runtime.json"
    store = ManagedWatchStore(store_path)
    runtime_store = WatchRuntimeStateStore(runtime_path)
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--shell",
            "python3 scripts/wait.py",
        ]
    )
    caller_env = {
        "AVIBE_SESSION_ID": "sesCaller",
        "AVIBE_RUN_ID": "runCaller",
        "AVIBE_CALLER_SOURCE": "agent_turn",
        "AVIBE_CALLER_BACKEND": "opencode",
        "AVIBE_NATIVE_SESSION_ID": "native-opencode-1",
    }

    with (
        patch.dict(os.environ, caller_env, clear=False),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    expected = {
        "kind": "caller_context",
        "caller": {
            "session_id": "sesCaller",
            "run_id": "runCaller",
            "source": "agent_turn",
            "backend": "opencode",
            "native_session_id": "native-opencode-1",
        },
    }
    assert payload["watch"]["metadata"]["created_by"] == expected
    stored = ManagedWatchStore(store_path).get_watch(payload["watch"]["id"])
    assert stored is not None
    assert stored.metadata["created_by"] == expected


def test_watch_add_create_per_run_scope_id_records_session_scope_metadata(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="project-agent", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-29T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-scope-watch", now=now)
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

    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    args = _parse_watch_add(
        [
            "--create-session-per-run",
            "--scope-id",
            "avibe::project::proj-scope-watch",
            "--shell",
            "python3 scripts/wait.py",
        ]
    )

    with (
        patch("os.getcwd", return_value=str(invoke_dir)),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["session_policy"] == "create_per_run"
    assert payload["watch"]["deliver_key"] is None
    assert payload["watch"]["cwd"] == str(invoke_dir)
    assert payload["watch"]["metadata"]["session_scope_id"] == "avibe::project::proj-scope-watch"
    assert payload["watch"]["metadata"]["session_workdir"] == str(invoke_dir)
    assert payload["watch"]["agent_name"] == "project-agent"


def test_watch_add_create_session_scope_id_uses_invocation_cwd(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="project-agent", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-29T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-watch-once", now=now)
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

    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    invoke_dir = tmp_path / "invoke"
    invoke_dir.mkdir()
    args = _parse_watch_add(
        [
            "--create-session",
            "--scope-id",
            "avibe::project::proj-watch-once",
            "--shell",
            "python3 scripts/wait.py",
        ]
    )

    with (
        patch("os.getcwd", return_value=str(invoke_dir)),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    target = cli.resolve_session_id_target(payload["watch"]["session_id"], db_path=db_path)
    assert target.workdir == str(invoke_dir)
    assert payload["watch"]["cwd"] == str(invoke_dir)
    assert payload["watch"]["metadata"]["session_workdir"] == str(invoke_dir)


def test_watch_add_defaults_target_to_caller_session(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="codex", backend="codex")
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-28T00:00:00+00:00"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-watch-defaults", now=now)
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
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(["--shell", "python3 scripts/wait.py"])

    with (
        patch.dict(os.environ, {"AVIBE_SESSION_ID": "sesCaller"}, clear=False),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["session_id"] == "sesCaller"
    assert payload["watch"]["session_policy"] == "existing"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Watch target Session defaulted to the caller Session from AVIBE_SESSION_ID.",
        "session_id": "sesCaller",
    }


def test_watch_add_accepts_message_template(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--message",
            "Summarize the waiter output.",
            "--shell",
            "echo done",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["message"] == "Summarize the waiter output."
    assert payload["watch"]["prefix"] is None


def test_watch_add_creates_exec_watch_with_retry_codes(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--forever",
            "--timeout",
            "600",
            "--lifetime-timeout",
            "7200",
            "--retry-exit-code",
            "1",
            "--retry-exit-code",
            "75",
            "--",
            "python3",
            "scripts/wait.py",
            "--build",
            "42",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["mode"] == "forever"
    assert payload["watch"]["command"] == ["python3", "scripts/wait.py", "--build", "42"]
    assert payload["watch"]["retry_exit_codes"] == [1, 75]


def test_watch_add_persists_absolute_cwd(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    workdir = tmp_path / "repo"
    workdir.mkdir()
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--cwd",
            str(workdir.relative_to(tmp_path)),
            "--shell",
            "echo done",
        ]
    )

    monkeypatch.chdir(tmp_path)

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=lambda *args, **kwargs: _startup_ok(store, runtime_store, args[2])),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["cwd"] == str(workdir.resolve())


def test_watch_add_returns_structured_error_when_startup_fails(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--shell",
            "python3 scripts/wait.py",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch(
            "vibe.cli._wait_for_watch_startup",
            side_effect=cli.TaskCliError(
                "watch failed during startup and has already been disabled",
                code="watch_startup_failed",
                hint="Inspect the stored watch error, fix the waiter or its dependencies, then recreate the watch if monitoring should continue.",
                example="vibe watch show abc",
                help_command="vibe watch show abc",
            ),
        ),
    ):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert payload["code"] == "watch_startup_failed"
    assert payload["hint"].startswith("Inspect the stored watch error")
    assert payload["example"] == "vibe watch show abc"
    assert payload["help_command"] == "vibe watch show abc"


def test_wait_for_watch_startup_accepts_stably_running_watch(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Stable watch",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    watch.last_started_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    store.upsert_watch(watch)
    runtime_store.write(
        {
            "watches": {
                watch.id: {
                    "running": True,
                    "pid": 1234,
                    "started_at": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        }
    )

    resolved_watch, runtime_entry = cli._wait_for_watch_startup(
        store,
        runtime_store,
        watch.id,
        timeout_seconds=0.2,
        poll_interval_seconds=0.01,
        stable_running_seconds=1.5,
    )

    assert resolved_watch.id == watch.id
    assert runtime_entry["running"] is True


def test_default_watch_startup_timeout_exceeds_reconcile_and_stable_windows() -> None:
    timeout_seconds = cli._default_watch_startup_timeout_seconds(
        stable_running_seconds=cli.WATCH_STARTUP_STABLE_RUNNING_SECONDS
    )

    assert timeout_seconds > cli.WATCH_RECONCILE_INTERVAL_SECONDS + cli.WATCH_STARTUP_STABLE_RUNNING_SECONDS


def test_wait_for_watch_startup_rejects_watch_that_fails_before_stable_window(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Flaky watch",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    watch.last_started_at = datetime.now(timezone.utc).isoformat()
    store.upsert_watch(watch)
    runtime_store.write(
        {
            "watches": {
                watch.id: {
                    "running": True,
                    "pid": 1234,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        }
    )

    monotonic_values = iter([0.0, 0.05, 0.1, 0.15, 0.2])

    def _fail_watch(_seconds: float) -> None:
        failed = store.get_watch(watch.id)
        assert failed is not None
        failed.enabled = False
        failed.last_error = "waiter crashed"
        failed.last_exit_code = 1
        store.upsert_watch(failed)
        runtime_store.write({"watches": {}})

    with (
        patch("vibe.cli.time.monotonic", side_effect=lambda: next(monotonic_values)),
        patch("vibe.cli.time.sleep", side_effect=_fail_watch),
    ):
        with pytest.raises(cli.TaskCliError) as exc:
            cli._wait_for_watch_startup(
                store,
                runtime_store,
                watch.id,
                timeout_seconds=0.2,
                poll_interval_seconds=0.01,
                stable_running_seconds=1.5,
            )

    assert exc.value.code == "watch_startup_failed"


def test_watch_list_brief_includes_runtime_state(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    runtime_store.write(
        {
            "watches": {
                watch.id: {
                    "running": True,
                    "pid": 1234,
                    "started_at": "2026-04-02T00:00:00+00:00",
                    "updated_at": "2026-04-02T00:00:00+00:00",
                }
            }
        }
    )

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_list(brief=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watches"][0]["state"] == "running"
    assert payload["watches"][0]["mode"] == "forever"


def test_watch_show_missing_returns_structured_error() -> None:
    result, payload = _capture_stderr_json(cli.cmd_watch_show, "missing-watch")

    assert result == 1
    assert payload["code"] == "watch_not_found"


def test_watch_pause_resume_and_remove_update_store(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        assert cli.cmd_watch_set_enabled(watch.id, False) == 0
        paused = json.loads(capsys.readouterr().out)
        assert paused["watch"]["enabled"] is False

        assert cli.cmd_watch_set_enabled(watch.id, True) == 0
        resumed = json.loads(capsys.readouterr().out)
        assert resumed["watch"]["enabled"] is True

        assert cli.cmd_watch_remove(watch.id) == 0
        removed = json.loads(capsys.readouterr().out)
        assert removed["removed_id"] == watch.id


def test_watch_update_renames_and_retargets_watch(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update(
        [
            watch.id,
            "--name",
            "Watch deploy",
            "--session-key",
            "slack::channel::C456",
            "--post-to",
            "channel",
            "--prefix",
            "Deploy finished.",
            "--forever",
            "--timeout",
            "1200",
            "--lifetime-timeout",
            "7200",
            "--retry-exit-code",
            "1",
            "--retry-exit-code",
            "75",
            "--retry-delay",
            "10",
            "--shell",
            "python3 wait_deploy.py",
        ]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["id"] == watch.id
    assert payload["watch"]["name"] == "Watch deploy"
    assert payload["watch"]["session_key"] == "slack::channel::C456"
    assert payload["watch"]["post_to"] == "channel"
    assert payload["watch"]["prefix"] == "Deploy finished."
    assert payload["watch"]["mode"] == "forever"
    assert payload["watch"]["timeout_seconds"] == 1200
    assert payload["watch"]["lifetime_timeout_seconds"] == 7200
    assert payload["watch"]["retry_exit_codes"] == [1, 75]
    assert payload["watch"]["retry_delay_seconds"] == 10
    assert payload["watch"]["shell_command"] == "python3 wait_deploy.py"


def test_watch_update_session_key_clears_previous_session_id(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="",
        session_id="sesk8m4q2p7x",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update([watch.id, "--session-key", "slack::channel::C456"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["session_id"] is None
    assert payload["watch"]["session_key"] == "slack::channel::C456"


def test_watch_update_reset_delivery_preserves_creation_scope_metadata(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=str(tmp_path),
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key="avibe::project::proj-reset-watch",
        agent_name="worker",
        session_policy="create_per_run",
        metadata={
            "session_scope_id": "avibe::project::proj-reset-watch",
            "session_workdir": str(tmp_path),
        },
    )
    agent_store = cli.VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    agent_store.create(name="worker", backend="codex")
    args = _parse_watch_update([watch.id, "--reset-delivery"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["post_to"] is None
    assert payload["watch"]["deliver_key"] is None
    assert payload["watch"]["metadata"]["session_scope_id"] == "avibe::project::proj-reset-watch"
    assert payload["watch"]["metadata"]["session_workdir"] == str(tmp_path)


def test_watch_update_replaces_argv_command(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update([watch.id, "--", "python3", "wait_deploy.py", "--flag", "value"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["command"] == ["python3", "wait_deploy.py", "--flag", "value"]
    assert payload["watch"]["shell_command"] is None


def test_watch_update_no_changes_returns_structured_error(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update([watch.id])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
    ):
        result, payload = _capture_stderr_json(cli.cmd_watch_update, args)

    assert result == 1
    assert payload["code"] == "no_watch_changes"


def test_watch_update_rejects_scope_without_session_creation(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch CI",
        session_id="sesExisting",
        session_key="",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update([watch.id, "--scope-id", "avibe::project::proj-ignored"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
        patch("vibe.cli._watch_store", return_value=store),
    ):
        result, payload = _capture_stderr_json(cli.cmd_watch_update, args)

    assert result == 1
    assert payload["code"] == "scope_without_session_creation"


def test_watch_update_allows_cwd_for_already_reserved_create_once_watch(tmp_path: Path, capsys) -> None:
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
    from storage.settings_service import upsert_scope

    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    with cli.create_sqlite_engine(db_path).begin() as conn:
        now = "2026-06-16T00:00:00Z"
        scope_id = upsert_scope(conn, "avibe", "project", "proj-existing", now=now)
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
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            agent_sessions.insert().values(
                id="sesExisting",
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="worker",
                agent_variant="codex",
                session_anchor="avibe_proj-existing:definition_old",
                native_session_id="native-old",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                workdir=str(tmp_path / "old"),
            )
        )
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch CI",
        session_id="sesExisting",
        session_key="",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=str(tmp_path / "old"),
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
        agent_name="worker",
        session_policy="create_once",
        metadata={"session_scope_id": "avibe::project::proj-existing"},
    )
    new_cwd = tmp_path / "new"
    new_cwd.mkdir()
    args = _parse_watch_update([watch.id, "--cwd", str(new_cwd)])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2(set())),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["watch"]["session_id"] == "sesExisting"
    assert payload["watch"]["cwd"] == str(new_cwd)
    assert payload["watch"]["metadata"]["session_workdir"] == str(new_cwd)


def test_watch_update_rejects_deprecated_prompt_argument(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    args = _parse_watch_update([watch.id, "--prompt", "hello"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
    ):
        result, payload = _capture_stderr_json(cli.cmd_watch_update, args)

    assert result == 1
    assert payload["code"] == "deprecated_prompt_argument"
    assert "--message" in payload["hint"]


def test_watch_add_rejects_deprecated_prompt_argument() -> None:
    args = _parse_watch_add(
        [
            "--session-key",
            "slack::channel::C123",
            "--prompt",
            "hello",
            "--",
            "python3",
            "wait.py",
        ]
    )

    with patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert payload["code"] == "deprecated_prompt_argument"
    assert "--message" in payload["hint"]
