"""Unit tests for storage/vault_service.py.

The data layer stores avault-produced envelopes and masked metadata only. It never
sees plaintext, machine keys, or Python crypto.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_audit, vault_requests, vault_secrets
from storage.vault_crypto import Sealed


@pytest.fixture
def vault(tmp_path):
    engine = create_sqlite_engine(tmp_path / "vault_test.sqlite")
    metadata.create_all(engine)
    return engine


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _create(engine, **kw):
    with engine.begin() as conn:
        return vs.create_secret(conn, sealed=_sealed(), **kw)


def _row(engine, name: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().one())


def test_value_preview_masks_tail_hint():
    assert vs.value_preview("") == ""
    assert vs.value_preview("abc") == "•••"
    assert vs.value_preview("abcd") == "••••"
    assert vs.value_preview("abcde") == "…bcde"


def test_create_stores_envelope_and_masked_meta(vault):
    meta = _create(
        vault,
        name="OPENAI_API_KEY",
        preview="…1234",
        description="key",
        policy={"allowed_hosts": ["api.example.com"]},
    )

    assert meta["name"] == "OPENAI_API_KEY"
    assert meta["protection"] == "standard"
    assert meta["group"] == "default"
    assert meta["preview"] == "…1234"
    assert meta["policy"] == {"allowed_hosts": ["api.example.com"]}
    assert "plaintext" not in json.dumps(meta)
    row = _row(vault, "OPENAI_API_KEY")
    assert row["ciphertext"] == "ct-1"
    assert row["nonce"] == "n-1"
    assert row["wrap_meta"] == "wm-1"


def test_get_envelope_and_get_envelopes_return_stored_envelopes(vault):
    _create(vault, name="A_KEY", preview="…1111")
    with vault.begin() as conn:
        vs.create_secret(conn, name="B_KEY", sealed=_sealed("2"), preview="…2222")
    with vault.connect() as conn:
        assert vs.get_envelope(conn, "A_KEY") == _sealed()
        assert vs.get_envelopes(conn, ["B_KEY", "A_KEY"]) == {
            "B_KEY": _sealed("2"),
            "A_KEY": _sealed(),
        }


def test_get_envelopes_validates_batch_before_returning(vault):
    _create(vault, name="A_KEY")
    with vault.connect() as conn, pytest.raises(vs.SecretNotFoundError):
        vs.get_envelopes(conn, ["A_KEY", "NOPE"])


def test_record_deliveries_bumps_usage_and_audits(vault):
    _create(vault, name="DB_URL")
    with vault.begin() as conn:
        assert vs.get_secret_meta(conn, "DB_URL")["use_count"] == 0
        vs.record_deliveries(conn, ["DB_URL"], requester={"agent": "claude"}, mode="run")
    with vault.connect() as conn:
        meta = vs.get_secret_meta(conn, "DB_URL")
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None
    assert "delivered" in {r["event"] for r in rows}


def test_record_proxy_use_bumps_usage_and_audits(vault):
    _create(vault, name="GH_PAT")
    with vault.begin() as conn:
        vs.record_proxy_use(conn, "GH_PAT", requester={"source": "cli"}, delivery={"status": 200})
    with vault.connect() as conn:
        meta = vs.get_secret_meta(conn, "GH_PAT")
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None
    assert "proxied" in {r["event"] for r in rows}


def test_list_secrets_masked_and_group_filtered(vault):
    _create(vault, name="A_KEY", preview="…1111", group="default")
    _create(vault, name="B_KEY", preview="…2222", group="crypto")
    _create(vault, name="C_KEY", preview="…3333", group="crypto")
    with vault.connect() as conn:
        all_names = [m["name"] for m in vs.list_secrets(conn)]
        crypto_names = [m["name"] for m in vs.list_secrets(conn, group="crypto")]
    assert all_names == ["A_KEY", "B_KEY", "C_KEY"]
    assert crypto_names == ["B_KEY", "C_KEY"]


def test_duplicate_name_rejected(vault):
    _create(vault, name="DUP")
    with pytest.raises(vs.SecretExistsError):
        _create(vault, name="DUP")


def test_invalid_name_rejected(vault):
    with pytest.raises(vs.InvalidSecretNameError):
        _create(vault, name="lower_case")


def test_protected_tier_not_available_in_p0(vault):
    with pytest.raises(vs.UnsupportedProtectionError):
        _create(vault, name="SECRET", protection="protected")


def test_rotate_changes_envelope_and_preview(vault):
    _create(vault, name="ROT", preview="…9999")
    with vault.begin() as conn:
        meta = vs.rotate_secret(conn, "ROT", _sealed("new"), preview="…0000")
    assert meta["preview"] == "…0000"
    with vault.connect() as conn:
        assert vs.get_envelope(conn, "ROT") == _sealed("new")


def test_delete_removes_secret(vault):
    _create(vault, name="GONE")
    with vault.begin() as conn:
        vs.delete_secret(conn, "GONE")
    with vault.connect() as conn, pytest.raises(vs.SecretNotFoundError):
        vs.get_secret_meta(conn, "GONE")


def test_audit_records_events_without_values(vault):
    _create(vault, name="AUD", preview="…lue42")
    with vault.begin() as conn:
        vs.record_deliveries(conn, ["AUD"], requester={"agent": "claude"}, mode="run")
        vs.delete_secret(conn, "AUD")
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    events = {r["event"] for r in rows}
    assert {"created", "delivered", "deleted"} <= events
    assert all("topsecretvalue42" not in json.dumps(r) for r in rows)


def test_create_auto_creates_missing_group(vault):
    meta = _create(vault, name="NEW_GROUP_KEY", group="brandnew")
    assert meta["group"] == "brandnew"
    with vault.connect() as conn:
        from storage.models import vault_groups

        groups = {r[0] for r in conn.execute(vault_groups.select().with_only_columns(vault_groups.c.name))}
    assert "brandnew" in groups


def test_create_fulfills_pending_provision_request(vault):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "ASKED_KEY", requester={"agent": "claude"})
    _create(vault, name="ASKED_KEY", preview="…ided")
    with vault.connect() as conn:
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()
    assert status == "fulfilled"


def test_request_for_existing_secret_is_born_fulfilled(vault):
    _create(vault, name="ALREADY")
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "ALREADY")
    assert req["status"] == "fulfilled"


def test_provision_request_and_fulfill(vault):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "NEW_KEY", reason="sync needs it", requester={"agent": "claude"})
    assert req["status"] == "pending"
    with vault.begin() as conn:
        meta = vs.fulfill_provision(conn, req["id"], _sealed("filled"), preview="…7777", description="filled")
    assert meta["preview"] == "…7777"
    assert meta["description"] == "filled"
    with vault.connect() as conn:
        assert vs.get_envelope(conn, "NEW_KEY") == _sealed("filled")
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()
    assert status == "fulfilled"
