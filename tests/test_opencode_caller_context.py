from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
import queue
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import pytest

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
    assert '"experimental.chat.system.transform"' in source
    assert "AVIBE_OPENCODE_CALLER_CONTEXT_PATH" in source
    assert "Skills provide specialized instructions and workflows" in source
    assert "</available_skills>" in source
    assert "if (!bindingPath" in source
    assert "output.system.splice" in source

    assert bridge.ensure_plugin_installed().changed is False


def _run_node_when_ready(
    node: str,
    script: str,
    *,
    env: dict[str, str],
    startup_timeout: float = 60,
    execution_timeout: float = 10,
) -> str:
    # Cold Node/module loading is infrastructure, not the plugin's execution
    # budget. Start that budget only after imports complete and Node is waiting
    # for our stdin signal. Bound startup separately so a broken runtime fails.
    bootstrap = '''
import { once } from "node:events"
const start = once(process.stdin, "data")
process.stdout.write("AVIBE_NODE_READY\\n")
await start
process.stdin.pause()
'''
    with tempfile.TemporaryFile(mode="w+t") as stderr:
        with subprocess.Popen(
            [node, "--input-type=module", "--eval", bootstrap + script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            env=env, text=True,
        ) as process:
            ready: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
            reader.start()
            phase = "startup"
            try:
                signal = ready.get(timeout=startup_timeout)
                reader.join()
                if signal != "AVIBE_NODE_READY\n":
                    raise RuntimeError(f"unexpected readiness signal: {signal!r}")
                phase = "execution"
                stdout, _ = process.communicate(input="go\n", timeout=execution_timeout)
                if process.returncode != 0:
                    raise RuntimeError(f"Node exited with status {process.returncode}")
                return stdout
            except (queue.Empty, subprocess.TimeoutExpired, RuntimeError) as exc:
                process.kill()
                process.wait()
                reader.join()
                stderr.seek(0)
                pytest.fail(f"Node {phase} failed: {exc}\nNode stderr:\n{stderr.read()}")


@pytest.mark.parametrize("outcome", ["success", "startup-exit", "startup-timeout", "execution-exit", "execution-timeout"])
def test_node_readiness_separates_startup_from_execution_and_reports_stderr(monkeypatch, outcome):
    events = []

    class Output(io.StringIO):
        def readline(self):
            events.append("ready")
            return super().readline()

    class Process:
        returncode = 1 if outcome == "execution-exit" else 0
        stdout = Output("" if outcome == "startup-exit" else "AVIBE_NODE_READY\n")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, *, input, timeout):
            assert events == ["ready"]
            assert input == "go\n"
            assert timeout == 10
            events.append("execute")
            if outcome == "execution-timeout":
                raise subprocess.TimeoutExpired("node", timeout)
            return ('["result"]', None)

        def kill(self):
            events.append("kill")

        def wait(self):
            events.append("wait")

    def popen(command, **kwargs):
        kwargs["stderr"].write("fixture diagnostic")
        kwargs["stderr"].flush()
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    if outcome == "startup-timeout":
        def never_ready(self, *, timeout):
            assert timeout == 60
            raise queue.Empty

        monkeypatch.setattr(queue.Queue, "get", never_ready)
    if outcome == "success":
        assert _run_node_when_ready("node", "", env={}) == '["result"]'
        assert events == ["ready", "execute"]
    else:
        phase = "startup" if outcome.startswith("startup-") else "execution"
        with pytest.raises(pytest.fail.Exception, match=f"Node {phase} failed:.*\\nNode stderr:\\nfixture diagnostic"):
            _run_node_when_ready("node", "", env={})
        assert events[-2:] == ["kill", "wait"]
        assert ("execute" in events) == (phase == "execution")


def test_plugin_removes_native_catalog_without_touching_managed_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the OpenCode runtime plugin")
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    plugin = bridge.ensure_plugin_installed().path
    native = (
        "before\n"
        "Skills provide specialized instructions and workflows for specific tasks.\n"
        "Use the skill tool to load a skill when a task matches its description.\n"
        "<available_skills>\n"
        "  <skill><name>native-only</name></skill>\n"
        "</available_skills>\n"
        "after"
    )
    managed = "## Skills\n\n### Available skills\n- managed-only: Managed by Avibe."
    script = f"""
import {{ AvibeCallerContextPlugin }} from {json.dumps(plugin.as_uri())}
const hooks = await AvibeCallerContextPlugin()
const output = {{ system: {json.dumps([native, managed])} }}
await hooks["experimental.chat.system.transform"]({{}}, output)
process.stdout.write(JSON.stringify(output.system))
"""
    env = os.environ.copy()
    env["AVIBE_OPENCODE_CALLER_CONTEXT_PATH"] = str(tmp_path / "bindings.json")
    stdout = _run_node_when_ready(node, script, env=env)

    assert json.loads(stdout) == ["before\nafter", managed]

    direct_env = os.environ.copy()
    direct_env.pop("AVIBE_OPENCODE_CALLER_CONTEXT_PATH", None)
    stdout = _run_node_when_ready(node, script, env=direct_env)

    assert json.loads(stdout) == [native, managed]


@pytest.mark.parametrize("hub_mode", [True, False])
def test_plugin_keeps_hub_provider_definitions_exact_after_native_merge(tmp_path, monkeypatch, hub_mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the OpenCode runtime plugin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    plugin = bridge.ensure_plugin_installed().path
    overlay = {
        "enabled_providers": ["avibe-openai"],
        "provider": {"avibe-openai": {
            "npm": "@ai-sdk/openai",
            "options": {"baseURL": "http://127.0.0.1:18443/v1", "apiKey": "fixture-token"},
            "models": {"menu-model": {"id": "menu-model"}},
        }},
    }
    native = {
        "provider": {
            "avibe-openai": {
                "options": {"headers": {"AUTHORIZATION": "stale-key"}},
                "models": {"menu-model": {"headers": {"x-api-key": "stale-key"}}},
                "future_transport_field": "unowned",
            },
            "user-owned": {"options": {"apiKey": "native-token"}},
        },
        "permission": "ask",
        "agent": {"custom": {"prompt": "Keep native configuration"}},
    }
    script = f"""
import {{ AvibeCallerContextPlugin }} from {json.dumps(plugin.as_uri())}
const hooks = await AvibeCallerContextPlugin()
const config = {json.dumps(native)}
await hooks.config(config)
process.stdout.write(JSON.stringify(config))
"""
    stdout = _run_node_when_ready(
        node, script,
        env={**os.environ, "AVIBE_OPENCODE_MODEL_HUB": "1" if hub_mode else "0",
             "OPENCODE_CONFIG_CONTENT": json.dumps(overlay)},
    )
    expected = {**native, "provider": {**native["provider"], **overlay["provider"]}} if hub_mode else native
    assert json.loads(stdout) == expected


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


def test_bind_session_preserves_an_empty_builtin_catalog_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        extra_env={"AVIBE_BUILTIN_SKILLS_ROOT": ""},
    )

    assert ok is True
    data = json.loads(bridge.binding_path().read_text(encoding="utf-8"))
    assert data["sessions"]["oc-session"]["env"]["AVIBE_BUILTIN_SKILLS_ROOT"] == ""
    assert 'key === "AVIBE_BUILTIN_SKILLS_ROOT"' in bridge.PLUGIN_SOURCE


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
