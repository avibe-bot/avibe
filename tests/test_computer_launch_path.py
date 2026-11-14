"""Windows launch-only path admission; native cases must execute in Windows CI."""
from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core import computer_server as cs
from core.computer_script_handles import (
    ComputerScriptHandleError, bind_handle_to_request, parse_handle_descriptor,
    validate_handle_is_current,
)
from core.computer_use import ComputerUseStatus
from tests.test_computer_server import FakeUpstream, _state


@pytest.mark.parametrize("value", [None, 1, True, "", "app.exe", r"C:app.exe", r"\app.exe",
    r"\\host\share\app.exe", r"\\?\C:\app.exe", r"\\.\C:\app.exe", "https://host/a.exe",
    r"C:\a:stream.exe", "C:\\a\x00.exe", r"C:\app.cmd", "C:\\app.exe "])
def test_literal_path_requires_drive_absolute_exe(value):
    with pytest.raises(cs.ComputerServerError):
        cs._literal_windows_executable(value)


@pytest.mark.parametrize("value", [r"C:\Program Files\Tool\App.EXE", "c:/tools/app.exe",
    r"D:\%literal%\a.exe", r"C:\a\..\b.exe", r"C:\.\b.exe", r"C:\SHORT~1\b.exe"])
def test_literal_path_leaves_resolution_to_windows(value):
    assert cs._literal_windows_executable(value) == value.replace("/", "\\")


@pytest.mark.parametrize("key,value", [("name", None), ("bundle_id", ""), ("launch_path", ""),
    ("aumid", "x"), ("urls", []), ("creates_new_application_instance", True),
    ("args", []), ("env", {}), ("cwd", "C:\\"), ("_session_id", "bad"),
    ("additional_arguments", "--foo"), ("additional_arguments", [1]),
    ("additional_arguments", ["x\0y"]), ("start_minimized", 1)])
def test_windows_launch_public_fields_are_exact(monkeypatch, key, value):
    monkeypatch.setattr(cs, "_windows_host", lambda: True)
    with pytest.raises(cs.ComputerServerError):
        cs._validate_input("launch_app", {"path": r"C:\a.exe", key: value})


def test_platform_branch_preserves_mac_and_refuses_path(monkeypatch):
    monkeypatch.setattr(cs, "_windows_host", lambda: False)
    args = {"bundle_id": "com.apple.TextEdit"}
    cs._validate_input("launch_app", args)
    assert cs._target_lock_keys("launch_app", args) == (("launch", "com.apple.TextEdit"),)
    with pytest.raises(cs.ComputerServerError, match="Windows-only"):
        cs._validate_input("launch_app", {"path": r"C:\a.exe"})


def path_descriptor():
    return {"kind": "app_path", "path": r"C:\Tools\App.exe"}


@pytest.mark.parametrize("extra", [{"pid": None}, {"pid": 42}, {"name": "app"}, {"bundle_id": None}])
def test_path_descriptor_is_exact_two_key_launch_target(extra):
    with pytest.raises(ComputerScriptHandleError):
        parse_handle_descriptor({**path_descriptor(), **extra})


def test_path_descriptor_cannot_nest_in_window():
    with pytest.raises(ComputerScriptHandleError):
        parse_handle_descriptor({"kind": "window", "pid": 42, "window_id": 9, "app": path_descriptor()})


@pytest.mark.asyncio
async def test_launch_target_never_discovers_or_attaches():
    descriptor = parse_handle_descriptor(path_descriptor())

    async def discover(*args):
        pytest.fail("a launch target must not attach via discovery")

    await validate_handle_is_current(descriptor, "launch_app", discover)
    for tool in ("kill_app", "list_windows", "get_window_state"):
        with pytest.raises(ComputerScriptHandleError):
            bind_handle_to_request(descriptor, tool, {"pid": 42})
        with pytest.raises(ComputerScriptHandleError):
            await validate_handle_is_current(descriptor, tool, discover)


def test_descriptor_binds_literal_path_and_rejects_mixed_selectors():
    descriptor = parse_handle_descriptor(path_descriptor())
    bind_handle_to_request(descriptor, "launch_app", {"path": descriptor.path})
    for args in [{"path": r"C:\other.exe"}, {"path": descriptor.path, "name": "app"},
                 {"path": descriptor.path, "pid": 42}]:
        with pytest.raises(ComputerScriptHandleError):
            bind_handle_to_request(descriptor, "launch_app", args)
    legacy = parse_handle_descriptor({"kind": "app", "pid": None, "name": "app", "bundle_id": None})
    with pytest.raises(ComputerScriptHandleError):
        bind_handle_to_request(legacy, "launch_app", {"name": "app", "path": descriptor.path})


class NativeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        assert len(args) == len(self.argtypes)
        self.calls.append(args)
        return self.callback(*args)


@pytest.fixture
def fake_kernel(monkeypatch):
    state = SimpleNamespace(drive=3, attributes=0, file_type=1, info_error=False,
                            create_error=False, final=r"\\?\C:\Tools\App.exe")
    closed = []

    def create(path, access, share, _sa, disposition, flags, _template):
        assert access == 0x80 and share == 7 and disposition == 3 and flags == 0x02000000
        return ctypes.c_void_p(-1).value if state.create_error else 10

    def info(handle, kind, pointer, size):
        assert kind == 9
        pointer._obj.attributes = state.attributes
        return not state.info_error

    def final(_handle, buffer, size, flags):
        assert flags == 0
        buffer.value = state.final
        return len(buffer.value)

    api = SimpleNamespace(
        CreateFileW=NativeFunction(create), CloseHandle=NativeFunction(lambda h: closed.append(h) or True),
        GetDriveTypeW=NativeFunction(lambda p: state.drive), GetFileType=NativeFunction(lambda h: state.file_type),
        GetFileInformationByHandleEx=NativeFunction(info), GetFinalPathNameByHandleW=NativeFunction(final),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(cs, "_windows_host", lambda: True)
    return state, api, closed


def test_single_native_open_preserves_os_spelling_and_closes(fake_kernel):
    state, api, closed = fake_kernel
    assert cs._windows_executable_path(r"C:\TOOLS\APP.EXE") == r"C:\Tools\App.exe"
    assert len(api.CreateFileW.calls) == 1
    assert closed == [10]
    assert api.GetDriveTypeW.calls == [("C:\\",)]
    state.create_error = True
    with pytest.raises(cs.ComputerServerError):
        cs._windows_executable_path(r"C:\Tools\App.exe")
    assert closed == [10]  # Never close INVALID_HANDLE_VALUE.


@pytest.mark.parametrize("attr,value", [("drive", 0), ("drive", 2), ("drive", 4), ("drive", 5),
    ("drive", 6), ("attributes", 0x10), ("file_type", 3), ("info_error", True),
    ("final", ""), ("final", r"\\?\UNC\host\share\App.exe"), ("final", r"\\?\C:\App.txt")])
def test_native_admission_refuses_nonregular_nonfixed_or_unknown(fake_kernel, attr, value):
    state, api, closed = fake_kernel
    setattr(state, attr, value)
    with pytest.raises(cs.ComputerServerError):
        cs._windows_executable_path(r"C:\Tools\App.exe")
    assert len(api.CreateFileW.calls) == 1
    assert closed == [10]


@pytest.mark.asyncio
async def test_only_canonical_string_is_the_launch_lock(fake_kernel):
    manager = cs._TargetLockManager(wait_seconds=0)
    canonical = cs._windows_executable_path(r"C:\TOOLS\APP.EXE")
    assert cs._target_lock_keys("launch_app", {}, launch_path=canonical) == (("launch_path", canonical),)
    async with await manager.acquire("launch_app", {}, launch_path=canonical):
        with pytest.raises(cs.ComputerServerError, match="busy"):
            await manager.acquire("launch_app", {}, launch_path=canonical)
        async with await manager.acquire("launch_app", {}, launch_path=r"C:\Other.exe"):
            pass


class LaunchUpstream(FakeUpstream):
    def __init__(self):
        super().__init__()
        self.launch_payload = {"pid": 42, "running": True, "name": "App", "bundle_id": None}

    async def request(self, method, params):
        result = await super().request(method, params)
        if params.get("name") == "launch_app":
            assert params["arguments"]["path"] == r"C:\Tools\App.exe"
            result["result"]["structuredContent"] = self.launch_payload.copy()
        return result


def server_for(tmp_path):
    state_path, state = _state(tmp_path)
    upstream = LaunchUpstream()
    server = cs.ComputerUseServer(state_path=state_path,
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        status_reader=lambda: ComputerUseStatus("ready", None, state))
    return server, upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_controller_forwards_canonical_and_preserves_created_pid_without_lookup(tmp_path, fake_kernel, running):
    server, upstream = server_for(tmp_path)
    upstream.launch_payload["running"] = running
    for _ in range(2):
        result = await server.call_tool("launch_app", {"session": "s", "path": r"C:\Tools\App.exe"},
                                        handle_descriptor=path_descriptor())
        assert result["structuredContent"] == upstream.launch_payload
    names = [call[1]["name"] for call in upstream.calls]
    assert names.count("launch_app") == 2  # Never turn a later launch into attachment.
    assert "list_apps" not in names
    assert fake_kernel[2] == [10, 10]


@pytest.mark.asyncio
async def test_controller_bad_file_never_dispatches(tmp_path, fake_kernel):
    server, upstream = server_for(tmp_path)
    fake_kernel[0].create_error = True
    result = await server.call_tool("launch_app", {"session": "s", "path": r"C:\Tools\App.exe"})
    assert result["isError"] and result["structuredContent"]["dispatched"] is False
    assert not upstream.calls


native = pytest.mark.skipif(os.name != "nt", reason="Windows native path API")


@native
def test_windows_native_path_canonical_regular_exe_and_missing(tmp_path):
    executable = tmp_path / "MixedCase.exe"
    shutil.copyfile(sys.executable, executable)
    canonical = cs._windows_executable_path(str(executable))
    assert canonical.endswith("\\MixedCase.exe")
    assert cs._windows_executable_path(str(executable).replace("\\", "/")) == canonical
    assert cs._windows_executable_path(str(tmp_path) + "\\.\\MixedCase.exe") == canonical
    assert cs._windows_executable_path(str(executable).lower()) == canonical
    for invalid in (tmp_path / "missing.exe", tmp_path / "directory.exe"):
        if invalid.name == "directory.exe":
            invalid.mkdir()
        with pytest.raises(cs.ComputerServerError):
            cs._windows_executable_path(str(invalid))
    executable.unlink()  # Admission leaves no retained deny-delete handle.


@native
def test_windows_native_path_follows_junction_to_final_local_file(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    shutil.copyfile(sys.executable, actual / "Target.exe")
    junction = tmp_path / "junction"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(actual)],
                   check=True, capture_output=True, timeout=10)
    try:
        assert cs._windows_executable_path(str(junction / "Target.exe")) == cs._windows_executable_path(str(actual / "Target.exe"))
    finally:
        junction.rmdir()
