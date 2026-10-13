from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from storage.db import create_sqlite_engine
from storage import migrations
from storage.models import metadata


_VAULT_TABLES = ("vault_secrets", "vault_requests", "vault_grants", "vault_audit")


def test_vault_metadata_schema_matches_grant_id_tag_model(tmp_path):
    engine = create_sqlite_engine(tmp_path / "vault_schema.sqlite")
    metadata.create_all(engine)

    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text("select name from sqlite_master where type='table'"))}
        assert set(_VAULT_TABLES) <= tables
        assert "vault_groups" not in tables
        assert "vault_links" not in tables

        secret_cols = {row[1] for row in conn.execute(text("pragma table_info(vault_secrets)"))}
        assert {"name", "tags", "kind", "protection", "ciphertext", "nonce", "wrap_meta", "fields"} <= secret_cols
        assert "group_name" not in secret_cols
        secret_indexes = {row[1] for row in conn.execute(text("select seq, name from pragma_index_list('vault_secrets')"))}
        assert "uq_vault_secrets_name_folded" in secret_indexes
        request_triggers = {
            row[0]
            for row in conn.execute(
                text("select name from sqlite_master where type = 'trigger' and tbl_name = 'vault_requests'")
            )
        }
        assert "trg_vault_requests_pending_provision_name_case_insert" in request_triggers
        assert "trg_vault_requests_pending_provision_name_case_update" in request_triggers

        grant_cols = {row[1] for row in conn.execute(text("pragma table_info(vault_grants)"))}
        assert {
            "id",
            "member_snapshot",
            "source_selector",
            "request_id",
            "session_id",
            "purpose",
            "one_shot",
            "expires_at",
            "agent_ready_at",
        } <= grant_cols
        assert "scope_type" not in grant_cols
        assert "scope_ref" not in grant_cols


def test_vault_fields_migration_round_trip_preserves_old_rows(tmp_path: Path):
    db_path = tmp_path / "vault_migration.sqlite"
    migrations.run_migrations(db_path, revision="20261012_0064")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into vault_secrets (
                id, name, kind, protection, source, ciphertext, nonce, wrap_meta,
                public_meta, use_count, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "vlt_old",
                "OLD_SECRET",
                "static",
                "standard",
                "manual",
                "ct-old",
                "n-old",
                "wm-old",
                json.dumps({"description": "legacy"}),
                0,
                "2026-10-13T00:00:00+00:00",
                "2026-10-13T00:00:00+00:00",
            ),
        )
        cursor = conn.execute("select * from vault_secrets where id = ?", ("vlt_old",))
        original_row = cursor.fetchone()
        original_columns = ", ".join(column[0] for column in cursor.description)
        conn.commit()

    migrations.run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(vault_secrets)")}
        row = conn.execute(
            "select name, ciphertext, public_meta, fields from vault_secrets where id = ?",
            ("vlt_old",),
        ).fetchone()
        assert "fields" in columns
        assert row == ("OLD_SECRET", "ct-old", json.dumps({"description": "legacy"}), None)

    command.downgrade(migrations.alembic_config(db_path), "20261012_0064")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(vault_secrets)")}
        row = conn.execute(
            "select name, ciphertext, public_meta from vault_secrets where id = ?",
            ("vlt_old",),
        ).fetchone()
        assert "fields" not in columns
        assert row == ("OLD_SECRET", "ct-old", json.dumps({"description": "legacy"}))
        assert conn.execute(
            f"select {original_columns} from vault_secrets where id = ?", ("vlt_old",)
        ).fetchone() == original_row
        indexes = {row[1] for row in conn.execute("pragma index_list(vault_secrets)")}
        assert {"ix_vault_secrets_name_kind", "uq_vault_secrets_name_folded"} <= indexes
        assert [row[2] for row in conn.execute("pragma index_info(ix_vault_secrets_name_kind)")] == ["name", "kind"]
        conn.execute(
            "insert into vault_secrets (id, name, created_at, updated_at) values ('vlt_other', 'OTHER', 'now', 'now')"
        )
        for duplicate in ("OLD_SECRET", "old_secret"):
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                conn.execute(
                    "insert into vault_secrets (id, name, created_at, updated_at) values (?, ?, 'now', 'now')",
                    (f"vlt_duplicate_{duplicate}", duplicate),
                )
        assert conn.execute("pragma integrity_check").fetchone() == ("ok",)
    engine = create_sqlite_engine(db_path)
    try:
        assert {"name": "uq_vault_secrets_name", "column_names": ["name"]} in inspect(engine).get_unique_constraints("vault_secrets")
    finally:
        engine.dispose()


def test_head_repair_adds_missing_fields_column_without_dropping_rows(tmp_path: Path):
    db_path = tmp_path / "vault_repair.sqlite"
    engine = create_sqlite_engine(db_path)
    metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(db_path) as conn:
        conn.execute('alter table "vault_secrets" drop column "fields"')
        conn.execute(
            """
            insert into vault_secrets (
                id, name, kind, protection, source, ciphertext, nonce, wrap_meta,
                use_count, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "vlt_repair",
                "REPAIR_SECRET",
                "static",
                "standard",
                "manual",
                "ct-repair",
                "n-repair",
                "wm-repair",
                0,
                "2026-10-13T00:00:00+00:00",
                "2026-10-13T00:00:00+00:00",
            ),
        )
        conn.commit()
    assert migrations.background_tables_ready(db_path) is False
    migrations.run_migrations(db_path)
    assert migrations.background_tables_ready(db_path) is True
    with sqlite3.connect(db_path) as conn:
        assert "fields" in {row[1] for row in conn.execute("pragma table_info(vault_secrets)")}
        assert conn.execute(
            "select name, ciphertext, fields from vault_secrets where id = ?",
            ("vlt_repair",),
        ).fetchone() == ("REPAIR_SECRET", "ct-repair", None)
