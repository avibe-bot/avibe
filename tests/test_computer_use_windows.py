"""Windows foundation: native CI must run every test_windows_native_* without skips."""
from __future__ import annotations

import ctypes
import hashlib
import json
import multiprocessing
import os
from pathlib import Path, PureWindowsPath
import sys
import types

import pytest

import core.computer_use as cu


@pytest.mark.parametrize("endpoint", [
    "\\\\.\\pipe\\", r"\\.\pipe\nested\name", "\\\\.\\PIPE\\trailing\\",
    r"\\.\pipe\slash/name", "\\\\.\\pipe\\nul\x00name", "\\\\.\\pipe\\" + "x" * 248,
])
def test_windows_malformed_pipe_cannot_fall_back_to_absolute(monkeypatch, endpoint):
    assert PureWindowsPath(endpoint).is_absolute()
    monkeypatch.setattr(cu, "Path", PureWindowsPath)
    with pytest.raises(cu.ComputerUseStateError, match="socket_path"):
        cu._endpoint_path({"socket_path": endpoint}, "socket_path")


class NativeFunction:
    def __init__(self, result=True):
        self.result = result
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        assert self.argtypes is not None
        assert len(args) == len(self.argtypes)
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake_lock_api(monkeypatch):
    api = types.SimpleNamespace(LockFileEx=NativeFunction(), UnlockFileEx=NativeFunction())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(cu, "_windows_private_artifact_is_safe", lambda *a, **kw: True)
    return api


@pytest.mark.parametrize(
    "result,error,expected",
    [(True, 0, "free"), (False, 33, "held"), (False, 32, "unverified"),
     (False, 5, "unverified"), (False, 6, "unverified"), (False, 87, "unverified"),
     (ctypes.ArgumentError("ABI"), 0, "unverified"),
     (TypeError("ABI"), 0, "unverified"), (OSError("native"), 0, "unverified")],
)
def test_windows_lock_abi_and_error_matrix(tmp_path, monkeypatch, fake_lock_api, result, error, expected):
    path = tmp_path / "lock"
    path.touch()
    fake_lock_api.LockFileEx.result = result
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert cu._windows_lock_probe(path) == expected
    assert len(fake_lock_api.LockFileEx.argtypes) == 6
    assert len(fake_lock_api.UnlockFileEx.argtypes) == 5
    # The descriptor passed to native code is closed on both success and error.
    fd = fake_lock_api.LockFileEx.calls[0][0]
    with pytest.raises(OSError):
        os.fstat(fd)


def test_windows_lock_unlock_failure_is_unverified(tmp_path, fake_lock_api):
    path = tmp_path / "lock"
    path.touch()
    fake_lock_api.UnlockFileEx.result = False
    assert cu._windows_lock_probe(path) == "unverified"


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("sharing violation")])
def test_windows_lock_open_errors_are_not_held(tmp_path, monkeypatch, fake_lock_api, error):
    path = tmp_path / "lock"
    path.touch()

    def denied(*args):
        raise error

    monkeypatch.setattr(cu, "os", types.SimpleNamespace(open=denied, O_RDWR=os.O_RDWR))
    assert cu._windows_lock_probe(path) == "unverified"
    assert not fake_lock_api.LockFileEx.calls


def test_windows_lock_rechecks_opened_artifact(tmp_path, monkeypatch, fake_lock_api):
    path = tmp_path / "lock"
    path.touch()
    checks = []

    def safe(_path, *, fd=None):
        checks.append(fd)
        return fd is None

    monkeypatch.setattr(cu, "_windows_private_artifact_is_safe", safe)
    assert cu._windows_lock_probe(path) == "unverified"
    assert checks[0] is None and isinstance(checks[1], int)
    assert not fake_lock_api.LockFileEx.calls


def write_ready(directory: Path, *, shell_pid: int | None = None) -> Path:
    snapshot = directory / "tools.json"
    snapshot.write_bytes(b'{"tools":[]}')
    state = directory / "computer-use.json"
    state.write_text(json.dumps({
        "schema_version": 1, "enabled": True, "state": "ready", "reason": None,
        "shell_pid": shell_pid or os.getpid(), "instance_id": "test", "generation": 1,
        "driver_version": "0.31.0", "socket_path": r"\\.\pipe\avibe-cu-test",
        "proxy_executable": str(directory / "cua-driver.exe"),
        "host_bundle_id": "bot.avibe.desktop",
        "tool_snapshot": {"path": str(snapshot), "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    return state


@pytest.mark.parametrize("machine,reason", [("AMD64", "pipe_unverified"), ("ARM64", "unsupported_architecture")])
@pytest.mark.parametrize("injected", [False, True])
def test_windows_ready_cannot_be_admitted(tmp_path, monkeypatch, machine, reason, injected):
    state = write_ready(tmp_path)
    monkeypatch.setattr(cu, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(cu.platform_module, "machine", lambda: machine)
    monkeypatch.setattr(cu, "_windows_private_artifact_is_safe", lambda *a, **kw: True)
    monkeypatch.setattr(cu, "_windows_lock_probe", lambda p: "held")
    kwargs = {}
    if injected:
        kwargs = {"shell_lock_held": lambda p: True, "socket_accepts": lambda p: True}
    status = cu.effective_computer_use_status(state_path=state, verifier=cu.SnapshotVerifier(), **kwargs)
    assert (status.status, status.reason) == ("unavailable", reason)
    assert cu._endpoint_accepts(r"\\.\pipe\avibe-cu-test") is False


# Native tests run only on Windows. CI must select these explicitly and assert
# an exact count and zero skips, rather than treating a skipped suite as evidence.
native = pytest.mark.skipif(os.name != "nt", reason="Windows Win32 integration")


def private_lock(path: Path) -> None:
    from core.private_fs import windows_security
    fd = windows_security().open_private_lock_file(path, allow_repair=False)
    os.close(fd)


def hold_lock(path, channel):
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                    ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                    ("event", wintypes.HANDLE)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    api.LockFileEx.restype = wintypes.BOOL
    fd = os.open(path, os.O_RDWR)
    try:
        overlapped = Overlapped()
        assert api.LockFileEx(msvcrt.get_osfhandle(fd), 3, 0, 1, 0, ctypes.byref(overlapped))
        channel.send("held")
        assert channel.poll(30), "parent failed to release child"
        channel.recv()
    finally:
        os.close(fd)
        channel.close()


@native
def test_windows_native_lock_free_and_missing(tmp_path):
    path = tmp_path / "lock"
    assert cu._windows_lock_probe(path) == "missing"
    private_lock(path)
    assert cu._windows_lock_probe(path) == "free"
    assert cu._windows_lock_probe(path) == "free"  # probe releases its byte-range lock


@native
def test_windows_native_lock_contention_and_ready_refusal(tmp_path):
    path = tmp_path / "computer-use.lock"
    private_lock(path)
    # Create with explicit current-user owner even on elevated CI runners.
    private_lock(tmp_path / "computer-use.json")
    state = write_ready(tmp_path)
    context = multiprocessing.get_context("spawn")
    parent, child_end = context.Pipe()
    child = context.Process(target=hold_lock, args=(str(path), child_end))
    child.start()
    child_end.close()
    try:
        assert parent.poll(15), "lock holder did not start"
        assert parent.recv() == "held"
        assert cu._windows_lock_probe(path) == "held"
        status = cu.effective_computer_use_status(state_path=state, verifier=cu.SnapshotVerifier())
        assert (status.status, status.reason) == ("unavailable", "pipe_unverified")
    finally:
        if child.is_alive():
            parent.send("release")
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join(5)
        parent.close()
    assert child.exitcode == 0
    assert cu._windows_lock_probe(path) == "free"


@native
def test_windows_native_sharing_denied_is_unverified(tmp_path):
    from ctypes import wintypes
    path = tmp_path / "lock"
    private_lock(path)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0xC0000000, 0, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        assert cu._windows_lock_probe(path) == "unverified"
    finally:
        assert api.CloseHandle(handle)


@native
@pytest.mark.parametrize("extra,accepted", [("", True), ("(A;;FA;;;SY)", True),
                                            ("(A;;FA;;;WD)", False), ("(A;;FA;;;BA)", False)])
def test_windows_native_artifact_dacl(tmp_path, extra, accepted):
    from core.private_fs import windows_security
    security = windows_security()
    path = tmp_path / "lock"
    private_lock(path)
    sid = security.current_user_sid
    security.secure_existing_owned_path(path, sddl=f"O:{sid}D:P(A;;FA;;;{sid}){extra}")
    assert cu._windows_private_artifact_is_safe(path) is accepted
    assert cu._windows_lock_probe(path) == ("free" if accepted else "unverified")
