"""Move future model selections, never execution records or user-authored commands."""

from __future__ import annotations

import json

from sqlalchemy import select, update

from core.vibe_agents import normalize_agent_name, resolve_effective_default_agent
from storage.db import get_cached_sqlite_engine, retry_sqlite_write
from storage.models import agents, agent_sessions, run_definitions, scope_settings
from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY
from storage.settings_revision import mark_runtime_settings_changed


def _object(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pages(conn, primary_key, *columns):
    """Legacy JSON isn't indexed. Keyset-page the read snapshot *before* taking a writer lock."""
    query = select(primary_key, *columns).order_by(primary_key).limit(128)
    cursor = None
    while True:
        rows = conn.execute(query if cursor is None else query.where(primary_key > cursor)).all()
        yield from rows
        if len(rows) < 128:
            return
        cursor = rows[-1][0]


def _replacement(moves, value):
    return moves.get(value) if isinstance(value, str) else None


def _plan_scopes(conn, backend, moves):
    table = scope_settings.c
    owners = {}
    updates = []
    for scope_id, agent_name, model, raw in _pages(
        conn, table.scope_id, table.agent_name, table.model, table.settings_json
    ):
        payload = _object(raw)
        routing = payload.get("routing")
        if not isinstance(routing, dict):
            routing = {}
        name = routing.get("agent_name") or routing.get("agent") or agent_name
        changes = {}
        fields = []
        if backend in {"claude", "codex", "opencode"}:
            fields.append(f"{backend}_model")
        if any(_replacement(moves, val) for val in (model, routing.get("model"), routing.get("model_override"))):
            if not name and None not in owners:
                default = resolve_effective_default_agent(conn)
                owners[None] = default.backend if default else None
            try:
                name = normalize_agent_name(name) if name else None
            except ValueError:
                name = ""
            if name and name not in owners:
                owners[name] = conn.scalar(
                    select(agents.c.backend).where(agents.c.normalized_name == name, agents.c.archived_at.is_(None))
                )
            if owners.get(name) == backend:
                fields.extend(("model", "model_override"))
                if new := _replacement(moves, model):
                    changes["model"] = new
        json_changed = False
        for key in fields:
            if new := _replacement(moves, routing.get(key)):
                routing[key] = new
                json_changed = True
        if json_changed:
            changes["settings_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if changes:
            updates.append((scope_id, changes))
    return updates


def _plan_definitions(conn, backend, moves):
    updates = []
    for rid, raw in _pages(conn, run_definitions.c.id, run_definitions.c.metadata_json):
        payload = _object(raw)
        snapshot = payload.get(SESSION_SETTINGS_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict) or snapshot.get("agent_backend") != backend:
            continue
        if new := _replacement(moves, snapshot.get("model")):
            snapshot["model"] = new
            updates.append((rid, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    return updates


def rename_selections(backend: str, moves: dict[str, str], *, engine=None) -> None:
    engine = engine or get_cached_sqlite_engine()

    def apply():
        with engine.begin() as conn:
            # Explicit BEGIN: sqlite3's legacy driver does not begin on SELECT. WAL permits
            # concurrent writes during discovery. A stale snapshot cannot upgrade to writer:
            # retry_sqlite_write then rolls the entire attempt forward from a fresh snapshot.
            conn.exec_driver_sql("BEGIN DEFERRED")
            scopes = _plan_scopes(conn, backend, moves)
            definitions = _plan_definitions(conn, backend, moves)
            moved = len(scopes) + len(definitions)
            for old, new in moves.items():
                for table, owner in ((agents, agents.c.backend), (agent_sessions, agent_sessions.c.agent_backend)):
                    moved += conn.execute(
                        update(table).where(owner == backend, table.c.model == old).values(model=new)
                    ).rowcount
            for scope_id, changes in scopes:
                conn.execute(update(scope_settings).where(scope_settings.c.scope_id == scope_id).values(**changes))
            for rid, raw in definitions:
                conn.execute(update(run_definitions).where(run_definitions.c.id == rid).values(metadata_json=raw))
            if moved:
                mark_runtime_settings_changed(conn)

    retry_sqlite_write(apply)
