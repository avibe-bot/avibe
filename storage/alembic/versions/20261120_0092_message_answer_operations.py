"""Persist message answer operation identities and receipts without changing execution."""

from alembic import op
import sqlalchemy as sa

revision = "20261120_0092"
down_revision = "20261118_0091"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "message_answer_operations",
        sa.Column("session_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("principal", sa.String(), primary_key=True, nullable=False),
        sa.Column("operation_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("choice", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=True),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        "ix_message_answer_operations_request",
        "message_answer_operations",
        ["session_id", "request_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_message_answer_operations_request", table_name="message_answer_operations")
    op.drop_table("message_answer_operations")
