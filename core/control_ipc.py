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
        with _descriptor_lock(target):
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
    _ensure_private_directory(target.parent)
    with _descriptor_lock(target):
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
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
            os.replace(temporary, target)
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
        with _descriptor_lock(target):
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


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


@contextmanager
def _descriptor_lock(descriptor_path: Path) -> Iterator[None]:
    lock_path = descriptor_path.with_name(f"{descriptor_path.name}.lock")
    _ensure_private_directory(lock_path.parent)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
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
