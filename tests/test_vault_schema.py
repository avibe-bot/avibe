"""Migration smoke test: the vault tables exist and the default group is seeded
after the standard state-init path. Relies on the autouse ``VIBE_REMOTE_HOME``
isolation in conftest, so this builds a fresh DB under tmp — never the real home.
"""

from __future__ import annotations

import sqlite3

from config import paths
from storage.importer import ensure_sqlite_state

_VAULT_TABLES = ("vault_groups", "vault_secrets", "vault_links", "vault_requests", "vault_grants", "vault_audit")


def test_vault_tables_created_and_default_group_seeded():
    ensure_sqlite_state(primary_platform=None)
    conn = sqlite3.connect(paths.get_sqlite_state_path())
    try:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        for table in _VAULT_TABLES:
            assert table in tables, f"missing vault table: {table}"

        secret_cols = {row[1] for row in conn.execute('pragma table_info("vault_secrets")')}
        assert {"name", "group_name", "kind", "protection", "ciphertext", "nonce", "wrap_meta"} <= secret_cols

        groups = {row[0]: row[1] for row in conn.execute("select name, grantable from vault_groups")}
        assert groups.get("default") == 1
    finally:
        conn.close()
