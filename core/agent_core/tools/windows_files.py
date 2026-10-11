"""What ``write`` and ``edit`` need from Windows files, through ``ctypes``.

POSIX gives them two guarantees by default that Windows gives only on request:

- privacy from the start: a file's permissions are its ACL, which ``os.access`` and ``stat`` modes do
  not see (the mode is only the read-only bit), and a new file has its directory's;
- a change time: ``stat`` reports the creation time as ``st_ctime`` on Python before 3.12.

A file replaced by a rename also keeps none of its own metadata. Importing this module works
everywhere; calling it works only on Windows.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import secrets
from typing import Any

logger = logging.getLogger(__name__)

_FILE_WRITE_DATA = 0x0002
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ_WRITE = 0x1 | 0x2
_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_BASIC_INFO = 0
_ERROR_SHARING_VIOLATION = 32
_ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 = 1177
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_LABEL_SECURITY_INFORMATION = 0x10
_INHERITED_ACE = 0x10
_TOKEN_QUERY = 0x0008
_TOKEN_OWNER = 4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_DACL_PROTECTED = 0x1000
_FILE_PERSISTENT_ACLS = 0x8
_MAX_PATH = 32_768
# Protected, so nothing is inherited: the owner and SYSTEM alone, as a POSIX 0600 file is the owner's.
_PRIVATE_SDDL = "D:P(A;;FA;;;OW)(A;;FA;;;SY)"
# FILETIME counts 100 ns from 1601-01-01; the Unix epoch is this many of them later.
_UNIX_EPOCH_FILETIME = 116_444_736_000_000_000

_api: Any = None


def _kernel32() -> Any:
    """kernel32 with the signatures used here, set up on first use."""
    global _api
    if _api is not None:
        return _api
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.advapi32 = advapi32
    _api = kernel32
    return kernel32


def _invalid(handle: Any) -> bool:
    return handle is None or handle == ctypes.c_void_p(-1).value


def _error(path: str) -> OSError:
    # With a winerror, OSError takes its errno, and so its subclass, from it.
    error = ctypes.get_last_error()
    return OSError(None, ctypes.FormatError(error), path, error)


def may_write(path: str) -> bool:
    """Whether this user may write the data of the existing file ``path``, by its ACL and read-only bit.

    The file is opened for writing and closed, never written. Windows checks access before sharing,
    so a sharing violation means the access was granted: another process merely has the file open.
    """
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path, _FILE_WRITE_DATA, _FILE_SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None
    )
    if _invalid(handle):
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return True


def open_for_rewriting(path: str) -> int:
    """A binary, write-only descriptor of the existing file ``path`` that asks for writing the data alone.

    ``os.open`` asks for ``GENERIC_WRITE``, which includes reading the file's security, so it fails on a file
    whose ACL grants writing but not ``READ_CONTROL``.
    """
    import msvcrt

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(path, _FILE_WRITE_DATA, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None)
    if _invalid(handle):
        raise _error(path)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def create_private(path: str) -> int:
    """Create ``path``, which must not exist, for writing, with an owner-only DACL: a binary descriptor.

    The counterpart of a 0600 file, so contents written to it are the owner's alone from the first byte.
    Raises ``OSError`` as ``os.open`` would (``FileExistsError``, ``PermissionError``).
    """
    import msvcrt
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32 = _kernel32()
    descriptor = ctypes.c_void_p()
    if not kernel32.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _PRIVATE_SDDL, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise _error(path)
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        handle = kernel32.CreateFileW(
            path,
            _GENERIC_WRITE,
            _FILE_SHARE_READ_WRITE,
            ctypes.byref(attributes),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if _invalid(handle):
            raise _error(path)
    finally:
        kernel32.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def owned_as_new_files_are(path: str) -> bool:
    """Whether the file ``path`` has the owner this process gives the files it creates (its token's default owner:
    the user, or the Administrators group in an elevated session); ``False`` when its owner cannot be read."""
    from ctypes import wintypes

    kernel32 = _kernel32()
    advapi32 = kernel32.advapi32
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _error(path)
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_OWNER, None, 0, ctypes.byref(needed))
        default = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_OWNER, default, needed, ctypes.byref(needed)):
            raise _error(path)
    finally:
        kernel32.CloseHandle(token)
    default_owner = ctypes.c_void_p.from_buffer(default).value  # TOKEN_OWNER is the owner SID's pointer
    descriptor, owner = ctypes.c_void_p(), ctypes.c_void_p()
    if advapi32.GetNamedSecurityInfoW(
        path, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION, ctypes.byref(owner), None, None, None,
        ctypes.byref(descriptor),
    ):
        return False  # READ_CONTROL denied, or a volume without owners
    try:
        return bool(owner.value) and bool(advapi32.EqualSid(owner, default_owner))
    finally:
        kernel32.LocalFree(descriptor)


def copy_security(source: str, destination: str) -> None:
    """Give ``destination`` exactly the DACL of ``source`` (its own entries, and whether it inherits from its
    directory, whose inheritable entries are then recomputed as for any file) and its explicit integrity label.
    Nothing on a volume that keeps no ACLs (FAT).

    ``ReplaceFileW`` merges the replaced file's DACL into the replacement's rather than putting it in its place,
    so a replacement created owner-only is given the file's own security first. ``source`` is a file this user
    owns, so its owner may read both.
    """
    from ctypes import wintypes

    kernel32 = _kernel32()
    root = ctypes.create_unicode_buffer(_MAX_PATH)
    flags = wintypes.DWORD()
    if not kernel32.GetVolumePathNameW(source, root, _MAX_PATH) or not kernel32.GetVolumeInformationW(
        root.value, None, 0, None, None, ctypes.byref(flags), None, 0
    ):
        raise _error(source)
    if not flags.value & _FILE_PERSISTENT_ACLS:
        return
    advapi32 = kernel32.advapi32
    descriptor, dacl, sacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        source,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION | _LABEL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if error:
        raise OSError(None, ctypes.FormatError(error), source, error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise _error(source)
        inheritance = (
            _PROTECTED_DACL_SECURITY_INFORMATION
            if control.value & _SE_DACL_PROTECTED
            else _UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        information = _DACL_SECURITY_INFORMATION | inheritance
        # The label is the SACL's one entry readable without privilege; an inherited one the replacement inherits.
        ace = ctypes.c_void_p()
        if sacl.value and advapi32.GetAce(sacl, 0, ctypes.byref(ace)):
            if not ctypes.c_ubyte.from_address(ace.value + 1).value & _INHERITED_ACE:  # ACE_HEADER.AceFlags
                information |= _LABEL_SECURITY_INFORMATION
        error = advapi32.SetNamedSecurityInfoW(destination, _SE_FILE_OBJECT, information, None, None, dacl, sacl)
        if error:
            raise OSError(None, ctypes.FormatError(error), destination, error)
    finally:
        kernel32.LocalFree(descriptor)


def change_time_ns(fd: int) -> int:
    """The change time of the open file ``fd``: any write or metadata change sets it, and nothing sets it back."""
    import msvcrt
    from ctypes import wintypes

    class BasicInformation(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    information = BasicInformation()
    handle = msvcrt.get_osfhandle(fd)
    if not _kernel32().GetFileInformationByHandleEx(
        handle, _FILE_BASIC_INFO, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise _error(f"<descriptor {fd}>")
    return (information.ChangeTime - _UNIX_EPOCH_FILETIME) * 100


def replace_existing(replacement: str, target: str) -> None:
    """Replace the existing file ``target`` with ``replacement`` through ``ReplaceFileW``, Windows' safe save.

    The result keeps ``target``'s encryption, compression, named streams (the Mark of the Web among them),
    and creation time, which a rename would drop, and merges its DACL into the replacement's (``copy_security``
    makes them the same first). The replaced file passes through a backup name beside it, so no failure loses
    it: where ``ReplaceFileW`` leaves it under that name, it is put back, unless a file was created at
    ``target`` meanwhile, which is never overwritten; then the error names the backup. The backup is removed
    once the replacement is in place. Raises ``OSError``.
    """
    kernel32 = _kernel32()
    backup = os.path.join(os.path.dirname(target), f".avibe-{secrets.token_hex(6)}.bak")
    if kernel32.ReplaceFileW(target, replacement, backup, 0, None, None):
        try:
            os.unlink(backup)
        except OSError as exc:
            # ReplaceFileW succeeded, so every handle on the replaced file shared delete; only a program that
            # opened the backup since can stop this. The write stands, and the copy it left is reported.
            logger.warning("Could not remove %s, the copy of %s from before it was replaced: %s", backup, target, exc)
        return
    error = ctypes.get_last_error()
    if error == _ERROR_UNABLE_TO_MOVE_REPLACEMENT_2:
        # The replaced file is at the backup name and the replacement could not take its place. Put it back with
        # a rename that fails rather than overwrite a file another program created there in the gap.
        try:
            os.rename(backup, target)
        except FileExistsError:
            raise FileExistsError(
                errno.EEXIST, f"it was created again while it was being replaced; its earlier content is in {backup}"
            ) from None
    raise OSError(None, ctypes.FormatError(error), target, error)
