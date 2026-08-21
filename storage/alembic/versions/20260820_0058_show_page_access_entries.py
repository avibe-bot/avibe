"""widen the Show Page Limited audience from emails to heterogeneous entries

Revision ID: 20260820_0058
Revises: 20260819_0057
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260820_0058"
down_revision = "20260819_0057"
branch_labels = None
depends_on = None

_ENTRIES_TABLE = "show_page_access_entries"
_LEGACY_EMAIL_TABLE = "show_page_authorized_emails"
_ORGANIZATION_INDEX = "uq_show_page_access_entries_organization"
_LOOKUP_INDEX = "ix_show_page_access_entries_lookup"
_LEGACY_EMAIL_INDEX = "ix_show_page_authorized_emails_email"


def _tables() -> set[str]:
    return {
        str(row[0])
        for row in op.get_bind()
        .exec_driver_sql("select name from sqlite_master where type = 'table'")
        .fetchall()
    }


def _create_entries_table() -> None:
    if _ENTRIES_TABLE not in _tables():
        op.create_table(
            _ENTRIES_TABLE,
            sa.Column(
                "page_id",
                sa.String(),
                sa.ForeignKey("show_pages.session_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("kind", sa.String(), primary_key=True),
            sa.Column("value", sa.String(), primary_key=True),
            sa.Column("organization_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "kind in ('email', 'group', 'organization')",
                name="ck_show_page_access_entries_kind",
            ),
            sa.CheckConstraint(
                "length(value) between 1 and 320",
                name="ck_show_page_access_entries_value_length",
            ),
            sa.CheckConstraint(
                "(kind = 'email' and organization_id is null) "
                "or (kind in ('group', 'organization') and organization_id is not null)",
                name="ck_show_page_access_entries_organization",
            ),
            sa.CheckConstraint(
                "kind <> 'organization' or value = organization_id",
                name="ck_show_page_access_entries_organization_value",
            ),
        )
    op.create_index(
        _LOOKUP_INDEX,
        _ENTRIES_TABLE,
        ["kind", "value"],
        if_not_exists=True,
    )
    op.create_index(
        _ORGANIZATION_INDEX,
        _ENTRIES_TABLE,
        ["page_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'organization'"),
        if_not_exists=True,
    )


def _create_legacy_email_table() -> None:
    if _LEGACY_EMAIL_TABLE not in _tables():
        op.create_table(
            _LEGACY_EMAIL_TABLE,
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
        _LEGACY_EMAIL_INDEX,
        _LEGACY_EMAIL_TABLE,
        ["normalized_email"],
        if_not_exists=True,
    )


def upgrade() -> None:
    _create_entries_table()

    # The email audience moves rather than forks: copy first, then retire the
    # source. ``insert or ignore`` keeps a replay -- unversioned databases are
    # stamped at the replay floor and run every later revision again -- from
    # duplicating or overwriting an entry that is already there.
    if _LEGACY_EMAIL_TABLE in _tables():
        op.execute(
            sa.text(
                f"insert or ignore into {_ENTRIES_TABLE} "
                "(page_id, kind, value, organization_id, created_at) "
                "select session_id, 'email', normalized_email, null, created_at "
                f"from {_LEGACY_EMAIL_TABLE}"
            )
        )
        op.drop_table(_LEGACY_EMAIL_TABLE)


def downgrade() -> None:
    _create_legacy_email_table()

    if _ENTRIES_TABLE in _tables():
        # Pre-0058 readers only understand emails. Group and organization
        # entries have no legacy representation, so they are dropped with the
        # table: the audience narrows and fails closed instead of being
        # silently widened into an email that was never granted.
        op.execute(
            sa.text(
                f"insert or ignore into {_LEGACY_EMAIL_TABLE} "
                "(session_id, normalized_email, created_at) "
                "select page_id, value, created_at "
                f"from {_ENTRIES_TABLE} where kind = 'email'"
            )
        )
        op.drop_table(_ENTRIES_TABLE)
