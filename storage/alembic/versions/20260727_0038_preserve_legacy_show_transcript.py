"""preserve legacy Show transcript rows

Revision ID: 20260727_0038
Revises: 20260726_0037
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260727_0038"
down_revision = "20260726_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "update messages set type = 'notify' "
            "where type = 'assistant' "
            "and case when json_valid(metadata_json) "
            "then json_extract(metadata_json, '$.source') end = 'show_page'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "update messages set type = 'assistant' "
            "where type = 'notify' "
            "and case when json_valid(metadata_json) "
            "then json_extract(metadata_json, '$.source') end = 'show_page'"
        )
    )
