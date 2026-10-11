"""What ``write`` and ``edit`` need from Windows files, through ``ctypes``.

POSIX gives them two guarantees by default that Windows gives only on request:

- privacy from the start: a file's permissions are its ACL, which ``os.access`` and ``stat`` modes do
  not see (the mode is only the read-only bit), and a new file has its directory's;
- a change time: ``stat`` reports the creation time as ``st_ctime`` on Python before 3.12.

An existing file is rewritten in place (C-7 tools.md section 3): a replacement would be a new file
object, which keeps none of the old one's owner, security, streams, or links. Importing this module
works everywhere; calling it works only on Windows.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any

_FILE_WRITE_DATA = 0x0002
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_READ_WRITE = 0x1 | 0x2
_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_BASIC_INFO = 0
_ERROR_SHARING_VIOLATION = 32
_SDDL_REVISION_1 = 1
# Protected, so nothing is inherited: the owner and SYSTEM alone, as a POSIX 0600 file is the owner's.
_PRIVATE_SDDL = "D:P(A;;FA;;;OW)(A;;FA;;;SY)"
# FILETIME counts 100 ns from 1601-01-01; the Unix epoch is this many of them later.
_UNIX_EPOCH_FILETIME = 116_444_736_000_000_000

_api: Any = None


def _kernel32() -> Any:
    """kernel32, and the one advapi32 call used here, with their signatures, set up on first use."""
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
    """A binary, write-only descriptor of the existing file ``path`` that asks for writing the data alone, and
    lets no other writer in while it is open.

    ``os.open`` asks for ``GENERIC_WRITE``, which includes reading the file's security, so it fails on a file
    whose ACL grants writing but not ``READ_CONTROL``. Without write sharing, a program that has the file open
    for writing makes this open fail before a byte changes, and none can open it for writing meanwhile.
    """
    import msvcrt

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(path, _FILE_WRITE_DATA, _FILE_SHARE_READ, None, _OPEN_EXISTING, 0, None)
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
