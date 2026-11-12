"""Solana / Ed25519 Vault contracts: exact-message signing, key creation, and the typed wire.

Expected values come from published sources, never from the code under test: RFC 8032
signatures, well-known Solana program addresses, and a sign-context fixture whose
canonical bytes were produced by the sandbox's own consumer. Storage is isolated by
conftest; avault is replaced only at its subprocess edge, so the request Avibe sends
to it is part of what is checked.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_service, vault_solana
from storage.models import vault_audit, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from vibe import api, cli

FIXTURES = Path(__file__).parent / "fixtures" / "vault_signing"
EMPTY, ONE_BYTE, TWO_BYTES = json.loads((FIXTURES / "rfc8032-ed25519.json").read_text())["vectors"]
SANDBOX = json.loads((FIXTURES / "protected-solana-sign-context.json").read_text())
RELEASE = SANDBOX["signedContext"]["release"]
# A 150-byte Solana transfer message signed by RFC 8032 TEST 1's key.
TRANSFER = {
    "public_key": SANDBOX["publicKey"],
    "message": RELEASE["messageHex"],
    "signature": SANDBOX["expectedSignature"],
}
SECP_KEY = {"curve": "secp256k1", "public_key": "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"}
SECP_SCHEMES = sorted(vault_service.SUPPORTED_SIGNATURE_SCHEMES - {"ed25519"})
DIGEST = "ab" * 32
ENVELOPE = {"ciphertext": "test-envelope", "nonce": "test-nonce", "wrap_meta": "test-meta"}
BLIND_BOX = {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}
ENTRYPOINTS = [api.vault_sign, api.request_vault_sign, "service", "cli"]


def solana_key(public_key: str) -> dict:
    return {"curve": "ed25519", "chain": "solana", "public_key": public_key}


def solana_context(message: str) -> dict:
    return {"kind": "solana-transaction-v1", "messageHex": message}


def protected_envelope(public_key: str, *, kind: str = "keypair", curve: str | None = None) -> Sealed:
    record_meta = {"kind": kind, "public_key": public_key}
    if curve is not None:
        record_meta["curve"] = curve
    return Sealed(
        ciphertext="protected-ciphertext",
        nonce="protected-nonce",
        wrap_meta=json.dumps({
            "v": 1,
            "scheme": "machine-aesgcm-v1",
            "copies": [],
            "record_meta": record_meta,
        }),
    )


def create_key(signing_key: dict | None, *, protection: str = "standard", always_ask: bool = False) -> None:
    sealed = Sealed(**ENVELOPE)
    if protection == "protected" and vault_solana.is_ed25519_key(signing_key):
        sealed = protected_envelope(signing_key["public_key"], curve="ed25519")
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(
            conn, name="KEY", kind="keypair", protection=protection, signer_kind="local",
            sealed=sealed, policy={"always_ask": True} if always_ask else None,
            public_meta={"signing_public_key": signing_key} if signing_key else {},
        )


@pytest.fixture
def avault(monkeypatch):
    """avault at its process boundary: records each request and answers with ``reply``."""
    fake = SimpleNamespace(calls=[], version=api.AVAULT_ED25519_MIN_VERSION, reply={}, notify=Mock())

    def run(args, *, stdin=None, **_kwargs):
        fake.calls.append((list(args), json.loads(stdin) if stdin else None))
        return SimpleNamespace(returncode=0, stdout=json.dumps(fake.reply), stderr="")

    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": fake.version})
    monkeypatch.setattr(api, "_run_avault", run)
    monkeypatch.setattr(api, "_notify_vault_request_created", fake.notify)
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", fake.notify)
    return fake


def request_rows() -> list[dict]:
    with api._vault_engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(vault_requests)).mappings()]


def audit_rows() -> list[dict]:
    with api._vault_engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(vault_audit)).mappings()]


def call_entrypoint(entrypoint, payload: dict, capfd) -> tuple[str, str]:
    """Run one sign entry point and return its ``(error code, error text)``."""
    if entrypoint == "service":
        # Catch inside the transaction: a rollback must not hide an inserted request.
        with api._vault_engine().begin() as conn:
            with pytest.raises(vault_service.InvalidRequestError) as service_error:
                vault_service.create_sign_request(
                    conn, payload["name"], scheme=payload["scheme"], digest=payload.get("digest"),
                    message_hex=payload.get("message_hex"), signing_context=payload.get("signing_context"),
                )
        return type(service_error.value).__name__, str(service_error.value)
    if entrypoint == "cli":
        argv = ["vault", "sign", payload["name"], "--scheme", payload["scheme"]]
        for flag, key in (("--digest", "digest"), ("--message-hex", "message_hex")):
            if payload.get(key) is not None:
                argv += [flag, payload[key]]
        if payload.get("signing_context") is not None:
            argv += ["--signing-context-json", json.dumps(payload["signing_context"])]
        assert cli.cmd_vault_sign(cli.build_parser().parse_args(argv)) == 1
        error = json.loads(capfd.readouterr().err)
        return error["code"], error["error"]
    with pytest.raises(api.VaultApiError) as api_error:
        entrypoint(payload)
    return api_error.value.code, str(api_error.value)


def assert_nothing_happened(avault) -> None:
    assert request_rows() == []
    assert avault.calls == []
    avault.notify.assert_not_called()


@pytest.mark.parametrize("address, public_key", [
    ("11111111111111111111111111111111", "00" * 32),
    ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "06ddf6e1d765a193d9cbe146ceeb79ac1cb485ed5f5b37913a8cf5857eff00a9"),
    ("SysvarRent111111111111111111111111111111111", "06a7d517192c5c51218cc94c3d4af17f58daee089ba1fd44e3dbd98a00000000"),
])
def test_solana_address_is_the_base58_public_key(address, public_key):
    """Published program ids, including the leading-zero case a big-integer encoder drops."""
    assert vault_solana.address(public_key) == address
    create_key(solana_key(public_key))
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "KEY")
    # One Solana row, and no EVM or Bitcoin identity for an Ed25519 key.
    assert meta["signing_curve"] == "ed25519"
    assert meta["signing_addresses"] == {"solana": address}
    assert meta["per_use_sign"] is True


@pytest.mark.parametrize("vector", [EMPTY, ONE_BYTE, TWO_BYTES], ids=lambda vector: vector["name"])
def test_rfc8032_signatures_verify_only_for_their_key_and_message(vector):
    vault_solana.verify_signature(vector["public_key"], vector["message"], vector["signature"])
    other = ONE_BYTE if vector is not ONE_BYTE else TWO_BYTES
    for public_key, message, signature in (
        (other["public_key"], vector["message"], vector["signature"]),
        (vector["public_key"], vector["message"] + "00", vector["signature"]),
        (vector["public_key"], vector["message"], "00" + vector["signature"][2:]),
        (vector["public_key"], vector["message"], vector["signature"][:-2]),
    ):
        with pytest.raises(vault_solana.SolanaSigningError):
            vault_solana.verify_signature(public_key, message, signature)


@pytest.mark.parametrize("approval", [False, True], ids=["headless", "always-ask"])
@pytest.mark.parametrize("case", ["valid", "empty-message", "wrong-key", "tampered", "truncated", "recovery-id"])
def test_standard_signing_sends_the_exact_message_and_delivers_only_a_verified_signature(avault, approval, case):
    """The message reaches avault unhashed, and a result is delivered only if it verifies under the pinned key."""
    vector = EMPTY if case == "empty-message" else TRANSFER
    create_key(solana_key(ONE_BYTE["public_key"] if case == "wrong-key" else vector["public_key"]), always_ask=approval)
    signature = vector["signature"]
    if case == "tampered":
        signature = "00" + signature[2:]
    if case == "truncated":
        signature = signature[:-2]
    avault.reply = {"signature": signature, "recovery_id": 0 if case == "recovery-id" else None}
    # Either hex case is accepted at the boundary and stored lower case.
    payload = {"name": "KEY", "scheme": "ed25519", "message_hex": vector["message"].upper()}
    if approval:
        pending = api.vault_sign(payload)
        assert pending["code"] == "approval_required"
        request = pending["request"]
        assert request["delivery"]["message_hex"] == vector["message"]
        assert "digest" not in request["delivery"]
        # A standard request binds its whole message; it has no typed context or key pin.
        assert request["delivery"]["operation_context"]["release"] == {
            "name": "KEY", "scheme": "ed25519", "messageHex": vector["message"],
        }
        assert avault.calls == []
        payload["request_id"] = request["id"]
    if case in {"valid", "empty-message"}:
        result = api.vault_sign(payload)
        assert result["signature"] == {"signature": vector["signature"], "recovery_id": None}
        if approval:
            assert result["request"]["status"] == "approved"
    else:
        with pytest.raises(api.VaultApiError, match="ed25519 signature"):
            api.vault_sign(payload)
        if approval:
            failed = api.get_vault_request(payload["request_id"])
            assert failed["request"]["status"] == "failed"
            assert not failed.get("result")
        with api._vault_engine().connect() as conn:
            assert vault_service.get_secret_meta(conn, "KEY").get("last_used_at") is None
        assert "signed" not in [row["event"] for row in audit_rows()]
    assert avault.calls == [(
        ["sign"],
        {"name": "KEY", "key_envelope": ENVELOPE, "message_hex": vector["message"], "scheme": "ed25519"},
    )]
    if case == "valid":
        # The audit trail names the message by hash and length, without copying it.
        signed = json.loads(next(row["delivery"] for row in audit_rows() if row["event"] == "signed"))
        assert signed["message_sha256"] == hashlib.sha256(bytes.fromhex(vector["message"])).hexdigest()
        assert signed["message_bytes"] == 150
        assert all(vector["message"] not in (row["delivery"] or "") for row in audit_rows())


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("case", ["digest-with-ed25519", "both-with-ed25519", "no-message", "message-with-secp"])
def test_each_scheme_accepts_only_its_own_signing_input(avault, capfd, entrypoint, case):
    """A digest must never stand in for a message, nor a message for a digest."""
    create_key(SECP_KEY if case == "message-with-secp" else solana_key(TWO_BYTES["public_key"]))
    payload = {
        "digest-with-ed25519": {"scheme": "ed25519", "digest": DIGEST},
        "both-with-ed25519": {"scheme": "ed25519", "digest": DIGEST, "message_hex": TWO_BYTES["message"]},
        "no-message": {"scheme": "ed25519"},
        "message-with-secp": {"scheme": "ecdsa-secp256k1-recoverable", "message_hex": TWO_BYTES["message"]},
    }[case]
    code, message = call_entrypoint(entrypoint, {"name": "KEY", **payload}, capfd)
    assert code == ("InvalidSignInputError" if entrypoint == "service" else "invalid_sign_input")
    assert ("--digest" if case == "message-with-secp" else "--message-hex") in message
    assert_nothing_happened(avault)


@pytest.mark.parametrize("message_hex", ["0", "zz", "af 82", 7, "00" * (1024 * 1024 + 1)])
def test_message_hex_must_be_whole_bytes_of_hex_within_the_limit(avault, message_hex):
    create_key(solana_key(TWO_BYTES["public_key"]))
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign({"name": "KEY", "scheme": "ed25519", "message_hex": message_hex})
    assert error.value.code == "invalid_sign_input"
    assert_nothing_happened(avault)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("scheme", ["ed25519", *SECP_SCHEMES])
def test_a_key_signs_only_with_its_own_curve(avault, capfd, entrypoint, scheme):
    """An Ed25519 seed must not reach a secp256k1 signer, and the reverse."""
    exact_message = scheme == "ed25519"
    create_key(SECP_KEY if exact_message else solana_key(TWO_BYTES["public_key"]))
    payload = {"name": "KEY", "scheme": scheme}
    payload.update({"message_hex": TWO_BYTES["message"]} if exact_message else {"digest": DIGEST})
    code, message = call_entrypoint(entrypoint, payload, capfd)
    assert code == ("InvalidSigningKeyError" if entrypoint == "service" else "invalid_signing_key")
    assert "Solana Ed25519 key" in message
    assert_nothing_happened(avault)


@pytest.mark.parametrize("public_key", [None, "", TWO_BYTES["public_key"].upper(), TWO_BYTES["public_key"][:-2], "zz" * 32])
def test_ed25519_signing_requires_the_canonical_pinned_public_key(avault, public_key):
    create_key({"curve": "ed25519", "chain": "solana", **({"public_key": public_key} if public_key is not None else {})})
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign({"name": "KEY", "scheme": "ed25519", "message_hex": TWO_BYTES["message"]})
    assert error.value.code == "invalid_signing_key"
    assert_nothing_happened(avault)
    # A key whose public key is damaged still says which chain it belongs to, and offers no address.
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "KEY")
    assert meta["signing_curve"] == "ed25519"
    assert "signing_addresses" not in meta


@pytest.mark.parametrize("signing_key, curve", [
    (SECP_KEY, "secp256k1"),
    ({"curve": "secp256k1", "public_key": "not-a-key"}, "secp256k1"),
    ({"curve": "p256", "public_key": SECP_KEY["public_key"]}, None),
    ({"public_key": SECP_KEY["public_key"]}, None),
    (None, None),
])
def test_metadata_reports_the_stored_curve_and_never_guesses_one(signing_key, curve):
    create_key(signing_key)
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "KEY")
    assert meta.get("signing_curve") == curve


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("protection", ["standard", "protected"])
@pytest.mark.parametrize("case", [
    "agent-operation", "evm-transaction", "eip-712", "bitcoin", "other-message", "upper-case", "empty", "extra-digest",
    "solana-with-secp",
])
def test_only_a_matching_solana_context_binds_an_exact_message(avault, capfd, entrypoint, protection, case):
    """The context kind, the scheme and the key's curve must agree, and the context must carry the same bytes."""
    message = TWO_BYTES["message"]
    create_key(
        SECP_KEY if case == "solana-with-secp" else solana_key(TWO_BYTES["public_key"]),
        always_ask=True, protection=protection,
    )
    context = {
        "agent-operation": {
            "kind": "avault-agent-operation", "digestAlgorithm": "avault-operation-hash-v1", "digest": DIGEST,
            "canonicalPreimage": "avault-agent-operation:v1\nsign a Solana message",
        },
        "evm-transaction": {"kind": "evm-transaction", "digestAlgorithm": "keccak256", "chainId": "1", "unsignedTransaction": "0x01", "digest": DIGEST},
        "eip-712": {"kind": "eip-712-typed-data", "digestAlgorithm": "eip712", "typedData": {}, "digest": DIGEST},
        "bitcoin": {"kind": "bitcoin-taproot-keypath-v1", "digest": DIGEST},
        "other-message": solana_context("af83"),
        "upper-case": solana_context(message.upper()),
        "empty": solana_context(""),
        "extra-digest": {**solana_context(message), "digest": DIGEST},
        "solana-with-secp": solana_context(message),
    }[case]
    payload = {"name": "KEY", "signing_context": context}
    if case == "solana-with-secp":
        payload.update({"scheme": "ecdsa-secp256k1-recoverable", "digest": DIGEST})
    elif case == "empty":
        payload.update({"scheme": "ed25519", "message_hex": ""})
    else:
        payload.update({"scheme": "ed25519", "message_hex": message})
    code, _message = call_entrypoint(entrypoint, payload, capfd)
    assert code == ("InvalidSigningContextError" if entrypoint == "service" else "invalid_request")
    assert_nothing_happened(avault)


class Unscanned(str):
    """A context value that fails the test if anything encodes it, as a generic JSON pass would."""

    def encode(self, *_args, **_kwargs):
        pytest.fail("an oversized Solana context was scanned before its size was checked")


@pytest.mark.parametrize("entrypoint", [api.vault_sign, api.request_vault_sign, "cli"])
@pytest.mark.parametrize("placement", ["signing_context", "signingContext", "delivery.signing_context", "delivery.signingContext"])
@pytest.mark.parametrize("scheme", ["ed25519", "ecdsa-secp256k1-recoverable"])
def test_an_oversized_solana_context_is_refused_before_it_is_scanned(avault, monkeypatch, capfd, entrypoint, placement, scheme):
    """The 1 MiB bound applies to the context too, ahead of the pass that walks and encodes every value."""
    create_key(solana_key(TWO_BYTES["public_key"]) if scheme == "ed25519" else SECP_KEY, always_ask=True)
    walk = Mock(side_effect=AssertionError("generic context traversal ran"))
    monkeypatch.setattr(api, "_validate_signing_context_json", walk)
    oversized = "00" * (vault_solana.MAX_MESSAGE_BYTES + 1)
    payload = {"name": "KEY", "scheme": scheme}
    payload.update({"message_hex": "00"} if scheme == "ed25519" else {"digest": DIGEST})
    if entrypoint == "cli":
        if placement != "signing_context":
            pytest.skip("the CLI has one context flag")
        payload["signing_context"] = solana_context(oversized)
    else:
        context = solana_context(Unscanned(oversized))
        if "." in placement:
            payload["delivery"] = {placement.split(".")[1]: context}
        else:
            payload[placement] = context
    code, message = call_entrypoint(entrypoint, payload, capfd)
    assert code == "invalid_request"
    assert ("1 MiB" if scheme == "ed25519" else "requires scheme ed25519") in message
    walk.assert_not_called()
    assert_nothing_happened(avault)


def test_a_standard_request_may_carry_the_solana_context_and_binds_it(avault):
    create_key(solana_key(TRANSFER["public_key"]), always_ask=True)
    context = solana_context(TRANSFER["message"])
    request = api.request_vault_sign(
        {"name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"], "signing_context": context},
    )["request"]
    assert request["delivery"]["signing_context"] == context
    assert request["delivery"]["operation_context"]["release"] == {
        "name": "KEY", "scheme": "ed25519", "messageHex": TRANSFER["message"], "signingContext": context,
    }
    # Approval must repeat the same message: a different one cannot ride on this request.
    with pytest.raises(api.VaultApiError, match="does not match the sign request"):
        api.vault_sign({"name": "KEY", "scheme": "ed25519", "message_hex": "00", "request_id": request["id"]})
    assert api.get_vault_request(request["id"])["request"]["status"] == "pending"
    assert avault.calls == []


def test_protected_solana_signing_creates_bound_pending_and_completes_once(avault, monkeypatch):
    custody_probe = Mock(side_effect=AssertionError("protected custody must not call avault"))
    monkeypatch.setattr(api, "_require_avault_ed25519_surface", custody_probe)
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    context = solana_context(TRANSFER["message"])
    payload = {
        "name": "KEY",
        "scheme": "ed25519",
        "message_hex": TRANSFER["message"],
        "signing_context": context,
    }
    pending = api.vault_sign(payload)
    assert pending["code"] == "browser_signature_required"
    request = pending["request"]
    assert request["status"] == "pending"
    assert request["delivery"]["message_hex"] == TRANSFER["message"]
    assert request["delivery"]["signing_context"] == context
    assert request["delivery"]["operation_context"]["release"] == {
        "name": "KEY",
        "scheme": "ed25519",
        "messageHex": TRANSFER["message"],
        "publicKey": TRANSFER["public_key"],
        "signingContext": context,
    }
    assert avault.calls == []

    completed = api.vault_sign({
        **payload,
        "request_id": request["id"],
        "signature": {"signature": TRANSFER["signature"], "recovery_id": None},
    })
    assert completed["ok"] is True
    assert completed["signature"] == {"signature": TRANSFER["signature"]}
    assert completed["request"]["status"] == "approved"
    assert avault.calls == []

    with pytest.raises(api.VaultApiError, match="not pending"):
        api.vault_sign({
            **payload,
            "request_id": request["id"],
            "signature": {"signature": TRANSFER["signature"]},
        })

    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "KEY")
    assert meta["per_use_sign"] is True
    custody_probe.assert_not_called()
    signed_events = [row for row in audit_rows() if row["event"] == "signed"]
    assert len(signed_events) == 1
    assert json.loads(signed_events[0]["delivery"])["message_bytes"] == 150


@pytest.mark.parametrize("mutator", [
    lambda payload: {**payload, "message_hex": "00"},
    lambda payload: {**payload, "signing_context": solana_context("00")},
    lambda payload: {**payload, "message_hex": "00", "signing_context": solana_context("00")},
    lambda payload: {**payload, "signing_context": None},
    lambda payload: {**payload, "scheme": "ecdsa-secp256k1-recoverable", "digest": "ab" * 32, "message_hex": None},
])
def test_protected_completion_cannot_switch_persisted_binding(avault, mutator):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {
        "name": "KEY",
        "scheme": "ed25519",
        "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    pending = api.vault_sign(payload)["request"]
    changed = mutator({**payload, "request_id": pending["id"], "signature": {"signature": TRANSFER["signature"]}})
    with pytest.raises(api.VaultApiError):
        api.vault_sign(changed)
    assert api.get_vault_request(pending["id"])["request"]["status"] == "pending"
    assert avault.calls == []


@pytest.mark.parametrize("signature", [
    {"signature": "00" + TRANSFER["signature"][2:]},
    {"signature": TRANSFER["signature"][:-2]},
    {"signature": TRANSFER["signature"], "recovery_id": 0},
    {"signature": TRANSFER["signature"], "recovery_id": False},
    {"signature": TRANSFER["signature"], "scheme": "ed25519"},
])
def test_protected_completion_rejects_bad_signature_without_settling(avault, signature):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {
        "name": "KEY",
        "scheme": "ed25519",
        "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    pending = api.vault_sign(payload)["request"]
    with pytest.raises(api.VaultApiError):
        api.vault_sign({**payload, "request_id": pending["id"], "signature": signature})
    failed = api.get_vault_request(pending["id"])
    assert failed["request"]["status"] == "pending"
    assert not failed.get("result")
    assert "signed" not in [row["event"] for row in audit_rows()]
    assert avault.calls == []


def test_protected_completion_expiry_and_missing_context_fail_closed(avault):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {"name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"]}
    with pytest.raises(api.VaultApiError, match="verifiable signing_context"):
        api.vault_sign(payload)
    assert request_rows() == []

    with_context = {**payload, "signing_context": solana_context(TRANSFER["message"])}
    pending = api.vault_sign(with_context)["request"]
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == pending["id"])
            .values(expires_at="2000-01-01T00:00:00+00:00")
        )
    with pytest.raises(api.VaultApiError, match="expired"):
        api.vault_sign({
            **with_context,
            "request_id": pending["id"],
            "signature": {"signature": TRANSFER["signature"]},
        })
    assert api.get_vault_request(pending["id"])["request"]["status"] == "expired"
    assert avault.calls == []


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("case", ["missing", "outer-oversize", "context-oversize"])
def test_protected_pending_requires_a_bounded_context(avault, capfd, entrypoint, case):
    """The owner and callers all refuse before requests, notifications or key use."""
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {"name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"]}
    if case != "missing":
        oversized = "00" * (vault_solana.MAX_MESSAGE_BYTES + 1)
        payload["signing_context"] = solana_context(oversized)
        if case == "outer-oversize":
            payload["message_hex"] = oversized
    call_entrypoint(entrypoint, payload, capfd)
    assert_nothing_happened(avault)


@pytest.mark.parametrize("case", ["context", "message", "scheme", "signature", "key", "record-id", "name"])
def test_protected_service_completion_is_the_final_binding_owner(avault, case):
    """API checks alone cannot protect internal completion or a replaced stored record."""
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {
        "name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    with api._vault_engine().begin() as conn:
        pending = vault_service.create_sign_request(
            conn, **payload,
            delivery={"signing_key": {"secret_id": "caller", "public_key": ONE_BYTE["public_key"]}},
        )
        assert pending["delivery"]["signing_key"]["public_key"] == TRANSFER["public_key"]
        completion = {**payload, "signature": {"signature": TRANSFER["signature"]}}
        if case == "context":
            completion.pop("signing_context")
        elif case == "message":
            completion.update(message_hex="00", signing_context=solana_context("00"))
        elif case == "scheme":
            completion.update(scheme="ecdsa-secp256k1-der", digest=DIGEST, message_hex=None)
        elif case == "signature":
            completion["signature"] = {"signature": "00" + TRANSFER["signature"][2:]}
        elif case == "name":
            completion["name"] = "OTHER"
        elif case == "key":
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == "KEY").values(
                public_meta=json.dumps({"signing_public_key": solana_key(ONE_BYTE["public_key"])}),
            ))
        else:
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == "KEY").values(id="vlt_replaced"))
        # Catch inside the transaction so an accidental settlement cannot be hidden by rollback.
        with pytest.raises(vault_service.InvalidRequestError):
            vault_service.complete_sign_request(conn, pending["id"], **completion, browser_signed=True)
        assert vault_service.get_request(conn, pending["id"])["status"] == "pending"
    assert "signed" not in [row["event"] for row in audit_rows()]
    assert avault.calls == []


@pytest.mark.parametrize("state", ["denied", "approved"])
def test_protected_completion_rejects_nonpending_state(avault, state):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {
        "name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    pending = api.vault_sign(payload)["request"]
    with api._vault_engine().begin() as conn:
        conn.execute(vault_requests.update().where(vault_requests.c.id == pending["id"]).values(status=state))
    with pytest.raises(api.VaultApiError, match="not pending"):
        api.vault_sign({**payload, "request_id": pending["id"], "signature": {"signature": TRANSFER["signature"]}})
    assert "signed" not in [row["event"] for row in audit_rows()]


def test_public_signature_can_settle_a_new_identical_request_without_attesting_fresh_uv(avault):
    """Preserved protocol residual: request replay is blocked, signature reuse is not UV proof."""
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    payload = {
        "name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    requests = [api.vault_sign(payload)["request"] for _ in range(2)]
    assert requests[0]["id"] != requests[1]["id"]
    for request in requests:
        result = api.vault_sign({
            **payload, "request_id": request["id"], "signature": {"signature": TRANSFER["signature"]},
        })
        assert result["request"]["status"] == "approved"
    assert avault.calls == []


def test_protected_http_create_sign_and_complete_uses_real_storage(avault):
    """REST adapters preserve the canonical input and return only a verified result."""
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    client = app.test_client()
    envelope = protected_envelope(TRANSFER["public_key"], curve="ed25519")
    created = client.post("/api/vault/secrets", headers=csrf_headers(client), json={
        "name": "KEY", "kind": "keypair", "protection": "protected",
        "sealed": {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce, "wrap_meta": envelope.wrap_meta},
        "public_meta": {"signing_public_key": solana_key(TRANSFER["public_key"])},
    })
    assert created.status_code == 200
    payload = {
        "name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"],
        "signing_context": solana_context(TRANSFER["message"]),
    }
    pending = client.post("/api/vault/sign", headers=csrf_headers(client), json=payload).get_json()
    assert pending["code"] == "browser_signature_required"
    completed = client.post("/api/vault/sign", headers=csrf_headers(client), json={
        **payload, "request_id": pending["request"]["id"], "signature": {"signature": TRANSFER["signature"]},
    })
    assert completed.status_code == 200
    assert completed.get_json()["request"]["status"] == "approved"
    assert avault.calls == []


def test_cli_protected_sign_creates_the_same_bound_pending_request(avault, capfd):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    args = cli.build_parser().parse_args([
        "vault", "sign", "KEY", "--scheme", "ed25519", "--message-hex", TRANSFER["message"],
        "--signing-context-json", json.dumps(solana_context(TRANSFER["message"])),
    ])
    assert cli.cmd_vault_sign(args) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["kind"] == "vault_sign_request"
    assert output["request"]["status"] == "pending"
    assert output["request"]["delivery"]["operation_context"]["release"] == {**RELEASE, "name": "KEY"}
    assert avault.calls == []
    assert cli.cmd_vault_list(cli.build_parser().parse_args(["vault", "list"])) == 0
    note = json.loads(capfd.readouterr().out)["secrets"][0]["solana_signing_note"]
    # The listing names the flags and context this request just used, not a refusal.
    for flag in ("--scheme ed25519", "--message-hex", "--signing-context-json", '"kind":"solana-transaction-v1"', '"messageHex"'):
        assert flag in note
    assert "--digest is refused" in note
    assert "not supported" not in note
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["vault", "sign", "--help"])
    help_text = " ".join(capfd.readouterr().out.split())
    assert '"kind":"solana-transaction-v1"' in help_text
    assert "cannot sign yet" not in help_text


def test_protected_solana_refusal_is_removed_without_downgrade_copy(avault):
    create_key(solana_key(TRANSFER["public_key"]), protection="protected")
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "KEY")
    assert meta["protection"] == "protected"
    assert meta["signing_addresses"] == {"solana": vault_solana.address(TRANSFER["public_key"])}
    assert "standard" not in json.dumps(meta).lower()
    assert avault.calls == []


@pytest.mark.parametrize("approval", [False, True])
def test_an_avault_without_ed25519_refuses_before_signing_and_keeps_the_approval(avault, approval):
    create_key(solana_key(TRANSFER["public_key"]), always_ask=approval)
    payload = {"name": "KEY", "scheme": "ed25519", "message_hex": TRANSFER["message"]}
    if approval:
        payload["request_id"] = api.vault_sign(payload)["request"]["id"]
    avault.version = "0.1.9"
    with pytest.raises(api.VaultApiError, match="requires avault >= 0.1.10") as error:
        api.vault_sign(payload)
    assert error.value.code == "avault_upgrade_required"
    assert avault.calls == []
    if approval:
        # The user can upgrade avault and approve the same request.
        assert api.get_vault_request(payload["request_id"])["request"]["status"] == "pending"


def test_the_protected_binding_is_the_fixture_the_sandbox_verifies():
    """Canonical bytes and daemon signature for the protected release, against the sandbox-made fixture."""
    signed_context = SANDBOX["signedContext"]
    unsigned = {key: value for key, value in signed_context.items() if key != "signature"}
    assert api._vault_sandbox_canonical_json(unsigned) == SANDBOX["canonicalUnsignedContext"]
    daemon_key = {
        "privateKey": base64.b64encode(bytes.fromhex(SANDBOX["daemonSeedHex"])).decode(),
        "keyId": SANDBOX["daemonKeyId"],
    }
    assert api._signed_operation_context(unsigned, daemon_key) == signed_context
    assert RELEASE["signingContext"] == vault_solana.validate_context(
        RELEASE["signingContext"], message_hex=RELEASE["messageHex"],
    )


def test_the_producer_binds_key_and_context_for_a_protected_solana_request(avault):
    """Real protected request → persisted snapshot → the exact fixture release."""
    create_key(solana_key(RELEASE["publicKey"]), protection="protected")
    request = api.request_vault_sign({
        "name": "KEY", "scheme": "ed25519", "message_hex": RELEASE["messageHex"],
        "signing_context": copy.deepcopy(RELEASE["signingContext"]),
    })["request"]
    signed = request["delivery"]["operation_context"]
    assert signed["release"] == {**RELEASE, "name": "KEY"}
    with api._vault_engine().connect() as conn:
        record_id = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == "KEY")).scalar_one()
    assert request["delivery"]["signing_key"] == {"secret_id": record_id, "public_key": RELEASE["publicKey"]}


def keypair_reply(vector: dict, **overrides) -> dict:
    # The address literal is RFC 8032 TEST 2's public key in base58, computed outside Avibe.
    return {
        "envelope": ENVELOPE, "curve": "ed25519", "chain": "solana", "public_key": vector["public_key"],
        "address": "586Z7H2vpX9qNhN2T4e9Utugie3ogjbxzGaMtM3E6HR5", **overrides,
    }


@pytest.mark.parametrize("keypair", [
    {"curve": "ed25519", "source": "generate"},
    {"curve": "ed25519", "source": "import", "format": "base58-secret"},
])
def test_invalid_protection_rejects_solana_keypair_before_avault_dispatch(avault, keypair):
    payload = {"name": "SOL", "kind": "keypair", "protection": "bogus", "keypair": keypair}
    if keypair["source"] == "import":
        payload["blind_box"] = BLIND_BOX
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code == "invalid_protection"
    assert avault.calls == []
    assert api.get_vault_secrets()["secrets"] == []


@pytest.mark.parametrize("source", ["generate", "solana-cli-json", "base58-secret"])
def test_avault_creates_the_solana_key_and_python_stores_only_public_metadata(avault, source):
    """Python relays the request and the opaque blind box; the seed stays inside avault."""
    avault.reply = keypair_reply(ONE_BYTE)
    payload = {"name": "SOL", "kind": "keypair", "protection": "standard", "keypair": {"curve": "ed25519", "source": "generate"}}
    expected = (["keypair", "generate", "--name", "SOL", "--curve", "ed25519"], None)
    if source != "generate":
        payload["keypair"] = {"curve": "ed25519", "chain": "solana", "source": "import", "format": source}
        payload["blind_box"] = BLIND_BOX
        expected = (
            ["keypair", "import", "--name", "SOL", "--curve", "ed25519", "--format", source, "--blind-box"], BLIND_BOX,
        )
    secret = api.create_vault_secret(payload)["secret"]
    assert avault.calls == [expected]
    assert secret["kind"] == "keypair"
    assert secret["protection"] == "standard"
    assert secret["signing_addresses"] == {"solana": avault.reply["address"]}
    assert secret["per_use_sign"] is True
    with api._vault_engine().connect() as conn:
        row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "SOL")).mappings().one()
    assert {key: row[key] for key in ENVELOPE} == ENVELOPE
    assert json.loads(row["public_meta"])["signing_public_key"] == solana_key(ONE_BYTE["public_key"])


def test_protected_solana_record_ingestion_is_opaque_and_never_calls_avault(avault):
    public_key = ONE_BYTE["public_key"]
    envelope = protected_envelope(public_key, curve="ed25519")
    secret = api.create_vault_secret({
        "name": "PROTECTED_SOL",
        "kind": "keypair",
        "protection": "protected",
        "sealed": {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce, "wrap_meta": envelope.wrap_meta},
        "public_meta": {"signing_public_key": solana_key(public_key)},
    })["secret"]
    assert secret["protection"] == "protected"
    assert secret["signing_addresses"] == {"solana": vault_solana.address(public_key)}
    assert secret["per_use_sign"] is True
    assert avault.calls == []
    with api._vault_engine().connect() as conn:
        stored = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "PROTECTED_SOL")).mappings().one()
    assert stored["ciphertext"] == envelope.ciphertext
    assert stored["nonce"] == envelope.nonce
    assert stored["wrap_meta"] == envelope.wrap_meta


@pytest.mark.parametrize("change", [
    {"name": "PROTECTED.SOL"},
    {"kind": "static"},
    {"public_meta": {}},
    {"public_meta": {"signing_public_key": "not-an-object"}},
    {"public_meta": {"signing_public_key": {**solana_key(ONE_BYTE["public_key"]), "extra": True}}},
    {"blind_box": BLIND_BOX},
    {"keypair": {"curve": "ed25519", "source": "generate"}},
    {"keypair": {"curve": "ed25519", "source": "import", "format": "base58-secret"}, "blind_box": BLIND_BOX},
    {"public_meta": {"signing_public_key": {"curve": "secp256k1", "chain": "solana", "public_key": ONE_BYTE["public_key"]}}},
    {"public_meta": {"signing_public_key": {"curve": "ed25519", "chain": "ethereum", "public_key": ONE_BYTE["public_key"]}}},
    {"public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"][:-2] + "00")}},
    {"sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": json.dumps({"v": 1, "scheme": "machine-aesgcm-v1", "copies": [], "record_meta": {"kind": "static", "curve": "ed25519", "public_key": ONE_BYTE["public_key"]}})}},
    {"sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": json.dumps({"v": 1, "scheme": "machine-aesgcm-v1", "copies": [], "record_meta": {"kind": "keypair", "curve": "ed25519", "public_key": TWO_BYTES["public_key"]}})}},
])
def test_protected_solana_record_ingestion_rejects_mismatched_metadata(avault, change):
    envelope = protected_envelope(ONE_BYTE["public_key"], curve="ed25519")
    payload = {
        "name": "PROTECTED_SOL",
        "kind": "keypair",
        "protection": "protected",
        "sealed": {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce, "wrap_meta": envelope.wrap_meta},
        "public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"])},
    }
    payload.update(change)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code in {"invalid_name", "invalid_keypair", "vault_error"}
    assert avault.calls == []
    assert api.get_vault_secrets()["secrets"] == []


@pytest.mark.parametrize("record_change", [
    {"kind": "static"}, {"curve": None}, {"curve": "secp256k1"},
    {"public_key": "AB" * 32}, {"public_key": "01"}, {"name": "OTHER"},
])
def test_protected_record_metadata_has_no_legacy_or_client_fallback(avault, record_change):
    envelope = protected_envelope(ONE_BYTE["public_key"], curve="ed25519")
    wrap = json.loads(envelope.wrap_meta)
    wrap["record_meta"].update(record_change)
    payload = {
        "name": "KEY", "kind": "keypair", "protection": "protected",
        "sealed": {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce, "wrap_meta": json.dumps(wrap)},
        "public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"])},
    }
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code == "invalid_keypair"
    assert api.get_vault_secrets()["secrets"] == []
    assert avault.calls == []


def test_python_does_not_claim_to_authenticate_the_opaque_record_name(avault):
    """Both outer names are valid metadata; only sandbox decryption can detect an AAD mismatch."""
    envelope = protected_envelope(ONE_BYTE["public_key"], curve="ed25519")
    for name in ("SEALED_NAME", "OTHER_NAME"):
        result = api.create_vault_secret({
            "name": name, "kind": "keypair", "protection": "protected",
            "sealed": {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce, "wrap_meta": envelope.wrap_meta},
            "public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"])},
        })
        assert result["secret"]["name"] == name
    assert avault.calls == []


@pytest.mark.parametrize("change, code", [
    ({"protection": "protected"}, "invalid_keypair"),
    ({"public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"])}}, "invalid_keypair"),
    ({"kind": "static"}, "invalid_keypair"),
    ({"blind_box": BLIND_BOX}, "invalid_keypair"),
    ({"keypair": {"curve": "ed25519", "source": "generate", "format": "base58-secret"}}, "invalid_keypair"),
    ({"keypair": {"curve": "ed25519", "source": "import", "format": "base58-secret"}}, "blind_box_required"),
    ({"keypair": {"curve": "ed25519", "source": "import", "format": "mnemonic"}, "blind_box": BLIND_BOX}, "invalid_keypair"),
    ({"keypair": {"curve": "ed25519", "source": "export"}}, "invalid_keypair"),
    ({"keypair": {"curve": "secp256k1", "source": "generate"}}, "invalid_keypair"),
    ({"keypair": {"curve": "ed25519", "chain": "ethereum", "source": "generate"}}, "invalid_keypair"),
    ({"keypair": {"curve": "ed25519", "source": "generate", "seed": "00" * 32}}, "invalid_keypair"),
    ({"keypair": "ed25519"}, "invalid_keypair"),
])
def test_solana_key_creation_rejects_everything_outside_the_contract_before_avault(avault, change, code):
    payload = {"name": "SOL", "kind": "keypair", "protection": "standard", "keypair": {"curve": "ed25519", "source": "generate"}}
    payload.update(change)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code == code
    assert avault.calls == []
    assert api.get_vault_secrets()["secrets"] == []


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_a_client_cannot_pin_its_own_solana_public_key(avault, protection):
    """The public key of a Solana key comes from avault, which holds the seed; a caller-supplied pin is refused."""
    payload = {
        "name": "SOL", "kind": "keypair", "protection": protection, "blind_box": BLIND_BOX, "sealed": ENVELOPE,
        "public_meta": {"signing_public_key": solana_key(ONE_BYTE["public_key"])},
    }
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code == "invalid_keypair"
    assert avault.calls == []
    assert api.get_vault_secrets()["secrets"] == []


@pytest.mark.parametrize("reply", [
    keypair_reply(ONE_BYTE, address="11111111111111111111111111111111"),
    keypair_reply(ONE_BYTE, public_key=ONE_BYTE["public_key"].upper()),
    keypair_reply(ONE_BYTE, curve="secp256k1"),
    keypair_reply(ONE_BYTE, chain="ethereum"),
    keypair_reply(ONE_BYTE, envelope={"ciphertext": "", "nonce": "n", "wrap_meta": "w"}),
    {"curve": "ed25519"},
    [],
])
def test_an_inconsistent_avault_keypair_reply_stores_no_key(avault, reply):
    avault.reply = reply
    with pytest.raises(api.VaultApiError, match="malformed output") as error:
        api.create_vault_secret(
            {"name": "SOL", "kind": "keypair", "protection": "standard", "keypair": {"curve": "ed25519", "source": "generate"}},
        )
    assert error.value.code == "avault_failed"
    assert api.get_vault_secrets()["secrets"] == []


def test_solana_key_creation_needs_an_avault_with_ed25519(avault):
    avault.version = "0.1.9"
    with pytest.raises(api.VaultApiError, match="requires avault >= 0.1.10") as error:
        api.create_vault_secret(
            {"name": "SOL", "kind": "keypair", "protection": "standard", "keypair": {"curve": "ed25519", "source": "generate"}},
        )
    assert error.value.code == "avault_upgrade_required"
    assert avault.calls == []
    assert api.get_vault_secrets()["secrets"] == []


def test_cli_signs_a_solana_message_and_lists_how_to_sign(avault, capfd):
    create_key(solana_key(TWO_BYTES["public_key"]))
    avault.reply = {"signature": TWO_BYTES["signature"], "recovery_id": None}
    args = cli.build_parser().parse_args(["vault", "sign", "KEY", "--scheme", "ed25519", "--message-hex", "AF82"])
    assert cli.cmd_vault_sign(args) == 0
    printed = json.loads(capfd.readouterr().out)
    assert printed["kind"] == "vault_signature"
    assert printed["scheme"] == "ed25519"
    assert printed["message_hex"] == "af82"
    assert "digest" not in printed
    assert printed["signature"] == {"signature": TWO_BYTES["signature"], "recovery_id": None}
    assert cli.cmd_vault_list(cli.build_parser().parse_args(["vault", "list"])) == 0
    row = json.loads(capfd.readouterr().out)["secrets"][0]
    assert row["signing_curve"] == "ed25519"
    assert row["signing_addresses"] == {"solana": vault_solana.address(TWO_BYTES["public_key"])}
    assert "--scheme ed25519 --message-hex" in row["solana_signing_note"]
    assert "bitcoin_signing_warning" not in row
