"""Hosted readiness using owned Python children, never local Apple tools.

The old 30s inventory-only path had no boot bound or durable failure evidence.
These tests protect that boundary, including CLI stdout and cleanup; the existing
session tests independently cover residual descendants in the shared group reaper.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.test_ios_direct_interop_gate import runner

RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-5"
FIRST = "00000000-0000-0000-0000-000000000001"
SECOND = "00000000-0000-0000-0000-000000000002"


def inventory():
    return {"devices": {
        RUNTIME: [{"name": "iPhone 17 Pro", "isAvailable": True, "udid": SECOND},
                  {"name": "iPhone 17 Pro", "isAvailable": True, "udid": FIRST},
                  {"name": "iPhone 17 Pro", "isAvailable": False, "udid": "unavailable"},
                  {"name": "iPhone 17", "isAvailable": True, "udid": "other-device"}],
        "com.apple.CoreSimulator.SimRuntime.iOS-26-6": [
            {"name": "iPhone 17 Pro", "isAvailable": True, "udid": "other-runtime"}],
    }}


@pytest.fixture
def fake_hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "hosted_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "merged_source", lambda: {"head": "a" * 40, "merged_base": "a" * 40})
    real_spawn = subprocess.Popen
    scripts = {"inventory": f"print({json.dumps(inventory())!r})", "boot": "print('boot ready')"}
    commands = []

    def spawn(command, **kwargs):
        commands.append(tuple(command))
        assert tuple(command[:2]) == ("xcrun", "simctl")
        stage = "inventory" if command[2] == "list" else "boot"
        assert kwargs["start_new_session"] is True
        return real_spawn([sys.executable, "-c", scripts[stage]], **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    return scripts, commands, tmp_path / "simulator-readiness"


def test_cli_selects_only_pinned_device_and_prints_after_readiness(fake_hosted, monkeypatch, capsys):
    _, commands, directory = fake_hosted
    monkeypatch.setattr(sys, "argv", ["run_direct_interop.py", "simulator"])

    def old_inventory(*args, **kwargs):
        # The pre-fix CLI fails here; the corrected CLI owns a 120s async command.
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runner.subprocess, "check_output", old_inventory)
    runner.main()
    assert capsys.readouterr().out == FIRST + "\n"
    assert commands == [("xcrun", "simctl", "list", "devices", "available", "--json"),
                        ("xcrun", "simctl", "bootstatus", FIRST, "-b")]
    for stage, bound in [("inventory", 120), ("boot", 600)]:
        record = json.loads((directory / f"{stage}.json").read_text())
        assert record["timeout_seconds"] == bound and record["status"] == "passed"
        assert record["pid"] == record["process_group"] and record["exit_code"] == 0
        assert record["elapsed_seconds"] >= 0
        with pytest.raises(ProcessLookupError):
            os.kill(record["pid"], 0)
    assert json.loads((directory / "readiness.json").read_text())["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "boot"])
async def test_timeout_retains_diagnostics_reaps_and_never_retries(fake_hosted, monkeypatch, stage):
    scripts, commands, directory = fake_hosted
    scripts[stage] = "import sys,time; print('waiting', flush=True); print('diagnostic', file=sys.stderr, flush=True); time.sleep(60)"
    original = runner.simulator_command

    async def shorter(directory, name, command, timeout):
        assert timeout == (120 if name == "inventory" else 600)
        await original(directory, name, command, 0.5 if name == stage else timeout)

    monkeypatch.setattr(runner, "simulator_command", shorter)
    with pytest.raises(runner.host.HostFault, match=f"{stage} failed.*TimeoutError"):
        await runner.ready_simulator()
    assert len(commands) == (1 if stage == "inventory" else 2)
    record = json.loads((directory / f"{stage}.json").read_text())
    assert record["failure"] == "TimeoutError" and record["status"] == "failed"
    assert record["exit_code"] is not None
    assert (directory / f"{stage}.stdout").read_text() == "waiting\n"
    assert (directory / f"{stage}.stderr").read_text() == "diagnostic\n"
    final = json.loads((directory / "readiness.json").read_text())
    assert final["status"] == "failed" and final["stage"] == stage
    with pytest.raises(ProcessLookupError):
        os.killpg(record["process_group"], 0)


@pytest.mark.parametrize("listing", ["not-json", "[]", '{"devices":[]}', '{"devices":{}}',
    json.dumps({"devices": {RUNTIME: [{"name": "iPhone 17", "isAvailable": True, "udid": FIRST}]}}),
    json.dumps({"devices": {RUNTIME: [{"name": "iPhone 17 Pro", "isAvailable": False, "udid": FIRST}]}}),
    json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-6": inventory()["devices"][RUNTIME]}}),
])
def test_invalid_or_missing_inventory_never_boots_or_prints(fake_hosted, monkeypatch, capsys, listing):
    scripts, commands, directory = fake_hosted
    scripts["inventory"] = f"print({listing!r})"
    monkeypatch.setattr(sys, "argv", ["run_direct_interop.py", "simulator"])
    with pytest.raises((runner.host.HostFault, json.JSONDecodeError)):
        runner.main()
    assert capsys.readouterr().out == ""
    assert len(commands) == 1
    assert (directory / "inventory.stdout").read_text().strip() == listing
    final = json.loads((directory / "readiness.json").read_text())
    assert final["status"] == "failed" and final["stage"] == "selection"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inventory", "boot"])
async def test_nonzero_exit_is_failure_with_reaped_child(fake_hosted, stage):
    scripts, _, directory = fake_hosted
    scripts[stage] = "import sys; print('simctl unavailable', file=sys.stderr); sys.exit(7)"
    with pytest.raises(runner.host.HostFault):
        await runner.ready_simulator()
    record = json.loads((directory / f"{stage}.json").read_text())
    assert record["exit_code"] == 7 and record["status"] == "failed"
    assert "exited 7" in record["detail"]
    assert json.loads((directory / "readiness.json").read_text())["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_output_is_capped_and_overflow_fails_closed(fake_hosted, stream):
    scripts, commands, directory = fake_hosted
    scripts["inventory"] = f"import sys; sys.{stream}.write('x' * (1024 * 1024 + 1))"
    with pytest.raises(runner.host.HostFault):
        await runner.ready_simulator()
    assert (directory / f"inventory.{stream}").stat().st_size == 1024 * 1024
    assert len(commands) == 1
    record = json.loads((directory / "inventory.json").read_text())
    assert "exceeded 1 MiB" in record["detail"]


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_term_kill_and_preserves_failure(fake_hosted, monkeypatch):
    scripts, commands, directory = fake_hosted
    scripts["inventory"] = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    original = runner.wait_group_empty
    bounds = []

    async def faster(group, seconds):
        bounds.append(seconds)
        await original(group, 0.2 if seconds == 5 else seconds)

    monkeypatch.setattr(runner, "wait_group_empty", faster)
    task = asyncio.create_task(runner.ready_simulator())
    async with asyncio.timeout(5):
        while not (directory / "inventory.stdout").exists() or not (directory / "inventory.stdout").read_bytes():
            await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bounds == [5, 3] and len(commands) == 1
    record = json.loads((directory / "inventory.json").read_text())
    assert record["failure"] == "CancelledError" and record["exit_code"] is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(record["process_group"], 0)
    assert json.loads((directory / "readiness.json").read_text())["status"] == "failed"


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_yield_ready(fake_hosted, monkeypatch):
    _, commands, directory = fake_hosted
    original = runner.close_process_group

    async def fail_after_reaping(process):
        await original(process)
        raise TimeoutError("cleanup boundary")

    monkeypatch.setattr(runner, "close_process_group", fail_after_reaping)
    with pytest.raises(runner.host.HostFault):
        await runner.ready_simulator()
    assert len(commands) == 1
    record = json.loads((directory / "inventory.json").read_text())
    assert record["cleanup_error"] == "TimeoutError: cleanup boundary"
    assert record["status"] == "failed"


@pytest.mark.parametrize("availability", ["false", "true", 0, 1, 0.0, None, [], {}])
def test_availability_must_be_an_exact_boolean(fake_hosted, monkeypatch, capsys, availability):
    scripts, commands, directory = fake_hosted
    listing = {"devices": {RUNTIME: [{"name": "iPhone 17 Pro", "isAvailable": availability, "udid": FIRST}]}}
    scripts["inventory"] = f"print({json.dumps(listing)!r})"
    monkeypatch.setattr(sys, "argv", ["run_direct_interop.py", "simulator"])
    with pytest.raises(runner.host.HostFault, match="availability must be Boolean"):
        runner.main()
    assert commands == [("xcrun", "simctl", "list", "devices", "available", "--json")]
    assert capsys.readouterr().out == ""
    assert json.loads((directory / "readiness.json").read_text())["stage"] == "selection"


@pytest.mark.parametrize("udid", [None, 1, "", "not-a-uuid", "--help"])
def test_matching_device_requires_a_uuid(udid):
    listing = {"devices": {RUNTIME: [{"name": "iPhone 17 Pro", "isAvailable": True, "udid": udid}]}}
    with pytest.raises(runner.host.HostFault, match="invalid simulator UDID"):
        runner.select_simulator(listing)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
@pytest.mark.parametrize("leader_exits", [False, True])
async def test_live_group_owned_before_delayed_pipe_attachment(fake_hosted, monkeypatch, stop, leader_exits):
    scripts, commands, directory = fake_hosted
    child_pid_file = directory.parent / "descendant-pid"
    leader_pid_file = directory.parent / "leader-pid"
    child = (
        "import os,pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    scripts["inventory"] = f'''
import os,pathlib,subprocess,sys,time
pathlib.Path({str(leader_pid_file)!r}).write_text(str(os.getpid()))
subprocess.Popen([sys.executable, "-c", {child!r}])
while not pathlib.Path({str(child_pid_file)!r}).exists():
    time.sleep(0.01)
if {leader_exits!r}:
    sys.exit(0)
time.sleep(60)
'''
    loop = asyncio.get_running_loop()
    original_connect = loop.connect_read_pipe
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stalled_connect(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_connect(*args, **kwargs)

    monkeypatch.setattr(loop, "connect_read_pipe", stalled_connect)
    original_command = runner.simulator_command
    original_wait = runner.wait_group_empty

    async def shorter(directory, stage, command, timeout):
        await original_command(directory, stage, command, 0.4 if stop == "deadline" else timeout)

    async def faster(group, seconds):
        await original_wait(group, 0.2 if seconds == 5 else seconds)

    monkeypatch.setattr(runner, "simulator_command", shorter)
    monkeypatch.setattr(runner, "wait_group_empty", faster)
    task = asyncio.create_task(runner.ready_simulator())
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            while not child_pid_file.exists():
                await asyncio.sleep(0.01)
            if leader_exits:
                while True:
                    try:
                        os.kill(int(leader_pid_file.read_text()), 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
        if stop == "cancel":
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            with pytest.raises(runner.host.HostFault, match="TimeoutError"):
                await asyncio.wait_for(task, 3)
        record = json.loads((directory / "inventory.json").read_text())
        assert record["pid"] == record["process_group"] and record["pid"] is not None
        assert record["status"] == "failed" and len(commands) == 1
        if leader_exits:
            assert record["exit_code"] == 0
        else:
            assert record["exit_code"] is not None
        with pytest.raises(ProcessLookupError):
            os.kill(int(child_pid_file.read_text()), 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(record["process_group"], 0)
        assert json.loads((directory / "readiness.json").read_text())["status"] == "failed"
    finally:
        # A failing pre-fix regression still retires only its recorded fixture group.
        if leader_pid_file.exists():
            group = int(leader_pid_file.read_text())
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        if leader_pid_file.exists():
            await original_wait(group, 3)


@pytest.mark.asyncio
async def test_overdue_synchronous_spawn_does_not_reset_budget(fake_hosted, monkeypatch):
    _, commands, directory = fake_hosted
    original_spawn = runner.subprocess.Popen
    original_command = runner.simulator_command

    def slow_spawn(*args, **kwargs):
        time.sleep(0.1)
        return original_spawn(*args, **kwargs)

    async def shorter(directory, stage, command, timeout):
        await original_command(directory, stage, command, 0.05)

    monkeypatch.setattr(runner.subprocess, "Popen", slow_spawn)
    monkeypatch.setattr(runner, "simulator_command", shorter)
    with pytest.raises(runner.host.HostFault, match="TimeoutError"):
        await runner.ready_simulator()
    record = json.loads((directory / "inventory.json").read_text())
    assert record["pid"] is not None and record["status"] == "failed"
    assert len(commands) == 1 and "launch exceeded" in record["detail"]
    with pytest.raises(ProcessLookupError):
        os.killpg(record["process_group"], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["attach", "receipt"])
async def test_attachment_or_receipt_failure_retains_owned_cleanup(fake_hosted, monkeypatch, boundary):
    _, commands, directory = fake_hosted
    original_spawn = runner.subprocess.Popen
    original_write = runner.host.write_json
    created = []

    def track_spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        created.append(process)
        return process

    async def broken_attach(*args, **kwargs):
        raise OSError("pipe attachment failed")

    def broken_receipt(path, value):
        if path.name == "inventory.json" and value["status"] == "running":
            raise OSError("running receipt failed")
        original_write(path, value)

    monkeypatch.setattr(runner.subprocess, "Popen", track_spawn)
    if boundary == "attach":
        monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", broken_attach)
    else:
        monkeypatch.setattr(runner.host, "write_json", broken_receipt)
    with pytest.raises(runner.host.HostFault, match="OSError"):
        await runner.ready_simulator()
    assert len(commands) == len(created) == 1
    process = created[0]
    assert process.returncode is not None and process.stdout.closed and process.stderr.closed
    record = json.loads((directory / "inventory.json").read_text())
    assert record["pid"] == process.pid and record["status"] == "failed"
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


@pytest.mark.asyncio
async def test_spawn_failure_has_terminal_evidence_without_a_pid(fake_hosted, monkeypatch):
    _, commands, directory = fake_hosted

    def missing(*args, **kwargs):
        raise FileNotFoundError("simctl fixture unavailable")

    monkeypatch.setattr(runner.subprocess, "Popen", missing)
    with pytest.raises(runner.host.HostFault, match="FileNotFoundError"):
        await runner.ready_simulator()
    record = json.loads((directory / "inventory.json").read_text())
    assert record["pid"] is None and record["exit_code"] is None
    assert record["status"] == "failed" and commands == []
    assert json.loads((directory / "readiness.json").read_text())["status"] == "failed"


@pytest.mark.asyncio
async def test_exited_leader_with_inherited_pipes_cannot_escape_cleanup(fake_hosted, monkeypatch):
    scripts, _, directory = fake_hosted
    scripts["inventory"] = (
        "import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
        "\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('descendant ready', flush=True); time.sleep(60)\"])"
    )
    original_command = runner.simulator_command
    original_wait = runner.wait_group_empty

    async def shorter(directory, stage, command, timeout):
        await original_command(directory, stage, command, 0.5)

    async def faster(group, seconds):
        await original_wait(group, 0.2 if seconds == 5 else seconds)

    monkeypatch.setattr(runner, "simulator_command", shorter)
    monkeypatch.setattr(runner, "wait_group_empty", faster)
    with pytest.raises(runner.host.HostFault, match="TimeoutError"):
        await runner.ready_simulator()
    record = json.loads((directory / "inventory.json").read_text())
    assert record["exit_code"] == 0 and record["status"] == "failed"
    assert (directory / "inventory.stdout").read_text() == "descendant ready\n"
    with pytest.raises(ProcessLookupError):
        os.killpg(record["process_group"], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["hosted_root", "merged_source"])
async def test_fences_precede_any_simulator_command(fake_hosted, monkeypatch, guard):
    _, commands, directory = fake_hosted

    def refuse():
        raise runner.host.HostFault("fence refused")

    monkeypatch.setattr(runner, guard, refuse)
    with pytest.raises(runner.host.HostFault, match="fence refused"):
        await runner.ready_simulator()
    assert commands == [] and not directory.exists()
