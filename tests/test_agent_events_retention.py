"""Bounded retention for internal agent trace events (avibe#1506 lane B).

Property under test — the retention service is the single owner of
``agent_events`` deletion, and only rows matching
``event_type='tool_call' AND visibility='trace' AND created_at < cutoff``
are ever removable. Messages, deliveries, non-trace events, and newer
traces are preserved by construction, including rows written concurrently
between delete batches.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import agent_events_retention, agent_events_service, messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, messages


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    yield engine
    engine.dispose()


_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_event(
    engine,
    *,
    event_id: str,
    event_type: str = "tool_call",
    visibility: str = "trace",
    created_at: datetime,
    payload: str | None = None,
) -> None:
    text = payload if payload is not None else "x" * 100
    if payload is None:
        text = f"{event_id}:x" * 40  # unique per event so the id rewrite matches one row
    with engine.begin() as conn:
        agent_events_service.append(
            conn,
            scope_id=None,
            session_id=None,
            platform="web",
            event_type=event_type,
            text=text,
            visibility=visibility,
        )
    # append() stamps now; rewrite id/created_at for deterministic age control.
    with engine.begin() as conn:
        updated = conn.execute(
            agent_events.update()
            .where(agent_events.c.content_text == text)
            .values(created_at=_iso(created_at), id=event_id)
        )
        assert updated.rowcount == 1


def test_run_deletes_only_old_tool_call_traces(state) -> None:
    engine = state
    old_trace = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _seed_event(engine, event_id="old-trace", created_at=old_trace)
    _seed_event(engine, event_id="boundary", created_at=_NOW - timedelta(days=30))  # exactly at cutoff: kept
    _seed_event(engine, event_id="new-trace", created_at=_NOW - timedelta(days=5))
    _seed_event(engine, event_id="old-visible", event_type="tool_call", visibility="user", created_at=old_trace)
    _seed_event(engine, event_id="old-other-type", event_type="session_state", visibility="trace", created_at=old_trace)

    result = agent_events_retention.run_retention(engine, retention_days=30, now=_NOW)

    assert result["deleted_rows"] == 1
    with engine.connect() as conn:
        remaining = {row[0] for row in conn.execute(select(agent_events.c.id))}
    assert remaining == {"boundary", "new-trace", "old-visible", "old-other-type"}


def test_messages_are_never_touched(state) -> None:
    engine = state
    _seed_event(engine, event_id="old-trace", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=None,
            session_id=None,
            platform="web",
            author="user",
            message_type="text",
            text="please keep me",
        )

    agent_events_retention.run_retention(engine, retention_days=30, now=_NOW)

    with engine.connect() as conn:
        assert conn.execute(select(messages.c.content_text)).fetchall() == [("please keep me",)]
        assert conn.execute(select(agent_events.c.id)).fetchall() == []


def _append_event_now(engine, *, event_id: str) -> None:
    """Insert a fresh trace row stamped 'now' without any rewrite."""
    text = f"{event_id}:fresh:{uuid.uuid4().hex}"
    with engine.begin() as conn:
        agent_events_service.append(
            conn,
            scope_id=None,
            session_id=None,
            platform="web",
            event_type="tool_call",
            text=text,
            visibility="trace",
        )
    with engine.begin() as conn:
        conn.execute(agent_events.update().where(agent_events.c.content_text == text).values(id=event_id))


def test_concurrent_appends_between_batches_survive(state) -> None:
    engine = state
    old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(5):
        _seed_event(engine, event_id=f"old-{i}", created_at=old)

    def _append_new_row() -> None:
        _append_event_now(engine, event_id=f"concurrent-{uuid.uuid4().hex[:8]}")

    result = agent_events_retention.run_retention(
        engine,
        retention_days=30,
        batch_rows=2,
        between_batches=_append_new_row,
        now=_NOW,
    )

    assert result["deleted_rows"] == 5
    with engine.connect() as conn:
        remaining = {row[0] for row in conn.execute(select(agent_events.c.id))}
    assert all(row.startswith("concurrent-") for row in remaining)
    assert len(remaining) == 3  # one fresh row per committed batch gap


def test_plan_reports_counts_and_logical_bytes(state) -> None:
    engine = state
    payload = "y" * 50
    _seed_event(engine, event_id="old-trace", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + "old")
    _seed_event(engine, event_id="new-trace", created_at=_NOW, payload=payload + "new")

    with engine.connect() as conn:
        report = agent_events_retention.plan(conn, retention_days=30, now=_NOW)

    assert report["eligible_count"] == 1
    assert report["eligible_logical_bytes"] >= len(payload)
    assert report["cutoff"] == _iso(_NOW - timedelta(days=30))


def test_run_once_is_idempotent_and_records_marker(state) -> None:
    engine = state
    _seed_event(engine, event_id="old-trace", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))

    first = agent_events_retention.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW)
    second = agent_events_retention.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW + timedelta(hours=1))

    assert first["status"] == "ok" and first["deleted_rows"] == 1
    assert second["status"] == "ok" and second["deleted_rows"] == 0
    marker = first["last_run"]
    assert marker and marker["deleted_rows"] == 1 and marker["finished_at"]

    # Cadence gate: without force, a same-day follow-up is not due.
    with engine.connect() as conn:
        assert agent_events_retention.should_run(conn, now=_NOW + timedelta(hours=2)) is False
        assert agent_events_retention.should_run(conn, now=_NOW + timedelta(hours=25)) is True


def test_lease_blocks_concurrent_run_once(state) -> None:
    engine = state
    with engine.begin() as conn:
        token = agent_events_retention.try_acquire_lease(conn, now=_NOW)
    assert token

    busy = agent_events_retention.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW)
    assert busy["status"] == "busy"

    with engine.begin() as conn:
        agent_events_retention.release_lease(conn)
    ok = agent_events_retention.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW)
    assert ok["status"] == "ok"


def test_maybe_compact_defers_on_low_disk(state, monkeypatch) -> None:
    engine = state
    payload = "w" * 2000
    for i in range(50):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")
    agent_events_retention.run_retention(engine, retention_days=30, batch_rows=25, now=_NOW)

    class _Tiny:
        free = 0
        total = 1
        used = 1

    result = agent_events_retention.maybe_compact(
        engine,
        min_reclaim_bytes=1,
        free_space_margin_bytes=64 * 1024 * 1024,
        disk_usage=lambda _path: _Tiny(),
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "insufficient_free_space"


def test_maybe_compact_vacuums_when_safe(state) -> None:
    engine = state
    payload = "z" * 2000
    for i in range(200):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")
    db_path = Path(engine.url.database)
    agent_events_retention.run_retention(engine, retention_days=30, batch_rows=50, now=_NOW)

    result = agent_events_retention.maybe_compact(engine, min_reclaim_bytes=1, free_space_margin_bytes=0)

    assert result["status"] == "vacuumed"
    assert result["reclaimed_bytes"] > 0
    assert db_path.stat().st_size < result["database_bytes_before"] + 1


def test_retention_days_clamp_and_cutoff(state) -> None:
    assert agent_events_retention.normalize_retention_days(0) == 1
    assert agent_events_retention.normalize_retention_days(-5) == 1
    assert agent_events_retention.normalize_retention_days("7") == 7
    assert agent_events_retention.cutoff_iso(30, now=_NOW) == _iso(_NOW - timedelta(days=30))


def test_plan_measures_utf8_bytes_not_characters(state) -> None:
    engine = state
    chinese = "测" * 100  # 300 UTF-8 bytes, 100 characters
    _seed_event(engine, event_id="cjk", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=chinese)
    with engine.connect() as conn:
        report = agent_events_retention.plan(conn, retention_days=30, now=_NOW)
    assert report["eligible_logical_bytes"] >= 300


def test_release_lease_removes_only_the_owned_token(state) -> None:
    engine = state
    with engine.begin() as conn:
        token_a = agent_events_retention.try_acquire_lease(conn, now=_NOW)
    # Simulate the lease expiring and a second runner taking over.
    with engine.begin() as conn:
        agent_events_retention.release_lease(conn)  # clear
        token_b = agent_events_retention.try_acquire_lease(conn, now=_NOW)
    with engine.begin() as conn:
        agent_events_retention.release_lease(conn, token_a)  # stale token: must not delete B's lease
        stored = agent_events_retention._read_meta(conn, agent_events_retention.RETENTION_LEASE_KEY)
        assert stored is not None and stored.get("token") == token_b
        agent_events_retention.release_lease(conn, token_b)
        assert agent_events_retention._read_meta(conn, agent_events_retention.RETENTION_LEASE_KEY) is None


def test_run_once_rechecks_cadence_under_lease(state, monkeypatch) -> None:
    """A runner finishing between the gate and lease acquisition must win."""
    engine = state
    from storage import agent_events_retention as module

    # Marker absent at the gate, present by the time the lease is held —
    # exactly the overlapping-startup race.
    with engine.begin() as conn:
        module._write_meta(
            conn,
            module.RETENTION_MARKER_KEY,
            {"finished_at": module._iso(_NOW), "deleted_rows": 9},
            now=_NOW,
        )
    gate_results = iter([True, False])  # gate: due; post-lease recheck: not due
    monkeypatch.setattr(module, "should_run", lambda conn, *, now=None: next(gate_results))
    result = module.run_once(engine, retention_days=30, force=False, compact=False, now=_NOW)
    assert result["status"] == "not_due"
    with engine.connect() as conn:
        assert module.get_last_run(conn)["deleted_rows"] == 9  # first runner's marker untouched
        assert module._read_meta(conn, module.RETENTION_LEASE_KEY) is None  # lease released


def test_maybe_compact_reserves_space_for_both_copies(state) -> None:
    """Free space between one and two database sizes must defer."""
    engine = state
    payload = "v" * 2000
    for i in range(50):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")
    agent_events_retention.run_retention(engine, retention_days=30, batch_rows=25, now=_NOW)

    class _Tight:
        def __init__(self, free: int) -> None:
            self.free = free

    with engine.connect() as conn:
        db_bytes = agent_events_retention.compaction_status(conn)["database_bytes"]
    result = agent_events_retention.maybe_compact(
        engine,
        min_reclaim_bytes=1,
        free_space_margin_bytes=0,
        disk_usage=lambda _p: _Tight(free=db_bytes + 1),  # old check would pass
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "insufficient_free_space"


def test_maybe_compact_defers_when_post_vacuum_checkpoint_busy(state) -> None:
    engine = state
    payload = "u" * 2000
    for i in range(50):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")
    agent_events_retention.run_retention(engine, retention_days=30, batch_rows=25, now=_NOW)
    checkpoints = iter([(0, 0, 0), (1, 2, 2)])  # preflight ok, post-VACUUM busy
    result = agent_events_retention.maybe_compact(
        engine,
        min_reclaim_bytes=1,
        free_space_margin_bytes=0,
        wal_checkpoint=lambda _engine: next(checkpoints),
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "post_checkpoint_busy"
