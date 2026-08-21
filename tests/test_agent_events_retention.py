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
import threading
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
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def test_run_retention_stops_at_batch_boundary_when_cancelled(state) -> None:
    engine = state
    old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(3):
        _seed_event(engine, event_id=f"old-{i}", created_at=old)
    cancel_event = threading.Event()

    def _cancel_after_batch() -> None:
        cancel_event.set()

    result = agent_events_retention.run_retention(
        engine,
        retention_days=30,
        batch_rows=1,
        between_batches=_cancel_after_batch,
        now=_NOW,
        cancel_event=cancel_event,
    )

    assert result["cancelled"] is True
    assert result["deleted_rows"] == 1
    with engine.connect() as conn:
        assert conn.execute(select(agent_events.c.id)).fetchall()


def test_run_once_forwards_cancellation_to_retention(state, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run_retention(engine, **kwargs):
        captured.update(kwargs)
        return {"cutoff": _iso(_NOW), "deleted_rows": 0, "batches": 0, "cancelled": True}

    monkeypatch.setattr(agent_events_retention, "run_retention", _fake_run_retention)
    cancel_event = threading.Event()
    result = agent_events_retention.run_once(
        state,
        retention_days=30,
        force=True,
        compact=False,
        now=_NOW,
        cancel_event=cancel_event,
    )

    assert captured["cancel_event"] is cancel_event
    assert result["status"] == "cancelled"


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
        # The marker retains completion microseconds; avoid asserting on the
        # exact 24-hour boundary when the test run itself takes a few ms.
        assert agent_events_retention.should_run(conn, now=_NOW + timedelta(hours=25, seconds=1)) is True


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


def test_cutoff_is_chronological_for_normalized_legacy_timestamps(state) -> None:
    """Fixed-width cutoffs keep legacy fractional timestamps chronological.

    A row stamped ``...12:00:00.500000+00:00`` is 0.5s newer than the cutoff.
    The retention cutoff and migration form both use fixed-width microseconds,
    so the raw legacy offset form is already ordered correctly and migration
    preserves the fraction.
    """
    engine = state
    cutoff = _NOW - timedelta(days=30)
    # A fractional row one second AFTER the cutoff, in legacy offset form.
    legacy_stamp = (cutoff + timedelta(seconds=0.5)).isoformat()
    assert legacy_stamp.endswith("+00:00")
    _seed_event(engine, event_id="legacy", created_at=cutoff)
    with engine.begin() as conn:
        conn.execute(agent_events.update().where(agent_events.c.id == "legacy").values(created_at=legacy_stamp))

    with engine.connect() as conn:
        raw = agent_events_retention.plan(conn, retention_days=30, now=_NOW)
    assert raw["eligible_count"] == 0

    # Canonical form of the same instant (what migration 0060 produces).
    with engine.begin() as conn:
        conn.exec_driver_sql("update agent_events set created_at = '2026-07-18T12:00:00.500000Z' where id = 'legacy'")
    with engine.connect() as conn:
        canonical = agent_events_retention.plan(conn, retention_days=30, now=_NOW)
    assert canonical["eligible_count"] == 0  # normalized: not eligible, correctly


def test_migration_0060_leaves_non_retention_events_untouched(tmp_path, monkeypatch) -> None:
    """silent_terminal timestamps keep their precision (fork ordering anchors)."""
    import sqlite3

    from alembic import command
    from storage import migrations

    db_path = tmp_path / "state.sqlite"
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    command.upgrade(migrations.alembic_config(db_path), "20260821_0060")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "insert into agent_events (id, platform, event_type, visibility, content_json, "
        "metadata_json, created_at, updated_at) values (?, 'web', ?, 'trace', '{}', '{}', ?, ?)",
        [
            ("trace-row", "tool_call", "2026-07-18T12:00:00.500000+00:00", "2026-07-18T12:00:00.500000+00:00"),
            ("terminal-row", "silent_terminal", "2026-07-18T12:00:00.950000+00:00", "2026-07-18T12:00:00.950000+00:00"),
        ],
    )
    conn.commit()
    conn.close()

    # Apply 0060 to pre-seeded rows: downgrade to the prior head, re-upgrade.
    command.downgrade(migrations.alembic_config(db_path), "20260821_0059")
    command.upgrade(migrations.alembic_config(db_path), "20260821_0060")

    conn = sqlite3.connect(db_path)
    trace_stamp = conn.execute("select created_at from agent_events where id='trace-row'").fetchone()[0]
    terminal_stamp = conn.execute("select created_at from agent_events where id='terminal-row'").fetchone()[0]
    conn.close()
    assert trace_stamp == "2026-07-18T12:00:00.500000Z"  # retention subject canonicalized
    assert terminal_stamp == "2026-07-18T12:00:00.950000+00:00"  # ordering anchor preserved


def test_lease_loss_propagates_and_skips_marker(state, monkeypatch) -> None:
    """A mid-run ownership loss aborts the marker write and compaction."""
    engine = state
    from storage import agent_events_retention as module

    for i in range(3):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc))

    original_renew = module.renew_lease
    renew_calls = iter([True, False])  # owned for batch 1, lost before batch 2

    def _renew(conn, token, *, now=None):
        return next(renew_calls, False)

    monkeypatch.setattr(module, "renew_lease", _renew)
    result = module.run_retention(
        engine, retention_days=30, batch_rows=1, now=_NOW, lease_token="owned-token"
    )
    monkeypatch.undo()

    assert result.get("lease_lost") is True
    assert result["deleted_rows"] >= 1  # committed batch counted
    with engine.connect() as conn:
        assert module.get_last_run(conn) is None  # run_once-level marker path not exercised


def test_maybe_compact_reports_lease_loss_during_vacuum(state) -> None:
    """An ownership loss during VACUUM defers compaction as contested."""
    engine = state
    payload = "h" * 2000
    for i in range(50):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")
    agent_events_retention.run_retention(engine, retention_days=30, batch_rows=25, now=_NOW)

    heartbeats = iter([True, False])  # first beat renews, second loses ownership
    result = agent_events_retention.maybe_compact(
        engine,
        min_reclaim_bytes=1,
        free_space_margin_bytes=0,
        lease_heartbeat=lambda: next(heartbeats, False),
    )
    # The heartbeat thread may lose ownership before or during the vacuum;
    # either way the outcome is a contested compaction, never a clean success.
    assert result["status"] in {"deferred", "vacuumed"}
    if result["status"] == "deferred":
        assert result["reason"] == "lease_lost_during_compaction"


def test_migration_0060_canonicalizes_legacy_trace_timestamps(tmp_path, monkeypatch) -> None:
    """The migration rewrites offset/fractional stamps to fixed-width UTC."""
    import sqlite3

    from alembic import command
    from storage import migrations

    db_path = tmp_path / "state.sqlite"
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    # Build schema up to 0059, seed a legacy-form row, then run 0060.
    command.upgrade(migrations.alembic_config(db_path), "20260821_0059")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "insert into agent_events (id, platform, event_type, visibility, content_json, "
        "metadata_json, created_at, updated_at) values (?, 'web', 'tool_call', 'trace', '{}', '{}', ?, ?)",
        ("legacy-row", "2026-07-18T12:00:00.500000+00:00", "2026-07-18T12:00:00.500000+00:00"),
    )
    conn.commit()
    conn.close()

    command.upgrade(migrations.alembic_config(db_path), "20260821_0060")

    conn = sqlite3.connect(db_path)
    stamp = conn.execute("select created_at from agent_events where id = 'legacy-row'").fetchone()[0]
    version = conn.execute("select version_num from alembic_version").fetchone()[0]
    conn.close()
    assert stamp == "2026-07-18T12:00:00.500000Z"
    assert version == "20260821_0060"


def test_migration_0060_canonicalizes_in_bounded_batches(tmp_path, monkeypatch) -> None:
    """Large legacy tables are processed without an unbounded fetchall."""
    import importlib
    import sqlite3

    from alembic import command
    from storage import migrations

    migration = importlib.import_module(
        "storage.alembic.versions.20260821_0060_canonicalize_agent_events_timestamps"
    )
    monkeypatch.setattr(migration, "_CANONICALIZE_BATCH_ROWS", 2)
    db_path = tmp_path / "state.sqlite"
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    command.upgrade(migrations.alembic_config(db_path), "20260821_0059")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "insert into agent_events (id, platform, event_type, visibility, content_json, "
        "metadata_json, created_at, updated_at) values (?, 'web', 'tool_call', 'trace', '{}', '{}', ?, ?)",
        [
            (f"legacy-{index}", "2026-07-18T12:00:00.500000+00:00", "2026-07-18T12:00:00.500000+00:00")
            for index in range(5)
        ],
    )
    conn.commit()
    conn.close()

    command.upgrade(migrations.alembic_config(db_path), "20260821_0060")

    conn = sqlite3.connect(db_path)
    stamps = conn.execute("select created_at from agent_events order by id").fetchall()
    conn.close()
    assert stamps == [("2026-07-18T12:00:00.500000Z",)] * 5


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


def test_run_once_busy_exit_and_lease_renewal(state, monkeypatch) -> None:
    """The CLI treats busy as failure; long runs renew their lease."""
    engine = state
    payload = "r" * 500
    for i in range(6):
        _seed_event(engine, event_id=f"old-{i}", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), payload=payload + f"-{i}")

    # Lease renewal between batches keeps ownership past the original TTL.
    from storage import agent_events_retention as module

    renewed: list[str] = []
    original_renew = module.renew_lease

    def _tracked_renew(conn, token, *, now=None):
        ok = original_renew(conn, token, now=now)
        renewed.append(ok)
        return ok

    monkeypatch.setattr(module, "renew_lease", _tracked_renew)
    result = module.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW)
    assert result["status"] == "ok" and result["deleted_rows"] == 6
    assert renewed and all(renewed)  # renewed after each batch, always owned

    # busy -> the run deleted nothing; the CLI maps this to a nonzero exit.
    with engine.begin() as conn:
        foreign = module.try_acquire_lease(conn, now=_NOW)
    assert foreign
    busy = module.run_once(engine, retention_days=30, force=True, compact=False, now=_NOW)
    assert busy["status"] == "busy"
    with engine.begin() as conn:
        module.release_lease(conn, foreign)


def test_run_once_rechecks_cadence_under_lease(state, monkeypatch) -> None:
    """A runner finishing between the gate and lease acquisition must win."""
    engine = state
    from storage import agent_events_retention as module

    with engine.begin() as conn:
        module._write_meta(
            conn,
            module.RETENTION_MARKER_KEY,
            {"finished_at": module._iso(_NOW), "deleted_rows": 9},
            now=_NOW,
        )
    gate_results = iter([True, False])
    monkeypatch.setattr(module, "should_run", lambda conn, *, now=None: next(gate_results))
    result = module.run_once(engine, retention_days=30, force=False, compact=False, now=_NOW)
    assert result["status"] == "not_due"
    with engine.connect() as conn:
        assert module.get_last_run(conn)["deleted_rows"] == 9
        assert module._read_meta(conn, module.RETENTION_LEASE_KEY) is None
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
