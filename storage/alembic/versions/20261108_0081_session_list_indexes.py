"""Cover the active Session list's filters and recent-activity ordering.

Revision ID: 20261108_0081
Revises: 20261108_0079
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "20261108_0081"
down_revision = "20261108_0079"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    # Ordinary browsing filters active foreground Sessions and optionally a
    # Project before keeping the recent-activity order. These indexes avoid a
    # growing temporary sort for those pages; the Run index bounds --mine's
    # reverse relationship lookup (which also includes background workers).
    op.create_index(
        "ix_agent_runs_source_session",
        "agent_runs",
        ["source_kind", "session_id", "source_actor"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_agent_sessions_status_visibility_activity",
        "agent_sessions",
        ["status", "visibility", "last_active_at", "created_at", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_agent_sessions_scope_status_visibility_activity",
        "agent_sessions",
        ["scope_id", "status", "visibility", "last_active_at", "created_at", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_runs_source_session",
        table_name="agent_runs",
        if_exists=True,
    )
    op.drop_index(
        "ix_agent_sessions_scope_status_visibility_activity",
        table_name="agent_sessions",
        if_exists=True,
    )
    op.drop_index(
        "ix_agent_sessions_status_visibility_activity",
        table_name="agent_sessions",
        if_exists=True,
    )
