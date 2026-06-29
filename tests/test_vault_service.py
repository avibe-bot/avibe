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


def test_keypair_signing_public_key_surfaces_in_masked_meta(vault):
    public_key = "02" + "cd" * 32
    _create(
        vault,
        name="ETH_KEY",
        kind="keypair",
        signer_kind="local",
        public_meta={
            "signing_public_key": {
                "curve": "secp256k1",
                "public_key": public_key,
                "ignored": "nope",
            }
        },
    )

    with vault.connect() as conn:
        meta = vs.get_secret_meta(conn, "ETH_KEY")
        listed = vs.list_secrets(conn)

    assert meta["signing_public_key"] == {"curve": "secp256k1", "public_key": public_key}
    listed_meta = next(item for item in listed if item["name"] == "ETH_KEY")
    assert listed_meta["signing_public_key"] == {"curve": "secp256k1", "public_key": public_key}
    assert "ignored" not in listed_meta["signing_public_key"]


def test_pubkey_pin_does_not_change_secret_version(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        before = dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == "A_KEY")).mappings().one())
        vs.store_pubkey_pin(conn, "A_KEY", {"public_key": "02" + "ab" * 32, "fingerprint": "fp_123"})
        after = dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == "A_KEY")).mappings().one())
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
        )

    assert before["updated_at"] == after["updated_at"]
    assert grant["member_snapshot"] == ["A_KEY"]


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


def test_keypair_is_not_value_deliverable(vault):
    _create(vault, name="ETH_KEY", kind="keypair", signer_kind="local")
    with vault.connect() as conn:
        with pytest.raises(vs.KeypairNotValueDeliverableError):
            vs.get_envelope(conn, "ETH_KEY")
        with pytest.raises(vs.KeypairNotValueDeliverableError):
            vs.get_envelopes(conn, ["ETH_KEY"])
        with pytest.raises(vs.KeypairNotValueDeliverableError):
            vs.resolve_secret_access(conn, "ETH_KEY")
        assert vs.get_key_envelope(conn, "ETH_KEY") == _sealed()


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


def test_create_grant_freezes_scope_members_and_keeps_key_material_out_of_python(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            ttl_seconds=900,
            created_by_request_id=req["id"],
            cache=cache,
        )

    assert grant["scope_type"] == "group"
    assert grant["member_snapshot"] == ["A_KEY", "B_KEY"]
    assert grant["runtime_member_count"] == 2
    assert cache.get(grant["id"], "A_KEY") is None
    with vault.connect() as conn:
        row = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one())
    assert "dek-a" not in json.dumps(row)
    assert json.loads(row["member_snapshot"]) == ["A_KEY", "B_KEY"]


def test_find_active_grant_for_secrets_chooses_covering_scope(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    with vault.begin() as conn:
        req_a = _access_request(conn, "A_KEY", session_id="ses_1")
        secret_grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req_a["id"],
        )
        req_group = _access_request(conn, "A_KEY", session_id="ses_1")
        group_grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_group["id"],
        )

        selected = vs.find_active_grant_for_secrets(conn, ["A_KEY", "B_KEY"], session_id="ses_1")

    assert selected is not None
    assert selected["id"] == group_grant["id"]
    assert selected["id"] != secret_grant["id"]


def test_group_grant_approves_sibling_requests_in_same_session(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_a = _access_request(conn, "A_KEY", session_id="ses_1")
        req_b = _access_request(conn, "B_KEY", session_id="ses_1")
        req_other = _access_request(conn, "B_KEY", session_id="ses_2")
        grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_a["id"],
            cache=cache,
        )
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_requests.c.id, vault_requests.c.status).where(
                    vault_requests.c.id.in_([req_a["id"], req_b["id"], req_other["id"]])
                )
            ).mappings()
        }

    assert grant["member_snapshot"] == ["A_KEY", "B_KEY"]
    assert statuses == {
        req_a["id"]: "approved",
        req_b["id"]: "approved",
        req_other["id"]: "pending",
    }


def test_group_grant_expires_stale_sibling_requests(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with vault.begin() as conn:
        req_a = _access_request(conn, "A_KEY", session_id="ses_1")
        req_b = vs.create_access_request(
            conn,
            "B_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
            expires_at=expired_at,
        )
        vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_a["id"],
            cache=cache,
        )
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_requests.c.id, vault_requests.c.status).where(
                    vault_requests.c.id.in_([req_a["id"], req_b["id"]])
                )
            ).mappings()
        }

    assert statuses == {
        req_a["id"]: "approved",
        req_b["id"]: "expired",
    }


def test_grant_uses_approval_member_snapshot_not_later_group_members(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        vs.create_secret(conn, name="B_KEY", protection="protected", group="crypto", sealed=_sealed("b"))
        grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache=cache,
        )

    assert grant["member_snapshot"] == ["A_KEY"]
    assert grant["runtime_member_count"] == 1
    assert cache.has(grant["id"], "A_KEY")
    assert not cache.has(grant["id"], "B_KEY")


def test_reusing_approval_request_does_not_create_second_grant(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        first = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache=cache,
        )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=req["id"],
                cache=cache,
            )
        grants = list(conn.execute(select(vault_grants)).mappings())

    assert [row["id"] for row in grants] == [first["id"]]
    assert cache.has(first["id"], "A_KEY")
    assert cache.get(first["id"], "A_KEY") is None


def test_find_active_grant_uses_persisted_readiness_across_process_cache(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache=cache,
        )
        process_cache = vs.VaultGrantRuntimeCache()
        active = vs.find_active_grant_for_secret(conn, "A_KEY", session_id="ses_1", cache=process_cache)
        listed = vs.list_grants(conn, cache=process_cache)
        assert active is not None
        assert active["id"] == grant["id"]
        assert listed[0]["id"] == grant["id"]
        assert listed[0]["delivery_ready"] is True
        assert listed[0]["delivery_status"] == "agent_cache_ready"
        assert process_cache.has(grant["id"], "A_KEY")
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "active"


def test_unrelayed_grant_is_not_selected_for_delivery(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache_ready=False,
            cache=cache,
        )
        active = vs.find_active_grant_for_secret(conn, "A_KEY", session_id="ses_1", cache=cache)
        resolved = vs.resolve_secret_access(conn, "A_KEY", session_id="ses_1", create_request=False, cache=cache)
        ready = vs.mark_grant_agent_ready(conn, grant["id"], cache=cache)
        process_cache = vs.VaultGrantRuntimeCache()
        active_after_ready = vs.find_active_grant_for_secret(conn, "A_KEY", session_id="ses_1", cache=process_cache)

    assert active is None
    assert resolved["status"] == "approval_required"
    assert ready["delivery_ready"] is True
    assert active_after_ready is not None
    assert active_after_ready["id"] == grant["id"]
    assert process_cache.has(grant["id"], "A_KEY")


def test_mark_grant_agent_ready_refreshes_expiry_to_relay_ttl(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            ttl_seconds=300,
            created_by_request_id=req["id"],
            cache_ready=False,
            cache=cache,
        )
        before = datetime.now(timezone.utc)
        ready = vs.mark_grant_agent_ready(conn, grant["id"], ttl_seconds=300, cache=cache)
        after = datetime.now(timezone.utc)

    expires_at = datetime.fromisoformat(ready["expires_at"])
    assert before + timedelta(seconds=299) <= expires_at <= after + timedelta(seconds=301)
    assert ready["delivery_ready"] is True


def test_resolve_secret_prefers_cache_ready_grant_over_stale_scope(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_group = _access_request(conn, "A_KEY", session_id="ses_1")
        stale_group = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_group["id"],
            cache_ready=False,
            cache=cache,
        )
        req_secret = _access_request(conn, "A_KEY", session_id="ses_1")
        ready_secret = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req_secret["id"],
            cache=cache,
        )

        active = vs.find_active_grant_for_secret(conn, "A_KEY", session_id="ses_1", cache=cache)
        resolved = vs.resolve_secret_access(conn, "A_KEY", session_id="ses_1", create_request=False, cache=cache)

    assert active is not None
    assert active["id"] == ready_secret["id"]
    assert active["id"] != stale_group["id"]
    assert resolved["status"] == "agent_delivery_ready"
    assert resolved["grant"]["id"] == ready_secret["id"]


def test_agent_release_scope_requires_remaining_grants_cover_removed_members(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_narrow = _access_request(conn, "A_KEY", session_id="ses_narrow")
        narrow_grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_narrow",
            created_by_request_id=req_narrow["id"],
            cache=cache,
        )
        vs.create_secret(conn, name="B_KEY", protection="protected", group="crypto", sealed=_sealed("b"))
        req_group = _access_request(conn, "A_KEY", session_id="ses_group")
        group_grant = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_group",
            created_by_request_id=req_group["id"],
            cache=cache,
        )
        rows = [dict(conn.execute(select(vault_grants).where(vault_grants.c.id == group_grant["id"])).mappings().one())]
        vs.revoke_grant(conn, group_grant["id"], cache=cache)
        release_scopes = vs.agent_release_scopes_after_rows(conn, rows, cache=cache)

    assert release_scopes == [{"scope_type": "group", "scope_ref": "crypto"}]
    assert not cache.has(group_grant["id"], "A_KEY")
    assert not cache.has(narrow_grant["id"], "A_KEY")
    with vault.connect() as conn:
        listed = {grant["id"]: grant for grant in vs.list_grants(conn, status=None, cache=vs.VaultGrantRuntimeCache())}
    assert listed[narrow_grant["id"]]["delivery_ready"] is False


def test_agent_release_scope_skips_when_remaining_members_cover_removed_scope(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    _create(vault, name="B_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_1 = _access_request(conn, "A_KEY", session_id="ses_1")
        grant_1 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_1["id"],
            cache=cache,
        )
        req_2 = _access_request(conn, "A_KEY", session_id="ses_2")
        grant_2 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_2",
            created_by_request_id=req_2["id"],
            cache=cache,
        )
        rows = [dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_1["id"])).mappings().one())]
        vs.revoke_grant(conn, grant_1["id"], cache=cache)
        release_scopes = vs.agent_release_scopes_after_rows(conn, rows, cache=cache)

    assert release_scopes == []
    assert not cache.has(grant_1["id"], "A_KEY")
    assert cache.has(grant_2["id"], "A_KEY")


def test_agent_release_scope_ignores_unready_remaining_grants(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_1 = _access_request(conn, "A_KEY", session_id="ses_1")
        grant_1 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_1["id"],
            cache=cache,
        )
        req_2 = _access_request(conn, "A_KEY", session_id="ses_2")
        grant_2 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_2",
            created_by_request_id=req_2["id"],
            cache_ready=False,
            cache=cache,
        )
        rows = [dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_1["id"])).mappings().one())]
        vs.revoke_grant(conn, grant_1["id"], cache=cache)
        release_scopes = vs.agent_release_scopes_after_rows(conn, rows, cache=cache)

    assert release_scopes == [{"scope_type": "group", "scope_ref": "crypto"}]
    assert not cache.has(grant_1["id"], "A_KEY")
    assert not cache.has(grant_2["id"], "A_KEY")
    with vault.connect() as conn:
        listed = {grant["id"]: grant for grant in vs.list_grants(conn, status=None, cache=vs.VaultGrantRuntimeCache())}
    assert listed[grant_2["id"]]["delivery_ready"] is False


def test_agent_release_scope_ignores_expired_remaining_grants(vault):
    _create(vault, name="A_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req_1 = _access_request(conn, "A_KEY", session_id="ses_1")
        grant_1 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_1",
            created_by_request_id=req_1["id"],
            cache=cache,
        )
        req_2 = _access_request(conn, "A_KEY", session_id="ses_2")
        grant_2 = vs.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_2",
            created_by_request_id=req_2["id"],
            cache=cache,
        )
        conn.execute(
            vault_grants.update()
            .where(vault_grants.c.id == grant_2["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )
        rows = [dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_1["id"])).mappings().one())]
        vs.revoke_grant(conn, grant_1["id"], cache=cache)
        release_scopes = vs.agent_release_scopes_after_rows(conn, rows, cache=cache)

    assert release_scopes == [{"scope_type": "group", "scope_ref": "crypto"}]
    assert not cache.has(grant_1["id"], "A_KEY")
    assert not cache.has(grant_2["id"], "A_KEY")
    with vault.connect() as conn:
        listed = {grant["id"]: grant for grant in vs.list_grants(conn, status=None, cache=vs.VaultGrantRuntimeCache())}
    assert listed[grant_2["id"]]["status"] == "expired"
    assert listed[grant_2["id"]]["delivery_ready"] is False


def test_grant_runtime_cache_drops_coverage_at_expiry_without_key_material():
    cache = vs.VaultGrantRuntimeCache()
    expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    cache.put("vgr_old", ["A_KEY"], expires_at=expires_at)

    assert not cache.has("vgr_old", "A_KEY")
    assert cache.get("vgr_old", "A_KEY") is None
    assert cache.covered_names("vgr_old") == []


def test_rotate_protected_secret_expires_active_grants(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
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
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
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


def test_unrelayed_active_grant_list_keeps_metadata_when_runtime_cache_is_empty(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache_ready=False,
            cache=cache,
        )
        cache.clear()
        listed = vs.list_grants(conn, cache=cache)
        assert len(listed) == 1
        assert listed[0]["id"] == grant["id"]
        assert listed[0]["runtime_member_count"] == 0
        assert listed[0]["delivery_ready"] is False
        assert listed[0]["delivery_status"] == "agent_cache_unverified"
    with vault.connect() as conn:
        status = conn.execute(select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "active"


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


def test_resolve_protected_with_grant_reports_agent_delivery_ready(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "PROTECTED_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache=cache,
        )
        result = vs.resolve_secret_access(conn, "PROTECTED_KEY", session_id="ses_1", create_request=False, cache=cache)

    assert result["status"] == "agent_delivery_ready"
    assert result["request"] is None
    assert result["grant"]["id"] == grant["id"]
    assert result["grant"]["delivery_ready"] is True
    assert result["envelope"] == _sealed()


def test_expire_grant_after_agent_cache_missing_reopens_approval(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "PROTECTED_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
            cache=cache,
        )
        expired = vs.expire_grant(conn, grant["id"], cache=cache, reason="grant-expired-agent-cache-missing")
        result = vs.resolve_secret_access(conn, "PROTECTED_KEY", session_id="ses_1", cache=cache)

    assert expired["status"] == "expired"
    assert result["status"] == "approval_required"
    assert result["request"]["secret_name"] == "PROTECTED_KEY"


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
        )
    assert grant["session_id"] == "ses_delivery"


def test_resolve_access_preserves_delivery_session_when_argument_is_missing(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        result = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            requester={},
            delivery={"session_id": "ses_delivery", "command": "deploy"},
            cache=cache,
        )
        card = result["request"]["card"]
        req_id = result["request"]["id"]
        raw_delivery = conn.execute(select(vault_requests.c.delivery).where(vault_requests.c.id == req_id)).scalar_one()
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            created_by_request_id=req_id,
            cache=cache,
        )
        same_session = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            delivery={"session_id": "ses_delivery"},
            create_request=False,
            cache=cache,
        )
        other_session = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            delivery={"session_id": "ses_other"},
            create_request=False,
            cache=cache,
        )

    assert card["session_id"] == "ses_delivery"
    assert json.loads(raw_delivery)["session_id"] == "ses_delivery"
    assert grant["session_id"] == "ses_delivery"
    assert same_session["status"] == "agent_delivery_ready"
    assert other_session["status"] == "approval_required"


def test_resolve_access_uses_requester_session_for_grant_lookup(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected", group="crypto")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = vs.create_access_request(
            conn,
            "PROTECTED_KEY",
            requester={"session_id": "ses_requester"},
        )
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            created_by_request_id=req["id"],
            cache=cache,
        )
        same_session = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            requester={"session_id": "ses_requester"},
            create_request=False,
            cache=cache,
        )
        other_session = vs.resolve_secret_access(
            conn,
            "PROTECTED_KEY",
            requester={"session_id": "ses_other"},
            create_request=False,
            cache=cache,
        )

    assert grant["session_id"] == "ses_requester"
    assert same_session["status"] == "agent_delivery_ready"
    assert same_session["grant"]["id"] == grant["id"]
    assert other_session["status"] == "approval_required"


def test_grant_can_be_intentionally_unbound_from_request_session(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    with vault.begin() as conn:
        req = _access_request(conn, "PROTECTED_KEY", session_id="ses_1")
        grant = vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="PROTECTED_KEY",
            created_by_request_id=req["id"],
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


def test_grant_creation_does_not_require_python_dek_material(vault):
    _create(vault, name="A_KEY", protection="protected")
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY")
        grant = vs.create_grant(conn, scope_type="secret", scope_ref="A_KEY", created_by_request_id=req["id"])

    assert grant["member_snapshot"] == ["A_KEY"]
    assert grant["delivery_status"] == "agent_cache_ready"


def test_grant_creation_rejects_expected_member_mismatch_without_claiming_request(vault):
    _create(vault, name="A_KEY", protection="protected")
    with vault.begin() as conn:
        req = _access_request(conn, "A_KEY")

    with pytest.raises(vs.InvalidGrantError):
        with vault.begin() as conn:
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                created_by_request_id=req["id"],
                expected_member_names={"B_KEY"},
            )

    with vault.connect() as conn:
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.secret_name == "A_KEY")).scalar_one()
        grants = conn.execute(select(vault_grants)).mappings().all()

    assert status == "pending"
    assert grants == []


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
            )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=sign_req["id"],
            )
        vs.create_grant(
            conn,
            scope_type="secret",
            scope_ref="A_KEY",
            session_id="ses_1",
            created_by_request_id=access_req["id"],
        )
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                session_id="ses_1",
                created_by_request_id=access_req["id"],
            )


def test_grant_creation_rejects_expired_access_request(vault):
    _create(vault, name="A_KEY", protection="protected")
    cache = vs.VaultGrantRuntimeCache()
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with vault.begin() as conn:
        req = vs.create_access_request(conn, "A_KEY", expires_at=expired_at)
        with pytest.raises(vs.InvalidRequestError):
            vs.create_grant(
                conn,
                scope_type="secret",
                scope_ref="A_KEY",
                created_by_request_id=req["id"],
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
            )
        status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == req["id"])).scalar_one()

    assert status == "pending"
