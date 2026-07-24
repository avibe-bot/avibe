"""add persisted project session pinning

Revision ID: 20260724_0034
Revises: 20260723_0033
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260724_0034"
down_revision = "20260723_0033"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _columns(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def _indexes(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma index_list("{table}")').fetchall()
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_sessions" not in _tables(bind):
        return
    if "pinned" not in _columns(bind, "agent_sessions"):
        op.add_column(
            "agent_sessions",
            sa.Column("pinned", sa.Integer(), nullable=False, server_default="0"),
        )
    if "ix_agent_sessions_scope_status_pinned_activity" not in _indexes(bind, "agent_sessions"):
        op.create_index(
            "ix_agent_sessions_scope_status_pinned_activity",
            "agent_sessions",
            ["scope_id", "status", "pinned", "last_active_at", "created_at", "id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_sessions" not in _tables(bind):
        return
    if "ix_agent_sessions_scope_status_pinned_activity" in _indexes(bind, "agent_sessions"):
        op.drop_index("ix_agent_sessions_scope_status_pinned_activity", table_name="agent_sessions")
    if "pinned" in _columns(bind, "agent_sessions"):
        op.drop_column("agent_sessions", "pinned")
