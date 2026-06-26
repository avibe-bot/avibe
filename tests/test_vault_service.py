"""Unit tests for storage/vault_service.py.

The data layer stores avault-produced envelopes and masked metadata only. It never
sees plaintext, machine keys, or Python crypto.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_audit, vault_grants, vault_links, vault_requests, vault_secrets
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


def _access_request(conn, name: str, *, session_id: str = "ses_1", skill: str | None = None) -> dict:
    payload = {"session_id": session_id}
    if skill:
        payload["skill"] = skill
    return vs.create_access_request(conn, name, requester=payload, delivery=payload)


def _row(engine, name: str) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().one())


def test_create_stores_envelope_and_value_free_meta(vault):
    meta = _create(
        vault,
        name="OPENAI_API_KEY",
        description="key",
        policy={"allowed_hosts": ["api.example.com"]},
    )

    assert meta["name"] == "OPENAI_API_KEY"
    assert meta["protection"] == "standard"
    assert meta["group"] == "default"
    assert "preview" not in meta
    assert meta["policy"] == {"allowed_hosts": ["api.example.com"]}
    assert "plaintext" not in json.dumps(meta)
    row = _row(vault, "OPENAI_API_KEY")
    assert row["ciphertext"] == "ct-1"
    assert row["nonce"] == "n-1"
    assert row["wrap_meta"] == "wm-1"
    assert json.loads(row["public_meta"]) == {"description": "key"}


def test_create_persists_no_value_derived_public_meta(vault):
    secret_value = "sk-ant-abcd1234"
    value_tail = secret_value[-4:]
    _create(vault, name="NO_PREVIEW_KEY")

    row = _row(vault, "NO_PREVIEW_KEY")
    assert row["public_meta"] is None

    with vault.connect() as conn:
        meta = vs.get_secret_meta(conn, "NO_PREVIEW_KEY")
        listed = vs.list_secrets(conn)

    assert "preview" not in meta
    assert "preview" not in json.dumps(listed)
    assert value_tail not in json.dumps(meta)
    assert value_tail not in json.dumps(listed)


def test_pubkey_pin_metadata_round_trips_through_masked_meta(vault):
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    pin = {
        "public_key": "02" + "ab" * 32,
        "fingerprint": "fp_123",
        "attested_at": "2026-06-26T00:00:00Z",
        "attestation": {"source": "avault"},
        "ignored": "nope",
    }

    with vault.begin() as conn:
        stored = vs.store_pubkey_pin(conn, "ETH_KEY", pin)
        listed = vs.list_secrets(conn)

    assert stored["avault_pubkey_pin"] == {
        "public_key": pin["public_key"],
        "fingerprint": "fp_123",
        "attested_at": "2026-06-26T00:00:00Z",
        "attestation": {"source": "avault"},
    }
    listed_meta = next(item for item in listed if item["name"] == "ETH_KEY")
    assert listed_meta["avault_pubkey_pin"]["fingerprint"] == "fp_123"
    assert "ignored" not in listed_meta["avault_pubkey_pin"]


def test_get_envelope_and_get_envelopes_return_stored_envelopes(vault):
    _create(vault, name="A_KEY")
    with vault.begin() as conn:
        vs.create_secret(conn, name="B_KEY", sealed=_sealed("2"))
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
    _create(vault, name="A_KEY", group="default")
    _create(vault, name="B_KEY", group="crypto")
    _create(vault, name="C_KEY", group="crypto")
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


def test_create_protected_stores_browser_envelope_without_decrypting(vault):
    meta = _create(vault, name="SECRET", protection="protected")

    assert meta["protection"] == "protected"
    with vault.connect() as conn:
        row = _row(vault, "SECRET")
        assert row["ciphertext"] == "ct-1"
        assert row["nonce"] == "n-1"
        assert row["wrap_meta"] == "wm-1"
        with pytest.raises(vs.UnsupportedProtectionError):
            vs.get_envelope(conn, "SECRET")


def test_rotate_changes_envelope_and_strips_legacy_preview(vault):
    _create(vault, name="ROT", description="rotating")
    with vault.begin() as conn:
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "ROT")
            .values(public_meta=json.dumps({"description": "rotating", "preview": "…9999"}))
        )
    with vault.begin() as conn:
        meta = vs.rotate_secret(conn, "ROT", _sealed("new"))
    assert "preview" not in meta
    assert meta["description"] == "rotating"
    with vault.connect() as conn:
        assert vs.get_envelope(conn, "ROT") == _sealed("new")
    row = _row(vault, "ROT")
    assert json.loads(row["public_meta"]) == {"description": "rotating"}


def test_delete_removes_secret(vault):
    _create(vault, name="GONE")
    with vault.begin() as conn:
        vs.delete_secret(conn, "GONE")
    with vault.connect() as conn, pytest.raises(vs.SecretNotFoundError):
        vs.get_secret_meta(conn, "GONE")


def test_audit_records_events_without_values(vault):
    _create(vault, name="AUD")
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
    _create(vault, name="ASKED_KEY")
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
        meta = vs.fulfill_provision(conn, req["id"], _sealed("filled"), description="filled")
    assert "preview" not in meta
    assert meta["description"] == "filled"
    with vault.connect() as conn:
        assert vs.get_envelope(conn, "NEW_KEY") == _sealed("filled")
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()
    assert status == "fulfilled"


def test_provision_request_carries_secure_input_card_without_value(vault):
    with vault.begin() as conn:
        req = vs.create_provision_request(conn, "NEW_CARD_KEY", reason="deploy", skill="release")
    assert req["card"]["card_type"] == "secure_input"
    assert req["card"]["secret_name"] == "NEW_CARD_KEY"
    assert req["card"]["value"] is None
    with vault.connect() as conn:
        listed = vs.list_requests(conn)
    assert listed[0]["card"]["default_protection"] == "protected"
    assert "secret-value" not in json.dumps(listed)


def test_create_grant_freezes_scope_members_and_cache_is_memory_only(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            ttl_seconds=900,
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a", "B_KEY": "dek-b"},
            cache=cache,
        )

    assert grant["scope_type"] == "group"
    assert grant["member_snapshot"] == ["A_KEY", "B_KEY"]
    assert grant["cached_member_count"] == 2
    with vault.connect() as conn:
        row = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one())
    assert "dek-a" not in json.dumps(row)
    assert json.loads(row["member_snapshot"]) == ["A_KEY", "B_KEY"]


def test_grant_uses_approval_member_snapshot_not_later_group_members(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        vs.create_secret(conn, name="B_KEY", protection="protected", group="crypto", sealed=_sealed("b"))
        grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )

    assert grant["member_snapshot"] == ["A_KEY"]
    assert grant["cached_member_count"] == 1
    assert cache.has(grant["id"], "A_KEY")
    assert not cache.has(grant["id"], "B_KEY")


def test_reusing_approval_request_does_not_create_second_grant(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        first = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=req["id"],
                deks_by_secret={"A_KEY": "dek-a-2"},
                cache=cache,
            )
        grants = list(conn.execute(select(vault_grants)).mappings())

    assert [row["id"] for row in grants] == [first["id"]]
    assert cache.get(first["id"], "A_KEY") == "dek-a"


def test_find_active_grant_requires_cached_dek_and_expires_stale_row(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )
        cache.clear()
        assert vs.find_active_grant_for_secret(conn, "A_KEY", session_id="ses_1", cache=cache) is None
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"


def test_grant_cache_drops_deks_at_expiry_without_vault_sweep():
    cache = vs.VaultGrantDekCache()
    expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    cache.put("vgr_old", {"A_KEY": "dek-a"}, expires_at=expires_at)

    assert not cache.has("vgr_old", "A_KEY")
    assert cache.get("vgr_old", "A_KEY") is None
    assert cache.covered_names("vgr_old") == []


def test_rotate_protected_secret_expires_active_grants(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )
        vs.rotate_secret(conn, "A_KEY", _sealed("rotated"), cache=cache)
        resolved = vs.resolve_secret_access(conn, "A_KEY", session_id="ses_1", create_request=False, cache=cache)

    assert resolved["status"] == "approval_required"
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"
    assert not cache.has(grant["id"], "A_KEY")


def test_delete_protected_secret_expires_active_grants_before_recreate(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )
        vs.delete_secret(conn, "A_KEY", cache=cache)
        vs.create_secret(conn, name="A_KEY", protection="protected", sealed=_sealed("recreated"))
        resolved = vs.resolve_secret_access(conn, "A_KEY", session_id="ses_1", create_request=False, cache=cache)

    assert resolved["status"] == "approval_required"
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"
    assert not cache.has(grant["id"], "A_KEY")


def test_active_grant_list_expires_rows_without_process_cache(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
            cache=cache,
        )
        cache.clear()
        assert vs.list_grants(conn, cache=cache) == []
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"


def test_resolve_protected_without_grant_returns_approval_card(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        vs.create_secret(conn, name="GROUP_KEY", protection="protected", group="crypto", sealed=_sealed("group"))
    with vault.begin() as conn:
        result = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            session_id="ses_1",
            requester={"session_id": "ses_1", "skill": "deploy"},
            delivery={"command": "python sync.py", "egress": "local child process", "skill": "deploy"},
        )
    assert result["status"] == "approval_required"
    card = result["request"]["card"]
    assert card["card_type"] == "approval"
    assert card["secret_name"] == "PROTECTED_KEY"
    assert card["command"] == "python sync.py"
    assert any(option["scope_type"] == "secret" for option in card["scope_options"])
    assert all("value" not in json.dumps(option) for option in card["scope_options"])
    assert card["secret_unlock_material"] == {
        "name": "PROTECTED_KEY",
        "kind": "static",
        "envelope": {"ciphertext": "ct-1", "nonce": "n-1", "wrap_meta": "wm-1"},
    }
    group_option = next(option for option in card["scope_options"] if option["scope_type"] == "group")
    assert group_option["member_snapshot"] == ["GROUP_KEY", "PROTECTED_KEY"]
    assert group_option["unlock_material"] == [
        {
            "name": "GROUP_KEY",
            "kind": "static",
            "envelope": {"ciphertext": "ct-group", "nonce": "n-group", "wrap_meta": "wm-group"},
        },
        {
            "name": "PROTECTED_KEY",
            "kind": "static",
            "envelope": {"ciphertext": "ct-1", "nonce": "n-1", "wrap_meta": "wm-1"},
        },
    ]
    assert all("member_versions" in option for option in card["scope_options"])
    with vault.connect() as conn:
        request_row = conn.execute(select(vault_requests).where(vault_requests.c.id == result["request"]["id"])).mappings().one()
        delivery = json.loads(request_row["delivery"])
        audit_delivery = conn.execute(
            select(vault_audit.c.delivery).where(vault_audit.c.event == "access_requested")
        ).scalar_one()
    persisted = json.dumps({"delivery": delivery, "audit_delivery": audit_delivery})
    assert "secret_unlock_material" not in persisted
    assert "unlock_material" not in persisted
    assert "ct-1" not in persisted
    assert "ct-group" not in persisted


def test_request_inbox_hydrates_unlock_material_without_persisting_it(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        req = vs.create_access_request(conn, "PROTECTED_KEY", delivery={"command": "python sync.py"})
    with vault.connect() as conn:
        listed = vs.list_requests(conn)
        raw_delivery = conn.execute(select(vault_requests.c.delivery).where(vault_requests.c.id == req["id"])).scalar_one()

    assert listed[0]["card"]["secret_unlock_material"]["envelope"] == {
        "ciphertext": "ct-1",
        "nonce": "n-1",
        "wrap_meta": "wm-1",
    }
    assert "secret_unlock_material" not in raw_delivery
    assert "ct-1" not in raw_delivery


def test_resolve_access_card_uses_delivery_session_fallback(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        result = vs.resolve_secret_access(conn, "PROTECTED_KEY", session_id="ses_delivery", requester={}, delivery={})
        card = result["request"]["card"]
        assert card["session_id"] == "ses_delivery"
        req_id = result["request"]["id"]
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            created_by_request_id=req_id,
            deks_by_secret={"PROTECTED_KEY": "dek"},
        )
    assert grant["session_id"] == "ses_delivery"


def test_grant_can_be_intentionally_unbound_from_request_session(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    with vault.begin() as conn:
        req = _access_request(conn, "PROTECTED_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            created_by_request_id=req["id"],
            deks_by_secret={"PROTECTED_KEY": "dek"},
            inherit_request_session=False,
            cache=cache,
        )

    assert grant["session_id"] is None
    assert cache.has(grant["id"], "PROTECTED_KEY")


def test_keypair_and_always_ask_are_not_grantable(vault):
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    _create(vault, name="STATIC_KEY", protection="protected", policy={"always_ask": True})
    with vault.begin() as conn:
        with pytest.raises(vs.NotGrantableError):
            vs.create_access_request(conn, "ETH_KEY", requester={"session_id": "ses_1"})
        with pytest.raises(vs.NotGrantableError):
            vs.create_access_request(conn, "STATIC_KEY", requester={"session_id": "ses_1"})
        requests = conn.execute(
            select(vault_requests).where(vault_requests.c.secret_name.in_(["ETH_KEY", "STATIC_KEY"]))
        ).mappings().all()
        sign_req = vs.create_sign_request(conn, "ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")

    assert requests == []
    assert sign_req["request_type"] == "sign"


def test_always_ask_access_request_is_rejected_until_one_shot_approval_exists(vault):
    _create(vault, name="ASK_KEY", protection="protected", group="crypto", policy={"always_ask": True})
    _create(vault, name="GROUP_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        conn.execute(vault_links.insert().values(id="ln_ask", secret_name="ASK_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        conn.execute(vault_links.insert().values(id="ln_group", secret_name="GROUP_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        with pytest.raises(vs.NotGrantableError):
            vs.resolve_secret_access(
                conn,
                "ASK_KEY",
                session_id="ses_1",
                requester={"session_id": "ses_1", "skill": "deploy"},
                delivery={"skill": "deploy"},
            )
        requests = conn.execute(select(vault_requests).where(vault_requests.c.secret_name == "ASK_KEY")).mappings().all()

    assert requests == []


def test_grant_creation_requires_released_dek_set(vault):
    _create(vault, name="A_KEY", protection="protected")
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY")
        with pytest.raises(vs.InvalidGrantError):
            vs.create_grant(conn, scope_type="secret", scope_ref="A_KEY", created_by_request_id=req["id"])


def test_grant_creation_must_match_pending_access_request(vault):
    _create(vault, name="A_KEY", protection="protected")
    _create(vault, name="B_KEY", protection="protected")
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    with vault.begin() as conn:
        access_req = _access_request(conn, "A_KEY", session_id="ses_1")
        sign_req = vs.create_sign_request(conn, "ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="B_KEY",
                session_id="ses_1",
                created_by_request_id=access_req["id"],
                deks_by_secret={"B_KEY": "dek-b"},
            )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=sign_req["id"],
                deks_by_secret={"A_KEY": "dek-a"},
            )
        vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=access_req["id"],
            deks_by_secret={"A_KEY": "dek-a"},
        )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=access_req["id"],
                deks_by_secret={"A_KEY": "dek-a"},
            )


def test_grant_creation_rejects_expired_access_request(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantDekCache()
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with vault.begin() as conn:
        req = vs.create_access_request(conn, "A_KEY", expires_at=expired_at)
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                created_by_request_id=req["id"],
                deks_by_secret={"A_KEY": "dek-a"},
                cache=cache,
            )
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()
        grants = conn.execute(select(vault_grants)).mappings().all()

    assert status == "expired"
    assert grants == []


def test_rotating_protected_secret_expires_pending_access_and_sign_requests(vault):
    _create(vault, name="A_KEY", protection="protected")
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    with vault.begin() as conn:
        access_req = _access_request(conn, "A_KEY", session_id="ses_1")
        vs.rotate_secret(conn, "A_KEY", _sealed("rotated"))
        access_status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == access_req["id"])).scalar_one()
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                created_by_request_id=access_req["id"],
                deks_by_secret={"A_KEY": "dek-a"},
            )

        sign_req = vs.create_sign_request(conn, "ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")
        vs.rotate_secret(conn, "ETH_KEY", _sealed("rotated-key"))
        sign_status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == sign_req["id"])).scalar_one()
        with pytest.raises(vs.InvalidRequestError):
            vs.complete_sign_request(
                conn,
                sign_req["id"],
                name="ETH_KEY",
                digest="00" * 32,
                scheme="ecdsa-secp256k1-recoverable",
                signature={"signature": "sig"},
            )

    assert access_status == "expired"
    assert sign_status == "expired"


def test_deleting_protected_secret_expires_pending_access_requests(vault):
    _create(vault, name="A_KEY", protection="protected")
    with vault.begin() as conn:
        access_req = _access_request(conn, "A_KEY", session_id="ses_1")
        vs.delete_secret(conn, "A_KEY")
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == access_req["id"])).scalar_one()

    assert status == "expired"


def test_sign_request_completion_can_only_claim_pending_once(vault):
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    digest = "00" * 32
    with vault.begin() as conn:
        sign_req = vs.create_sign_request(conn, "ETH_KEY", digest=digest, scheme="ecdsa-secp256k1-recoverable")
        first = vs.complete_sign_request(
            conn,
            sign_req["id"],
            name="ETH_KEY",
            digest=digest,
            scheme="ecdsa-secp256k1-recoverable",
            signature={"signature": "ab" * 64, "recovery_id": 1},
        )
        with pytest.raises(vs.InvalidRequestError):
            vs.complete_sign_request(
                conn,
                sign_req["id"],
                name="ETH_KEY",
                digest=digest,
                scheme="ecdsa-secp256k1-recoverable",
                signature={"signature": "cd" * 64, "recovery_id": 1},
            )
        signed_events = [
            row["event"]
            for row in conn.execute(select(vault_audit.c.event).where(vault_audit.c.event == "signed")).mappings()
        ]
        meta = vs.get_secret_meta(conn, "ETH_KEY")

    assert first["status"] == "approved"
    assert signed_events == ["signed"]
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None


def test_sign_request_completion_rejects_malformed_signatures(vault):
    _create(vault, name="ETH_KEY", protection="protected", kind="keypair", signer_kind="local")
    digest = "00" * 32
    with vault.begin() as conn:
        req = vs.create_sign_request(conn, "ETH_KEY", digest=digest, scheme="ecdsa-secp256k1-recoverable")
        with pytest.raises(vs.InvalidRequestError):
            vs.complete_sign_request(
                conn,
                req["id"],
                name="ETH_KEY",
                digest=digest,
                scheme="ecdsa-secp256k1-recoverable",
                signature={"signature": "not-hex", "recovery_id": 1},
            )
        with pytest.raises(vs.InvalidRequestError):
            vs.complete_sign_request(
                conn,
                req["id"],
                name="ETH_KEY",
                digest=digest,
                scheme="ecdsa-secp256k1-recoverable",
                signature={"signature": "ab" * 64},
            )
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()
        meta = vs.get_secret_meta(conn, "ETH_KEY")

    assert status == "pending"
    assert meta["use_count"] == 0


def test_skill_grant_uses_vault_links(vault):
    _create(vault, name="A_KEY", protection="protected")
    _create(vault, name="B_KEY", protection="protected")
    with vault.begin() as conn:
        conn.execute(vault_links.insert().values(id="ln_a", secret_name="A_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        conn.execute(vault_links.insert().values(id="ln_b", secret_name="B_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        req = _access_request(conn, "A_KEY", skill="deploy")
        grant = vs.create_grant(
            conn,
            scope_type="skill",
            scope_ref="deploy",
            session_id="ses_1",
            created_by_request_id=req["id"],
            deks_by_secret={"A_KEY": "dek-a", "B_KEY": "dek-b"},
        )
    assert grant["member_snapshot"] == ["A_KEY", "B_KEY"]


def test_scope_grant_rejects_stale_member_snapshot_after_rotation(vault):
    _create(vault, name="A_KEY", protection="protected")
    _create(vault, name="B_KEY", protection="protected")
    with vault.begin() as conn:
        conn.execute(vault_links.insert().values(id="ln_a", secret_name="A_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        conn.execute(vault_links.insert().values(id="ln_b", secret_name="B_KEY", skill_name="deploy", source="user", required=1, created_at="now"))
        req = _access_request(conn, "A_KEY", skill="deploy")
        vs.rotate_secret(conn, "B_KEY", _sealed("rotated-b"))
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="skill",
                scope_ref="deploy",
                session_id="ses_1",
                created_by_request_id=req["id"],
                deks_by_secret={"A_KEY": "dek-a", "B_KEY": "old-dek-b"},
            )
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()

    assert status == "pending"
