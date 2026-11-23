"""Move future selections in one SQLite transaction. Never edit execution records."""

from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import bindparam, case, func, or_, select, update

from config.v2_settings import LEGACY_ROUTING_ALIASES
from core.handlers.model_hub.identifiers import opencode_menu_model_id
from core.vibe_agents import normalize_agent_name, resolve_effective_default_agent
from storage.db import get_cached_sqlite_engine, retry_sqlite_write
from storage.models import agents, agent_sessions, run_definitions, scope_settings
from storage.session_reclaim import DEFINITION_AGENT_BINDING_REVISION_KEY, SESSION_SETTINGS_SNAPSHOT_KEY
from storage.settings_revision import mark_runtime_settings_changed
from storage.agent_session_rows import UNOWNED_SESSION_BACKENDS

# Characters Python str.strip() removes. SQLite trim defaults to ASCII space only.
_PIN_WHITESPACE = "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def _object(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_field(column, path):
    return func.json_extract(case((func.json_valid(column), column)), path)


def _owner_lookup(conn):
    @lru_cache(None)
    def backend(agent_id, name):
        if agent_id:
            return conn.scalar(select(agents.c.backend).where(agents.c.id == agent_id))
        if name:
            try:
                normalized = normalize_agent_name(name)
            except ValueError:
                return None
            # Archived and disabled Agents still own durable bindings.
            return conn.scalar(select(agents.c.backend).where(agents.c.normalized_name == normalized))
        default = resolve_effective_default_agent(conn)
        return default.backend if default else None

    def lookup(row):
        bound = str(row.get("agent_backend") or "").strip()
        if bound not in UNOWNED_SESSION_BACKENDS:
            return bound
        return backend(str(row.get("agent_id") or ""), str(row.get("agent_name") or ""))

    return lookup


def _plan_scopes(conn, backend, candidates, replacement, owner):
    table = scope_settings.c
    generic = ("model", "model_override")
    legacy = f"{backend}_model"
    keys = (*generic, legacy) if legacy in LEGACY_ROUTING_ALIASES else generic
    query = select(table.scope_id, table.agent_name, table.model, table.settings_json).where(
        or_(
            candidates(table.model), *(candidates(_json_field(table.settings_json, f"$.routing.{key}")) for key in keys)
        )
    )
    updates = []
    for scope_id, agent_name, model, raw in conn.execute(query):
        payload = _object(raw)
        routing = payload.get("routing")
        if not isinstance(routing, dict):
            routing = {}
        name = routing.get("agent_name") or routing.get("agent") or agent_name
        is_owner = owner({"agent_name": name}) == backend
        changes = {}
        if is_owner and (new := replacement(model)):
            changes["model"] = new
        changed_fields = {
            key: new for key in keys if (is_owner or key == legacy) and (new := replacement(routing.get(key)))
        }
        if changed_fields:
            routing.update(changed_fields)
            changes["settings_json"] = _dump(payload)
        if changes:
            updates.append((scope_id, {"agent_name": agent_name, "model": model, "settings_json": raw}, changes))
    return updates


def _plan_definitions(conn, backend, candidates, replacement, owner):
    table = run_definitions.c
    query = select(table.id, table.metadata_json).where(
        candidates(_json_field(table.metadata_json, f"$.{SESSION_SETTINGS_SNAPSHOT_KEY}.model"))
    )
    updates = []
    for rid, raw in conn.execute(query):
        payload = _object(raw)
        snapshot = payload.get(SESSION_SETTINGS_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict) or owner(snapshot) != backend:
            continue
        if new := replacement(snapshot.get("model")):
            snapshot["model"] = new
            # Fence full-metadata edits derived from the previous task snapshot.
            payload[DEFINITION_AGENT_BINDING_REVISION_KEY] = uuid4().hex
            updates.append((rid, {"metadata_json": raw}, {"metadata_json": _dump(payload)}))
    return updates


class SelectionChanged(Exception):
    """A candidate changed before its keyed update; retry the whole draft."""


def _apply_by_key(conn, table, key, updates):
    groups = defaultdict(list)
    for identity, before, changes in updates:
        groups[tuple(before), tuple(changes)].append(
            {
                "_pk": identity,
                **{f"before_{k}": v for k, v in before.items()},
                **{f"after_{k}": v for k, v in changes.items()},
            }
        )
    for (before, fields), params in groups.items():
        statement = (
            update(table)
            .where(key == bindparam("_pk"), *(table.c[k].is_(bindparam(f"before_{k}")) for k in before))
            .values({k: bindparam(f"after_{k}") for k in fields})
        )
        if conn.execute(statement, params).rowcount != len(params):
            raise SelectionChanged


def rename_selections(backend: str, moves: dict[str, str], *, opencode_menu_ids=None, engine=None) -> None:
    engine = engine or get_cached_sqlite_engine()
    targets = frozenset(moves.values())
    # One bound parameter even for a large whole-list edit. Exact resolution in
    # Python excludes qualified OpenCode selections that have their own menu row.
    old = select(func.json_each(_dump(list(moves))).table_valued("value").c.value)

    def candidates(value):
        value = func.trim(value, _PIN_WHITESPACE)
        if opencode_menu_ids is None:
            return value.in_(old)
        return or_(value.in_(old), func.substr(value, func.instr(value, "/") + 1).in_(old))

    def replacement(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value in targets:
            return None
        row_id = opencode_menu_model_id(value, opencode_menu_ids) if opencode_menu_ids is not None else value
        return moves.get(row_id)

    def apply():
        with engine.connect() as conn:
            # data_version is connection-local and observes all commits by other
            # connections, including reclaim and default-Agent changes. Compare
            # on this SAME connection only after acquiring the writer lock.
            generation = conn.exec_driver_sql("PRAGMA main.data_version").scalar_one()
            owner = _owner_lookup(conn)
            scopes = _plan_scopes(conn, backend, candidates, replacement, owner)
            definitions = _plan_definitions(conn, backend, candidates, replacement, owner)
            agent_rows = []
            for row in conn.execute(
                select(agents.c.id, agents.c.model).where(agents.c.backend == backend, candidates(agents.c.model))
            ):
                if new := replacement(row.model):
                    agent_rows.append((row.id, {"model": row.model, "backend": backend}, {"model": new}))
            session_rows = []
            cols = agent_sessions.c
            for row in conn.execute(
                select(cols.id, cols.agent_name, cols.agent_id, cols.agent_backend, cols.model).where(
                    func.trim(func.coalesce(cols.agent_backend, ""), _PIN_WHITESPACE).in_(
                        (backend, *UNOWNED_SESSION_BACKENDS)
                    ),
                    candidates(cols.model),
                )
            ).mappings():
                if owner(row) == backend and (new := replacement(row.model)):
                    session_rows.append(
                        (
                            row.id,
                            {key: row[key] for key in ("agent_id", "agent_name", "agent_backend", "model")},
                            {"model": new},
                        )
                    )
            # End the read transaction, not the connection. Only version checks
            # and primary-key writes hold the writer; no discovery under lock.
            conn.commit()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if conn.exec_driver_sql("PRAGMA main.data_version").scalar_one() != generation:
                raise SelectionChanged
            _apply_by_key(conn, agents, agents.c.id, agent_rows)
            _apply_by_key(conn, agent_sessions, cols.id, session_rows)
            _apply_by_key(conn, scope_settings, scope_settings.c.scope_id, scopes)
            _apply_by_key(conn, run_definitions, run_definitions.c.id, definitions)
            if agent_rows or session_rows or scopes or definitions:
                mark_runtime_settings_changed(conn)
            conn.commit()

    for attempt in range(3):
        try:
            return retry_sqlite_write(apply)
        except SelectionChanged:
            if attempt == 2:
                raise
