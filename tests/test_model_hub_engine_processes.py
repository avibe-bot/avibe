"""Native fixture-process tests, distinct from injected journal-fault tests.

The fake CPA HTTP process is actually spawned/reaped on the host. Its launcher
receives the verified generation path but runs a test-owned Python fixture; this
is not a packaged-CPA or physical power-loss certification.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
import pytest

# Dedicated native-platform CI runs these real-child integration cases.
pytestmark = pytest.mark.integration

from tests.test_model_hub_engine_lifecycle import owner_fixture, signed_candidate
from tests.test_model_hub_runtime import _write_mock_engine
from vibe.model_hub_runtime import lifecycle as lifecycle_module
from vibe.model_hub_runtime.state import EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineSupervisor


def real_owner(tmp_path, monkeypatch, *, bundled_version="v8.0.30"):
    original, _recording, baseline = owner_fixture(tmp_path, monkeypatch, bundled_version=bundled_version)
    manager = original.manager
    script = tmp_path / "fixture-cpa.py"
    _write_mock_engine(script)
    state = EngineStateStore(tmp_path / "real-state")
    processes = []
    observations = []
    native_popen = subprocess.Popen

    def spawn(command, *args, **kwargs):
        binary = Path(command[0])
        if (manager.runtime_dir / "versions") not in binary.parents:
            return native_popen(command, *args, **kwargs)
        config = yaml.safe_load(Path(command[-1]).read_text())
        isolated = Path(config["auth-dir"]).resolve() != state.auth_dir.resolve()
        if not isolated:
            assert not any(not prior_isolated and process.poll() is None for process, prior_isolated in processes)
        else:
            home = Path(kwargs["env"]["HOME"])
            assert home.name.startswith("engine-preflight-")
            assert kwargs["env"]["USERPROFILE"] == str(home)
            assert Path(config["auth-dir"]).is_relative_to(home)
            assert not list(Path(config["auth-dir"]).glob("*.json"))
            assert not any(config.get(key) for key in (
                "claude-api-key", "codex-api-key", "openai-compatibility", "gemini-api-key",
            ))
        trace = tmp_path / f"fixture-cpa-{len(processes)}.log"
        with trace.open("wb") as output:
            kwargs.update(stdout=output, stderr=subprocess.STDOUT)
            process = native_popen([sys.executable, str(script), *command[1:]], *args, **kwargs)
        process._fixture_trace = trace
        processes.append((process, isolated))
        observations.append({"binary": binary, "isolated": isolated, "pid": process.pid})
        return process

    supervisor = EngineSupervisor(installer=manager, state_store=state, process_factory=spawn, startup_timeout=15)
    owner = supervisor.lifecycle
    # Preserve the test roots used by the prepared owner, not production trust.
    owner.channel = original.channel
    monkeypatch.setattr(lifecycle_module.subprocess, "Popen", spawn)
    owner.recover()
    return owner, supervisor, baseline, observations, processes


def finish(supervisor, processes):
    failed = sys.exc_info()[0] is not None
    supervisor.stop()
    for process, _isolated in processes:
        if process.poll() is None:
            process.kill()  # only this test's recorded PIDs
        process.wait(timeout=10)
        if failed:
            trace = process._fixture_trace
            print(f"Native test fixture {process.pid}: {trace.read_text(encoding='utf-8', errors='replace')[-4000:]}")


def test_native_fixture_bootstrap_needs_no_host_reverse_dns(tmp_path, monkeypatch):
    owner, supervisor, _baseline, _observations, processes = real_owner(tmp_path, monkeypatch)
    script = tmp_path / "fixture-cpa.py"
    script.write_text(
        "import socket\n"
        "def deny_reverse_dns(*_args):\n"
        "    raise AssertionError('local-only fixture consulted machine DNS')\n"
        "socket.getfqdn = deny_reverse_dns\n" + script.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    try:
        owner.ensure_running()
        assert supervisor.live_connection() is not None
    finally:
        finish(supervisor, processes)


def test_native_launch_witness_recovery_reports_the_reaped_engine(tmp_path, monkeypatch, caplog):
    owner, supervisor, _baseline, _observations, processes = real_owner(tmp_path, monkeypatch)
    try:
        owner.ensure_running()
        first = supervisor._process
        caplog.clear()
        owner.recover()
        assert first.poll() is not None
        assert "Reaped a Model Hub engine left running by an earlier service" in caplog.text
        owner.ensure_running()
        assert supervisor._process.pid != first.pid
        caplog.clear()
        supervisor.stop_for_activation()
        owner.recover()
        assert "Reaped a Model Hub engine left running by an earlier service" not in caplog.text
    finally:
        finish(supervisor, processes)


def test_native_preflight_swap_identity_fence_and_real_process_death(tmp_path, monkeypatch):
    owner, supervisor, baseline, observations, processes = real_owner(tmp_path, monkeypatch)
    try:
        owner.ensure_running()
        first = supervisor._process
        assert first is not None and first.poll() is None
        # Ordinary preflight gets neither this grant nor the production home.
        grant = supervisor.state_store.auth_dir / "test-owner-grant.json"
        grant.write_text('{"credential":"test-only-not-a-user-secret"}')
        grant.chmod(0o600)
        candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
        actual_save = owner._save
        observed_fence = []

        def save(value):
            if value["active"] == candidate.document() and value["pending"] is None:
                snapshot = supervisor.status()
                assert owner.running_release is None
                assert snapshot["running_release"] is None
                assert snapshot["status"]["listening"] is None
                assert snapshot["engine_lifecycle"]["running"] is None
                observed_fence.append(True)
            actual_save(value)

        monkeypatch.setattr(owner, "_save", save)
        owner.activate(candidate)
        monkeypatch.setattr(owner, "_save", actual_save)
        assert observed_fence and first.poll() is not None
        assert any(item["isolated"] for item in observations)
        assert supervisor.status()["running_release"] == candidate.asset_release_tag
        assert owner.journal.load()["active"] == candidate.document()
        assert owner.journal.load()["previous"] == baseline["generation"]
        # This is a real child death, not an injected stop/start/health result.
        killed = supervisor._process
        killed.kill()
        killed.wait(timeout=10)
        owner.ensure_running()
        assert supervisor._process.pid != killed.pid
        assert owner.running_release == candidate.asset_release_tag
        assert owner.journal.load()["active"] == candidate.document()
    finally:
        finish(supervisor, processes)
