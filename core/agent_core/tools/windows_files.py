"""What ``write`` and ``edit`` need from Windows file security, through ``ctypes``.

A Windows file's permissions are its ACL, which ``os.access`` and ``stat`` modes do not see: the
mode is only the read-only bit. Importing this module works everywhere; calling it works only on
Windows.
"""

from __future__ import annotations

import ctypes
from typing import Any

_FILE_WRITE_DATA = 0x0002
_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_ERROR_SHARING_VIOLATION = 32
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_DACL_PROTECTED = 0x1000

_api: Any = None


def _dlls() -> Any:
    """kernel32 and advapi32 with the signatures used here, set up on first use."""
    global _api
    if _api is not None:
        return _api
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
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
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
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
    _api = (kernel32, advapi32, wintypes)
    return _api


def may_write(path: str) -> bool:
    """Whether this user may write the data of the existing file ``path``, by its ACL and read-only bit.

    The file is opened for writing and closed, never written. Windows checks access before sharing,
    so a sharing violation means the access was granted: another process merely has the file open.
    """
    kernel32, _, _ = _dlls()
    handle = kernel32.CreateFileW(
        path, _FILE_WRITE_DATA, _FILE_SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return True


def copy_dacl(source: str, destination: str) -> None:
    """Give ``destination`` the DACL of ``source``: its own entries, and whether it inherits from its directory.

    A replacement file otherwise has only what its directory passes on, so a file kept private by an
    ACL of its own would lose it when it is replaced.
    """
    kernel32, advapi32, wintypes = _dlls()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        source, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if error:
        raise OSError(None, ctypes.FormatError(error), source, error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            error = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(error), source, error)
        # Inherited entries in the DACL are recomputed from the destination's directory, as for any file.
        protection = (
            _PROTECTED_DACL_SECURITY_INFORMATION
            if control.value & _SE_DACL_PROTECTED
            else _UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        error = advapi32.SetNamedSecurityInfoW(
            destination, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | protection, None, None, dacl, None
        )
        if error:
            raise OSError(None, ctypes.FormatError(error), destination, error)
    finally:
        kernel32.LocalFree(descriptor)
