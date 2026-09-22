"""Take a credential's address out of persisted model selections.

A model id is a *selection* in these stores, not a record of one: a turn reads
them back to decide what it asks for. The precedence a turn resolves is the
session's pin, then the channel's routing override, then the Vibe Agent's own
model, so all three are selections and all three can hold an id an older
release offered — the menu is where a user picked one, and every one of these
rows is a copy of what the menu said at the time. A reclaimed session's
settings snapshot is a fourth: a selection with no row left to sit on, held
until the Session that replaces it is created.

Repairing the Model Hub catalog alone would leave them naming a model that no
longer exists under that name. Repairing them is the other half of the same
write: one id, every place it is a key.

``core.vibe_agents._rebind_agent_references`` already solves this shape for a
renamed Agent, and its reach is the one this call matches: every persisted copy
of the identifier, plus the settings revision that tells a live reader its
cache is stale. A write that lands in the database but not in the revision is
half a repair — ``V2SettingsStore.maybe_reload`` never looks, so the turn keeps
resolving the address out of memory.

A run's recorded model and a usage ledger's key are deliberately *not* here.
Those are records of a call that was made, not inputs to one that will be, and
rewriting them would restate history.

Ownership is proven by the caller, never guessed: only an address the engine
actually mints for a bound credential is removed, so an upstream identity that
happens to be spelled like one is untouched.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from core.handlers.model_hub.identifiers import model_id_without_credential_addresses
from storage.db import get_cached_sqlite_engine
from storage.models import agent_sessions, agents, run_definitions, scope_settings
from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY
from storage.settings_revision import mark_runtime_settings_changed

logger = logging.getLogger(__name__)

# Every key a scope's routing payload can spell a model selection under. The
# payload is read before the column (``_routing_from_row``), so repairing the
# column alone would leave the value a turn actually resolves.
_ROUTING_MODEL_KEYS = (
    "model",
    "model_override",
    "opencode_model",
    "claude_model",
    "codex_model",
)

# Table, primary key, and the column holding the selection.
_SELECTION_COLUMNS = (
    (agents, agents.c.id, agents.c.model),
    (scope_settings, scope_settings.c.scope_id, scope_settings.c.model),
    (agent_sessions, agent_sessions.c.id, agent_sessions.c.model),
)


def remove_credential_addresses_from_selections(
    addresses: Collection[str],
    *,
    engine: Engine | None = None,
) -> int:
    """Rewrite every persisted selection carrying one of these addresses.

    ``addresses`` is the set the Model Hub repair proved for its Sources. An
    address absent from it is not an address as far as this call is concerned.

    Returns how many stored values were rewritten. Idempotent: a second call
    with the same set finds nothing left to move.
    """

    known = frozenset(value for value in addresses if value)
    if not known:
        return 0
    engine = engine or get_cached_sqlite_engine()
    moved = 0
    scope_moved = 0
    with engine.begin() as connection:
        for table, key, column in _SELECTION_COLUMNS:
            rows = connection.execute(
                select(key, column).where(column.is_not(None))
            ).all()
            for row_key, value in rows:
                if not isinstance(value, str):
                    continue
                identity = model_id_without_credential_addresses(value, known)
                if identity == value:
                    continue
                connection.execute(
                    update(table).where(key == row_key).values({column.key: identity})
                )
                moved += 1
                if table is scope_settings:
                    scope_moved += 1
        routing_moved = _repaired_routing_payloads(connection, known)
        moved += routing_moved
        scope_moved += routing_moved
        moved += _repaired_session_snapshots(connection, known)
        if scope_moved:
            # Published in the same transaction, so a reader sees the repaired
            # rows and the new revision together or neither. Without it the
            # settings store keeps serving what it cached at load: the IM turn
            # resolves the stale override, and the next same-scope save writes
            # it back.
            mark_runtime_settings_changed(connection)
    return moved


def _repaired_routing_payloads(connection: Any, known: frozenset[str]) -> int:
    """Rewrite the model a scope's routing payload spells, under any of its keys."""

    rows = connection.execute(
        select(scope_settings.c.scope_id, scope_settings.c.settings_json)
    ).all()
    moved = 0
    for scope_id, raw in rows:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            # A payload this call cannot read is one it cannot repair. Leaving
            # it is exactly the state the previous release was already in.
            logger.debug("model hub: unreadable routing payload for scope %s", scope_id)
            continue
        routing = payload.get("routing") if isinstance(payload, dict) else None
        if not isinstance(routing, dict):
            continue
        repaired = dict(routing)
        changed = 0
        for routing_key in _ROUTING_MODEL_KEYS:
            value = routing.get(routing_key)
            if not isinstance(value, str) or not value:
                continue
            identity = model_id_without_credential_addresses(value, known)
            if identity == value:
                continue
            repaired[routing_key] = identity
            changed += 1
        if not changed:
            continue
        connection.execute(
            update(scope_settings)
            .where(scope_settings.c.scope_id == scope_id)
            .values(
                settings_json=json.dumps(
                    {**payload, "routing": repaired},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        )
        moved += changed
    return moved


def _repaired_session_snapshots(connection: Any, known: frozenset[str]) -> int:
    """Rewrite the model a reclaimed Session's settings snapshot still pins.

    ``run_definitions`` carries no model column, so a ``create_once`` Task that
    outlived its Session keeps the selection here and
    ``_rebind_create_once_session`` writes it straight onto the replacement
    Session. A snapshot left addressed re-seeds the exact value this repair
    removed, onto a row created after the catalog stopped offering it, and
    nothing runs the repair a second time.

    Soft-deleted definitions are included. The rename path skips them because
    it keeps live bindings pointed at the right Agent; this one makes the
    address absent from storage, and a row that is only marked deleted still
    stores it.
    """

    rows = connection.execute(
        select(run_definitions.c.id, run_definitions.c.metadata_json)
    ).all()
    moved = 0
    for definition_id, raw in rows:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug(
                "model hub: unreadable definition metadata for %s", definition_id
            )
            continue
        snapshot = (
            payload.get(SESSION_SETTINGS_SNAPSHOT_KEY)
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(snapshot, dict):
            continue
        value = snapshot.get("model")
        if not isinstance(value, str) or not value:
            continue
        identity = model_id_without_credential_addresses(value, known)
        if identity == value:
            continue
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(
                metadata_json=json.dumps(
                    {
                        **payload,
                        SESSION_SETTINGS_SNAPSHOT_KEY: {**snapshot, "model": identity},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        )
        moved += 1
    return moved
