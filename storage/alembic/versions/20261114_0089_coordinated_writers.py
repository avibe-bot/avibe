"""Activate the coordinated cache, Vault and Skill representations.

Existing row bytes are untouched. The partial ownership index is a one-time
migration scan, not a JSON backfill; old bridge deletes also execute the GC
triggers. This breaking boundary always retains the conservative backup path.
"""
from alembic import op

from storage.compatibility import advance_compatibility_epoch

revision = "20261114_0089"
down_revision = "20261112_0088"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "backfill"
MIGRATION_COMPATIBILITY = "breaking"
COMPATIBILITY_EPOCH = 3

# CASE is intentional: SQL predicate evaluation order cannot guard invalid JSON.
_REF = "CASE WHEN json_valid(content_json) THEN json_extract(content_json, '$.entries_ref') END"
_PREDICATE = """visibility = 'trace' AND event_type = 'skill.catalog_result'
AND CASE WHEN json_valid(content_json) THEN
    json_type(content_json) = 'object'
    AND json_type(content_json, '$.entries') IS NULL
    AND json_type(content_json, '$.entries_ref') = 'text'
    AND length(json_extract(content_json, '$.entries_ref')) = 64
    AND json_extract(content_json, '$.entries_ref') NOT GLOB '*[^0-9a-f]*'
ELSE 0 END"""
_OLD_REF = "CASE WHEN json_valid(OLD.content_json) THEN json_extract(OLD.content_json, '$.entries_ref') END"


def upgrade() -> None:
    conn = op.get_bind()
    # This reserved key is new at this boundary. A collision must fail, not reset
    # an existing high-water value. The descending primary-key seek is bounded.
    conn.exec_driver_sql("""
        INSERT INTO state_meta(key, value_json, updated_at)
        VALUES ('vault_audit_snapshot_high_water',
            CAST(COALESCE((SELECT id FROM vault_audit_access_snapshots ORDER BY id DESC LIMIT 1), 0) AS TEXT),
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """)
    conn.exec_driver_sql(f"CREATE INDEX ix_agent_events_catalog_ref ON agent_events ({_REF}) WHERE {_PREDICATE}")
    for suffix, event in (("delete", "DELETE"), ("update", "UPDATE OF content_json, event_type, visibility")):
        # Match the OLD digest directly on both sides. Comparing with the TEXT
        # catalog column coerces affinity and turns the expression seek into scan.
        conn.exec_driver_sql(f"""
            CREATE TRIGGER skill_catalog_gc_{suffix} AFTER {event} ON agent_events
            WHEN OLD.visibility = 'trace' AND OLD.event_type = 'skill.catalog_result'
            BEGIN
                DELETE FROM skill_catalogs WHERE digest = {_OLD_REF}
                AND NOT EXISTS (
                    SELECT 1 FROM agent_events INDEXED BY ix_agent_events_catalog_ref
                    WHERE {_PREDICATE} AND {_REF} = {_OLD_REF}
                );
            END
        """)
    # The reader bridge doesn't know a new Python teardown helper. Keep cleanup
    # database-owned for its archive/delete paths too, without rebuilding tables.
    conn.exec_driver_sql("""
        CREATE TRIGGER session_cache_delete AFTER DELETE ON agent_sessions
        BEGIN DELETE FROM session_cache_state WHERE session_id = OLD.id; END
    """)
    conn.exec_driver_sql("""
        CREATE TRIGGER session_cache_archive AFTER UPDATE OF status ON agent_sessions
        WHEN NEW.status = 'archived'
        BEGIN DELETE FROM session_cache_state WHERE session_id = OLD.id; END
    """)
    advance_compatibility_epoch(conn, 3)


def downgrade() -> None:
    raise RuntimeError(
        "Compatibility epochs cannot be downgraded. Select the verified 3.2.6 reader without restamping, "
        "or stop all writers and explicitly restore a verified backup; restoring discards writes "
        "made since that snapshot. Prefer a forward repair."
    )
