"""Computer Use's Windows-only, atomically job-owned Node process.

JOB_LIST assigns the child at CreateProcess time; post-creation assignment is
not a substitute (the parent can die in between). HANDLE_LIST inherits only
stdin and null output, never the non-inheritable kill-on-close job handle.
Unsupported Job assignment fails closed. This is lifecycle containment, not a
sandbox against arbitrary code running as the same Windows user.
"""

from __future__ import annotations

import asyncio
import ctypes
from functools import lru_cache
import math
import os
import subprocess
import uuid
from typing import Any, IO


@lru_cache(maxsize=1)
def _api() -> Any:
    from ctypes import wintypes as w
    from core.agent_core.tools.windows_job import _kernel32

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
            ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
            ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
            ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
            ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
            ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
            ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
        ]

    class StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("hProcess", w.HANDLE), ("hThread", w.HANDLE),
            ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD),
        ]

    api = _kernel32()
    signatures = {
        "InitializeProcThreadAttributeList": (
            [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL,
        ),
        "UpdateProcThreadAttribute": (
            [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
             ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], w.BOOL,
        ),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "DuplicateHandle": (
            [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD], w.BOOL,
        ),
        "CreateProcessW": (
            [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
             w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfoEx),
             ctypes.POINTER(ProcessInfo)], w.BOOL,
        ),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "CreateNamedPipeW": (
            [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p], w.HANDLE,
        ),
        "GetNamedPipeClientProcessId": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "DisconnectNamedPipe": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = args
        function.restype = result
    api.StartupInfoEx = StartupInfoEx
    api.ProcessInfo = ProcessInfo
    api.HANDLE = w.HANDLE
    return api


def _error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


class WindowsKernelPipe:
    """Single-use owner+SYSTEM pipe, admitted only for the owned Node PID.

    CPython's stock start_serving_pipe cannot supply a security descriptor.
    Create the overlapped handle with the existing private ACL policy, then
    use the same Proactor accept/duplex transport primitives (signatures audited
    across CPython 3.10–3.15; native tests pin their actual behavior). No TCP fallback.
    """

    def __init__(self) -> None:
        from asyncio.windows_utils import PipeHandle
        from core.private_fs import WindowsSecurity

        self._pipe = None
        self._loop = asyncio.get_running_loop()
        if (
            not isinstance(self._loop, asyncio.ProactorEventLoop)
            or not callable(getattr(getattr(self._loop, "_proactor", None), "accept_pipe", None))
            or not callable(getattr(self._loop, "_make_duplex_pipe_transport", None))
        ):
            raise OSError("Computer Use kernel pipes require a Windows Proactor event loop")
        self._api = _api()
        self.path = rf"\\.\pipe\avibe-computer-js-{uuid.uuid4().hex}"
        security = WindowsSecurity()
        with security._security_attributes() as attributes:
            handle = self._api.CreateNamedPipeW(
                self.path,
                0x00000003 | 0x40000000 | 0x00080000,  # DUPLEX | OVERLAPPED | FIRST_INSTANCE
                0x00000008,  # byte stream, blocking/overlapped, REJECT_REMOTE_CLIENTS
                1, 65536, 65536, 0, ctypes.byref(attributes),
            )
            if handle is None or handle == ctypes.c_void_p(-1).value:
                raise _error()
        try:
            self._pipe = PipeHandle(handle)
        except BaseException:
            self._api.CloseHandle(handle)
            raise

    async def connect(self, process: AsyncWindowsProcess, *, limit: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        pipe = self._pipe
        exited = asyncio.create_task(process.wait())
        accepting = None
        try:
            # A same-user unexpected client is refused, not allowed to consume
            # this generation forever. The manager also owns a total deadline.
            for _ in range(8):
                accepting = self._loop._proactor.accept_pipe(pipe)
                await asyncio.wait({accepting, exited}, return_when=asyncio.FIRST_COMPLETED)
                if exited.done() or process.returncode is not None:
                    raise OSError("Computer Use kernel exited before pipe admission")
                await accepting
                client_pid = self._api.DWORD()
                if not self._api.GetNamedPipeClientProcessId(pipe.fileno(), ctypes.byref(client_pid)):
                    raise _error()
                if client_pid.value == process.pid:
                    break
                if not self._api.DisconnectNamedPipe(pipe.fileno()) and ctypes.get_last_error() != 233:
                    raise _error()
                await asyncio.sleep(0)
            else:
                raise PermissionError("Too many unexpected Computer Use kernel pipe clients")
            reader = asyncio.StreamReader(limit=limit)
            protocol = asyncio.StreamReaderProtocol(reader)
            ready = self._loop.create_future()
            transport = self._loop._make_duplex_pipe_transport(pipe, protocol, ready)
            self._pipe = None  # transport now owns the handle, including cancellation
            try:
                await ready
            except BaseException:
                transport.close()
                raise
            return reader, asyncio.StreamWriter(transport, protocol, reader, self._loop)
        finally:
            if accepting is not None:
                accepting.cancel()
            exited.cancel()
            await asyncio.gather(exited, *([accepting] if accepting is not None else []), return_exceptions=True)

    def close(self) -> None:
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None


class WindowsProcess:
    """Minimal Popen surface needed by Computer Use; owns the whole tree.

    Construct/wait on a worker when called from asyncio. ``kill`` and ``poll``
    are nonblocking. Call ``close`` after reaping to release the process handle.
    """

    def __init__(
        self, command: list[str], *, env: dict[str, str],
        cwd: str | None = None, stdin: IO[Any] | None = None,
    ) -> None:
        self.args = command
        self.returncode: int | None = None
        self._job = None
        self._handle = None
        self._api = _api()
        try:
            self._launch(command, env, cwd, stdin)
        except BaseException:
            self.close()
            raise

    def _launch(self, command, env, cwd, stdin) -> None:
        import msvcrt

        api = self._api
        self._job = api.CreateJobObjectW(None, None)
        if not self._job:
            raise _error()
        limits = api.ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        if not api.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise _error()

        inherited = []
        attributes = None
        info = api.ProcessInfo()
        try:
            # Duplicate only these handles, rather than temporarily making the
            # caller's originals inheritable in a multi-threaded Controller.
            with open(os.devnull, "r+b", buffering=0) as null:
                current = api.GetCurrentProcess()
                for stream in (stdin if stdin is not None else null, null):
                    duplicate = api.HANDLE()
                    if not api.DuplicateHandle(
                        current, msvcrt.get_osfhandle(stream.fileno()), current,
                        ctypes.byref(duplicate), 0, True, 2,  # DUPLICATE_SAME_ACCESS
                    ):
                        raise _error()
                    inherited.append(duplicate.value)

            size = ctypes.c_size_t()
            api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise _error()
            storage = ctypes.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(storage, 2, 0, ctypes.byref(size)):
                raise _error()
            attributes = storage
            handles = (api.HANDLE * len(inherited))(*inherited)
            jobs = (api.HANDLE * 1)(self._job)
            # Arrays must stay alive until DeleteProcThreadAttributeList.
            for key, values in ((0x20002, handles), (0x2000D, jobs)):
                if not api.UpdateProcThreadAttribute(
                    attributes, 0, key, values, ctypes.sizeof(values), None, None,
                ):
                    raise _error()
            startup = api.StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = inherited[0]
            startup.StartupInfo.hStdOutput = inherited[1]
            startup.StartupInfo.hStdError = inherited[1]
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0"
            )
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            # Atomic membership applies even if the parent dies before resume.
            flags = 0x00080000 | 0x00000400 | 0x00000004 | 0x08000000
            if not api.CreateProcessW(
                command[0], command_line, None, None, True, flags,
                environment, cwd, ctypes.byref(startup), ctypes.byref(info),
            ):
                raise _error()
            self._handle = info.hProcess
            self.pid = info.dwProcessId
            if api.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise _error()
        finally:
            # Capture even when a Python interrupt arrives immediately after
            # CreateProcess returns, before the normal ownership assignment.
            if info.hProcess and self._handle is None:
                self._handle = info.hProcess
            if info.hThread:
                api.CloseHandle(info.hThread)
            if attributes is not None:
                api.DeleteProcThreadAttributeList(attributes)
            for handle in inherited:
                api.CloseHandle(handle)

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                self.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
        status = self._api.WaitForSingleObject(self._handle, milliseconds)
        if status == 0x102:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if status != 0:
            raise _error()
        code = self._api.DWORD()
        if not self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise _error()
        self.returncode = code.value
        return self.returncode

    def kill(self) -> None:
        if self._job:
            self._api.CloseHandle(self._job)
            self._job = None

    def close(self) -> None:
        self.kill()
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class AsyncWindowsProcess:
    """Async wait without an uncancellable thread holding a process handle."""

    def __init__(self, process: WindowsProcess) -> None:
        self._process = process
        self.pid = process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    async def wait(self) -> int:
        while (code := self._process.poll()) is None:
            await asyncio.sleep(0.02)
        return code

    def kill(self) -> None:
        self._process.kill()

    def close(self) -> None:
        self._process.close()


async def create_windows_process(command: list[str], **kwargs: Any) -> AsyncWindowsProcess:
    """Do not abandon a CreateProcess worker when its awaiting task is cancelled."""
    creating = asyncio.create_task(asyncio.to_thread(WindowsProcess, command, **kwargs))
    try:
        process = await asyncio.shield(creating)
    except asyncio.CancelledError:
        # Repeated shutdown/request cancellation must not propagate into the
        # thread Future and discard its eventual process owner.
        while not creating.done():
            try:
                await asyncio.shield(creating)
            except asyncio.CancelledError:
                continue
        process = creating.result()
        process.kill()
        process.close()
        raise
    return AsyncWindowsProcess(process)
