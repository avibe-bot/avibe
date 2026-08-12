"""preserve trusted media references across legacy sessions

Revision ID: 20260725_0037
Revises: 20260725_0036
Create Date: 2026-07-25
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "20260725_0037"
down_revision = "20260725_0036"
branch_labels = None
depends_on = None

_MEDIA_TOKEN_RE = re.compile(r"/(?:api|__show)/media/([A-Za-z0-9_-]+)")


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
        op.create_index(
            "ix_media_object_references_session",
            "media_object_references",
            ["session_id"],
        )

    bind.exec_driver_sql(
        """
        insert or ignore into media_object_references (token, session_id, created_at)
        select token, session_id, created_at
        from media_objects
        where session_id is not null
        """
    )
    if "messages" not in tables:
        return

    known_tokens = {
        str(row[0])
        for row in bind.exec_driver_sql("select token from media_objects")
    }
    references: dict[tuple[str, str], str] = {}
    rows = bind.exec_driver_sql(
        """
        select session_id, content_text, content_json, created_at
        from messages
        where author = 'agent'
          and session_id is not null
          and (content_text like '%/media/%' or content_json like '%/media/%')
        """
    )
    for session_id, content_text, content_json, created_at in rows:
        haystack = f"{content_text or ''}\n{content_json or ''}"
        for token in _MEDIA_TOKEN_RE.findall(haystack):
            if token in known_tokens:
                references[(token, str(session_id))] = str(created_at)
    if references:
        bind.execute(
            sa.text(
                """
                insert or ignore into media_object_references
                    (token, session_id, created_at)
                values (:token, :session_id, :created_at)
                """
            ),
            [
                {
                    "token": token,
                    "session_id": session_id,
                    "created_at": created_at,
                }
                for (token, session_id), created_at in references.items()
            ],
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
