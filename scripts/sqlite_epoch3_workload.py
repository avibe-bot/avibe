"""Isolated workload helpers for sqlite_epoch3_measure; never a live-state tool."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import time


def footprint(db: Path) -> dict:
    """Checkpoint first; count all table/index pages, including shared state."""
    with closing(sqlite3.connect(db)) as conn:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0]:
            raise RuntimeError("measurement requires an idle fixture database")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        try:
            objects = dict(conn.execute("SELECT name,sum(pgsize) FROM dbstat GROUP BY name"))
        except sqlite3.OperationalError:
            objects = None
    return {"logical_bytes": pages * page_size, "allocated_bytes": db.stat().st_blocks * 512,
            "used_page_bytes": (pages - free_pages) * page_size, "objects": objects}


def seed(home: Path) -> dict:
    from storage import importer
    from storage.sessions_service import SQLiteSessionsService
    from vibe.authorization import AuthorizationContext

    db = home / "state" / "vibe.sqlite"
    importer.repair_sqlite_state(db_path=db, state_dir=db.parent)
    sessions = SQLiteSessionsService(db)
    try:
        session = sessions.reserve_standalone_agent_session(
            agent_backend="vibey", session_anchor="epoch3-measure", workdir=str(home / "workspace"),
            authorization_context=AuthorizationContext(instance_role="owner"),
        )
    finally:
        sessions.close()
    (home / "fixture.json").write_text(json.dumps({"session": session}))
    try:
        version = importlib.metadata.version("avibe-os")
    except importlib.metadata.PackageNotFoundError:
        version = "source"
    return {"version": version, "session": session}


def workload(home: Path, group: str, count: int) -> dict:
    """Same API calls/payloads under either binary, no forcing the writer gate."""
    from sqlalchemy import select
    from core import skill_observability as observations
    from storage import messages_service, resource_access_service as acl, skill_observability as skills, vault_service as vault
    from storage.db import create_sqlite_engine
    from storage.models import vault_secrets
    from storage.vault_crypto import Sealed

    db = home / "state" / "vibe.sqlite"
    session = json.loads((home / "fixture.json").read_text())["session"]
    engine = create_sqlite_engine(db)
    before = footprint(db)
    started = time.monotonic()
    try:
        if group == "cache":
            from core.agent_core.harness.cache_metrics import MAX_PREFIX_BOUNDARIES, valid_record
            from core.agent_core.messages import AssistantMessage, Origin, TextBlock, UserMessage
            from storage.agent_transcript import SQLiteTranscriptStore

            store = SQLiteTranscriptStore(engine)
            async def cache_calls():
                states = {}
                for i in range(count):
                    with engine.begin() as conn:
                        user = messages_service.append(conn, scope_id=None, session_id=session, platform="avibe",
                            author="user", message_type="user", text=f"synthetic turn {i}")
                    await store.consume_input(session, user["id"], UserMessage((TextBlock(f"synthetic turn {i}"),)))
                    model = f"model-{i % 4}"
                    record = {"version": 1, "provider": "measurement", "model": model,
                              "route": hashlib.sha256(model.encode()).hexdigest()[:32],
                              "started_at": 100 + i, "context_seq": i * 2, "input_tokens": 9600,
                              "prefix": [[hashlib.sha256(f"block-{j}".encode()).hexdigest()[:32], (j + 1) * 300, j]
                                         for j in range(MAX_PREFIX_BOUNDARIES)]}
                    assert valid_record(record)
                    states[(record["provider"], record["model"])] = record
                    await store.append_response(session, AssistantMessage((TextBlock("synthetic response"),),
                        Origin("measurement", "anthropic", model), "stop"), final=True, cache=record)
                    await store.append_audit(session, "attempt", {"version": 1, "cache": record, "cache_state": list(states.values())})
            asyncio.run(cache_calls())
        elif group == "skill":
            # One complete 25-entry page, repeated with distinct receipt IDs.
            entries = [{"skill_key": hashlib.sha256(f"skill-{i}".encode()).hexdigest(),
                        "skill_name": f"skill-{i}", "source_kind": "builtin",
                        "descriptor_sha256": hashlib.sha256(f"descriptor-{i}".encode()).hexdigest(),
                        "position": i + 1} for i in range(25)]
            payload = {"entry_point": "cli_list", "outcome": "success", "error_code": None,
                       "page": 1, "has_more": False, "catalog_digest": hashlib.sha256(
                           json.dumps(entries, separators=(",", ":")).encode()).hexdigest(), "entries": entries}
            now = datetime.now(timezone.utc)
            for _ in range(count):
                event = observations.observation("skill.catalog_result", payload, session_id=session)
                event["metadata"]["observed_at"] = skills.timestamp(now)
                with engine.begin() as conn:
                    assert skills.record(conn, event, enabled=lambda: True, now=now) == "accepted"
        elif group == "vault":
            with engine.begin() as conn:
                vault.create_secret(conn, name="MEASUREMENT", sealed=Sealed("fixture", "fixture", "fixture"))
                resource = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == "MEASUREMENT")).scalar_one()
                acl.ensure_resource_policy(conn, resource_kind="vault_secret", resource_id=resource,
                    organization_id="fixture-org", owner_user_id="fixture-owner", access_level="public", group_ids=[],
                    policy_revision=1, last_applied_control_plane_revision=1)
                snapshot = vault._build_audit_access_snapshot(conn, ["MEASUREMENT"])
            for i in range(count):
                with engine.begin() as conn:
                    vault.audit(conn, "proxied", secret_name="MEASUREMENT", access_snapshot=snapshot,
                                requester={"source": "measurement", "request": i}, delivery={"status": 200})
        else:
            raise ValueError(group)
    finally:
        engine.dispose()
    after = footprint(db)
    return {"group": group, "operations": count, "seconds": time.monotonic() - started,
            "before": before, "after": after, "representation": representation(db, group),
            "growth_bytes": after["logical_bytes"] - before["logical_bytes"],
            "used_page_growth_bytes": after["used_page_bytes"] - before["used_page_bytes"]}


def representation(db: Path, group: str) -> dict:
    """Check that the benchmark actually exercised each intended representation."""
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        if group == "cache":
            state = conn.execute("SELECT count(*) FROM session_cache_state").fetchone()[0]
            inline = conn.execute("SELECT count(*) FROM messages WHERE json_type(content_json,'$.model.cache.prefix')='array'").fetchone()[0]
            compact = conn.execute("SELECT count(*) FROM messages WHERE json_type(content_json,'$.model.cache')='object' "
                                   "AND json_type(content_json,'$.model.cache.prefix') IS NULL").fetchone()[0]
            audit_prefix = conn.execute("SELECT count(*) FROM agent_events WHERE json_type(content_json,'$.cache.prefix')='array'").fetchone()[0]
            return {"state_rows": state, "inline_rows": inline, "compact_rows": compact, "audit_prefix_rows": audit_prefix}
        if group == "skill":
            rows = conn.execute("SELECT content_json FROM agent_events WHERE event_type='skill.catalog_result'").fetchall()
            compact = sum("entries_ref" in json.loads(row[0]) for row in rows)
            state = conn.execute("SELECT count(*) FROM skill_catalogs").fetchone()[0]
        else:
            rows = conn.execute("SELECT delivery FROM vault_audit WHERE event='proxied'").fetchall()
            compact = sum("ref" in json.loads(row[0])["_resource_access_snapshot"] for row in rows)
            state = conn.execute("SELECT count(*) FROM vault_audit_access_snapshots").fetchone()[0]
        return {"state_rows": state, "inline_rows": len(rows) - compact, "compact_rows": compact}


def populate(home: Path, target_bytes: int) -> dict:
    """Create a valid, synthetic message history under the real predecessor.

    Bulk rows clone a real API-produced message's shape. This deliberately
    measures upgrade of populated history, not the speed of the message API.
    """
    from storage import messages_service
    from storage.db import create_sqlite_engine

    db = home / "state" / "vibe.sqlite"
    session = json.loads((home / "fixture.json").read_text())["session"]
    engine = create_sqlite_engine(db)
    with engine.begin() as conn:
        row = messages_service.append(conn, scope_id=None, session_id=session, platform="avibe", author="user",
                                      message_type="user", text="synthetic history " + "x" * 65536)
    engine.dispose()
    with closing(sqlite3.connect(db)) as conn:
        columns = [item[1] for item in conn.execute("PRAGMA table_info(messages)")]
        template = list(conn.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone())
        identifier = columns.index("id")
        statement = "INSERT INTO messages(" + ",".join('"' + c + '"' for c in columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
        count = 0
        while conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0] < target_bytes:
            batch = []
            for _ in range(128):
                value = template.copy()
                value[identifier] = f"measurement-history-{count:09d}"
                batch.append(value)
                count += 1
            conn.executemany(statement, batch)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"bulk_messages": count, "target_bytes": target_bytes, "footprint": footprint(db)}


def history_facts(home: Path) -> dict:
    db = home / "state" / "vibe.sqlite"
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        # Streaming digest detects any historical byte edits without holding the
        # multi-GB fixture in memory; migration metadata is deliberately excluded.
        digest = hashlib.sha256()
        counts = {}
        for table in ("messages", "agent_events", "vault_audit"):
            count = 0
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY id'):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                count += 1
            counts[table] = count
    return {"history_sha256": digest.hexdigest(), "rows": counts}


def upgrade(home: Path, refuse: bool) -> dict:
    from storage import migrations
    from storage.migration_space import InsufficientMigrationSpaceError

    db = home / "state" / "vibe.sqlite"
    decision = migrations.preflight_database(db, read_only=True)
    plan = migrations.require_upgrade_space(db, decision) if not refuse else None
    backup_seconds = 0.0
    original = migrations.create_sqlite_migration_backup
    def timed_backup(*args, **kwargs):
        nonlocal backup_seconds
        start = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            backup_seconds += time.monotonic() - start
    migrations.create_sqlite_migration_backup = timed_backup
    started = time.monotonic()
    try:
        migrations.run_migrations(db)
    except InsufficientMigrationSpaceError as exc:
        if not refuse:
            raise
        return {"refused": True, "message": str(exc), "plan": exc.plan.to_dict(), "seconds": time.monotonic() - started}
    if refuse:
        raise AssertionError("near-full upgrade unexpectedly admitted")
    return {"refused": False, "plan": plan.to_dict() if plan else None,
            "seconds": time.monotonic() - started, "backup_seconds": backup_seconds,
            "after": footprint(db)}


def run(action: str, home: Path, *, group: str, count: int, target_bytes: int) -> dict:
    if action == "seed":
        return seed(home)
    if action == "workload":
        return workload(home, group, count)
    if action == "populate":
        return populate(home, target_bytes)
    if action == "history":
        return history_facts(home)
    if action in {"refuse", "upgrade"}:
        return upgrade(home, action == "refuse")
    raise ValueError(action)
