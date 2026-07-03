from __future__ import annotations

from sqlalchemy import text

from storage.db import create_sqlite_engine
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
        assert {"name", "tags", "kind", "protection", "ciphertext", "nonce", "wrap_meta"} <= secret_cols
        assert "group_name" not in secret_cols

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
