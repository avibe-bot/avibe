"""What ``write`` and ``edit`` need from Windows files, through ``ctypes``.

POSIX gives them two guarantees by default that Windows gives only on request:

- privacy from the start: a file's permissions are its ACL, which ``os.access`` and ``stat`` modes do
  not see (the mode is only the read-only bit), and a new file has its directory's;
- a change time: ``stat`` reports the creation time as ``st_ctime`` on Python before 3.12.

An existing file is rewritten in place (C-7 tools.md section 3): a replacement would be a new file
object, which keeps none of the old one's owner, security, streams, or links.

A job's own files are another matter. A file opened with Python's ``open`` or ``os.open`` cannot be
renamed over until it is closed: a reader opened here can be (``open_for_reading``), but only by a
rename with POSIX semantics (``replace``); ``os.replace`` fails even then.

Standard library only: the job wrapper beside it runs as ``python -I -S`` and loads this file by its
path. Importing it works everywhere; calling it works only on Windows.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any, Optional

_FILE_WRITE_DATA = 0x0002
_FILE_READ_ATTRIBUTES = 0x0080
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_READ_WRITE = 0x1 | 0x2
_FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_RENAME_INFO_EX = 22
_FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x1
_FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x2
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_PARAMETER = 87
_FILE_BASIC_INFO = 0
_FILE_ATTRIBUTE_TAG_INFO = 9
# IsReparseTagNameSurrogate: the reparse point names another file (a symbolic link, a junction).
_REPARSE_TAG_NAME_SURROGATE = 0x20000000
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
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
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


def open_for_rewriting(path: str) -> Optional[int]:
    """A binary, write-only descriptor of the existing file ``path`` that asks for writing the data (and reading
    the attributes) alone, and lets no other program write, rename, or delete the file while it is open;
    ``None`` when ``path`` names a link (a symbolic link or a junction) rather than a file.

    The open does not follow a link. Any other reparse point (a OneDrive placeholder, a compressed file) belongs
    to the file itself, which is then opened normally, so the filter behind it does its work.

    ``os.open`` asks for ``GENERIC_WRITE``, which includes reading the file's security, so it fails on a file
    whose ACL grants writing but not ``READ_CONTROL``. Without write sharing, a program that has the file open
    for writing makes this open fail before a byte changes, and none can open it for writing meanwhile.
    """
    import msvcrt

    kernel32 = _kernel32()

    def open_file(flags: int) -> Any:
        handle = kernel32.CreateFileW(
            path, _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES, _FILE_SHARE_READ, None, _OPEN_EXISTING, flags, None
        )
        if _invalid(handle):
            raise _error(path)
        return handle

    handle = open_file(_FILE_FLAG_OPEN_REPARSE_POINT)
    try:
        tag = _reparse_tag(handle, path)
        if tag is not None:
            kernel32.CloseHandle(handle)
            handle = None
            if tag & _REPARSE_TAG_NAME_SURROGATE:
                return None
            handle = open_file(0)
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        if handle is not None:
            kernel32.CloseHandle(handle)
        raise


def _reparse_tag(handle: Any, path: str) -> Optional[int]:
    """The reparse tag of the open file ``handle``, or ``None`` when it is not a reparse point.

    The attributes come first: a volume without reparse points (FAT) does not answer for their tags.
    """
    from ctypes import wintypes

    class AttributeTagInformation(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    if not _basic_information(handle, path).FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    information = AttributeTagInformation()
    if not _kernel32().GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise _error(path)
    return int(information.ReparseTag)


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


def open_attributes(path: str) -> int:
    """A descriptor of the file ``path`` that can read its attributes and nothing else, so ``os.fstat`` and
    ``change_time_ns`` work on a file whose ACL denies reading its data or its security."""
    import msvcrt

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None
    )
    if _invalid(handle):
        raise _error(path)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _basic_information(handle: Any, path: str) -> Any:
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
    if not _kernel32().GetFileInformationByHandleEx(
        handle, _FILE_BASIC_INFO, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise _error(path)
    return information


def change_time_ns(fd: int) -> int:
    """The change time of the open file ``fd``: any write or metadata change sets it, and nothing sets it back."""
    import msvcrt

    information = _basic_information(msvcrt.get_osfhandle(fd), f"<descriptor {fd}>")
    return (information.ChangeTime - _UNIX_EPOCH_FILETIME) * 100


def open_for_reading(path: str) -> int:
    """A binary, read-only descriptor of the file ``path``, shared for delete, so ``replace`` can still
    rename a new file onto its name; this reader keeps reading the file it opened. Raises ``OSError`` as
    ``open`` does (``FileNotFoundError`` for a missing file)."""
    import msvcrt

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(path, _GENERIC_READ, _FILE_SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None)
    if _invalid(handle):
        raise _error(path)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def replace(source: str, destination: str) -> None:
    """``os.replace``, with POSIX semantics where the filesystem has them (NTFS, Windows 10 1607 and later).

    It succeeds while readers hold ``destination`` open through ``open_for_reading``, where ``os.replace``
    fails. A reader that did not share delete (Python's ``open``) still makes it fail. A filesystem without
    POSIX semantics (FAT, some network shares) gets ``os.replace``. For a job's own files, which carry no
    metadata of their own; a user's file is rewritten in place instead.
    """
    from ctypes import wintypes

    kernel32 = _kernel32()

    class RenameInformation(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    name = os.path.abspath(destination)
    size = RenameInformation.FileName.offset + (len(name) + 1) * ctypes.sizeof(wintypes.WCHAR)
    buffer = ctypes.create_string_buffer(size)
    information = RenameInformation.from_buffer(buffer)
    information.Flags = _FILE_RENAME_FLAG_REPLACE_IF_EXISTS | _FILE_RENAME_FLAG_POSIX_SEMANTICS
    information.FileNameLength = len(name) * ctypes.sizeof(wintypes.WCHAR)
    ctypes.memmove(
        ctypes.addressof(information) + RenameInformation.FileName.offset,
        ctypes.create_unicode_buffer(name),
        (len(name) + 1) * ctypes.sizeof(wintypes.WCHAR),
    )
    handle = kernel32.CreateFileW(
        source,
        _DELETE,
        _FILE_SHARE_READ_WRITE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _invalid(handle):
        raise _error(source)
    try:
        if kernel32.SetFileInformationByHandle(handle, _FILE_RENAME_INFO_EX, buffer, size):
            return
        if ctypes.get_last_error() not in (_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED):
            raise _error(destination)
    finally:
        kernel32.CloseHandle(handle)
    os.replace(source, destination)
