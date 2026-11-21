"""Keep private per-login references and a transactional account sign-out outbox."""

from alembic import op
import sqlalchemy as sa

revision = "20261121_0096"
down_revision = "20261121_0095"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "remote_account_refs",
        sa.Column("sid", sa.String(), primary_key=True),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("reference", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("reason", sa.String()),
        if_not_exists=True,
    )
    op.create_index("ix_remote_account_refs_digest", "remote_account_refs", ['digest'], if_not_exists=True)
    op.create_index("ix_remote_account_refs_epoch", "remote_account_refs", ['epoch'], if_not_exists=True)
    op.create_index("ix_remote_account_refs_expires_at", "remote_account_refs", ['expires_at'], if_not_exists=True)
    op.create_table(
        "remote_account_revocations",
        sa.Column("digest", sa.String(), primary_key=True),
        sa.Column("epoch", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("reference", sa.String()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("next_at", sa.Integer()),
        sa.Column("forget_at", sa.Integer()),
        if_not_exists=True,
    )
    op.create_index("ix_rar_forget", "remote_account_revocations", ['forget_at'], if_not_exists=True)
    op.create_index("ix_rar_next", "remote_account_revocations", ['epoch', 'next_at'], if_not_exists=True)
    op.create_index("ix_remote_account_revocations_epoch", "remote_account_revocations", ['epoch'], if_not_exists=True)
    op.create_table(
        "remote_account_pairings",
        sa.Column("epoch", sa.String(), primary_key=True),
        sa.Column("blocked_until", sa.Integer(), nullable=False),
        sa.Column("refused", sa.Integer(), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "remote_signout_receipts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("binding", sa.String(), nullable=False),
        sa.Column("subject", sa.String()),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer()),
        if_not_exists=True,
    )
    op.create_index("ix_rsr_binding", "remote_signout_receipts", ['binding', 'created_at', 'id'], if_not_exists=True)
    op.create_index("ix_rsr_expiry", "remote_signout_receipts", ['expires_at'], if_not_exists=True)
    op.create_index("ix_rsr_subject", "remote_signout_receipts", ['binding', 'subject', 'created_at', 'id'], if_not_exists=True)
    op.create_table(
        "remote_signout_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt", sa.String(), nullable=False),
        sa.Column("job", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String()),
        sa.Column("count", sa.Integer(), nullable=False),
        if_not_exists=True,
    )
    op.create_index("ix_remote_signout_items_job", "remote_signout_items", ['job'], if_not_exists=True)
    op.create_index("ix_remote_signout_items_receipt", "remote_signout_items", ['receipt'], if_not_exists=True)


    op.create_table(
        'device_health_owner_checks',
        sa.Column('mobile_id', sa.String(), primary_key=True),
        sa.Column('operation', sa.String(), nullable=False),
        sa.Column('epoch', sa.String(), nullable=False),
        sa.Column('binding', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('device_id', sa.String(), nullable=False),
        sa.Column('owner_email', sa.String(), nullable=False),
        sa.Column('linked_at', sa.String(), nullable=False),
        sa.Column('next_at', sa.Integer(), nullable=False), sa.Column('expires_at', sa.Integer(), nullable=False),
        if_not_exists=True,
    )
    op.create_index('ix_device_health_owner_checks_next_at', 'device_health_owner_checks', ['next_at'], if_not_exists=True)


def downgrade() -> None:
    op.drop_table('device_health_owner_checks')
    op.drop_table("remote_signout_items")
    op.drop_table("remote_signout_receipts")
    op.drop_table("remote_account_pairings")
    op.drop_table("remote_account_revocations")
    op.drop_table("remote_account_refs")
