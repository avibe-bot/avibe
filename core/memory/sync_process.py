"""Owned auxiliary process for the pathless EverOS ``cascade sync`` command.

Sync deliberately has its own singleton record.  It can coexist with the live
sidecar, but a malformed or live record always blocks a new launch until the
recorded process is proven gone.  This is a lifecycle primitive, not a queue or
durable recovery scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import psutil

from config import paths
from core.memory.process import (
    _MemoryChildRole,
    _ProcessKind,
    _ProcessIdentity,
    _ProcessHost,
    _ensure_owner_directory,
    EverOSProcessSettings,
    _memory_child_environment,
    _positive_timeout,
    _finish_handoff_despite_cancellation,
    _terminate_owned_process_tree,
)


SYNC_RECORD_FILENAME = "everos.sync.json"
SYNC_BOOTSTRAP_ENV = "AVIBE_MEMORY_SYNC_BOOTSTRAP"
SYNC_NONCE_ENV = "AVIBE_MEMORY_SYNC_NONCE"
SYNC_PARENT_PID_ENV = "AVIBE_MEMORY_SYNC_PARENT_PID"
SYNC_PARENT_CREATE_TIME_ENV = "AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME"
SYNC_PARENT_UID_ENV = "AVIBE_MEMORY_SYNC_PARENT_UID"
SYNC_ROLE = _MemoryChildRole.CASCADE_SYNC.value
SYNC_ARGV = ("-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync")
_MAX_RECORD_BYTES = 16 * 1024
_STOP_TIMEOUT_SECONDS = 10.0
_SYNC_TIMEOUT_SECONDS = 30 * 60.0
_HANDSHAKE_TIMEOUT_SECONDS = 30.0
_SYNC_LOCK_FILENAME = "everos.sync.lock"


class SyncProcessResult(str, Enum):
    COMPLETED = "completed"
    ALREADY_RUNNING = "already_running"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class SyncOwnershipError(RuntimeError):
    """A sync ownership record could not be proven safe to reconcile."""


class _SyncOwnershipBusy(SyncOwnershipError):
    """Another launcher won the record claim."""


class _SyncIdentityMismatch(SyncOwnershipError):
    """A disclosed child identity proves a recorded pid was recycled."""


def sync_record_path(memory_dir: Path | str) -> Path:
    return Path(memory_dir) / ".rt" / SYNC_RECORD_FILENAME


@dataclass(frozen=True)
class _ParentIdentity:
    pid: int
    create_time: float
    uid: int | None


def _parent_identity() -> _ParentIdentity:
    process = psutil.Process(os.getpid())
    create_time = float(process.create_time())
    getter = getattr(os, "getuid", None)
    uid = int(getter()) if callable(getter) else None
    return _ParentIdentity(os.getpid(), create_time, uid)


class SyncOwnership:
    """Read, atomically write, and reconcile the independent sync record."""

    def __init__(self, path: Path, *, provider_root: Path, host: _ProcessHost) -> None:
        self.path = Path(path)
        self.provider_root = Path(provider_root)
        self.host = host

    @contextlib.contextmanager
    def _locked(self):
        """Serialize record mutations across independent launcher processes."""
        _ensure_owner_directory(self.path.parent)
        lock_path = self.path.parent / _SYNC_LOCK_FILENAME
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_CLOEXEC", 0))
        no_follow = int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(lock_path, flags | no_follow, 0o600)
        try:
            info = os.fstat(descriptor)
            getter = getattr(os, "getuid", None)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise SyncOwnershipError("sync ownership lock is unsafe")
            if callable(getter) and info.st_uid != int(getter()):
                raise SyncOwnershipError("sync ownership lock has unexpected owner")
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except SyncOwnershipError:
            raise
        except OSError as exc:
            raise SyncOwnershipError("sync ownership lock is unsafe") from exc
        finally:
            os.close(descriptor)

    def read(self) -> dict[str, Any] | None:
        _ensure_owner_directory(self.path.parent)
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SyncOwnershipError("sync ownership record cannot be inspected") from exc
        if not info or not self.path.is_file() or self.path.is_symlink() or info.st_size > _MAX_RECORD_BYTES:
            raise SyncOwnershipError("sync ownership record is unsafe")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SyncOwnershipError("sync ownership record is invalid") from exc
        if not isinstance(payload, dict):
            raise SyncOwnershipError("sync ownership record is invalid")
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        """Test/maintenance writer that refuses to clobber another nonce."""
        with self._locked():
            current = self.read()
            if current is not None and current.get("nonce") != payload.get("nonce"):
                raise _SyncOwnershipBusy("sync ownership record is already claimed")
            self._write_unlocked(payload)

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        _ensure_owner_directory(self.path.parent)
        encoded = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > _MAX_RECORD_BYTES:
            raise SyncOwnershipError("sync ownership record is too large")
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, TypeError, ValueError) as exc:
            raise SyncOwnershipError("sync ownership record could not be written") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def claim(self, payload: Mapping[str, Any]) -> None:
        """Atomically claim the absent record; never replace another nonce."""
        with self._locked():
            if self.path.exists() or self.path.is_symlink():
                raise _SyncOwnershipBusy("sync ownership record is already claimed")
            _ensure_owner_directory(self.path.parent)
            encoded = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
            if len(encoded) > _MAX_RECORD_BYTES:
                raise SyncOwnershipError("sync ownership record is too large")
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_CLOEXEC", 0)),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError as exc:
                raise _SyncOwnershipBusy("sync ownership record is already claimed") from exc
            except (OSError, TypeError, ValueError) as exc:
                raise SyncOwnershipError("sync ownership record could not be claimed") from exc

    def finalize(self, payload: Mapping[str, Any], *, nonce: str) -> None:
        """Transition pending -> finalized only for this exact nonce."""
        with self._locked():
            current = self.read()
            if current is None or current.get("nonce") != nonce:
                raise SyncOwnershipError("sync ownership record changed during finalization")
            if current.get("state") != "pending":
                raise SyncOwnershipError("sync ownership record is no longer pending")
            self._write_unlocked(payload)

    def remove(self, *, nonce: str) -> None:
        with self._locked():
            current = self.read()
            if current is None:
                return
            if current.get("nonce") != nonce:
                raise SyncOwnershipError("sync ownership record changed during cleanup")
            try:
                self.path.unlink()
            except FileNotFoundError:
                return

    async def reconcile(self) -> None:
        """Fail closed unless a recorded process is gone and exactly identified."""

        record = self.read()
        if record is None:
            return
        _validate_record(record, provider_root=self.provider_root)
        parent_identity = self.host.inspect_identity(int(record["parent_pid"]))
        if parent_identity is not None:
            expected_uid = record.get("parent_uid")
            if parent_identity.create_time is None or (
                expected_uid is not None and parent_identity.uid is None
            ):
                raise SyncOwnershipError("sync parent identity is unavailable")
            if (
                parent_identity.create_time == float(record["parent_create_time"])
                and parent_identity.uid == expected_uid
            ):
                raise SyncOwnershipError("sync operation is already owned by a live parent")
        state = record["state"]
        pid = record.get("pid")
        if state == "pending":
            finder = getattr(self.host, "find_syncs", None)
            if not callable(finder):
                raise SyncOwnershipError("pending sync ownership record requires process discovery")
            candidates = finder(
                provider_root=self.provider_root,
                python=Path(record["argv"][0]),
                nonce=str(record["nonce"]),
            )
            if not candidates:
                self.remove(nonce=str(record["nonce"]))
                return
            if len(candidates) != 1:
                raise SyncOwnershipError("pending sync ownership record is ambiguous")
            pid, created_at = next(iter(candidates.items()))
            identity = self.host.inspect_identity(pid)
            if identity is None:
                raise SyncOwnershipError("pending sync child disappeared during reconciliation")
            group = self.host.process_group(pid)
            if group is None:
                raise SyncOwnershipError("pending sync child has no isolated process group")
            record = {
                **record,
                "state": "finalized",
                "pid": pid,
                "create_time": created_at,
                "process_group": group,
            }
            _validate_child_identity(identity, record, provider_root=self.provider_root)
            self.finalize(record, nonce=str(record["nonce"]))
        if not isinstance(pid, int) or pid <= 1:
            raise SyncOwnershipError("finalized sync ownership record has no pid")
        identity = self.host.inspect_identity(pid)
        if identity is not None:
            try:
                _validate_child_identity(identity, record, provider_root=self.provider_root)
            except _SyncIdentityMismatch:
                # A disclosed mismatch (for example, a different creation time)
                # proves the recorded leader is gone and this pid was recycled.
                self.remove(nonce=str(record["nonce"]))
                return
        group = record.get("process_group")
        if not isinstance(group, int) or group <= 1:
            raise SyncOwnershipError("finalized sync ownership record has no process group")
        # The leader may have exited while a helper remains. Enumerate and
        # authenticate the complete recorded group before retiring anything.
        claimed, foreign = self.host.recorded_group_members(
            group,
            socket_path=Path(record["socket_path"]),
            provider_root=self.provider_root,
            role=_MemoryChildRole.CASCADE_SYNC,
        )
        if foreign:
            raise SyncOwnershipError("sync process group contains unverifiable members")
        identities: dict[int, float] = {}
        if identity is not None and record.get("create_time") is not None:
            identities[pid] = float(record["create_time"])
        for member_pid, created_at in claimed.items():
            member = self.host.inspect_identity(member_pid)
            if member is None:
                continue
            member_record = {**record, "pid": member_pid, "create_time": created_at}
            _validate_child_identity(
                member,
                member_record,
                provider_root=self.provider_root,
                require_argv=False,
            )
            identities[member_pid] = float(created_at)
        if self.host.live(identities):
            for signum, timeout in ((signal.SIGTERM, _STOP_TIMEOUT_SECONDS), (getattr(signal, "SIGKILL", signal.SIGTERM), 3.0)):
                self.host.signal(identities, signum, process_group=group)
                if await self.host.wait_for_exit(identities, timeout):
                    break
            else:
                raise SyncOwnershipError("sync process group did not exit")
            remaining, remaining_foreign = self.host.recorded_group_members(
                group,
                socket_path=Path(record["socket_path"]),
                provider_root=self.provider_root,
                role=_MemoryChildRole.CASCADE_SYNC,
            )
            if remaining_foreign or self.host.live(remaining):
                raise SyncOwnershipError("sync process group death is unproven")
        self.remove(nonce=str(record["nonce"]))


class EverOSSyncProcess:
    """Run one exact, artifact-owned ``cascade sync`` child."""

    def __init__(
        self,
        python: Path | str | None,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        timeout_seconds: float = _SYNC_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        settings: EverOSProcessSettings | None = None,
        _host: _ProcessHost | None = None,
    ) -> None:
        self.python = Path(os.path.abspath(os.fspath(python))) if python is not None else None
        self.effective_home = Path(os.path.abspath(os.fspath(effective_home or paths.get_vibe_remote_dir())))
        self.memory_dir = self.effective_home / "memory"
        self.provider_root = Path(os.path.abspath(os.fspath(provider_root or self.memory_dir / "everos-root")))
        self.timeout_seconds = _positive_timeout(timeout_seconds, _SYNC_TIMEOUT_SECONDS)
        self.stop_timeout_seconds = _positive_timeout(stop_timeout_seconds, _STOP_TIMEOUT_SECONDS)
        self.settings = settings or EverOSProcessSettings()
        self.host = _host or _default_host()
        self._ownership = SyncOwnership(
            sync_record_path(self.memory_dir),
            provider_root=self.provider_root,
            host=self.host,
        )

    async def reconcile_orphan(self) -> None:
        await self._ownership.reconcile()

    async def run(self) -> SyncProcessResult:
        if os.name != "posix" or self.python is None or not self.python.is_file():
            return SyncProcessResult.FAILED
        try:
            _ensure_owner_directory(self.memory_dir)
            _ensure_owner_directory(self.memory_dir / ".rt")
            await self._ownership.reconcile()
        except SyncOwnershipError:
            return SyncProcessResult.ALREADY_RUNNING
        parent = _parent_identity()
        nonce = secrets.token_hex(32)
        argv = [str(self.python), *SYNC_ARGV]
        pending: dict[str, Any] = {
            "state": "pending",
            "nonce": nonce,
            "pid": None,
            "create_time": None,
            "process_group": None,
            "parent_pid": parent.pid,
            "parent_create_time": parent.create_time,
            "parent_uid": parent.uid,
            "provider_root": str(self.provider_root),
            "socket_path": str(self.memory_dir / ".rt" / "everos.sock"),
            "role": SYNC_ROLE,
            "argv": argv,
        }
        process: asyncio.subprocess.Process | None = None
        group: int | None = None
        identities: dict[int, float] = {}
        spawn_interrupted = False
        try:
            self._ownership.claim(pending)
            env = _sync_environment(
                self.python,
                self.memory_dir,
                self.provider_root,
                parent,
                nonce,
                settings=self.settings,
            )
            process, spawn_interrupted = await _finish_handoff_despite_cancellation(
                self._ownership.host.spawn(
                    _ProcessKind.CASCADE_SYNC,
                    self.python,
                    cwd=self.memory_dir,
                    env=env,
                )
            )
            group = self._ownership.host.process_group(process.pid)
            identities = self._ownership.host.snapshot_tree(process.pid, group)
            if not await self._ownership.host.wait_for_stopped(
                process.pid,
                _HANDSHAKE_TIMEOUT_SECONDS,
            ):
                raise SyncOwnershipError("sync child did not enter ownership handshake")
            identity = self._ownership.host.inspect_identity(process.pid)
            if identity is None or group is None:
                raise SyncOwnershipError("sync child identity could not be observed")
            _validate_child_identity(identity, {**pending, "pid": process.pid, "create_time": identity.create_time}, provider_root=self.provider_root)
            if identity.create_time is None:
                raise SyncOwnershipError("sync child creation time is unavailable")
            finalized = {**pending, "state": "finalized", "pid": process.pid, "create_time": identity.create_time, "process_group": group}
            self._ownership.finalize(finalized, nonce=nonce)
            process.send_signal(signal.SIGCONT)
            if spawn_interrupted:
                result = SyncProcessResult.INTERRUPTED
            else:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
                    result = SyncProcessResult.COMPLETED if process.returncode == 0 else SyncProcessResult.FAILED
                except asyncio.TimeoutError:
                    result = SyncProcessResult.TIMED_OUT
                except asyncio.CancelledError:
                    result = SyncProcessResult.INTERRUPTED
            identities[process.pid] = float(identity.create_time)
            await self._terminate_owned_sync_tree(
                process,
                process_group=group,
                owned_processes=identities,
                stop_timeout_seconds=self.stop_timeout_seconds,
            )
            self._ownership.remove(nonce=nonce)
            return result
        except asyncio.CancelledError:
            await self._cleanup_failed_launch(process, group, identities, nonce)
            return SyncProcessResult.INTERRUPTED
        except _SyncOwnershipBusy:
            return SyncProcessResult.ALREADY_RUNNING
        except (OSError, SyncOwnershipError, asyncio.TimeoutError):
            await self._cleanup_failed_launch(process, group, identities, nonce)
            return SyncProcessResult.FAILED

    async def _terminate_owned_sync_tree(
        self,
        process: asyncio.subprocess.Process,
        *,
        process_group: int | None,
        owned_processes: Mapping[int, float],
        stop_timeout_seconds: float,
    ) -> None:
        """Authenticate late group members before retiring sync ownership."""

        identities = dict(owned_processes)
        if process_group is not None:
            claimed, foreign = self._ownership.host.recorded_group_members(
                process_group,
                socket_path=self.memory_dir / ".rt" / "everos.sock",
                provider_root=self.provider_root,
                role=_MemoryChildRole.CASCADE_SYNC,
            )
            if foreign:
                raise SyncOwnershipError("sync process group contains unverifiable members")
            for pid, created_at in claimed.items():
                identities.setdefault(pid, created_at)

        await _terminate_owned_process_tree(
            self._ownership.host,
            process,
            process_group=process_group,
            owned_processes=identities,
            stop_timeout_seconds=stop_timeout_seconds,
        )

        if process_group is not None:
            remaining, foreign = self._ownership.host.recorded_group_members(
                process_group,
                socket_path=self.memory_dir / ".rt" / "everos.sock",
                provider_root=self.provider_root,
                role=_MemoryChildRole.CASCADE_SYNC,
            )
            if foreign or self._ownership.host.live(remaining):
                raise SyncOwnershipError("sync process group death is unproven")

    async def _cleanup_failed_launch(
        self,
        process: asyncio.subprocess.Process | None,
        group: int | None,
        identities: Mapping[int, float],
        nonce: str,
    ) -> None:
        if process is None:
            # A spawn exception may happen before asyncio returns a Process.
            # Retire only our still-pending nonce after exact child discovery;
            # an unknown finder keeps the evidence for boot reconciliation.
            finder = getattr(self._ownership.host, "find_syncs", None)
            if callable(finder):
                try:
                    current = self._ownership.read()
                    if current is not None and current.get("nonce") == nonce and current.get("state") == "pending":
                        candidates = finder(
                            provider_root=self.provider_root,
                            python=Path(current["argv"][0]),
                            nonce=nonce,
                        )
                        if not candidates:
                            self._ownership.remove(nonce=nonce)
                except Exception:
                    # Preserve the record when discovery is uncertain.
                    pass
            return
        try:
            await self._terminate_owned_sync_tree(
                process,
                process_group=group,
                owned_processes=identities,
                stop_timeout_seconds=self.stop_timeout_seconds,
            )
            self._ownership.remove(nonce=nonce)
        except Exception:
            # The pending/finalized record is retained so boot reconciliation
            # remains fail-closed when exact cleanup cannot be proven.
            return


def _default_host() -> _ProcessHost:
    from core.memory.process import _SystemProcessHost

    return _SystemProcessHost()


def _sync_environment(
    python: Path,
    memory_dir: Path,
    provider_root: Path,
    parent: _ParentIdentity,
    nonce: str,
    *,
    settings: EverOSProcessSettings,
) -> dict[str, str]:
    env = _memory_child_environment(
        python=python,
        memory_dir=memory_dir,
        provider_root=provider_root,
        attachments_root=memory_dir / "attachments",
        settings=settings,
        role=_MemoryChildRole.CASCADE_SYNC,
    )
    # ``-I`` already ignores PYTHONPATH; omit it entirely so the artifact-local
    # bootstrap has no mutable checkout path even in its inherited environment.
    env.pop("PYTHONPATH", None)
    env.update(
        {
            SYNC_BOOTSTRAP_ENV: "1",
            SYNC_NONCE_ENV: nonce,
            SYNC_PARENT_PID_ENV: str(parent.pid),
            SYNC_PARENT_CREATE_TIME_ENV: float(parent.create_time).hex(),
            SYNC_PARENT_UID_ENV: "" if parent.uid is None else str(parent.uid),
        }
    )
    return env


def _validate_record(record: Mapping[str, Any], *, provider_root: Path) -> None:
    if record.get("role") != SYNC_ROLE or record.get("provider_root") != str(provider_root):
        raise SyncOwnershipError("sync ownership record is for another role or root")
    if record.get("state") not in {"pending", "finalized"}:
        raise SyncOwnershipError("sync ownership record state is invalid")
    nonce = record.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        raise SyncOwnershipError("sync ownership nonce is invalid")
    argv = record.get("argv")
    if (
        not isinstance(argv, list)
        or len(argv) != len(SYNC_ARGV) + 1
        or not isinstance(argv[0], str)
        or not Path(argv[0]).is_absolute()
        or tuple(argv[1:]) != SYNC_ARGV
    ):
        raise SyncOwnershipError("sync ownership argv is not exact")
    parent_pid = record.get("parent_pid")
    parent_create_time = record.get("parent_create_time")
    parent_uid = record.get("parent_uid")
    if not isinstance(parent_pid, int) or parent_pid <= 1:
        raise SyncOwnershipError("sync parent pid is invalid")
    if not isinstance(parent_create_time, (int, float)) or isinstance(parent_create_time, bool):
        raise SyncOwnershipError("sync parent create-time is invalid")
    if parent_uid is not None and (not isinstance(parent_uid, int) or isinstance(parent_uid, bool) or parent_uid < 0):
        raise SyncOwnershipError("sync parent uid is invalid")


def _validate_child_identity(
    identity: _ProcessIdentity,
    record: Mapping[str, Any],
    *,
    provider_root: Path,
    require_argv: bool = True,
) -> None:
    expected_create = record.get("create_time")
    if expected_create is not None:
        if identity.create_time is None:
            raise SyncOwnershipError("sync child creation time is unavailable")
        if identity.create_time != float(expected_create):
            raise _SyncIdentityMismatch("sync child creation time does not match")
    uid = record.get("parent_uid")
    if uid is not None:
        if identity.uid is None:
            raise SyncOwnershipError("sync child uid is unavailable")
        if identity.uid != int(uid):
            raise _SyncIdentityMismatch("sync child uid does not match")
    argv = record.get("argv")
    if require_argv:
        if identity.cmdline is None:
            raise SyncOwnershipError("sync child argv is unavailable")
        if tuple(identity.cmdline) != tuple(argv):
            raise _SyncIdentityMismatch("sync child argv does not match")
    environment = identity.environment
    if environment is None:
        raise SyncOwnershipError("sync child environment is unavailable")
    if environment.get("EVEROS_ROOT") != str(provider_root):
        raise _SyncIdentityMismatch("sync child provider root does not match")
    if environment.get("AVIBE_MEMORY_CHILD_ROLE") != SYNC_ROLE:
        raise _SyncIdentityMismatch("sync child role does not match")
    if environment.get(SYNC_NONCE_ENV) != record.get("nonce"):
        raise _SyncIdentityMismatch("sync child nonce does not match")
    if environment.get(SYNC_PARENT_PID_ENV) != str(record.get("parent_pid")):
        raise _SyncIdentityMismatch("sync parent pid does not match")
    if environment.get(SYNC_PARENT_CREATE_TIME_ENV) != float(record.get("parent_create_time")).hex():
        raise _SyncIdentityMismatch("sync parent create-time does not match")
    expected_uid = "" if uid is None else str(uid)
    if environment.get(SYNC_PARENT_UID_ENV) != expected_uid:
        raise _SyncIdentityMismatch("sync parent uid does not match")
