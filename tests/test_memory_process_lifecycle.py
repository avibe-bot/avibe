"""MEMORY-WAKE-204: native lifecycle and deterministic generation boundaries."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
from types import SimpleNamespace

import psutil
import pytest

import avibe_memory.process as module

pytestmark = pytest.mark.asyncio


async def spawn(tmp_path, code="import time; time.sleep(60)"):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        code,
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "PATH": os.defpath,
        },
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )


async def dispose(child):
    if child.returncode is None:
        child.kill()
    await child.wait()


def settings():
    return module.EverOSProcessSettings(
        llm_base_url="http://127.0.0.1:1/v1",
        llm_model="test",
        llm_api_key="test",
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_model="test",
        embedding_api_key="test",
    )


async def test_native_shift_survives_full_scans_and_stop(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("Darwin native fault")
    from psutil import _psosx

    host = module._SystemProcessHost()
    child = await spawn(tmp_path, "import sys; [print(line.strip().upper(), flush=True) for line in sys.stdin]")
    owner = module.EverOSProcess(sys.executable, effective_home=tmp_path, _host=host)
    owner._process = child
    owner._process_group = host.process_group(child.pid)
    owner._owned_processes = host.snapshot_tree(child.pid, owner._process_group)
    owner._desired_running = True
    original = owner._owned_processes[child.pid]
    stamp = psutil.Process(child.pid).create_time()
    notifications = []
    owner._on_unexpected_exit = lambda: notifications.append("wake")
    monitor = asyncio.create_task(owner._monitor_child(child))
    try:
        monkeypatch.setattr(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME + 1)
        assert psutil.Process(child.pid).create_time() == stamp + 1
        await asyncio.sleep(3.2)  # More than three unshortened production scans.
        child.stdin.write(b"new input\n")
        await child.stdin.drain()
        assert await asyncio.wait_for(child.stdout.readline(), 2) == b"NEW INPUT\n"
        assert child.returncode is None
        assert notifications == []
        assert owner._owned_processes[child.pid] is original
        await owner._terminate_owned_tree(
            child, process_group=owner._process_group, owned_processes=owner._owned_processes
        )
        assert child.returncode is not None
        assert not original.is_running()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await dispose(child)


async def test_native_probe_timeout_during_shift(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("version-bound Darwin boot-time fault injection")
    from psutil import _psosx

    class Host(module._SystemProcessHost):
        async def spawn(self, *args, **kwargs):
            self.child = await spawn(tmp_path)
            return self.child

        def capture(self, pid):
            reference = super().capture(pid)
            stamp = reference.create_time()
            monkeypatch.setattr(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME + 1)
            assert psutil.Process(pid).create_time() == stamp + 1
            return reference

    host = Host()
    owner = module.EverOSProcess(
        sys.executable, effective_home=tmp_path, settings=settings(), _host=host, stop_timeout_seconds=0.2
    )
    monkeypatch.setattr(module, "_processing_probe_timeout_seconds", lambda _: 0.1)
    assert not await owner.processing_healthy()
    assert host.child.returncode is not None


@pytest.mark.parametrize("consumer", ["stop", "start_failure", "probe_timeout"])
@pytest.mark.parametrize("transient_read", ["create_time", "is_running"])
async def test_capture_keeps_reference_through_transient_read_failure(
    tmp_path, monkeypatch, consumer, transient_read,
):
    with tempfile.TemporaryDirectory(prefix="mc1990-", dir="/tmp") as temporary:
        tmp_path = Path(temporary).resolve()
        (tmp_path / "memory/everos-root").mkdir(parents=True, mode=0o700)
        (tmp_path / "memory").chmod(0o700)

        class Host(module._SystemProcessHost):
            async def spawn(self, *args, **kwargs):
                self.child = await spawn(tmp_path)
                return self.child

            def capture(self, pid):
                reference = psutil.Process(pid)
                self.reference = reference

                def inaccessible():
                    raise psutil.AccessDenied(pid)

                # Only the capture boundary loses visibility; it must not discard
                # a successfully constructed reference because of an extra read.
                with monkeypatch.context() as fault:
                    fault.setattr(reference, transient_read, inaccessible)
                    fault.setattr(psutil, "Process", lambda target: reference)
                    captured = super().capture(pid)
                assert captured is reference
                return captured

            def signal(self, identities, signum, **kwargs):
                assert identities[self.child.pid] is self.reference
                super().signal(identities, signum, **kwargs)

        host = Host()
        owner = module.EverOSProcess(
            sys.executable, effective_home=tmp_path, settings=settings(), _host=host,
            startup_timeout_seconds=0.1, stop_timeout_seconds=0.2,
            provider_root_guard=lambda: None,
        )
        try:
            if consumer == "probe_timeout":
                monkeypatch.setattr(module, "_processing_probe_timeout_seconds", lambda _: 0.1)
                assert not await owner.processing_healthy()
            elif consumer == "start_failure":
                # Real start/record/failed-readiness cleanup, with a sleeping test child.
                assert not await owner.start()
            else:
                owner._process = await host.spawn()
                owner._process_group = host.process_group(host.child.pid)
                owner._owned_processes = {host.child.pid: host.capture(host.child.pid)}
                await owner.stop()
            assert host.child.returncode is not None
            assert not host.reference.is_running()
            assert not owner.retains_active_config
        finally:
            if hasattr(host, "child"):
                await dispose(host.child)


async def test_probe_without_initial_reference_reports_incomplete_cleanup(tmp_path, monkeypatch):
    class Host(module._SystemProcessHost):
        async def spawn(self, *args, **kwargs):
            self.child = await spawn(tmp_path)
            return self.child

        def capture(self, pid):
            return None

    host = Host()
    owner = module.EverOSProcess(
        sys.executable, effective_home=tmp_path, settings=settings(), _host=host,
        stop_timeout_seconds=0.1,
    )
    monkeypatch.setattr(module, "_processing_probe_timeout_seconds", lambda _: 0.1)
    try:
        assert not await owner.processing_healthy()
        assert host.child.returncode is None  # Unknown is neither authority nor cleanup.
    finally:
        await dispose(host.child)


async def test_native_orphan_reaper_retains_record_through_shift(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("Darwin native fault")
    from psutil import _psosx

    # A test-owned module has the released CLI/environment shape. Classification,
    # retained references, signals, waits and record retirement are production code.
    package = tmp_path / "avibe_memory"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "sidecar.py").write_text("import time; time.sleep(60)")
    root = tmp_path / "memory/everos-root"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    socket_path = tmp_path / "memory/.rt/everos.sock"
    record_path = socket_path.with_suffix(".sidecar.json")
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "avibe_memory.sidecar", "--uds", str(socket_path),
        cwd=tmp_path, start_new_session=True,
        env={"HOME": str(tmp_path), "EVEROS_ROOT": str(root), "AVIBE_MEMORY_CHILD_ROLE": "sidecar"},
    )
    stamp = psutil.Process(child.pid).create_time()

    class Host(module._SystemProcessHost):
        rounds = 0

        def signal(self, identities, signum, **kwargs):
            self.rounds += 1
            assert record_path.exists()
            if self.rounds == 1:
                monkeypatch.setattr(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME + 1)
                assert psutil.Process(child.pid).create_time() == stamp + 1
                return  # First bounded wait must not retire this live execution.
            super().signal(identities, signum, **kwargs)

        async def wait_for_exit(self, identities, timeout_seconds, **kwargs):
            result = await super().wait_for_exit(identities, timeout_seconds, **kwargs)
            assert record_path.exists()
            assert result is (self.rounds == 2)
            return result

    host = Host()
    ownership = module.SidecarOwnership(
        record_path=record_path, socket_path=socket_path, provider_root=root,
        python=Path(sys.executable), stop_timeout_seconds=0.1, _host=host,
    )
    try:
        ownership.record_launch(child.pid, stamp, child.pid)
        await ownership.reap()
        await child.wait()
        assert host.rounds == 2
        assert not record_path.exists()
    finally:
        await dispose(child)


async def test_real_tcp_listener_is_rejected(tmp_path):
    child = await spawn(
        tmp_path,
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print('ready',flush=True); time.sleep(60)",
    )
    try:
        assert await child.stdout.readline() == b"ready\n"
        host = module._SystemProcessHost()
        assert host.has_tcp_listener(host.snapshot_tree(child.pid, host.process_group(child.pid)))
    finally:
        await dispose(child)


class Generation:
    """Controlled kernel generation; replacing the table entry is actual model reuse."""

    def __init__(self, kernel, pid):
        self.kernel, self.pid = kernel, pid
        self.birth = object()
        self.terminal = False
        self.denied = False
        self.signals = []
        self.descendants = []
        kernel[pid] = self

    def is_running(self):
        if self.kernel.get(self.pid) is not self:
            self.terminal = True
        return not self.terminal

    def status(self):
        if self.denied:
            raise psutil.AccessDenied(self.pid)
        return "running"

    def children(self, recursive=False):
        return self.descendants

    def send_signal(self, signum):
        assert self.is_running(), "signal attempted on a replacement generation"
        self.signals.append(signum)

    def net_connections(self, kind):
        assert self.is_running(), "inspected replacement generation"
        return []


@pytest.mark.parametrize("captured", [True, False])
async def test_reused_pid_stays_terminal_during_delayed_child_callback(monkeypatch, captured):
    kernel = {}
    old = Generation(kernel, 451)
    retained = old if captured else None
    owned = {451: retained}
    replacement = Generation(kernel, 451)
    host = module._SystemProcessHost()
    callback = asyncio.Event()

    async def wait():
        await callback.wait()
        return 0

    child = SimpleNamespace(pid=451, returncode=None, wait=wait)
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {})
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid in kernel)
    host.snapshot_tree(451, None, owned)
    host.signal(owned, signal.SIGTERM, process=child)
    assert replacement.birth is not old.birth
    assert not old.signals and not replacement.signals
    assert owned[451] is retained
    if captured:
        assert not host.has_tcp_listener(owned)
    else:
        with pytest.raises(RuntimeError, match="listeners"):
            host.has_tcp_listener(owned)
    assert not await host.wait_for_exit(owned, 0.1, process=child)
    callback.set()
    if not captured:
        assert not await host.wait_for_exit(owned, 0.1, process=child)
        kernel.clear()
    assert await host.wait_for_exit(owned, 0.2, process=child)


async def test_unknown_group_and_retained_descendant_prevent_false_cleanup(monkeypatch):
    kernel = {}
    root = Generation(kernel, 451)
    helper = Generation(kernel, 452)
    root.descendants = [helper]
    owned = {451: root}
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {453: None} if group else {})
    monkeypatch.setattr(module, "_isolated_process_group", lambda pid: None)
    host = module._SystemProcessHost()
    module._refresh_owned_process_tree(host, owned, 451, None)
    del kernel[451]
    helper.denied = True
    assert module._live_owned_processes(owned) == {452: helper}
    host.signal(owned, signal.SIGTERM, process_group=451)
    assert helper.signals == []
    assert not module._group_contains_only_confirmed_owned_processes(451, owned)
    assert not await module._wait_for_identities_exit(owned, 0.1, 451)
    helper.denied = False
    host.signal(owned, signal.SIGTERM, process_group=451)
    assert helper.signals == [signal.SIGTERM]
    del kernel[452]
    assert not await module._wait_for_identities_exit(owned, 0.1, 451)
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {})
    assert await module._wait_for_identities_exit(owned, 0.1, 451)


async def test_reused_member_cannot_be_readopted_from_group(monkeypatch):
    kernel = {}
    root = Generation(kernel, 451)
    old = Generation(kernel, 452)
    owned = {451: root, 452: old}
    replacement = Generation(kernel, 452)
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {451: root, 452: replacement})
    monkeypatch.setattr(module, "_isolated_process_group", lambda pid: 451)
    module._refresh_owned_process_tree(module._SystemProcessHost(), owned, 451, 451)
    assert owned[452] is old
    assert not module._group_contains_only_confirmed_owned_processes(451, owned)
    module._signal_owned_processes(owned, signal.SIGTERM)
    assert replacement.signals == []


async def test_classifier_cannot_transfer_authority_between_generations():
    kernel = {}
    old = Generation(kernel, 451)

    class Host:
        def inspect_identity(self, pid):
            Generation(kernel, pid)
            return SimpleNamespace(stamp=1)

    with pytest.raises(RuntimeError, match="classification"):
        module._inspect_captured_identity(Host(), 451, old)


async def test_successful_group_signal_also_reaches_escaped_retained_child(monkeypatch):
    kernel = {}
    leader = Generation(kernel, 451)
    escaped = Generation(kernel, 452)
    group_signals = []
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {451: leader})
    monkeypatch.setattr(module.os, "killpg", lambda group, sig: group_signals.append((group, sig)))
    host = module._SystemProcessHost()
    owned = {451: leader, 452: escaped}
    host.signal(owned, signal.SIGTERM, process_group=451)
    assert group_signals == [(451, signal.SIGTERM)]
    assert escaped.signals == [signal.SIGTERM]
    kernel.clear()
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {})
    assert await host.wait_for_exit(owned, 0.1, process_group=451)


async def test_failed_stop_keeps_newly_discovered_references(monkeypatch):
    kernel = {}
    root = Generation(kernel, 451)
    descendant = Generation(kernel, 452)
    root.descendants = [descendant]
    owned = {451: root}
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {})

    class Host(module._SystemProcessHost):
        async def wait_for_exit(self, identities, *args, **kwargs):
            return False

    with pytest.raises(RuntimeError, match="did not exit"):
        await module._terminate_owned_process_tree(
            Host(), SimpleNamespace(pid=451), process_group=None, owned_processes=owned, stop_timeout_seconds=0.1
        )
    assert owned[452] is descendant


async def test_native_leader_exit_retains_child_and_grandchild(tmp_path):
    helper_code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
    code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-u','-c',{helper_code!r}]); sys.stdin.readline()"
    child = await spawn(tmp_path, code)
    owned = {}
    try:
        grandchild_pid = int(await asyncio.wait_for(child.stdout.readline(), 3))
        host = module._SystemProcessHost()
        group = host.process_group(child.pid)
        owned = host.snapshot_tree(child.pid, group)
        assert grandchild_pid in owned
        assert len(owned) == 3
        child.stdin.write(b"exit\n")
        await child.stdin.drain()
        for _ in range(100):
            if child.returncode is not None:
                break
            await asyncio.sleep(0.01)
        assert child.returncode == 0
        assert host.live(owned)
        await module._terminate_owned_process_tree(
            host, child, process_group=group, owned_processes=owned, stop_timeout_seconds=1
        )
        assert not host.live(owned)
    finally:
        # Only retained test-created generations are eligible for fallback cleanup.
        module._signal_owned_processes(owned, signal.SIGKILL)
        await dispose(child)


async def test_unreadable_discovered_sidecar_remains_unresolved(monkeypatch, tmp_path):
    socket_path = tmp_path / "owned.sock"
    observed = SimpleNamespace(
        pid=451,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        cmdline=lambda: [sys.executable, "-m", "avibe_memory.sidecar", "--uds", str(socket_path)],
    )
    monkeypatch.setattr(module.psutil, "process_iter", lambda: [observed])
    monkeypatch.setattr(module, "_capture_process", lambda pid: None)
    monkeypatch.setattr(module.psutil, "pid_exists", lambda pid: True)
    found = module._processes_serving_owned_socket(socket_path=socket_path)
    assert found == {451: None}
    assert module._live_owned_processes(found) == found
    assert module._confirmed_owned_processes(found) == {}
    module._signal_owned_processes(found, signal.SIGTERM)
    with pytest.raises(RuntimeError, match="listeners"):
        module._SystemProcessHost().has_tcp_listener(found)
