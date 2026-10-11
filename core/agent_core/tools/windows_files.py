"""What ``write`` and ``edit`` need from Windows files, through ``ctypes``.

A Windows file's permissions are its ACL, which ``os.access`` and ``stat`` modes do not see: the
mode is only the read-only bit. A file replaced by a rename keeps none of its own metadata.
Importing this module works everywhere; calling it works only on Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import secrets
from typing import Any

_FILE_WRITE_DATA = 0x0002
_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_ERROR_SHARING_VIOLATION = 32
_ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 = 1177

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
    _api = kernel32
    return kernel32


def may_write(path: str) -> bool:
    """Whether this user may write the data of the existing file ``path``, by its ACL and read-only bit.

    The file is opened for writing and closed, never written. Windows checks access before sharing,
    so a sharing violation means the access was granted: another process merely has the file open.
    """
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path, _FILE_WRITE_DATA, _FILE_SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return True


def replace_existing(replacement: str, target: str) -> None:
    """Replace the existing file ``target`` with ``replacement`` through ``ReplaceFileW``, Windows' safe save.

    The result keeps ``target``'s DACL, encryption, compression, named streams (the Mark of the Web among
    them), and creation time, which a rename would drop. The replaced file passes through a backup name
    beside it, so no failure loses it: where ``ReplaceFileW`` leaves it under that name, it is put back.
    The backup is removed once the replacement is in place. Raises ``OSError`` with ``target`` as it was.
    """
    kernel32 = _kernel32()
    backup = os.path.join(os.path.dirname(target), f".avibe-{secrets.token_hex(6)}.bak")
    if kernel32.ReplaceFileW(target, replacement, backup, 0, None, None):
        with contextlib.suppress(OSError):
            os.unlink(backup)
        return
    error = ctypes.get_last_error()
    if error == _ERROR_UNABLE_TO_MOVE_REPLACEMENT_2:
        # The replaced file is at the backup name and the replacement could not take its place.
        os.replace(backup, target)
    raise OSError(None, ctypes.FormatError(error), target, error)
