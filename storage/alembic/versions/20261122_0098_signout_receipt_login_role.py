"""Record on a sign-out receipt the role its device signed in with.

A receipt outlives its device's row. For a viewer on this computer itself the
device list sorts "mine" from "others" by that role, so the receipt keeps it.
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
    op.add_column("remote_signout_receipts", sa.Column("login_role", sa.String(), nullable=True))


def downgrade() -> None:
    # A batch rebuild: SQLite before 3.35 cannot DROP COLUMN.
    with op.batch_alter_table("remote_signout_receipts") as batch:
        batch.drop_column("login_role")
