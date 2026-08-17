"""Bounded retention for internal agent trace events (avibe#1506 lane B).

This module is the single owner of ``agent_events`` deletion policy. The
eligibility property, defined once here and shared by planning, execution,
and tests:

    Only rows with ``event_type='tool_call'`` AND ``visibility='trace'`` AND
    ``created_at`` strictly older than the retention cutoff are ever removable.

Everything else — user messages (a separate table), message deliveries,
session/run records, Vault audit data, non-trace events, and newer traces —
is preserved by construction: no code path in this module deletes a row
outside the predicate, regardless of caller.

Operational shape:

* Deletes run in small batches, each in its own transaction, so concurrent
  writers (WAL mode) are never blocked for an unbounded interval.
* A ``state_meta`` marker records the last successful run and throttles the
  automatic cadence to at most once per day; a lease row serializes
  concurrent runners (controller task vs CLI) across processes.
* Physical compaction (``VACUUM``) only runs after a free-space and
  writer-safety preflight, and only when reclaimable space justifies it.
  When compaction is unsafe it is reported as deferred — never started on a
  nearly-full volume.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import Engine, LargeBinary, delete, func, select, cast
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.engine import Connection

from storage.models import agent_events, state_meta

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MIN_RUN_INTERVAL_SECONDS = 24 * 3600
DELETE_BATCH_ROWS = 1000
LEASE_TTL_SECONDS = 3600
# Compaction only when the free list alone would return at least this much,
# so a daily run does not rewrite a large database for pocket change.
VACUUM_MIN_RECLAIM_BYTES = 64 * 1024 * 1024
# VACUUM writes a full temp copy and the WAL rewrite can coexist with it, so
# the preflight must reserve room for both copies plus margin.
VACUUM_FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024

RETENTION_MARKER_KEY = "agent_events_trace_retention.last_run"
RETENTION_LEASE_KEY = "agent_events_trace_retention.lease"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_retention_days(retention_days: Any) -> int:
    try:
        days = int(retention_days)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(MIN_RETENTION_DAYS, days)


def cutoff_iso(retention_days: int, *, now: Optional[datetime] = None) -> str:
    """Rows strictly older than this instant are eligible."""
    moment = now or _utc_now()
    return _iso(moment - timedelta(days=normalize_retention_days(retention_days)))


def eligible_filter(cutoff: str):
    """The single eligibility predicate shared by plan and delete.

    Keep byte-for-byte compatible with the partial index
    ``ix_agent_events_trace_retention`` (storage/models.py and the alembic
    migration) so the scan stays bounded.
    """
    return (
        (agent_events.c.event_type == "tool_call")
        & (agent_events.c.visibility == "trace")
        & (agent_events.c.created_at < cutoff)
    )


def plan(conn: Connection, *, retention_days: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Dry-run report: how many eligible rows exist and their logical payload size.

    Sizes are measured in UTF-8 bytes (``CAST(.. AS BLOB)``); SQLite's plain
    ``length(TEXT)`` counts Unicode characters and underreports CJK payloads
    several-fold.
    """
    cutoff = cutoff_iso(retention_days, now=now)

    def _blob_bytes(column) -> Any:
        return func.length(cast(func.coalesce(column, ""), LargeBinary))

    logical_bytes = func.sum(
        _blob_bytes(agent_events.c.content_text)
        + _blob_bytes(agent_events.c.content_json)
        + _blob_bytes(agent_events.c.metadata_json)
    )
    row = conn.execute(
        select(func.count(), logical_bytes).where(eligible_filter(cutoff))
    ).one()
    return {
        "retention_days": normalize_retention_days(retention_days),
        "cutoff": cutoff,
        "eligible_count": int(row[0] or 0),
        "eligible_logical_bytes": int(row[1] or 0),
    }


def _delete_batch(conn: Connection, cutoff: str, batch_rows: int) -> int:
    result = conn.execute(
        delete(agent_events).where(
            agent_events.c.id.in_(
                select(agent_events.c.id)
                .where(eligible_filter(cutoff))
                .order_by(agent_events.c.created_at, agent_events.c.id)
                .limit(batch_rows)
            )
        )
    )
    return int(result.rowcount or 0)


def run_retention(
    engine: Engine,
    *,
    retention_days: int,
    batch_rows: int = DELETE_BATCH_ROWS,
    max_batches: Optional[int] = None,
    between_batches: Optional[Callable[[], None]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Delete eligible rows in bounded batches, one transaction per batch.

    ``between_batches`` runs after each committed batch (test hook for
    simulating concurrent writers between transactions).
    """
    cutoff = cutoff_iso(retention_days, now=now)
    deleted_rows = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        with engine.begin() as conn:
            removed = _delete_batch(conn, cutoff, batch_rows)
        batches += 1
        if removed == 0:
            break
        deleted_rows += removed
        if between_batches is not None:
            between_batches()
    return {"cutoff": cutoff, "deleted_rows": deleted_rows, "batches": batches}


@dataclass
class _MetaPayload:
    payload: dict[str, Any]


def _read_meta(conn: Connection, key: str) -> Optional[dict[str, Any]]:
    value = conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == key)).scalar_one_or_none()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_meta(conn: Connection, key: str, payload: dict[str, Any], *, now: datetime) -> None:
    stamp = _iso(now)
    conn.execute(state_meta.delete().where(state_meta.c.key == key))
    conn.execute(
        state_meta.insert().values(key=key, value_json=json.dumps(payload, sort_keys=True), updated_at=stamp)
    )


def get_last_run(conn: Connection) -> Optional[dict[str, Any]]:
    return _read_meta(conn, RETENTION_MARKER_KEY)


def should_run(conn: Connection, *, now: Optional[datetime] = None) -> bool:
    last = get_last_run(conn)
    if not last:
        return True
    finished = _parse_iso(str(last.get("finished_at") or ""))
    if finished is None:
        return True
    return (now or _utc_now()) - finished >= timedelta(seconds=MIN_RUN_INTERVAL_SECONDS)


def try_acquire_lease(conn: Connection, *, now: Optional[datetime] = None) -> Optional[str]:
    """Best-effort cross-process lease. Returns the lease token or ``None``.

    Takes the database writer lock up front (``BEGIN IMMEDIATE`` via a
    prelude statement) so the read-decide-write sequence below is atomic
    against other contenders: a deferred transaction could let two processes
    both see an absent lease, then serialize in an order where the loser's
    unconditional delete removes the winner's still-valid lease.
    """
    moment = now or _utc_now()
    token = uuid.uuid4().hex
    # BEGIN IMMEDIATE equivalent: force the write lock before reading.
    conn.exec_driver_sql("UPDATE state_meta SET updated_at = updated_at WHERE key = ?", (RETENTION_LEASE_KEY,))
    existing = _read_meta(conn, RETENTION_LEASE_KEY)
    if existing:
        expires = _parse_iso(str(existing.get("expires_at") or ""))
        if expires is not None and expires > moment and existing.get("token") != token:
            return None
    conn.execute(state_meta.delete().where(state_meta.c.key == RETENTION_LEASE_KEY))
    try:
        conn.execute(
            state_meta.insert().values(
                key=RETENTION_LEASE_KEY,
                value_json=json.dumps(
                    {"token": token, "expires_at": _iso(moment + timedelta(seconds=LEASE_TTL_SECONDS))},
                    sort_keys=True,
                ),
                updated_at=_iso(moment),
            )
        )
    except IntegrityError:
        return None
    return token


def release_lease(conn: Connection, token: Optional[str] = None) -> None:
    """Release the lease, deleting only the row this runner owns.

    A long run can outlive the lease TTL; deleting unconditionally would then
    remove a *newer* runner's lease. With ``token``, the row is removed only
    while it still carries that token.
    """
    if token is None:
        conn.execute(state_meta.delete().where(state_meta.c.key == RETENTION_LEASE_KEY))
        return
    stored = _read_meta(conn, RETENTION_LEASE_KEY)
    if stored and stored.get("token") == token:
        conn.execute(state_meta.delete().where(state_meta.c.key == RETENTION_LEASE_KEY))


def compaction_status(conn: Connection) -> dict[str, Any]:
    page_size = int(conn.exec_driver_sql("PRAGMA page_size").scalar() or 0)
    page_count = int(conn.exec_driver_sql("PRAGMA page_count").scalar() or 0)
    freelist_count = int(conn.exec_driver_sql("PRAGMA freelist_count").scalar() or 0)
    return {
        "page_size": page_size,
        "database_bytes": page_size * page_count,
        "reclaimable_bytes": page_size * freelist_count,
    }


def _wal_size(db_path: Path) -> int:
    wal = db_path.parent / f"{db_path.name}-wal"
    try:
        return wal.stat().st_size
    except OSError:
        return 0


def maybe_compact(
    engine: Engine,
    *,
    db_path: Optional[Path] = None,
    min_reclaim_bytes: int = VACUUM_MIN_RECLAIM_BYTES,
    free_space_margin_bytes: int = VACUUM_FREE_SPACE_MARGIN_BYTES,
    wal_checkpoint: Optional[Callable[[Engine], tuple]] = None,
    disk_usage: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Physically reclaim freed pages, only when it is safe to do so.

    Order of gates: reclaimable-below-threshold (skip) -> checkpoint WAL
    (defer if busy) -> free-space preflight (defer if the volume cannot hold
    the rewrite) -> VACUUM. A deferred compaction is a reported state, never
    an error, and never worsens a low-disk condition. ``wal_checkpoint`` and
    ``disk_usage`` are injectable for tests.
    """
    if db_path is None:
        db_path = Path(engine.url.database or "")
    with engine.connect() as conn:
        status = compaction_status(conn)
    if status["reclaimable_bytes"] < min_reclaim_bytes:
        return {"status": "skipped", "reason": "below_threshold", **status}

    _checkpoint = wal_checkpoint or _default_wal_checkpoint
    try:
        checkpoint = _checkpoint(engine)
    except OperationalError:
        return {"status": "deferred", "reason": "checkpoint_busy", **status}
    checkpoint_busy = bool(checkpoint and int(checkpoint[0]) != 0)
    if checkpoint_busy:
        return {"status": "deferred", "reason": "checkpoint_busy", **status}

    database_bytes = max(status["database_bytes"], 1)
    _disk_usage = disk_usage or shutil.disk_usage
    try:
        free_bytes = _disk_usage(str(db_path.parent)).free
    except OSError:
        return {"status": "deferred", "reason": "free_space_unknown", **status}
    required = 2 * database_bytes + _wal_size(db_path) + free_space_margin_bytes
    if free_bytes < required:
        return {"status": "deferred", "reason": "insufficient_free_space", **status,
                "free_bytes": free_bytes, "required_bytes": required}

    before = db_path.stat().st_size if db_path.exists() else 0
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
        # In WAL mode the compacted database lands in the -wal file; a
        # post-VACUUM checkpoint is what actually returns bytes to the OS.
        # A busy checkpoint here means the space is still trapped in the WAL:
        # report it as deferred rather than a completed compaction.
        post_checkpoint = _checkpoint(engine)
    except OperationalError as exc:
        return {"status": "deferred", "reason": f"vacuum_failed: {exc.__class__.__name__}", **status}
    if post_checkpoint and int(post_checkpoint[0]) != 0:
        return {"status": "deferred", "reason": "post_checkpoint_busy", **status}
    after = db_path.stat().st_size if db_path.exists() else 0
    return {
        "status": "vacuumed",
        "database_bytes_before": before,
        "database_bytes_after": after,
        "reclaimed_bytes": max(before - after, 0),
        **status,
    }


def _default_wal_checkpoint(engine: Engine) -> tuple:
    with engine.connect() as conn:
        return conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


def run_once(
    engine: Engine,
    *,
    retention_days: int,
    force: bool = False,
    compact: bool = True,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One full maintenance pass: gate -> lease -> batched delete -> marker -> compact."""
    moment = now or _utc_now()
    with engine.connect() as conn:
        last_run = get_last_run(conn)
        if not force and not should_run(conn, now=moment):
            return {"status": "not_due", "last_run": last_run}

    token: Optional[str] = None
    with engine.begin() as conn:
        token = try_acquire_lease(conn, now=moment)
    if token is None:
        return {"status": "busy", "last_run": last_run}

    # Recheck the cadence under the lease: a runner that finished between the
    # gate above and this acquisition already did today's pass. Without this,
    # overlapping controller startups would double-run deletion + compaction.
    if not force:
        with engine.connect() as conn:
            if not should_run(conn, now=moment):
                with engine.begin() as write_conn:
                    release_lease(write_conn, token)
                return {"status": "not_due", "last_run": get_last_run(conn)}

    started = time.monotonic()
    try:
        result = run_retention(engine, retention_days=retention_days, now=moment)
        finished = moment + timedelta(seconds=round(time.monotonic() - started, 3))
        with engine.begin() as conn:
            _write_meta(
                conn,
                RETENTION_MARKER_KEY,
                {
                    "finished_at": _iso(finished),
                    "deleted_rows": result["deleted_rows"],
                    "retention_days": normalize_retention_days(retention_days),
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                now=finished,
            )
            result_marker = get_last_run(conn)
        compaction: dict[str, Any] = {"status": "not_attempted"}
        if compact:
            compaction = maybe_compact(engine, db_path=db_path)
        return {
            "status": "ok",
            "deleted_rows": result["deleted_rows"],
            "batches": result["batches"],
            "cutoff": result["cutoff"],
            "duration_seconds": round(time.monotonic() - started, 3),
            "last_run": result_marker,
            "compaction": compaction,
        }
    finally:
        with engine.begin() as conn:
            release_lease(conn, token)


def retention_status(engine: Engine, *, retention_days: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Read-only status: candidates, last successful run, compaction outlook."""
    with engine.connect() as conn:
        return {
            "plan": plan(conn, retention_days=retention_days, now=now),
            "last_run": get_last_run(conn),
            "compaction": compaction_status(conn),
        }
