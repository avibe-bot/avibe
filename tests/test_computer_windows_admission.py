"""Admission-owner tests: no Win32 calls, desktop, clipboard or credentials.

These pin the Win32 evidence decisions separately from status routing: dropping
any token, connected-handle ACL, active-session or peer check must fail here.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from core.computer_windows import AdmissionRefused, WindowsAdmission


def pointer_value(value):
    return value.value if hasattr(value, "value") else value


@contextlib.contextmanager
def descriptor(value):
    yield 222 if value == "O:SY" else 111


@pytest.fixture
def admission():
    api = object.__new__(WindowsAdmission)
    api.closed = []
    api.kernel = SimpleNamespace(CloseHandle=lambda handle: api.closed.append(pointer_value(handle)))
    api.advapi = SimpleNamespace(
        OpenProcessToken=lambda process, access, target: setattr(target._obj, "value", 44) or 1,
        EqualSid=lambda left, right: int(pointer_value(left) == pointer_value(right)),
    )
    api.security = SimpleNamespace(
        current_user_sid="S-1-5-21-123", _descriptor_from_sddl=descriptor,
        _descriptor_owner=lambda value: value,
        _ACCESS_DENIED_ACE_TYPE=1, _ACCESS_ALLOWED_ACE_TYPE=0,
    )
    return api


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class TokenUser(ctypes.Structure):
    _fields_ = [("User", SidAndAttributes)]


def token_buffers(*, elevated=0, ui_access=0, integrity=0x2000, authority=16, sid=111, malformed_pointer=False):
    label = (ctypes.c_size_t * 12)()
    sid_address = ctypes.addressof(label) + ctypes.sizeof(TokenUser)
    ctypes.cast(label, ctypes.POINTER(wintypes.LPVOID))[0] = sid_address if not malformed_pointer else 1
    ctypes.memmove(sid_address, bytes([1, 1, 0, 0, 0, 0, 0, authority]) + integrity.to_bytes(4, "little"), 12)
    return {20: wintypes.DWORD(elevated), 26: wintypes.DWORD(ui_access), 25: label,
            1: TokenUser(SidAndAttributes(sid, 0))}


@pytest.mark.parametrize("options,expected", [
    ({}, None), ({"elevated": 1}, "elevated_runtime"), ({"ui_access": 1}, "elevated_runtime"),
    ({"integrity": 0x3000}, "elevated_runtime"), ({"integrity": 0x4000}, "elevated_runtime"),
    ({"authority": 5}, "elevated_runtime"), ({"malformed_pointer": True}, "elevated_runtime"),
    ({"sid": 333}, "pipe_unverified"),
])
def test_original_token_requires_ordinary_integrity_and_same_user(admission, options, expected):
    buffers = token_buffers(**options)
    admission.security.TokenUser = TokenUser
    admission._token_information = lambda _token, kind: ctypes.pointer(buffers[kind]) if kind != 25 else buffers[kind]
    if expected:
        with pytest.raises(AdmissionRefused, match=expected):
            admission.ordinary_token(9, reason="elevated_runtime")
    else:
        admission.ordinary_token(9, reason="elevated_runtime")
    assert admission.closed == [44]


@pytest.mark.parametrize("owner,entries,expected", [
    (111, [(0, 0, 111)], True), (111, [(0, 0, 111), (0, 0, 222)], True),
    (111, [(0, 0, 333)], False), (222, [(0, 0, 111)], False),
    (None, [(0, 0, 111)], False), (111, [(5, 0, 111)], False),
])
def test_pipe_acl_is_read_from_connected_handle_and_only_current_user_or_system(admission, owner, entries, expected):
    observed = []

    def get_security(handle, kind, flags, owner_out, _group, dacl, _sacl, sd):
        observed.append((handle, kind, flags))
        owner_out._obj.value = owner
        dacl._obj.value = 123
        sd._obj.value = 789
        return 0

    admission.advapi.GetSecurityInfo = get_security
    admission.kernel.LocalFree = lambda sd: admission.closed.append(pointer_value(sd))
    admission.security._aces = lambda _dacl: entries
    if expected:
        admission.private_pipe(55)
    else:
        with pytest.raises(AdmissionRefused, match="pipe_unverified"):
            admission.private_pipe(55)
    assert observed == [(55, 1, 5)]
    assert admission.closed == [789]


@pytest.mark.parametrize("session,active,desktop,name,reason", [
    (1, 0, 9, "Default", None), (0, 0, 9, "Default", "session_unavailable"),
    (1, 4, 9, "Default", "session_unavailable"), (1, 0, None, "Default", "session_locked"),
    (1, 0, 9, "Winlogon", "session_locked"),
])
def test_interactive_requires_nonzero_active_unlocked_input_desktop(admission, session, active, desktop, name, reason):
    state = ctypes.c_int(active)
    admission.kernel.GetCurrentProcessId = lambda: 123
    admission.kernel.GetCurrentThreadId = lambda: 124
    admission.kernel.ProcessIdToSessionId = lambda pid, target: setattr(target._obj, "value", session) or 1

    def query(_server, queried_session, kind, data, size):
        assert (queried_session, kind) == (session, 8)
        data._obj.value = ctypes.addressof(state)
        size._obj.value = ctypes.sizeof(state)
        return 1

    freed = []
    admission.wts = SimpleNamespace(WTSQuerySessionInformationW=query, WTSFreeMemory=lambda data: freed.append(data.value))
    admission.user = SimpleNamespace(
        GetProcessWindowStation=lambda: 8,
        GetThreadDesktop=lambda tid: 10,
        OpenInputDesktop=lambda _flags, inherit, rights: desktop,
        CloseDesktop=lambda handle: admission.closed.append(handle),
        GetUserObjectInformationW=lambda handle, kind, output, _size, needed: setattr(output, "value", "Default" if handle == 10 else name) or 1,
    )
    if reason:
        with pytest.raises(AdmissionRefused, match=reason):
            admission.interactive_session()
    else:
        assert admission.interactive_session() == 1
    assert len(freed) == int(session != 0)
    assert admission.closed == ([9] if session and active == 0 and desktop else [])


@pytest.mark.parametrize("peer_session,alive,reason", [(1, True, None), (2, True, "session_mismatch"), (1, False, "daemon_unreachable")])
def test_peer_is_live_same_session_and_token_checked_while_handle_retained(admission, peer_session, alive, reason):
    admission.kernel.OpenProcess = lambda access, inherit, pid: 77
    admission.kernel.WaitForSingleObject = lambda handle, timeout: 258 if alive else 0
    admission.session = lambda pid: peer_session
    tokens = []
    admission.ordinary_token = lambda handle, **kw: tokens.append(handle)
    if reason:
        with pytest.raises(AdmissionRefused, match=reason):
            with admission.peer(123, 1):
                pytest.fail("unsafe peer admitted")
    else:
        with admission.peer(123, 1) as handle:
            assert handle == 77
            assert admission.closed == []
    assert tokens == ([77] if reason is None else [])
    assert admission.closed == [77]


@pytest.mark.parametrize("required", [4, 28])
def test_token_information_keeps_alignment_but_passes_exact_native_length(admission, required):
    calls = []

    def query(token, kind, buffer, length, size):
        calls.append((token, kind, length))
        size._obj.value = required
        if buffer is None:
            assert length == 0
            return 0
        assert length == required  # never allocation capacity (8 or 32)
        assert ctypes.addressof(buffer) % ctypes.alignment(ctypes.c_size_t) == 0
        assert ctypes.sizeof(buffer) >= required
        ctypes.memset(buffer, 0, length)
        return 1

    admission.advapi.GetTokenInformation = query
    admission._token_information(44, 20)
    assert calls == [(44, 20, 0), (44, 20, required)]


def test_verify_keeps_shell_and_server_evidence_and_rechecks_desktop(admission):
    events = []
    admission.kernel.GetCurrentProcess = lambda: 8
    admission.ordinary_token = lambda handle, **kw: events.append(("token", handle))
    admission.interactive_session = lambda: events.append("desktop") or 1

    @contextlib.contextmanager
    def peer(pid, session):
        events.append(("open-peer", pid, session))
        yield pid
        events.append(("close-peer", pid))

    admission.peer = peer
    admission.private_pipe = lambda handle: events.append(("acl", handle))

    def create(path, access, share, security, disposition, flags, template):
        assert (path, access, disposition, flags) == (r"\\.\pipe\generation", 0xC0020000, 3, 0x00110000)
        events.append("connect")
        return 55

    admission.kernel.CreateFileW = create
    admission.kernel.GetNamedPipeServerProcessId = lambda handle, pid: setattr(pid._obj, "value", 456) or 1
    admission.verify(r"\\.\pipe\generation", 123)
    assert events == [("token", 8), "desktop", ("open-peer", 123, 1), "connect", ("acl", 55),
                      ("open-peer", 456, 1), "desktop", ("close-peer", 456), ("close-peer", 123)]
    assert admission.closed == [55]
