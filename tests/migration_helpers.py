"""Fixtures for testing frozen pre-bridge migrations, not product downgrade APIs."""
from contextlib import closing
from datetime import datetime
import sqlite3

from alembic import command
from alembic.config import Config
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


def downgrade_legacy_schema(config: Config, revision: str) -> None:
    """Reconstruct the pre-bridge fixture, then test a historical downgrade.

    The bridge adds only a marker, so removing that marker and its stamp creates
    exactly the historical fixture these tests used before epochs existed. This
    is deliberately test-only: the product refuses cross-epoch downgrades.
    """
    path = make_url(config.get_main_option("sqlalchemy.url")).database
    with closing(sqlite3.connect(path)) as conn, conn:
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        if stamp == [("20261108_0076",)]:
            assert conn.execute(
                "SELECT value_json FROM state_meta WHERE key = 'compatibility_epoch'"
            ).fetchone() == ("1",)
            conn.execute("DELETE FROM state_meta WHERE key = 'compatibility_epoch'")
            conn.execute("UPDATE alembic_version SET version_num = '20261107_0075'")
    command.downgrade(config, revision)
