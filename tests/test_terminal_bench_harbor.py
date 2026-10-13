"""Optional real-Harbor boundary tests, no container/provider calls.

Normal product CI skips this file when Harbor is not installed. The explicit
benchmark-host test command installs pinned requirements-harbor.txt separately.
The fake Environment is a transport recorder; real Harbor construction, profile,
wheel verification, source snapshot, cancellation and result projection run.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

pytest.importorskip("harbor.agents.installed.base")
if version("harbor") != "0.24.0":
    pytest.skip("optional boundary requires pinned Harbor 0.24.0", allow_module_level=True)

from harbor.models.agent.context import AgentContext
from scripts.terminal_bench.harbor_agent import FullAvibeAgent


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / "avibe_os-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("avibe_os-9.9.9.dist-info/METADATA", "Name: avibe-os\nVersion: 9.9.9\n")
    return path, sha256(path.read_bytes()).hexdigest()


def make_agent(tmp_path, wheel, **changes):
    path, digest = wheel
    options = {"logs_dir": tmp_path / "agent-logs", "model_name": "fixture-model",
               "wheel_path": str(path), "wheel_sha256": digest,
               "profile": {"model": "fixture-model", "base_url": "http://127.0.0.1:9/v1",
                           "setup_seconds": 20, "wall_seconds": 5, "cleanup_seconds": 1}}
    options.update(changes)
    return FullAvibeAgent(**options)


class Environment:
    default_user = None

    def __init__(self, *, fail_cleanup=False, block=False):
        self.commands = []
        self.uploads = {}
        self.files = {}
        self.report = {
            "outcome": "succeeded", "run_id": "fixture-run", "session_id": "fixture-session",
            "cleanup": {"owned_processes_stopped": True}, "accounting_complete": False,
            "usage": {"totals": {"token_reports": 2, "input_tokens": 100,
                                   "cached_input_tokens": 25, "output_tokens": 30}},
        }
        self.fail_cleanup, self.block = fail_cleanup, block
        self.started = asyncio.Event()
        self.cleanup_complete = False
        self.downloaded = False
        self.key_removed = False
        self.secret_upload_modes = None
        self.upload_error = False
        self.block_upload = False
        self.upload_started = asyncio.Event()
        self.allow_upload = asyncio.Event()

    async def upload_file(self, source, target):
        path = Path(source)
        if target.endswith("credential/provider.key"):
            self.secret_upload_modes = (path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777)
            self.upload_started.set()
            if self.block_upload:
                await self.allow_upload.wait()
            self.files[target] = path.read_bytes()
            if self.upload_error:
                raise OSError("synthetic upload failed after write")
        else:
            self.files[target] = path.read_bytes()

    async def upload_dir(self, source, target):
        self.uploads[target] = {p.relative_to(source).as_posix(): p.read_bytes()
                                for p in Path(source).rglob("*") if p.is_file()}

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command == "id -u":
            return SimpleNamespace(return_code=0, stdout="0\n", stderr="")
        if command == "pwd -P":
            return SimpleNamespace(return_code=0, stdout="/app\n", stderr="")
        if "scripts.terminal_bench.credential metadata" in command:
            return SimpleNamespace(return_code=0, stdout=json.dumps({"regular": True, "mode": "0600", "directory_mode": "0700"}), stderr="")
        if "scripts.terminal_bench.credential clean" in command:
            self.key_removed = True
            for name in list(self.files):
                if name.endswith("credential/provider.key"):
                    self.files.pop(name)
            return SimpleNamespace(return_code=0, stdout=json.dumps({"removed": True}), stderr="")
        if "scripts.terminal_bench.launch run" in command:
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
        if "scripts.terminal_bench.launch stop" in command:
            await asyncio.sleep(.01)
            self.cleanup_complete = not self.fail_cleanup
            return SimpleNamespace(return_code=2 if self.fail_cleanup else 0,
                                   stdout=json.dumps({"quiescent": not self.fail_cleanup, "started": self.started.is_set()}), stderr="")
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def download_file(self, source, target):
        assert self.cleanup_complete, "grading artifact download cannot precede owned shutdown"
        assert source.endswith("execution.json"), "raw service state may not be exported"
        self.downloaded = True
        Path(target).write_text(json.dumps(self.report))


@pytest.mark.asyncio
async def test_verified_wheel_and_only_wrapper_are_installed(tmp_path, wheel, monkeypatch):
    agent = make_agent(tmp_path, wheel)
    env = Environment()
    dependencies = []

    async def ensure(environment, names):
        assert environment is env
        dependencies.extend(names)

    monkeypatch.setattr(agent, "ensure_system_dependencies", ensure)
    await agent.install(env)
    assert set(dependencies) == {"python3", "python_venv"}
    transferred = next(iter(env.uploads.values()))
    assert set(transferred) == {
        "scripts/__init__.py", "scripts/terminal_bench/__init__.py",
        "scripts/terminal_bench/profile.py", "scripts/terminal_bench/control.py",
        "scripts/terminal_bench/runner.py", "scripts/terminal_bench/launch.py", "scripts/terminal_bench/credential.py",
    }
    assert agent.provenance["wheel_sha256"] == wheel[1]
    assert agent.provenance["avibe_version"] == "9.9.9"
    assert agent.capabilities.atif is False
    assert any("-m pip install" in command and wheel[0].name in command for command, _ in env.commands)
    assert not any("editable" in command or "PYTHONPATH" in command for command, _ in env.commands)


@pytest.mark.asyncio
async def test_nonroot_task_refused_without_silent_ownership_change(tmp_path, wheel, monkeypatch):
    agent = make_agent(tmp_path, wheel)
    env = Environment()

    async def nonroot(command, **kwargs):
        assert command == "id -u"
        return SimpleNamespace(return_code=0, stdout="1000\n", stderr="")

    monkeypatch.setattr(env, "exec", nonroot)
    with pytest.raises(RuntimeError, match="root task user"):
        await agent.install(env)
    assert env.files == {} and env.uploads == {}


@pytest.mark.asyncio
async def test_hash_mismatch_fails_before_task_mutation(tmp_path, wheel):
    agent = make_agent(tmp_path, wheel, wheel_sha256="0" * 64)
    env = Environment()
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        await agent.install(env)
    assert env.commands == [] and env.files == {} and env.uploads == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel, fail_cleanup", [(False, False), (True, False), (True, True)])
async def test_run_joins_cancel_before_artifact_handoff(tmp_path, wheel, monkeypatch, cancel, fail_cleanup):
    agent = make_agent(tmp_path, wheel)
    agent.provenance = {"wheel_sha256": wheel[1], "atif": False}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-real-credential")
    env = Environment(block=cancel, fail_cleanup=fail_cleanup)
    ctx = AgentContext()
    pending = asyncio.create_task(agent.run("Write the requested note.", env, ctx))
    if cancel:
        await asyncio.wait_for(env.started.wait(), 2)
        pending.cancel()
        with pytest.raises(RuntimeError if fail_cleanup else asyncio.CancelledError):
            await pending
    else:
        await pending
    if fail_cleanup:
        assert not env.downloaded
    else:
        assert env.cleanup_complete and env.downloaded
        assert ctx.n_input_tokens == 100
        assert ctx.n_cache_tokens == 25
        assert ctx.n_output_tokens == 30
        assert ctx.cost_usd is None
        assert ctx.metadata["accounting_complete"] is False
        assert ctx.metadata["verifier_reward"] is None
    start_command, options = next((cmd, opts) for cmd, opts in env.commands
                                  if "scripts.terminal_bench.launch run" in cmd)
    assert "not-real-credential" not in start_command
    assert options.get("env", {}) == {}
    assert "--credential-file" in start_command
    assert env.secret_upload_modes == (0o600, 0o700)
    assert env.key_removed
    assert all("not-real-credential" not in cmd + json.dumps(opts) for cmd, opts in env.commands)
    assert options["cwd"] == str(agent.root)
    assert "--sandbox-environment" in start_command
    assert "--workdir /app" in start_command
    assert options["timeout_sec"] > agent.profile.wall_seconds + agent.profile.cleanup_seconds
    assert not any("not-real-credential".encode() in data for data in env.files.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_upload_failure_or_cancel_joins_then_removes_staged_key(tmp_path, wheel, monkeypatch, cancel):
    agent = make_agent(tmp_path, wheel)
    agent.provenance = {"wheel_sha256": wheel[1]}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-real-credential")
    env = Environment()
    env.upload_error = not cancel
    env.block_upload = cancel
    pending = asyncio.create_task(agent.run("fixture", env, AgentContext()))
    if cancel:
        await asyncio.wait_for(env.upload_started.wait(), 2)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not env.key_removed, "in-flight transfer must settle before cleanup"
        env.allow_upload.set()
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await pending
    assert env.key_removed
    assert not env.started.is_set()
    assert not env.downloaded
    assert not any("not-real-credential".encode() in data for data in env.files.values())
    assert all("not-real-credential" not in cmd + json.dumps(opts) for cmd, opts in env.commands)


def test_explicit_model_must_match_profile(tmp_path, wheel):
    with pytest.raises(ValueError, match="model_name"):
        make_agent(tmp_path, wheel, model_name="different-model")
    with pytest.raises(ValueError, match="host environment"):
        make_agent(tmp_path, wheel, extra_env={"DEEPSEEK_API_KEY": "sentinel-not-config-or-argv"})
