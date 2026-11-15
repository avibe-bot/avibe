"""Protected ordinary BTC saved identity and atomic storage-owner regressions.

The capable registry entry is an explicit hermetic fixture, NOT a shipped pin or
browser support claim. All cryptographic keys below are public NEVER-FUND tests.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from sqlalchemy import func, select

from storage import vault_service
from storage.models import vault_audit, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from tests.test_vault_bitcoin_ecdsa_context import PUBLIC, _case, _signature
from vibe import api, vault_sandbox_release as release


@pytest.fixture
def capable_cohort(monkeypatch):
    current = release.current_support()
    assert current is not None and current.ordinary_input_types == ("p2tr",)
    monkeypatch.setattr(release, "CAPABILITIES_BY_PIN_ID", {current.pin_id: {
        "ordinary_input_types": ("p2pkh", "p2wpkh", "p2tr"),
        "hd_input_types": (), "ecdsa_return_types": ("p2pkh", "p2wpkh", "p2tr"),
    }})
    monkeypatch.setattr(api, "_notify_vault_request_created", lambda *a, **k: None)
    monkeypatch.setattr(api, "_publish_vaults_updated", lambda *a, **k: None)
    return release.current_support()


def _create():
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(
            conn, name="PUBLIC_TEST_BTC", kind="keypair", protection="protected", signer_kind="local",
            sealed=Sealed(ciphertext="test-only-opaque", nonce="test-only-nonce", wrap_meta=' {"fixture":"é","raw": true} '),
            public_meta={"signing_public_key": {"curve": "secp256k1", "public_key": PUBLIC.hex()}},
        )


def _request(address_type="p2pkh", *, attach=True, delivery_alias=False):
    context = _case(address_type)
    if attach:
        payload = {"name": "PUBLIC_TEST_BTC", "digest": context["digest"], "scheme": "ecdsa-secp256k1-der",
                   "requester": {"source": "cli", "session_id": "original-test-session"}}
        if delivery_alias:
            payload["delivery"] = {"signingContext": context, "command": "public fixture"}
        else:
            payload["signing_context"] = context
        request = api.request_vault_sign(payload)["request"]
    else:
        with api._vault_engine().begin() as conn:
            request = vault_service.create_sign_request(
                conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der",
                signing_context=context, requester={"source": "cli", "session_id": "original-test-session"},
            )
    return request, context


def _complete(conn, request, context, **kwargs):
    return vault_service.complete_sign_request(
        conn, request["id"], name="PUBLIC_TEST_BTC", scheme="ecdsa-secp256k1-der",
        digest=context["digest"], signing_context=context,
        signature=kwargs.pop("signature", {"signature": _signature(context), "recovery_id": None}),
        requester={"source": "untrusted-completer", "session_id": "do-not-record-this"},
        **kwargs,
    )


def _state(conn, request_id):
    request = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    secret = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "PUBLIC_TEST_BTC")).mappings().one()
    signed = conn.execute(select(func.count()).select_from(vault_audit).where(vault_audit.c.event == "signed")).scalar_one()
    return request["status"], secret["use_count"], signed


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("delivery_alias", [False, True])
def test_capture_before_attachment_exact_release_and_original_requester_atomic_use(capable_cohort, address_type, delivery_alias):
    _create()
    request, context = _request(address_type, delivery_alias=delivery_alias)
    delivery = request["delivery"]
    with api._vault_engine().connect() as conn:
        secret = dict(conn.execute(select(vault_secrets).where(vault_secrets.c.name == "PUBLIC_TEST_BTC")).mappings().one())
    fingerprint = hashlib.sha256(json.dumps(
        [secret["ciphertext"], secret["nonce"], secret["wrap_meta"]], separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    assert delivery["signing_key"] == {"secret_id": secret["id"], "public_key": PUBLIC.hex(), "envelope_sha256": fingerprint}
    assert delivery["bitcoin_identity"] == {"kind": "keypair", "protection": "protected", "signer_kind": "local"}
    assert delivery["bitcoin_cohort"] == capable_cohort.cohort
    assert "derivation" not in delivery and "operation_context" not in delivery["card"]
    assert "operation_context" not in request["card"]
    operation = delivery["operation_context"]
    assert operation["release"] == {
        "name": "PUBLIC_TEST_BTC", "scheme": "ecdsa-secp256k1-der", "digest": context["digest"],
        "publicKey": PUBLIC.hex(), "signingContext": context, "signingKey": delivery["signing_key"],
        "sandboxCohort": {"buildHash": capable_cohort.build_hash, "manifestHash": capable_cohort.manifest_hash},
    }
    with api._vault_engine().begin() as conn:
        completed = _complete(conn, request, context, browser_signed=False)
        assert completed["status"] == "approved"
        assert set(completed["delivery"]["signature"]) == {"signature"}
        assert _state(conn, request["id"]) == ("approved", 1, 1)
        audit = conn.execute(select(vault_audit).where(vault_audit.c.event == "signed")).mappings().one()
        assert json.loads(audit["requester"])["session_id"] == "original-test-session"
        assert "do-not-record-this" not in audit["requester"]
    # Completed result reads remain frozen even after the current key/cohort changes.
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(nonce="changed-after-completion"))
    saved = api.get_vault_request(request["id"])
    assert saved["request"]["delivery"]["signing_key"] == delivery["signing_key"]
    assert set(saved["result"]) == {"type", "signature"}
    assert saved["result"]["signature"] == completed["delivery"]["signature"]


@pytest.mark.parametrize("stage", ["before-attachment", "before-completion"])
def test_same_name_replacement_even_identical_point_and_envelope_never_adopts_capture(capable_cohort, stage):
    _create()
    request, context = _request(attach=stage != "before-attachment")
    initial_key = copy.deepcopy(request["delivery"]["signing_key"])
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(id="vlt_replacement_same_public_facts"))
    if stage == "before-attachment":
        with pytest.raises(api.VaultApiError, match="identity changed"):
            api._attach_signed_sign_operation_context(request["id"])
    for audience in (vault_service.REQUEST_AUDIENCE_UI, vault_service.REQUEST_AUDIENCE_AGENT):
        with pytest.raises(api.VaultApiError, match="identity changed"):
            api.get_vault_request(request["id"], audience=audience)
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError, match="identity changed"):
            _complete(conn, request, context)
        assert _state(conn, request["id"]) == ("pending", 0, 0)
        stored = json.loads(conn.execute(select(vault_requests.c.delivery).where(vault_requests.c.id == request["id"])).scalar_one())
        assert stored["signing_key"] == initial_key


@pytest.mark.parametrize("mutation", [
    {"signer_kind": ""}, {"signer_kind": "external"}, {"kind": "static"}, {"protection": "standard"},
])
def test_saved_classification_changes_refuse_even_with_same_id_point_and_envelope(capable_cohort, mutation):
    # Saved-key equality alone misses an unknown falsy signer kind being treated
    # as the legacy NULL/local classification. Exercise the storage use owner.
    _create()
    request, context = _request()
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(**mutation))
        with pytest.raises(vault_service.InvalidRequestError):
            _complete(conn, request, context)
        assert _state(conn, request["id"]) == ("pending", 0, 0)


def test_saved_local_classification_keeps_legacy_null_equivalence(capable_cohort):
    _create()
    request, context = _request()
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(signer_kind=None))
        assert _complete(conn, request, context)["status"] == "approved"
        assert _state(conn, request["id"]) == ("approved", 1, 1)


def test_create_audit_failure_caught_by_outer_transaction_leaves_no_partial_request(capable_cohort, monkeypatch):
    _create()
    audit = vault_service.audit

    def fail(conn, event, **kwargs):
        if event == "sign_requested":
            raise RuntimeError("injected creation audit failure")
        return audit(conn, event, **kwargs)

    monkeypatch.setattr(vault_service, "audit", fail)
    context = _case()
    with api._vault_engine().begin() as conn:
        with pytest.raises(RuntimeError, match="creation audit"):
            vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", digest=context["digest"], scheme="ecdsa-secp256k1-der", signing_context=context)
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0
    with api._vault_engine().connect() as conn:
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0


@pytest.mark.parametrize("failure", ["bad-der", "audit", "use", "outer-rollback"])
def test_completion_failure_is_atomic_even_caught_inside_outer_transaction(capable_cohort, monkeypatch, failure):
    _create()
    request, context = _request()
    original_audit, original_use = vault_service.audit, vault_service.record_signing_use

    def fail_audit(conn, event, **kwargs):
        if event == "signed":
            raise RuntimeError("injected audit failure")
        return original_audit(conn, event, **kwargs)

    def fail_use(conn, *args, **kwargs):
        original_use(conn, *args, **kwargs)
        raise RuntimeError("injected post-use failure")

    if failure == "audit":
        monkeypatch.setattr(vault_service, "audit", fail_audit)
    elif failure == "use":
        monkeypatch.setattr(vault_service, "record_signing_use", fail_use)
    if failure == "outer-rollback":
        with pytest.raises(RuntimeError, match="outer rollback"):
            with api._vault_engine().begin() as conn:
                _complete(conn, request, context)
                assert _state(conn, request["id"]) == ("approved", 1, 1)
                raise RuntimeError("outer rollback")
    else:
        with api._vault_engine().begin() as conn:
            with pytest.raises((vault_service.InvalidRequestError, RuntimeError)):
                _complete(conn, request, context, **({"signature": {"signature": "3006020101020101"}} if failure == "bad-der" else {}))
            assert _state(conn, request["id"]) == ("pending", 0, 0)
    with api._vault_engine().connect() as conn:
        assert _state(conn, request["id"]) == ("pending", 0, 0)


def test_changed_cohort_refuses_hydration_release_and_completion_without_recapture(capable_cohort, monkeypatch):
    _create()
    request, context = _request()
    monkeypatch.setattr(release, "current_support", lambda: None)
    with pytest.raises(api.VaultApiError) as error:
        api.get_vault_request(request["id"], audience=vault_service.REQUEST_AUDIENCE_UI)
    assert error.value.code == "protected_bitcoin_cohort_changed"
    with pytest.raises(api.VaultApiError) as error:
        api._attach_signed_sign_operation_context(request["id"])
    assert error.value.code == "protected_bitcoin_cohort_changed"
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.ProtectedBitcoinCohortChangedError):
            _complete(conn, request, context)
        assert _state(conn, request["id"]) == ("pending", 0, 0)


@pytest.mark.parametrize("target", ["publicKey", "signingKey", "sandboxCohort", "extra"])
def test_release_must_match_initial_capture_at_storage_completion(capable_cohort, target):
    _create()
    request, context = _request()
    with api._vault_engine().begin() as conn:
        delivery = json.loads(conn.execute(select(vault_requests.c.delivery).where(vault_requests.c.id == request["id"])).scalar_one())
        delivery["operation_context"]["release"][target] = "not the initial capture"
        conn.execute(vault_requests.update().where(vault_requests.c.id == request["id"]).values(delivery=json.dumps(delivery)))
        with pytest.raises(vault_service.InvalidRequestError, match="release"):
            _complete(conn, request, context)
        assert _state(conn, request["id"]) == ("pending", 0, 0)


def test_current_legacy_pin_cannot_advertise_or_admit_new_inputs():
    _create()
    context = _case()
    with api._vault_engine().begin() as conn:
        meta = vault_service.get_secret_meta(conn, "PUBLIC_TEST_BTC")
        assert meta["bitcoin_address_types"] == ["p2tr"]
        assert meta["signing_addresses"]["eth"]
        assert meta["signing_addresses"]["btc_legacy"] is None and meta["signing_addresses"]["btc_segwit"] is None
        assert meta["signing_addresses"]["btc_taproot"]
        with pytest.raises(vault_service.ProtectedBitcoinUnsupportedError):
            vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", scheme="ecdsa-secp256k1-der", digest=context["digest"], signing_context=context)
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0


def test_no_headless_claim_and_completion_cannot_replay(capable_cohort):
    _create()
    request, context = _request()
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError, match="headless claim"):
            vault_service.claim_sign_request(conn, request["id"], name="PUBLIC_TEST_BTC", scheme="ecdsa-secp256k1-der", digest=context["digest"], signing_context=context)
        assert _state(conn, request["id"]) == ("pending", 0, 0)
        _complete(conn, request, context)
        with pytest.raises(vault_service.InvalidRequestError, match="not pending"):
            _complete(conn, request, context)
        assert _state(conn, request["id"]) == ("approved", 1, 1)


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("context_mode", ["absent", "matching", "null", "mismatch"])
def test_api_completion_context_omission_is_new_only_and_exact(capable_cohort, address_type, context_mode):
    # Storage-only coverage cannot catch the parent's existing context-free POST
    # being rejected, or explicit null silently acquiring saved-request authority.
    _create()
    request, context = _request(address_type)
    signature = {"signature": _signature(context)}
    payload = {"name": "PUBLIC_TEST_BTC", "request_id": request["id"], "digest": context["digest"],
               "scheme": "ecdsa-secp256k1-der", "signature": signature}
    if context_mode != "absent":
        payload["signing_context"] = None if context_mode == "null" else copy.deepcopy(context)
        if context_mode == "mismatch":
            payload["signing_context"]["network"] = "testnet"
    if context_mode in {"absent", "matching"}:
        result = api.vault_sign(payload)
        assert result["ok"] is True and result["signature"] == signature
        assert result["request"]["delivery"]["signing_key"] == request["delivery"]["signing_key"]
        assert result["request"]["delivery"]["signing_context"] == context
        saved = api.get_vault_request(request["id"])
        assert saved["result"] == {"type": "signature", "signature": signature}
    else:
        with pytest.raises(api.VaultApiError):
            api.vault_sign(payload)
    with api._vault_engine().connect() as conn:
        expected = ("approved", 1, 1) if context_mode in {"absent", "matching"} else ("pending", 0, 0)
        assert _state(conn, request["id"]) == expected


@pytest.mark.parametrize("selector", [
    {"chain": "bitcoin", "account": 0, "address_type": "p2pkh"},
    {"chain": "evm"}, {"account": 0}, {"address_type": "p2pkh"},
    {"scheme": "schnorr-secp256k1-bip341-keypath"},
    {"scheme": "ed25519", "message_hex": "01"},
])
def test_new_api_pairing_refuses_with_exact_type_code_before_request_or_custody(capable_cohort, monkeypatch, selector):
    _create()
    context = _case()
    monkeypatch.setattr(api, "_require_avault_hd_seed_surface", lambda **kw: pytest.fail("new ordinary mismatch reached custody/version probe"))
    payload = {"name": "PUBLIC_TEST_BTC", "scheme": "ecdsa-secp256k1-der", "digest": context["digest"],
               "signing_context": context, **selector}
    with pytest.raises(api.VaultApiError) as error:
        api.request_vault_sign(payload)
    assert error.value.code == "protected_bitcoin_type_mismatch" and error.value.status == 409
    with api._vault_engine().connect() as conn:
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0


@pytest.mark.parametrize("mutation", [{"digest": "not-a-digest"}, {"digest": "00" * 32}])
def test_new_api_bad_digest_has_existing_context_error_not_legacy_input_code(capable_cohort, mutation):
    _create()
    context = _case()
    with pytest.raises(api.VaultApiError) as error:
        api.request_vault_sign({"name": "PUBLIC_TEST_BTC", "scheme": "ecdsa-secp256k1-der", "digest": context["digest"],
                                "signing_context": context, **mutation})
    assert error.value.code == "invalid_request" and error.value.status == 409


def test_new_context_cannot_fall_back_to_standard_custody(capable_cohort, monkeypatch):
    _create()
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(protection="standard"))
    context = _case()
    monkeypatch.setattr(api, "avault_sign", lambda *a, **k: pytest.fail("new protected kind fell through to standard custody"))
    monkeypatch.setattr(api, "_run_avault", lambda *a, **k: pytest.fail("new protected kind reached avault"))
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign({"name": "PUBLIC_TEST_BTC", "digest": context["digest"], "scheme": "ecdsa-secp256k1-der", "signing_context": context})
    assert error.value.code == "protected_bitcoin_unsupported" and error.value.status == 409


@pytest.mark.parametrize(("mutation", "code"), [
    ({"chain": "bitcoin", "address_type": "p2pkh"}, "protected_bitcoin_type_mismatch"),
    ({"account": 0}, "protected_bitcoin_type_mismatch"),
    ({"scheme": "schnorr-secp256k1-bip341-keypath"}, "protected_bitcoin_type_mismatch"),
    ({"scheme": "ed25519", "message_hex": "01"}, "protected_bitcoin_type_mismatch"),
    ({"digest": "not-a-digest"}, "invalid_request"),
    ({"digest": "00" * 32}, "invalid_request"),
])
def test_context_free_completion_keeps_new_pairing_errors_and_no_partial_success(capable_cohort, mutation, code):
    _create()
    request, context = _request()
    payload = {"name": "PUBLIC_TEST_BTC", "request_id": request["id"], "digest": context["digest"],
               "scheme": "ecdsa-secp256k1-der", "signature": {"signature": _signature(context)}, **mutation}
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign(payload)
    assert error.value.code == code and error.value.status == 409
    with api._vault_engine().connect() as conn:
        assert _state(conn, request["id"]) == ("pending", 0, 0)


@pytest.mark.parametrize("requester", [{"extra": {"parentTransaction": "00"}}, {"note": "x" * 524_288}])
def test_storage_intake_bounds_requester_before_writer_or_json_clone(capable_cohort, monkeypatch, requester):
    from storage import agent_session_rows

    _create()
    engine = api._vault_engine()
    context = _case()
    monkeypatch.setattr(agent_session_rows, "reserve_write_lock", lambda *a: pytest.fail("invalid requester reached writer reservation"))
    with engine.begin() as conn:
        with pytest.raises(vault_service.InvalidSigningContextError):
            vault_service.create_sign_request(conn, "PUBLIC_TEST_BTC", scheme="ecdsa-secp256k1-der", digest=context["digest"],
                                              signing_context=context, requester=requester)
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0
