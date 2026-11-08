"""Cover reverse-dispatch probes by both Session predicates.

The report-hint fallback asks whether a dispatcher has sent an Agent Run into a
specific recipient Session. The existing source-actor index leaves recipient as
a residual filter, so unsuccessful probes scan the dispatcher's whole history.

Revision ID: 20261108_0082
Revises: 20261108_0076
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "20261108_0082"
down_revision = "20261108_0076"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"


def upgrade() -> None:
    op.create_index(
        "ix_agent_runs_dispatch_probe",
        "agent_runs",
        ["source_kind", "source_actor", "session_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_dispatch_probe", table_name="agent_runs", if_exists=True)
