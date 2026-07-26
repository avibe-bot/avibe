"""Cross-platform transport contract for the Controller's internal HTTP API."""

from __future__ import annotations

import errno
import hmac
import json
import os
import re
import secrets
import socket
import stat
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from config import paths

CONTROL_IPC_SCHEMA_VERSION = 1
CONTROL_IPC_INSTANCE_HEADER = "X-Avibe-Control-Instance"
MAX_DESCRIPTOR_BYTES = 4096
_DESCRIPTOR_FIELDS = frozenset(
    {
        "schema_version",
        "transport",
        "host",
        "port",
        "instance_id",
        "bearer_token",
    }
)
_INSTANCE_ID_RE = re.compile(r"[0-9a-f]{32}")
_BEARER_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")


class ControlIpcDescriptorError(ValueError):
    """Raised when the Windows endpoint descriptor is absent or invalid."""


class ControlIpcSecurityError(PermissionError):
    """Raised when a Windows control IPC path is not private to its owner."""


@dataclass(frozen=True)
class ControlIpcDescriptor:
    schema_version: int
    transport: str
    host: str
    port: int
    instance_id: str
    bearer_token: str = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "transport": self.transport,
            "host": self.host,
            "port": self.port,
            "instance_id": self.instance_id,
            "bearer_token": self.bearer_token,
        }


@dataclass(frozen=True)
class BoundControlIpc:
    listener: socket.socket
    transport: str
    socket_path: Optional[Path] = None
    descriptor: Optional[ControlIpcDescriptor] = None


@dataclass(frozen=True)
class ControlIpcClientEndpoint:
    transport: str
    socket_path: Optional[Path] = None
    descriptor: Optional[ControlIpcDescriptor] = None

    @property
    def base_url(self) -> str:
        if self.descriptor is None:
            return "http://localhost"
        return f"http://{self.descriptor.host}:{self.descriptor.port}"

    @property
    def headers(self) -> dict[str, str]:
        if self.descriptor is None:
            return {}
        return {"Authorization": f"Bearer {self.descriptor.bearer_token}"}


def default_unix_socket_path() -> Path:
    override = os.environ.get("VIBE_INTERNAL_DISPATCH_SOCKET")
    if override:
        return Path(override).expanduser()
    return paths.get_state_dir() / "dispatch.sock"


def default_descriptor_path() -> Path:
    return paths.get_runtime_control_ipc_endpoint_path()


class ControlIpcHost(ABC):
    """Own one pre-bound listener and its discoverable endpoint lifecycle."""

    instance_id: Optional[str] = None
    bearer_token: Optional[str] = None

    @abstractmethod
    def bind(self) -> BoundControlIpc:
        raise NotImplementedError

    @abstractmethod
    def publish(self, bound: BoundControlIpc) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, bound: BoundControlIpc) -> None:
        raise NotImplementedError


class PosixUnixSocketHost(ControlIpcHost):
    def __init__(self, socket_path: Optional[Path] = None):
        self.socket_path = (socket_path or default_unix_socket_path()).expanduser().resolve()

    def bind(self) -> BoundControlIpc:
        target = self.socket_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()

        previous_umask = os.umask(0o077)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(target))
            listener.listen(2048)
            listener.setblocking(False)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return BoundControlIpc(
                listener=listener,
                transport="unix",
                socket_path=target,
            )
        except Exception:
            listener.close()
            raise
        finally:
            os.umask(previous_umask)

    def publish(self, bound: BoundControlIpc) -> None:
        if bound.transport != "unix" or bound.socket_path != self.socket_path:
            raise ValueError("bound endpoint does not belong to this Unix socket host")

    def cleanup(self, bound: BoundControlIpc) -> None:
        try:
            bound.listener.close()
        except OSError:
            pass
        target = bound.socket_path
        if target is None:
            return
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass


class WindowsLoopbackHost(ControlIpcHost):
    def __init__(
        self,
        descriptor_path: Optional[Path] = None,
        *,
        socket_factory: Callable[[int, int], socket.socket] = socket.socket,
        max_bind_attempts: int = 3,
        instance_id: Optional[str] = None,
        bearer_token: Optional[str] = None,
    ):
        self.descriptor_path = _absolute_path(descriptor_path or default_descriptor_path())
        self.socket_factory = socket_factory
        self.max_bind_attempts = max(1, max_bind_attempts)
        self.instance_id = instance_id or secrets.token_hex(16)
        self.bearer_token = bearer_token or secrets.token_urlsafe(32)
        _validate_instance_id(self.instance_id)
        _validate_bearer_token(self.bearer_token)

    def bind(self) -> BoundControlIpc:
        for attempt in range(1, self.max_bind_attempts + 1):
            listener = self.socket_factory(socket.AF_INET, socket.SOCK_STREAM)
            try:
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(2048)
                listener.setblocking(False)
                host, port = listener.getsockname()[:2]
                descriptor = ControlIpcDescriptor(
                    schema_version=CONTROL_IPC_SCHEMA_VERSION,
                    transport="tcp",
                    host=str(host),
                    port=int(port),
                    instance_id=self.instance_id,
                    bearer_token=self.bearer_token,
                )
                validate_descriptor(descriptor.to_dict())
                return BoundControlIpc(
                    listener=listener,
                    transport="tcp",
                    descriptor=descriptor,
                )
            except OSError as exc:
                listener.close()
                if exc.errno == errno.EADDRINUSE and attempt < self.max_bind_attempts:
                    continue
                raise
            except Exception:
                listener.close()
                raise
        raise AssertionError("unreachable")

    def publish(self, bound: BoundControlIpc) -> None:
        descriptor = bound.descriptor
        if (
            bound.transport != "tcp"
            or descriptor is None
            or descriptor.instance_id != self.instance_id
            or not hmac.compare_digest(descriptor.bearer_token, self.bearer_token)
        ):
            raise ValueError("bound endpoint does not belong to this Windows host")
        write_descriptor_atomic(self.descriptor_path, descriptor)

    def cleanup(self, bound: BoundControlIpc) -> None:
        try:
            bound.listener.close()
        except OSError:
            pass
        remove_descriptor_if_owned(
            self.descriptor_path,
            instance_id=self.instance_id,
            bearer_token=self.bearer_token,
        )


def select_control_ipc_host(
    *,
    platform_name: Optional[str] = None,
    socket_path: Optional[Path] = None,
    descriptor_path: Optional[Path] = None,
) -> ControlIpcHost:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        if socket_path is not None:
            raise ValueError("Windows control IPC does not accept a Unix socket path")
        return WindowsLoopbackHost(descriptor_path)
    return PosixUnixSocketHost(socket_path)


def resolve_client_endpoint(
    *,
    platform_name: Optional[str] = None,
    socket_path: Optional[Path] = None,
    descriptor_path: Optional[Path] = None,
) -> ControlIpcClientEndpoint:
    platform_name = platform_name or os.name
    if socket_path is not None or platform_name != "nt":
        target = (socket_path or default_unix_socket_path()).expanduser().resolve()
        return ControlIpcClientEndpoint(transport="unix", socket_path=target)
    descriptor = load_descriptor(descriptor_path or default_descriptor_path())
    return ControlIpcClientEndpoint(transport="tcp", descriptor=descriptor)


def validate_descriptor(payload: object) -> ControlIpcDescriptor:
    if not isinstance(payload, dict):
        raise ControlIpcDescriptorError("control IPC descriptor must be a JSON object")
    if frozenset(payload) != _DESCRIPTOR_FIELDS:
        raise ControlIpcDescriptorError("control IPC descriptor fields are invalid")

    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != CONTROL_IPC_SCHEMA_VERSION:
        raise ControlIpcDescriptorError("control IPC descriptor schema is unsupported")
    if payload.get("transport") != "tcp":
        raise ControlIpcDescriptorError("control IPC descriptor transport is unsupported")
    if payload.get("host") != "127.0.0.1":
        raise ControlIpcDescriptorError("control IPC descriptor host is not loopback")

    port = payload.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ControlIpcDescriptorError("control IPC descriptor port is invalid")

    instance_id = payload.get("instance_id")
    bearer_token = payload.get("bearer_token")
    _validate_instance_id(instance_id)
    _validate_bearer_token(bearer_token)
    return ControlIpcDescriptor(
        schema_version=schema_version,
        transport="tcp",
        host="127.0.0.1",
        port=port,
        instance_id=instance_id,
        bearer_token=bearer_token,
    )


def load_descriptor(descriptor_path: Path) -> ControlIpcDescriptor:
    target = _absolute_path(descriptor_path)
    try:
        with _descriptor_lock(target, prepare_directory=False):
            return _load_descriptor_unlocked(target)
    except ControlIpcDescriptorError:
        raise
    except OSError as exc:
        raise ControlIpcDescriptorError(f"control IPC descriptor is unavailable at {target}") from exc


def _load_descriptor_unlocked(target: Path) -> ControlIpcDescriptor:
    if target.is_symlink():
        raise ControlIpcDescriptorError(f"control IPC descriptor is a symlink at {target}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise ControlIpcDescriptorError(f"control IPC descriptor is unavailable at {target}") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlIpcDescriptorError(f"control IPC descriptor is not a regular file at {target}")
        if os.name == "nt":
            _windows_security().validate_file_descriptor(fd, target)
        if metadata.st_size <= 0 or metadata.st_size > MAX_DESCRIPTOR_BYTES:
            raise ControlIpcDescriptorError(f"control IPC descriptor size is invalid at {target}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ControlIpcDescriptorError(f"control IPC descriptor is unreadable at {target}") from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw.encode("utf-8")) > MAX_DESCRIPTOR_BYTES:
        raise ControlIpcDescriptorError(f"control IPC descriptor size is invalid at {target}")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ControlIpcDescriptorError(f"control IPC descriptor JSON is invalid at {target}") from exc
    return validate_descriptor(payload)


def write_descriptor_atomic(
    descriptor_path: Path,
    descriptor: ControlIpcDescriptor,
) -> None:
    descriptor = validate_descriptor(descriptor.to_dict())
    target = _absolute_path(descriptor_path)
    _ensure_private_directory(target.parent, allow_repair=True)
    with _descriptor_lock(target, prepare_directory=True):
        if os.name == "nt":
            fd, temporary = _windows_security().create_private_temporary_file(target)
        else:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                fd = -1
                json.dump(
                    descriptor.to_dict(),
                    stream,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt" and target.exists():
                if target.is_symlink():
                    raise ControlIpcSecurityError(f"control IPC descriptor security is invalid at {target}")
                _windows_security().secure_existing_owned_path(target)
            os.replace(temporary, target)
            if os.name == "nt":
                _windows_security().validate_path(target)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def remove_descriptor_if_owned(
    descriptor_path: Path,
    *,
    instance_id: str,
    bearer_token: str,
) -> bool:
    target = _absolute_path(descriptor_path)
    try:
        with _descriptor_lock(target, prepare_directory=False):
            try:
                descriptor = _load_descriptor_unlocked(target)
            except ControlIpcDescriptorError:
                return False
            if descriptor.instance_id != instance_id or not hmac.compare_digest(
                descriptor.bearer_token,
                bearer_token,
            ):
                return False
            try:
                target.unlink()
            except FileNotFoundError:
                return False
            return True
    except OSError:
        return False


def response_instance_matches(
    descriptor: ControlIpcDescriptor,
    response_instance_id: Optional[str],
) -> bool:
    return bool(response_instance_id) and hmac.compare_digest(
        descriptor.instance_id,
        response_instance_id,
    )


def _validate_instance_id(value: object) -> None:
    if not isinstance(value, str) or _INSTANCE_ID_RE.fullmatch(value) is None:
        raise ControlIpcDescriptorError("control IPC descriptor instance is invalid")


def _validate_bearer_token(value: object) -> None:
    if not isinstance(value, str) or _BEARER_TOKEN_RE.fullmatch(value) is None:
        raise ControlIpcDescriptorError("control IPC descriptor credential is invalid")


def _ensure_private_directory(directory: Path, *, allow_repair: bool = False) -> None:
    if os.name == "nt":
        _windows_security().ensure_private_directory(directory, allow_repair=allow_repair)
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


@contextmanager
def _descriptor_lock(
    descriptor_path: Path,
    *,
    prepare_directory: bool,
) -> Iterator[None]:
    lock_path = descriptor_path.with_name(f"{descriptor_path.name}.lock")
    _ensure_private_directory(lock_path.parent, allow_repair=prepare_directory)
    if os.name == "nt":
        fd = _windows_security().open_private_lock_file(
            lock_path,
            allow_repair=prepare_directory,
        )
    else:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name != "nt":
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class _WindowsSecurity:
    """Minimal Win32 owner/DACL operations for control IPC artifacts."""

    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _DACL_CONTROL_MASK = 0x0004 | 0x0008 | 0x0100 | 0x0400 | 0x1000
    _SE_DACL_PROTECTED = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
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

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        self.SecurityAttributes = SecurityAttributes
        self.TokenUser = TokenUser
        self.AclSizeInformation = AclSizeInformation
        self._configure_functions()
        self.current_user_sid = self._read_current_user_sid()
        self.sddl = f"O:{self.current_user_sid}D:P(A;;FA;;;{self.current_user_sid})(A;;FA;;;SY)"

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
                self._TOKEN_USER,
                None,
                0,
                ctypes.byref(required),
            )
            if ctypes.get_last_error() != self._ERROR_INSUFFICIENT_BUFFER:
                self._raise_last_error("cannot size the current process token")
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                self._raise_last_error("cannot read the current process token")
            token_user = ctypes.cast(buffer, ctypes.POINTER(self.TokenUser)).contents
            sid_text = wintypes.LPWSTR()
            if not self.advapi32.ConvertSidToStringSidW(
                token_user.User.Sid,
                ctypes.byref(sid_text),
            ):
                self._raise_last_error("cannot encode the current user SID")
            try:
                value = sid_text.value
                if not value:
                    raise ControlIpcSecurityError("the current process token has no user SID")
                return value
            finally:
                self.kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        finally:
            self.kernel32.CloseHandle(token)

    @contextmanager
    def _security_descriptor(self) -> Iterator[object]:
        ctypes = self.ctypes
        wintypes = self.wintypes
        descriptor = wintypes.LPVOID()
        size = wintypes.DWORD()
        if not self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self.sddl,
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
    def _security_attributes(self) -> Iterator[object]:
        ctypes = self.ctypes
        with self._security_descriptor() as descriptor:
            attributes = self.SecurityAttributes(
                nLength=ctypes.sizeof(self.SecurityAttributes),
                lpSecurityDescriptor=descriptor,
                bInheritHandle=False,
            )
            yield attributes

    def ensure_private_directory(self, directory: Path, *, allow_repair: bool) -> None:
        directory = _absolute_path(directory)
        if directory.exists():
            if not directory.is_dir() or directory.is_symlink():
                raise ControlIpcSecurityError(f"control IPC runtime directory security is invalid at {directory}")
            if allow_repair:
                self.secure_existing_owned_path(directory)
            else:
                self.validate_path(directory)
            return

        directory.parent.mkdir(parents=True, exist_ok=True)
        with self._security_attributes() as attributes:
            if not self.kernel32.CreateDirectoryW(str(directory), self.ctypes.byref(attributes)):
                error = self.ctypes.get_last_error()
                if error != self._ERROR_ALREADY_EXISTS:
                    raise ControlIpcSecurityError(
                        f"cannot create the control IPC runtime directory at {directory}"
                    ) from self.ctypes.WinError(error)
        self.validate_path(directory)

    def open_private_lock_file(self, path: Path, *, allow_repair: bool) -> int:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ControlIpcSecurityError(f"control IPC lock security is invalid at {path}")
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
            raise ControlIpcSecurityError(
                f"cannot create the private control IPC file at {path}"
            ) from self.ctypes.WinError(error)
        try:
            return msvcrt.open_osfhandle(
                handle,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except Exception:
            self.kernel32.CloseHandle(handle)
            raise

    def secure_existing_owned_path(self, path: Path) -> None:
        if not self._named_path_owner_matches(path):
            raise ControlIpcSecurityError(f"control IPC path is not owned by the current user at {path}")
        with self._security_descriptor() as expected:
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
                raise ControlIpcSecurityError(
                    f"cannot secure the control IPC path at {path}"
                ) from self.ctypes.WinError(result)
        self.validate_path(path)

    def validate_path(self, path: Path) -> None:
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
            raise ControlIpcSecurityError(f"cannot inspect the control IPC path at {path}") from ctypes.WinError(result)
        try:
            self._validate_security_descriptor(owner, dacl, descriptor, path)
        finally:
            self.kernel32.LocalFree(descriptor)

    def validate_file_descriptor(self, fd: int, path: Path) -> None:
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
            raise ControlIpcSecurityError(f"cannot inspect the control IPC file at {path}") from ctypes.WinError(result)
        try:
            self._validate_security_descriptor(owner, dacl, descriptor, path)
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
            raise ControlIpcSecurityError(f"cannot inspect the control IPC path owner at {path}") from ctypes.WinError(
                result
            )
        try:
            with self._security_descriptor() as expected:
                expected_owner = self._descriptor_owner(expected)
                return bool(owner) and bool(self.advapi32.EqualSid(owner, expected_owner))
        finally:
            self.kernel32.LocalFree(descriptor)

    def _validate_security_descriptor(
        self,
        owner: object,
        dacl: object,
        descriptor: object,
        path: Path,
    ) -> None:
        control = self._descriptor_control(descriptor)
        with self._security_descriptor() as expected:
            expected_owner = self._descriptor_owner(expected)
            expected_dacl = self._descriptor_dacl(expected)
            expected_control = self._descriptor_control(expected)
            valid = (
                bool(owner)
                and bool(dacl)
                and bool(self.advapi32.EqualSid(owner, expected_owner))
                and bool(control & self._SE_DACL_PROTECTED)
                and (control & self._DACL_CONTROL_MASK) == (expected_control & self._DACL_CONTROL_MASK)
                and self._acl_signature(dacl) == self._acl_signature(expected_dacl)
            )
        if not valid:
            raise ControlIpcSecurityError(f"control IPC path owner or DACL is invalid at {path}")

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
            raise ControlIpcSecurityError("private control IPC descriptor has no owner")
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
            raise ControlIpcSecurityError("private control IPC descriptor has no DACL")
        return dacl

    def _descriptor_control(self, descriptor: object) -> int:
        control = self.wintypes.WORD()
        revision = self.wintypes.DWORD()
        if not self.advapi32.GetSecurityDescriptorControl(
            descriptor,
            self.ctypes.byref(control),
            self.ctypes.byref(revision),
        ):
            self._raise_last_error("cannot read control IPC DACL controls")
        return control.value

    def _acl_signature(self, acl: object) -> tuple[int, tuple[bytes, ...]]:
        info = self.AclSizeInformation()
        if not self.advapi32.GetAclInformation(
            acl,
            self.ctypes.byref(info),
            self.ctypes.sizeof(info),
            self._ACL_SIZE_INFORMATION_CLASS,
        ):
            self._raise_last_error("cannot inspect a control IPC DACL")
        revision = self.ctypes.string_at(acl, 1)[0]
        entries: list[bytes] = []
        for index in range(info.AceCount):
            ace = self.wintypes.LPVOID()
            if not self.advapi32.GetAce(acl, index, self.ctypes.byref(ace)):
                self._raise_last_error("cannot inspect a control IPC DACL entry")
            header = self.ctypes.string_at(ace, 4)
            size = int.from_bytes(header[2:4], "little")
            if size < 4 or size > info.AclBytesInUse:
                raise ControlIpcSecurityError("control IPC DACL entry size is invalid")
            entries.append(self.ctypes.string_at(ace, size))
        return revision, tuple(entries)

    def _raise_last_error(self, message: str) -> None:
        error = self.ctypes.get_last_error()
        raise ControlIpcSecurityError(message) from self.ctypes.WinError(error)


_WINDOWS_SECURITY: Optional[_WindowsSecurity] = None


def _windows_security() -> _WindowsSecurity:
    global _WINDOWS_SECURITY
    if _WINDOWS_SECURITY is None:
        if os.name != "nt":
            raise RuntimeError("Windows control IPC security is unavailable")
        _WINDOWS_SECURITY = _WindowsSecurity()
    return _WINDOWS_SECURITY
