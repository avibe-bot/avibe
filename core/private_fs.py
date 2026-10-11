"""Owner-private filesystem paths.

A path is owner-private when only the current user can use it. On POSIX that is
mode 0o700 for a directory and 0o600 for a regular file. Windows keeps no such
modes -- ``chmod`` there only toggles the read-only attribute, and ``stat``
reports 0o777 or 0o666 whatever the access rules are -- so there it means the
path is owned by this process and its DACL grants this user and SYSTEM alone.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivatePathError(PermissionError):
    """Raised when a path that must be owner-private is not, or cannot be made so."""


def ensure_private_directory(path: Path, *, repair: bool = True) -> None:
    """Create ``path`` and any missing parents, and leave ``path`` owner-private.

    Parents keep their defaults. An existing ``path`` is repaired when
    ``repair`` is set and must already be private otherwise. On Windows its
    protected DACL is inheritable, so whatever any process later creates inside
    starts private too.
    """

    if os.name == "nt":
        windows_security().ensure_private_directory(path, allow_repair=repair, inheritable=True)
        return
    path.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise PrivatePathError(f"private directory security is invalid at {path}")
    if repair:
        os.chmod(path, PRIVATE_DIRECTORY_MODE)
    elif not is_private(path, mode):
        raise PrivatePathError(f"private directory permissions are unsafe at {path}")


def is_private(path: Path, mode: int, *, fd: Optional[int] = None) -> bool:
    """Whether the directory or regular file ``path`` is owner-private.

    ``mode`` is the caller's ``lstat`` of ``path``, or its ``fstat`` of ``fd``;
    with ``fd`` Windows checks the open file rather than what ``path`` names now.
    """

    directory = stat.S_ISDIR(mode)
    if os.name == "nt":
        return windows_security().grants_only_self(path, directory=directory, fd=fd)
    return stat.S_IMODE(mode) == (PRIVATE_DIRECTORY_MODE if directory else PRIVATE_FILE_MODE)


def make_private_file(path: Path) -> None:
    """Restrict an existing regular file this user owns to owner-only access."""

    if os.name == "nt":
        windows_security().secure_existing_owned_path(path)
        return
    os.chmod(path, PRIVATE_FILE_MODE)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


class WindowsSecurity:
    """Win32 owner and DACL operations behind owner-private paths."""

    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PRESENT = 0x0004
    _SE_DACL_PROTECTED = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _TOKEN_OWNER = 4
    _ERROR_INSUFFICIENT_BUFFER = 122
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _ACL_SIZE_INFORMATION_CLASS = 2
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ACCESS_DENIED_ACE_TYPE = 1
    _OBJECT_INHERIT_ACE = 0x01
    _CONTAINER_INHERIT_ACE = 0x02

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("Sid", wintypes.LPVOID),
                ("Attributes", wintypes.DWORD),
            ]

        class TokenUser(ctypes.Structure):
            _fields_ = [("User", SidAndAttributes)]

        class TokenOwner(ctypes.Structure):
            _fields_ = [("Owner", wintypes.LPVOID)]

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        self.SecurityAttributes = SecurityAttributes
        self.TokenUser = TokenUser
        self.TokenOwner = TokenOwner
        self.AclSizeInformation = AclSizeInformation
        self._configure_functions()
        self.current_user_sid = self._read_current_user_sid()
        # Windows stamps newly created objects with the token's *default owner*,
        # which is BUILTIN\Administrators for an elevated token rather than the
        # token user. Both SIDs therefore name "an object this process made".
        self.current_owner_sid = self._read_current_owner_sid()
        # ``sddl`` carries no inheritance flags on purpose. Control IPC creates
        # every object it secures -- the endpoint directory, the descriptor and
        # the lock -- with this descriptor applied explicitly, so nothing
        # depends on inheriting it. Adding OI/CI would not fix anything and
        # would push this policy onto files control IPC does not own; the
        # directory boundary in paths.get_runtime_control_ipc_endpoint_path is
        # what keeps that set closed.
        self.sddl = f"O:{self.current_user_sid}D:P(A;;FA;;;{self.current_user_sid})(A;;FA;;;SY)"
        # A directory that owns its whole subtree carries the same entries as
        # inheritable ones instead, so whatever any process later creates
        # inside it -- including files this process never writes itself --
        # starts private. Windows ignores the umask, so nothing else would.
        self.inheritable_sddl = (
            f"O:{self.current_user_sid}D:P(A;OICI;FA;;;{self.current_user_sid})(A;OICI;FA;;;SY)"
        )

    def _configure_functions(self) -> None:
        ctypes = self.ctypes
        wintypes = self.wintypes

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.advapi32.GetSecurityDescriptorOwner.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        self.advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        self.advapi32.GetSecurityDescriptorDacl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        ]
        self.advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.advapi32.GetSecurityDescriptorControl.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self.advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self.advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        self.advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
        self.advapi32.EqualSid.restype = wintypes.BOOL
        self.advapi32.GetAclInformation.argtypes = [
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        self.advapi32.GetAclInformation.restype = wintypes.BOOL
        self.advapi32.GetAce.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
        ]
        self.advapi32.GetAce.restype = wintypes.BOOL

        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL
        self.kernel32.CreateDirectoryW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(self.SecurityAttributes),
        ]
        self.kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(self.SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE

    def _read_current_user_sid(self) -> str:
        return self._read_token_sid(self._TOKEN_USER, self.TokenUser, lambda info: info.User.Sid, "user")

    def _read_current_owner_sid(self) -> str:
        return self._read_token_sid(self._TOKEN_OWNER, self.TokenOwner, lambda info: info.Owner, "owner")

    def _read_token_sid(self, information_class: int, structure: type, select, label: str) -> str:
        ctypes = self.ctypes
        wintypes = self.wintypes
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            self._TOKEN_QUERY,
            ctypes.byref(token),
        ):
            self._raise_last_error("cannot open the current process token")
        try:
            required = wintypes.DWORD()
            self.advapi32.GetTokenInformation(
                token,
                information_class,
                None,
                0,
                ctypes.byref(required),
            )
            if ctypes.get_last_error() != self._ERROR_INSUFFICIENT_BUFFER:
                self._raise_last_error("cannot size the current process token")
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi32.GetTokenInformation(
                token,
                information_class,
                buffer,
                required,
                ctypes.byref(required),
            ):
                self._raise_last_error("cannot read the current process token")
            information = ctypes.cast(buffer, ctypes.POINTER(structure)).contents
            sid_text = wintypes.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(
                select(information),
                ctypes.byref(sid_text),
            ):
                self._raise_last_error(f"cannot encode the current {label} SID")
            try:
                value = sid_text.value
                if not value:
                    raise PrivatePathError(f"the current process token has no {label} SID")
                return value
            finally:
                self.kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        finally:
            self.kernel32.CloseHandle(token)

    @contextmanager
    def _security_descriptor(self, sddl: Optional[str] = None) -> Iterator[object]:
        with self._descriptor_from_sddl(sddl or self.sddl) as descriptor:
            yield descriptor

    def _self_owner_sddls(self) -> tuple[str, ...]:
        """Every SDDL owner this process can legitimately have created a path as.

        An unelevated token owns what it creates as the token user. An elevated
        token owns it as the token's default owner, normally
        ``BUILTIN\\Administrators``. Both are "us"; anything else is not.
        """

        if self.current_owner_sid == self.current_user_sid:
            return (self.sddl,)
        return (self.sddl, f"O:{self.current_owner_sid}")

    def _owner_is_self(self, owner: object) -> bool:
        if not owner:
            return False
        for sddl in self._self_owner_sddls():
            with self._descriptor_from_sddl(sddl) as expected:
                if bool(self.advapi32.EqualSid(owner, self._descriptor_owner(expected))):
                    return True
        return False

    @contextmanager
    def _descriptor_from_sddl(self, sddl: str) -> Iterator[object]:
        ctypes = self.ctypes
        wintypes = self.wintypes
        descriptor = wintypes.LPVOID()
        size = wintypes.DWORD()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            self._SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(size),
        ):
            self._raise_last_error("cannot construct the private security descriptor")
        try:
            yield descriptor
        finally:
            self.kernel32.LocalFree(descriptor)

    @contextmanager
    def _security_attributes(self, sddl: Optional[str] = None) -> Iterator[object]:
        ctypes = self.ctypes
        with self._security_descriptor(sddl) as descriptor:
            attributes = self.SecurityAttributes(
                nLength=ctypes.sizeof(self.SecurityAttributes),
                lpSecurityDescriptor=descriptor,
                bInheritHandle=False,
            )
            yield attributes

    def ensure_private_directory(self, directory: Path, *, allow_repair: bool, inheritable: bool = False) -> None:
        sddl = self.inheritable_sddl if inheritable else self.sddl
        directory = _absolute_path(directory)
        if directory.exists():
            if not directory.is_dir() or directory.is_symlink():
                raise PrivatePathError(f"private directory security is invalid at {directory}")
            # Setting an inheritable DACL rewrites the inherited entries of the
            # whole subtree, so a directory that is already private is left as is.
            if allow_repair and not (inheritable and self._path_matches(directory, sddl)):
                self.secure_existing_owned_path(directory, sddl=sddl)
            else:
                self.validate_path(directory, sddl=sddl)
            return

        directory.parent.mkdir(parents=True, exist_ok=True)
        with self._security_attributes(sddl) as attributes:
            if not self.kernel32.CreateDirectoryW(str(directory), self.ctypes.byref(attributes)):
                error = self.ctypes.get_last_error()
                if error != self._ERROR_ALREADY_EXISTS:
                    raise PrivatePathError(
                        f"cannot create the private directory at {directory}"
                    ) from self.ctypes.WinError(error)
        self.validate_path(directory, sddl=sddl)

    def open_private_lock_file(self, path: Path, *, allow_repair: bool) -> int:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise PrivatePathError(f"private lock file security is invalid at {path}")
            if allow_repair:
                self.secure_existing_owned_path(path)
        fd = self._create_file(
            path,
            creation_disposition=self._OPEN_ALWAYS,
            share_mode=(self._FILE_SHARE_READ | self._FILE_SHARE_WRITE | self._FILE_SHARE_DELETE),
        )
        try:
            self.validate_file_descriptor(fd, path)
        except Exception:
            os.close(fd)
            raise
        return fd

    def create_private_temporary_file(self, target: Path) -> tuple[int, Path]:
        for _ in range(128):
            temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
            try:
                fd = self._create_file(
                    temporary,
                    creation_disposition=self._CREATE_NEW,
                    share_mode=0,
                )
            except FileExistsError:
                continue
            try:
                self.validate_file_descriptor(fd, temporary)
            except Exception:
                os.close(fd)
                temporary.unlink(missing_ok=True)
                raise
            return fd, temporary
        raise FileExistsError(f"cannot allocate a private descriptor beside {target}")

    def _create_file(
        self,
        path: Path,
        *,
        creation_disposition: int,
        share_mode: int,
    ) -> int:
        import msvcrt

        with self._security_attributes() as attributes:
            handle = self.kernel32.CreateFileW(
                str(path),
                self._GENERIC_READ | self._GENERIC_WRITE,
                share_mode,
                self.ctypes.byref(attributes),
                creation_disposition,
                self._FILE_ATTRIBUTE_NORMAL,
                None,
            )
        invalid_handle = self.ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = self.ctypes.get_last_error()
            if error in {self._ERROR_FILE_EXISTS, self._ERROR_ALREADY_EXISTS}:
                raise FileExistsError(error, os.strerror(error), str(path))
            raise PrivatePathError(
                f"cannot create the private file at {path}"
            ) from self.ctypes.WinError(error)
        try:
            return msvcrt.open_osfhandle(
                handle,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except Exception:
            self.kernel32.CloseHandle(handle)
            raise

    def secure_existing_owned_path(self, path: Path, *, sddl: Optional[str] = None) -> None:
        if not self._named_path_owner_matches(path):
            raise PrivatePathError(f"private path is not owned by the current user at {path}")
        with self._security_descriptor(sddl) as expected:
            expected_dacl = self._descriptor_dacl(expected)
            result = self.advapi32.SetNamedSecurityInfoW(
                str(path),
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION | self._PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                expected_dacl,
                None,
            )
            if result:
                raise PrivatePathError(
                    f"cannot secure the private path at {path}"
                ) from self.ctypes.WinError(result)
        self.validate_path(path, sddl=sddl)

    def validate_path(self, path: Path, *, sddl: Optional[str] = None) -> None:
        with self._named_path_security(path) as (owner, dacl, descriptor):
            self._validate_security_descriptor(owner, dacl, descriptor, path, sddl=sddl)

    def validate_file_descriptor(self, fd: int, path: Path) -> None:
        with self._file_descriptor_security(fd, path) as (owner, dacl, descriptor):
            self._validate_security_descriptor(owner, dacl, descriptor, path)

    def grants_only_self(self, path: Path, *, directory: bool, fd: Optional[int] = None) -> bool:
        """Whether only this user and SYSTEM can open ``path``, inherited entries included.

        What a private directory holds inherits its entries rather than carrying
        a copy, so this checks what the DACL grants, not that it equals one
        descriptor. A directory's entries must also be inheritable, or what is
        created inside it later would not start private. With ``fd`` the open
        file is checked, not whatever ``path`` names now.
        """

        reader = (
            self._named_path_security(path) if fd is None else self._file_descriptor_security(fd, path)
        )
        with reader as (owner, dacl, _descriptor):
            if not self._owner_is_self(owner) or not dacl:
                return False
            with self._descriptor_from_sddl(f"O:{self.current_user_sid}") as user, self._descriptor_from_sddl(
                "O:SY"
            ) as system:
                trusted = (self._descriptor_owner(user), self._descriptor_owner(system))
                for ace_type, flags, sid in self._aces(dacl):
                    if ace_type == self._ACCESS_DENIED_ACE_TYPE:
                        continue
                    if ace_type != self._ACCESS_ALLOWED_ACE_TYPE:
                        return False
                    if not any(self.advapi32.EqualSid(sid, candidate) for candidate in trusted):
                        return False
                    inherit = self._OBJECT_INHERIT_ACE | self._CONTAINER_INHERIT_ACE
                    if directory and flags & inherit != inherit:
                        return False
        return True

    def _path_matches(self, path: Path, sddl: str) -> bool:
        try:
            self.validate_path(path, sddl=sddl)
        except PrivatePathError:
            return False
        return True

    @contextmanager
    def _named_path_security(self, path: Path) -> Iterator[tuple[object, object, object]]:
        ctypes = self.ctypes
        owner = self.wintypes.LPVOID()
        dacl = self.wintypes.LPVOID()
        descriptor = self.wintypes.LPVOID()
        result = self.advapi32.GetNamedSecurityInfoW(
            str(path),
            self._SE_FILE_OBJECT,
            self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise PrivatePathError(f"cannot inspect the private path at {path}") from ctypes.WinError(result)
        try:
            yield owner, dacl, descriptor
        finally:
            self.kernel32.LocalFree(descriptor)

    @contextmanager
    def _file_descriptor_security(self, fd: int, path: Path) -> Iterator[tuple[object, object, object]]:
        import msvcrt

        ctypes = self.ctypes
        owner = self.wintypes.LPVOID()
        dacl = self.wintypes.LPVOID()
        descriptor = self.wintypes.LPVOID()
        result = self.advapi32.GetSecurityInfo(
            msvcrt.get_osfhandle(fd),
            self._SE_FILE_OBJECT,
            self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise PrivatePathError(f"cannot inspect the private file at {path}") from ctypes.WinError(result)
        try:
            yield owner, dacl, descriptor
        finally:
            self.kernel32.LocalFree(descriptor)

    def _named_path_owner_matches(self, path: Path) -> bool:
        ctypes = self.ctypes
        owner = self.wintypes.LPVOID()
        descriptor = self.wintypes.LPVOID()
        result = self.advapi32.GetNamedSecurityInfoW(
            str(path),
            self._SE_FILE_OBJECT,
            self._OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise PrivatePathError(f"cannot inspect the private path owner at {path}") from ctypes.WinError(result)
        try:
            return self._owner_is_self(owner)
        finally:
            self.kernel32.LocalFree(descriptor)

    def _validate_security_descriptor(
        self,
        owner: object,
        dacl: object,
        descriptor: object,
        path: Path,
        *,
        sddl: Optional[str] = None,
    ) -> None:
        control = self._descriptor_control(descriptor)
        owner_is_self = self._owner_is_self(owner)
        with self._security_descriptor(sddl) as expected:
            expected_dacl = self._descriptor_dacl(expected)
            valid = (
                owner_is_self
                and bool(dacl)
                and bool(control & self._SE_DACL_PRESENT)
                and bool(control & self._SE_DACL_PROTECTED)
                and self._acl_signature(dacl) == self._acl_signature(expected_dacl)
            )
        if not valid:
            raise PrivatePathError(f"private path owner or DACL is invalid at {path}")

    def _descriptor_owner(self, descriptor: object) -> object:
        owner = self.wintypes.LPVOID()
        defaulted = self.wintypes.BOOL()
        if not self.advapi32.GetSecurityDescriptorOwner(
            descriptor,
            self.ctypes.byref(owner),
            self.ctypes.byref(defaulted),
        ):
            self._raise_last_error("cannot read the private descriptor owner")
        if not owner:
            raise PrivatePathError("private security descriptor has no owner")
        return owner

    def _descriptor_dacl(self, descriptor: object) -> object:
        present = self.wintypes.BOOL()
        dacl = self.wintypes.LPVOID()
        defaulted = self.wintypes.BOOL()
        if not self.advapi32.GetSecurityDescriptorDacl(
            descriptor,
            self.ctypes.byref(present),
            self.ctypes.byref(dacl),
            self.ctypes.byref(defaulted),
        ):
            self._raise_last_error("cannot read the private descriptor DACL")
        if not present or not dacl:
            raise PrivatePathError("private security descriptor has no DACL")
        return dacl

    def _descriptor_control(self, descriptor: object) -> int:
        control = self.wintypes.WORD()
        revision = self.wintypes.DWORD()
        if not self.advapi32.GetSecurityDescriptorControl(
            descriptor,
            self.ctypes.byref(control),
            self.ctypes.byref(revision),
        ):
            self._raise_last_error("cannot read private DACL controls")
        return control.value

    def _acl_signature(self, acl: object) -> tuple[bytes, ...]:
        return tuple(sorted(self.ctypes.string_at(ace, size) for ace, size in self._ace_entries(acl)))

    def _aces(self, acl: object) -> Iterator[tuple[int, int, int]]:
        """Yield each entry's type, flags and SID address.

        Only the allowed and denied entry types are decoded; their SID follows
        the 4-byte header and the 4-byte access mask.
        """

        for ace, size in self._ace_entries(acl):
            header = self.ctypes.string_at(ace, 4)
            ace_type, flags = header[0], header[1]
            if ace_type in {self._ACCESS_ALLOWED_ACE_TYPE, self._ACCESS_DENIED_ACE_TYPE} and size < 16:
                raise PrivatePathError("private DACL entry size is invalid")
            yield ace_type, flags, ace.value + 8

    def _ace_entries(self, acl: object) -> Iterator[tuple[object, int]]:
        info = self.AclSizeInformation()
        if not self.advapi32.GetAclInformation(
            acl,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
            self._ACL_SIZE_INFORMATION_CLASS,
        ):
            self._raise_last_error("cannot inspect a private DACL")
        ace_count = int(info.AceCount)
        acl_bytes_in_use = int(info.AclBytesInUse)
        for index in range(ace_count):
            ace = self.wintypes.LPVOID()
            if not self.advapi32.GetAce(acl, index, self.ctypes.byref(ace)):
                self._raise_last_error("cannot inspect a private DACL entry")
            header = self.ctypes.string_at(ace, 4)
            size = int.from_bytes(header[2:4], "little")
            if size < 4 or size > acl_bytes_in_use:
                raise PrivatePathError("private DACL entry size is invalid")
            yield ace, size

    def _raise_last_error(self, message: str) -> None:
        error = self.ctypes.get_last_error()
        raise PrivatePathError(message) from self.ctypes.WinError(error)


_WINDOWS_SECURITY: Optional[WindowsSecurity] = None


def windows_security() -> WindowsSecurity:
    global _WINDOWS_SECURITY
    if _WINDOWS_SECURITY is None:
        if os.name != "nt":
            raise RuntimeError("Windows private path security is unavailable")
        _WINDOWS_SECURITY = WindowsSecurity()
    return _WINDOWS_SECURITY
