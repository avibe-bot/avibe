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


def _add_test_watch(
    store: ManagedWatchStore,
    *,
    name: str,
    mode: str = "once",
):
    return store.add_watch(
        name=name,
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode=mode,
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )


def test_watch_help_describes_session_id_guidance(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["watch", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "managed background watchers" in captured.out
    assert "vibe watch add --session-id sesk8m4q2p7x --name 'Wait for export' --message" in captured.out
    assert "{add,update,list,show,pause,resume,remove}" in captured.out


def test_watch_add_help_mentions_shell_and_lifetime_timeout(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["watch", "add", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Pass either --shell '<command>' or a command after '--'." in captured.out
    assert "--lifetime-timeout" in captured.out
    assert "vibe watch add --session-id sesk8m4q2p7x --message 'The export finished. Inspect it and continue.'" in captured.out
    assert "watches follow up in this conversation by default" in captured.out
    assert "Prefer --message or --message-file for follow-up instructions" in captured.out
    assert "Terminal failures also send a follow-up and disable the watch." in captured.out
    assert "If this is your first time using this command, read this whole help entry before creating a watch." in captured.out
    assert "--same-scope" in captured.out
    assert "--scope-id" in captured.out
    assert "--post-to" not in captured.out
    assert "--deliver-key" not in captured.out


def test_watch_update_preserves_archived_agent_reference(tmp_path: Path, capsys) -> None:
    db_path = cli.paths.get_sqlite_state_path()
    agent_store = cli.VibeAgentStore(db_path)
    try:
        agent = agent_store.create(name="pm", backend="codex")
        agent_store.create(name="zz-fallback", backend="codex")
        store = ManagedWatchStore()
        watch = store.add_watch(
            name="Review watch",
            session_key="slack::channel::C123",
            agent_name=agent.name,
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
        archived = agent_store.archive(agent.name)
        assert archived is not None
        store.load()
        runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")

        args = _parse_watch_update([watch.id, "--name", "Renamed watch"])
        with (
            patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
            patch("vibe.cli._watch_store", return_value=store),
            patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
            patch(
                "vibe.cli._agent_store",
                side_effect=lambda: cli.VibeAgentStore(db_path),
            ),
        ):
            assert cli.cmd_watch_update(args) == 0

        assert json.loads(capsys.readouterr().out)["definition"]["agent_name"] == archived.archived_name
        assert ManagedWatchStore().get_watch(watch.id).agent_name == archived.archived_name

        explicit = _parse_watch_update([watch.id, "--agent", archived.archived_name])
        with (
            patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
            patch("vibe.cli._watch_store", return_value=ManagedWatchStore()),
            patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
            patch(
                "vibe.cli._agent_store",
                side_effect=lambda: cli.VibeAgentStore(db_path),
            ),
        ):
            result, payload = _capture_stderr_json(cli.cmd_watch_update, explicit)
        assert result == 1
        assert "disabled" in payload["error"]
    finally:
        agent_store.close()


def test_watch_list_help_describes_bounded_history(capsys) -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["watch", "list", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Successful one-shot watches are hidden by default" in captured.out
    assert "--include-finished" in captured.out
    assert "--page" in captured.out
    assert "--limit" in captured.out
    assert "--all" not in captured.out


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
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    original_add_watch = store.add_watch
    captured: dict[str, object] = {}

    def add_watch(**kwargs):
        captured.update(kwargs)
        return original_add_watch(**kwargs)

    with (
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch.object(store, "add_watch", side_effect=add_watch),
        patch(
            "vibe.cli._wait_for_watch_startup",
            side_effect=lambda *args, **kwargs: _startup_ok(args[0], args[1], args[2]),
        ),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["definition"]["agent_name"] == default_agent.name
    assert captured["expected_enabled_agent_id"] == default_agent.id


def test_watch_add_releases_create_once_session_when_definition_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    args = _parse_watch_add(
        [
            "--create-session",
            "--scope-id",
            "avibe::project::proj-cleanup-watch",
            "--cwd",
            str(tmp_path),
            "--shell",
            "true",
        ]
    )
    released: list[tuple[str, str]] = []
    agent = SimpleNamespace(id="agent-pm", name="pm", backend="claude")

    with (
        patch(
            "vibe.cli._resolve_agent_target",
            return_value=SimpleNamespace(agent=agent, requires_enabled_write_guard=True),
        ),
        patch(
            "vibe.cli._resolve_definition_scope_key",
            return_value="avibe::project::proj-cleanup-watch",
        ),
        patch("vibe.cli._resolve_definition_session_cwd", return_value=str(tmp_path)),
        patch("vibe.cli._reserve_definition_session", return_value="ses-reserved-watch"),
        patch("vibe.cli._validate_definition_delivery_target", return_value=(None, None)),
        patch(
            "vibe.cli._watch_store",
            return_value=SimpleNamespace(
                add_watch=lambda **_kwargs: (_ for _ in ()).throw(
                    ValueError("agent 'pm' was archived before the write")
                )
            ),
        ),
        patch(
            "vibe.cli._release_cli_session_reservation",
            side_effect=lambda session_id, *, reason: released.append((session_id, reason)) or True,
        ),
    ):
        result, payload = _capture_stderr_json(cli.cmd_watch_add, args)

    assert result == 1
    assert "archived before the write" in payload["error"]
    assert released == [
        (
            "ses-reserved-watch",
            "watch creation failed before its Session reservation was adopted",
        )
    ]


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
    assert "watch" not in payload
    assert payload["definition"]["name"] == "Wait for export"
    assert payload["definition"]["shell_command"] == "python3 scripts/wait.py"
    assert payload["definition"]["command"] == []
    assert payload["definition"]["mode"] == "once"
    assert payload["definition"]["retry_exit_codes"] == [75]


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
        "AVIBE_CALLER_REMOTE": "1",
        "AVIBE_CALLER_RESOURCE_CONTEXT": json.dumps(
            {
                "sub": "remote-editor",
                "vibe_instance_role": "editor",
                "vibe_instance_access_source": "email",
                "vibe_group_ids": [],
                "claims_issued_at": 1_900_000_000,
                "authorization_expires_at": 1_900_043_200,
            }
        ),
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
    assert payload["definition"]["metadata"]["created_by"] == expected
    stored = ManagedWatchStore(store_path).get_watch(payload["definition"]["id"])
    assert stored is not None
    assert stored.metadata["created_by"] == expected
    assert stored.metadata["resource_user_context"]["sub"] == "remote-editor"


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
    assert payload["definition"]["session_policy"] == "create_per_run"
    assert payload["definition"]["deliver_key"] is None
    assert payload["definition"]["cwd"] == str(invoke_dir)
    assert payload["definition"]["metadata"]["session_scope_id"] == "avibe::project::proj-scope-watch"
    assert "session_workdir" not in payload["definition"]["metadata"]
    assert payload["definition"]["agent_name"] == "project-agent"


def test_watch_add_create_per_run_without_scope_records_standalone_definition(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    args = _parse_watch_add(
        [
            "--agent",
            "worker",
            "--create-session-per-run",
            "--shell",
            "echo done",
        ]
    )

    with (
        patch.dict(os.environ, {"AVIBE_SESSION_ID": ""}, clear=False),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch(
            "vibe.cli._wait_for_watch_startup",
            side_effect=lambda *items, **kwargs: _startup_ok(store, runtime_store, items[2]),
        ),
    ):
        result = cli.cmd_watch_add(args)

    assert result == 0
    watch = json.loads(capsys.readouterr().out)["definition"]
    assert watch["session_policy"] == "create_per_run"
    assert watch["session_id"] is None
    assert watch["deliver_key"] is None
    assert "session_scope_id" not in watch["metadata"]
    assert "session_workdir" not in watch["metadata"]


def test_watch_add_create_session_scope_id_snapshots_scope_workdir(tmp_path: Path, capsys) -> None:
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
    target = cli.resolve_session_id_target(payload["definition"]["session_id"], db_path=db_path)
    assert target.visibility == "foreground"
    assert target.suppress_delivery is False
    assert target.workdir == str(tmp_path)
    assert payload["definition"]["cwd"] == str(invoke_dir)
    assert "session_workdir" not in payload["definition"]["metadata"]


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
    assert payload["definition"]["session_id"] == "sesCaller"
    assert payload["definition"]["session_policy"] == "existing"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Watch target Session defaulted to this Agent Session.",
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
    assert payload["definition"]["message"] == "Summarize the waiter output."
    assert payload["definition"]["prefix"] is None


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
    assert payload["definition"]["mode"] == "forever"
    assert payload["definition"]["command"] == ["python3", "scripts/wait.py", "--build", "42"]
    assert payload["definition"]["retry_exit_codes"] == [1, 75]


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
    assert payload["definition"]["cwd"] == str(workdir.resolve())


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


def test_default_watch_startup_timeout_accounts_for_recovery_entries(tmp_path: Path) -> None:
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    runtime_store.write(
        {
            "watches": {
                "first": {"running": True, "pid": 1234},
                "second": {"running": True, "pid": 5678},
                "finished": {"running": False, "pid": 9012},
            }
        }
    )

    recovery_entry_count = cli._watch_recovery_entry_count(runtime_store)
    timeout_seconds = cli._default_watch_startup_timeout_seconds(
        stable_running_seconds=cli.WATCH_STARTUP_STABLE_RUNNING_SECONDS,
        recovery_entry_count=recovery_entry_count,
    )
    base_timeout = cli._default_watch_startup_timeout_seconds(
        stable_running_seconds=cli.WATCH_STARTUP_STABLE_RUNNING_SECONDS,
    )

    assert recovery_entry_count == 2
    assert timeout_seconds == base_timeout + (2 * cli.WATCH_RECOVERY_ENTRY_TIMEOUT_SECONDS)


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
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
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
    assert payload["definitions"][0]["state"] == "running"
    assert payload["definitions"][0]["mode"] == "forever"


def test_watch_list_hides_finished_one_shots_by_default(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    active = _add_test_watch(store, name="Active")
    completed = _add_test_watch(store, name="Completed")
    failed = _add_test_watch(store, name="Failed")
    paused = _add_test_watch(store, name="Paused")
    failed_forever = _add_test_watch(store, name="Failed forever", mode="forever")
    store.mark_cycle_result(completed.id, exit_code=0, error=None, event_detected=True, disable=True)
    store.mark_cycle_result(failed.id, exit_code=1, error="failed", disable=True)
    store.set_enabled(paused.id, False)
    store.mark_cycle_result(failed_forever.id, exit_code=1, error="failed", disable=True)

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_list(brief=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in payload["definitions"]}
    assert ids == {active.id, failed.id, paused.id, failed_forever.id}
    assert completed.id not in ids
    assert next(item for item in payload["definitions"] if item["id"] == failed.id)["state"] == "failed"


def test_resumed_then_paused_one_shot_remains_in_default_list(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    watch = _add_test_watch(store, name="Resumable")
    store.mark_cycle_result(watch.id, exit_code=0, error=None, event_detected=True, disable=True)

    store.set_enabled(watch.id, True)
    store.set_enabled(watch.id, False)

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        assert cli.cmd_watch_list() == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["definitions"]] == [watch.id]
    assert payload["definitions"][0]["state"] == "paused"


def test_paused_forever_watch_changed_to_once_starts_new_lifecycle(
    tmp_path: Path,
    capsys,
) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    watch = _add_test_watch(store, name="Change mode", mode="forever")
    store.mark_cycle_result(watch.id, exit_code=0, error=None, event_detected=True, disable=False)
    store.set_enabled(watch.id, False)
    args = _parse_watch_update([watch.id, "--once"])

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        assert cli.cmd_watch_update(args) == 0

    updated = store.get_watch(watch.id)
    assert updated is not None
    assert updated.mode == "once"
    assert updated.last_started_at is None
    assert updated.last_finished_at is None
    assert updated.last_event_at is None
    assert updated.last_exit_code is None
    assert updated.last_error is None

    capsys.readouterr()
    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        assert cli.cmd_watch_list() == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["definitions"]] == [watch.id]
    assert payload["definitions"][0]["state"] == "paused"


def test_watch_list_defaults_to_first_page(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    for index in range(25):
        _add_test_watch(store, name=f"Watch {index:02d}")

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_list(brief=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert "watches" not in payload
    assert len(payload["definitions"]) == 20
    assert payload["pagination"] == {
        "page": 1,
        "limit": 20,
        "returned": 20,
        "has_more": True,
        "next_page": 2,
        "next_command": "vibe watch list --page 2 --limit 20",
    }


def test_watch_list_cli_dispatches_pagination_flags(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    for index in range(3):
        _add_test_watch(store, name=f"Watch {index}")

    monkeypatch.setattr(sys, "argv", ["vibe", "watch", "list", "--limit", "2"])
    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        pytest.raises(SystemExit) as exc,
    ):
        cli.main()

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["definitions"]) == 2
    assert payload["pagination"]["next_command"] == "vibe watch list --page 2 --limit 2"


def test_watch_list_include_finished_keeps_history_paginated(tmp_path: Path, capsys) -> None:
    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    for index in range(3):
        watch = _add_test_watch(store, name=f"Finished {index}")
        store.mark_cycle_result(watch.id, exit_code=0, error=None, event_detected=True, disable=True)

    with (
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_list(
            include_finished=True,
            brief=True,
            page_request=cli.PageRequest(page=1, limit=2),
        )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["definitions"]) == 2
    assert payload["pagination"]["has_more"] is True
    assert payload["pagination"]["next_command"] == (
        "vibe watch list --include-finished --page 2 --limit 2"
    )


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
        assert paused["definition"]["enabled"] is False

        assert cli.cmd_watch_set_enabled(watch.id, True) == 0
        resumed = json.loads(capsys.readouterr().out)
        assert resumed["definition"]["enabled"] is True

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
    assert payload["definition"]["id"] == watch.id
    assert payload["definition"]["name"] == "Watch deploy"
    assert payload["definition"]["session_key"] == "slack::channel::C456"
    assert payload["definition"]["post_to"] == "channel"
    assert payload["definition"]["prefix"] == "Deploy finished."
    assert payload["definition"]["mode"] == "forever"
    assert payload["definition"]["timeout_seconds"] == 1200
    assert payload["definition"]["lifetime_timeout_seconds"] == 7200
    assert payload["definition"]["retry_exit_codes"] == [1, 75]
    assert payload["definition"]["retry_delay_seconds"] == 10
    assert payload["definition"]["shell_command"] == "python3 wait_deploy.py"


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
    assert payload["definition"]["session_id"] is None
    assert payload["definition"]["session_key"] == "slack::channel::C456"


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
    assert payload["definition"]["post_to"] is None
    assert payload["definition"]["deliver_key"] is None
    assert payload["definition"]["metadata"]["session_scope_id"] == "avibe::project::proj-reset-watch"
    assert payload["definition"]["metadata"]["session_workdir"] == str(tmp_path)


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
    assert payload["definition"]["command"] == ["python3", "wait_deploy.py", "--flag", "value"]
    assert payload["definition"]["shell_command"] is None


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
    assert payload["definition"]["session_id"] == "sesExisting"
    assert payload["definition"]["cwd"] == str(new_cwd)
    assert "session_workdir" not in payload["definition"]["metadata"]


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


def test_watch_update_rejects_agent_together_with_clear_agent(tmp_path: Path) -> None:
    """HFR-256 — ``--agent X --clear-agent`` re-pinned today's default Agent.

    The sibling half of HFR-255, on ``vibe watch update``. The two flags mean
    opposite things and -- unlike ``--name`` / ``--clear-name`` in the same command
    -- the pair raised nothing. It also did not simply pick one: ``--clear-agent``
    won for ``agent_name`` (-> None) while the mere PRESENCE of ``--agent`` POPS the
    follow-the-session marker, so the definition looked like "no Agent pinned, and
    not following its Session" and the re-resolution wrote today's scope / default
    Agent back as a HARD PIN -- with neither flag's meaning honoured.

    THE FIX is the convention ``--name`` / ``--clear-name`` already sets in this very
    command: reject the contradictory pair, because the intent is genuinely ambiguous.
    Asserted BOTH ways below so the two pairs cannot drift apart.

    The stored definition must also be untouched: a rejected command may not have
    written a pin on its way to failing.
    """
    from storage.importer import ensure_sqlite_state

    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    agent_store = cli.VibeAgentStore(db_path)
    try:
        # The Agent the definition's Session runs as, and a DIFFERENT current
        # default. The gap between them is what makes the re-pin observable at all.
        agent_store.create(name="rebound", backend="codex")
        successor = agent_store.create(name="successor", backend="claude")
        agent_store.set_default_agent_name(successor.name)
    finally:
        agent_store.close()

    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="ci watch",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix="CI finished.",
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key="slack::channel::C123",
        agent_name=None,
        metadata={cli.BINDING_FOLLOWS_SESSION_METADATA_KEY: True},
    )

    def _update(*argv: str) -> tuple[int, str]:
        """Run the REAL command. Returns ``(exit code, raw stderr)``.

        Stderr is returned unparsed on purpose: the pre-fix command SUCCEEDS and
        writes nothing there, so parsing it eagerly would turn the interesting red
        into a ``JSONDecodeError`` and hide which field was corrupted.
        """
        args = _parse_watch_update([watch.id, *argv])
        cli_agent_store = cli.VibeAgentStore(db_path)
        stderr = io.StringIO()
        try:
            with (
                patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
                patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
                patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
                patch("vibe.cli._watch_store", return_value=store),
                patch("vibe.cli._watch_runtime_store", return_value=WatchRuntimeStateStore(tmp_path / "watch_runtime.json")),
                patch("vibe.cli._agent_store", return_value=cli_agent_store),
                redirect_stderr(stderr),
            ):
                return cli.cmd_watch_update(args), stderr.getvalue()
        finally:
            cli_agent_store.close()

    result, stderr_text = _update("--agent", "rebound", "--clear-agent")

    # The persisted definition first: this is the damage, and asserting it before the
    # exit code keeps the red pointed at the regression rather than at the reporting.
    stored = store.get_watch(watch.id)
    assert stored is not None
    assert stored.agent_name is None, (
        f"the contradictory pair pinned agent_name={stored.agent_name!r} — today's "
        f"default Agent ({successor.name!r}), which is neither the Agent that was "
        "passed nor the cleared state that was asked for; every future watch hook now "
        "runs as the wrong Agent"
    )
    assert stored.metadata.get(cli.BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "the contradictory pair dropped the follow-the-session state, so the bound "
        "Session no longer governs the definition's Agent"
    )
    assert result == 1, (
        "vibe watch update accepted --agent together with --clear-agent; it honours "
        "neither flag and re-pins the definition to today's default Agent instead"
    )
    assert json.loads(stderr_text)["code"] == "conflicting_agent_update", stderr_text

    # The convention this mirrors, asserted so the two pairs cannot drift apart.
    name_result, name_stderr = _update("--name", "renamed", "--clear-name")
    assert name_result == 1
    assert json.loads(name_stderr)["code"] == "conflicting_name_update", name_stderr


def _reclaim_bound_definitions_now(session_id: str, *, mode: str, reason: str) -> dict[str, int]:
    """The shared teardown reclaim, run against the isolated state database."""
    from config import paths
    from storage.db import create_sqlite_engine
    from storage.session_reclaim import reclaim_bound_definitions

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            return reclaim_bound_definitions(conn, session_id, mode=mode, reason=reason)
    finally:
        engine.dispose()


def _create_bare_agent_session(*, workdir: Path, anchor: str = "slack_C123") -> str:
    """A Session row worth snapshotting, with no Agent for the CLI to resolve."""
    from config import paths
    from storage.agent_session_rows import create_agent_session_row
    from storage.db import create_sqlite_engine

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            return create_agent_session_row(
                conn,
                scope_id=None,
                session_anchor=anchor,
                agent_backend="codex",
                agent_variant="codex",
                model="gpt-5.5-codex",
                native_session_id="codex-native",
                workdir=str(workdir),
                require_workdir=False,
            )
    finally:
        engine.dispose()


def test_watch_update_refuses_to_undo_a_reclaim_committed_after_its_read(tmp_path: Path) -> None:
    """HFR-261, watch half — ``upsert_watch`` is the same full-row write.

    Watches live in the same ``run_definitions`` table, are reclaimed by the same
    ``reclaim_bound_definitions`` call, and were written back by the same whole-row
    UPDATE keyed on ``id`` alone. Guarding only the task side would have left the
    identical hole open for every ``vibe watch update``: the reclaim's pause, its
    reason and its ``session_settings_snapshot`` restored to their pre-teardown
    values, with the command reporting success.
    """
    from storage.session_reclaim import RECLAIM_PAUSE, SESSION_SETTINGS_SNAPSHOT_KEY

    store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    session_id = _create_bare_agent_session(workdir=tmp_path)
    watch = store.add_watch(
        name="Watch CI",
        session_key="",
        session_id=session_id,
        session_policy="existing",
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
        metadata={"origin": "cli"},
    )

    summary = _reclaim_bound_definitions_now(
        session_id, mode=RECLAIM_PAUSE, reason="the bound agent session was cleared"
    )
    assert summary == {"paused": 1, "deleted": 0, "snapshotted": 1}, (
        f"the reclaim itself did not land ({summary!r}), so the rest of this test is "
        "meaningless"
    )

    args = _parse_watch_update([watch.id, "--name", "Renamed by the user"])
    stderr = io.StringIO()
    with (
        redirect_stderr(stderr),
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
    ):
        result = cli.cmd_watch_update(args)

    assert result == 1, (
        "the command reported success for a write the database refused; the stored "
        "watch is whatever the teardown left, not what the user was shown"
    )
    payload = json.loads(stderr.getvalue())
    assert payload["code"] == "definition_write_conflict"
    assert payload["details"]["watch_id"] == watch.id

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None
    assert stored.enabled is False, (
        "the stale full-row write re-enabled a watch the teardown paused; its "
        "supervisor starts cycles again against a session that no longer exists"
    )
    assert stored.last_error == "the bound agent session was cleared"
    assert SESSION_SETTINGS_SNAPSHOT_KEY in stored.metadata, (
        "the stale write replaced the reclaim's settings snapshot with the "
        "pre-teardown metadata"
    )
    assert stored.name == "Watch CI", (
        "the refused write partially landed — a lost compare-and-set must change NOTHING"
    )
    # HFR-271's rule: the store the COMMAND used is still in scope, and it is the half a
    # fresh read cannot see. A refusal must leave both halves saying the same thing.
    live = store.get_watch(watch.id)
    assert live is not None and live.to_dict() == stored.to_dict(), (
        "the write was refused and the live store kept the mutation: it still serves "
        f"name={None if live is None else live.name!r} "
        f"enabled={None if live is None else live.enabled!r} while the row says "
        f"name={stored.name!r} enabled={stored.enabled!r}"
    )


# --- the reserved workspace-notifications Session is not a Watch target -------
#
# Round-16 review thread 3678900318 (blocking, comment 5124692513), the Watch half of
# the same admission contract the Task and direct Agent Run lanes carry in
# ``tests/test_cli_task_command.py``. A watch is the WORST lane to leave open: it is
# long-lived and self-firing, so one accepted definition dispatches a turn into the
# runtime's own notice row on every event, not once.
#
# Subordinate coverage under HFR-094; no new scenario id.


def _capture_stderr_text(func, *args) -> tuple[int, str]:
    """Like ``_capture_stderr_json``, but WITHOUT parsing.

    The refusal test below asserts the EXIT CODE before it touches the payload. Against
    ``d00bc038`` the command succeeds, writes to stdout and leaves stderr empty, so a
    helper that parses first reports a ``JSONDecodeError`` about an empty string instead
    of the real regression ("this watch was admitted").
    """
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        result = func(*args)
    return result, stderr.getvalue()


def _no_caller_context(monkeypatch) -> None:
    """Run the command as a BARE terminal invocation.

    ``caller_context_from_env`` keys off ``AVIBE_SESSION_ID``, which is set inside every
    Avibe-hosted Agent shell — including the one a coding agent runs these tests from.
    Left alone it defaults the target Session and relaxes session-policy validation, so the
    same test would exercise a different path locally than in CI.
    """
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    monkeypatch.delenv("AVIBE_CALLER_REMOTE", raising=False)
    monkeypatch.delenv("AVIBE_CALLER_RESOURCE_CONTEXT", raising=False)


def _reserved_session_cli_db(tmp_path: Path):
    """A migrated CLI state DB holding the reserved row plus one ordinary session.

    Both rows in ONE database: the point is DISCRIMINATION — the same command and store
    must refuse one id and accept the other.

    Returns ``(db_path, agent_store, ordinary_session_id)``.
    """
    from storage.agent_session_rows import resolve_workspace_notice_session
    from storage.importer import ensure_sqlite_state
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = cli.VibeAgentStore(db_path)
    agent_store.create(name="worker", backend="codex")
    ensure_sqlite_state(db_path=db_path, primary_platform="slack")

    with cli.create_sqlite_engine(db_path).begin() as conn:
        assert resolve_workspace_notice_session(conn, title="Workspace notifications") == (
            "ses-workspace-notices"
        )

    service = SQLiteSessionsService(db_path)
    try:
        ordinary = service.bind_agent_session(
            scope_key="slack::channel::C900",
            agent_name="worker",
            session_anchor="slack_C900",
            native_session_id="native-C900",
        )
    finally:
        service.close()
    assert ordinary
    return db_path, agent_store, ordinary


def _message_rows(db_path: Path, session_id: str) -> list[tuple]:
    from sqlalchemy import text as sa_text

    with cli.create_sqlite_engine(db_path).begin() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                sa_text(
                    "SELECT author, type, content_text FROM messages "
                    "WHERE session_id = :sid ORDER BY created_at, id"
                ),
                {"sid": session_id},
            )
        ]


def test_watch_add_refuses_the_reserved_session_with_no_side_effects(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """``vibe watch add --session-id ses-workspace-notices`` is refused at ADMISSION.

    ``cmd_watch_add`` resolves the pin through ``_resolve_agent_for_target`` — and so
    through the shared ``resolve_session_id_target`` — before it stores the definition or
    starts a waiter, so the resolver guard closes this door with no watch-local
    exception. Zero side effects, per comment 5124692513: no watch row, no waiter
    started, nothing written into the reserved transcript.

    ``_wait_for_watch_startup`` is patched to a spy that MUST NOT be called: on this lane
    the absence of a dispatch is the claim, and a startup that fired would mean a real
    waiter subprocess was launched against a definition that should never have existed.

    POSITIVE CONTROL in the same test: the ordinary session id, same command, same store,
    is accepted and does start.
    """
    _no_caller_context(monkeypatch)
    db_path, agent_store, ordinary = _reserved_session_cli_db(tmp_path)
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    started: list[str] = []

    def _spy_startup(*args, **kwargs):
        started.append(args[2])
        return _startup_ok(store, runtime_store, args[2])

    args = _parse_watch_add(
        ["--session-id", "ses-workspace-notices", "--shell", "python3 scripts/wait.py"]
    )

    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=_spy_startup),
    ):
        result, stderr_text = _capture_stderr_text(cli.cmd_watch_add, args)

    assert result == 1, (
        "``vibe watch add --session-id ses-workspace-notices`` was ADMITTED. A watch is "
        "long-lived and self-firing, so this definition would dispatch a turn into the "
        f"runtime's own notice row on every event. stdout={capsys.readouterr().out!r} "
        f"started={started}"
    )
    payload = json.loads(stderr_text)
    assert payload["ok"] is False
    assert payload["code"] == "reserved_session", (
        "the refusal must be TYPED here too, with the same token the Web surface and the "
        f"other two admission doors use — one contract, every surface: {payload}"
    )
    assert "reserved for the runtime" in payload["error"], (
        f"the refusal has to say WHY, in the resolver's own diagnostic: {payload}"
    )
    assert "ses-workspace-notices" in payload["error"], (
        f"and it has to name the session that was refused: {payload}"
    )
    assert payload["details"] == {
        "session_id": "ses-workspace-notices",
        "reason": "reserved",
    }, f"and it must carry the machine-readable subject and reason: {payload}"

    # --- zero side effects --------------------------------------------------
    assert ManagedWatchStore(tmp_path / "watches.json").list_watches() == [], (
        "a watch pinned to a row that takes no turns must never be PERSISTED: it would "
        "dispatch a turn into the runtime's notice row on every event, not once"
    )
    assert store.list_watches() == [], "and the live store the command used must agree"
    assert started == [], (
        f"no waiter may be started for a definition that was never admitted: {started}"
    )
    assert runtime_store.load().get("watches", {}) == {}, (
        "and no runtime state may be recorded for it"
    )
    assert _message_rows(db_path, "ses-workspace-notices") == [], (
        "nothing may be written into the runtime's own row"
    )

    # --- positive control: the ordinary session is accepted -----------------
    ok_args = _parse_watch_add(
        ["--session-id", ordinary, "--shell", "python3 scripts/wait.py"]
    )
    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2({"slack"})),
        patch("vibe.cli.paths.get_state_dir", return_value=db_path.parent),
        patch("vibe.cli.paths.get_sqlite_state_path", return_value=db_path),
        patch("vibe.cli._agent_store", return_value=agent_store),
        patch("vibe.cli._watch_store", return_value=store),
        patch("vibe.cli._watch_runtime_store", return_value=runtime_store),
        patch("vibe.cli._wait_for_watch_startup", side_effect=_spy_startup),
    ):
        assert cli.cmd_watch_add(ok_args) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["ok"] is True
    assert accepted["definition"]["session_id"] == ordinary, (
        f"the guard must not have narrowed ordinary Watch targeting: {accepted['definition']}"
    )
    assert [watch.session_id for watch in store.list_watches()] == [ordinary]
    assert started == [accepted["definition"]["id"]], (
        f"and an admitted watch really does start: {started}"
    )
