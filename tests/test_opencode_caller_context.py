from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading

from core import git_runtime
from modules.agents.opencode import caller_context as bridge


def test_ensure_plugin_installed_writes_global_opencode_plugin(tmp_path: Path, monkeypatch) -> None:
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    result = bridge.ensure_plugin_installed()
    path = result.path

    assert path == xdg_home / "opencode" / "plugins" / bridge.PLUGIN_FILENAME
    assert result.changed is True
    source = path.read_text(encoding="utf-8")
    assert '"shell.env"' in source
    assert "AVIBE_OPENCODE_CALLER_CONTEXT_PATH" in source

    assert bridge.ensure_plugin_installed().changed is False


def test_bind_session_writes_env_binding(tmp_path: Path, monkeypatch) -> None:
    avibe_home = tmp_path / "avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)

    ok = bridge.bind_session(
        "oc-session",
        {
            "task_execution_id": "run123",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {
                "id": "ses123",
                "agent_backend": "opencode",
                "native_session_id": "oc-session",
            },
        },
        base_env={"PATH": "/usr/bin"},
        working_dir=tmp_path / "workspace",
        extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path / "workspace")},
    )

    assert ok is True
    data = json.loads(bridge.binding_path().read_text(encoding="utf-8"))
    entry = data["sessions"]["oc-session"]
    assert entry["env"] == {
        "AVIBE_SESSION_ID": "ses123",
        "AVIBE_RUN_ID": "run123",
        "AVIBE_CALLER_SOURCE": "agent_run",
        "AVIBE_CALLER_BACKEND": "opencode",
        "AVIBE_NATIVE_SESSION_ID": "oc-session",
        "AVIBE_SKILL_WORKING_DIR": str(tmp_path / "workspace"),
    }
    assert entry["caller_context"]["session_id"] == "ses123"
    assert entry["owner_pid"] == os.getpid()
    assert isinstance(entry["binding_token"], str)
    assert datetime.fromisoformat(entry["expires_at"]) > datetime.now(timezone.utc)


def test_prune_sessions_bounds_legacy_bindings_by_updated_at() -> None:
    now = datetime.now(timezone.utc)
    sessions = {
        "current-legacy": {"updated_at": (now - timedelta(hours=1)).isoformat()},
        "expired-legacy": {"updated_at": (now - timedelta(hours=25)).isoformat()},
        "unbounded-legacy": {"owner_pid": os.getpid()},
    }

    assert bridge._prune_sessions(sessions, now) == {
        "current-legacy": sessions["current-legacy"]
    }


def test_bind_session_skips_without_resolved_caller_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)

    assert (
        bridge.bind_session(
            "oc-session",
            {"platform": "slack"},
            base_env={"PATH": "/usr/bin"},
            working_dir=tmp_path / "workspace",
        )
        is False
    )
    assert not bridge.binding_path().exists()


def test_bind_session_writes_vendored_path_without_caller_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))

    def inject_git(env, *, base_env, working_dir):
        assert env == {}
        assert base_env == {"PATH": ""}
        assert working_dir == tmp_path / "workspace"
        env["PATH"] = "/managed/git/bin"
        return True

    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", inject_git)

    assert (
        bridge.bind_session(
            "oc-session",
            {"platform": "slack"},
            base_env={"PATH": ""},
            working_dir=tmp_path / "workspace",
        )
        is True
    )
    data = json.loads(bridge.binding_path().read_text(encoding="utf-8"))
    entry = data["sessions"]["oc-session"]
    assert entry["env"] == {"PATH": "/managed/git/bin"}
    assert "caller_context" not in entry


def test_bind_session_clears_stale_path_when_git_override_disappears(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    inject = {"enabled": True}

    def inject_git(env, *, base_env, working_dir):
        if inject["enabled"]:
            env["PATH"] = "/managed/git/bin"
            return True
        return False

    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", inject_git)
    kwargs = {
        "base_env": {"PATH": "/usr/bin"},
        "working_dir": tmp_path / "workspace",
    }
    assert bridge.bind_session("oc-session", {"platform": "slack"}, **kwargs) is True

    inject["enabled"] = False

    assert bridge.bind_session("oc-session", {"platform": "slack"}, **kwargs) is False
    data = json.loads(bridge.binding_path().read_text(encoding="utf-8"))
    assert "oc-session" not in data["sessions"]


def test_unbind_session_removes_only_matching_turn_binding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    kwargs = {
        "base_env": {"PATH": "/usr/bin"},
        "working_dir": tmp_path / "workspace",
        "extra_env": {"AVIBE_SKILL_WORKING_DIR": str(tmp_path / "workspace")},
    }
    assert bridge.bind_session("oc-session", {}, binding_token="new-turn", **kwargs) is True

    assert bridge.unbind_session("oc-session", binding_token="old-turn") is False
    assert "oc-session" in json.loads(bridge.binding_path().read_text(encoding="utf-8"))["sessions"]
    assert bridge.unbind_session("oc-session", binding_token="new-turn") is True
    assert "oc-session" not in json.loads(bridge.binding_path().read_text(encoding="utf-8"))["sessions"]


def test_concurrent_bindings_preserve_every_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    barrier = threading.Barrier(2)

    def bind(session_id: str) -> bool:
        barrier.wait()
        return bridge.bind_session(
            session_id,
            {},
            base_env={"PATH": "/usr/bin"},
            working_dir=tmp_path / "workspace",
            extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path / "workspace")},
            binding_token=f"token-{session_id}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, ("session-a", "session-b")))

    assert results == [True, True]
    data = json.loads(bridge.binding_path().read_text(encoding="utf-8"))
    assert set(data["sessions"]) == {"session-a", "session-b"}
