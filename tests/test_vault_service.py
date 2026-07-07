"""Unit tests for the Vaults service final grant-id/tag model."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from storage import vault_service as vs, vault_webauthn
from storage.db import create_sqlite_engine
from storage.models import metadata, vault_auth_factors, vault_grants, vault_operation_challenges, vault_requests, vault_secrets
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


def _protected_delete_authz(conn, name: str) -> vs.ProtectedOperationAuthz:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().one()
    return vs.ProtectedOperationAuthz(
        operation="delete_secret",
        secret_name=row["name"],
        secret_id=row["id"],
        secret_updated_at=row["updated_at"],
        challenge_id="test-challenge",
        factor_id="test-factor",
    )


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


def _grant_from_request(conn, request: dict, *, cache_ready: bool = True, ttl_seconds: int | None = None) -> dict:
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        request_id=request["id"],
        ttl_seconds=ttl_seconds,
        cache_ready=cache_ready,
    )


def test_secret_metadata_is_global_name_plus_tags(vault):
    meta = _create(vault, name="OPENAI_API_KEY", description="key", tags=["prod", "skill:deploy"])

    assert meta["name"] == "OPENAI_API_KEY"
    assert meta["tags"] == ["prod", "skill:deploy"]
    assert "group" not in meta
    assert "preview" not in meta

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
        {"name": "C_KEY", "env": "LOCAL_C", "kind": "static", "protection": "standard"},
        {"name": "A_KEY", "env": "A_KEY", "kind": "static", "protection": "protected"},
        {"name": "B_KEY", "env": "B_KEY", "kind": "static", "protection": "standard"},
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
        {"name": "PROTECTED_KEY", "env": "api_key", "kind": "static", "protection": "protected"}
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


def test_create_grant_caps_ttl_to_approval_options(vault):
    _create(vault, name="A_KEY", protection="protected", tags=["deploy"])

    with vault.begin() as conn:
        req = vs.create_access_request(conn, source_selector={"tags": ["deploy"]})
        grant = _grant_from_request(conn, req, ttl_seconds=86_400)

    created = datetime.fromisoformat(grant["created_at"])
    expires = datetime.fromisoformat(grant["expires_at"])
    assert expires - created == timedelta(seconds=3600)


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
        vs.delete_secret(conn, "B_KEY", protected_authz=_protected_delete_authz(conn, "B_KEY"))
        vs.update_secret_classification(conn, "C_KEY", protection="standard")
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(select(vault_grants).where(vault_grants.c.id.in_([grant_a["id"], grant_b["id"], grant_c["id"]]))).mappings()
        }

    assert statuses == {grant_a["id"]: "expired", grant_b["id"]: "expired", grant_c["id"]: "expired"}


def test_protected_delete_requires_verified_authz_at_chokepoint(vault):
    _create(vault, name="PROTECTED_KEY", protection="protected")

    with vault.begin() as conn:
        with pytest.raises(vs.ProtectedAuthRequiredError):
            vs.delete_secret(conn, "PROTECTED_KEY")
        assert vs.get_secret_meta(conn, "PROTECTED_KEY")["protection"] == "protected"


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


def test_delete_challenge_expiry_and_replay_are_rejected(vault):
    credential = WebAuthnTestCredential()

    with vault.begin() as conn:
        _credential, registered = _establish_protected_secret(conn, "PROTECTED_KEY", credential)
        expired = vs.create_delete_challenge(conn, "PROTECTED_KEY", rp_id=credential.rp_id, origin=credential.origin)
        conn.execute(
            vault_operation_challenges.update()
            .where(vault_operation_challenges.c.id == expired["challenge_id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )
        with pytest.raises(vs.InvalidProtectedAuthzError):
            vs.verify_delete_secret_authz(
                conn,
                "PROTECTED_KEY",
                credential.assertion_authz(
                    challenge_id=expired["challenge_id"],
                    factor_id=registered["id"],
                    challenge_b64=expired["webauthn"]["challenge"],
                ),
            )

        fresh = vs.create_delete_challenge(conn, "PROTECTED_KEY", rp_id=credential.rp_id, origin=credential.origin)
        authz = credential.assertion_authz(
            challenge_id=fresh["challenge_id"],
            factor_id=registered["id"],
            challenge_b64=fresh["webauthn"]["challenge"],
            sign_count=3,
        )
        assert vs.verify_delete_secret_authz(conn, "PROTECTED_KEY", authz).challenge_id == fresh["challenge_id"]
        with pytest.raises(vs.InvalidProtectedAuthzError):
            vs.verify_delete_secret_authz(conn, "PROTECTED_KEY", authz)


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
