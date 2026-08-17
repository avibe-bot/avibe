"""make Show Page audience local and independent from availability

Revision ID: 20260817_0055
Revises: 20260815_0054
Create Date: 2026-08-17
"""

from __future__ import annotations

import secrets
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

revision = "20260817_0055"
down_revision = "20260815_0054"
branch_labels = None
depends_on = None

_SHARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,62}[A-Za-z0-9]$")


def _tables() -> set[str]:
    return {
        str(row[0])
        for row in op.get_bind()
        .exec_driver_sql("select name from sqlite_master where type = 'table'")
        .fetchall()
    }


def _indexes(table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind()
        .exec_driver_sql(f'pragma index_list("{table_name}")')
        .fetchall()
    }


def _columns(table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind()
        .exec_driver_sql(f'pragma table_info("{table_name}")')
        .fetchall()
    }


def _new_share_id() -> str:
    while True:
        candidate = secrets.token_urlsafe(8).strip("_-")
        if _SHARE_ID_PATTERN.fullmatch(candidate):
            return candidate


def _allocate_missing_share_ids() -> None:
    bind = op.get_bind()
    session_ids = [
        str(row[0])
        for row in bind.exec_driver_sql(
            "select session_id from show_pages where share_id is null order by session_id"
        ).fetchall()
    ]
    for session_id in session_ids:
        while True:
            try:
                bind.execute(
                    sa.text(
                        "update show_pages set share_id = :share_id "
                        "where session_id = :session_id and share_id is null"
                    ),
                    {"session_id": session_id, "share_id": _new_share_id()},
                )
                break
            except IntegrityError:
                continue


def _ensure_authorized_email_table() -> None:
    if "show_page_authorized_emails" not in _tables():
        op.create_table(
            "show_page_authorized_emails",
            sa.Column(
                "session_id",
                sa.String(),
                sa.ForeignKey("show_pages.session_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("normalized_email", sa.String(), primary_key=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "length(normalized_email) between 3 and 320",
                name="ck_show_page_authorized_emails_length",
            ),
        )
    op.create_index(
        "ix_show_page_authorized_emails_email",
        "show_page_authorized_emails",
        ["normalized_email"],
        if_not_exists=True,
    )


def upgrade() -> None:
    columns = _columns("show_pages")
    # Unversioned databases created from current metadata are stamped at the
    # replay floor before later migrations run. Their ShowAccess schema is
    # already current, so this revision must be replay-safe.
    if "access_mode" in columns and "visibility" not in columns:
        _allocate_missing_share_ids()
        op.create_index(
            "ix_show_pages_access_mode",
            "show_pages",
            ["access_mode"],
            if_not_exists=True,
        )
        _ensure_authorized_email_table()
        return

    op.add_column(
        "show_pages",
        sa.Column("access_mode", sa.String(), nullable=True),
    )
    op.add_column(
        "show_pages",
        sa.Column("access_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        sa.text(
            "update show_pages set access_mode = case visibility "
            "when 'public' then 'public' else 'private' end"
        )
    )
    # Legacy offline rows did not retain their previous audience. Preserve the
    # operational state and fail closed instead of guessing that a retained
    # share_id meant the page used to be public.
    op.execute(
        sa.text(
            "update show_pages set offline_at = coalesce(offline_at, updated_at, created_at) "
            "where visibility = 'offline'"
        )
    )
    _allocate_missing_share_ids()

    if "ix_show_pages_visibility" in _indexes("show_pages"):
        op.drop_index("ix_show_pages_visibility", table_name="show_pages")
    with op.batch_alter_table("show_pages") as batch_op:
        batch_op.alter_column(
            "access_mode",
            existing_type=sa.String(),
            nullable=False,
            server_default="private",
        )
        batch_op.create_check_constraint(
            "ck_show_pages_access_mode",
            "access_mode in ('private', 'limited', 'public')",
        )
        batch_op.create_check_constraint(
            "ck_show_pages_access_revision",
            "access_revision >= 0",
        )
        batch_op.drop_column("visibility")
    op.create_index(
        "ix_show_pages_access_mode",
        "show_pages",
        ["access_mode"],
        if_not_exists=True,
    )

    _ensure_authorized_email_table()


def downgrade() -> None:
    if "show_page_authorized_emails" in _tables():
        op.drop_table("show_page_authorized_emails")

    op.add_column(
        "show_pages",
        sa.Column("visibility", sa.String(), nullable=True),
    )
    # Older releases cannot represent Limited. Mapping it to private disables
    # the shared route while retaining the inactive stable slug.
    op.execute(
        sa.text(
            "update show_pages set visibility = case "
            "when offline_at is not null then 'offline' "
            "when access_mode = 'public' then 'public' else 'private' end"
        )
    )
    if "ix_show_pages_access_mode" in _indexes("show_pages"):
        op.drop_index("ix_show_pages_access_mode", table_name="show_pages")
    with op.batch_alter_table("show_pages") as batch_op:
        batch_op.drop_constraint("ck_show_pages_access_revision", type_="check")
        batch_op.drop_constraint("ck_show_pages_access_mode", type_="check")
        batch_op.alter_column(
            "visibility",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.drop_column("access_revision")
        batch_op.drop_column("access_mode")
    op.create_index(
        "ix_show_pages_visibility",
        "show_pages",
        ["visibility"],
        if_not_exists=True,
    )
