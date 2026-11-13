"""Atomic dialog saves: one field batch owns validation, storage and audit.

Per-field tests cannot catch a later field failing after an earlier edit was
stored, nor a default rename racing the browser's batch sealing step.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_audit, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from tests.ui_server_test_helpers import csrf_headers
from vibe import api
from vibe.ui_server import app


def sealed(label):
    return Sealed(ciphertext=f"ct-{label}", nonce=f"n-{label}", wrap_meta=f"wm-{label}")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    engine = create_sqlite_engine(tmp_path / "batch.sqlite")
    metadata.create_all(engine)
    vs.GRANT_RUNTIME_CACHE.clear()
    monkeypatch.setattr(api, "_vault_engine", lambda: engine)
    monkeypatch.setattr(api, "_publish_vaults_updated", Mock())
    monkeypatch.setattr(api, "release_vault_agent_scopes", Mock())
    yield engine
    engine.dispose()
    vs.GRANT_RUNTIME_CACHE.clear()


def create(vault, protection="standard", **kwargs):
    with vault.begin() as conn:
        return vs.create_secret(conn, name="LOGIN", protection=protection, sealed=sealed("default"), policy={"always_ask": True},
                                fields=[{"name": "username", "sealed": sealed("username")},
                                        {"name": "obsolete", "sealed": sealed("obsolete")}], **kwargs)


def snapshot(conn):
    return {
        "secret": dict(conn.execute(select(vault_secrets)).mappings().one()),
        "audit": [dict(row) for row in conn.execute(select(vault_audit)).mappings()],
        "requests": [dict(row) for row in conn.execute(select(vault_requests)).mappings()],
    }


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_batch_rename_replace_add_remove_commits_once(vault, protection):
    before = create(vault, protection)
    with vault.begin() as conn:
        request = vs.create_access_request(conn, "LOGIN")
        option = request["card"]["grant_options"][0]
        grant = vs.create_grant(conn, member_names=option["member_snapshot"], source_selector=option["source_selector"],
                                purpose=option["purpose"], request_id=request["id"], cache_ready=True)
        pending = vs.create_access_request(conn, "LOGIN")
        scopes = []
        result = vs.update_secret_fields(
            conn, "LOGIN", expected_updated_at=before["updated_at"], default_field="password",
            values=[{"field": "password", "record": "LOGIN", "sealed": sealed("new-default")},
                    {"field": "username", "record": "LOGIN.username", "sealed": sealed("new-user")},
                    {"field": "totp", "record": "LOGIN.totp", "sealed": sealed("totp")}],
            remove=["obsolete"], release_scopes=scopes,
        )
        assert result["fields"] == ["password", "username", "totp"]
        assert result["updated_at"] > before["updated_at"]
        get = vs.get_envelope if protection == "standard" else vs.get_protected_envelope
        assert get(conn, "LOGIN") == sealed("new-default")
        assert get(conn, "LOGIN.username") == sealed("new-user")
        assert get(conn, "LOGIN.totp") == sealed("totp")
        assert vs.get_request(conn, pending["id"])["status"] == "expired"
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "expired"
        assert scopes == ([{"grant_id": grant["id"]}] if protection == "protected" else [])
        audits = list(conn.execute(select(vault_audit.c.delivery).where(vault_audit.c.event == "updated")).scalars())
        assert [{key: json.loads(item)[key] for key in ("field", "op")} for item in audits] == [
            {"field": "password", "op": "replace"}, {"field": "username", "op": "replace"},
            {"field": "totp", "op": "add"}, {"field": "obsolete", "op": "remove"},
        ]
        assert not any(key in json.dumps(result) for key in ("ciphertext", "nonce", "wrap_meta"))


@pytest.mark.parametrize("case,error", [
    ("record", vs.RecordMismatchError), ("remove_missing", vs.FieldNotFoundError),
    ("remove_default", vs.InvalidRequestError), ("overlap", vs.InvalidRequestError),
    ("duplicates", vs.InvalidRequestError), ("rename_collision", vs.FieldExistsError),
    ("stale", vs.SecretConflictError),
])
def test_invalid_later_edit_leaves_everything_unchanged_even_if_error_is_caught(vault, case, error):
    meta = create(vault)
    values = [{"field": "value", "record": "LOGIN", "sealed": sealed("replacement")}]
    remove = []
    kwargs = {}
    if case == "record":
        values.append({"field": "username", "record": "LOGIN.wrong", "sealed": sealed("bad")})
    elif case == "remove_missing":
        remove = ["missing"]
    elif case == "remove_default":
        remove = ["value"]
    elif case == "overlap":
        values.append({"field": "username", "record": "LOGIN.username", "sealed": sealed("bad")})
        remove = ["username"]
    elif case == "duplicates":
        values *= 2
    elif case == "rename_collision":
        kwargs["default_field"] = "username"
    elif case == "stale":
        meta = {**meta, "updated_at": "stale"}
    with vault.begin() as conn:
        before = snapshot(conn)
        with pytest.raises(error):
            vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], values=values, remove=remove, **kwargs)
        assert snapshot(conn) == before


def test_audit_failure_rolls_back_complete_batch_inside_catching_outer_transaction(vault, monkeypatch):
    meta = create(vault)
    audit = vs.audit
    calls = 0

    def fail_second_audit(*args, **kwargs):
        nonlocal calls
        audit(*args, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(vs, "audit", fail_second_audit)
    with vault.begin() as conn:
        before = snapshot(conn)
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            vs.update_secret_fields(
                conn, "LOGIN", expected_updated_at=meta["updated_at"],
                values=[{"field": "value", "record": "LOGIN", "sealed": sealed("replacement")}], remove=["obsolete"],
            )
        assert snapshot(conn) == before
    with vault.connect() as conn:
        assert snapshot(conn) == before


def test_batch_stays_inside_callers_outer_transaction(vault):
    meta = create(vault)
    with pytest.raises(RuntimeError, match="outer rollback"):
        with vault.begin() as conn:
            vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], remove=["obsolete"])
            raise RuntimeError("outer rollback")
    with vault.connect() as conn:
        assert vs.get_secret_meta(conn, "LOGIN")["fields"] == meta["fields"]


def test_rename_only_preserves_versions_and_pending_authorization(vault):
    meta = create(vault)
    with vault.begin() as conn:
        request = vs.create_access_request(conn, "LOGIN")
        result = vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], default_field="password")
        assert result["fields"] == ["password", "username", "obsolete"]
        assert result["updated_at"] == meta["updated_at"]
        assert vs.get_request(conn, request["id"])["status"] == "pending"
        assert vs.get_envelope(conn, "LOGIN") == sealed("default")


@pytest.mark.parametrize("race,code", [("value", "secret_conflict"), ("rename", "record_mismatch")])
def test_api_revalidates_after_sealing_and_never_stores_partial_batch(vault, monkeypatch, race, code):
    meta = create(vault)
    called = False

    def seal_and_race(record, box):
        nonlocal called
        if not called:
            called = True
            with vault.begin() as winner:
                if race == "rename":
                    vs.update_secret_metadata(winner, "LOGIN", default_field="password")
                else:
                    vs.update_secret_field(winner, "LOGIN", "value", sealed("winner"), record="LOGIN")
        return sealed("loser")

    monkeypatch.setattr(api, "avault_seal_blind_box", seal_and_race)
    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret_fields("LOGIN", {
            "expected_updated_at": meta["updated_at"],
            "values": [{"field": "value", "record": "LOGIN", "blind_box": {"ct": "opaque"}},
                       {"field": "totp", "record": "LOGIN.totp", "blind_box": {"ct": "opaque"}}],
            "remove": ["obsolete"],
        })
    assert (exc.value.code, exc.value.status) == (code, 409)
    with vault.connect() as conn:
        assert vs.get_secret_meta(conn, "LOGIN")["fields"] == (["password", "username", "obsolete"] if race == "rename" else meta["fields"])
        assert vs.get_envelope(conn, "LOGIN") == sealed("default" if race == "rename" else "winner")
    api._publish_vaults_updated.assert_not_called()
    api.release_vault_agent_scopes.assert_not_called()


def test_api_later_sealing_failure_does_not_rename_or_write_first_envelope(vault, monkeypatch):
    meta = create(vault)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(side_effect=[sealed("first"), api.AvaultError("synthetic failure")]))
    with vault.connect() as conn:
        before = snapshot(conn)
    with pytest.raises(api.VaultApiError):
        api.update_vault_secret_fields("LOGIN", {
            "expected_updated_at": meta["updated_at"], "default_field": "password",
            "values": [{"field": "password", "record": "LOGIN", "blind_box": {"ct": "opaque"}},
                       {"field": "username", "record": "LOGIN.username", "blind_box": {"ct": "opaque"}}],
            "remove": ["obsolete"],
        })
    with vault.connect() as conn:
        assert snapshot(conn) == before


@pytest.mark.parametrize("race,exception", [("rename", vs.RecordMismatchError), ("description", vs.SecretConflictError)])
def test_storage_cas_detects_metadata_race_after_snapshot(vault, monkeypatch, race, exception):
    meta = create(vault)
    original = vs._require_secret_resource_management
    raced = False

    def race_after_snapshot(conn, row, context):
        nonlocal raced
        captured = original(conn, row, context)
        if not raced:
            raced = True
            with vault.begin() as winner:
                changes = {"default_field": "password"} if race == "rename" else {"description": "winner"}
                vs.update_secret_metadata(winner, "LOGIN", **changes)
        return captured

    monkeypatch.setattr(vs, "_require_secret_resource_management", race_after_snapshot)
    with vault.begin() as conn:
        with pytest.raises(exception):
            vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], remove=["obsolete"])
        assert vs.get_envelope(conn, "LOGIN.obsolete") == sealed("obsolete")
        assert not list(conn.execute(select(vault_audit).where(vault_audit.c.event == "updated")))


def test_batch_enforces_final_field_limit_not_intermediate_add_order(vault):
    with vault.begin() as conn:
        meta = vs.create_secret(conn, name="LOGIN", sealed=sealed("default"), fields=[
            {"name": f"field{i}", "sealed": sealed(str(i))} for i in range(31)
        ])
        with pytest.raises(vs.TooManyFieldsError):
            vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], values=[
                {"field": "new", "record": "LOGIN.new", "sealed": sealed("new")},
            ])
        updated = vs.update_secret_fields(conn, "LOGIN", expected_updated_at=meta["updated_at"], values=[
            {"field": "new", "record": "LOGIN.new", "sealed": sealed("new")},
        ], remove=["field0"])
        assert len(updated["fields"]) == 32
        assert "new" in updated["fields"] and "field0" not in updated["fields"]


@pytest.mark.parametrize("body,code", [
    ({}, "invalid_request"), ({"expected_updated_at": None}, "invalid_request"),
    ({"expected_updated_at": ""}, "invalid_request"),
    ({"unexpected": True}, "invalid_request"), ({"default_field": None}, "invalid_field_name"),
    ({"values": None}, "invalid_request"), ({"remove": None}, "invalid_request"),
    ({"values": [{"field": "totp", "record": "LOGIN.totp", "blind_box": {"ct": "opaque"}, "sealed": {}}]}, "invalid_request"),
    ({"values": [{"field": "totp", "record": "LOGIN.totp", "blind_box": {"value": "plaintext"}}]}, "plaintext_value_rejected"),
    ({"values": [{"field": "totp", "record": "LOGIN.wrong", "blind_box": {"ct": "opaque"}}]}, "record_mismatch"),
    ({"values": [{"field": "value", "record": "LOGIN", "blind_box": {"ct": "opaque"}},
                 {"field": "totp", "record": "LOGIN.totp", "sealed": {}}]}, "invalid_request"),
])
def test_api_rejects_entire_invalid_request_before_custody(vault, monkeypatch, body, code):
    meta = create(vault)
    seal = Mock(return_value=sealed("must-not-store"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with vault.connect() as conn:
        before = snapshot(conn)
    payload = {"expected_updated_at": meta["updated_at"], **body} if body else {}
    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret_fields("LOGIN", payload)
    assert exc.value.code == code
    seal.assert_not_called()
    with vault.connect() as conn:
        assert snapshot(conn) == before


@pytest.mark.parametrize("target", ["keypair", "corrupt"])
def test_batch_cannot_mutate_keypairs_or_partially_read_corrupt_fields(vault, monkeypatch, target):
    with vault.begin() as conn:
        if target == "keypair":
            meta = vs.create_secret(conn, name="LOGIN", kind="keypair", signer_kind="local", sealed=sealed("key"))
        else:
            meta = vs.create_secret(conn, name="LOGIN", sealed=sealed("default"))
            conn.execute(vault_secrets.update().values(fields="invalid-json"))
        before = snapshot(conn)
    seal = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret_fields("LOGIN", {"expected_updated_at": meta["updated_at"], "default_field": "password"})
    assert exc.value.code == ("fields_not_supported" if target == "keypair" else "fields_corrupt")
    seal.assert_not_called()
    with vault.connect() as conn:
        assert snapshot(conn) == before


def test_api_batch_management_acl_applies_before_custody(vault, monkeypatch):
    from storage.resource_access_service import ResourceUserContext

    meta = create(vault)
    seal = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret_fields("LOGIN", {
            "expected_updated_at": meta["updated_at"],
            "values": [{"field": "value", "record": "LOGIN", "blind_box": {"ct": "opaque"}}],
        }, user_context=ResourceUserContext(subject="editor", organization_id="org", organization_role="member", instance_role="editor", is_remote=True))
    assert exc.value.status == 403
    seal.assert_not_called()


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_rest_batch_uses_final_label_and_canonical_record(vault, monkeypatch, protection):
    meta = create(vault, protection)
    seal = Mock(return_value=sealed("new-default"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    entry = {"field": "password", "record": "LOGIN"}
    entry.update({"blind_box": {"ct": "opaque"}} if protection == "standard" else {"sealed": vars(sealed("new-default"))})
    client = app.test_client()
    response = client.post("/api/vault/secrets/LOGIN/fields", headers=csrf_headers(client), json={
        "expected_updated_at": meta["updated_at"], "default_field": "password", "values": [entry], "remove": ["obsolete"],
    })
    assert response.status_code == 200
    assert response.get_json()["secret"]["fields"] == ["password", "username"]
    if protection == "standard":
        seal.assert_called_once_with("LOGIN", {"ct": "opaque"})
    else:
        seal.assert_not_called()
    stale = client.post("/api/vault/secrets/LOGIN/fields", headers=csrf_headers(client), json={
        "expected_updated_at": meta["updated_at"], "remove": ["username"],
    })
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "secret_conflict"
