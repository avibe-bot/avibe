"""Unit tests for storage/vault_service.py (P0 data layer).

Uses a temp sqlite engine + an explicit machine-key path under ``tmp_path`` so neither
the DB nor the key touch the real ``~/.avibe``.
"""

from __future__ import annotations

import json

import pytest

from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_audit


@pytest.fixture
def vault(tmp_path):
    engine = create_sqlite_engine(tmp_path / "vault_test.sqlite")
    metadata.create_all(engine)
    key_path = tmp_path / "machine.key"
    return engine, key_path


def _create(engine, key_path, **kw):
    with engine.begin() as conn:
        return vs.create_secret(conn, key_path=key_path, **kw)


def test_create_returns_masked_meta_without_value(vault):
    engine, key_path = vault
    meta = _create(engine, key_path, name="OPENAI_API_KEY", value="sk-ant-abcd1234")
    assert meta["name"] == "OPENAI_API_KEY"
    assert meta["protection"] == "standard"
    assert meta["group"] == "default"
    assert meta["preview"] == "…1234"
    # The masked metadata must not carry the plaintext anywhere.
    assert "sk-ant-abcd1234" not in json.dumps(meta)


def test_resolve_round_trip_and_usage(vault):
    engine, key_path = vault
    _create(engine, key_path, name="DB_URL", value="postgres://secret@host/db")
    with engine.connect() as conn:
        values = vs.resolve(conn, ["DB_URL"], key_path=key_path)
    assert values == {"DB_URL": "postgres://secret@host/db"}
    # resolve alone neither audits nor bumps usage — delivery is recorded only after the
    # actual delivery action succeeds.
    with engine.begin() as conn:
        assert vs.get_secret_meta(conn, "DB_URL")["use_count"] == 0
        vs.record_deliveries(conn, ["DB_URL"], requester={"agent": "claude"}, mode="run")
    with engine.begin() as conn:
        meta = vs.get_secret_meta(conn, "DB_URL")
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None


def test_list_secrets_masked_and_group_filtered(vault):
    engine, key_path = vault
    with engine.begin() as conn:
        # group_name is a FK to vault_groups; create the 'crypto' group first.
        from storage.models import vault_groups

        vs.ensure_default_group(conn)
        conn.execute(vault_groups.insert().values(name="crypto", grantable=1, max_grant_ttl_seconds=900, created_at=vs._now()))
    _create(engine, key_path, name="A_KEY", value="aaaa1111", group="default")
    _create(engine, key_path, name="B_KEY", value="bbbb2222", group="crypto")
    _create(engine, key_path, name="C_KEY", value="cccc3333", group="crypto")
    with engine.begin() as conn:
        all_names = [m["name"] for m in vs.list_secrets(conn)]
        crypto_names = [m["name"] for m in vs.list_secrets(conn, group="crypto")]
    assert all_names == ["A_KEY", "B_KEY", "C_KEY"]
    assert crypto_names == ["B_KEY", "C_KEY"]


def test_duplicate_name_rejected(vault):
    engine, key_path = vault
    _create(engine, key_path, name="DUP", value="one")
    with pytest.raises(vs.SecretExistsError):
        _create(engine, key_path, name="DUP", value="two")


def test_invalid_name_rejected(vault):
    engine, key_path = vault
    with pytest.raises(vs.InvalidSecretNameError):
        _create(engine, key_path, name="lower_case", value="x")


def test_protected_tier_not_available_in_p0(vault):
    engine, key_path = vault
    with pytest.raises(vs.UnsupportedProtectionError):
        _create(engine, key_path, name="SECRET", value="x", protection="protected")


def test_rotate_changes_value(vault):
    engine, key_path = vault
    _create(engine, key_path, name="ROT", value="old-value-9999")
    with engine.begin() as conn:
        meta = vs.rotate_secret(conn, "ROT", "new-value-0000", key_path=key_path)
    assert meta["preview"] == "…0000"
    with engine.begin() as conn:
        assert vs.resolve(conn, ["ROT"], key_path=key_path) == {"ROT": "new-value-0000"}


def test_delete_removes_secret(vault):
    engine, key_path = vault
    _create(engine, key_path, name="GONE", value="x")
    with engine.begin() as conn:
        vs.delete_secret(conn, "GONE")
    with engine.begin() as conn, pytest.raises(vs.SecretNotFoundError):
        vs.get_secret_meta(conn, "GONE")


def test_resolve_unknown_raises(vault):
    engine, key_path = vault
    with engine.begin() as conn, pytest.raises(vs.SecretNotFoundError):
        vs.resolve(conn, ["NOPE"], key_path=key_path)


def test_audit_records_events_without_value(vault):
    engine, key_path = vault
    _create(engine, key_path, name="AUD", value="topsecretvalue42")
    with engine.begin() as conn:
        vs.resolve(conn, ["AUD"], key_path=key_path)
        vs.record_deliveries(conn, ["AUD"], requester={"agent": "claude"}, mode="run")
        vs.delete_secret(conn, "AUD")
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    events = {r["event"] for r in rows}
    assert {"created", "delivered", "deleted"} <= events
    # No audit row may contain the plaintext anywhere.
    assert all("topsecretvalue42" not in json.dumps(r) for r in rows)


def test_create_auto_creates_missing_group(vault):
    engine, key_path = vault
    # No manual group seed — create_secret must auto-create 'brandnew' so the FK holds.
    meta = _create(engine, key_path, name="NEW_GROUP_KEY", value="v", group="brandnew")
    assert meta["group"] == "brandnew"
    with engine.connect() as conn:
        from storage.models import vault_groups

        groups = {r[0] for r in conn.execute(vault_groups.select().with_only_columns(vault_groups.c.name))}
    assert "brandnew" in groups


def test_create_fulfills_pending_provision_request(vault):
    engine, key_path = vault
    with engine.begin() as conn:
        req = vs.create_provision_request(conn, "ASKED_KEY", requester={"agent": "claude"})
    # Saving the secret through the plain create path (not fulfill_provision) must still
    # mark the pending request fulfilled, so `request --wait` resolves.
    _create(engine, key_path, name="ASKED_KEY", value="provided-value")
    with engine.begin() as conn:
        from storage.models import vault_requests

        status = conn.execute(vault_requests.select().where(vault_requests.c.id == req["id"])).mappings().first()["status"]
    assert status == "fulfilled"


def test_request_for_existing_secret_is_born_fulfilled(vault):
    engine, key_path = vault
    _create(engine, key_path, name="ALREADY", value="here")
    with engine.begin() as conn:
        req = vs.create_provision_request(conn, "ALREADY")
    assert req["status"] == "fulfilled"  # no pending row that --wait would block on


def test_provision_request_and_fulfill(vault):
    engine, key_path = vault
    with engine.begin() as conn:
        req = vs.create_provision_request(conn, "NEW_KEY", reason="sync needs it", requester={"agent": "claude"})
    assert req["status"] == "pending"
    with engine.begin() as conn:
        vs.fulfill_provision(conn, req["id"], "filled-value-7777", key_path=key_path)
    with engine.begin() as conn:
        assert vs.resolve(conn, ["NEW_KEY"], key_path=key_path) == {"NEW_KEY": "filled-value-7777"}
        from storage.models import vault_requests

        status = conn.execute(vault_requests.select().where(vault_requests.c.id == req["id"])).mappings().first()["status"]
    assert status == "fulfilled"
