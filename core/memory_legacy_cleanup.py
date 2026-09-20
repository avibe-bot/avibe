"""Retire process ownership records shipped before Memory became optional.

This module deliberately lives in the host package. Disabled Avibe must be able
to stop a released Memory child even when ``avibe-memory`` is not installed.
It only consumes the two ownership formats that Avibe has already shipped; it
never starts Memory work or writes new workflow state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import stat
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import psutil

from config import paths

_SIDECAR_RECORD = Path(".rt/everos.sidecar.json")
_SYNC_PREFIX = "cascade-sync-"
_LOCK_PREFIX = "cascade-rebuild-"
_LOCK_DIRECTORY = ".avibe-memory-locks"
_SIDECAR_MAX_BYTES = 4 * 1024
_SYNC_MAX_BYTES = 16 * 1024
_SYNC_ARGV = ("-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync")
_SIDECAR_ENTRYPOINTS = {"avibe_memory.sidecar", "core.memory.sidecar"}
_REBUILD_ENTRYPOINT = "core.memory.rebuild_child"


@dataclass(frozen=True, slots=True)
class _Identity:
    pid: int
    stamp: float | None
    wall_create_time: float | None
    uid: int | None
    command: tuple[str, ...] | None
    environment: Mapping[str, str] | None
    process_group: int | None


def _canonical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path))).resolve(strict=False)


def _same_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        observed = _canonical(value)
        configured = _canonical(expected)
        if observed == configured:
            return True
        left = os.stat(observed)
        right = os.stat(configured)
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    except OSError:
        return False


def _is_stamp(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _linux_starttime(pid: int) -> float:
    data = Path(f"/proc/{pid}/stat").read_bytes()
    close = data.rfind(b")")
    if close < 0:
        raise ValueError("process start time is unavailable")
    return float(data[close + 1 :].split()[19])


def _read_field(reader: Any) -> Any:
    try:
        return reader()
    except (AttributeError, OSError, psutil.AccessDenied, PermissionError):
        return None


def _inspect(pid: int) -> _Identity | None:
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        wall_create_time = _read_field(process.create_time)
        stamp = (
            _read_field(lambda: _linux_starttime(pid))
            if sys.platform.startswith("linux")
            else wall_create_time
        )
        command = _read_field(process.cmdline)
        environment = _read_field(process.environ)
        uids = _read_field(process.uids)
        group = _read_field(lambda: os.getpgid(pid)) if os.name == "posix" else None
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError):
        return None
    except psutil.Error:
        return _Identity(pid, None, None, None, None, None, None)
    return _Identity(
        pid=pid,
        stamp=float(stamp) if _is_stamp(stamp) else None,
        wall_create_time=(
            float(wall_create_time) if _is_stamp(wall_create_time) else None
        ),
        uid=None if uids is None else int(uids.real),
        command=None if command is None else tuple(str(value) for value in command),
        environment=environment,
        process_group=group if isinstance(group, int) and group > 1 else None,
    )


def _assert_safe_chain(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop.parent:
        try:
            info = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError("legacy Memory path is unavailable") from exc
        else:
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("legacy Memory path contains a symlink")
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("legacy Memory path has an unsafe parent")
        if current == current.parent:
            raise RuntimeError("legacy Memory path escaped its home")
        current = current.parent


def _read_record(path: Path, *, max_bytes: int) -> Mapping[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("legacy Memory ownership cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("legacy Memory ownership is unsafe")
    if info.st_size > max_bytes:
        raise RuntimeError("legacy Memory ownership is oversized")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        raise RuntimeError("legacy Memory ownership has another owner")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("legacy Memory ownership is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("legacy Memory ownership is invalid")
    return value


def _retire(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("legacy Memory ownership cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("legacy Memory ownership is unsafe")
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError("legacy Memory ownership cannot be retired") from exc


def _root_artifact(provider_root: Path, prefix: str, suffix: str) -> Path:
    root = _canonical(provider_root)
    parent = root.parent.resolve(strict=False)
    identity_root = root
    if sys.platform == "darwin" and not root.exists():
        identity_root = parent / root.name.casefold()
    digest = hashlib.sha256(f"path:{identity_root}".encode()).hexdigest()
    return parent / _LOCK_DIRECTORY / f"{prefix}{digest}{suffix}"


@contextmanager
def _provider_lock(provider_root: Path) -> Iterator[None]:
    path = _root_artifact(provider_root, _LOCK_PREFIX, ".lock")
    _assert_safe_chain(path.parent, stop=provider_root.parent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_safe_chain(path.parent, stop=provider_root.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


def _owned_environment(identity: _Identity, *, root: Path, role: str) -> bool:
    environment = identity.environment
    return (
        environment is not None
        and _same_path(environment.get("EVEROS_ROOT"), root)
        and environment.get("AVIBE_MEMORY_CHILD_ROLE") == role
    )


def _owned_command(
    identity: _Identity,
    *,
    role: str,
    python: Path,
    socket_path: Path,
) -> bool:
    command = identity.command
    if command is None or not command or command[0] != str(python):
        return False
    if role == "sidecar":
        return (
            len(command) == 5
            and command[1] == "-m"
            and command[2] in _SIDECAR_ENTRYPOINTS
            and command[3] == "--uds"
            and _same_path(command[4], socket_path)
        )
    if role == "cascade_rebuild":
        return command[1:] == (
            "-m",
            _REBUILD_ENTRYPOINT,
            "cascade",
            "rebuild",
            "--yes",
        )
    return role == "cascade_sync" and command[1:] == _SYNC_ARGV


def _validate_identity(
    identity: _Identity,
    record: Mapping[str, Any],
    *,
    provider_root: Path,
    socket_path: Path,
    role: str,
    require_record_stamp: bool,
    require_command: bool = True,
) -> None:
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    if own_uid is not None and identity.uid != own_uid:
        raise RuntimeError("legacy Memory process identity is unavailable")
    python_value = record.get("python")
    if role == "cascade_sync":
        argv = record.get("argv")
        python_value = argv[0] if isinstance(argv, list) and argv else None
    if not isinstance(python_value, str) or not Path(python_value).is_absolute():
        raise RuntimeError("legacy Memory process identity is unavailable")
    command_matches = _owned_command(
        identity,
        role=role,
        python=Path(python_value),
        socket_path=socket_path,
    )
    legacy_sidecar = role == "sidecar" and record.get("role") is None
    environment_matches = _owned_environment(
        identity,
        root=provider_root,
        role=role,
    )
    if (
        (require_command and not command_matches)
        or (not environment_matches and not (legacy_sidecar and command_matches))
    ):
        raise RuntimeError("legacy Memory process identity is unavailable")
    if require_record_stamp:
        expected = record.get("starttime_ticks", record.get("create_time"))
        observed = (
            identity.stamp
            if "starttime_ticks" in record
            else identity.wall_create_time
        )
        if not _is_stamp(expected) or observed != float(expected):
            raise RuntimeError("legacy Memory process identity is unavailable")


def _group_members(group: int) -> list[_Identity]:
    members: list[_Identity] = []
    for process in psutil.process_iter(["pid"]):
        pid = process.info.get("pid")
        if not isinstance(pid, int) or pid == os.getpid():
            continue
        try:
            if os.name != "posix" or os.getpgid(pid) != group:
                continue
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (OSError, psutil.Error) as exc:
            raise RuntimeError(
                "legacy Memory process group cannot be verified"
            ) from exc
        identity = _inspect(pid)
        if identity is not None:
            members.append(identity)
    return members


def _signal_and_wait(identities: list[_Identity], timeout: float) -> None:
    if not identities:
        return
    for signum, budget in (
        (signal.SIGTERM, timeout),
        (getattr(signal, "SIGKILL", signal.SIGTERM), min(timeout, 3.0)),
    ):
        live: list[psutil.Process] = []
        for identity in identities:
            if not _is_stamp(identity.stamp):
                raise RuntimeError("legacy Memory process identity is unavailable")
            current = _inspect(identity.pid)
            if current is None or current.stamp != identity.stamp:
                continue
            try:
                os.kill(identity.pid, signum)
                live.append(psutil.Process(identity.pid))
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
        if not live:
            return
        _gone, remaining = psutil.wait_procs(live, timeout=budget)
        if not remaining:
            return
    raise RuntimeError("legacy Memory process group did not exit")


class ReleasedEverOSOrphanReconciler:
    """Consume released EverOS ownership without importing ``avibe_memory``."""

    def __init__(
        self,
        *,
        provider_root: Path | str,
        effective_home: Path | str,
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        logical_home = Path(os.path.abspath(os.path.expanduser(os.fspath(effective_home))))
        physical_home = paths.physical_home(logical_home)
        root = Path(os.path.abspath(os.path.expanduser(os.fspath(provider_root))))
        for home in (logical_home, physical_home):
            if root.is_relative_to(home):
                root = physical_home.joinpath(*root.relative_to(home).parts)
                break
        else:
            raise RuntimeError("legacy Memory provider root escaped its home")
        self._home = physical_home
        self._memory_dir = physical_home / "memory"
        self._provider_root = root
        self._socket_path = self._memory_dir / ".rt" / "everos.sock"
        self._timeout = max(0.001, float(stop_timeout_seconds))

    async def reconcile_orphans(self) -> None:
        await asyncio.to_thread(self._reconcile_orphans)

    def _reconcile_orphans(self) -> None:
        _assert_safe_chain(self._memory_dir, stop=self._home)
        _assert_safe_chain(self._provider_root, stop=self._home)
        with _provider_lock(self._provider_root):
            self._reconcile_sync()
            self._reconcile_sidecar()

    def _reconcile_sync(self) -> None:
        path = _root_artifact(self._provider_root, _SYNC_PREFIX, ".json")
        record = _read_record(path, max_bytes=_SYNC_MAX_BYTES)
        if record is None:
            return
        self._validate_record_paths(record, role="cascade_sync")
        nonce = record.get("nonce")
        argv = record.get("argv")
        if (
            record.get("state") not in {"pending", "finalized"}
            or not isinstance(nonce, str)
            or len(nonce) != 64
            or any(char not in "0123456789abcdef" for char in nonce)
            or not isinstance(argv, list)
            or len(argv) != len(_SYNC_ARGV) + 1
            or tuple(argv[1:]) != _SYNC_ARGV
        ):
            raise RuntimeError("released sync ownership is invalid")
        parent_pid = record.get("parent_pid")
        if (
            not isinstance(parent_pid, int)
            or parent_pid <= 1
            or not _is_stamp(record.get("parent_create_time"))
        ):
            raise RuntimeError("released sync ownership is invalid")
        parent = _inspect(parent_pid)
        if parent is not None:
            parent_status = self._parent_status(parent, record)
            if parent_status == "owned":
                raise RuntimeError("released sync is still owned by a live parent")
            if parent_status == "unknown":
                raise RuntimeError("released sync parent identity is unavailable")

        identities: list[_Identity] = []
        if record["state"] == "finalized":
            pid = record.get("pid")
            group = record.get("process_group")
            if not isinstance(pid, int) or pid <= 1 or not isinstance(group, int) or group <= 1:
                raise RuntimeError("released sync ownership is invalid")
            if hasattr(os, "getpgrp") and group == os.getpgrp():
                raise RuntimeError("released sync process group is unsafe")
            leader = _inspect(pid)
            if leader is not None:
                _validate_identity(
                    leader,
                    record,
                    provider_root=self._provider_root,
                    socket_path=self._socket_path,
                    role="cascade_sync",
                    require_record_stamp=True,
                )
            identities = _group_members(group)
        else:
            for process in psutil.process_iter(["pid"]):
                pid = process.info.get("pid")
                if not isinstance(pid, int):
                    continue
                identity = _inspect(pid)
                if identity is None or identity.environment is None:
                    continue
                if identity.environment.get("AVIBE_MEMORY_SYNC_NONCE") == nonce:
                    identities.append(identity)

        for identity in identities:
            _validate_identity(
                identity,
                record,
                provider_root=self._provider_root,
                socket_path=self._socket_path,
                role="cascade_sync",
                require_record_stamp=(identity.pid == record.get("pid")),
                require_command=(identity.pid == record.get("pid")),
            )
            self._validate_sync_environment(identity, record)
        _signal_and_wait(identities, self._timeout)
        if record["state"] == "finalized" and _group_members(group):
            raise RuntimeError("released sync process group did not exit")
        _retire(path)

    def _reconcile_sidecar(self) -> None:
        path = self._memory_dir / _SIDECAR_RECORD
        record = _read_record(path, max_bytes=_SIDECAR_MAX_BYTES)
        if record is None:
            return
        role = record.get("role") or "sidecar"
        if role not in {"sidecar", "cascade_rebuild"}:
            raise RuntimeError("released sidecar ownership is invalid")
        self._validate_record_paths(record, role=role)
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            raise RuntimeError("released sidecar ownership is invalid")
        leader = _inspect(pid)
        if leader is not None:
            _validate_identity(
                leader,
                record,
                provider_root=self._provider_root,
                socket_path=self._socket_path,
                role=role,
                require_record_stamp=True,
            )
        group = record.get("process_group")
        identities = [leader] if leader is not None else []
        if isinstance(group, int) and group > 1:
            if hasattr(os, "getpgrp") and group == os.getpgrp():
                raise RuntimeError("released sidecar process group is unsafe")
            identities = _group_members(group)
            for identity in identities:
                _validate_identity(
                    identity,
                    record,
                    provider_root=self._provider_root,
                    socket_path=self._socket_path,
                    role=role,
                    require_record_stamp=(identity.pid == pid),
                    require_command=(identity.pid == pid),
                )
        _signal_and_wait(identities, self._timeout)
        if isinstance(group, int) and group > 1 and _group_members(group):
            raise RuntimeError("released sidecar process group did not exit")
        _retire(path)

    def _validate_record_paths(self, record: Mapping[str, Any], *, role: str) -> None:
        record_role = record.get("role") or "sidecar"
        if (
            record_role != role
            or not _same_path(record.get("provider_root"), self._provider_root)
            or not _same_path(record.get("socket_path"), self._socket_path)
        ):
            raise RuntimeError("legacy Memory ownership is for another installation")

    @staticmethod
    def _parent_status(
        parent: _Identity,
        record: Mapping[str, Any],
    ) -> str:
        expected_uid = record.get("parent_uid")
        if expected_uid is not None:
            if parent.uid is None:
                return "unknown"
            if parent.uid != expected_uid:
                return "foreign"
        if "parent_starttime_ticks" in record:
            if parent.stamp is None:
                return "unknown"
            return (
                "owned"
                if parent.stamp == float(record["parent_starttime_ticks"])
                else "foreign"
            )
        value = record.get("parent_create_time")
        if not _is_stamp(value) or parent.wall_create_time is None:
            return "unknown"
        return "owned" if parent.wall_create_time == float(value) else "foreign"

    def _validate_sync_environment(
        self,
        identity: _Identity,
        record: Mapping[str, Any],
    ) -> None:
        environment = identity.environment
        expected_uid = record.get("parent_uid")
        if environment is None or (
            environment.get("AVIBE_MEMORY_SYNC_NONCE") != record["nonce"]
            or environment.get("AVIBE_MEMORY_SYNC_PARENT_PID")
            != str(record["parent_pid"])
            or environment.get("AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME")
            != float(record["parent_create_time"]).hex()
            or environment.get("AVIBE_MEMORY_SYNC_PARENT_UID")
            != ("" if expected_uid is None else str(expected_uid))
        ):
            raise RuntimeError("released sync identity is unavailable")
