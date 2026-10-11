"""Owner-private paths: the portable rule and the Windows DACL decisions behind it."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import private_fs

windows_only = pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACLs")


def widen(path: Path) -> None:
    """Let everyone read ``path``: a wider mode on POSIX, an Everyone ACE on Windows."""

    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True, text=True)
    else:
        path.chmod(0o755 if path.is_dir() else 0o644)


def restore_windows_inheritance(root: Path) -> None:
    """Undo protected DACLs so pytest can still delete ``root`` afterwards.

    Teardown only, and deliberately not ``check=True``: a cleanup failure must
    never replace the assertion failure the test exists to report.
    """

    if os.name != "nt":
        return
    for arguments in (["/inheritance:e"], ["/reset", "/T", "/C", "/Q"]):
        subprocess.run(["icacls", str(root), *arguments], capture_output=True, text=True)


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / "private"
    try:
        yield root
    finally:
        restore_windows_inheritance(tmp_path)


def _private(path: Path) -> bool:
    return private_fs.is_private(path, path.lstat().st_mode)


def test_widened_paths_are_not_private_until_repaired(private_root):
    private_fs.ensure_private_directory(private_root)
    secret = private_root / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    private_fs.make_private_file(secret)
    assert _private(private_root) and _private(secret)

    widen(secret)
    widen(private_root)
    assert not _private(secret)
    assert not _private(private_root)
    with pytest.raises(private_fs.PrivatePathError):
        private_fs.ensure_private_directory(private_root, repair=False)

    private_fs.make_private_file(secret)
    private_fs.ensure_private_directory(private_root)
    assert _private(secret) and _private(private_root)
    private_fs.ensure_private_directory(private_root, repair=False)


@windows_only
def test_windows_private_directory_makes_what_any_process_creates_inside_private(private_root):
    """Windows ignores the umask, so only inheritance can make the engine's files private.

    The engine writes its OAuth grants with plain file APIs, and Python's
    ``mkstemp`` and ``mkdir`` ask for nothing either. Each must come out
    private from the directory's inheritable entries alone.
    """

    private_fs.ensure_private_directory(private_root)
    nested = private_root / "auth" / "nested"
    os.makedirs(nested)
    created = [private_root / "file.json", private_root / "auth" / "grant.json", nested / "deep.json"]
    for path in created:
        path.write_text("{}", encoding="utf-8")
    child_file = private_root / "from-child-process.json"
    subprocess.run([sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('{}')", child_file], check=True)

    for path in [private_root / "auth", nested, *created, child_file]:
        assert _private(path), path
    descriptor = os.open(created[0], os.O_RDONLY)
    try:
        assert private_fs.is_private(created[0], os.fstat(descriptor).st_mode, fd=descriptor)
    finally:
        os.close(descriptor)


@windows_only
def test_windows_directory_without_inheritable_entries_is_not_private(private_root):
    """Owner and SYSTEM alone are not enough: what is created inside must start private too."""

    security = private_fs.windows_security()
    security.ensure_private_directory(private_root, allow_repair=True)
    security.validate_path(private_root)
    assert not _private(private_root)

    private_fs.ensure_private_directory(private_root)
    assert _private(private_root)


class _FakeSid:
    """A SID pointer stand-in that compares by the SDDL text that produced it."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def __bool__(self) -> bool:
        return self.value is not None


class _FakeCtypes:
    @staticmethod
    def byref(cell):
        return cell


class _FakeWinTypes:
    LPVOID = _FakeSid
    BOOL = _FakeSid
    DWORD = _FakeSid


def _windows_security_stub(*, user_sid: str, owner_sid: str):
    """Drive the real owner-acceptance logic without Windows ctypes bindings.

    ``WindowsSecurity.__init__`` loads ``advapi32``/``kernel32``, so it cannot
    run off Windows. Every method under test reads only instance attributes, so
    a bypassed constructor with fake bindings exercises the real decision.
    """

    security = object.__new__(private_fs.WindowsSecurity)
    security.ctypes = _FakeCtypes
    security.wintypes = _FakeWinTypes
    security.current_user_sid = user_sid
    security.current_owner_sid = owner_sid
    security.sddl = f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)"

    def _convert(sddl, _revision, descriptor_ref, _size_ref):
        descriptor_ref.value = sddl
        return 1

    def _descriptor_owner(descriptor, owner_ref, _defaulted_ref):
        owner_ref.value = descriptor.value.split("D:", 1)[0][len("O:") :]
        return 1

    security.advapi32 = MagicMock(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=_convert,
        GetSecurityDescriptorOwner=_descriptor_owner,
        EqualSid=lambda left, right: left.value == right.value,
    )
    security.kernel32 = MagicMock(LocalFree=lambda *_args: None)
    return security


_USER_SID = "S-1-5-21-1111-2222-3333-1001"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_STRANGER_SID = "S-1-5-21-9999-8888-7777-500"


def test_elevated_token_default_owner_is_accepted_as_self():
    """An elevated token stamps BUILTIN\\Administrators on what it creates."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_ADMINISTRATORS_SID)

    assert security._owner_is_self(_FakeSid(_USER_SID)) is True
    assert security._owner_is_self(_FakeSid(_ADMINISTRATORS_SID)) is True
    assert security._owner_is_self(_FakeSid(_STRANGER_SID)) is False
    assert security._owner_is_self(_FakeSid(None)) is False


def test_administrators_is_not_trusted_when_it_is_not_this_token_owner():
    """Acceptance follows the process token, not a hardcoded well-known SID."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_USER_SID)

    assert security._self_owner_sddls() == (security.sddl,)
    assert security._owner_is_self(_FakeSid(_USER_SID)) is True
    assert security._owner_is_self(_FakeSid(_ADMINISTRATORS_SID)) is False


def test_relaxed_owner_still_requires_the_protected_private_dacl():
    """Only the owner rule moved; the DACL contract is unchanged."""

    security = _windows_security_stub(user_sid=_USER_SID, owner_sid=_ADMINISTRATORS_SID)
    security._descriptor_dacl = lambda _descriptor: "private-dacl"
    security._acl_signature = lambda acl: (str(acl).encode(),)
    control = private_fs.WindowsSecurity._SE_DACL_PRESENT | private_fs.WindowsSecurity._SE_DACL_PROTECTED

    security._descriptor_control = lambda _descriptor: control
    security._validate_security_descriptor(
        _FakeSid(_ADMINISTRATORS_SID), "private-dacl", object(), Path("runtime")
    )

    for broken_control, dacl in (
        (private_fs.WindowsSecurity._SE_DACL_PRESENT, "private-dacl"),
        (control, "inherited-dacl"),
    ):
        security._descriptor_control = lambda _descriptor, value=broken_control: value
        with pytest.raises(private_fs.PrivatePathError, match="owner or DACL"):
            security._validate_security_descriptor(
                _FakeSid(_ADMINISTRATORS_SID), dacl, object(), Path("runtime")
            )
