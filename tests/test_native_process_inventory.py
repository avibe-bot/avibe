"""Configured script identities must survive interpreter and config wrappers."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from config import paths
from config.v2_compat import to_app_config
from config.v2_config import V2Config
from core.agent_auth_service import AgentAuthService
from core.backend_restart import (
    NativeMigrationBlockedError,
    _native_process_backend,
    native_cli_processes,
)
from core.handlers.model_hub.service import ModelHubError, V2ModelHubConfigStore
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import _isolate_native_home, _service
from tests.test_native_takeover_lifecycle import controller_fixture


def _forbidden(*args, **kwargs):
    raise AssertionError("real native process or credential access is forbidden")


@pytest.fixture(autouse=True)
def native_boundary(monkeypatch, tmp_path):
    _isolate_native_home(monkeypatch, tmp_path / "native")
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess.Popen, "__init__", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbidden)
    monkeypatch.setattr(psutil, "process_iter", _forbidden)
    monkeypatch.setattr(psutil.Process, "kill", _forbidden)
    monkeypatch.setattr(psutil.Process, "terminate", _forbidden)
    monkeypatch.setattr(paths, "get_config_path", lambda: tmp_path / "config.json")


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("prefix", [
    ["python"], ["/usr/bin/python3"], ["python3.12"], ["pypy3"],
    ["python3", "-I", "-u"], ["python3", "-X", "dev"],
    ["python3", "-W", "ignore", "-Xdev"],
    ["python3", "--check-hash-based-pycs", "always", "--"],
    ["/bin/bash"], ["sh"], ["dash"], ["zsh"], ["ksh"],
    ["bash", "-"], ["zsh", "-"],
    ["bash", "-eu"], ["bash", "--noprofile", "--norc"],
    ["bash", "-o", "pipefail", "-O", "extglob"],
    ["bash", "--rcfile", "/fixture/rc", "--"],
    ["node"], ["node", "--require", "/fixture/bootstrap.js"],
    ["node", "--import", "/fixture/bootstrap.mjs", "--"],
    ["node", "--watch-path", "/fixture/watch"],
    ["bun", "--preload", "/fixture/bootstrap.js"],
    ["bun", "run", "--"],
])
def test_configured_script_is_the_executed_entrypoint(backend, prefix):
    script = f"/fixture/custom-{backend}"
    assert _native_process_backend([*prefix, script, "--prompt", "other"], {backend: script}) == backend


@pytest.mark.parametrize("command", [
    ["python", "/fixture/other.py", "SCRIPT"],
    ["python", "-c", "pass", "SCRIPT"],
    ["python", "-Ic", "pass", "SCRIPT"],
    ["python", "-m", "unrelated.module", "SCRIPT"],
    ["python", "-Imunrelated.module", "SCRIPT"],
    ["python", "-", "SCRIPT"],
    ["python", "-X", "SCRIPT", "/fixture/other.py"],
    ["python", "-W", "SCRIPT", "/fixture/other.py"],
    ["bash", "/fixture/other.sh", "SCRIPT"],
    ["bash", "-c", "echo ordinary", "SCRIPT"],
    ["bash", "-lc", "echo ordinary", "SCRIPT"],
    ["sh", "-s", "SCRIPT"],
    ["node", "/fixture/other.js", "SCRIPT"],
    ["node", "-e", "codex", "SCRIPT"],
    ["node", "--eval=codex", "SCRIPT"],
    ["node", "-p", "codex", "SCRIPT"],
    ["node", "--require", "/fixture/bootstrap.js", "/fixture/other.js", "SCRIPT"],
    ["node", "-", "SCRIPT"],
    ["/bin/echo", "SCRIPT"],
])
def test_prompt_data_and_non_script_modes_are_not_executable_identity(command):
    script = "/fixture/custom-codex"
    command = [script if value == "SCRIPT" else value for value in command]
    assert _native_process_backend(command, {"codex": script}) is None


@pytest.mark.parametrize("interpreter", ["python.exe", "pythonw.exe", "node.exe", "bash.exe"])
def test_windows_style_interpreter_paths_keep_configured_script(interpreter):
    script = r"C:\fixture\custom-codex"
    assert _native_process_backend(
        [rf"C:\runtime\{interpreter}", script], {"codex": script},
    ) == "codex"


def test_ambiguous_interpreter_options_cannot_certify_native_absence():
    script = "/fixture/custom-codex"
    with pytest.raises(NativeMigrationBlockedError, match="process_inventory_unavailable"):
        _native_process_backend(["python", "--unknown-option", "value", script], {"codex": script})
    assert _native_process_backend(
        ["python", "--unknown-option", "value", "/fixture/unrelated.py"], {"codex": script},
    ) is None


def _configured_owner(config):
    controller, coordinator, admissions, turns = controller_fixture()
    controller.config = config
    controller.agent_auth_service = AgentAuthService(controller)
    return controller, coordinator, admissions, turns


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("shape", ["raw", "enabled-compat", "disabled-compat"])
def test_inventory_uses_existing_configured_binary_owner(backend, shape):
    config = V2Config.default()
    native = getattr(config.agents, backend)
    native.cli_path = f"/fixture/custom-{backend}"
    native.enabled = shape != "disabled-compat"
    config.save()
    _, coordinator, _, _ = _configured_owner(config if shape == "raw" else to_app_config(config))
    assert coordinator._native_binaries((backend,)) == {backend: native.cli_path}


def _row(command, *, pid=123):
    return SimpleNamespace(info={
        "pid": pid, "uids": SimpleNamespace(real=os.getuid()),
        "name": Path(command[0]).name, "cmdline": command, "status": "running",
    })


@pytest.mark.parametrize("boundary", ["entry", "final_idle"])
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("prefix", [
    ["python3", "-X", "dev"], ["bash", "-eu"], ["bash", "-"], ["zsh", "-"],
    ["node", "--require", "/fixture/preload.js"], ["bun", "run"],
    ["python", "--unknown-option", "value"],
])
def test_real_guard_blocks_wrapped_cli_before_takeover(monkeypatch, tmp_path, backend, boundary, enabled, prefix):
    config = V2Config.default()
    native = getattr(config.agents, backend)
    native.cli_path = f"/fixture/custom-{backend}"
    native.enabled = enabled
    config.model_hub.agents[backend].mode = "direct"
    if backend != "opencode":
        native.auth_mode = "api_key"
        native.api_key = "fixture-native-key"
        native.base_url = "https://fixture.example"
    else:
        auth = Path.home() / ".config/opencode/opencode.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"provider":{"openai":{"options":{"apiKey":"fixture-native-key"}}}}')
    config.save()
    service, _, adapter = _service(tmp_path, migration_home=Path.home())
    service.store = V2ModelHubConfigStore()
    controller, coordinator, admissions, turns = _configured_owner(to_app_config(config))
    if not enabled:
        controller.agent_service.agents.pop(backend)
    controller.model_hub_service = service
    coordinator._process_inventory = native_cli_processes
    service.migration_guard = coordinator.migration_guard
    calls = []

    def inventory(attrs):
        calls.append(tuple(attrs))
        assert "environ" not in attrs
        if boundary == "final_idle" and len(calls) == 1:
            return []
        return [_row([*prefix, native.cli_path])]

    monkeypatch.setattr(psutil, "process_iter", inventory)
    before = paths.get_config_path().read_bytes()
    ids = [row["id"] for row in service.migration_scan()["items"] if row["backend"] == backend]
    assert ids
    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(ids))
    assert error.value.code == "migration_native_busy"
    assert len(calls) == (1 if boundary == "entry" else 2)
    assert paths.get_config_path().read_bytes() == before
    assert service.migration_journal.load() is None
    assert not service.migration_blocked_backends and not admissions and not turns
    if boundary == "entry":
        assert not adapter.provisioned
    else:
        assert len(adapter.provisioned) == len(adapter.revoked) == 1
    if backend == "opencode":
        assert "fixture-native-key" in auth.read_text()
    controller.agent_service.force_cancel_backend_turns.assert_not_awaited()
    assert isinstance(coordinator._refresh, Mock)
    coordinator._refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "opencode"])
@pytest.mark.parametrize("boundary", ["entry", "final_idle"])
@pytest.mark.parametrize("failure", ["read-error", "recovery-defaults", "binary-recovery"])
async def test_real_guard_refuses_unreadable_disabled_binary_config(monkeypatch, backend, boundary, failure):
    config = V2Config.default()
    native = getattr(config.agents, backend)
    native.enabled = False
    native.cli_path = f"/fixture/custom-{backend}"
    config.save()
    controller, coordinator, admissions, turns = _configured_owner(to_app_config(config))
    coordinator._process_inventory = native_cli_processes
    inventory = Mock(return_value=[])
    monkeypatch.setattr(psutil, "process_iter", inventory)

    def fail_load(*args, **kwargs):
        if failure == "read-error":
            raise OSError("fixture-sensitive-path")
        recovered = V2Config.default()
        if failure == "recovery-defaults":
            recovered.whole_config_recovery = True
        else:
            recovered.recovered_sections = (f"agents.{backend}.cli_path",)
        return recovered

    if boundary == "entry":
        monkeypatch.setattr(V2Config, "load", fail_load)
    with pytest.raises(NativeMigrationBlockedError, match="^process_inventory_unavailable$") as error:
        async with coordinator.migration_guard((backend,)) as verify_idle:
            monkeypatch.setattr(V2Config, "load", fail_load)
            inventory.return_value = [_row([native.cli_path])]
            await verify_idle()
    assert error.value.backends == (backend,)
    assert inventory.call_count == (0 if boundary == "entry" else 1)
    assert not admissions and not turns
    controller.agent_service.force_cancel_backend_turns.assert_not_awaited()


@pytest.mark.parametrize("backend", ["codex", "opencode"])
def test_settings_binary_resolution_retains_best_effort_default(monkeypatch, backend):
    config = V2Config.default()
    getattr(config.agents, backend).enabled = False
    controller, _, _, _ = _configured_owner(to_app_config(config))
    monkeypatch.setattr(V2Config, "load", Mock(side_effect=OSError("fixture-unreadable")))
    assert controller.agent_auth_service._get_cli_binary(backend) == backend


def test_strict_binary_resolution_keeps_unrelated_recovery_out_of_scope(monkeypatch):
    config = V2Config.default()
    config.agents.codex.enabled = False
    config.agents.codex.cli_path = "/fixture/custom-codex"
    config.recovered_sections = ("agents.claude", "logging")
    _, coordinator, _, _ = _configured_owner(to_app_config(config))
    load = Mock(return_value=config)
    monkeypatch.setattr(V2Config, "load", load)
    assert coordinator._native_binaries(("codex",)) == {"codex": "/fixture/custom-codex"}
    load.assert_called_once_with(persist_migrations=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    ["node", "--eval=0", "SCRIPT"], ["node", "-", "SCRIPT"],
    ["python3", "-m", "unrelated", "SCRIPT"],
    ["bash", "-lc", "echo ordinary", "SCRIPT"],
    ["python3", "/fixture/other.py", "SCRIPT"],
])
async def test_real_guard_does_not_confuse_data_with_a_configured_script(monkeypatch, command):
    config = V2Config.default()
    config.agents.codex.enabled = False
    config.agents.codex.cli_path = script = "/fixture/custom-codex"
    config.save()
    controller, coordinator, admissions, turns = _configured_owner(to_app_config(config))
    command = [script if value == "SCRIPT" else value for value in command]
    inventory = Mock(return_value=[_row(command)])
    monkeypatch.setattr(psutil, "process_iter", inventory)
    coordinator._process_inventory = native_cli_processes
    async with coordinator.migration_guard(("codex",)) as verify_idle:
        await verify_idle()
        assert admissions == turns == {"codex"}
    assert inventory.call_count == 2
    assert not admissions and not turns
    controller.agent_service.force_cancel_backend_turns.assert_not_awaited()
