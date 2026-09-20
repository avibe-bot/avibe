"""record which sessions a media object is trusted by

Revision ID: 20260725_0037
Revises: 20260725_0036
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0037"
down_revision = "20260725_0036"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "media_objects" not in tables or "agent_sessions" not in tables:
        return
    if "media_object_references" not in tables:
        op.create_table(
            "media_object_references",
            sa.Column("token", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(
                ["token"],
                ["media_objects.token"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["agent_sessions.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("token", "session_id"),
        )
    # Outside the table guard on purpose. A run interrupted between the table and
    # its index leaves the table present, and a guard that covers both would then
    # skip the index forever -- permanently, once 20260819_0056 replays this
    # revision and stamps, because a stamped revision never runs again.
    op.create_index(
        "ix_media_object_references_session",
        "media_object_references",
        ["session_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "media_object_references" not in _tables(bind):
        return
    op.drop_index(
        "ix_media_object_references_session",
        table_name="media_object_references",
    )
    op.drop_table("media_object_references")
