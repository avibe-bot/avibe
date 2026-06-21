"""``vibe data query`` must refuse to read ``vault_secrets`` while leaving the sibling
vault tables (audit/requests/links/grants) queryable for inspection.
"""

from __future__ import annotations

import sqlite3

import pytest

from storage.read_only_query import ReadOnlyQueryError, run_read_only_query


def _make_db(tmp_path):
    db = tmp_path / "vault_test.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table vault_secrets (id text, name text, ciphertext text)")
    conn.execute("insert into vault_secrets values ('1', 'OPENAI_API_KEY', 'ciphered')")
    conn.execute("create table vault_audit (id text, event text)")
    conn.execute("insert into vault_audit values ('a', 'created')")
    conn.commit()
    conn.close()
    return db


def test_select_vault_secrets_is_denied(tmp_path):
    db = _make_db(tmp_path)
    with pytest.raises(ReadOnlyQueryError):
        run_read_only_query("select * from vault_secrets", page_request=None, db_path=db)


def test_select_vault_secrets_column_is_denied(tmp_path):
    db = _make_db(tmp_path)
    with pytest.raises(ReadOnlyQueryError):
        run_read_only_query("select name from vault_secrets", page_request=None, db_path=db)


def test_sibling_vault_table_is_queryable(tmp_path):
    db = _make_db(tmp_path)
    result = run_read_only_query("select * from vault_audit", page_request=None, db_path=db)
    assert [row["event"] for row in result.rows] == ["created"]
