"""Win32 call-contract tests plus opt-in-by-platform native process tests.

Fakes prove API arguments and cleanup paths, not Windows kernel behavior. Native
cases below exercise real descendants/parent death when run on Windows; they
are not a substitute for the separately authorized interactive desktop gate.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from vibe import computer_process as processes


@pytest.fixture
def api(monkeypatch):
    from core.agent_core.tools import windows_job

    state = SimpleNamespace(
        fail=None, live=set(), closed=[], attributes={}, calls=[], exited=False,
        exit_code=259, duplicates=0,
    )

    class Basic(ctypes.Structure):
        _fields_ = [("LimitFlags", ctypes.c_uint32)]

    class Limits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic)]

    dll = SimpleNamespace(ExtendedLimitInformation=Limits, DWORD=ctypes.c_uint32)
    state.dll = dll

    def call(name, function):
        def invoke(*args):
            state.calls.append(name)
            return function(*args)
        setattr(dll, name, invoke)

    def new_job(*_):
        state.live.add(11)
        return 11

    def limits(_job, kind, pointer, _size):
        assert kind == 9
        assert pointer._obj.BasicLimitInformation.LimitFlags == 0x2000
        return state.fail != "limits"

    def duplicate(_source, _handle, _target, output, access, inherit, options):
        state.duplicates += 1
        assert (access, inherit, options) == (0, True, 2)
        if state.fail == "duplicate" and state.duplicates == 2:
            return False
        output._obj.value = 40 + state.duplicates
        state.live.add(output._obj.value)
        return True

    def initialize(storage, count, flags, size):
        assert (count, flags) == (2, 0)
        size._obj.value = 128
        return storage is not None and state.fail != "attributes"

    def update(_storage, _flags, key, values, size, _previous, _return_size):
        assert size == ctypes.sizeof(values)
        state.attributes[key] = list(values)
        return state.fail != {0x20002: "handle_list", 0x2000D: "job_list"}[key]

    def create(application, command, pa, ta, inherit, flags, env, cwd, startup, info):
        state.creation = {
            "application": application, "command": command.value,
            "inherit": inherit, "flags": flags, "cwd": cwd,
            "environment": "".join(env),
            "stdin": startup._obj.StartupInfo.hStdInput,
            "stdout": startup._obj.StartupInfo.hStdOutput,
            "stderr": startup._obj.StartupInfo.hStdError,
        }
        assert pa is None and ta is None  # non-inheritable process/thread handles
        assert startup._obj.StartupInfo.dwFlags == 0x100
        assert startup._obj.StartupInfo.cb == ctypes.sizeof(startup._obj)
        if state.fail == "create":
            return False
        info._obj.hProcess = 21
        info._obj.hThread = 22
        info._obj.dwProcessId = 1234
        state.live.update([21, 22])
        return True

    def close(handle):
        assert handle in state.live, f"double close or unknown handle: {handle}"
        state.live.remove(handle)
        state.closed.append(handle)
        return True

    def exit_code(_handle, output):
        output._obj.value = state.exit_code
        return True

    call("CreateJobObjectW", new_job)
    call("SetInformationJobObject", limits)
    call("GetCurrentProcess", lambda: -1)
    call("DuplicateHandle", duplicate)
    call("InitializeProcThreadAttributeList", initialize)
    call("UpdateProcThreadAttribute", update)
    call("DeleteProcThreadAttributeList", lambda *_: None)
    call("CreateProcessW", create)
    call("ResumeThread", lambda _thread: 0xFFFFFFFF if state.fail == "resume" else 1)
    call("CloseHandle", close)
    call("WaitForSingleObject", lambda _handle, _timeout: 0 if state.exited else 0x102)
    call("GetExitCodeProcess", exit_code)
    call("CreateNamedPipeW", lambda *_: None)
    call("GetNamedPipeClientProcessId", lambda *_: False)
    call("DisconnectNamedPipe", lambda *_: True)
    monkeypatch.setattr(windows_job, "_kernel32", lambda: dll)
    monkeypatch.setattr(processes, "_error", lambda: OSError("Win32 failure"))
    # Fake CRT lookup only; production never imports msvcrt on POSIX paths.
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd + 100))
    processes._api.cache_clear()
    yield state
    processes._api.cache_clear()


def test_atomic_job_creation_inherits_only_stdio_and_preserves_exit_code(api, tmp_path):
    """No post-create assign gap; the job handle cannot survive in the child."""
    command = [r"C:\Program Files\Node\node.exe", "a script 中文.mjs"]
    with (tmp_path / "request.json").open("w+") as stdin:
        process = processes.WindowsProcess(command, stdin=stdin, env={"Z": "2", "A": "1"}, cwd=str(tmp_path))
    assert api.attributes == {0x20002: [41, 42], 0x2000D: [11]}
    assert api.creation["command"] == subprocess.list2cmdline(command)
    assert api.creation["application"] == command[0]
    assert api.creation["flags"] == 0x08080404
    assert api.creation["inherit"] is True
    assert api.creation["environment"].startswith("A=1\0Z=2\0\0")
    assert [api.creation[key] for key in ("stdin", "stdout", "stderr")] == [41, 42, 42]
    assert api.live == {11, 21}
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0)
    api.exited = True
    # STILL_ACTIVE as an actual exit code is not confused with a live process.
    assert process.wait(timeout=0) == 259
    process.kill()
    assert api.live == {21}  # close job even after the root has exited
    process.close()
    process.close()
    assert api.live == set()


@pytest.mark.parametrize("failure", ["limits", "duplicate", "attributes", "handle_list", "job_list", "create", "resume"])
def test_atomic_launch_failure_closes_every_partial_handle_without_fallback(api, failure):
    """No naked Popen fallback and no leaked suspended child on resume failure."""
    api.fail = failure
    with pytest.raises(OSError, match="Win32 failure"):
        processes.WindowsProcess(["node.exe", "kernel.mjs"], env={})
    assert api.live == set()
    assert api.closed.count(11) == 1
    assert api.calls.count("CreateProcessW") == (1 if failure in {"create", "resume"} else 0)
    assert api.calls.count("ResumeThread") == (1 if failure == "resume" else 0)


def test_async_creation_cancellation_collects_and_kills_thread_result(monkeypatch):
    """Cancelling to_thread must not discard the only owner of its late child."""
    entered = threading.Event()
    release = threading.Event()
    child = SimpleNamespace(pid=123, killed=False, closed=False)
    child.poll = lambda: 1 if child.killed else None
    child.kill = lambda: setattr(child, "killed", True)
    child.close = lambda: setattr(child, "closed", True)

    def create(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return child

    monkeypatch.setattr(processes, "WindowsProcess", create)

    async def scenario():
        pending = asyncio.create_task(processes.create_windows_process(["node"], env={}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert child.killed and child.closed

    asyncio.run(asyncio.wait_for(scenario(), 30))


def _pipe_fixture(monkeypatch, api, peers, *, pending=False):
    from core import private_fs

    loop = asyncio.get_running_loop()
    state = SimpleNamespace(accepts=[], transports=[], disconnects=0, peer_ids=iter(peers))

    class Attributes(ctypes.Structure):
        _fields_ = [("descriptor", ctypes.c_void_p), ("inheritable", ctypes.c_int)]

    class Security:
        @contextmanager
        def _security_attributes(self):
            yield Attributes(0x123400, False)

    class Handle:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle

        def close(self):
            if self.handle is not None:
                api.dll.CloseHandle(self.handle)
                self.handle = None

    def create(path, flags, mode, instances, _out, _in, _timeout, attributes):
        assert path.startswith("\\\\.\\pipe\\avibe-computer-js-")
        assert (flags, mode, instances) == (0x40080003, 8, 1)
        assert attributes._obj.descriptor == 0x123400 and not attributes._obj.inheritable
        api.live.add(31)
        return 31

    def peer(_handle, result):
        result._obj.value = next(state.peer_ids)
        return True

    def disconnect(_handle):
        state.disconnects += 1
        return True

    def accept(_pipe):
        future = loop.create_future()
        state.accepts.append(future)
        if not pending:
            future.set_result(None)
        return future

    def transport(pipe, protocol, ready):
        owned = SimpleNamespace(
            close=pipe.close, is_closing=lambda: pipe.handle is None,
        )
        state.transports.append(owned)
        ready.set_result(None)
        return owned

    api.dll.CreateNamedPipeW = create
    api.dll.GetNamedPipeClientProcessId = peer
    api.dll.DisconnectNamedPipe = disconnect
    monkeypatch.setattr(private_fs, "WindowsSecurity", Security)
    monkeypatch.setitem(sys.modules, "asyncio.windows_utils", SimpleNamespace(PipeHandle=Handle))
    monkeypatch.setattr(asyncio, "ProactorEventLoop", type(loop), raising=False)
    monkeypatch.setattr(loop, "_proactor", SimpleNamespace(accept_pipe=accept), raising=False)
    monkeypatch.setattr(loop, "_make_duplex_pipe_transport", transport, raising=False)
    return state


def test_pipe_rejects_wrong_pid_then_admits_owned_live_generation(monkeypatch, api):
    """A peer with no init bytes cannot consume the legitimate pipe forever."""
    async def scenario():
        state = _pipe_fixture(monkeypatch, api, [9999, 1234])
        process = SimpleNamespace(pid=1234, returncode=None, wait=asyncio.Event().wait)
        pipe = processes.WindowsKernelPipe()
        try:
            _reader, writer = await pipe.connect(process, limit=4096)
            assert state.disconnects == 1 and len(state.accepts) == 2
            assert len(state.transports) == 1
            writer.close()
        finally:
            pipe.close()
        assert api.live == set()

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.parametrize("ending", ["wrong_peers", "dead_generation", "cancel"])
def test_pipe_admission_failure_closes_handle_and_pending_accept(monkeypatch, api, ending):
    """PID reuse/death, unbounded spoof attempts and abandoned overlapped accepts fail closed."""
    async def scenario():
        state = _pipe_fixture(monkeypatch, api, [9999] * 8, pending=ending == "cancel")
        process = SimpleNamespace(pid=1234, returncode=None, wait=asyncio.Event().wait)
        pipe = processes.WindowsKernelPipe()
        try:
            if ending == "dead_generation":
                process.returncode = 0
            task = asyncio.create_task(pipe.connect(process, limit=4096))
            if ending == "cancel":
                while not state.accepts:
                    await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert state.accepts[0].cancelled()
            else:
                with pytest.raises(OSError):
                    await task
            assert not state.transports
            if ending == "wrong_peers":
                assert len(state.accepts) == 8 and state.disconnects == 8
        finally:
            pipe.close()
        assert api.live == set()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_public_js_route_stays_disabled_on_windows_with_prior_enabled_state(monkeypatch):
    from core import computer_js_kernels as kernels

    async def scenario():
        monkeypatch.setattr(kernels, "os", SimpleNamespace(name="nt"))
        sessions = SimpleNamespace(
            open=lambda *_: {"session_id": "prior-enabled", "connection_token": "test-only"},
            close=lambda *_: None,
        )
        manager = kernels.ComputerJsKernelManager(
            sessions,
            status_reader=lambda: SimpleNamespace(status="unavailable", state=SimpleNamespace(enabled=True)),
        )
        result = await manager.evaluate("prior-enabled", "42", 1000)
        assert result["error"]["code"] == "platform_unsupported", result
        assert manager._slots["prior-enabled"].kernel is None

    asyncio.run(asyncio.wait_for(scenario(), 30))


# These tests require a real Windows OS, not mocked os.name. Normal POSIX CI
# collects/skips them; running a Windows job still requires the lead's slot.
native_windows = pytest.mark.skipif(os.name != "nt", reason="requires actual Windows Job Objects")


def _wait_file(path: Path, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.02)
    pytest.fail(f"child did not write {path}")


def _wait_dead(pid: int) -> None:
    from core.agent_core.tools.windows_job import process_running

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_running(pid):
            return
        time.sleep(0.02)
    pytest.fail(f"owned process survived: {pid}")


@native_windows
@pytest.mark.parametrize("root_exits", [False, True])
def test_windows_job_kills_descendants_even_after_root_exit(tmp_path, root_exits):
    child_pid = tmp_path / "child.pid"
    source = (
        "import subprocess,sys,time,pathlib; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid),encoding='utf-8'); "
        + ("sys.exit(0)" if root_exits else "time.sleep(60)")
    )
    process = processes.WindowsProcess([sys.executable, "-c", source], env=dict(os.environ))
    try:
        descendant = int(_wait_file(child_pid))
        if root_exits:
            assert process.wait(timeout=5) == 0
        process.kill()
        process.wait(timeout=5)
        _wait_dead(descendant)
    finally:
        process.close()


@native_windows
@pytest.mark.parametrize("phase", ["before_resume", "running"])
def test_windows_parent_death_kills_atomic_child_and_descendants(tmp_path, phase):
    pids = tmp_path / "pids.json"
    supervisor = tmp_path / "supervisor.py"
    source = (
        "import subprocess,sys,time,pathlib,json; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(pids)!r}).write_text(json.dumps([__import__('os').getpid(),child.pid])); "
        "time.sleep(60)"
    )
    # The stopped ResumeThread hook is a crash injection, not an alternative
    # launcher. CreateProcess/JOB_LIST/HANDLE_LIST remain the real OS path.
    supervisor.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "from vibe.computer_process import WindowsProcess, _api\n"
        + (
            "def pause(thread):\n"
            f"    Path({str(pids)!r}).write_text(json.dumps([launched[0]]))\n"
            "    time.sleep(60)\n"
            "api=_api()\nreal_create=api.CreateProcessW\nlaunched=[]\n"
            "def create(*args):\n"
            "    ok=real_create(*args)\n"
            "    if ok: launched.append(args[-1]._obj.dwProcessId)\n"
            "    return ok\n"
            "api.CreateProcessW=create\napi.ResumeThread=pause\n"
            if phase == "before_resume" else ""
        )
        + f"child=WindowsProcess([sys.executable,'-c',{source!r}],env=dict(os.environ))\n"
        "child.wait()\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    owner = subprocess.Popen([sys.executable, str(supervisor)], env=environment)
    try:
        owned_pids = json.loads(_wait_file(pids))
        owner.kill()  # forced parent death, no Python cleanup handler
        owner.wait(timeout=5)
        for pid in owned_pids:
            _wait_dead(pid)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


@native_windows
def test_windows_pipe_acl_and_wrong_peer_do_not_consume_owned_connection(tmp_path):
    """Real pipe ACL plus same-user wrong-PID rejection; no desktop access."""
    from core.private_fs import WindowsSecurity

    async def scenario():
        pipe = processes.WindowsKernelPipe()
        security = WindowsSecurity()
        owner = security.wintypes.LPVOID()
        dacl = security.wintypes.LPVOID()
        descriptor = security.wintypes.LPVOID()
        error = security.advapi32.GetSecurityInfo(
            pipe._pipe.fileno(), 6, 5, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        assert error == 0
        try:
            security._validate_security_descriptor(owner, dacl, descriptor, Path(pipe.path))
        finally:
            security.kernel32.LocalFree(descriptor)
        ready = tmp_path / "wrong-peer-ready"
        wrong = subprocess.Popen([
            sys.executable, "-c",
            f"import time,pathlib; p=open({pipe.path!r},'r+b',buffering=0); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
        ])
        owned = None
        writer = None
        try:
            await asyncio.to_thread(_wait_file, ready)
            owned = await processes.create_windows_process([
                sys.executable, "-c",
                # Wait for the first peer to be refused; our connect loop
                # gives the legitimate client an opportunity to attach.
                f"import time; time.sleep(0.2); p=open({pipe.path!r},'r+b',buffering=0); "
                "p.write(b'owned\\n'); time.sleep(30)",
            ], env=dict(os.environ))
            reader, writer = await asyncio.wait_for(pipe.connect(owned, limit=4096), 5)
            assert await asyncio.wait_for(reader.readline(), 5) == b"owned\n"
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            pipe.close()
            wrong.kill()
            await asyncio.to_thread(wrong.wait, 5)
            if owned is not None:
                owned.kill()
                await asyncio.wait_for(owned.wait(), 5)
                owned.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


@native_windows
@pytest.mark.parametrize("timeout", [False, True])
def test_windows_script_runner_preserves_stdin_exit_and_kills_tree(tmp_path, timeout):
    from vibe.computer_script_runner import _run_node_process

    node = shutil.which("node")
    assert node, "normal Windows CI must supply Node"
    output = tmp_path / "result.json"
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        "import fs from 'node:fs'; import {spawn} from 'node:child_process';\n"
        "const request=JSON.parse(fs.readFileSync(0,'utf8'));\n"
        "const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});\n"
        "fs.writeFileSync(request.result_path,JSON.stringify({request,pid:child.pid}));\n"
        + ("setInterval(()=>{},1000);\n" if timeout else "process.exit(7);\n"),
        encoding="utf-8",
    )
    request = {"result_path": str(output), "unicode": "中文 🧀"}
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            _run_node_process(Path(node), runner, request, 2)
    else:
        result = _run_node_process(Path(node), runner, request, 10)
        assert result.returncode == 7
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["request"] == request
    _wait_dead(result["pid"])


@native_windows
@pytest.mark.parametrize("ending", ["reset", "request_cancel"])
def test_windows_real_kernel_pipe_timeout_and_tree_cleanup(ending, monkeypatch):
    from core import computer_js_kernels
    from core.computer_js_kernels import ComputerJsKernelManager

    # The foundation does not activate the public Windows route. Everything
    # below the platform gate (ACL, pipe peer, OS process/Job) remains real.
    monkeypatch.setattr(computer_js_kernels, "_kernel_platform_supported", lambda: True)

    node = shutil.which("node")
    assert node, "normal Windows CI must supply Node"

    async def scenario():
        # Admission itself has full Controller tests elsewhere; here it issues
        # no real desktop token and the JS performs no Computer calls.
        sessions = SimpleNamespace(
            open=lambda *_: {"session_id": "native-kernel", "connection_token": "test-only"},
            close=lambda *_: None, check_caller=lambda *_: "native-kernel",
        )
        manager = ComputerJsKernelManager(
            sessions, node_resolver=lambda: Path(node),
            endpoint_resolver=lambda: {"transport": "tcp", "host": "127.0.0.1", "port": 1},
            status_reader=lambda: SimpleNamespace(status="ready"),
        )
        try:
            result = await manager.evaluate("native-kernel", "let saved = 40; saved + 2", 5000)
            assert result["ok"] is True and result["result"] == 42, result
            timed = await manager.evaluate("native-kernel", "await new Promise(() => {})", 50)
            assert timed["error"]["code"] == "js_timeout", timed
            assert (await manager.evaluate("native-kernel", "saved", 1000))["result"] == 40
            result = await manager.evaluate(
                "native-kernel",
                "process.getBuiltinModule('child_process').spawn(process.execPath, "
                "['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore'}).pid", 1000,
            )
            assert result["ok"] is True, result
            descendant = result["result"]
            kernel = manager._slots["native-kernel"].kernel
            if ending == "reset":
                assert manager.reset("native-kernel")["discarded"]
            else:
                pending = asyncio.create_task(manager.evaluate("native-kernel", "await new Promise(() => {})", 30000))
                while not kernel.pending:
                    await asyncio.sleep(0)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await asyncio.wait_for(kernel.process.wait(), 5)
            await asyncio.to_thread(_wait_dead, descendant)
        finally:
            manager.close_all()
            await asyncio.gather(*manager._background)

    asyncio.run(asyncio.wait_for(scenario(), 30))
