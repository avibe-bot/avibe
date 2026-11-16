"""Fixtures for testing frozen pre-bridge migrations, not product downgrade APIs."""
from contextlib import closing
from datetime import datetime
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url


def migration_seed_data(conn: sqlite3.Connection) -> list[str]:
    """Compare all real seed rows, ignoring only the epoch's wall-clock time.

    A copied schema template and a new migration run have identical persisted
    contracts, but were established at different times. Validate that timestamp
    rather than dropping the whole epoch row from fixture-equivalence checks.
    """
    replacements = []
    for key, stamp in conn.execute(
        "SELECT key, updated_at FROM state_meta WHERE key IN ('compatibility_epoch', 'vault_audit_snapshot_high_water')"
    ):
        assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None
        (literal,) = conn.execute("SELECT quote(?)", (stamp,)).fetchone()
        replacements.append((key, literal))
    rows = []
    for row in conn.iterdump():
        if not row.startswith("INSERT INTO"):
            continue
        for key, literal in replacements:
            if row.startswith(f'INSERT INTO "state_meta" VALUES(\'{key}\','):
                row = row.replace(literal, "'<epoch-migration-time>'")
        rows.append(row)
    return sorted(rows)


def latest_migration_revision() -> str:
    """Expected completed stamp from the checkout's graph, not a stale literal."""
    from storage import migrations

    head = ScriptDirectory.from_config(migrations.alembic_config()).get_current_head()
    assert head is not None
    return head


def downgrade_legacy_schema(config: Config, revision: str) -> None:
    """Reconstruct a pre-bridge fixture, then test a frozen historical downgrade.

    The two marker-only transitions and the empty epoch-3 lifecycle schema may
    be removed, never from fixtures with projection/compact data. Structural
    downgrades still run. This is NOT a supported downgrade path; production
    epoch guards stay intact.
    """
    path = make_url(config.get_main_option("sqlalchemy.url")).database
    script = ScriptDirectory.from_config(config)
    for boundary, epoch in (("20261114_0089", 3), ("20261108_0083", 2), ("20261108_0076", 1)):
        with closing(sqlite3.connect(path)) as conn:
            [(stamp,)] = conn.execute("SELECT version_num FROM alembic_version").fetchall()
            if boundary not in {item.revision for item in script.iterate_revisions(stamp, "base")}:
                continue
            assert conn.execute(
                "SELECT value_json FROM state_meta WHERE key = 'compatibility_epoch'"
            ).fetchone() == (str(epoch),)
            if epoch >= 2:
                assert conn.execute(
                    "SELECT count(*) FROM messages WHERE CASE WHEN json_valid(metadata_json) "
                    "THEN json_extract(metadata_json, '$.tool_projection') IS NOT NULL ELSE 0 END"
                ).fetchone() == (0,), "cannot relabel canonical projection data as a historical fixture"
            if epoch == 3:
                for table in ("session_cache_state", "skill_catalogs", "vault_audit_access_snapshots"):
                    assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone() == (0,), "cannot relabel compact data"
                for table, column, key in (("agent_events", "content_json", "$.entries_ref"),
                                           ("vault_audit", "delivery", "$._resource_access_snapshot.ref"),
                                           ("messages", "content_json", "$.model.cache")):
                    assert conn.execute(
                        f"SELECT count(*) FROM {table} WHERE CASE WHEN json_valid({column}) "
                        f"THEN json_type({column}, ?) IS NOT NULL ELSE 0 END", (key,),
                    ).fetchone() == (0,), "cannot relabel compact data"
        if stamp != boundary:
            command.downgrade(config, boundary)
        with closing(sqlite3.connect(path)) as conn, conn:
            if epoch == 3:
                for trigger in ("session_cache_archive", "session_cache_delete", "skill_catalog_gc_update", "skill_catalog_gc_delete"):
                    conn.execute(f'DROP TRIGGER "{trigger}"')
                conn.execute("DROP INDEX ix_agent_events_catalog_ref")
                conn.execute("DELETE FROM state_meta WHERE key='vault_audit_snapshot_high_water'")
                conn.execute("UPDATE state_meta SET value_json='2' WHERE key='compatibility_epoch'")
            elif epoch == 2:
                conn.execute("UPDATE state_meta SET value_json='1' WHERE key='compatibility_epoch'")
            else:
                conn.execute("DELETE FROM state_meta WHERE key='compatibility_epoch'")
            conn.execute("UPDATE alembic_version SET version_num=?", (script.get_revision(boundary).down_revision,))
    command.downgrade(config, revision)
