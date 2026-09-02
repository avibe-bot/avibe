from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
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
    assert isinstance(entry["binding_token"], str)
    assert "expires_at" in entry


def test_prune_sessions_keeps_unexpired_released_entries() -> None:
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    sessions = {
        "current": {"expires_at": (now + timedelta(hours=1)).isoformat()},
        "expired": {"expires_at": (now - timedelta(seconds=1)).isoformat()},
        "legacy": {"updated_at": "2026-09-02T00:00:00+00:00"},
    }

    assert bridge._prune_sessions(sessions, now) == {
        "current": sessions["current"],
        "legacy": sessions["legacy"],
    }


def test_plugin_keeps_the_released_expiry_protocol() -> None:
    assert "binding.expires_at" in bridge.PLUGIN_SOURCE
    assert "execFileSync" not in bridge.PLUGIN_SOURCE


def test_new_binding_preserves_an_unexpired_released_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    path = bridge.binding_path()
    bridge._write_bindings(
        path,
        {
            "version": 1,
            "sessions": {
                "released-session": {
                    "env": {"AVIBE_SESSION_ID": "released"},
                    "updated_at": bridge._utc_now().isoformat(),
                    "expires_at": (bridge._utc_now() + timedelta(hours=1)).isoformat(),
                }
            },
        },
    )

    assert bridge.bind_session(
        "new-session",
        None,
        base_env={},
        working_dir=tmp_path,
        extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path)},
    )

    sessions = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    assert set(sessions) == {"released-session", "new-session"}


def test_binding_write_does_not_require_posix_fchmod(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.delattr(bridge.os, "fchmod")
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)

    assert bridge.bind_session(
        "oc-session",
        None,
        base_env={},
        working_dir=tmp_path,
        extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path)},
    )


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


def test_conditional_bind_cannot_replace_a_newer_turn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    kwargs = {
        "base_env": {"PATH": "/usr/bin"},
        "working_dir": tmp_path / "workspace",
        "extra_env": {"AVIBE_SKILL_WORKING_DIR": str(tmp_path / "workspace")},
    }
    assert bridge.bind_session(
        "oc-session",
        {},
        binding_token="new-turn",
        **kwargs,
    )

    assert not bridge.bind_session(
        "oc-session",
        {},
        binding_token="old-restored-turn",
        replace_existing=False,
        **kwargs,
    )
    entry = json.loads(bridge.binding_path().read_text(encoding="utf-8"))["sessions"][
        "oc-session"
    ]
    assert entry["binding_token"] == "new-turn"


def test_refresh_session_extends_only_the_matching_live_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    initial = datetime(2026, 9, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(bridge, "_utc_now", lambda: initial)
    assert bridge.bind_session(
        "oc-session",
        {},
        base_env={"PATH": "/usr/bin"},
        working_dir=tmp_path,
        extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path)},
        binding_token="owned-turn",
    )
    before = json.loads(bridge.binding_path().read_text(encoding="utf-8"))["sessions"][
        "oc-session"
    ]

    monkeypatch.setattr(bridge, "_utc_now", lambda: initial + timedelta(hours=1))
    assert bridge.refresh_session("oc-session", binding_token="other-turn") is False
    assert bridge.refresh_session("oc-session", binding_token="owned-turn") is True

    after = json.loads(bridge.binding_path().read_text(encoding="utf-8"))["sessions"][
        "oc-session"
    ]
    assert after["env"] == before["env"]
    assert datetime.fromisoformat(after["expires_at"]) == initial + timedelta(hours=25)


def test_binding_operations_can_target_an_adopted_server_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "new-avibe-home"))
    monkeypatch.setattr(git_runtime, "prepend_vendored_git_to_path", lambda *args, **kwargs: False)
    adopted_path = tmp_path / "old-avibe-home" / "runtime" / "opencode_caller_context.json"

    assert bridge.bind_session(
        "oc-session",
        {},
        base_env={"PATH": "/usr/bin"},
        working_dir=tmp_path,
        extra_env={"AVIBE_SKILL_WORKING_DIR": str(tmp_path)},
        binding_token="adopted-turn",
        path=adopted_path,
    )
    assert adopted_path.is_file()
    assert not bridge.binding_path().exists()
    assert bridge.refresh_session(
        "oc-session",
        binding_token="adopted-turn",
        path=adopted_path,
    )
    assert bridge.unbind_session(
        "oc-session",
        binding_token="adopted-turn",
        path=adopted_path,
    )
    assert "oc-session" not in json.loads(adopted_path.read_text(encoding="utf-8"))["sessions"]


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
