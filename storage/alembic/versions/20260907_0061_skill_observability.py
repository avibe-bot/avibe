"""Local Skill observations and daily usage.

Revision ID: 20260907_0061
Revises: 20260821_0060
"""

from __future__ import annotations

import sqlite3

from alembic import op

revision = "20260907_0061"
down_revision = "20260821_0060"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_DDL = """
-- Contract: harness-skill-observability.md.

CREATE TABLE IF NOT EXISTS skill_usage_daily (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    scope_id TEXT REFERENCES scopes(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES agent_sessions(id) ON DELETE CASCADE,
    skill_key TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    skill_revision TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    trigger_kind TEXT NOT NULL DEFAULT 'unknown',
    platform TEXT NOT NULL DEFAULT 'unknown',
    avibe_version TEXT NOT NULL,
    catalog_offer_count INTEGER NOT NULL DEFAULT 0,
    load_success_count INTEGER NOT NULL DEFAULT 0,
    load_failure_count INTEGER NOT NULL DEFAULT 0,
    load_duration_samples INTEGER NOT NULL DEFAULT 0,
    load_duration_ms_sum INTEGER NOT NULL DEFAULT 0,
    load_duration_ms_max INTEGER NOT NULL DEFAULT 0,
    loaded_body_bytes_sum INTEGER NOT NULL DEFAULT 0,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,

    CONSTRAINT ck_skill_usage_day CHECK (
        day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    CONSTRAINT ck_skill_usage_ids CHECK (
        (scope_id IS NULL OR length(scope_id) > 0)
        AND (session_id IS NULL OR length(session_id) > 0)
    ),
    CONSTRAINT ck_skill_usage_key CHECK (
        length(skill_key) = 64 AND skill_key NOT GLOB '*[^0-9a-f]*'
    ),
    CONSTRAINT ck_skill_usage_name CHECK (length(skill_name) BETWEEN 1 AND 64),
    CONSTRAINT ck_skill_usage_source CHECK (
        source_kind IN ('builtin', 'project', 'global', 'unresolved')
    ),
    CONSTRAINT ck_skill_usage_revision CHECK (
        skill_revision = '' OR (
            length(skill_revision) = 64
            AND skill_revision NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CONSTRAINT ck_skill_usage_trigger CHECK (
        trigger_kind IN (
            'human', 'task', 'watch', 'agent_run', 'callback',
            'standalone', 'unknown'
        )
    ),
    CONSTRAINT ck_skill_usage_counts CHECK (
        typeof(catalog_offer_count) = 'integer' AND catalog_offer_count >= 0
        AND typeof(load_success_count) = 'integer' AND load_success_count >= 0
        AND typeof(load_failure_count) = 'integer' AND load_failure_count >= 0
        AND catalog_offer_count + load_success_count + load_failure_count > 0
        AND (load_success_count = 0 OR skill_revision <> '')
        AND (catalog_offer_count = 0 OR skill_revision = '')
    ),
    CONSTRAINT ck_skill_usage_duration CHECK (
        typeof(load_duration_samples) = 'integer'
        AND load_duration_samples BETWEEN 0 AND load_success_count + load_failure_count
        AND typeof(load_duration_ms_sum) = 'integer' AND load_duration_ms_sum >= 0
        AND typeof(load_duration_ms_max) = 'integer'
        AND load_duration_ms_max BETWEEN 0 AND load_duration_ms_sum
        AND (load_duration_samples > 0 OR load_duration_ms_sum = 0)
    ),
    CONSTRAINT ck_skill_usage_bytes CHECK (
        typeof(loaded_body_bytes_sum) = 'integer' AND loaded_body_bytes_sum >= 0
        AND (load_success_count > 0 OR loaded_body_bytes_sum = 0)
    ),
    CONSTRAINT ck_skill_usage_observed CHECK (
        length(first_observed_at) = 27 AND length(last_observed_at) = 27
        AND substr(first_observed_at, 1, 10) = day
        AND substr(last_observed_at, 1, 10) = day
        AND first_observed_at <= last_observed_at
    )
);

-- SQLite normally treats two NULLs as distinct in a UNIQUE key. Normalize
-- optional IDs here so unassociated observations share one real bucket.
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_usage_daily_grain ON skill_usage_daily (
    day, coalesce(scope_id, ''), coalesce(session_id, ''), skill_key,
    skill_revision, backend, model, trigger_kind, platform, avibe_version
);
CREATE INDEX IF NOT EXISTS ix_skill_usage_daily_skill_day
    ON skill_usage_daily (skill_key, day);
CREATE INDEX IF NOT EXISTS ix_skill_usage_daily_session_day
    ON skill_usage_daily (session_id, day);
CREATE INDEX IF NOT EXISTS ix_skill_usage_daily_scope_day
    ON skill_usage_daily (scope_id, day);

-- Existing table: no new columns, no change to existing rows or event types.
CREATE INDEX IF NOT EXISTS ix_agent_events_skill_created_id ON agent_events (created_at, id)
    WHERE visibility = 'trace'
      AND event_type IN ('skill.catalog_result', 'skill.load_result');
"""


def upgrade() -> None:
    statement = ""
    for line in _DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            op.get_bind().exec_driver_sql(statement)
            statement = ""


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "delete from agent_events where event_type in "
        "('skill.catalog_result', 'skill.load_result') and visibility = 'trace'"
    )
    op.drop_index("ix_agent_events_skill_created_id", table_name="agent_events")
    op.drop_table("skill_usage_daily")
