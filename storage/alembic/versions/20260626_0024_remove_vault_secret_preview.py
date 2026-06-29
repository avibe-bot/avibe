"""remove value-derived vault secret previews

Revision ID: 20260626_0024
Revises: 20260621_0023
Create Date: 2026-06-26
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "20260626_0024"
down_revision = "20260621_0023"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first()
    return row is not None


def _strip_preview(raw: str | None) -> tuple[bool, str | None]:
    if not raw:
        return False, raw
    try:
        payload: Any = json.loads(raw)
    except (TypeError, ValueError):
        return False, raw
    if not isinstance(payload, dict) or "preview" not in payload:
        return False, raw
    payload.pop("preview", None)
    return True, json.dumps(payload) if payload else None


def upgrade() -> None:
    if not _table_exists("vault_secrets"):
        return
    bind = op.get_bind()
    rows = list(bind.exec_driver_sql("select id, public_meta from vault_secrets").mappings())
    for row in rows:
        changed, public_meta = _strip_preview(row["public_meta"])
        if changed:
            bind.exec_driver_sql(
                "update vault_secrets set public_meta = ? where id = ?",
                (public_meta, row["id"]),
            )


def downgrade() -> None:
    # Value-derived previews are intentionally not recoverable.
    pass
