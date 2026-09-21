"""Take a credential's address out of persisted model selections.

A model id is a *selection* in these tables, not a record of one: a turn reads
them back to decide what it asks for. The precedence a turn resolves is the
session's pin, then the channel's routing override, then the Vibe Agent's own
model, so all three are selections and all three can hold an id an older
release offered — the menu is where a user picked one, and every one of these
rows is a copy of what the menu said at the time.

Repairing the Model Hub catalog alone would leave them naming a model that no
longer exists under that name. Repairing them is the other half of the same
write: one id, every place it is a key.

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
from storage.models import agent_sessions, agents, scope_settings

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
        moved += _repaired_routing_payloads(connection, known)
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
