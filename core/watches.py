"""Managed background watch persistence and runtime orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from config import paths
from core import watch_worker
from core.command_runner import SupervisedCommandStartupError, run_supervised_command
from core.process_isolation import (
    DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS,
    PersistedProcessIdentity,
    inspect_process_identity,
    process_group_exists,
    process_group_identity_status,
    process_identity_matches,
    terminate_process_group_by_pgid,
    terminate_process_tree_by_pid,
)
from core.scheduled_tasks import TaskExecutionRequest, TaskExecutionStore
from storage.background import (
    DEFINITION_CYCLE_COLUMNS,
    DefinitionWriteConflict,
    DefinitionWriteExpectation,
    SQLiteBackgroundTaskStore,
    definition_resume_clear_columns,
)
from vibe import runtime
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

DEFAULT_RETRY_EXIT_CODE = 75
WATCH_RECONCILE_INTERVAL_SECONDS = 2.0
WATCH_STORE_RECONCILE_FUSE_FAILURES = 3
WATCH_RECOVERY_ENTRY_TIMEOUT_SECONDS = 2 * DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_signature(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _payload_float(payload: dict[str, Any], key: str, default: float) -> float:
    if key not in payload or payload.get(key) is None:
        return default
    return float(payload[key])


def _serialize_process_identity(identity: PersistedProcessIdentity) -> dict[str, Any]:
    return {
        "pid": identity.pid,
        "create_time": identity.create_time,
        "worker_fingerprint": identity.worker_fingerprint,
    }


def _valid_worker_fingerprint(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(char in "0123456789abcdef" for char in value[7:])


def _process_identity_from_runtime_entry(
    entry: dict[str, Any],
    pid: int,
) -> PersistedProcessIdentity | None:
    payload = entry.get("process_identity")
    if not isinstance(payload, dict):
        return None
    identity_pid = payload.get("pid")
    create_time = payload.get("create_time")
    worker_fingerprint = payload.get("worker_fingerprint")
    try:
        normalized_create_time = float(create_time)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not isinstance(identity_pid, int)
        or isinstance(identity_pid, bool)
        or identity_pid != pid
        or not isinstance(create_time, (int, float))
        or isinstance(create_time, bool)
        or not math.isfinite(normalized_create_time)
        or normalized_create_time <= 0
        or not _valid_worker_fingerprint(worker_fingerprint)
    ):
        return None
    return PersistedProcessIdentity(
        pid=identity_pid,
        create_time=normalized_create_time,
        worker_fingerprint=worker_fingerprint,
    )


def _entry_updated_timestamp(entry: dict[str, Any]) -> float | None:
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return None
    try:
        return datetime.fromisoformat(updated_at).timestamp()
    except ValueError:
        return None


def _legacy_pid_was_reused(entry: dict[str, Any], create_time: float) -> bool:
    updated_timestamp = _entry_updated_timestamp(entry)
    return updated_timestamp is not None and create_time > updated_timestamp


@dataclass
class ManagedWatch:
    id: str
    name: Optional[str]
    session_key: str
    session_id: Optional[str] = None
    agent_name: Optional[str] = None
    session_policy: Optional[str] = None
    command: list[str] = field(default_factory=list)
    shell_command: Optional[str] = None
    prefix: Optional[str] = None
    message: Optional[str] = None
    cwd: Optional[str] = None
    mode: str = "once"
    timeout_seconds: float = 21600.0
    lifetime_timeout_seconds: float = 0.0
    retry_exit_codes: list[int] = field(default_factory=lambda: [DEFAULT_RETRY_EXIT_CODE])
    retry_delay_seconds: float = 30.0
    post_to: Optional[str] = None
    deliver_key: Optional[str] = None
    enabled: bool = True
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    last_started_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    retired_at: Optional[str] = None
    last_event_at: Optional[str] = None
    last_error: Optional[str] = None
    last_exit_code: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManagedWatch":
        return cls(
            id=str(payload.get("id") or uuid4().hex[:12]),
            name=(str(payload["name"]).strip() if payload.get("name") is not None else None) or None,
            session_key=str(payload.get("session_key") or ""),
            session_id=(str(payload["session_id"]).strip() if payload.get("session_id") else None),
            agent_name=(str(payload["agent_name"]).strip() if payload.get("agent_name") else None),
            session_policy=(str(payload["session_policy"]).strip() if payload.get("session_policy") else None),
            command=list(payload.get("command") or []),
            shell_command=(str(payload["shell_command"]).strip() if payload.get("shell_command") else None) or None,
            prefix=(str(payload["prefix"]).strip() if payload.get("prefix") else None) or None,
            message=(str(payload["message"]).strip() if payload.get("message") else None) or None,
            cwd=(str(payload["cwd"]).strip() if payload.get("cwd") else None) or None,
            mode=str(payload.get("mode") or "once"),
            timeout_seconds=_payload_float(payload, "timeout_seconds", 21600.0),
            lifetime_timeout_seconds=_payload_float(payload, "lifetime_timeout_seconds", 0.0),
            retry_exit_codes=[int(code) for code in (payload.get("retry_exit_codes") or [DEFAULT_RETRY_EXIT_CODE])],
            retry_delay_seconds=_payload_float(payload, "retry_delay_seconds", 30.0),
            post_to=payload.get("post_to"),
            deliver_key=payload.get("deliver_key"),
            enabled=bool(payload.get("enabled", True)),
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            updated_at=str(payload.get("updated_at") or _utc_now_iso()),
            last_started_at=payload.get("last_started_at"),
            last_finished_at=payload.get("last_finished_at"),
            retired_at=payload.get("retired_at"),
            last_event_at=payload.get("last_event_at"),
            last_error=payload.get("last_error"),
            last_exit_code=(int(payload["last_exit_code"]) if payload.get("last_exit_code") is not None else None),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )


def _missing_watch_cwd_error(watch: ManagedWatch) -> Optional[str]:
    if not watch.cwd or Path(watch.cwd).is_dir():
        return None
    return f"watch working directory no longer exists or is not a directory: {watch.cwd}"


def _watch_spawn_cwd(watch: ManagedWatch) -> str:
    if watch.cwd:
        return watch.cwd
    stable_cwd = paths.get_vibe_remote_dir()
    stable_cwd.mkdir(parents=True, exist_ok=True)
    return str(stable_cwd)


class ManagedWatchStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.get_watches_path()
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None
        self._signature: Optional[tuple[int, int, int]] = None
        self._watches: dict[str, ManagedWatch] = {}
        #: Set when a failed write left this mirror INCOMPLETE, cleared by the reload
        #: that repairs it. See ``maybe_reload`` and ``_reload_after_lost_write``.
        self._reload_required = False
        self.load()

    def load(self) -> None:
        if self._sqlite is not None:
            self._watches = {
                item["id"]: ManagedWatch.from_dict(item)
                for item in self._sqlite.list_watches()
            }
            self._reload_required = False
            return
        if not self.path.exists():
            self._watches = {}
            self._signature = None
            self._reload_required = False
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load managed watches: %s", exc)
            self._watches = {}
            self._signature = None
            return

        raw_watches = payload.get("watches", []) if isinstance(payload, dict) else []
        watches: dict[str, ManagedWatch] = {}
        for item in raw_watches:
            if not isinstance(item, dict):
                continue
            watch = ManagedWatch.from_dict(item)
            watches[watch.id] = watch
        self._watches = watches
        self._signature = _path_signature(self.path)
        self._reload_required = False

    def maybe_reload(self) -> bool:
        """Refresh the mirror when the database changed -- or when WE know it is stale.

        HFR-277, the task store's twin. The probe behind ``self._sqlite.maybe_reload``
        is ``PRAGMA data_version``, which only moves for a COMMITTED write by another
        connection. The write that drops an entry in ``_reload_after_lost_write`` ROLLED
        BACK, so data_version is unchanged and every later call here answered "nothing
        changed": the dropped watch stayed invisible to ``reconcile_watches`` -- which
        picks what runs out of exactly this dict -- while its row sat enabled in SQLite,
        durably, until a restart or an unrelated commit bumped the counter.

        ``_reload_required`` is in-process state rather than a database column on
        purpose: it describes THIS mirror, not the data, and a restart reloads from
        SQLite anyway, so there is nothing for it to survive. Making it durable would
        also mean writing to the database that was just proven unwritable. Only ``load``
        clears it, so a reload that fails again is retried on every later tick.
        """

        if self._sqlite is not None:
            changed = self._sqlite.maybe_reload()
            if self._reload_required:
                try:
                    self.load()
                except Exception:
                    # Still unreachable. Keep the flag and the incomplete mirror, and
                    # report "nothing changed" -- the retry is the next tick's.
                    logger.exception(
                        "Could not reload managed watches after a lost write; the live "
                        "store stays incomplete until a later attempt succeeds"
                    )
                    return False
                return True
            if changed:
                self.load()
            return changed
        signature = _path_signature(self.path)
        if signature == self._signature and not self._reload_required:
            return False
        self.load()
        return True

    def _save(self) -> None:
        if self._sqlite is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"watches": [watch.to_dict() for watch in self.list_watches()]}
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2)
            tmp_path = Path(handle.name)
        tmp_path.replace(self.path)
        self._signature = _path_signature(self.path)

    def list_watches(self) -> list[ManagedWatch]:
        return sorted(self._watches.values(), key=lambda item: (item.created_at, item.id))

    def list_watches_for_recovery(self) -> list[ManagedWatch]:
        """Return a strict, current snapshot suitable for process recovery."""
        if self._sqlite is not None:
            raw_watches = self._sqlite.list_watches()
        elif not self.path.exists():
            raw_watches = []
        else:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("watches"), list):
                raise ValueError("managed watch store must contain a watches list")
            raw_watches = payload["watches"]

        watches: list[ManagedWatch] = []
        seen_ids: set[str] = set()
        for item in raw_watches:
            if not isinstance(item, dict):
                raise ValueError("managed watch store contains an invalid watch entry")
            watch_id = item.get("id")
            if not isinstance(watch_id, str) or not watch_id or watch_id in seen_ids:
                raise ValueError("managed watch store contains a missing or duplicate watch id")
            command = item.get("command")
            if command is not None and (
                not isinstance(command, list)
                or any(not isinstance(part, str) for part in command)
                or (command and not command[0])
            ):
                raise ValueError(f"managed watch {watch_id} contains an invalid command")
            shell_command = item.get("shell_command")
            if shell_command is not None and not isinstance(shell_command, str):
                raise ValueError(f"managed watch {watch_id} contains an invalid shell command")
            watch = ManagedWatch.from_dict(item)
            watches.append(watch)
            seen_ids.add(watch.id)
        return sorted(watches, key=lambda item: (item.created_at, item.id))

    def get_watch(self, watch_id: str) -> Optional[ManagedWatch]:
        return self._watches.get(watch_id)

    @staticmethod
    def _read_state(watch: ManagedWatch) -> DefinitionWriteExpectation:
        """The guarded state a full-row payload for ``watch`` is derived from.

        The task-store twin, for the same reason: ``ManagedWatch`` is a mirror of the
        stored row, every writer below edits a few fields and writes them ALL back,
        and the dataclass has no ``deleted_at`` to write back at all.
        """

        return DefinitionWriteExpectation.from_read(
            session_id=watch.session_id,
            enabled=watch.enabled,
            deleted_at=None,
            metadata=watch.metadata,
        )

    @property
    def sqlite_backend(self) -> Optional[SQLiteBackgroundTaskStore]:
        """The SQLite backend behind this store, or ``None`` for the file backend.

        The guard ``_write_watch`` applies lives in SQLite; the file backend has no
        compare-and-set at all. Exposed so a caller can ask whether a guarded stamp and
        an outbox row can be committed together (HFR-269).
        """

        return self._sqlite

    def _write_watch(
        self,
        watch: ManagedWatch,
        expect: DefinitionWriteExpectation,
        *,
        queued_run: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> bool:
        """Persist a whole watch row; ``False`` means the guard refused the write.

        ``queued_run`` is an ``agent_runs`` outbox payload this write AUTHORISES; it is
        committed in the SAME transaction as the row (HFR-269), so a refusal or a
        failure leaves neither behind. Only the SQLite backend can do that, and passing
        one to the file backend is a caller bug -- see ``sqlite_backend``.

        EVERY way this write can fail to land reloads the mirror (HFR-271). This store is
        a write-through cache: each caller mutates the cached ``ManagedWatch`` and hands
        the whole row here, so if the write does not stick, the mutation must not either.
        Reloading on the ``False`` return alone was half the job -- a raised exception
        rolls the transaction back just as completely, and left the process serving edits
        the database never accepted. ``reconcile_watches`` chooses which watches keep
        running from this dict, ``_read_state`` derives the NEXT compare-and-set's
        expectation from it, and ``_watch_store_call`` swallows the exception, so nothing
        downstream would ever have corrected it.
        """

        try:
            if self._sqlite is None:
                if queued_run is not None:
                    raise ValueError(
                        "a file-backed watch store cannot commit a queued run with the watch row"
                    )
                self._save()
                return True
            if queued_run is None:
                landed = self._sqlite.upsert_watch(
                    watch.to_dict(),
                    expect=expect,
                    expected_enabled_agent_id=expected_enabled_agent_id,
                    expected_reference_agent_id=expected_reference_agent_id,
                )
            else:
                landed = self._sqlite.upsert_watch_with_queued_run(
                    watch.to_dict(), expect=expect, run_payload=queued_run
                )
        except Exception:
            self._reload_after_lost_write(watch.id)
            raise
        if landed:
            return True
        self.load()
        return False

    def _reload_after_lost_write(self, watch_id: str) -> None:
        """Drop a mirror entry the database did not accept, reloading if it can.

        The reload itself can fail -- the fault that killed the write is usually still
        there. Keeping the mutated entry would be the worse answer of the two, so it is
        dropped: an absent watch reads as "gone" to ``reconcile_watches`` and stops that
        watch, where a stale one keeps it running against state the database never had,
        and ``maybe_reload`` restores it as soon as the database is reachable again --
        which it can only do because dropping the entry also marks the mirror as needing
        an UNCONDITIONAL reload (HFR-277). The failed write rolled back, so ``PRAGMA
        data_version`` never moved and the probe alone would report "nothing changed"
        forever.
        """

        try:
            self.load()
        except Exception:
            logger.exception(
                "Could not reload managed watches after a failed write; dropping the "
                "stale mirror entry for %s",
                watch_id,
            )
            self._watches.pop(watch_id, None)
            self._signature = None
            self._reload_required = True

    def upsert_watch(
        self,
        watch: ManagedWatch,
        *,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> ManagedWatch:
        """Create or adopt a whole watch row (unguarded: the payload is not a re-read).

        The mirror rolls back with the write here too (HFR-275). This is the one entry
        point that can add an id the database has never seen, and a phantom is worse than
        a stale edit: ``reconcile_watches`` would START a watch whose creation the caller
        was told had FAILED, spawning its command and posting its output on every tick,
        with no durable row to stop it and nothing to reload it away.
        """

        watch.updated_at = _utc_now_iso()
        self._watches[watch.id] = watch
        try:
            if self._sqlite is not None:
                # No ``expect``: the create/adopt entry point (``add_watch``), whose
                # payload is not derived from a stored row.
                self._sqlite.upsert_watch(
                    watch.to_dict(),
                    expected_enabled_agent_id=expected_enabled_agent_id,
                    expected_reference_agent_id=expected_reference_agent_id,
                )
                if expected_reference_agent_id is not None:
                    self.load()
                    return self._watches[watch.id]
                return watch
            self._save()
        except Exception:
            self._reload_after_lost_write(watch.id)
            raise
        return watch

    def add_watch(
        self,
        *,
        name: Optional[str],
        session_key: str,
        command: list[str],
        shell_command: Optional[str],
        prefix: Optional[str],
        cwd: Optional[str],
        mode: str,
        timeout_seconds: float,
        lifetime_timeout_seconds: float,
        retry_exit_codes: list[int],
        retry_delay_seconds: float,
        post_to: Optional[str],
        deliver_key: Optional[str],
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> ManagedWatch:
        watch = ManagedWatch(
            id=uuid4().hex[:12],
            name=name,
            session_key=session_key,
            session_id=session_id,
            agent_name=agent_name,
            session_policy=session_policy or ("existing" if session_id or session_key else None),
            command=command,
            shell_command=shell_command,
            prefix=prefix,
            message=message or prefix,
            cwd=cwd,
            mode=mode,
            timeout_seconds=timeout_seconds,
            lifetime_timeout_seconds=lifetime_timeout_seconds,
            retry_exit_codes=retry_exit_codes,
            retry_delay_seconds=retry_delay_seconds,
            post_to=post_to,
            deliver_key=deliver_key,
            metadata=dict(metadata or {}),
        )
        return self.upsert_watch(
            watch,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
        )

    def remove_watch(self, watch_id: str) -> bool:
        """Delete a watch; the mirror rolls back with the delete (HFR-275).

        The safer direction of the same class -- an entry dropped here reads as "gone"
        and stops the watch -- but silently, and NOT self-healing: with the row still
        there and unchanged, ``maybe_reload`` sees no external write, so the watch the
        user was told could not be deleted just stops until the process restarts.
        """

        if watch_id not in self._watches:
            return False
        del self._watches[watch_id]
        try:
            if self._sqlite is not None:
                self._sqlite.remove_task(watch_id)
                return True
            self._save()
        except Exception:
            self._reload_after_lost_write(watch_id)
            raise
        return True

    def set_enabled(self, watch_id: str, enabled: bool) -> ManagedWatch:
        watch = self._watches[watch_id]
        expect = self._read_state(watch)
        if enabled and not watch.enabled:
            # Same field split the storage layer applies to the Harness UI's
            # toggle, so the two doorways cannot drift apart again.
            self._clear_cycle_state(
                watch,
                definition_resume_clear_columns("watch", watch.mode),
            )
        watch.enabled = enabled
        watch.updated_at = _utc_now_iso()
        if not self._write_watch(watch, expect):
            # Same as the task side: this payload also restores ``last_error`` and the
            # Session binding, so a pause/resume that lost to a teardown must fail
            # loudly rather than quietly undo it.
            raise DefinitionWriteConflict(watch_id, definition_type="watch")
        return watch

    def update_watch(
        self,
        watch_id: str,
        *,
        name: Optional[str],
        session_key: str,
        session_id: Optional[str],
        command: list[str],
        shell_command: Optional[str],
        prefix: Optional[str],
        cwd: Optional[str],
        mode: str,
        timeout_seconds: float,
        lifetime_timeout_seconds: float,
        retry_exit_codes: list[int],
        retry_delay_seconds: float,
        post_to: Optional[str],
        deliver_key: Optional[str],
        agent_name: Optional[str] = None,
        session_policy: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        expected_enabled_agent_id: Optional[str] = None,
        expected_reference_agent_id: Optional[str] = None,
    ) -> ManagedWatch:
        watch = self._watches[watch_id]
        # Captured before the first mutation: the state ``vibe watch update`` read and
        # resolved its payload from.
        expect = self._read_state(watch)
        if mode != watch.mode:
            # A mode change starts a new lifecycle. Completion and failure
            # metadata from the old mode remains available in run history, but
            # must not determine the definition state under the new mode.
            self._clear_cycle_state(watch, DEFINITION_CYCLE_COLUMNS)
        watch.name = name
        watch.session_key = session_key
        watch.session_id = session_id
        watch.agent_name = agent_name
        if session_policy is None:
            session_policy = watch.session_policy or ("existing" if session_id or session_key else None)
        watch.session_policy = session_policy
        watch.command = command
        watch.shell_command = shell_command
        watch.prefix = prefix
        watch.message = message or prefix
        watch.cwd = cwd
        watch.mode = mode
        watch.timeout_seconds = timeout_seconds
        watch.lifetime_timeout_seconds = lifetime_timeout_seconds
        watch.retry_exit_codes = retry_exit_codes
        watch.retry_delay_seconds = retry_delay_seconds
        watch.post_to = post_to
        watch.deliver_key = deliver_key
        if metadata is not None:
            watch.metadata = dict(metadata)
        watch.updated_at = _utc_now_iso()
        if not self._write_watch(
            watch,
            expect,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
        ):
            # The edit did NOT land. ``cmd_watch_update`` turns this into a non-zero
            # exit with an error payload instead of echoing an unwritten watch.
            raise DefinitionWriteConflict(watch_id, definition_type="watch")
        if expected_reference_agent_id is not None:
            self.load()
            return self._watches[watch_id]
        return watch

    @staticmethod
    def _clear_cycle_state(watch: ManagedWatch, columns: tuple[str, ...]) -> None:
        # The same columns ``set_definition_enabled`` nulls out, applied to the
        # in-memory mirror.
        for column in columns:
            setattr(watch, column, None)

    def mark_cycle_start(self, watch_id: str) -> bool:
        self.maybe_reload()
        watch = self._watches.get(watch_id)
        if watch is None:
            return False
        expect = self._read_state(watch)
        watch.last_started_at = _utc_now_iso()
        watch.last_error = None
        watch.updated_at = _utc_now_iso()
        # A runtime stamp: a lost write is reported by the return value, not by an
        # exception through the supervisor loop.
        return self._write_watch(watch, expect)

    def mark_cycle_result(
        self,
        watch_id: str,
        *,
        exit_code: Optional[int],
        error: Optional[str],
        event_detected: bool = False,
        disable: bool = False,
        queued_run: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Stamp a cycle's outcome; ``False`` means the store refused the write.

        ``queued_run`` is the completion-hook outbox row this stamp AUTHORISES. Passing
        it makes the stamp and the hook ONE transaction (HFR-269): both land or neither
        does, so no teardown can commit between them and no failure can disable a
        ``once`` watch while losing the hook that tells the user it finished.
        """

        self.maybe_reload()
        watch = self._watches.get(watch_id)
        if watch is None:
            return False
        expect = self._read_state(watch)
        now = _utc_now_iso()
        # Retirement is state, not a conclusion drawn from cycle history.
        # Only the cycle that changes enabled -> disabled may write it. A cycle
        # landing after a manual pause must preserve that pause; a later result
        # must likewise not erase a genuine earlier retirement.
        was_enabled = watch.enabled
        if was_enabled:
            watch.last_finished_at = now if disable else None
            watch.retired_at = now if disable else None
        watch.last_exit_code = exit_code
        watch.last_error = error
        if event_detected:
            watch.last_event_at = now
        if disable:
            watch.enabled = False
        watch.updated_at = _utc_now_iso()
        # Guarded for the reason ``mark_task_result`` is: a cycle result landing after a
        # ``/new`` reclaim would otherwise re-enable the watch and restore the binding
        # the teardown cleared.
        return self._write_watch(watch, expect, queued_run=queued_run)


class WatchRuntimeStateStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.get_watch_runtime_path()
        self._sqlite = SQLiteBackgroundTaskStore() if path is None else None

    def write(self, payload: dict[str, Any]) -> None:
        if self._sqlite is not None:
            self._sqlite.write_watch_runtime(payload, updated_at=_utc_now_iso())
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2)
            tmp_path = Path(handle.name)
        tmp_path.replace(self.path)

    def load(self) -> dict[str, Any]:
        if self._sqlite is not None:
            return self._sqlite.load_watch_runtime()
        try:
            return self.load_for_recovery()
        except Exception:
            return {"watches": {}}

    def load_for_recovery(self) -> dict[str, Any]:
        """Load and validate state used to identify workers from a prior service."""
        if self._sqlite is not None:
            payload = self._sqlite.load_watch_runtime()
        elif not self.path.exists():
            payload = {"watches": {}}
        else:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("watch runtime state must be a JSON object")
        watches = payload.get("watches")
        if not isinstance(watches, dict):
            raise ValueError("watch runtime state must contain a watches object")
        if any(
            not isinstance(watch_id, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("running"), bool)
            for watch_id, entry in watches.items()
        ):
            raise ValueError("watch runtime state contains an invalid watch entry")
        return payload


def _shared_run_ledger_backend(
    store: ManagedWatchStore,
    request_store: TaskExecutionStore,
) -> Optional[SQLiteBackgroundTaskStore]:
    """The one SQLite database holding BOTH the watch definitions and the run outbox.

    ``None`` when the two stores cannot share a transaction, which is what decides
    whether a guarded stamp and the hook it authorises can be committed together
    (HFR-269). In production both stores default to the same
    ``paths.get_sqlite_state_path()`` database, so the answer is always the shared
    backend; file-backed stores (tests, legacy state) keep watches in a JSON file and
    runs in a directory of JSON files, and the ``db_path`` comparison also refuses two
    SQLite stores that happen to point at different databases.
    """

    watch_backend = store.sqlite_backend
    run_backend = request_store.sqlite_backend
    if watch_backend is None or run_backend is None:
        return None
    if watch_backend.db_path != run_backend.db_path:
        return None
    return watch_backend


@dataclass
class _CycleResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class ManagedWatchService:
    def __init__(
        self,
        controller,
        store: Optional[ManagedWatchStore] = None,
        request_store: Optional[TaskExecutionStore] = None,
        runtime_store: Optional[WatchRuntimeStateStore] = None,
    ):
        self.controller = controller
        self.store = store or ManagedWatchStore()
        self.request_store = request_store or TaskExecutionStore()
        self.runtime_store = runtime_store or WatchRuntimeStateStore()
        self._running = False
        self._startup_task: Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._active_pids: dict[str, int] = {}
        self._active_process_identities: dict[str, PersistedProcessIdentity] = {}
        self._watch_started_at: dict[str, str] = {}
        self._fused_watch_ids: set[str] = set()
        self._recovery_blocked_watch_ids: set[str] = set()
        self._unreaped_runtime_entries: dict[str, dict[str, Any]] = {}
        self._store_error_fused = False
        self._store_reconcile_failures = 0
        self._recovery_pending = True
        self._requires_service_lease = runtime.service_instance_lock_attached_to_process()
        self._reconcile_dirty = True
        self._runtime_state_dirty = True

    def _t(self, key: str, **kwargs: Any) -> str:
        controller_translator = getattr(self.controller, "_t", None)
        if callable(controller_translator):
            return controller_translator(key, **kwargs)
        language = getattr(getattr(self.controller, "config", None), "language", "en")
        return i18n_t(key, language, **kwargs)

    def _localize_watch_worker_error(self, stderr: str) -> str:
        error = watch_worker.decode_watch_worker_error(stderr)
        if error is None:
            return stderr
        code, detail = error
        protocol_error_keys = {
            "specTooLarge": "harness.watch.protocolErrors.specTooLarge",
            "invalidEncoding": "harness.watch.protocolErrors.invalidEncoding",
            "invalidJson": "harness.watch.protocolErrors.invalidJson",
            "unsupportedVersion": "harness.watch.protocolErrors.unsupportedVersion",
            "invalidCommand": "harness.watch.protocolErrors.invalidCommand",
        }
        if code in protocol_error_keys:
            detail = self._t(protocol_error_keys[code])
        elif not detail:
            detail = self._t("harness.watch.unknownSupervisorFailure")
        return self._t("harness.watch.supervisorFailed", detail=detail)

    def active_process_pids(self) -> set[int]:
        """Return active waiter process roots owned by managed watches."""
        return {pid for pid in self._active_pids.values() if isinstance(pid, int) and pid > 0}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._startup_task = asyncio.create_task(self._start_after_recovery())

    async def _start_after_recovery(self) -> None:
        try:
            recovered = await asyncio.to_thread(self._reap_stale_workers)
        except Exception:
            recovered = False
            logger.exception("Unexpected error during stale watch worker recovery")
        try:
            if not self._running or not self._owns_service_instance():
                return
            self._recovery_pending = not recovered
            if recovered:
                try:
                    if self.reconcile_watches():
                        self._runtime_state_dirty = True
                    self._write_runtime_state()
                    self._reconcile_dirty = False
                except Exception as exc:
                    self._reconcile_dirty = True
                    self._handle_reconcile_store_error(exc)
            if self._running and self._owns_service_instance():
                self._reconcile_task = asyncio.create_task(self._watch_store())
        finally:
            if self._startup_task is asyncio.current_task():
                self._startup_task = None

    def _reap_stale_workers(self) -> bool:
        try:
            runtime_state = self.runtime_store.load_for_recovery()
        except Exception:
            logger.warning(
                "Unable to read prior watch runtime state; deferring watch reconciliation",
                exc_info=True,
            )
            return False

        if not isinstance(runtime_state, dict):
            logger.warning("Prior watch runtime state is malformed; deferring watch reconciliation")
            return False
        runtime_watches = runtime_state.get("watches")
        if not isinstance(runtime_watches, dict) or any(
            not isinstance(watch_id, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("running"), bool)
            for watch_id, entry in runtime_watches.items()
        ):
            logger.warning("Prior watch runtime state is malformed; deferring watch reconciliation")
            return False
        if not runtime_watches:
            return True

        try:
            watches = self.store.list_watches_for_recovery()
            if not isinstance(watches, list) or any(not isinstance(watch, ManagedWatch) for watch in watches):
                raise ValueError("managed watch store returned an invalid watch list")
        except Exception:
            logger.warning(
                "Unable to read managed watch definitions; deferring watch reconciliation",
                exc_info=True,
            )
            return False
        watch_ids = {watch.id for watch in watches}

        for watch_id, entry in runtime_watches.items():
            if not entry.get("running"):
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
                continue
            expected_identity = _process_identity_from_runtime_entry(entry, pid)
            try:
                pid_is_alive = runtime.pid_alive(pid)
                group_is_alive = process_group_exists(pid, logger, f"stale watch {watch_id}")
                if not pid_is_alive:
                    if not group_is_alive:
                        continue
                    if expected_identity is None:
                        logger.warning(
                            "Unable to verify historical identity for stale watch process group "
                            "pgid=%s watch_id=%s; leaving it untouched",
                            pid,
                            watch_id,
                        )
                        self._block_watch_after_failed_recovery(watch_id, entry)
                        continue
                    logger.warning(
                        "Reaping stale watch process group pgid=%s watch_id=%s after its leader exited",
                        pid,
                        watch_id,
                    )
                    if not terminate_process_group_by_pgid(
                        pid,
                        logger,
                        f"stale watch {watch_id}",
                        expected_identity=expected_identity,
                    ):
                        self._block_watch_after_failed_recovery(watch_id, entry)
                    continue
            except Exception:
                logger.warning(
                    "Unable to inspect stale watch worker pid=%s watch_id=%s; leaving it untouched",
                    pid,
                    watch_id,
                    exc_info=True,
                )
                self._block_watch_after_failed_recovery(watch_id, entry)
                continue
            if expected_identity is None:
                live_identity = inspect_process_identity(pid)
                if live_identity is not None and _legacy_pid_was_reused(
                    entry,
                    live_identity.create_time,
                ):
                    logger.info(
                        "Legacy stale watch worker pid=%s watch_id=%s no longer exists; "
                        "the pid belongs to a newer process",
                        pid,
                        watch_id,
                    )
                    continue
                logger.warning(
                    "Unable to verify historical identity for stale watch worker pid=%s watch_id=%s; "
                    "leaving it untouched",
                    pid,
                    watch_id,
                )
                self._block_watch_after_failed_recovery(watch_id, entry)
                continue
            try:
                live_identity = inspect_process_identity(pid)
            except Exception:
                live_identity = None
                logger.warning(
                    "Unable to inspect stale watch worker identity pid=%s watch_id=%s; leaving it untouched",
                    pid,
                    watch_id,
                    exc_info=True,
                )
            if live_identity is None:
                logger.warning(
                    "Unable to verify stale watch worker identity pid=%s watch_id=%s; leaving it untouched",
                    pid,
                    watch_id,
                )
                self._block_watch_after_failed_recovery(watch_id, entry)
                continue
            if live_identity.create_time != expected_identity.create_time:
                logger.info(
                    "Recorded stale watch worker pid=%s watch_id=%s no longer exists; "
                    "the pid belongs to a newer process",
                    pid,
                    watch_id,
                )
                continue
            if not process_identity_matches(expected_identity, live_identity):
                logger.warning(
                    "Refusing to reap stale watch worker pid=%s watch_id=%s because its marker changed",
                    pid,
                    watch_id,
                )
                self._block_watch_after_failed_recovery(watch_id, entry)
                continue

            watch_label = f"stale watch {watch_id}"
            if watch_id not in watch_ids:
                watch_label += " (deleted definition)"
            logger.warning("Reaping stale watch worker pid=%s watch_id=%s", pid, watch_id)
            try:
                terminated = terminate_process_tree_by_pid(
                    pid,
                    logger,
                    watch_label,
                    expected_identity=expected_identity,
                )
            except Exception:
                terminated = False
                logger.exception("Unexpected error reaping stale watch worker pid=%s watch_id=%s", pid, watch_id)
            if not terminated:
                logger.error("Failed to reap stale watch worker pid=%s watch_id=%s", pid, watch_id)
                self._block_watch_after_failed_recovery(watch_id, entry)
        return True

    def _block_watch_after_failed_recovery(self, watch_id: str, entry: dict[str, Any]) -> None:
        self._recovery_blocked_watch_ids.add(watch_id)
        self._unreaped_runtime_entries[watch_id] = dict(entry)
        self._runtime_state_dirty = True

    def _recheck_recovery_blocked_watches(self) -> bool:
        unblocked = False
        for watch_id in list(self._recovery_blocked_watch_ids):
            entry = self._unreaped_runtime_entries.get(watch_id)
            if not isinstance(entry, dict):
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                continue
            try:
                if not runtime.pid_alive(pid):
                    label = f"blocked stale watch {watch_id}"
                    if not process_group_exists(pid, logger, label):
                        worker_is_gone = True
                    else:
                        expected_identity = _process_identity_from_runtime_entry(entry, pid)
                        if expected_identity is None:
                            continue
                        group_status = process_group_identity_status(
                            pid,
                            expected_identity,
                            logger,
                            label,
                        )
                        if group_status != "mismatch":
                            continue
                        worker_is_gone = True
                else:
                    live_identity = inspect_process_identity(pid)
                    if live_identity is None:
                        continue
                    expected_identity = _process_identity_from_runtime_entry(entry, pid)
                    if expected_identity is None:
                        worker_is_gone = _legacy_pid_was_reused(
                            entry,
                            live_identity.create_time,
                        )
                    else:
                        worker_is_gone = (
                            live_identity.create_time
                            != expected_identity.create_time
                        )
            except Exception:
                logger.debug(
                    "Failed to recheck blocked stale watch pid=%s watch_id=%s",
                    pid,
                    watch_id,
                    exc_info=True,
                )
                continue
            if not worker_is_gone:
                continue
            logger.info(
                "Unblocking recovered watch_id=%s after its prior worker exited",
                watch_id,
            )
            self._recovery_blocked_watch_ids.discard(watch_id)
            self._unreaped_runtime_entries.pop(watch_id, None)
            unblocked = True
        return unblocked

    async def stop(self) -> None:
        self._begin_stop()
        startup_task = self._startup_task
        if startup_task and startup_task is not asyncio.current_task():
            await startup_task
        self._startup_task = None
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        self._active_tasks.clear()
        self._active_pids.clear()
        self._active_process_identities.clear()
        self._watch_started_at.clear()
        self._runtime_state_dirty = True
        self._write_runtime_state()

    async def _watch_store(self) -> None:
        while self._running:
            if not self._owns_service_instance():
                return
            if self._recovery_pending:
                try:
                    recovered = await asyncio.to_thread(self._reap_stale_workers)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    recovered = False
                    logger.exception("Unexpected error retrying stale watch worker recovery")
                if not recovered:
                    await asyncio.sleep(WATCH_RECONCILE_INTERVAL_SECONDS)
                    continue
                self._recovery_pending = False
                self._reconcile_dirty = True
                self._runtime_state_dirty = True
            if self._store_error_fused:
                await asyncio.sleep(WATCH_RECONCILE_INTERVAL_SECONDS)
                continue
            try:
                if self._recovery_blocked_watch_ids and await asyncio.to_thread(
                    self._recheck_recovery_blocked_watches
                ):
                    self._reconcile_dirty = True
                    self._runtime_state_dirty = True
                should_reconcile = self.store.maybe_reload() or self._reconcile_dirty
                if should_reconcile:
                    if self.reconcile_watches():
                        self._runtime_state_dirty = True
                if self._runtime_state_dirty:
                    self._write_runtime_state()
                self._store_reconcile_failures = 0
                self._reconcile_dirty = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconcile_dirty = True
                self._handle_reconcile_store_error(exc)
            await asyncio.sleep(WATCH_RECONCILE_INTERVAL_SECONDS)

    def reconcile_watches(self) -> bool:
        if not self._owns_service_instance():
            return False
        if self._store_error_fused:
            return False
        watches = self.store.list_watches()
        desired_ids = {watch.id for watch in watches if watch.enabled}
        changed = False
        for watch in watches:
            if (
                not watch.enabled
                or watch.id in self._active_tasks
                or watch.id in self._fused_watch_ids
                or watch.id in self._recovery_blocked_watch_ids
            ):
                continue
            task = asyncio.create_task(self._run_watch(watch.id))
            self._active_tasks[watch.id] = task
            task.add_done_callback(lambda _task, watch_id=watch.id: self._on_watch_done(watch_id))
            changed = True

        for watch_id, task in list(self._active_tasks.items()):
            if watch_id in desired_ids:
                continue
            task.cancel()
            changed = True

        return changed

    def _on_watch_done(self, watch_id: str) -> None:
        self._active_tasks.pop(watch_id, None)
        self._active_pids.pop(watch_id, None)
        self._active_process_identities.pop(watch_id, None)
        self._watch_started_at.pop(watch_id, None)
        self._runtime_state_dirty = True
        self._write_runtime_state()
        self._reconcile_dirty = True

    def _write_runtime_state(self) -> None:
        if self._recovery_pending:
            return
        payload = {
            "watches": {
                watch_id: dict(entry)
                for watch_id, entry in self._unreaped_runtime_entries.items()
            }
        }
        now = _utc_now_iso()
        for watch_id, task in self._active_tasks.items():
            entry = {
                "running": not task.done(),
                "pid": self._active_pids.get(watch_id),
                "started_at": self._watch_started_at.get(watch_id),
                "updated_at": now,
            }
            identity = self._active_process_identities.get(watch_id)
            if identity is not None:
                entry["process_identity"] = _serialize_process_identity(identity)
            payload["watches"][watch_id] = entry
        try:
            self.runtime_store.write(payload)
            self._runtime_state_dirty = False
        except Exception:
            self._runtime_state_dirty = True
            logger.exception("Failed to persist watch runtime state")

    def _fuse_store_after_error(self, operation: str, exc: Exception, *, watch_id: str | None = None) -> None:
        if watch_id is not None:
            self._fused_watch_ids.add(watch_id)
        self._store_error_fused = True
        logger.error(
            "Disabling watch store reconciliation after persistent store error "
            "(watch_id=%s operation=%s): %s",
            watch_id,
            operation,
            exc,
            exc_info=True,
        )

    def _handle_reconcile_store_error(self, exc: Exception) -> None:
        self._store_reconcile_failures += 1
        if self._store_reconcile_failures >= WATCH_STORE_RECONCILE_FUSE_FAILURES:
            self._fuse_store_after_error("reconcile", exc)
            return
        logger.warning(
            "Managed watch reconcile failed; will retry "
            "(attempt=%s/%s): %s",
            self._store_reconcile_failures,
            WATCH_STORE_RECONCILE_FUSE_FAILURES,
            exc,
            exc_info=True,
        )

    def _watch_store_call(self, watch_id: str, operation: str, callback, *, guarded: bool = False) -> bool:
        """Run a store call for ``watch_id``; ``False`` means "do not proceed".

        TWO ways a store call can fail to happen, and the supervisor has to stop for
        BOTH. An exception fuses the store, as before. ``guarded=True`` says the
        callback's OWN return value is the answer as well: ``mark_cycle_start`` and
        ``mark_cycle_result`` are compare-and-set writes (HFR-261) that return
        ``False`` when a ``/new`` reclaim or an archive committed after the payload
        was read, and this wrapper used to DISCARD that -- ``callback(); return True``
        reported a refused write to the loop as a landed one, so the cycle went on to
        spawn its command and enqueue its hook against a definition the database had
        already torn down.

        Not the default, because a ``False`` return is not universally a refusal:
        ``maybe_reload`` returns ``False`` for "nothing changed", which is the common
        case and must not stop the watch. Only the guarded writers opt in.
        """

        try:
            result = callback()
        except Exception as exc:
            self._fuse_store_after_error(operation, exc, watch_id=watch_id)
            return False
        if guarded and result is False:
            # NOT a store error: the row is fine, this write simply lost to a
            # concurrent lifecycle change. Fusing would disable reconciliation for a
            # healthy database, so the watch is stopped and the store left alone.
            logger.warning(
                "Watch %s stopping: the store refused %s because the definition's "
                "Session binding, enabled state, deletion or reclaim snapshot changed",
                watch_id,
                operation,
            )
            return False
        return True

    def _current_asyncio_task(self) -> Optional["asyncio.Task[Any]"]:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _begin_stop(self, *, cancel_reconcile: bool = True) -> None:
        self._running = False
        current_task = self._current_asyncio_task()
        if cancel_reconcile and self._reconcile_task and self._reconcile_task is not current_task:
            self._reconcile_task.cancel()
        for task in list(self._active_tasks.values()):
            if task is not current_task:
                task.cancel()
        self._runtime_state_dirty = True
        self._write_runtime_state()

    def _owns_service_instance(self) -> bool:
        if not self._requires_service_lease:
            return True
        if runtime.current_process_owns_service_instance():
            return True
        logger.error("Managed watch service stopping because this process no longer owns the service lock")
        self._begin_stop()
        return False

    async def _run_watch(self, watch_id: str) -> None:
        lifetime_started = asyncio.get_running_loop().time()
        self._watch_started_at[watch_id] = _utc_now_iso()
        self._runtime_state_dirty = True
        self._write_runtime_state()

        while self._running:
            if not self._owns_service_instance():
                return
            if watch_id in self._fused_watch_ids:
                return
            if not self._watch_store_call(watch_id, "reload", self.store.maybe_reload):
                return
            watch = self.store.get_watch(watch_id)
            if watch is None or not watch.enabled:
                return

            if watch.mode == "forever" and watch.lifetime_timeout_seconds > 0:
                elapsed = asyncio.get_running_loop().time() - lifetime_started
                remaining_lifetime = watch.lifetime_timeout_seconds - elapsed
                if remaining_lifetime <= 0:
                    # ONE DECISION (HFR-269), not a stamp followed by a hook. See
                    # ``_commit_cycle_result``.
                    self._commit_cycle_result(
                        watch,
                        # Running out of lifetime is a timeout, and the row has
                        # to be able to say so: ``definition_lifecycle_detail``
                        # reads the exit code, and a ``None`` here made the
                        # supervisor's own deadline read as a normal ending.
                        # 124 is the same convention the per-cycle timeout uses.
                        exit_code=124,
                        error=None,
                        disable=True,
                        prefix=watch.message or watch.prefix or "Watch stopped after reaching its lifetime timeout.",
                        body=(
                            f"Watch '{watch.name or watch.id}' reached its lifetime timeout after "
                            f"{int(watch.lifetime_timeout_seconds)} second(s)."
                        ),
                    )
                    return
                cycle_timeout = watch.timeout_seconds
                if cycle_timeout <= 0:
                    cycle_timeout = remaining_lifetime
                else:
                    cycle_timeout = min(cycle_timeout, remaining_lifetime)
            else:
                cycle_timeout = watch.timeout_seconds

            # ``guarded=True``: a REFUSED start stamp stops the cycle here, BEFORE
            # ``_run_cycle`` spawns the waiter and before any hook is enqueued. The
            # refusal means a teardown (``/new`` reclaim, archive) committed after this
            # loop read the watch, so the definition it is about to run no longer
            # exists in the state this iteration decided from.
            if not self._watch_store_call(
                watch.id,
                "mark_cycle_start",
                lambda: self.store.mark_cycle_start(watch.id),
                guarded=True,
            ):
                return
            cwd_error = _missing_watch_cwd_error(watch)
            if cwd_error:
                self._stop_watch_for_missing_cwd(watch, error_text=cwd_error)
                return
            try:
                result = await self._run_cycle(watch, timeout_seconds=cycle_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cwd_error = _missing_watch_cwd_error(watch)
                if cwd_error:
                    self._stop_watch_for_missing_cwd(watch, error_text=cwd_error)
                    return
                result = _CycleResult(
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    timed_out=False,
                )

            if not self._owns_service_instance():
                return

            if result.exit_code == 0:
                # Building the prompt is pure; the stamp and the hook it authorises are
                # committed together (HFR-269).
                if not self._commit_cycle_result(
                    watch,
                    exit_code=0,
                    error=None,
                    event_detected=True,
                    disable=watch.mode == "once",
                    prompt=_build_prompt(watch.message or watch.prefix, result.stdout),
                ):
                    return
                if watch.mode != "forever":
                    return
                continue

            if result.timed_out or result.exit_code == 124:
                error_text = "timed out"
                if watch.mode == "forever" and 124 in set(watch.retry_exit_codes):
                    # A retry authorises no hook: the watch keeps running, and there is
                    # nothing to tell the user yet.
                    if not self._commit_cycle_result(
                        watch, exit_code=124, error=error_text, disable=False
                    ):
                        return
                    await asyncio.sleep(watch.retry_delay_seconds)
                    continue
                self._commit_cycle_result(
                    watch,
                    exit_code=124,
                    error=error_text,
                    disable=True,
                    prefix=watch.message or watch.prefix,
                    body=_failure_hook_body(
                        watch,
                        exit_code=124,
                        error_text=f"Watch timed out after {int(cycle_timeout)} second(s).",
                    ),
                )
                return

            error_text = _squash_error(result.stderr) or f"watch command exited with status {result.exit_code}"
            if watch.mode == "forever" and result.exit_code in set(watch.retry_exit_codes):
                if not self._commit_cycle_result(
                    watch, exit_code=result.exit_code, error=error_text, disable=False
                ):
                    return
                await asyncio.sleep(watch.retry_delay_seconds)
                continue

            self._commit_cycle_result(
                watch,
                exit_code=result.exit_code,
                error=error_text,
                disable=True,
                prefix=watch.message or watch.prefix,
                body=_failure_hook_body(watch, exit_code=result.exit_code, error_text=error_text),
            )
            return

    async def _run_cycle(self, watch: ManagedWatch, *, timeout_seconds: float) -> _CycleResult:
        """Run one waiter cycle through the shared supervised-command runner.

        The runner owns the process mechanics; this method owns watch policy:
        the active-pid/identity registries, runtime-state writes, and the
        localization of worker errors.
        """

        spawn_cwd = _watch_spawn_cwd(watch)

        def _register_spawn(pid: int, identity: Optional[PersistedProcessIdentity]) -> None:
            self._active_pids[watch.id] = pid
            if identity is not None:
                self._active_process_identities[watch.id] = identity
            else:
                logger.warning(
                    "Unable to capture process identity for watch worker pid=%s watch_id=%s; "
                    "a future recovery will leave it untouched",
                    pid,
                    watch.id,
                )
            self._runtime_state_dirty = True
            self._write_runtime_state()

        try:
            result = await run_supervised_command(
                command=watch.command,
                shell_command=watch.shell_command,
                cwd=spawn_cwd,
                timeout_seconds=timeout_seconds,
                label=f"watch {watch.id}",
                on_spawn=_register_spawn,
                max_output_bytes=None,
            )
        except SupervisedCommandStartupError as exc:
            detail = self._localize_watch_worker_error(exc.detail)
            raise RuntimeError(
                detail or self._t("harness.watch.supervisorExitedDuringStartup")
            ) from None
        finally:
            self._active_pids.pop(watch.id, None)
            self._active_process_identities.pop(watch.id, None)
            self._runtime_state_dirty = True
            self._write_runtime_state()

        if result.timed_out:
            return _CycleResult(
                exit_code=result.exit_code,
                stdout="",
                stderr=result.stderr,
                timed_out=True,
            )

        return _CycleResult(
            exit_code=result.exit_code,
            stdout=result.stdout.strip(),
            stderr=self._localize_watch_worker_error(result.stderr.strip()),
            timed_out=False,
        )

    def _hook_request(
        self,
        watch: ManagedWatch,
        *,
        prompt: Optional[str] = None,
        prefix: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Optional[TaskExecutionRequest]:
        """The cycle's completion hook, BUILT and not yet durable.

        ``None`` when there is no prompt to deliver. Composing the request is pure;
        ``_commit_cycle_result`` decides where it becomes durable.
        """

        final_prompt = prompt or _build_prompt(prefix, body)
        if not final_prompt:
            return None
        return self.request_store.build_hook_send(
            session_key=watch.session_key,
            session_id=watch.session_id,
            post_to=watch.post_to,
            deliver_key=watch.deliver_key,
            prompt=final_prompt,
            agent_name=watch.agent_name,
            session_policy=watch.session_policy,
            run_type="watch",
            definition_id=watch.id,
            source_kind="watch",
            metadata=watch.metadata,
        )

    def _commit_cycle_result(
        self,
        watch: ManagedWatch,
        *,
        exit_code: Optional[int],
        error: Optional[str],
        event_detected: bool = False,
        disable: bool = False,
        prompt: Optional[str] = None,
        prefix: Optional[str] = None,
        body: Optional[str] = None,
    ) -> bool:
        """Record a cycle's outcome and queue the hook it authorises, as ONE decision.

        ``False`` means the guarded stamp was refused, nothing was written and no hook
        exists; the caller must stop the watch.

        HFR-267 made the guarded stamp run BEFORE the hook, which was necessary and not
        sufficient: TWO COMMITS ARE NOT ONE DECISION.

            self.store.mark_cycle_result(...)             # transaction 1, COMMITS
            self.request_store.enqueue_hook_send(...)      # transaction 2, COMMITS

        A ``/new`` reclaim or an archive from another connection can commit in the gap
        between those commits. The stamp is accepted -- it won its compare-and-set
        fairly, against the state that existed when it ran -- and the hook is queued
        afterwards anyway, into a session the teardown has deleted and under a
        definition it has paused or soft-deleted. Nothing is refused, because by the
        time the teardown lands there is nothing left to refuse. Reordering moved the
        window; it could not close it.

        And the ordering introduced the inverse: a failure between the two commits left
        a ``once``/terminal watch durably DISABLED with its completion hook lost, so the
        user is never told the watch finished.

        Both disappear when the stamp and the outbox row share a transaction (HFR-269).
        When the two stores do not share a database -- file-backed test and legacy
        configurations -- they cannot, and the fallback keeps HFR-267's order; the
        file-backed watch store has no compare-and-set for a teardown to outrun.
        """

        request = self._hook_request(watch, prompt=prompt, prefix=prefix, body=body)
        atomic = request is not None and _shared_run_ledger_backend(self.store, self.request_store) is not None
        queued_run = self.request_store.queued_run_payload(request) if atomic and request else None
        if not self._watch_store_call(
            watch.id,
            "mark_cycle_result",
            lambda: self.store.mark_cycle_result(
                watch.id,
                exit_code=exit_code,
                error=error,
                event_detected=event_detected,
                disable=disable,
                queued_run=queued_run,
            ),
            guarded=True,
        ):
            return False
        if request is not None and queued_run is None:
            self.request_store.enqueue(request)
        return True

    def _stop_watch_for_missing_cwd(self, watch: ManagedWatch, *, error_text: str) -> None:
        watch_label = watch.name or watch.id
        self._commit_cycle_result(
            watch,
            exit_code=1,
            error=error_text,
            disable=True,
            body=(
                f"Watch '{watch_label}' stopped because its working directory is no longer available.\n"
                f"Working directory: {watch.cwd}\n"
                "Update or recreate the watch with a valid cwd before monitoring continues."
            ),
        )


def _failure_hook_body(watch: ManagedWatch, *, exit_code: int, error_text: str) -> str:
    """The body of the hook that tells the user why a watch stopped failing."""

    watch_label = watch.name or watch.id
    if exit_code == 124:
        return (
            f"Watch '{watch_label}' stopped because the waiter timed out.\n"
            f"Check whether the timeout is too short or the waiter is blocked, then recreate the watch if monitoring should continue.\n"
            f"Details: {error_text}"
        )
    return (
        f"Watch '{watch_label}' stopped because the waiter exited with code {exit_code}.\n"
        f"Review the error below, fix the waiter or its dependencies, then recreate the watch if monitoring should continue.\n"
        f"Error: {error_text}"
    )


def _build_prompt(prefix: Optional[str], body: Optional[str]) -> str:
    parts = []
    if prefix:
        parts.append(prefix.strip())
    if body:
        body_text = body.strip()
        if body_text:
            parts.append(body_text)
    return "\n\n".join(parts).strip()


def _squash_error(text: str, *, limit: int = 240) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
