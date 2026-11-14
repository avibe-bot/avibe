"""Coordinated writer boundary: real commits, legacy coexistence and cleanup.

The proof literal is patched for source fixtures only; these tests are not a
substitute for the separately exercised published predecessor wheel.
"""
from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest
from sqlalchemy import event as sa_event, select

from storage import compatibility, migrations, skill_observability, vault_service
from storage.db import create_sqlite_engine
from storage.models import agent_events, agent_sessions, messages, session_cache_state, skill_catalogs, state_meta
from storage.models import vault_audit, vault_audit_access_snapshots
from storage.skill_catalogs import read_content
from storage.vault_snapshots import HIGH_WATER_KEY
from tests.test_session_cache_state import _record, _restore
from tests.test_skill_catalogs import catalog
from tests.test_skill_observability import NOW
from tests.test_transcript_store_contract import SESSION, _SQLite, _session, _started
from tests.test_vault_audit_snapshots import _member, _secret_snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "writers.sqlite"
    migrations.run_migrations(path)
    engine = create_sqlite_engine(path)
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 3)
    yield engine
    engine.dispose()


@pytest.fixture
async def cache_backend(monkeypatch):
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 3)
    backend = _SQLite(adapter=False)
    await _started(backend)
    yield backend
    backend.close()


@pytest.mark.parametrize("proof,epoch,expected", [(None, 3, False), (2, 3, False), (3, 2, False), (3, 3, True)])
def test_shared_gate_requires_both_proof_and_boundary(db, monkeypatch, proof, epoch, expected):
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", proof)
    with db.begin() as conn:
        conn.execute(state_meta.update().where(state_meta.c.key == "compatibility_epoch").values(value_json=str(epoch)))
        assert compatibility.permits_compact_writes(conn) is expected
        snapshot = _secret_snapshot(conn)
        vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=snapshot)
        body = json.loads(conn.execute(select(vault_audit.c.delivery).where(vault_audit.c.event == "proxied")).scalar_one())
        assert ("ref" in body["_resource_access_snapshot"]) is expected
        observation = catalog()
        skill_observability.record(conn, observation, enabled=lambda: True, now=NOW)
        body = json.loads(conn.execute(select(agent_events.c.content_json)).scalar_one())
        assert ("entries_ref" in body) is expected
        assert compatibility.permits_tool_trace_suppression(conn) is (proof in (2, 3) and (epoch == 2 or proof == 3))


async def test_cache_response_and_audits_compact_without_losing_routes(cache_backend):
    backend = cache_backend
    _, message, first = _record(now=100)
    await backend.store.append_response(SESSION, message, final=True, cache=first)
    with backend.engine.connect() as conn:
        response = json.loads(conn.execute(select(messages.c.content_json).where(messages.c.author == "agent")).scalar_one())
        assert response["model"]["cache"] == {k: v for k, v in first.items() if k != "prefix"}
        assert json.loads(conn.execute(select(session_cache_state.c.record_json)).scalar_one()) == first
    _, _, newer = _record(now=120)
    _, _, other = _record(now=110, model="other")
    await backend.store.append_audit(SESSION, "fork_turn", {
        "version": 1, "cache_attempts": [{"cache": newer}, {"cache": other}],
        "cache_state": [first], "purpose": "test",
    })
    await backend.store.append_audit(SESSION, "attempt", {"version": 1, "cache": first, "cache_state": [other]})
    audits = backend.audits()
    assert all("cache_state" not in payload for _, payload in audits)
    assert all("prefix" not in item["cache"] for item in audits[0][1]["cache_attempts"])
    assert list((await _restore(backend)).latest.values()) == [newer, other]
    # Inline diagnostics remain; no signature fields or request contents change.
    assert audits[1][1]["cache"] == {k: v for k, v in first.items() if k != "prefix"}


async def test_first_compact_audit_carries_old_snapshot_only_route(cache_backend, monkeypatch):
    backend = cache_backend
    _, _, old_route = _record(now=100, model="historical")
    _, _, current = _record(now=110)
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 2)
    await backend.store.append_audit(SESSION, "attempt", {"version": 1, "cache": current, "cache_state": [old_route, current]})
    restored = await _restore(backend)
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 3)
    await backend.store.append_audit(SESSION, "attempt", {
        "version": 1, "cache": current, "cache_state": list(restored.latest.values()),
    })
    assert (await _restore(backend)).latest == restored.latest


async def test_cache_prefixless_ties_no_usage_and_old_writer_interleave(cache_backend, monkeypatch):
    backend = cache_backend
    _, message, old = _record(now=100)
    prefixless = {k: v for k, v in old.items() if k != "prefix"}
    for record in (prefixless, old, prefixless):
        await backend.store.append_response(SESSION, message, final=True, cache=record)
    assert (await _restore(backend)).latest[(old["provider"], old["model"])] == old
    newer = {**prefixless, "started_at": 110}
    await backend.store.append_response(SESSION, message, final=True, cache=newer)
    await backend.store.append_response(SESSION, message, final=True, cache={**old, "started_at": 120, "input_tokens": None})
    assert (await _restore(backend)).latest[(old["provider"], old["model"])] == newer
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 2)
    inline = {**old, "started_at": 130}
    await backend.store.append_response(SESSION, message, final=True, cache=inline)
    assert (await _restore(backend)).latest[(old["provider"], old["model"])] == inline
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 3)
    await backend.store.append_audit(SESSION, "attempt", {"version": 1, "cache": old, "cache_state": [inline]})
    assert (await _restore(backend)).latest[(old["provider"], old["model"])] == inline


@pytest.mark.parametrize("kind", ["response", "attempt", "fork_turn"])
async def test_cache_originating_row_failure_rolls_back_state(cache_backend, kind):
    backend = cache_backend
    _, message, record = _record()
    def fail(conn, cursor, statement, parameters, context, executemany):
        table = "messages" if kind == "response" else "agent_events"
        if statement.startswith(f"INSERT INTO {table} "):
            raise RuntimeError("origin failure")
    sa_event.listen(backend.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="origin failure"):
            if kind == "response":
                await backend.store.append_response(SESSION, message, final=True, cache=record)
            else:
                await backend.store.append_audit(SESSION, kind, {
                    "version": 1, "cache": record, "cache_attempts": [{"cache": record}],
                })
    finally:
        sa_event.remove(backend.engine, "before_cursor_execute", fail)
    with backend.engine.connect() as conn:
        assert conn.execute(select(session_cache_state)).first() is None
        assert conn.execute(select(messages).where(messages.c.author == "agent")).first() is None
        assert conn.execute(select(agent_events)).first() is None


@pytest.mark.parametrize("action", ["archive", "delete"])
async def test_cache_cleanup_also_works_for_bridge_sql(cache_backend, action):
    backend = cache_backend
    _, message, record = _record()
    await backend.store.append_response(SESSION, message, final=True, cache=record)
    with backend.engine.begin() as conn:
        target = SESSION
        if action == "archive":
            conn.execute(agent_sessions.update().where(agent_sessions.c.id == target).values(status="archived"))
        else:
            # Sessions with retained messages are archived by product teardown.
            # Physical deletion is for an otherwise empty Session.
            target = "empty"
            _session(conn, target, backend.scope)
            conn.execute(session_cache_state.insert().values(
                session_id=target, provider=record["provider"], model=record["model"], record_json=json.dumps(record),
            ))
            conn.execute(agent_sessions.delete().where(agent_sessions.c.id == target))
        assert conn.execute(select(session_cache_state).where(session_cache_state.c.session_id == target)).first() is None


def test_vault_deduplicates_only_snapshots_preserving_each_request(db, monkeypatch):
    with db.begin() as conn:
        snapshot = _secret_snapshot(conn)
        times = iter(["exact-one", "exact-two", "exact-three"])
        monkeypatch.setattr(vault_service, "_now", lambda: next(times))
        for name in ("proxied", "proxied", "delivered"):
            vault_service.audit(conn, name, secret_name="KEY", requester={"pid": 12},
                                delivery={"status": 200}, access_snapshot=snapshot)
        rows = list(conn.execute(select(vault_audit).where(vault_audit.c.ts.like("exact-%"))).mappings())
        assert len({row["id"] for row in rows}) == 3
        assert [row["ts"] for row in rows] == ["exact-one", "exact-two", "exact-three"]
        assert all(row["requester"] == '{"pid": 12}' and row["request_id"] is None for row in rows)
        assert len(conn.execute(select(vault_audit_access_snapshots)).all()) == 1
        assert json.loads(rows[0]["delivery"]) == json.loads(rows[1]["delivery"])
        assert json.loads(rows[2]["delivery"])["_resource_access_snapshot"] == snapshot
        assert len([row for row in vault_service.list_audit(conn, user_context=_member()) if row["ts"].startswith("exact-")]) == 3


def test_vault_deleted_id_never_becomes_different_authority(db):
    with db.begin() as conn:
        snapshot = _secret_snapshot(conn)
        vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=snapshot)
        old = conn.execute(select(vault_audit.c.id).where(vault_audit.c.event == "proxied")).scalar_one()
        first = conn.execute(select(vault_audit_access_snapshots.c.id)).scalar_one()
        conn.execute(vault_audit_access_snapshots.delete())  # synthetic corruption, never product GC
        changed = deepcopy(snapshot)
        changed["resources"][0]["policy"]["policy_revision"] += 1
        vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=changed)
        assert conn.execute(select(vault_audit_access_snapshots.c.id)).scalar_one() > first
        assert old not in {row["id"] for row in vault_service.list_audit(conn, user_context=_member())}


@pytest.mark.parametrize("damage", [None, "null", "true", "-1", '"3"', str(2**63-1), "broken"])
def test_vault_allocator_fails_closed(db, damage):
    with db.begin() as conn:
        snapshot = _secret_snapshot(conn)
        if damage is None:
            conn.execute(state_meta.delete().where(state_meta.c.key == HIGH_WATER_KEY))
        else:
            conn.execute(state_meta.update().where(state_meta.c.key == HIGH_WATER_KEY).values(value_json=damage))
    with pytest.raises(ValueError, match="allocator"):
        with db.begin() as conn:
            vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=snapshot)
    with db.connect() as conn:
        assert conn.execute(select(vault_audit_access_snapshots)).first() is None
        assert conn.execute(select(vault_audit).where(vault_audit.c.event == "proxied")).first() is None


def test_vault_audit_failure_rolls_back_allocation(db):
    with db.begin() as conn:
        snapshot = _secret_snapshot(conn)
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO vault_audit "):
            raise RuntimeError("audit failure")
    sa_event.listen(db, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="audit failure"):
            with db.begin() as conn:
                vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=snapshot)
    finally:
        sa_event.remove(db, "before_cursor_execute", fail)
    with db.connect() as conn:
        assert conn.execute(select(vault_audit_access_snapshots)).first() is None
        assert conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == HIGH_WATER_KEY)).scalar_one() == "0"


def test_skill_shared_lifetime_duplicate_and_old_inline_writer(db, monkeypatch):
    one, two = catalog(), catalog()
    with db.begin() as conn:
        for item in (one, two):
            assert skill_observability.record(conn, item, enabled=lambda: True, now=NOW) == "accepted"
        before = conn.execute(select(agent_events).order_by(agent_events.c.id)).all()
        assert skill_observability.record(conn, one, enabled=lambda: True, now=NOW) == "duplicate"
        assert conn.execute(select(agent_events).order_by(agent_events.c.id)).all() == before
        assert len(conn.execute(select(skill_catalogs)).all()) == 1
        row = dict(conn.execute(select(agent_events).where(agent_events.c.id == one["id"])).mappings().one())
        assert json.loads(read_content(conn, row)) == one["content"]
        conn.execute(agent_events.delete().where(agent_events.c.id == one["id"]))
        assert len(conn.execute(select(skill_catalogs)).all()) == 1
        monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", 2)
        third = catalog()
        assert skill_observability.record(conn, third, enabled=lambda: True, now=NOW) == "accepted"
        assert "entries" in json.loads(conn.execute(select(agent_events.c.content_json).where(agent_events.c.id == third["id"])).scalar_one())
        # Exactly the old bridge clear SQL; DB-owned GC works without its knowing it.
        skill_observability.clear(conn, now=NOW)
        assert conn.execute(select(skill_catalogs)).first() is None


@pytest.mark.parametrize("update", ["inline", "visibility", "event_type", "same"])
def test_skill_last_owner_update_cleanup(db, update):
    item = catalog()
    with db.begin() as conn:
        skill_observability.record(conn, item, enabled=lambda: True, now=NOW)
        row = conn.execute(select(agent_events.c.content_json)).scalar_one()
        values = {"inline": {"content_json": json.dumps(item["content"])},
                  "visibility": {"visibility": "audit"}, "event_type": {"event_type": "other"},
                  "same": {"content_json": row}}[update]
        conn.execute(agent_events.update().where(agent_events.c.id == item["id"]).values(**values))
        assert bool(conn.execute(select(skill_catalogs)).first()) is (update == "same")


@pytest.mark.no_sqlite_template
def test_boundary_preserves_old_bytes_and_commits_epoch_with_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    migrations.run_migrations(path, revision="20261112_0088")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO vault_audit(id,ts,event,delivery) VALUES ('old','exact','proxied',?)", ('{ "x":  1 }',))
        before = conn.execute("SELECT * FROM vault_audit").fetchall()
        conn.execute("INSERT INTO vault_audit_access_snapshots(id,snapshot) VALUES (7,'legacy-unused')")
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM vault_audit").fetchall() == before
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("3",)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("20261114_0089",)
        assert conn.execute("SELECT value_json FROM state_meta WHERE key=?", (HIGH_WATER_KEY,)).fetchone() == ("7",)
        assert conn.execute("SELECT snapshot FROM vault_audit_access_snapshots").fetchall() == [("legacy-unused",)]


@pytest.mark.no_sqlite_template
@pytest.mark.parametrize("crash", ["before", "after"])
def test_real_boundary_crash_preserves_complete_old_or_new_state(tmp_path, crash):
    import subprocess
    import sys
    from tests.test_sqlite_migration_preflight import _snapshot

    path = tmp_path / "crash.sqlite"
    migrations.run_migrations(path, revision="20261112_0088")
    before = _snapshot(path)["database.sql"]
    decision = migrations.preflight_database(path)
    assert decision.compatibility_epoch == 2 and not decision.skip_snapshot
    result = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from storage import migrations
def crash(conn, cursor, statement, parameters, context, executemany):
    if sys.argv[2] == 'before' and statement.startswith('UPDATE alembic_version SET') and '20261114_0089' in statement:
        os._exit(71)
event.listen(Engine, 'after_cursor_execute', crash)
migrations.run_migrations(Path(sys.argv[1]))
os._exit(72)
""", str(path), crash], check=False, capture_output=True, text=True)
    assert result.returncode == (71 if crash == "before" else 72), result.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == (
            "2" if crash == "before" else "3",
        )
        assert bool(conn.execute("SELECT name FROM sqlite_master WHERE name='ix_agent_events_catalog_ref'").fetchone()) is (crash == "after")
        assert bool(conn.execute("SELECT value_json FROM state_meta WHERE key=?", (HIGH_WATER_KEY,)).fetchone()) is (crash == "after")
    [backup] = list((tmp_path / "backups").glob("*/vibe.sqlite"))
    assert _snapshot(backup)["database.sql"] == before
    if crash == "before":
        assert _snapshot(path)["database.sql"] == before
    else:
        from alembic import command
        with pytest.raises(RuntimeError, match="restoring discards writes"):
            command.downgrade(migrations.alembic_config(path), "20261112_0088")


def test_concurrent_equal_vault_snapshots_converge(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with db.begin() as conn:
        snapshot = _secret_snapshot(conn)
    barrier = Barrier(2)
    def append():
        barrier.wait(timeout=10)
        with db.begin() as conn:
            vault_service.audit(conn, "proxied", secret_name="KEY", access_snapshot=snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    with db.connect() as conn:
        assert len(conn.execute(select(vault_audit_access_snapshots)).all()) == 1
        assert len(conn.execute(select(vault_audit).where(vault_audit.c.event == "proxied")).all()) == 2


def test_skill_clear_rolls_back_catalog_and_events_together(db):
    observation = catalog()
    with db.begin() as conn:
        skill_observability.record(conn, observation, enabled=lambda: True, now=NOW)
        rows = conn.execute(select(agent_events)).all()
        snapshots = conn.execute(select(skill_catalogs)).all()
    with pytest.raises(RuntimeError, match="rollback"):
        with db.begin() as conn:
            skill_observability.clear(conn, now=NOW)
            assert conn.execute(select(skill_catalogs)).first() is None
            raise RuntimeError("rollback")
    with db.connect() as conn:
        assert conn.execute(select(agent_events)).all() == rows
        assert conn.execute(select(skill_catalogs)).all() == snapshots


def test_skill_gc_uses_two_index_seeks_not_history_scan(db):
    import importlib

    migration = importlib.import_module("storage.alembic.versions.20261114_0089_coordinated_writers")
    with db.connect() as conn:
        # The trigger's OLD scalar, not the catalog column's TEXT affinity,
        # must be on the right side of the expression-index equality.
        plan = conn.exec_driver_sql(
            f"EXPLAIN QUERY PLAN DELETE FROM skill_catalogs WHERE digest = ? AND NOT EXISTS ("
            f"SELECT 1 FROM agent_events INDEXED BY ix_agent_events_catalog_ref "
            f"WHERE {migration._PREDICATE} AND {migration._REF} = ?)", ("a" * 64, "a" * 64),
        ).fetchall()
        detail = "\n".join(row[-1] for row in plan)
        assert "SEARCH skill_catalogs" in detail
        assert "SEARCH agent_events USING INDEX ix_agent_events_catalog_ref (<expr>=?)" in detail
        assert "SCAN" not in detail
