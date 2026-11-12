"""Add the durable, FTS-independent message-search change journal.

Only empty ordinary tables and journal-only triggers are installed at startup.
The optional derived FTS index is created/backfilled by post-readiness maintenance.
Old binaries keep writing through the journal without needing an FTS5 module.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261112_0086"
down_revision = "20261112_0085"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "message_search_dirty",
        sa.Column("message_id", sa.Text(), primary_key=True, nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "message_search_documents",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False, unique=True),
        sa.Column("oversized", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    op.create_index("ix_message_search_documents_oversized", "message_search_documents", ["oversized"], unique=False, if_not_exists=True)
    op.create_table(
        "message_search_state",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("ready", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    op.execute("""CREATE TRIGGER IF NOT EXISTS message_search_insert AFTER INSERT ON messages
        WHEN EXISTS (SELECT 1 FROM message_search_state WHERE id = 1) BEGIN
        INSERT OR IGNORE INTO message_search_dirty (message_id) VALUES (new.id);
    END""")
    op.execute("""CREATE TRIGGER IF NOT EXISTS message_search_update AFTER UPDATE OF id, content_text ON messages
        WHEN EXISTS (SELECT 1 FROM message_search_state WHERE id = 1) BEGIN
        INSERT OR IGNORE INTO message_search_dirty (message_id) VALUES (old.id);
        INSERT OR IGNORE INTO message_search_dirty (message_id) VALUES (new.id);
    END""")
    op.execute("""CREATE TRIGGER IF NOT EXISTS message_search_delete AFTER DELETE ON messages
        WHEN EXISTS (SELECT 1 FROM message_search_state WHERE id = 1) BEGIN
        INSERT OR IGNORE INTO message_search_dirty (message_id) VALUES (old.id);
    END""")


def downgrade() -> None:
    # Explicit fixture downgrade only; supported binary rollback keeps the epoch
    # and journal in place. Drop triggers before their independent target table.
    op.execute("DROP TRIGGER message_search_insert")
    op.execute("DROP TRIGGER message_search_update")
    op.execute("DROP TRIGGER message_search_delete")
    op.execute("DROP TABLE IF EXISTS message_search_fts")
    op.drop_table("message_search_state")
    op.drop_table("message_search_documents")
    op.drop_table("message_search_dirty")
