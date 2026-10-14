"""Unit tests for the Vaults service final grant-id/tag model."""

from __future__ import annotations

import concurrent.futures
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from storage import vault_service as vs, vault_webauthn
from storage.db import create_sqlite_engine
from storage.models import (
    agent_sessions,
    metadata,
    state_meta,
    vault_audit,
    vault_auth_factors,
    vault_grants,
    vault_requests,
    vault_secrets,
)
from storage import message_deliveries
from storage.vault_crypto import Sealed
from tests.vault_webauthn_helpers import WebAuthnTestCredential


@pytest.fixture
def vault(tmp_path):
    vs.GRANT_RUNTIME_CACHE.clear()
    engine = create_sqlite_engine(tmp_path / "vault_test.sqlite")
    metadata.create_all(engine)
    return engine


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _create(engine, **kw):
    with engine.begin() as conn:
        return vs.create_secret(conn, sealed=_sealed(kw.get("name", "x").lower()), **kw)


def _establish_protected_secret(conn, name: str, credential: WebAuthnTestCredential | None = None) -> tuple[WebAuthnTestCredential, dict]:
    credential = credential or WebAuthnTestCredential()
    options = vs.create_webauthn_registration_options(
        conn,
        rp_id=credential.rp_id,
        origin=credential.origin,
    )
    vs.create_secret(
        conn,
        name=name,
        protection="protected",
        sealed=_sealed(name.lower()),
        establishing_vmk=True,
        authz_factor_registration=credential.registration_payload(
            challenge_id=options["challenge_id"],
            challenge_b64=options["webauthn"]["challenge"],
        ),
        authz_factor_origin=credential.origin,
    )
    factor = conn.execute(
        select(vault_auth_factors).where(vault_auth_factors.c.credential_id == credential.credential_id_b64)
    ).mappings().one()
    return credential, dict(factor)


def _grant_from_request(
    conn,
    request: dict,
    *,
    cache_ready: bool = True,
    ttl_seconds: int | None = None,
    expires_at: str | None = None,
) -> dict:
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        request_id=request["id"],
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        cache_ready=cache_ready,
    )


def test_secret_metadata_is_global_name_plus_tags(vault):
    meta = _create(vault, name="OPENAI_API_KEY", description="key", tags=["prod", "skill:deploy"])

    assert meta["name"] == "OPENAI_API_KEY"
    assert meta["tags"] == ["prod", "skill:deploy"]
    assert "group" not in meta
    assert "preview" not in meta
    assert meta["default_field"] == "value"
    assert meta["fields"] == ["value"]
    assert meta["last_used_at"] is None
    assert meta["policy"] == {}

    with vault.connect() as conn:
        row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "OPENAI_API_KEY")).mappings().one()
        assert row["ciphertext"] == "ct-openai_api_key"
        assert json.loads(row["public_meta"]) == {"description": "key"}
        assert vs.list_secrets(conn, tag="prod")[0]["name"] == "OPENAI_API_KEY"


def test_secret_names_preserve_case_and_reject_case_only_duplicates(vault):
    meta = _create(vault, name="openAiKey", description="key")

    assert meta["name"] == "openAiKey"
    with vault.connect() as conn:
        assert vs.get_secret_meta(conn, "openAiKey")["name"] == "openAiKey"
        with pytest.raises(vs.SecretNotFoundError):
            vs.get_secret_meta(conn, "OPENAIKEY")

    with pytest.raises(vs.SecretNameCaseConflictError) as exc:
        _create(vault, name="OpenAIKey")
    assert exc.value.existing_name == "openAiKey"


def test_secret_name_case_uniqueness_is_enforced_by_database(vault):
    _create(vault, name="openAiKey")

    with vault.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                vault_secrets.insert().values(
                    id="vlt_case_race",
                    name="OpenAIKey",
                    kind="static",
                    protection="standard",
                    source="manual",
                    ciphertext="ct",
                    nonce="nonce",
                    wrap_meta="wrap",
                    use_count=0,
                    created_at="now",
                    updated_at="now",
                )
            )


def test_provision_request_rejects_case_only_duplicate_secret(vault):
    _create(vault, name="OpenAIKey")

    with vault.begin() as conn:
        fulfilled = vs.create_provision_request(conn, "OpenAIKey")
        with pytest.raises(vs.SecretNameCaseConflictError) as exc:
            vs.create_provision_request(conn, "openAIKey")

    assert fulfilled["status"] == "fulfilled"
    assert exc.value.existing_name == "OpenAIKey"


def test_provision_request_uses_active_turn_as_plain_claude_anchor(vault):
    now = "2026-08-08T00:00:00+00:00"
    session_id = "ses_claude_anchor"
    delivery_id = "del_claude_anchor"
    turn_id = "turn_claude_anchor"
    with vault.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=None,
                agent_id=None,
                agent_name="claude",
                agent_backend="claude",
                agent_variant="claude",
                model=None,
                reasoning_effort=None,
                session_anchor=session_id,
                workdir="/tmp",
                native_session_id="",
                title=None,
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="busy",
                composer_draft_text=None,
                composer_draft_updated_at=None,
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p0",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=None,
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                text="request a key",
            ),
            dispatch_text="request a key",
            now=now,
        )
        message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session_id,
            backend="claude",
            deliveries=[delivery],
            dispatch_text="request a key",
        )
        request = vs.create_provision_request(
            conn,
            "CLAUDE_KEY",
            requester={"source": "agent-cli", "session_id": session_id},
        )

    assert request["status"] == "pending"
    row = request
    assert row["id"]
    with vault.connect() as conn:
        stored = conn.execute(select(vault_requests).where(vault_requests.c.id == row["id"])).mappings().one()
    assert stored["message_id"] is None
    assert json.loads(stored["requester"])["turn_id"] == turn_id


def test_provision_request_does_not_store_derived_delivery_as_reply_anchor(vault, monkeypatch):
    monkeypatch.setattr(
        vs,
        "_active_session_turn_anchor",
        lambda conn, session_id: ("msg_initial_delivery", "turn_active"),
    )

    with vault.begin() as conn:
        request = vs.create_provision_request(
            conn,
            "DERIVED_KEY",
            requester={"source": "agent-cli", "session_id": "ses_active"},
        )
        stored = conn.execute(
            select(vault_requests).where(vault_requests.c.id == request["id"])
        ).mappings().one()

    assert stored["message_id"] is None
    assert json.loads(stored["requester"])["turn_id"] == "turn_active"


def test_provision_request_rejects_case_only_duplicate_pending_request(vault):
    with vault.begin() as conn:
        pending = vs.create_provision_request(conn, "openAiKey")
        with pytest.raises(vs.SecretNameCaseConflictError) as exc:
            vs.create_provision_request(conn, "OpenAIKey")

        rows = (
            conn.execute(select(vault_requests).where(vault_requests.c.request_type == "provision"))
            .mappings()
            .all()
        )

    assert pending["status"] == "pending"
    assert exc.value.existing_name == "openAiKey"
    assert [row["secret_name"] for row in rows] == ["openAiKey"]


def test_provision_request_case_guard_is_database_enforced(vault):
    with vault.begin() as conn:
        conn.execute(
            vault_requests.insert().values(
                id="vrq_a",
                request_type="provision",
                secret_name="openAiKey",
                status="pending",
                delivery="{}",
                created_at="now",
            )
        )
        conn.execute(
            vault_requests.insert().values(
                id="vrq_exact_duplicate",
                request_type="provision",
                secret_name="openAiKey",
                status="pending",
                delivery="{}",
                created_at="now",
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                vault_requests.insert().values(
                    id="vrq_b",
                    request_type="provision",
                    secret_name="OpenAIKey",
                    status="pending",
                    delivery="{}",
                    created_at="now",
                )
            )


def test_create_secret_rejects_case_only_duplicate_pending_request(vault):
    with vault.begin() as conn:
        pending = vs.create_provision_request(conn, "openAiKey")
        with pytest.raises(vs.SecretNameCaseConflictError) as exc:
            vs.create_secret(conn, name="OpenAIKey", sealed=_sealed("case"))

        secrets = conn.execute(select(vault_secrets.c.name)).scalars().all()
        requests = (
            conn.execute(select(vault_requests.c.secret_name, vault_requests.c.status))
            .mappings()
            .all()
        )

    assert pending["status"] == "pending"
    assert exc.value.existing_name == "openAiKey"
    assert secrets == []
    assert [dict(row) for row in requests] == [{"secret_name": "openAiKey", "status": "pending"}]


def test_skill_links_are_stored_as_skill_tags(vault):
    _create(vault, name="GH_PAT", tags=["github"])

    with vault.begin() as conn:
        vs.link_secret_to_skills(conn, "GH_PAT", ["github-release", "github-release"])
        meta = vs.get_secret_meta(conn, "GH_PAT")

    assert meta["tags"] == ["github", "skill:github-release"]


@pytest.mark.parametrize(
    "value",
    ["", "value ", " value", "Value", "value-name", "value.name", "a" * 65, None],
)
def test_validate_field_name_is_strict(value):
    with pytest.raises(vs.InvalidFieldNameError):
        vs.validate_field_name(value)


def test_provision_field_spec_contains_only_valid_distinct_labels():
    assert vs.normalize_provision_spec({"fields": ["username", "password"]}) == {
        "fields": ["username", "password"], "default_field": "username",
    }
    assert vs.normalize_provision_spec({
        "fields": ["username", "password"], "default_field": "password",
    })["default_field"] == "password"
    for invalid in [
        {"default_field": "password"},
        {"fields": []},
        {"fields": ["password", "password"]},
        {"fields": ["username"], "default_field": "password"},
        {"fields": ["password "]},
        {"fields": [{"name": "password", "value": "plaintext"}]},
    ]:
        with pytest.raises(vs.VaultServiceError):
            vs.normalize_provision_spec(invalid)


def test_fields_use_canonical_records_and_default_label_fallback(vault):
    _create(
        vault,
        name="lowerSecret",
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )

    with vault.begin() as conn:
        meta = vs.get_secret_meta(conn, "lowerSecret")
        default_ref = vs.get_secret_record_meta(conn, "lowerSecret.password")
        extra_ref = vs.get_secret_record_meta(conn, "lowerSecret.username")

        assert meta["default_field"] == "password"
        assert meta["fields"] == ["password", "username"]
        assert vs.secret_record_names(meta) == ["lowerSecret", "lowerSecret.username"]
        assert default_ref["record_name"] == "lowerSecret"
        assert default_ref["field"] == "password"
        assert extra_ref["record_name"] == "lowerSecret.username"
        assert all(key not in json.dumps(meta) for key in ("ciphertext", "nonce", "wrap_meta"))
        assert vs.get_envelope(conn, "lowerSecret") == _sealed("lowersecret")
        assert vs.get_envelope(conn, "lowerSecret.username") == _sealed("username")

        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "lowerSecret")
            .values(public_meta=json.dumps({"default_field": "INVALID"}))
        )
        assert vs.get_secret_meta(conn, "lowerSecret")["default_field"] == "value"


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_default_rename_is_metadata_only_and_collision_safe(vault, protection):
    _create(
        vault,
        name="LOGIN",
        protection=protection,
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
        policy={"always_ask": True},
    )

    with vault.begin() as conn:
        request = vs.create_access_request(conn, "LOGIN")
        grant = _grant_from_request(conn, request)
        pending = vs.create_access_request(conn, "LOGIN")
        pending_versions = pending["card"]["grant_options"][0]["member_versions"]
        before = vs.get_secret_meta(conn, "LOGIN")
        get_envelope = vs.get_protected_envelope if protection == "protected" else vs.get_envelope
        default_envelope = get_envelope(conn, "LOGIN")
        renamed = vs.update_secret_metadata(conn, "LOGIN", default_field="credential")
        after = vs.get_secret_meta(conn, "LOGIN")

        assert renamed["default_field"] == "credential"
        assert renamed["fields"] == ["credential", "username"]
        assert renamed["policy"] == {"always_ask": True}
        assert after["updated_at"] == before["updated_at"]
        hydrated = vs.get_request(conn, pending["id"])
        assert hydrated["status"] == "pending"
        assert hydrated["card"]["grant_options"][0]["member_versions"] == pending_versions
        assert hydrated["card"]["grant_options"][0]["member_fields"] == [
            {"name": "LOGIN", "default_field": "credential", "fields": ["credential", "username"]}
        ]
        # The persisted authorization snapshot is unchanged; only its UI projection is current.
        stored = conn.execute(select(vault_requests.c.delivery).where(vault_requests.c.id == pending["id"])).scalar_one()
        assert json.loads(stored)["card"]["grant_options"][0]["member_fields"][0]["default_field"] == "password"
        assert _grant_from_request(conn, pending)["status"] == "active"
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "active"
        assert get_envelope(conn, "LOGIN") == default_envelope
        with pytest.raises(vs.RecordMismatchError):
            vs.update_secret_field(
                conn, "LOGIN", "password", _sealed("stale-default"), record="LOGIN",
                expected_updated_at=before["updated_at"],
            )
        with pytest.raises(vs.FieldExistsError):
            vs.update_secret_metadata(conn, "LOGIN", default_field="username")
        assert get_envelope(conn, "LOGIN") == default_envelope


@pytest.mark.parametrize("malformed", ["invalid_json", "non_array", "bad_entry", "duplicate", "default_collision"])
@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_corrupt_fields_disable_mutations_without_losing_bytes(vault, caplog, malformed, protection):
    _create(vault, name="LOGIN", protection=protection, fields=[{"name": "username", "sealed": _sealed("username")}])
    with vault.begin() as conn:
        stored = conn.execute(select(vault_secrets.c.fields).where(vault_secrets.c.name == "LOGIN")).scalar_one()
        valid = json.loads(stored)
        damaged = {
            "invalid_json": '{"unparseable":',
            "non_array": '{"name":"username"}',
            "bad_entry": json.dumps([*valid, {"name": "password", "ciphertext": "must-not-disappear"}]),
            "duplicate": json.dumps([*valid, valid[0]]),
            "default_collision": json.dumps([*valid, {**valid[0], "name": "value"}]),
        }[malformed]
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "LOGIN").values(fields=damaged))
        caplog.clear()
        meta = vs.get_secret_meta(conn, "LOGIN")
        assert meta["fields"] == ["value"]
        assert meta["fields_state"] == "corrupt"
        assert vs.list_secrets(conn)[0]["fields_state"] == "corrupt"
        get_envelope = vs.get_protected_record_envelope if protection == "protected" else vs.get_envelope
        assert get_envelope(conn, "LOGIN") == _sealed("login")
        assert get_envelope(conn, "LOGIN.value") == _sealed("login")
        assert sum("corrupt fields" in entry.message.lower() for entry in caplog.records) == 1
        assert "must-not-disappear" not in caplog.text
        before = dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == "LOGIN")).mappings().one())
        with pytest.raises(vs.FieldsCorruptError):
            get_envelope(conn, "LOGIN.username")
        operations = [
            lambda: vs.update_secret_field(conn, "LOGIN", "totp", _sealed("new"), record="LOGIN.totp"),
            lambda: vs.update_secret_field(conn, "LOGIN", "username", _sealed("new"), record="LOGIN.username"),
            lambda: vs.update_secret_field(conn, "LOGIN", "value", _sealed("new"), record="LOGIN"),
            lambda: vs.remove_secret_field(conn, "LOGIN", "username"),
            lambda: vs.update_secret_metadata(conn, "LOGIN", default_field="password"),
            lambda: vs.rotate_secret(conn, "LOGIN", _sealed("new")),
        ]
        for operation in operations:
            with pytest.raises(vs.FieldsCorruptError):
                operation()
            after = dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == "LOGIN")).mappings().one())
            assert after == before
            assert after["fields"] == damaged
        assert not list(conn.execute(select(vault_audit.c.id).where(vault_audit.c.event == "updated")))


def test_ordinary_metadata_preserves_approval_and_policy_edits_expire_it(vault):
    _create(
        vault, name="LOGIN", protection="protected", description="old",
        policy={"always_ask": True, "allowed_hosts": ["example.com"]},
    )
    with vault.begin() as conn:
        before = vs.get_secret_meta(conn, "LOGIN")
        request = vs.create_access_request(conn, "LOGIN")
        edited = vs.update_secret_metadata(conn, "LOGIN", description="new", tags=["new"])
        assert edited["updated_at"] == before["updated_at"]
        hydrated = vs.get_request(conn, request["id"])
        assert hydrated["card"]["grant_options"][0]["unlock_material"][0]["name"] == "LOGIN"
        grant = _grant_from_request(conn, request)
        scopes = []
        updated = vs.update_secret_metadata(
            conn, "LOGIN", policy={"allowed_hosts": ["new.example.com"]}, release_scopes=scopes,
        )
        assert updated["policy"] == {"always_ask": True, "allowed_hosts": ["new.example.com"]}
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "expired"
        assert scopes == [{"grant_id": grant["id"]}]


@pytest.mark.parametrize("operation", ["add", "replace_default", "replace_extra", "remove"])
def test_field_write_cas_without_explicit_version_has_no_conflict_side_effects(vault, monkeypatch, operation):
    _create(
        vault, name="LOGIN", protection="protected",
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )
    with vault.begin() as conn:
        grant = _grant_from_request(conn, vs.create_access_request(conn, "LOGIN"))
        pending = vs.create_access_request(conn, "LOGIN")

    require_management = vs._require_secret_resource_management
    raced = False

    def write_after_snapshot(conn, row, context):
        nonlocal raced
        snapshot = require_management(conn, row, context)
        if not raced:
            raced = True
            with vault.begin() as winner:
                vs.update_secret_field(winner, "LOGIN", "other", _sealed("winner"), record="LOGIN.other")
        return snapshot

    monkeypatch.setattr(vs, "_require_secret_resource_management", write_after_snapshot)
    release_scopes = []
    with vault.begin() as conn:
        with pytest.raises(vs.SecretConflictError):
            if operation == "remove":
                vs.remove_secret_field(conn, "LOGIN", "username", release_scopes=release_scopes)
            else:
                field = {"add": "totp", "replace_default": "value", "replace_extra": "username"}[operation]
                record = "LOGIN" if field == "value" else f"LOGIN.{field}"
                vs.update_secret_field(conn, "LOGIN", field, _sealed("loser"), record=record, release_scopes=release_scopes)
        assert vs.get_secret_meta(conn, "LOGIN")["fields"] == ["value", "username", "other"]
        assert vs.get_protected_envelope(conn, "LOGIN") == _sealed("login")
        assert vs.get_protected_envelope(conn, "LOGIN.username") == _sealed("username")
        assert vs.get_protected_envelope(conn, "LOGIN.other") == _sealed("winner")
        # Only the winning write changed authorization and produced a field audit.
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "expired"
        assert vs.get_request(conn, pending["id"])["status"] == "expired"
        assert vs.GRANT_RUNTIME_CACHE.covered_names(grant["id"]) == []
        updates = list(conn.execute(select(vault_audit.c.delivery).where(vault_audit.c.event == "updated")).scalars())
        assert [json.loads(payload)["field"] for payload in updates] == ["other"]
        assert release_scopes == []


@pytest.mark.parametrize("field,record", [("value", "LOGIN"), ("password", "LOGIN.password")])
def test_protected_field_write_rejects_rename_after_record_resolution(vault, monkeypatch, field, record):
    _create(vault, name="LOGIN", protection="protected")
    with vault.begin() as conn:
        grant = _grant_from_request(conn, vs.create_access_request(conn, "LOGIN"))
        pending = vs.create_access_request(conn, "LOGIN")
        before = vs.get_secret_meta(conn, "LOGIN")
    original = vs._require_secret_resource_management
    raced = False

    def rename_after_snapshot(conn, row, context):
        nonlocal raced
        snapshot = original(conn, row, context)
        if not raced:
            raced = True
            with vault.begin() as winner:
                vs.update_secret_metadata(winner, "LOGIN", default_field="password")
        return snapshot

    monkeypatch.setattr(vs, "_require_secret_resource_management", rename_after_snapshot)
    release_scopes = []
    with vault.begin() as conn:
        with pytest.raises(vs.RecordMismatchError):
            vs.update_secret_field(conn, "LOGIN", field, _sealed("loser"), record=record, release_scopes=release_scopes)
        assert vs.get_protected_envelope(conn, "LOGIN") == _sealed("login")
        after = vs.get_secret_meta(conn, "LOGIN")
        assert after["updated_at"] == before["updated_at"]
        assert after["fields"] == ["password"]
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "active"
        assert vs.get_request(conn, pending["id"])["status"] == "pending"
        assert release_scopes == []


@pytest.mark.parametrize("operation", ["rename", "pin", "rotate"])
def test_metadata_writers_do_not_overwrite_a_concurrent_description(vault, monkeypatch, operation):
    _create(vault, name="LOGIN", description="old")
    original = vs._require_secret_resource_management
    raced = False

    def edit_after_snapshot(conn, row, context):
        nonlocal raced
        snapshot = original(conn, row, context)
        if not raced:
            raced = True
            with vault.begin() as winner:
                vs.update_secret_metadata(winner, "LOGIN", description="concurrent")
        return snapshot

    monkeypatch.setattr(vs, "_require_secret_resource_management", edit_after_snapshot)
    with vault.begin() as conn:
        with pytest.raises(vs.SecretConflictError):
            if operation == "rename":
                vs.update_secret_metadata(conn, "LOGIN", default_field="password")
            elif operation == "pin":
                vs.store_pubkey_pin(conn, "LOGIN", {"public_key": "public", "fingerprint": "fingerprint"})
            else:
                vs.rotate_secret(conn, "LOGIN", _sealed("loser"))
        assert vs.get_secret_meta(conn, "LOGIN")["description"] == "concurrent"
        assert vs.get_envelope(conn, "LOGIN") == _sealed("login")


def test_field_writes_replace_add_remove_with_versioned_cas(vault):
    _create(vault, name="LOGIN")

    with vault.begin() as conn:
        original = vs.get_secret_meta(conn, "LOGIN")
        added = vs.update_secret_field(
            conn,
            "LOGIN",
            "username",
            _sealed("username"),
            record="LOGIN.username",
            expected_updated_at=original["updated_at"],
        )
        replaced = vs.update_secret_field(
            conn,
            "LOGIN",
            "value",
            _sealed("replacement"),
            record="LOGIN",
            expected_updated_at=added["updated_at"],
        )

        assert replaced["updated_at"] > added["updated_at"]
        assert vs.get_envelope(conn, "LOGIN") == _sealed("replacement")
        with pytest.raises(vs.SecretConflictError):
            vs.update_secret_field(
                conn,
                "LOGIN",
                "username",
                _sealed("stale"),
                record="LOGIN.username",
                expected_updated_at=original["updated_at"],
            )
        assert vs.get_secret_meta(conn, "LOGIN")["fields"] == ["value", "username"]

        removed = vs.remove_secret_field(
            conn,
            "LOGIN",
            "username",
            expected_updated_at=replaced["updated_at"],
        )
        assert removed["fields"] == ["value"]
        assert removed["updated_at"] > replaced["updated_at"]
        with pytest.raises(vs.SecretConflictError):
            vs.remove_secret_field(
                conn,
                "LOGIN",
                "username",
                expected_updated_at=replaced["updated_at"],
            )


def test_field_limits_duplicates_and_version_monotonicity(vault):
    with vault.begin() as conn:
        with pytest.raises(vs.VaultServiceError, match="fields must be an array"):
            vs.create_secret(conn, name="INVALID", sealed=_sealed(), fields={})
        with pytest.raises(vs.FieldsNotSupportedError):
            vs.create_secret(
                conn, name="INVALID", sealed=_sealed(), kind="keypair",
                fields=[{"name": "username", "sealed": _sealed()}],
            )
        for names in [["password"], ["username", "username"]]:
            with pytest.raises(vs.FieldExistsError):
                vs.create_secret(
                    conn, name="INVALID", sealed=_sealed(), default_field="password",
                    fields=[{"name": name, "sealed": _sealed()} for name in names],
                )
        with pytest.raises(vs.TooManyFieldsError):
            vs.create_secret(
                conn, name="INVALID", sealed=_sealed(),
                fields=[{"name": f"field{i}", "sealed": _sealed()} for i in range(32)],
            )
        vs.create_secret(
            conn, name="LOGIN", sealed=_sealed(),
            fields=[{"name": f"field{i}", "sealed": _sealed()} for i in range(31)],
        )
        with pytest.raises(vs.TooManyFieldsError):
            vs.update_secret_field(conn, "LOGIN", "overflow", _sealed(), record="LOGIN.overflow")
        with pytest.raises(vs.InvalidRequestError, match="default field"):
            vs.remove_secret_field(conn, "LOGIN", "value")
        with pytest.raises(vs.FieldNotFoundError, match="fields: value, field0"):
            vs.get_envelope(conn, "LOGIN.unknown")
        # A persisted future timestamp models a clock that moved backwards.
        future = "2100-01-01T00:00:00+00:00"
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "LOGIN").values(updated_at=future))
        replaced = vs.update_secret_field(conn, "LOGIN", "field0", _sealed("replacement"), record="LOGIN.field0")
        assert replaced["updated_at"] == "2100-01-01T00:00:00.000001+00:00"
        removed = vs.remove_secret_field(conn, "LOGIN", "field0")
        assert removed["updated_at"] == "2100-01-01T00:00:00.000002+00:00"
        assert vs.get_envelope(conn, "LOGIN.field1") == _sealed()


def test_keypair_field_refs_and_mutations_are_rejected(vault):
    _create(vault, name="SIGNING_KEY", kind="keypair", signer_kind="local")

    with vault.begin() as conn:
        with pytest.raises(vs.FieldsNotSupportedError):
            vs.get_secret_record_meta(conn, "SIGNING_KEY.value")
        with pytest.raises(vs.FieldsNotSupportedError):
            vs.update_secret_field(conn, "SIGNING_KEY", "value", _sealed("replacement"), record="SIGNING_KEY")
        with pytest.raises(vs.FieldsNotSupportedError):
            vs.remove_secret_field(conn, "SIGNING_KEY", "value")


def test_selector_records_preserve_secret_case_and_reject_empty_alias(vault):
    _create(
        vault,
        name="lowerSecret",
        protection="protected",
        tags=["deploy"],
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )

    with vault.begin() as conn:
        expanded = vs.expand_value_delivery_selector(conn, env=["lowerSecret.username"])
        assert expanded["secrets"] == [
            {
                "name": "lowerSecret",
                "field": "username",
                "record": "lowerSecret.username",
                "env": "lowerSecret_USERNAME",
                "kind": "static",
                "protection": "protected",
            }
        ]
        tagged = vs.expand_value_delivery_selector(conn, tags=["deploy"])
        assert [item["env"] for item in tagged["secrets"]] == [
            "lowerSecret",
            "lowerSecret_USERNAME",
        ]
        with pytest.raises(vs.InvalidRequestError):
            vs.expand_value_delivery_selector(conn, env=["=lowerSecret"])
        default_ref = vs.expand_value_delivery_selector(conn, env=["lowerSecret.value"])
        assert default_ref["secrets"][0]["record"] == "lowerSecret"
        assert default_ref["secrets"][0]["env"] == "lowerSecret_VALUE"
        for conflict in [
            ["lowerSecret", "lowerSecret.value"],
            ["SAME=lowerSecret", "SAME=lowerSecret.username"],
        ]:
            with pytest.raises(vs.InvalidRequestError):
                vs.expand_value_delivery_selector(conn, env=conflict)


def test_access_request_deduplicates_secret_members_and_exposes_field_metadata(vault):
    _create(
        vault,
        name="PROTECTED_LOGIN",
        protection="protected",
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )

    with vault.begin() as conn:
        request = vs.create_access_request(
            conn,
            source_selector={"env": ["PROTECTED_LOGIN", "PROTECTED_LOGIN.username"]},
        )
        option = request["card"]["grant_options"][0]
        assert option["member_snapshot"] == ["PROTECTED_LOGIN"]
        assert option["member_fields"] == [
            {
                "name": "PROTECTED_LOGIN",
                "default_field": "password",
                "fields": ["password", "username"],
            }
        ]
        assert [item["name"] for item in option["unlock_material"]] == [
            "PROTECTED_LOGIN",
            "PROTECTED_LOGIN.username",
        ]
        assert request["card"]["secret_unlock_material"] == {
            "name": "PROTECTED_LOGIN",
            "kind": "static",
            "envelope": {
                "ciphertext": "ct-protected_login",
                "nonce": "n-protected_login",
                "wrap_meta": "wm-protected_login",
            },
        }
        agent_payload = vs.get_request(conn, request["id"], audience=vs.REQUEST_AUDIENCE_AGENT)
        persisted = json.loads(conn.execute(
            select(vault_requests.c.delivery).where(vault_requests.c.id == request["id"])
        ).scalar_one())
        for payload in [agent_payload, persisted]:
            encoded = json.dumps(payload)
            assert "ciphertext" not in encoded
            assert "unlock_material" not in encoded
            assert payload["card"]["grant_options"][0]["member_fields"] == option["member_fields"]
        # Legacy binding issues one approval per record. Retain the default
        # approval while the next field receives its own release context.
        for name in ["PROTECTED_LOGIN", "PROTECTED_LOGIN.username"]:
            recorded = vs.record_request_agent_binding_approvals(
                conn, request["id"],
                {"grant_id": option["grant_id"], "items": [{"name": name}]},
            )
        assert [
            item["items"][0]["name"]
            for item in recorded["delivery"]["agent_binding_approvals"]
        ] == ["PROTECTED_LOGIN", "PROTECTED_LOGIN.username"]
        grant = _grant_from_request(conn, request)
        resolved = vs.resolve_secret_access(conn, "PROTECTED_LOGIN.username")
        assert resolved["grant"]["member_snapshot"] == ["PROTECTED_LOGIN"]
        assert resolved["envelope"] == _sealed("username")
        scopes = []
        vs.update_secret_field(conn, "PROTECTED_LOGIN", "username", _sealed("new"), record="PROTECTED_LOGIN.username", release_scopes=scopes)
        assert vs.get_grant_created_by_request(conn, grant["request_id"])["status"] == "expired"
        assert scopes == [{"grant_id": grant["id"]}]
        next_grant = _grant_from_request(conn, vs.create_access_request(conn, "PROTECTED_LOGIN"))
        pending = vs.create_access_request(conn, "PROTECTED_LOGIN")
        scopes.clear()
        vs.remove_secret_field(conn, "PROTECTED_LOGIN", "username", release_scopes=scopes)
        assert vs.get_request(conn, pending["id"])["status"] == "expired"
        assert vs.get_grant_created_by_request(conn, next_grant["request_id"])["status"] == "expired"
        assert scopes == [{"grant_id": next_grant["id"]}]


def test_standard_ask_card_persists_fields_and_field_access_uses_secret_members(vault):
    _create(
        vault, name="LOGIN", policy={"always_ask": True},
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )
    with vault.begin() as conn:
        request = vs.create_access_request(conn, "LOGIN.username", audience=vs.REQUEST_AUDIENCE_AGENT)
        option = request["card"]["grant_options"][0]
        assert request["secret_name"] == "LOGIN"
        assert option["member_snapshot"] == ["LOGIN"]
        assert option["member_fields"] == [
            {"name": "LOGIN", "default_field": "value", "fields": ["value", "username"]}
        ]
        assert "unlock_material" not in json.dumps(request)


def test_delivery_use_count_is_per_secret_and_audit_lists_fields(vault):
    _create(vault, name="LOGIN", fields=[{"name": "username", "sealed": _sealed("username")}])

    with vault.begin() as conn:
        identities = vs.capture_delivery_identities(conn, ["LOGIN", "LOGIN.username"])
        vs.record_deliveries(conn, identities, mode="run")
        meta = vs.get_secret_meta(conn, "LOGIN")
        events = conn.execute(
            select(vs.vault_audit.c.delivery).where(
                vs.vault_audit.c.secret_name == "LOGIN",
                vs.vault_audit.c.event == "delivered",
            )
        ).scalars().all()

    assert meta["use_count"] == 1
    assert len(events) == 1
    assert json.loads(events[0])["fields"] == ["value", "username"]


def test_selector_expansion_unions_env_tags_and_skills(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])
    _create(vault, name="B_KEY", protection="standard", tags=["deploy", "skill:release"])
    _create(vault, name="C_KEY", protection="standard", tags=["other"])

    with vault.begin() as conn:
        expanded = vs.expand_value_delivery_selector(
            conn,
            env=["LOCAL_C=C_KEY"],
            tags=["deploy"],
            skills=["release"],
        )

    assert expanded["source_selector"] == {"env": ["LOCAL_C=C_KEY"], "tags": ["deploy", "skill:release"]}
    assert expanded["secrets"] == [
        {
            "name": "C_KEY",
            "field": "value",
            "record": "C_KEY",
            "env": "LOCAL_C",
            "kind": "static",
            "protection": "standard",
        },
        {
            "name": "A_KEY",
            "field": "value",
            "record": "A_KEY",
            "env": "A_KEY",
            "kind": "static",
            "protection": "protected",
        },
        {
            "name": "B_KEY",
            "field": "value",
            "record": "B_KEY",
            "env": "B_KEY",
            "kind": "static",
            "protection": "standard",
        },
    ]


def test_selector_rejects_env_conflicts_and_keypairs(vault):
    _create(vault, name="A_KEY", tags=["deploy"])
    _create(vault, name="SIGNING_KEY", kind="keypair", signer_kind="local", tags=["deploy"])

    with vault.begin() as conn:
        with pytest.raises(vs.InvalidRequestError):
            vs.expand_value_delivery_selector(conn, env=["ONE=A_KEY", "TWO=A_KEY"])
        with pytest.raises(vs.KeypairNotValueDeliverableError):
            vs.expand_value_delivery_selector(conn, tags=["deploy"])


def test_selector_allows_shell_env_alias_for_secret(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")

    with vault.begin() as conn:
        expanded = vs.expand_value_delivery_selector(conn, env=["api_key=PROTECTED_KEY"])

    assert expanded["source_selector"] == {"env": ["api_key=PROTECTED_KEY"]}
    assert expanded["secrets"] == [
        {
            "name": "PROTECTED_KEY",
            "field": "value",
            "record": "PROTECTED_KEY",
            "env": "api_key",
            "kind": "static",
            "protection": "protected",
        }
    ]


def test_access_request_for_tag_selector_freezes_protected_subset_only(vault):
    _create(vault, name="PROTECTED_A", protection="protected", tags=["deploy"])
    _create(vault, name="STANDARD_B", protection="standard", tags=["deploy"])

    with vault.begin() as conn:
        expanded = vs.expand_value_delivery_selector(conn, tags=["deploy"])
        request = vs.create_access_request(
            conn,
            source_selector=expanded["source_selector"],
            purpose="run",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1", "command": "python deploy.py"},
        )
        card = request["card"]

    assert card["source_selector"] == {"tags": ["deploy"]}
    assert card["protected_secret_names"] == ["PROTECTED_A"]
    assert card["grant_options"][0]["grant_id"].startswith("vgr_")
    assert card["grant_options"][0]["member_snapshot"] == ["PROTECTED_A"]
    assert "STANDARD_B" not in json.dumps(card["grant_options"])


def test_tag_edits_do_not_mutate_existing_grant_snapshot(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, "A_KEY", requester={"session_id": "ses_1"}, delivery={"session_id": "ses_1"})
        grant = _grant_from_request(conn, req)
        vs.create_secret(conn, name="B_KEY", protection="protected", sealed=_sealed("b"), tags=["deploy"])
        vs.update_secret_tags(conn, "B_KEY", ["deploy", "new-tag"])
        row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one()

    assert json.loads(row["member_snapshot"]) == ["A_KEY"]
    assert grant["member_snapshot"] == ["A_KEY"]


def test_create_grant_stores_final_fields_and_readiness_by_grant_id(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            purpose="inject",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant = _grant_from_request(conn, req)
        ready = vs.mark_grant_agent_ready(conn, grant["id"], ttl_seconds=300)
        release_refs = vs.agent_release_scopes_after_rows(
            conn,
            [dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one())],
        )

    assert ready["source_selector"] == {"tags": ["deploy"]}
    assert ready["id"] == req["card"]["grant_options"][0]["grant_id"]
    assert ready["purpose"] == "inject"
    assert ready["request_id"] == req["id"]
    assert ready["delivery_ready"] is True
    assert release_refs == [{"grant_id": grant["id"]}]


def test_create_grant_rejects_removed_ttl_options(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, source_selector={"tags": ["deploy"]})

        with pytest.raises(vs.InvalidGrantError):
            _grant_from_request(conn, req, ttl_seconds=86_400)


def test_create_grant_one_time_uses_short_one_shot_window(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, source_selector={"tags": ["deploy"]}, requester={"session_id": "ses_1"})
        option = req["card"]["grant_options"][0]
        grant = vs.create_grant(
            conn,
            member_names=option["member_snapshot"],
            source_selector=option["source_selector"],
            purpose=option["purpose"],
            request_id=req["id"],
            grant_duration="one-time",
        )
        settings = vs.get_vault_settings(conn)

    created = datetime.fromisoformat(grant["created_at"])
    expires = datetime.fromisoformat(grant["expires_at"])
    assert expires - created == timedelta(seconds=60)
    assert grant["one_shot"] is True
    assert grant["session_id"] == "ses_1"
    assert settings["last_grant_ttl"] == "one-time"


def test_save_vault_settings_first_write_is_atomic_under_concurrency(vault):
    barrier = threading.Barrier(6)

    def save_settings(index: int) -> dict:
        with vault.begin() as conn:
            barrier.wait(timeout=5)
            return vs.save_vault_settings(conn, {"last_grant_ttl": 300 if index % 2 else 900})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(save_settings, range(6)))

    with vault.connect() as conn:
        rows = list(conn.execute(select(state_meta).where(state_meta.c.key == vs.VAULT_SETTINGS_META_KEY)).mappings())

    assert len(rows) == 1
    assert {result["last_grant_ttl"] for result in results}.issubset({300, 900})


def test_protected_vault_establishment_is_atomic_under_concurrency(vault, monkeypatch):
    init_lock_key = vs.PROTECTED_VAULT_ESTABLISHMENT_LOCK_META_KEY
    monkeypatch.setattr(vs, "register_webauthn_factor", lambda *_args, **_kwargs: None)

    lock_barrier = threading.Barrier(2)
    pre_guard_barrier = threading.Barrier(2)
    post_guard_barrier = threading.Barrier(2)
    lock_seen = threading.Event()
    database_access_before_lock = threading.Event()
    thread_state = threading.local()

    def is_init_lock(statement: str, parameters) -> bool:
        return statement.lstrip().upper().startswith("INSERT INTO STATE_META") and init_lock_key in parameters

    def is_protected_guard(statement: str) -> bool:
        normalized = " ".join(statement.upper().split())
        return "FROM VAULT_SECRETS" in normalized and "WHERE VAULT_SECRETS.PROTECTION =" in normalized

    def synchronize_before_guard(_conn, _cursor, statement, parameters, _context, _executemany):
        if is_init_lock(statement, parameters):
            thread_state.init_lock_seen = True
            lock_seen.set()
            lock_barrier.wait(timeout=5)
        else:
            if not getattr(thread_state, "init_lock_seen", False):
                database_access_before_lock.set()
            if not lock_seen.is_set() and is_protected_guard(statement):
                # On the unfixed implementation, force both deferred transactions
                # through the empty-vault read before either reaches its first write.
                pre_guard_barrier.wait(timeout=5)

    def synchronize_after_guard(_conn, _cursor, statement, _parameters, _context, _executemany):
        if not lock_seen.is_set() and is_protected_guard(statement):
            post_guard_barrier.wait(timeout=5)

    event.listen(vault, "before_cursor_execute", synchronize_before_guard)
    event.listen(vault, "after_cursor_execute", synchronize_after_guard)

    def establish(index: int):
        try:
            with vault.begin() as conn:
                return vs.create_secret(
                    conn,
                    name="CONCURRENT_PROTECTED",
                    sealed=_sealed(f"concurrent-{index}"),
                    protection="protected",
                    establishing_vmk=True,
                    authz_factor_registration={},
                )
        except vs.VaultServiceError as exc:
            return exc

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(establish, range(2)))
    finally:
        event.remove(vault, "before_cursor_execute", synchronize_before_guard)
        event.remove(vault, "after_cursor_execute", synchronize_after_guard)

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, vs.VaultServiceError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], vs.VaultAlreadyInitializedError)
    assert not database_access_before_lock.is_set()

    with vault.connect() as conn:
        protected_names = list(
            conn.execute(select(vault_secrets.c.name).where(vault_secrets.c.protection == "protected")).scalars()
        )
        lock_row = conn.execute(select(state_meta).where(state_meta.c.key == init_lock_key)).mappings().one()

    assert protected_names == [successes[0]["name"]]
    assert lock_row["value_json"] == "{}"


def test_sibling_approval_requires_matching_purpose(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        run_req = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            purpose="run",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        fetch_req = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            purpose="fetch",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        _grant_from_request(conn, run_req)
        fetch_row = conn.execute(select(vault_requests).where(vault_requests.c.id == fetch_req["id"])).mappings().one()

    assert fetch_row["status"] == "pending"


def test_secret_delete_rotate_and_classification_changes_expire_covering_grants(vault):
    _create(vault, name="A_KEY", protection="protected")
    _create(vault, name="B_KEY", protection="protected")
    _create(vault, name="C_KEY", protection="protected")

    with vault.begin() as conn:
        grant_a = _grant_from_request(conn, vs.create_access_request(conn, "A_KEY"))
        grant_b = _grant_from_request(conn, vs.create_access_request(conn, "B_KEY"))
        grant_c = _grant_from_request(conn, vs.create_access_request(conn, "C_KEY"))
        vs.rotate_secret(conn, "A_KEY", _sealed("rotated"))
        vs.delete_secret(conn, "B_KEY")
        vs.update_secret_classification(conn, "C_KEY", protection="standard")
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(select(vault_grants).where(vault_grants.c.id.in_([grant_a["id"], grant_b["id"], grant_c["id"]]))).mappings()
        }

    assert statuses == {grant_a["id"]: "expired", grant_b["id"]: "expired", grant_c["id"]: "expired"}


def test_protected_delete_does_not_require_authz_at_chokepoint(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")

    with vault.begin() as conn:
        vs.delete_secret(conn, "PROTECTED_KEY")
        with pytest.raises(vs.SecretNotFoundError):
            vs.get_secret_meta(conn, "PROTECTED_KEY")


def test_standard_delete_does_not_require_authz(vault):
    _create(vault, name="STANDARD_KEY", protection="standard")

    with vault.begin() as conn:
        vs.delete_secret(conn, "STANDARD_KEY")
        with pytest.raises(vs.SecretNotFoundError):
            vs.get_secret_meta(conn, "STANDARD_KEY")


def test_webauthn_factor_registration_stores_public_key(vault):
    credential = WebAuthnTestCredential()

    with vault.begin() as conn:
        _credential, factor = _establish_protected_secret(conn, "PROTECTED_KEY", credential)
        row = conn.execute(select(vault_auth_factors).where(vault_auth_factors.c.id == factor["id"])).mappings().one()

    assert factor["credential_id"] == credential.credential_id_b64
    assert row["public_key"]
    assert row["alg"] == -7
    assert json.loads(row["transports"]) == ["internal"]


def test_sandbox_cross_origin_webauthn_registration_and_assertion_accept_allowed_parent(vault):
    credential = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin="https://sandbox.avibe.bot")

    with vault.begin() as conn:
        options = vs.create_webauthn_registration_options(conn, rp_id=credential.rp_id, origin=credential.origin)
        registration = credential.registration_payload(
            challenge_id=options["challenge_id"],
            challenge_b64=options["webauthn"]["challenge"],
            cross_origin=True,
            top_origin="http://localhost:5173",
        )
        factor = vs.register_webauthn_factor(conn, registration, rp_id=credential.rp_id, origin=credential.origin, establishment=True)[
            "factor"
        ]

    with vault.connect() as conn:
        factor_row = conn.execute(select(vault_auth_factors).where(vault_auth_factors.c.id == factor["id"])).mappings().one()
    public_key = factor_row["public_key"]
    challenge = b"sandbox-cross-origin-challenge"
    challenge_b64 = vault_webauthn.b64encode(challenge)
    allowed_assertion = credential.assertion_authz(
        challenge_id="vop_cross_origin",
        factor_id=factor["id"],
        challenge_b64=challenge_b64,
        sign_count=2,
        cross_origin=True,
        top_origin="https://team.avibe.bot",
    )["assertion"]
    assert (
        vault_webauthn.verify_assertion(
            allowed_assertion,
            credential_id=credential.credential_id_b64,
            public_key=public_key,
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=1,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        ).sign_count
        == 2
    )

    safari_assertion = credential.assertion_authz(
        challenge_id="vop_cross_origin",
        factor_id=factor["id"],
        challenge_b64=challenge_b64,
        sign_count=3,
        cross_origin=True,
    )["assertion"]
    assert (
        vault_webauthn.verify_assertion(
            safari_assertion,
            credential_id=credential.credential_id_b64,
            public_key=public_key,
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=2,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        ).sign_count
        == 3
    )


def test_sandbox_cross_origin_webauthn_rejects_wrong_top_origin():
    credential = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin="https://sandbox.avibe.bot")
    challenge = b"sandbox-cross-origin-challenge"
    challenge_b64 = vault_webauthn.b64encode(challenge)

    assertion = credential.assertion_authz(
        challenge_id="vop_cross_origin",
        factor_id="vaf_cross_origin",
        challenge_b64=challenge_b64,
        cross_origin=True,
        top_origin="https://evil.example",
    )["assertion"]

    with pytest.raises(vault_webauthn.WebAuthnVerificationError, match="top origin"):
        vault_webauthn.verify_assertion(
            assertion,
            credential_id=credential.credential_id_b64,
            public_key=vault_webauthn.b64encode(credential.public_key_cose()),
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=0,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        )


def test_webauthn_counter_regression_rejects_zero_after_nonzero_counter():
    credential = WebAuthnTestCredential()
    challenge = b"counter-challenge"
    challenge_b64 = vault_webauthn.b64encode(challenge)
    public_key = vault_webauthn.b64encode(credential.public_key_cose())

    zero_assertion = credential.assertion_authz(
        challenge_id="vop_counter",
        factor_id="vaf_counter",
        challenge_b64=challenge_b64,
        sign_count=0,
    )["assertion"]
    assert (
        vault_webauthn.verify_assertion(
            zero_assertion,
            credential_id=credential.credential_id_b64,
            public_key=public_key,
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=0,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        ).sign_count
        == 0
    )
    with pytest.raises(vault_webauthn.WebAuthnVerificationError):
        vault_webauthn.verify_assertion(
            zero_assertion,
            credential_id=credential.credential_id_b64,
            public_key=public_key,
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=1,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        )

    increasing_assertion = credential.assertion_authz(
        challenge_id="vop_counter",
        factor_id="vaf_counter",
        challenge_b64=challenge_b64,
        sign_count=2,
    )["assertion"]
    assert (
        vault_webauthn.verify_assertion(
            increasing_assertion,
            credential_id=credential.credential_id_b64,
            public_key=public_key,
            alg=vault_webauthn.ALG_ES256,
            stored_sign_count=1,
            expected_challenge_hash=vault_webauthn.challenge_hash(challenge),
            expected_origin=credential.origin,
            rp_id=credential.rp_id,
        ).sign_count
        == 2
    )


def test_webauthn_registration_invalid_cose_point_fails_closed(vault):
    credential = WebAuthnTestCredential()

    with vault.begin() as conn:
        options = vs.create_webauthn_registration_options(
            conn,
            rp_id=credential.rp_id,
            origin=credential.origin,
        )
        payload = credential.registration_payload(
            challenge_id=options["challenge_id"],
            challenge_b64=options["webauthn"]["challenge"],
            public_key_cose=WebAuthnTestCredential.es256_public_key_cose(1, 1),
        )

        with pytest.raises(vs.InvalidProtectedAuthzError) as exc:
            vs.register_webauthn_factor(conn, payload, rp_id=credential.rp_id, origin=credential.origin, establishment=True)

    assert isinstance(exc.value.__cause__, vault_webauthn.WebAuthnVerificationError)


def test_standard_secret_does_not_create_access_request_unless_always_ask(vault):
    _create(vault, name="STANDARD_KEY", protection="standard")
    _create(vault, name="ASK_KEY", protection="standard", policy={"always_ask": True})

    with vault.begin() as conn:
        assert vs.resolve_secret_access(conn, "STANDARD_KEY")["status"] == "standard"
        with pytest.raises(vs.NotGrantableError):
            vs.create_access_request(conn, "STANDARD_KEY")
        req = vs.create_access_request(conn, "ASK_KEY", requester={"session_id": "ses_1"}, delivery={"session_id": "ses_1"})
        grant = _grant_from_request(conn, req)
        reserved = vs.find_active_grant_for_secret(conn, "ASK_KEY", session_id="ses_1", reserve_one_shot=True)

    assert req["card"]["one_shot"] is True
    assert grant["one_shot"] is True
    assert reserved["status"] == "reserved"


def test_multi_member_one_shot_grant_is_consumed_once(vault):
    _create(vault, name="ASK_KEY", protection="standard", tags=["deploy"], policy={"always_ask": True})
    _create(vault, name="PROTECTED_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, source_selector={"tags": ["deploy"]}, requester={"session_id": "ses_1"})
        grant = _grant_from_request(conn, req)
        reserved = vs.find_active_grant_for_secret(
            conn,
            "ASK_KEY",
            session_id="ses_1",
            reserve_one_shot=True,
        )
        releases = vs.consume_one_shot_grant(conn, grant["id"])
        row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one()

    assert req["card"]["one_shot"] is True
    assert grant["one_shot"] is True
    assert reserved["status"] == "reserved"
    assert row["status"] == "expired"
    assert releases == [{"grant_id": grant["id"]}]


def test_create_grant_rejects_expired_binding_without_claiming_request(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    with vault.begin() as conn:
        req = vs.create_access_request(conn, "PROTECTED_KEY", requester={"session_id": "ses_1"})

        with pytest.raises(vs.InvalidGrantError, match="grant binding has expired"):
            _grant_from_request(conn, req, expires_at=expired_at)

        row = conn.execute(select(vault_requests).where(vault_requests.c.id == req["id"])).mappings().one()
        grants = conn.execute(select(vault_grants).where(vault_grants.c.request_id == req["id"])).mappings().all()

    assert row["status"] == "pending"
    assert grants == []


def test_selector_request_expires_when_member_rotates(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])
    _create(vault, name="B_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, source_selector={"tags": ["deploy"]})
        assert req["secret_name"] is None
        expired = vs.rotate_secret(conn, "A_KEY", _sealed("rotated"))
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == req["id"])).mappings().one()

    assert expired["name"] == "A_KEY"
    assert row["status"] == "expired"


def test_selector_request_restores_after_failed_grant(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])
    _create(vault, name="B_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(
            conn,
            source_selector={"tags": ["deploy"]},
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant = _grant_from_request(conn, req, cache_ready=False)
        restored = vs.restore_access_request_after_failed_grant(
            conn,
            request_id=req["id"],
            member_names=grant["member_snapshot"],
            session_id=grant["session_id"],
        )
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == req["id"])).mappings().one()

    assert restored == 1
    assert row["status"] == "pending"
    assert row["decided_at"] is None


def test_request_payload_hydrates_unlock_material_only_for_ui(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")

    with vault.begin() as conn:
        req = vs.create_access_request(
            conn,
            "PROTECTED_KEY",
            requester={"source": "agent-cli", "session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        agent_payload = vs.get_request(conn, req["id"], audience=vs.REQUEST_AUDIENCE_AGENT)
        ui_payload = vs.get_request(conn, req["id"], audience=vs.REQUEST_AUDIENCE_UI)

    assert "unlock_material" not in json.dumps(agent_payload)
    assert ui_payload["card"]["grant_options"][0]["unlock_material"][0]["name"] == "PROTECTED_KEY"


def test_expired_request_cannot_create_grant(vault):
    _create(vault, name="A_KEY", protection="protected")
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    with vault.begin() as conn:
        req = vs.create_access_request(conn, "A_KEY", expires_at=expired_at)
        with pytest.raises(vs.InvalidRequestError):
            _grant_from_request(conn, req)


def test_get_grant_created_by_request_uses_request_id(vault):
    _create(vault, name="A_KEY", protection="protected")

    with vault.begin() as conn:
        req = vs.create_access_request(conn, "A_KEY")
        grant = _grant_from_request(conn, req)
        fetched = vs.get_grant_created_by_request(conn, req["id"])

    assert fetched["id"] == grant["id"]
