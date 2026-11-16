"""Reader-first contract: references hydrate, but released writers stay inline.

Existing Skill tests only exercise inline observations. These cases cover the
new storage representation, its SQL/RBAC boundary, and a populated upgrade that
must not silently turn on deduplication or rewrite event bytes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import event as sqlalchemy_event, select, text

from core import skill_observability as runtime
from storage import agent_events_service, migrations, skill_observability as store
from storage.agent_events_retention import run_skill_retention
from storage.db import create_sqlite_engine
from storage.migration_policy import classify_migration
from storage.models import agent_events, skill_catalogs
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
from storage.skill_catalogs import CatalogReferenceError
from tests.test_skill_observability import NOW, engine, seed_session, skill, write  # noqa: F401
from vibe.authorization import AuthorizationContext


@pytest.fixture(autouse=True)
def released_inline_writer(monkeypatch):
    # This module proves the reader-first contract; writer activation has its
    # own suite, including duplicate receipts and lifecycle cleanup.
    monkeypatch.setattr("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", 2)


def catalog(*, entries=True, session_id=None):
    event = runtime.observation(
        "skill.catalog_result", runtime.catalog_result([skill()] if entries else []), session_id=session_id
    )
    event["metadata"]["observed_at"] = store.timestamp(NOW)
    return event


def compact(conn, event):
    """A future writer fixture, not a production deduplication seam."""
    body = deepcopy(event["content"])
    entries_json = json.dumps(body.pop("entries"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(entries_json.encode("utf-8")).hexdigest()
    conn.exec_driver_sql(
        "INSERT INTO skill_catalogs(digest, entries_json) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (digest, entries_json),
    )
    body["entries_ref"] = digest
    conn.execute(agent_events.update().where(agent_events.c.id == event["id"]).values(content_json=json.dumps(body)))
    return digest


@pytest.mark.no_sqlite_template
def test_populated_upgrade_and_production_writes_keep_exact_inline_bytes(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision="20261108_0083")
    db = create_sqlite_engine(path)
    event = catalog()
    with db.begin() as conn:
        assert store.record(conn, event, enabled=lambda: True, now=NOW) == "accepted"
        before = dict(conn.execute(select(agent_events)).mappings().one())
    # An explicit read-only query of the old file must not require or create the
    # new table. Missing optional snapshots cannot break legacy-only content.
    assert json.loads(run_read_only_query(
        "SELECT content_json FROM skill_events", page_request=None, db_path=path
    ).rows[0]["content_json"]) == event["content"]
    with db.connect() as conn:
        assert conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='skill_catalogs'").first() is None
        assert dict(conn.execute(select(agent_events)).mappings().one()) == before
    db.dispose()
    migrations.run_migrations(path, revision="20261112_0088")
    db = create_sqlite_engine(path)
    try:
        with db.begin() as conn:
            assert dict(conn.execute(select(agent_events)).mappings().one()) == before
            assert store.record(conn, event, enabled=lambda: True, now=NOW) == "duplicate"
            repeated = deepcopy(event)
            repeated["id"] += "-repeat"
            assert store.record(conn, repeated, enabled=lambda: True, now=NOW) == "accepted"
            after = dict(conn.execute(select(agent_events).where(agent_events.c.id == repeated["id"])).mappings().one())
            assert {**after, "id": before["id"]} == before
            assert conn.execute(select(skill_catalogs)).first() is None
            assert conn.exec_driver_sql("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").scalar() == "2"
    finally:
        db.dispose()
    decision = classify_migration(
        Path(migrations.alembic_dir()) / "versions" / "20261112_0088_skill_catalog_readers.py"
    )
    assert decision.permitted == "additive", decision.reason


@pytest.mark.parametrize("entries", [True, False])
def test_mixed_shared_catalogs_hydrate_service_and_sql_without_mutation(engine, entries):
    session = seed_session(engine)
    events = [catalog(entries=entries, session_id=session) for _ in range(3)]
    for event in events:
        assert write(engine, event) == "accepted"
    with engine.begin() as conn:
        for event in events[1:]:
            compact(conn, event)
        stored = conn.execute(select(agent_events).order_by(agent_events.c.id)).all()
        assert len(conn.execute(select(skill_catalogs)).all()) == 1
        payloads = agent_events_service.list_session_events(conn, session_id=session)
        assert len(payloads) == 3
        assert all(payload["content"] == events[0]["content"] for payload in payloads)
        assert agent_events_service.list_session_events(conn, session_id=session, newest_first=True, limit=1) == payloads[-1:]
        assert conn.execute(select(agent_events).order_by(agent_events.c.id)).all() == stored
    rows = run_read_only_query(
        "SELECT content_json FROM skill_events ORDER BY id", page_request=None
    ).rows
    assert [json.loads(row["content_json"]) for row in rows] == [events[0]["content"]] * 3
    rows = run_read_only_query(
        "SELECT count(*) AS n FROM skill_events e, json_each(e.content_json, '$.entries')", page_request=None
    ).rows
    assert rows == [{"n": 3 if entries else 0}]
    raw = run_read_only_query(
        "SELECT count(*) AS n FROM agent_events WHERE json_type(content_json, '$.entries_ref') = 'text'",
        page_request=None,
    ).rows
    assert raw == [{"n": 2}]


def test_inline_retry_of_reference_is_duplicate_without_rewriting_it(engine):
    event = catalog()
    assert write(engine, event) == "accepted"
    with engine.begin() as conn:
        compact(conn, event)
        before = conn.execute(select(agent_events.c.content_json)).scalar_one()
        offers = conn.exec_driver_sql("SELECT sum(catalog_offer_count) FROM skill_usage_daily").scalar_one()
    assert write(engine, event) == "duplicate"
    changed = deepcopy(event)
    changed["content"]["has_more"] = True
    with pytest.raises(store.ObservationError, match="observation_id_conflict"):
        write(engine, changed)
    with engine.connect() as conn:
        assert conn.execute(select(agent_events.c.content_json)).scalar_one() == before
        assert conn.exec_driver_sql("SELECT sum(catalog_offer_count) FROM skill_usage_daily").scalar_one() == offers


def test_event_and_catalog_read_share_snapshot_across_concurrent_pruning(engine):
    session = seed_session(engine)
    observation = catalog(session_id=session)
    write(engine, observation)
    with engine.begin() as conn:
        compact(conn, observation)

    def prune_after_event_select(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT agent_events."):
            with engine.begin() as writer:
                writer.execute(agent_events.delete())
                writer.execute(skill_catalogs.delete())

    with engine.connect() as reader:
        sqlalchemy_event.listen(reader, "after_cursor_execute", prune_after_event_select)
        payloads = agent_events_service.list_session_events(reader, session_id=session)
        assert [payload["content"] for payload in payloads] == [observation["content"]]
    with engine.connect() as conn:
        assert conn.execute(select(skill_catalogs)).first() is None


@pytest.mark.parametrize("damage", ["missing", "bad-reference", "invalid-json", "wrong-digest", "not-list"])
def test_unresolvable_reference_fails_visibly_in_both_readers(engine, damage):
    session = seed_session(engine)
    event = catalog(session_id=session)
    write(engine, event)
    with engine.begin() as conn:
        digest = compact(conn, event)
        if damage == "missing":
            conn.execute(skill_catalogs.delete())
        elif damage == "bad-reference":
            conn.exec_driver_sql("UPDATE agent_events SET content_json = json_set(content_json, '$.entries_ref', 'bad')")
        else:
            replacement = {"invalid-json": "[", "wrong-digest": "[]", "not-list": "{}"}[damage]
            conn.execute(skill_catalogs.update().values(entries_json=replacement).where(skill_catalogs.c.digest == digest))
    with engine.connect() as conn, pytest.raises(CatalogReferenceError):
        agent_events_service.list_session_events(conn, session_id=session)
    with pytest.raises(ReadOnlyQueryError, match="Skill catalog"):
        run_read_only_query("SELECT content_json FROM skill_events", page_request=None)


def test_inline_precedence_and_unrelated_events_never_resolve_references(engine):
    session = seed_session(engine)
    event = catalog(session_id=session)
    write(engine, event)
    body = {**event["content"], "entries_ref": "invalid-but-ignored"}
    with engine.begin() as conn:
        conn.execute(agent_events.update().values(content_json=json.dumps(body)))
        assert agent_events_service.list_session_events(conn, session_id=session)[0]["content"] == body
        other = agent_events_service.append(
            conn, session_id=session, scope_id=None, platform="web", event_type="unrelated",
            content={"entries_ref": "not-a-catalog"},
        )
        assert other["content"] == {"entries_ref": "not-a-catalog"}
    assert json.loads(run_read_only_query(
        "SELECT content_json FROM skill_events", page_request=None
    ).rows[0]["content_json"]) == body


@pytest.mark.parametrize("role", ["owner", "member", "editor", "viewer", None])
def test_catalog_and_projection_keep_existing_global_diagnostics_floor(engine, role):
    session = seed_session(engine)
    event = catalog(session_id=session)
    write(engine, event)
    with engine.begin() as conn:
        compact(conn, event)
    remote = AuthorizationContext(is_remote=True, instance_role=role)
    for query in (
        "SELECT entries_json FROM skill_catalogs",
        "SELECT content_json FROM skill_events",
        "WITH catalogs AS (SELECT * FROM main.skill_catalogs) SELECT * FROM catalogs",
        "SELECT skill_catalog_content(e.content_json,e.event_type,e.visibility,c.entries_json) "
        "FROM agent_events e JOIN skill_catalogs c ON 1=1",
    ):
        if role in {"owner", "member"}:
            assert len(run_read_only_query(query, page_request=None, user_context=remote).rows) == 1
        else:
            with pytest.raises(ReadOnlyQueryError):
                run_read_only_query(query, page_request=None, user_context=remote)


def test_shared_snapshot_survives_other_event_retention_and_session_deletion(engine):
    # Catalogs are global immutable snapshots, not owned by the first event or
    # Session. Reader-first maintenance must not garbage-collect a shared row.
    session = seed_session(engine)
    first = catalog(session_id=session)
    survivor = catalog()
    for event in (first, survivor):
        write(engine, event)
    with engine.begin() as conn:
        compact(conn, first)
        compact(conn, survivor)
        conn.execute(agent_events.update().where(agent_events.c.id == first["id"]).values(
            created_at="2020-01-01T00:00:00.000000Z"
        ))
    assert run_skill_retention(engine, now=NOW)["events"] == 1
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM agent_sessions WHERE id = ?", (session,))
        assert len(conn.execute(select(skill_catalogs)).all()) == 1
    rows = run_read_only_query("SELECT content_json FROM skill_events", page_request=None).rows
    assert [json.loads(row["content_json"]) for row in rows] == [survivor["content"]]


def test_hydrated_reads_use_event_session_and_catalog_primary_key_indexes(engine):
    with engine.connect() as conn:
        event_plan = conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT * FROM agent_events WHERE session_id = :session "
            "ORDER BY created_at, id LIMIT 5"
        ), {"session": "fixture"}).all()
        catalog_plan = conn.execute(text(
            "EXPLAIN QUERY PLAN SELECT entries_json FROM skill_catalogs WHERE digest = :digest"
        ), {"digest": "a" * 64}).all()
    for plan in (event_plan, catalog_plan):
        details = " ".join(row[3] for row in plan)
        assert "SEARCH" in details
        assert "SCAN" not in details and "USE TEMP B-TREE" not in details
    plan = run_read_only_query(
        "EXPLAIN QUERY PLAN SELECT content_json FROM skill_events WHERE session_id = 'fixture' "
        "ORDER BY created_at, id LIMIT 5", page_request=None,
    ).rows
    details = " ".join(row["detail"] for row in plan)
    assert "SEARCH e USING INDEX ix_agent_events_session_created_id" in details
    assert "SEARCH c USING INDEX sqlite_autoindex_skill_catalogs_1" in details
    assert "SCAN" not in details and "USE TEMP B-TREE" not in details
