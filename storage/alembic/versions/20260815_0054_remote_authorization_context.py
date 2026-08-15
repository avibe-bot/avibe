"""persist current remote authorization contexts

Revision ID: 20260815_0054
Revises: 20260812_0053
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260815_0054"
down_revision = "20260812_0053"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind()
        .exec_driver_sql('pragma table_info("remote_access_authorizations")')
        .fetchall()
    }


def _indexes() -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind()
        .exec_driver_sql('pragma index_list("remote_access_authorizations")')
        .fetchall()
    }


def upgrade() -> None:
    columns = _columns()
    additions = (
        ("email", sa.String()),
        ("scope_kind", sa.String()),
        ("scope_ref", sa.String()),
        ("authorization_state", sa.String()),
        ("last_checked_at", sa.Integer()),
        ("updated_at", sa.Integer()),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column(
                "remote_access_authorizations",
                sa.Column(name, column_type, nullable=True),
            )

    # Scoped records are durable and therefore do not have a row expiry. SQLite
    # requires a table rebuild to relax the legacy NOT NULL constraint.
    expires = next(
        (
            row
            for row in op.get_bind()
            .exec_driver_sql('pragma table_info("remote_access_authorizations")')
            .fetchall()
            if str(row[1]) == "expires_at"
        ),
        None,
    )
    if expires is not None and int(expires[3]) == 1:
        with op.batch_alter_table("remote_access_authorizations") as batch_op:
            batch_op.alter_column(
                "expires_at",
                existing_type=sa.Integer(),
                nullable=True,
            )

    if "ux_remote_access_authorizations_scope" not in _indexes():
        op.create_index(
            "ux_remote_access_authorizations_scope",
            "remote_access_authorizations",
            ["instance_id", "subject", "scope_kind", "scope_ref"],
            unique=True,
            sqlite_where=sa.text("scope_kind is not null and scope_ref is not null"),
        )


def downgrade() -> None:
    if "ux_remote_access_authorizations_scope" in _indexes():
        op.drop_index(
            "ux_remote_access_authorizations_scope",
            table_name="remote_access_authorizations",
        )
    # Pre-0054 readers require an expiry and cannot represent scoped durable
    # contexts. Remove only the new shape before restoring the legacy NOT NULL
    # contract; legacy random-reference rows remain untouched.
    op.execute(
        sa.text(
            "delete from remote_access_authorizations "
            "where scope_kind is not null or scope_ref is not null"
        )
    )
    columns = _columns()
    removable = (
        "updated_at",
        "last_checked_at",
        "authorization_state",
        "scope_ref",
        "scope_kind",
        "email",
    )
    with op.batch_alter_table("remote_access_authorizations") as batch_op:
        for name in removable:
            if name in columns:
                batch_op.drop_column(name)
        batch_op.alter_column(
            "expires_at",
            existing_type=sa.Integer(),
            nullable=False,
        )
