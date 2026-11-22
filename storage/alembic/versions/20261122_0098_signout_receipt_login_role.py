"""Record with a sign-out receipt the role its device signed in with.

A receipt outlives its device's row. For a viewer on this computer itself the
device list sorts "mine" from "others" by that role, so it is kept beside the
receipt. A table of its own, as ``device_session_extensions`` is for sessions:
adding a column cannot be both a literal operation and safe to replay on a
database that already has the head shape.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261122_0098"
down_revision = "20261121_0097"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "remote_signout_receipt_roles",
        sa.Column("receipt", sa.String(), primary_key=True),
        sa.Column("login_role", sa.String(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("remote_signout_receipt_roles", if_exists=True)
