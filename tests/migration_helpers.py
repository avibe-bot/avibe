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
    (stamp,) = conn.execute(
        "SELECT updated_at FROM state_meta WHERE key = 'compatibility_epoch'"
    ).fetchone()
    assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None
    (literal,) = conn.execute("SELECT quote(?)", (stamp,)).fetchone()
    return sorted(
        row.replace(literal, "'<epoch-migration-time>'")
        if row.startswith('INSERT INTO "state_meta" VALUES(\'compatibility_epoch\',') else row
        for row in conn.iterdump() if row.startswith("INSERT INTO")
    )


def latest_migration_revision() -> str:
    """Expected completed stamp from the checkout's graph, not a stale literal."""
    from storage import migrations

    head = ScriptDirectory.from_config(migrations.alembic_config()).get_current_head()
    assert head is not None
    return head


def downgrade_legacy_schema(config: Config, revision: str) -> None:
    """Reconstruct a pre-bridge fixture, then test a frozen historical downgrade.

    Only the two known marker-only transitions may be removed, and never from a
    fixture with projection-capable responses. Structural downgrades still run.
    This is NOT a supported downgrade path; production epoch guards stay intact.
    """
    path = make_url(config.get_main_option("sqlalchemy.url")).database
    script = ScriptDirectory.from_config(config)
    for boundary, epoch in (("20261108_0080", 2), ("20261108_0076", 1)):
        with closing(sqlite3.connect(path)) as conn:
            [(stamp,)] = conn.execute("SELECT version_num FROM alembic_version").fetchall()
            if boundary not in {item.revision for item in script.iterate_revisions(stamp, "base")}:
                continue
            assert conn.execute(
                "SELECT value_json FROM state_meta WHERE key = 'compatibility_epoch'"
            ).fetchone() == (str(epoch),)
            if epoch == 2:
                assert conn.execute(
                    "SELECT count(*) FROM messages WHERE CASE WHEN json_valid(metadata_json) "
                    "THEN json_extract(metadata_json, '$.tool_projection') IS NOT NULL ELSE 0 END"
                ).fetchone() == (0,), "cannot relabel canonical projection data as a historical fixture"
        if stamp != boundary:
            command.downgrade(config, boundary)
        with closing(sqlite3.connect(path)) as conn, conn:
            if epoch == 2:
                conn.execute("UPDATE state_meta SET value_json='1' WHERE key='compatibility_epoch'")
            else:
                conn.execute("DELETE FROM state_meta WHERE key='compatibility_epoch'")
            conn.execute("UPDATE alembic_version SET version_num=?", (script.get_revision(boundary).down_revision,))
    command.downgrade(config, revision)
