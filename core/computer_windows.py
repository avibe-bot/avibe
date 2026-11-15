"""Windows Computer Use admission, on the connected pipe handle.

The private shell state/lock and per-generation endpoint are the v1 trust
boundary. A PID is checked for liveness, user and session, not treated as a
secret or as independent proof of the shell's child-generation binding.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from pathlib import Path

from core.private_fs import windows_security


def read_state_text(path: Path) -> str:
    """Read one generation without blocking the shell's atomic replacement."""
    import io
    import msvcrt
    import os

    security = windows_security()

    def opener(name: str, _flags: int) -> int:
        # READ | WRITE | DELETE sharing is essential: the shell publishes the
        # next generation by replacing this path while readers retain the old
        # handle. OPEN_EXISTING neither creates nor repairs state.
        handle = security.kernel32.CreateFileW(name, 0x80000000, 7, None, 3, 0x80, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            security.kernel32.CloseHandle(handle)
            raise

    with io.open(path, "r", encoding="utf-8", opener=opener) as stream:
        return stream.read()


class AdmissionRefused(OSError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class WindowsAdmission:
    def __init__(self) -> None:
        self.security = windows_security()
        self.kernel = self.security.kernel32
        self.advapi = self.security.advapi32
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        pointer = ctypes.POINTER
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetCurrentProcessId.restype = wintypes.DWORD
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.user.GetProcessWindowStation.restype = wintypes.HANDLE
        self.user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        self.user.GetThreadDesktop.restype = wintypes.HANDLE
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, pointer(wintypes.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, pointer(wintypes.DWORD)]
        self.kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.user.CloseDesktop.restype = wintypes.BOOL
        self.user.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, pointer(wintypes.DWORD),
        ]
        self.user.GetUserObjectInformationW.restype = wintypes.BOOL
        self.wts.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, pointer(wintypes.LPVOID), pointer(wintypes.DWORD),
        ]
        self.wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
        self.wts.WTSFreeMemory.argtypes = [wintypes.LPVOID]
        self.wts.WTSFreeMemory.restype = None

    def session(self, pid: int) -> int:
        session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, ctypes.byref(session)) or not session.value:
            raise AdmissionRefused("session_unavailable")
        return session.value

    def interactive_session(self) -> int:
        session = self.session(self.kernel.GetCurrentProcessId())
        data, size = wintypes.LPVOID(), wintypes.DWORD()
        # WTSConnectState (8), WTSActive (0). Disconnected RDP is not interactive.
        if not self.wts.WTSQuerySessionInformationW(None, session, 8, ctypes.byref(data), ctypes.byref(size)):
            raise AdmissionRefused("session_unavailable")
        try:
            if not data or size.value != ctypes.sizeof(ctypes.c_int) or ctypes.cast(data, ctypes.POINTER(ctypes.c_int))[0] != 0:
                raise AdmissionRefused("session_unavailable")
        finally:
            self.wts.WTSFreeMemory(data)
        if not self.user.GetProcessWindowStation():
            raise AdmissionRefused("session_unavailable")
        thread_desktop = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
        if not thread_desktop or self._desktop_name(thread_desktop).casefold() != "default":
            raise AdmissionRefused("session_unavailable")
        desktop = self.user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            raise AdmissionRefused("session_locked")
        try:
            if self._desktop_name(desktop).casefold() != "default":
                raise AdmissionRefused("session_locked")
        finally:
            self.user.CloseDesktop(desktop)
        return session

    def _desktop_name(self, desktop: object) -> str:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            raise AdmissionRefused("session_unavailable")
        return name.value

    def _token_information(self, token: object, kind: int) -> ctypes.Array:
        size = wintypes.DWORD()
        self.advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise AdmissionRefused("pipe_unverified") from ctypes.WinError(ctypes.get_last_error())
        # Win32 structures contain pointers; use pointer-aligned storage.
        buffer = (ctypes.c_size_t * ((size.value + ctypes.sizeof(ctypes.c_size_t) - 1) // ctypes.sizeof(ctypes.c_size_t)))()
        if not self.advapi.GetTokenInformation(token, kind, buffer, ctypes.sizeof(buffer), ctypes.byref(size)):
            raise AdmissionRefused("pipe_unverified") from ctypes.WinError(ctypes.get_last_error())
        return buffer

    def ordinary_token(self, process: object, *, reason: str) -> None:
        token = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise AdmissionRefused(reason)
        try:
            # TokenElevation (20), TokenUIAccess (26), TokenIntegrityLevel (25).
            elevation = self._token_information(token, 20)
            ui_access = self._token_information(token, 26)
            if ctypes.cast(elevation, ctypes.POINTER(wintypes.DWORD))[0] or ctypes.cast(ui_access, ctypes.POINTER(wintypes.DWORD))[0]:
                raise AdmissionRefused(reason)
            integrity = self._token_information(token, 25)
            sid = ctypes.cast(integrity, ctypes.POINTER(wintypes.LPVOID))[0]
            start = ctypes.addressof(integrity)
            if not sid or not start + ctypes.sizeof(self.security.TokenUser) <= sid <= start + ctypes.sizeof(integrity) - 12:
                raise AdmissionRefused(reason)
            raw = ctypes.string_at(sid, 12)
            # Integrity labels are S-1-16-RID; accept ordinary <= medium only.
            if raw[:8] != b"\x01\x01\x00\x00\x00\x00\x00\x10" or int.from_bytes(raw[8:12], "little") > 0x2000:
                raise AdmissionRefused(reason)
            user = self._token_information(token, 1)
            user_sid = ctypes.cast(user, ctypes.POINTER(self.security.TokenUser)).contents.User.Sid
            with self.security._descriptor_from_sddl(f"O:{self.security.current_user_sid}") as descriptor:
                if not self.advapi.EqualSid(user_sid, self.security._descriptor_owner(descriptor)):
                    raise AdmissionRefused("pipe_unverified")
        finally:
            self.kernel.CloseHandle(token)

    @contextlib.contextmanager
    def peer(self, pid: int, session: int):
        # Keep the process handle alive until endpoint verification completes.
        process = self.kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not process:
            raise AdmissionRefused("pipe_unverified")
        try:
            if self.kernel.WaitForSingleObject(process, 0) != 258:  # WAIT_TIMEOUT = still running
                raise AdmissionRefused("daemon_unreachable")
            if self.session(pid) != session:
                raise AdmissionRefused("session_mismatch")
            self.ordinary_token(process, reason="pipe_unverified")
            yield process
            if self.kernel.WaitForSingleObject(process, 0) != 258:
                raise AdmissionRefused("daemon_unreachable")
        finally:
            self.kernel.CloseHandle(process)

    def private_pipe(self, handle: object) -> None:
        owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
        result = self.advapi.GetSecurityInfo(
            handle, 1, 0x00000001 | 0x00000004,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if result:
            raise AdmissionRefused("pipe_unverified")
        try:
            with contextlib.ExitStack() as stack:
                trusted = [self.security._descriptor_owner(stack.enter_context(
                    self.security._descriptor_from_sddl(f"O:{sid}")
                )) for sid in (self.security.current_user_sid, "SY")]
                if not owner or not dacl or not self.advapi.EqualSid(owner, trusted[0]):
                    raise AdmissionRefused("pipe_unverified")
                for ace_type, _flags, sid in self.security._aces(dacl):
                    if ace_type == self.security._ACCESS_DENIED_ACE_TYPE:
                        continue
                    if ace_type != self.security._ACCESS_ALLOWED_ACE_TYPE or not any(self.advapi.EqualSid(sid, candidate) for candidate in trusted):
                        raise AdmissionRefused("pipe_unverified")
        finally:
            self.kernel.LocalFree(descriptor)

    def verify(self, endpoint: str, shell_pid: int) -> None:
        self.ordinary_token(self.kernel.GetCurrentProcess(), reason="elevated_runtime")
        session = self.interactive_session()
        with self.peer(shell_pid, session):
            # Never permit a server to impersonate this Runtime. READ_CONTROL
            # is needed for the security descriptor on this connected handle.
            pipe = self.kernel.CreateFileW(endpoint, 0xC0020000, 0, None, 3, 0x00110000, None)
            if pipe in (None, ctypes.c_void_p(-1).value):
                raise AdmissionRefused("daemon_unreachable")
            try:
                self.private_pipe(pipe)
                pid = wintypes.DWORD()
                if not self.kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(pid)) or not pid.value:
                    raise AdmissionRefused("pipe_unverified")
                with self.peer(pid.value, session):
                    if self.interactive_session() != session:
                        raise AdmissionRefused("session_mismatch")
            finally:
                self.kernel.CloseHandle(pipe)


def windows_ready_refusal(endpoint: str, shell_pid: int) -> str | None:
    """Return a stable refusal; unknown Win32/security evidence fails closed."""
    try:
        WindowsAdmission().verify(endpoint, shell_pid)
    except AdmissionRefused as exc:
        return exc.reason
    except (OSError, ImportError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        return "pipe_unverified"
    return None
