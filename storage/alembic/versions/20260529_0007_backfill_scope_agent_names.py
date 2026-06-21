"""retire legacy scope backend routing backfill

Revision ID: 20260529_0007
Revises: 20260526_0006
Create Date: 2026-05-29
"""

from __future__ import annotations

import json
import re
from typing import Any

from alembic import op

revision = "20260529_0007"
down_revision = "20260526_0006"
branch_labels = None
depends_on = None

_BACKENDS = {"opencode", "claude", "codex"}


def upgrade() -> None:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")}
    if "scope_settings" not in tables:
        return

    rows = bind.exec_driver_sql(
        "select scope_id, settings_json from scope_settings",
    ).fetchall()
    for scope_id, settings_json in rows:
        routing = _routing_payload(settings_json)
        if not routing:
            continue
        agent_name = _optional_str(routing.get("agent_name") or routing.get("agent"))
        if not agent_name:
            continue
        backend = _backend_for_agent_name(bind, agent_name)
        if backend is None:
            continue
        variant = _optional_str(routing.get(f"{backend}_agent"))
        model = _optional_str(routing.get("model") or routing.get("model_override"))
        effort = _optional_str(routing.get("reasoning_effort") or routing.get("reasoning_effort_override"))
        bind.exec_driver_sql(
            """
            update scope_settings
               set agent_name = coalesce(nullif(agent_name, ''), ?),
                   agent_variant = coalesce(nullif(agent_variant, ''), ?),
                   model = coalesce(nullif(model, ''), ?),
                   reasoning_effort = coalesce(nullif(reasoning_effort, ''), ?)
             where scope_id = ?
            """,
            (agent_name, variant, model, effort, scope_id),
        )


def downgrade() -> None:
    pass


def _routing_payload(settings_json: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(settings_json or "{}")
    except (TypeError, ValueError):
        return {}
    routing = payload.get("routing") if isinstance(payload, dict) else None
    return routing if isinstance(routing, dict) else {}


def _backend_for_agent_name(bind, agent_name: str) -> str | None:
    if agent_name in _BACKENDS:
        return agent_name
    if not _has_table(bind, "agents"):
        return None
    row = bind.exec_driver_sql(
        "select backend from agents where name = ? or normalized_name = ? limit 1",
        (agent_name, _normalize_agent_name(agent_name)),
    ).fetchone()
    return str(row[0]) if row and row[0] in _BACKENDS else None


def _has_table(bind, table: str) -> bool:
    row = bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
        (table,),
    ).fetchone()
    return row is not None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_agent_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
