"""MEMORY-WAKE-204: native lifecycle and deterministic generation boundaries."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

import psutil
import pytest

import avibe_memory.process as module

pytestmark = pytest.mark.asyncio

_CHILD_IO_TIMEOUT_SECONDS = 3
_CHILD_CLEANUP_TIMEOUT_SECONDS = 3


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
        try:
            child.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(child.wait(), _CHILD_CLEANUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise AssertionError("test child did not exit during cleanup") from exc


async def read_child_line(child, description):
    try:
        line = await asyncio.wait_for(
            child.stdout.readline(),
            _CHILD_IO_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"{description} did not produce output within "
            f"{_CHILD_IO_TIMEOUT_SECONDS} seconds"
        ) from exc
    if not line:
        raise AssertionError(f"{description} exited before producing output")
    return line


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


@pytest.mark.parametrize("consumer", ["stop", "start_failure", "record_exit", "probe_timeout"])
async def test_capture_keeps_reference_through_transient_read_failure(
    tmp_path, monkeypatch, caplog, consumer,
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
                    fault.setattr(reference, "create_time", inaccessible)
                    fault.setattr(reference, "is_running", inaccessible)
                    fault.setattr(psutil, "Process", lambda target: reference)
                    captured = super().capture(pid)
                assert captured is reference
                return captured

            def inspect_identity(self, pid):
                if consumer == "record_exit":
                    self.reference.terminate()
                    return None
                return super().inspect_identity(pid)

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
            elif consumer in {"start_failure", "record_exit"}:
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
            if consumer == "record_exit":
                assert "sidecar exited before ownership could be recorded" in caplog.text
                assert "AttributeError" not in caplog.text
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


@pytest.mark.parametrize("leader_exited", [False, True])
async def test_reused_member_cannot_be_readopted_from_group(monkeypatch, leader_exited):
    kernel = {}
    root = Generation(kernel, 451)
    old = Generation(kernel, 452)
    owned = {451: root, 452: old}
    replacement = Generation(kernel, 452)
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {451: root, 452: replacement})
    monkeypatch.setattr(module, "_isolated_process_group", lambda pid: 451)
    host = module._SystemProcessHost()
    if leader_exited:
        del kernel[451]
        monkeypatch.setattr(host, "recorded_group_members", lambda *args, **kwargs: ({452: replacement}, []))
    module._refresh_terminating_process_tree(
        host, owned, 451, 451, socket_path=Path("/test/socket"),
        provider_root=Path("/test/root"), role="sidecar",
    )
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

    assert module._inspect_captured_identity(Host(), 451, old) is None


@pytest.mark.parametrize("group_delivery", ["success", "refused", "failed", "reused_during_lookup"])
async def test_successful_group_signal_also_reaches_escaped_retained_child(monkeypatch, group_delivery):
    kernel = {}
    leader = Generation(kernel, 451)
    escaped = Generation(kernel, 452)
    group_signals = []
    monkeypatch.setattr(module, "_snapshot_process_group", lambda group: {451: leader})
    terminal = Generation(kernel, 453)
    Generation(kernel, 453)
    denied = Generation(kernel, 454)
    denied.denied = True

    def getpgid(pid):
        assert pid in {451, 452}, "looked up a terminal/unreadable reference"
        if pid == 452 and group_delivery == "reused_during_lookup":
            Generation(kernel, pid)
        return 451 if pid == leader.pid else 452

    monkeypatch.setattr(module.os, "getpgid", getpgid)

    def killpg(group, sig):
        if group_delivery == "failed":
            raise PermissionError
        group_signals.append((group, sig))
        leader.send_signal(sig)

    monkeypatch.setattr(module.os, "killpg", killpg)
    if group_delivery == "refused":
        monkeypatch.setattr(module, "_group_contains_only_confirmed_owned_processes", lambda *args: False)
    host = module._SystemProcessHost()
    owned = {451: leader, 452: escaped, 453: terminal, 454: denied}
    host.signal(owned, signal.SIGTERM, process_group=451)
    assert group_signals == ([(451, signal.SIGTERM)] if group_delivery in {"success", "reused_during_lookup"} else [])
    assert leader.signals == [signal.SIGTERM]
    assert escaped.signals == ([] if group_delivery == "reused_during_lookup" else [signal.SIGTERM])
    assert not terminal.signals and not denied.signals
    if group_delivery == "reused_during_lookup":
        assert not kernel[452].signals
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
            Host(), SimpleNamespace(pid=451), process_group=None, owned_processes=owned, stop_timeout_seconds=0.1,
            socket_path=Path("/test/socket"), provider_root=Path("/test/root"),
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
            host, child, process_group=group, owned_processes=owned, stop_timeout_seconds=1,
            socket_path=tmp_path / "memory.sock", provider_root=tmp_path,
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


@pytest.mark.parametrize(
    ("consumer", "timing", "mismatch"),
    [("stop", "before", None), ("stop", "term", None),
     ("watch", "before", None), ("probe", "before", None),
     ("probe", "term", None), ("orphan", "term", None),
     ("stop", "before", "root"), ("stop", "before", "role"),
     ("stop", "before", "unreadable")],
)
async def test_native_late_group_helper_is_classified_before_cleanup(
    monkeypatch, consumer, timing, mismatch,
):
    """MEMORY-WAKE-204: unseen helper survives leader; context, not PGID, owns it."""
    with tempfile.TemporaryDirectory(prefix="mlate-", dir="/tmp") as temporary:
        current_cmdline = psutil.Process().cmdline()
        python = Path(current_cmdline[0] if current_cmdline else sys.executable)
        home = Path(temporary).resolve()
        root = home / "memory/everos-root"
        root.mkdir(parents=True, mode=0o700)
        root.parent.chmod(0o700)
        package = home / "avibe_memory"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "sidecar.py").write_text(
            "import os,signal,subprocess,sys,time\n"
            "def leave(*args):\n"
            " with open('term-count','a') as log: log.write('1')\n"
            " if len(open('term-count').read()) > 1: return\n"
            " time.sleep(0.05)\n"
            " env=dict(os.environ)\n"
            f" if {mismatch == 'root'!r}: env['EVEROS_ROOT'] += '-foreign'\n"
            f" if {mismatch == 'role'!r}: env['AVIBE_MEMORY_CHILD_ROLE'] = 'foreign'\n"
            " p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],env=env,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            " print(p.pid,flush=True)\n"
            " sys.exit(0)\n"
            "signal.signal(signal.SIGTERM,leave)\n"
            "print('ready',flush=True)\n"
            "sys.stdin.readline()\n"
            "leave()\n"
        )
        socket_path = home / "memory/.rt/everos.sock"
        role = "processing_probe" if consumer == "probe" else "sidecar"
        child = await asyncio.create_subprocess_exec(
            str(python), "-m", "avibe_memory.sidecar", "--uds", str(socket_path),
            cwd=home, start_new_session=True,
            env={"HOME": str(home), "EVEROS_ROOT": str(root), "AVIBE_MEMORY_CHILD_ROLE": role},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        assert await read_child_line(child, "sidecar readiness") == b"ready\n"

        async def observe_helper():
            line = await read_child_line(child, "helper PID")
            try:
                return psutil.Process(int(line)) if line else None
            except ValueError as exc:
                raise AssertionError(f"helper PID was not an integer: {line!r}") from exc
            except psutil.NoSuchProcess:
                return None

        if timing == "term":
            deliver_group = module._signal_owned_group

            def group_delivery(*args):
                delivered = deliver_group(*args)
                if delivered and args[2] == signal.SIGTERM:
                    # Ensure the native handler has begun before individual
                    # delivery, so coalescing cannot hide a duplicate TERM.
                    for _ in range(50):
                        if (home / "term-count").exists():
                            break
                        time.sleep(0.002)
                return delivered

            monkeypatch.setattr(module, "_signal_owned_group", group_delivery)
        helper_task = asyncio.create_task(observe_helper())
        helper = None
        host = module._SystemProcessHost()
        owner = module.EverOSProcess(
            python, effective_home=home, _host=host, settings=settings(),
            provider_root_guard=lambda: None, stop_timeout_seconds=0.1,
        )
        owned = {child.pid: host.capture(child.pid)}
        owner._process, owner._process_group, owner._owned_processes = child, child.pid, owned
        owner._ownership.record_launch(child.pid, module._process_creation_stamp(owned[child.pid]), child.pid)
        record_path = owner._ownership.record_path
        try:
            if consumer == "probe":
                async def probe_spawn(*args, **kwargs):
                    assert kwargs["env"]["AVIBE_MEMORY_CHILD_ROLE"] == role
                    return child

                monkeypatch.setattr(host, "spawn", probe_spawn)
                monkeypatch.setattr(module, "_processing_probe_timeout_seconds", lambda _: 0.1)
                # The probe's own capture/scan happens before release of this leader.
                original_snapshot = host.snapshot_tree

                def snapshot(*args, **kwargs):
                    result = original_snapshot(*args, **kwargs)
                    if timing == "before" and child.returncode is None:
                        child.stdin.write(b"go\n")
                    return result

                monkeypatch.setattr(host, "snapshot_tree", snapshot)
            elif timing == "before":
                child.stdin.write(b"go\n")
                await child.stdin.drain()
                helper = await helper_task
                await asyncio.wait_for(child.wait(), _CHILD_IO_TIMEOUT_SECONDS)
                assert helper.pid not in owned
                if mismatch == "unreadable":
                    original_environment = module._disclosed_process_environment
                    monkeypatch.setattr(
                        module, "_disclosed_process_environment",
                        lambda process: None if process.pid == helper.pid else original_environment(process),
                    )
            if mismatch:
                with pytest.raises(RuntimeError, match="did not exit"):
                    await owner.stop()
                assert helper.is_running() and record_path.exists()
                assert owner.retains_active_config
            else:
                if consumer == "probe":
                    result = await owner.processing_healthy()
                    assert result is (timing == "before")
                elif consumer == "orphan":
                    await owner._ownership.reap()
                elif consumer == "watch":
                    await owner._watch_child(child)
                else:
                    await owner.stop()
                if helper is None:
                    helper = await asyncio.wait_for(helper_task, _CHILD_IO_TIMEOUT_SECONDS)
                assert child.returncode is not None
                assert not module._snapshot_process_group(child.pid)
                assert (home / "term-count").read_text() == "1"
                if consumer != "probe":
                    assert not record_path.exists()
                if consumer in {"stop", "watch"}:
                    assert not owner.retains_active_config  # Replacement admission is clear.
        finally:
            try:
                await dispose(child)
            finally:
                if not helper_task.done():
                    try:
                        helper = await asyncio.wait_for(
                            helper_task,
                            _CHILD_IO_TIMEOUT_SECONDS,
                        )
                    except (asyncio.TimeoutError, AssertionError):
                        helper_task.cancel()
                        await asyncio.gather(helper_task, return_exceptions=True)
                if helper is not None and module._reference_state(helper, helper.pid) is True:
                    try:
                        helper.kill()
                    except psutil.NoSuchProcess:
                        pass
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(helper.wait, _CHILD_CLEANUP_TIMEOUT_SECONDS),
                            _CHILD_CLEANUP_TIMEOUT_SECONDS,
                        )
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired) as exc:
                        raise AssertionError("test helper did not exit during cleanup") from exc


@pytest.mark.parametrize("discovery", ["socket", "root", "sync"])
async def test_discovery_skips_generation_gone_during_classification(monkeypatch, tmp_path, discovery):
    kernel = {}
    reference = Generation(kernel, 451)
    socket_path = tmp_path / "memory.sock"
    cmdline = [sys.executable, "-m", "avibe_memory.sidecar", "--uds", str(socket_path)]
    if discovery == "sync":
        cmdline = [sys.executable, "-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"]
    reference.uids = lambda: SimpleNamespace(real=os.getuid())
    reference.cmdline = lambda: cmdline
    reference.create_time = lambda: 1.0

    def environment():
        kernel.clear()
        return {"EVEROS_ROOT": str(tmp_path), "AVIBE_MEMORY_CHILD_ROLE": "cascade_sync" if discovery == "sync" else "sidecar", "AVIBE_MEMORY_SYNC_NONCE": "test"}

    reference.environ = environment
    if discovery == "socket":
        def command():
            kernel.clear()
            return cmdline
        reference.cmdline = command
    monkeypatch.setattr(psutil, "process_iter", lambda: [reference])
    monkeypatch.setattr(module, "_capture_process", lambda pid: reference)
    monkeypatch.setattr(module, "_process_creation_stamp", lambda process: 1.0)
    if discovery == "socket":
        result = module._processes_serving_owned_socket(socket_path=socket_path)
    elif discovery == "root":
        result = module._processes_serving_owned_root(provider_root=tmp_path)
    else:
        result = module._processes_syncing_owned_root(provider_root=tmp_path, python=Path(sys.executable), nonce="test")
    assert not kernel, "test must reach the classification race"
    assert result == {}


@pytest.mark.parametrize("timeout", [False, True], ids=["ready-stop", "startup-timeout"])
async def test_startup_retains_helper_observed_before_detachment(monkeypatch, timeout):
    """Readiness discovery must survive later loss of both ancestry and group."""
    with tempfile.TemporaryDirectory(prefix="mr1990-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "memory/everos-root").mkdir(parents=True, mode=0o700)
        (root / "memory").chmod(0o700)
        helper_code = """
import os, pathlib, time
root = pathlib.Path('.')
(root / 'helper.tmp').write_text(str(os.getpid()))
(root / 'helper.tmp').replace(root / 'helper')
while not (root / 'detach').exists(): time.sleep(.005)
os.setsid()
(root / 'detached').touch()
time.sleep(60)
"""
        middle_code = f"""
import pathlib, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', {helper_code!r}],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not pathlib.Path('detach').exists(): time.sleep(.005)
"""
        leader_code = f"""
import pathlib, subprocess, sys, time
while not pathlib.Path('spawn-helper').exists(): time.sleep(.005)
subprocess.Popen([sys.executable, '-c', {middle_code!r}],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(60)
"""
        helper = None
        observed = None
        children = []

        async def until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.005)

        class Host(module._SystemProcessHost):
            async def spawn(self, *args, **kwargs):
                if children:
                    assert module._reference_state(helper, helper.pid) is False
                child = await spawn(root, leader_code if not children else "import time; time.sleep(60)")
                children.append(child)
                owner._socket_path.parent.mkdir(parents=True, exist_ok=True)
                owner._socket_path.touch()
                return child

            def inspect_identity(self, pid):
                identity = super().inspect_identity(pid)
                if len(children) == 1:
                    assert list(owner._owned_processes) == [pid]
                    (root / "spawn-helper").touch()
                return identity

        class Port:
            def __init__(self, *args, **kwargs):
                self.polls = 0

            async def health(self):
                nonlocal helper, observed
                if len(children) > 1:
                    return True
                self.polls += 1
                if self.polls == 1:
                    await until(lambda: (root / "helper").exists())
                    helper = psutil.Process(int((root / "helper").read_text()))
                    return False  # Leave the helper visible for the next discovery poll.
                if self.polls == 2:
                    observed = owner._owned_processes.get(helper.pid)
                    original_parent = helper.ppid()
                    (root / "detach").touch()
                    await until(lambda: (root / "detached").exists() and helper.ppid() != original_parent)
                    assert os.getpgid(helper.pid) == helper.pid
                    assert helper.pid not in {
                        child.pid for child in psutil.Process(children[0].pid).children(recursive=True)
                    }
                return not timeout

        owner = module.EverOSProcess(
            sys.executable, effective_home=root, settings=settings(), _host=Host(),
            provider_root_guard=lambda: None, startup_timeout_seconds=2, stop_timeout_seconds=.3,
        )
        monkeypatch.setattr(module, "EverOSPort", Port)
        monkeypatch.setattr(owner, "_secure_socket", lambda: None)
        try:
            assert await owner.start() is (not timeout)
            if not timeout:
                await owner.stop()
            assert helper is not None
            assert module._reference_state(helper, helper.pid) is False
            assert observed is not None
            assert module._reference_state(observed, helper.pid) is False
            assert not owner.retains_active_config
            assert await owner.start()  # Spawn checks old helper is gone before replacement.
        finally:
            if helper is not None and module._reference_state(helper, helper.pid) is True:
                helper.kill()
            await owner.stop()
            for child in children:
                await dispose(child)
