"""Tests for the vault REST wrappers in vibe/api.py.

REST create delegates sealing to avault and stores only the returned envelope.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import socket
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_service
from storage.models import agent_sessions, scopes, vault_audit, vault_auth_factors, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from tests.vault_webauthn_helpers import WebAuthnTestCredential
from vibe import api

TEST_AGENT_PUBKEY = {"public_key": "pk", "fingerprint": "fp"}


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _insert_workbench_session(conn, *, session_id: str, title: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    scope_id = f"scope_{session_id}"
    conn.execute(
        scopes.insert().values(
            id=scope_id,
            platform="avibe",
            scope_type="project",
            native_id=f"project_{session_id}",
            parent_scope_id=None,
            display_name="Vault Project",
            native_type=None,
            is_private=1,
            supports_threads=1,
            metadata_json="{}",
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
    )
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_id=None,
            agent_name="codex",
            agent_backend="codex",
            agent_variant="codex",
            model=None,
            reasoning_effort=None,
            session_anchor=session_id,
            workdir="/tmp/work",
            native_session_id="native-1",
            title=title,
            status="active",
            agent_status="idle",
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def _grant_from_request(conn, request: dict, *, session_id: str | None = None, cache_ready: bool = True) -> dict:
    option = request["card"]["grant_options"][0]
    return vault_service.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        session_id=session_id,
        request_id=request["id"],
        cache_ready=cache_ready,
    )


def _browser_ecdsa_signature_for_digest(digest: str, *, key_value: int = 1) -> tuple[dict, dict]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    private_key = ec.derive_private_key(key_value, ec.SECP256K1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )
    der = private_key.sign(bytes.fromhex(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = utils.decode_dss_signature(der)
    recovery_id = next(
        index
        for index in range(4)
        if api._secp256k1_recover_ecdsa_public_key(r, s, bytes.fromhex(digest), index)
        == api._secp256k1_decompress_public_key(public_key)
    )
    signature = {
        "signature": r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex(),
        "recovery_id": recovery_id,
    }
    public_meta = {"signing_public_key": {"curve": "secp256k1", "public_key": public_key.hex()}}
    return public_meta, signature


def _signing_context(digest: str) -> dict:
    return {
        "kind": "avault-agent-operation",
        "canonicalPreimage": f"vault-sign-test:{digest}",
        "digestAlgorithm": "avault-operation-hash-v1",
        "digest": digest,
    }


def _assert_no_unlock_material(payload: object) -> None:
    encoded = json.dumps(payload)
    assert "secret_unlock_material" not in encoded
    assert "unlock_material" not in encoded
    assert "ct-protected" not in encoded
    assert "wm-protected" not in encoded


def _payload_keys(payload: object) -> set[str]:
    if isinstance(payload, dict):
        keys = {str(key) for key in payload}
        for value in payload.values():
            keys.update(_payload_keys(value))
        return keys
    if isinstance(payload, list):
        keys: set[str] = set()
        for item in payload:
            keys.update(_payload_keys(item))
        return keys
    return set()


def _stable_json_like_js(payload: object) -> str:
    if isinstance(payload, dict):
        return "{" + ",".join(
            f"{json.dumps(str(key), ensure_ascii=False, separators=(',', ':'))}:{_stable_json_like_js(payload[key])}"
            for key in sorted(payload)
        ) + "}"
    if isinstance(payload, list):
        return "[" + ",".join(_stable_json_like_js(item) for item in payload) + "]"
    return api._jsonify_surrogates_like_js(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _verified_signed_context(context: dict) -> dict:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    unsigned = dict(context)
    signature = unsigned.pop("signature")
    root_key = api.get_vault_sandbox_root_metadata()["root_metadata"]["daemon"]["verificationKeys"][0]
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(root_key["publicKey"]))
    public_key.verify(
        base64.b64decode(signature["value"]),
        api._vault_sandbox_canonical_json(unsigned).encode("utf-8"),
    )
    assert signature["alg"] == "ed25519"
    assert signature["keyId"] == root_key["keyId"]
    return unsigned


def _agent_dek_blindbox(enc: str = "enc", ct: str = "ct") -> dict:
    return {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": enc, "ct": ct}


def _issued_agent_dek_payload(
    request_id: str,
    *,
    grant_duration: str | int = 300,
    blindboxes: dict[str, dict] | None = None,
) -> dict:
    issued = api.create_vault_agent_bindings_batch({"request_id": request_id, "grant_duration": grant_duration})
    return {
        "agent_pubkey": issued["agent_pubkey"],
        "deks": [
            {
                "name": item["name"],
                "dek_blindbox": (blindboxes or {}).get(item["name"], _agent_dek_blindbox()),
                "approval": item["approval"],
            }
            for item in issued["items"]
        ],
    }


def _auth_factor_for_credential(credential: WebAuthnTestCredential) -> dict:
    with api._vault_engine().connect() as conn:
        row = conn.execute(
            select(vault_auth_factors).where(vault_auth_factors.c.credential_id == credential.credential_id_b64)
        ).mappings().one()
    return dict(row)


def _establish_protected_secret_with_factor(
    name: str = "PROTECTED_DELETE",
    credential: WebAuthnTestCredential | None = None,
) -> tuple[WebAuthnTestCredential, dict]:
    credential = credential or WebAuthnTestCredential()
    options = api.create_vault_authz_webauthn_options(origin=credential.origin)
    api.create_vault_secret(
        {
            "name": name,
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d"}},
            "establishing_vmk": True,
            "authz_factor_registration": credential.registration_payload(
                challenge_id=options["challenge_id"],
                challenge_b64=options["webauthn"]["challenge"],
            ),
        },
        origin=credential.origin,
    )
    return credential, _auth_factor_for_credential(credential)


@pytest.fixture
def avault_p2(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: dict(TEST_AGENT_PUBKEY))


def test_vault_sandbox_canonical_json_preserves_unicode_for_signed_contexts():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = api._new_vault_daemon_binding_key()
    context = {
        "v": 2,
        "purpose": "agent-deliver",
        "requestId": "vab_unicode",
        "display": {
            "secrets": [{"name": "生产密钥", "kind": "static"}],
            "sessionLabel": "Alex 的工作台 🚀",
            "command": "部署：echo 你好 🌕",
            "egress": "api.example.com/路径",
            "grantTtlSeconds": 300,
        },
        "expiresAt": "2030-01-01T00:00:00Z",
    }

    signed = api._signed_operation_context(context, key)
    unsigned = dict(signed)
    signature = unsigned.pop("signature")
    canonical = api._vault_sandbox_canonical_json(unsigned)

    assert canonical == _stable_json_like_js(unsigned)
    assert "Alex 的工作台 🚀" in canonical
    assert "部署：echo 你好 🌕" in canonical
    assert "\\u" not in canonical

    public_key = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(key["publicKey"]))
    public_key.verify(base64.b64decode(signature["value"]), canonical.encode("utf-8"))

    envelope = {"ciphertext": "密文🚀", "nonce": "随机值", "wrap_meta": {"label": "中文🔐"}}
    envelope_canonical = api._vault_sandbox_canonical_json(envelope)
    assert envelope_canonical == _stable_json_like_js(envelope)
    assert "\\u" not in envelope_canonical
    assert api._envelope_hash(envelope)["digest"] == hashlib.sha256(envelope_canonical.encode("utf-8")).hexdigest()


def test_vault_sandbox_canonical_json_escapes_lone_surrogates_like_js():
    key = api._new_vault_daemon_binding_key()
    context = {
        "v": 2,
        "purpose": "reveal",
        "requestId": "vrl_surrogate",
        "display": {
            "secrets": [{"name": "BROKEN_INPUT", "kind": "static"}],
            "command": "bad high \ud800 low \ude80 paired \ud83d\ude80",
        },
        "expiresAt": "2030-01-01T00:00:00Z",
    }

    signed = api._signed_operation_context(context, key)
    unsigned = dict(signed)
    unsigned.pop("signature")
    canonical = api._vault_sandbox_canonical_json(unsigned)

    assert canonical == _stable_json_like_js(unsigned)
    assert "\\ud800" in canonical
    assert "\\ude80" in canonical
    assert "paired 🚀" in canonical
    assert not any(0xD800 <= ord(char) <= 0xDFFF for char in canonical)
    canonical.encode("utf-8")


def test_create_list_delete_roundtrip(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("api"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    blind_box = {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}
    created = api.create_vault_secret({"name": "OPENAI_API_KEY", "blind_box": blind_box, "description": "key"})
    assert created["ok"] is True
    assert created["secret"]["name"] == "OPENAI_API_KEY"
    assert "preview" not in created["secret"]
    assert "sk-ant-abcd1234" not in json.dumps(created)
    assert "1234" not in json.dumps(created)
    seal.assert_called_once_with("OPENAI_API_KEY", blind_box)
    with api._vault_engine().connect() as conn:
        assert vault_service.get_envelope(conn, "OPENAI_API_KEY") == _sealed("api")
        public_meta_raw = conn.execute(
            select(vault_secrets.c.public_meta).where(vault_secrets.c.name == "OPENAI_API_KEY")
        ).scalar_one()
        public_meta = json.loads(public_meta_raw)
        assert public_meta == {"description": "key"}
        assert "preview" not in public_meta
        assert "1234" not in json.dumps(vault_service.get_secret_meta(conn, "OPENAI_API_KEY"))

    listed = api.get_vault_secrets()
    assert [s["name"] for s in listed["secrets"]] == ["OPENAI_API_KEY"]
    assert "sk-ant-abcd1234" not in json.dumps(listed)
    assert "1234" not in json.dumps(listed)

    removed = api.delete_vault_secret("OPENAI_API_KEY")
    assert removed == {"ok": True, "removed": True, "name": "OPENAI_API_KEY"}
    assert api.get_vault_secrets()["secrets"] == []


def test_create_vault_secret_publishes_update_event(monkeypatch):
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))

    api.create_vault_secret(
        {
            "name": "EVENT_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    assert ("vaults.updated", {"scope": "secret", "secret_name": "EVENT_KEY"}) in published


def test_standard_rest_create_rejects_plaintext_value(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "OPENAI_API_KEY", "value": "secret"})

    assert exc.value.code == "plaintext_value_rejected"
    blind_box.assert_not_called()


def test_create_rejects_plaintext_value_even_with_sealed_payload(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as standard_exc:
        api.create_vault_secret(
            {
                "name": "MIXED_KEY",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "value": "secret",
            }
        )
    assert standard_exc.value.code == "plaintext_value_rejected"

    with pytest.raises(api.VaultApiError) as protected_exc:
        api.create_vault_secret(
            {
                "name": "PROTECTED_MIXED",
                "protection": "protected",
                "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
                "value": "secret",
            }
        )
    assert protected_exc.value.code == "plaintext_value_rejected"
    blind_box.assert_not_called()


def test_create_rejects_nested_plaintext_value_fields(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as standard_exc:
        api.create_vault_secret(
            {
                "name": "NESTED_STANDARD",
                "blind_box": {
                    "scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1",
                    "enc": "enc",
                    "ct": "ct",
                    "value": "secret",
                },
            }
        )
    assert standard_exc.value.code == "plaintext_value_rejected"

    with pytest.raises(api.VaultApiError) as protected_exc:
        api.create_vault_secret(
            {
                "name": "NESTED_PROTECTED",
                "protection": "protected",
                "sealed": {
                    "ciphertext": "ct",
                    "nonce": "n",
                    "wrap_meta": "wm",
                    "value": "secret",
                },
            }
        )
    assert protected_exc.value.code == "plaintext_value_rejected"
    blind_box.assert_not_called()


def test_create_with_policy_persists_allowed_hosts(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "GH_PAT",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "bearer"}},
        }
    )
    secret = api.get_vault_secrets()["secrets"][0]
    assert secret["policy"]["allowed_hosts"] == ["api.github.com"]


def test_update_secret_metadata_preserves_grants_and_internal_policy(monkeypatch):
    published = []
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda event_type, data: published.append((event_type, data)))
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)

    with api._vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="FETCH_TOKEN",
            protection="protected",
            sealed=_sealed("fetch"),
            tags=["old", "skill:legacy"],
            description="Old description",
            policy={"allowed_hosts": ["old.example.com"], "auth": {"type": "bearer"}},
        )
        request = vault_service.create_access_request(
            conn,
            source_selector={"tags": ["old"]},
            requester={"source": "test", "session_id": "ses_1"},
            delivery={"session_id": "ses_1", "mode": "run"},
        )
        grant = _grant_from_request(conn, request, session_id="ses_1")
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "FETCH_TOKEN")
            .values(policy=json.dumps({"allowed_hosts": ["old.example.com"], "auth": {"type": "bearer"}, "always_ask": True}))
        )
        original_updated_at = conn.execute(select(vault_secrets.c.updated_at).where(vault_secrets.c.name == "FETCH_TOKEN")).scalar_one()

    updated = api.update_vault_secret(
        "FETCH_TOKEN",
        {
            "description": "New description",
            "tags": ["prod", "skill:github"],
            "policy": {
                "always_ask": True,
                "allowed_hosts": ["old.example.com"],
                "auth": {"type": "bearer"},
            },
        },
    )

    assert updated["ok"] is True
    secret = updated["secret"]
    assert secret["description"] == "New description"
    assert secret["tags"] == ["prod", "skill:github"]
    assert secret["policy"] == {
        "always_ask": True,
        "allowed_hosts": ["old.example.com"],
        "auth": {"type": "bearer"},
    }
    assert ("vaults.updated", {"scope": "secret", "secret_name": "FETCH_TOKEN"}) in published
    with api._vault_engine().connect() as conn:
        grant_row = conn.execute(select(vault_service.vault_grants).where(vault_service.vault_grants.c.id == grant["id"])).mappings().one()
        assert grant_row["status"] == "active"
        assert conn.execute(select(vault_secrets.c.updated_at).where(vault_secrets.c.name == "FETCH_TOKEN")).scalar_one() == original_updated_at
        events = conn.execute(select(vault_audit.c.event, vault_audit.c.secret_name)).all()
        assert ("metadata-updated", "FETCH_TOKEN") in events


def test_update_secret_metadata_expires_grants_when_fetch_policy_changes(monkeypatch, avault_p2):
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda *args, **kwargs: None)
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)

    with api._vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="FETCH_POLICY_CHANGE",
            protection="protected",
            sealed=_sealed("fetch-policy"),
            policy={"allowed_hosts": ["old.example.com"], "auth": {"type": "bearer"}},
        )
        request = vault_service.create_access_request(
            conn,
            "FETCH_POLICY_CHANGE",
            purpose="fetch",
            requester={"source": "test", "session_id": "ses_1"},
            delivery={"session_id": "ses_1", "mode": "fetch"},
        )
        grant = _grant_from_request(conn, request, session_id="ses_1")

    updated = api.update_vault_secret(
        "FETCH_POLICY_CHANGE",
        {"policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "header", "name": "X-GitHub-Token"}}},
    )

    assert updated["ok"] is True
    assert updated["secret"]["policy"] == {
        "allowed_hosts": ["api.github.com"],
        "auth": {"type": "header", "name": "X-GitHub-Token"},
    }
    with api._vault_engine().connect() as conn:
        grant_row = conn.execute(select(vault_service.vault_grants).where(vault_service.vault_grants.c.id == grant["id"])).mappings().one()
        assert grant_row["status"] == "expired"
        events = conn.execute(select(vault_audit.c.event, vault_audit.c.grant_id)).all()
        assert ("grant-expired-policy-changed", grant["id"]) in events
    agent_release.assert_called_once_with(grant_id=grant["id"])


def test_update_secret_metadata_repairs_invalid_stored_fetch_policy(monkeypatch):
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda *args, **kwargs: None)
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="FETCH_REPAIR", sealed=_sealed("fetch-repair"))
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "FETCH_REPAIR")
            .values(policy=json.dumps({"allowed_hosts": ["api.github.com"], "auth": {"type": "header", "name": "Host"}}))
        )

    updated = api.update_vault_secret(
        "FETCH_REPAIR",
        {"policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "header", "name": "X-Api-Key"}}},
    )

    assert updated["ok"] is True
    assert updated["secret"]["policy"] == {
        "allowed_hosts": ["api.github.com"],
        "auth": {"type": "header", "name": "X-Api-Key"},
    }


def test_update_secret_metadata_rejects_unusable_fetch_auth_names(monkeypatch):
    monkeypatch.setattr("vibe.sse_broker.broker.publish", lambda *args, **kwargs: None)
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="FETCH_BAD_AUTH", sealed=_sealed("fetch-bad-auth"))

    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret(
            "FETCH_BAD_AUTH",
            {"policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "header", "name": "Host"}}},
        )

    assert exc.value.code == "invalid_metadata"


def test_update_secret_metadata_does_not_stale_pending_protected_grant(monkeypatch, avault_p2):
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "validate_avault_agent_pubkey", Mock())

    api.create_vault_secret(
        {
            "name": "PENDING_PROTECTED",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
            "tags": ["old"],
        }
    )
    requested = api.request_vault_access({"name": "PENDING_PROTECTED", "session_id": "ses_1"})
    request_id = requested["request"]["id"]
    grant_id = requested["request"]["card"]["grant_options"][0]["grant_id"]

    api.update_vault_secret("PENDING_PROTECTED", {"description": "Edited while pending", "tags": ["new"]})

    hydrated = api.get_vault_request(request_id, audience=vault_service.REQUEST_AUDIENCE_UI)
    unlock_material = hydrated["request"]["card"]["grant_options"][0]["unlock_material"]
    assert unlock_material[0]["name"] == "PENDING_PROTECTED"
    issued = _issued_agent_dek_payload(request_id)

    fulfilled = api.fulfill_vault_access_request(
        request_id,
        {
            "grant_id": grant_id,
            "session_id": "ses_1",
            **issued,
        },
    )

    assert fulfilled["ok"] is True
    assert fulfilled["grant"]["id"] == grant_id
    assert fulfilled["grant"]["member_snapshot"] == ["PENDING_PROTECTED"]


def test_update_secret_metadata_rejects_secret_material(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "NO_VALUE_PATCH",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.update_vault_secret("NO_VALUE_PATCH", {"tags": ["prod"], "value": "plaintext"})

    assert exc.value.code == "secret_material_rejected"


def test_create_with_links_persists_skill_link(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "GH_PAT",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "links": {"skills": ["github-pr-review"]},
        }
    )
    assert api.get_vault_secrets()["secrets"][0]["tags"] == ["skill:github-pr-review"]


def test_create_with_invalid_skill_link_returns_vault_api_error(monkeypatch):
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "GH_PAT",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "links": {"skills": ["github review"]},
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409
    seal.assert_not_called()


def test_get_vault_secrets_invalid_tag_filter_returns_vault_api_error():
    with pytest.raises(api.VaultApiError) as exc:
        api.get_vault_secrets(tag="bad tag")

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_get_vault_secrets_filters_value_free_metadata(monkeypatch):
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    api.create_vault_secret(
        {
            "name": "OPENAI_PROD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "description": "OpenAI production token",
            "tags": ["openai", "prod"],
            "policy": {"allowed_hosts": ["api.openai.com"], "auth": {"type": "bearer"}},
        }
    )
    api.create_vault_secret(
        {
            "name": "GITHUB_PROD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "description": "GitHub production token",
            "tags": ["github", "prod"],
        }
    )

    result = api.get_vault_secrets(tags=["prod"], query="openai", kind="static", protection="standard")

    assert [secret["name"] for secret in result["secrets"]] == ["OPENAI_PROD_KEY"]


def test_get_vault_tags_returns_tag_inventory(monkeypatch):
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    api.create_vault_secret(
        {
            "name": "OPENAI_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "tags": ["openai", "prod", "skill:model-work"],
        }
    )
    api.create_vault_secret(
        {
            "name": "OPENAI_DEV_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "tags": ["openai", "dev", "skill:model-work"],
        }
    )

    result = api.get_vault_tags(query="model", tag_type="skill")

    assert result["tags"] == [
        {"tag": "skill:model-work", "type": "skill", "secret_count": 2, "skill": "model-work"}
    ]


def test_get_provision_request_by_name_returns_pending_spec():
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["github"], "links": {"skills": ["github-pr-review"]}},
        )

    result = api.get_vault_provision_request_by_name("GH_TOKEN")

    assert result["request"]["id"] == req["id"]
    assert result["request"]["card"]["spec"]["tags"] == ["github", "skill:github-pr-review"]
    assert result["ambiguous"] is False


def test_provision_request_card_carries_session_id():
    # The chat surface scopes request cards by card.session_id; provision must set it from the
    # requester like access/sign do, or its card is invisible in the originating chat.
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(
            conn,
            "DEPLOY_TOKEN",
            requester={"source": "agent-cli", "session_id": "ses_abc123"},
        )
    assert req["card"]["session_id"] == "ses_abc123"
    result = api.get_vault_provision_request_by_name("DEPLOY_TOKEN")
    assert (result["request"]["card"] or {}).get("session_id") == "ses_abc123"


def test_vault_request_ui_payload_resolves_workbench_session_summary():
    with api._vault_engine().begin() as conn:
        _insert_workbench_session(conn, session_id="ses_vault_title", title="Deploy checklist")
        req = vault_service.create_provision_request(
            conn,
            "DEPLOY_TOKEN",
            requester={"source": "agent-cli", "session_id": "ses_vault_title"},
        )

    listed = api.get_vault_requests(session="ses_vault_title")

    assert listed["requests"][0]["session"] == {
        "id": "ses_vault_title",
        "title": "Deploy checklist",
        "label": "Deploy checklist",
        "platform": "avibe",
        "scope_kind": "project",
        "is_workbench": True,
    }
    agent_payload = api.get_vault_request(req["id"], audience=vault_service.REQUEST_AUDIENCE_AGENT)
    assert "session" not in agent_payload["request"]


def test_list_requests_scopes_by_session():
    # A session-scoped query returns only that session's requests (filtered before the limit).
    with api._vault_engine().begin() as conn:
        vault_service.create_provision_request(conn, "TOK_A", requester={"source": "cli", "session_id": "ses_A"})
        vault_service.create_provision_request(conn, "TOK_B", requester={"source": "cli", "session_id": "ses_B"})
    scoped = api.get_vault_requests(session="ses_A")
    assert {r["secret_name"] for r in scoped["requests"]} == {"TOK_A"}
    assert {r["secret_name"] for r in api.get_vault_requests()["requests"]} >= {"TOK_A", "TOK_B"}


def test_get_provision_request_returns_request_id_match():
    with api._vault_engine().begin() as conn:
        old_req = vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["old"]},
        )
        vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["new"]},
        )

    result = api.get_vault_provision_request(old_req["id"])

    assert result["request"]["id"] == old_req["id"]
    assert result["request"]["card"]["spec"]["tags"] == ["old"]


def test_get_provision_request_by_name_returns_none_when_ambiguous():
    with api._vault_engine().begin() as conn:
        vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["old"]})
        vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["new"]})

    result = api.get_vault_provision_request_by_name("GH_TOKEN")

    assert result["request"] is None
    assert result["ambiguous"] is True


def test_create_secret_with_provision_request_id_fulfills_sibling_requests(monkeypatch):
    seal = Mock(return_value=_sealed("api"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        old_req = vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["old"]})
        new_req = vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["new"]})

    created = api.create_vault_secret(
        {
            "name": "GH_TOKEN",
            "tags": ["new"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "provision_request_id": new_req["id"],
        }
    )

    assert created["secret"]["tags"] == ["new"]
    with api._vault_engine().connect() as conn:
        rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_requests.c.id, vault_requests.c.status).where(
                    vault_requests.c.id.in_([old_req["id"], new_req["id"]])
                )
            ).mappings()
        }
    assert rows == {old_req["id"]: "fulfilled", new_req["id"]: "fulfilled"}


def test_create_secret_with_fulfilled_provision_request_returns_secret_exists(monkeypatch):
    seal = Mock(side_effect=[_sealed("first"), _sealed("second")])
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(conn, "GH_TOKEN")

    payload = {
        "name": "GH_TOKEN",
        "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        "provision_request_id": req["id"],
    }
    created = api.create_vault_secret(payload)
    assert created["ok"] is True

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({**payload, "blind_box": {**payload["blind_box"], "enc": "enc2", "ct": "ct2"}})

    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_create_secret_exact_duplicate_still_returns_secret_exists(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    api.create_vault_secret(
        {
            "name": "openAiKey",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "openAiKey",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc2", "ct": "ct2"},
            }
        )
    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_secret_exists_response_commits_stale_pending_provision_cleanup(monkeypatch):
    seal = Mock(side_effect=[_sealed("first"), _sealed("second")])
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(conn, "GH_TOKEN")

    payload = {
        "name": "GH_TOKEN",
        "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        "provision_request_id": req["id"],
    }
    api.create_vault_secret(payload)
    with api._vault_engine().begin() as conn:
        stale = vault_service.create_provision_request(conn, "GH_TOKEN")
        conn.execute(vault_requests.update().where(vault_requests.c.id == stale["id"]).values(status="pending"))

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({**payload, "blind_box": {**payload["blind_box"], "enc": "enc2", "ct": "ct2"}})

    assert exc.value.code == "secret_exists"
    with api._vault_engine().connect() as conn:
        stale_status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == stale["id"])).scalar_one()
    assert stale_status == "fulfilled"


def test_secret_exists_response_publishes_when_fulfilling_stale_provisions(monkeypatch):
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    seal = Mock(side_effect=[_sealed("first"), _sealed("second")])
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(conn, "GH_TOKEN")

    payload = {
        "name": "GH_TOKEN",
        "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        "provision_request_id": req["id"],
    }
    api.create_vault_secret(payload)
    published.clear()
    with api._vault_engine().begin() as conn:
        stale = vault_service.create_provision_request(conn, "GH_TOKEN")
        conn.execute(vault_requests.update().where(vault_requests.c.id == stale["id"]).values(status="pending"))

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({**payload, "blind_box": {**payload["blind_box"], "enc": "enc2", "ct": "ct2"}})

    assert exc.value.code == "secret_exists"
    assert ("vaults.updated", {"scope": "secret", "request_status": "fulfilled", "secret_name": "GH_TOKEN"}) in published


def test_duplicate_name_conflict(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "DUP", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "DUP", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc2", "ct": "ct2"}})
    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_mixed_case_name_is_preserved_and_case_duplicate_rejected(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    created = api.create_vault_secret(
        {
            "name": "openAiKey",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    assert created["secret"]["name"] == "openAiKey"
    seal.assert_called_once_with(
        "openAiKey",
        {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "OpenAIKey",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc2", "ct": "ct2"},
            }
        )
    assert exc.value.code == "secret_name_case_conflict"
    assert exc.value.status == 409
    assert "openAiKey" in str(exc.value)


def test_case_conflict_is_rejected_before_standard_seal(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(side_effect=api.AvaultError("seal failed"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="openAiKey", sealed=_sealed("existing"))

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "OpenAIKey",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            }
        )

    assert exc.value.code == "secret_name_case_conflict"
    assert exc.value.status == 409
    seal.assert_not_called()


def test_create_secret_rejects_case_only_pending_provision(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        vault_service.create_provision_request(conn, "openAiKey")

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "OpenAIKey",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            }
        )

    assert exc.value.code == "secret_name_case_conflict"
    assert exc.value.status == 409
    assert "openAiKey" in str(exc.value)


def test_invalid_name_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "bad-name",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            }
        )
    assert exc.value.code == "invalid_name"
    seal.assert_not_called()


def test_rest_plaintext_value_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "NO_PLAINTEXT", "protection": "protected", "value": "secret"})
    assert exc.value.code == "plaintext_value_rejected"
    seal.assert_not_called()


def test_avault_failure_maps_to_api_error(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(side_effect=api.AvaultError("seal failed")))
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "FAIL_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    assert exc.value.code == "avault_failed"


def test_delete_missing_is_404():
    with pytest.raises(api.VaultApiError) as exc:
        api.delete_vault_secret("NOPE")
    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404


def test_delete_protected_secret_without_assertion_deletes(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {"name": "PROTECTED_DELETE", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )

    removed = api.delete_vault_secret("PROTECTED_DELETE")

    assert removed == {"ok": True, "removed": True, "name": "PROTECTED_DELETE"}
    with api._vault_engine().connect() as conn:
        with pytest.raises(vault_service.SecretNotFoundError):
            vault_service.get_secret_meta(conn, "PROTECTED_DELETE")


def test_delete_protected_secret_with_registered_factor_deletes_without_assertion(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    _establish_protected_secret_with_factor("PROTECTED_DELETE")

    removed = api.delete_vault_secret("PROTECTED_DELETE")

    assert removed == {"ok": True, "removed": True, "name": "PROTECTED_DELETE"}
    with api._vault_engine().connect() as conn:
        with pytest.raises(vault_service.SecretNotFoundError):
            vault_service.get_secret_meta(conn, "PROTECTED_DELETE")


def test_deleting_last_protected_secret_allows_fresh_vault_establishment(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    first, first_factor = _establish_protected_secret_with_factor("FIRST_PROTECTED")

    removed = api.delete_vault_secret("FIRST_PROTECTED")

    assert removed["removed"] is True
    with api._vault_engine().connect() as conn:
        old_factor = conn.execute(
            select(vault_auth_factors).where(vault_auth_factors.c.id == first_factor["id"])
        ).mappings().one()
        assert old_factor["disabled_at"] is not None

    second = WebAuthnTestCredential(credential_id=b"second-vault-first-factor")
    _credential, second_factor = _establish_protected_secret_with_factor("SECOND_PROTECTED", second)

    assert second_factor["credential_id"] == second.credential_id_b64


def test_forged_webauthn_factor_registration_still_requires_existing_factor(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    _establish_protected_secret_with_factor("PROTECTED_DELETE")
    forged = WebAuthnTestCredential(credential_id=b"forged-agent-factor")
    options = api.create_vault_authz_webauthn_options(origin=forged.origin)

    with pytest.raises(api.VaultApiError) as exc:
        api.register_vault_authz_webauthn_factor(
            forged.registration_payload(
                challenge_id=options["challenge_id"],
                challenge_b64=options["webauthn"]["challenge"],
            ),
            origin=forged.origin,
        )

    assert exc.value.code == "protected_auth_required"

    removed = api.delete_vault_secret("PROTECTED_DELETE")
    assert removed["removed"] is True


def test_registering_second_webauthn_factor_requires_existing_factor_assertion(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    first, first_factor = _establish_protected_secret_with_factor("PROTECTED_DELETE")
    second = WebAuthnTestCredential(credential_id=b"second-factor")
    options = api.create_vault_authz_webauthn_options(origin=second.origin)
    payload = second.registration_payload(
        challenge_id=options["challenge_id"],
        challenge_b64=options["webauthn"]["challenge"],
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.register_vault_authz_webauthn_factor(payload, origin=second.origin)
    assert exc.value.code == "protected_auth_required"

    payload["authz"] = first.assertion_authz(
        challenge_id=options["authorization"]["challenge_id"],
        factor_id=first_factor["id"],
        challenge_b64=options["authorization"]["webauthn"]["challenge"],
    )
    result = api.register_vault_authz_webauthn_factor(payload, origin=second.origin)

    assert result["factor"]["credential_id"] == second.credential_id_b64


def test_first_webauthn_factor_registration_only_happens_during_establishment(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    credential = WebAuthnTestCredential()
    options = api.create_vault_authz_webauthn_options(origin=credential.origin)
    payload = credential.registration_payload(
        challenge_id=options["challenge_id"],
        challenge_b64=options["webauthn"]["challenge"],
    )

    with pytest.raises(api.VaultApiError) as empty_exc:
        api.register_vault_authz_webauthn_factor(payload, origin=credential.origin)
    assert empty_exc.value.code == "protected_authz_setup_required"

    api.create_vault_secret(
        {"name": "FACTORLESS", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )
    late = WebAuthnTestCredential(credential_id=b"late-first-factor")
    late_options = api.create_vault_authz_webauthn_options(origin=late.origin)
    with pytest.raises(api.VaultApiError) as late_exc:
        api.register_vault_authz_webauthn_factor(
            late.registration_payload(
                challenge_id=late_options["challenge_id"],
                challenge_b64=late_options["webauthn"]["challenge"],
            ),
            origin=late.origin,
        )
    assert late_exc.value.code == "protected_authz_setup_required"


def test_factorless_protected_secret_deletes_normally(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {"name": "FACTORLESS", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )

    removed = api.delete_vault_secret("FACTORLESS")

    assert removed == {"ok": True, "removed": True, "name": "FACTORLESS"}
    with api._vault_engine().connect() as conn:
        with pytest.raises(vault_service.SecretNotFoundError):
            vault_service.get_secret_meta(conn, "FACTORLESS")


def test_audit_lists_events_without_values(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "AUD_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    api.delete_vault_secret("AUD_KEY")
    audit = api.get_vault_audit()
    events = {e["event"] for e in audit["events"]}
    assert {"created", "deleted"} <= events
    assert "supersecret-AUD" not in json.dumps(audit)


def test_create_protected_stores_browser_envelope_without_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("should-not-use"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    passkey_wrap_meta = {
        "v": 1,
        "copies": [
            {
                "kind": "passkey",
                "credential_id": "cred-1",
                "kdf": "webauthn-prf-hkdf-sha256",
                "prf_salt": "salt",
                "nonce": "copy-nonce",
                "wrapped": "wrapped-vmk",
            }
        ],
        "wrapped_dek": "dek",
        "dek_nonce": "dek-nonce",
    }
    created = api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "browser-ct", "nonce": "browser-n", "wrap_meta": passkey_wrap_meta},
            "public_meta": {"factor_hint": "passkey-first"},
        }
    )
    assert created["secret"]["protection"] == "protected"
    seal.assert_not_called()
    with api._vault_engine().connect() as conn:
        row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "PROTECTED_KEY")).mappings().one()
    assert row["ciphertext"] == "browser-ct"
    assert json.loads(row["wrap_meta"]) == passkey_wrap_meta


def test_vault_sandbox_root_metadata_persists_daemon_verification_key():
    first = api.get_vault_sandbox_root_metadata()
    second = api.get_vault_sandbox_root_metadata()

    key = first["root_metadata"]["daemon"]["verificationKeys"][0]
    assert key["alg"] == "ed25519"
    assert key["keyId"] == "vault-daemon-ed25519-v1"
    assert len(base64.b64decode(key["publicKey"])) == 32
    assert second["root_metadata"]["daemon"]["verificationKeys"][0]["publicKey"] == key["publicKey"]


def test_vault_sandbox_root_metadata_first_key_wins_under_concurrency(monkeypatch):
    original = api._new_vault_daemon_binding_key
    barrier = threading.Barrier(6)

    def racing_key() -> dict[str, str]:
        barrier.wait(timeout=5)
        return original()

    monkeypatch.setattr(api, "_new_vault_daemon_binding_key", racing_key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _index: api.get_vault_sandbox_root_metadata(), range(6)))

    keys = {
        result["root_metadata"]["daemon"]["verificationKeys"][0]["publicKey"]
        for result in results
    }
    assert len(keys) == 1


def test_create_vault_agent_bindings_batch_signs_value_free_contexts(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "agent-pk", "fingerprint": "agent-fp"}))
    with api._vault_engine().begin() as conn:
        _insert_workbench_session(conn, session_id="ses_binding", title="fix-ci-flake")
    api.create_vault_secret(
        {
            "name": "PROTECTED_BINDING",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d"}},
        }
    )
    with api._vault_engine().begin() as conn:
        request = vault_service.create_access_request(
            conn,
            "PROTECTED_BINDING",
            requester={"session_id": "ses_binding"},
            delivery={"session_id": "ses_binding"},
        )
    grant_id = request["card"]["grant_options"][0]["grant_id"]

    result = api.create_vault_agent_bindings_batch(
        {"request_id": request["id"], "grant_duration": 300}
    )

    assert result["ok"] is True
    assert result["agent_pubkey"] == {"public_key": "agent-pk", "fingerprint": "agent-fp"}
    assert result["grant_duration"] == 300
    assert result["ttl_seconds"] == 300
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["name"] == "PROTECTED_BINDING"
    assert set(item["approval"]) == {"nonce", "expires_at_unix"}
    context = _verified_signed_context(item["context"])
    assert context["v"] == 2
    assert context["purpose"] == "agent-deliver"
    assert context["requestId"].startswith("vab_")
    assert context["grantId"] == grant_id
    assert context["agent"]["fingerprint"] == "agent-fp"
    assert context["display"]["secrets"] == [{"name": "PROTECTED_BINDING", "kind": "static"}]
    assert context["display"]["sessionLabel"] == "fix-ci-flake"
    assert context["display"]["grantTtlSeconds"] == 300
    assert context["release"] == {
        "name": "PROTECTED_BINDING",
        "ttlSecs": 300,
        "approvalNonce": list(base64.b64decode(item["approval"]["nonce"])),
        "approvalExpiresAtUnix": item["approval"]["expires_at_unix"],
        "operationHash": api._agent_deliver_operation_hash("PROTECTED_BINDING", 300),
    }


def test_agent_bindings_batch_covers_all_protected_selector_members_one_time(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "agent-pk", "fingerprint": "agent-fp"}))
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    for name in ("PROTECTED_A", "PROTECTED_B"):
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["deploy"],
                "sealed": {"ciphertext": f"ct-{name}", "nonce": "n", "wrap_meta": "wm"},
            }
        )
    api.create_vault_secret(
        {
            "name": "STANDARD_ASK",
            "tags": ["deploy"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"always_ask": True},
        }
    )
    request = api.request_vault_access(
        {
            "source_selector": {"tags": ["deploy"]},
            "session_id": "ses_batch",
            "command": "deploy\nprod",
            "egress": "api.github.com",
        }
    )["request"]

    result = api.create_vault_agent_bindings_batch(
        {
            "request_id": request["id"],
            "grant_duration": "one-time",
        }
    )

    assert result["grant_duration"] == "one-time"
    assert result["ttl_seconds"] == 60
    assert [item["name"] for item in result["items"]] == ["PROTECTED_A", "PROTECTED_B"]
    assert request["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_A", "PROTECTED_B", "STANDARD_ASK"]
    contexts = [_verified_signed_context(item["context"]) for item in result["items"]]
    assert len({context["requestId"] for context in contexts}) == 2
    assert all(context["requestId"].startswith("vab_") for context in contexts)
    for context in contexts:
        assert context["display"]["secrets"] == [
            {"name": "PROTECTED_A", "kind": "static"},
            {"name": "PROTECTED_B", "kind": "static"},
            {"name": "STANDARD_ASK", "kind": "static"},
        ]
        assert context["display"]["command"] == "deploy prod"
        assert context["display"]["egress"] == "api.github.com"
        assert context["display"]["source"] == {"tags": ["deploy"]}
        assert context["display"]["grantTtlSeconds"] == 60
        assert context["release"]["ttlSecs"] == 60
        assert context["release"]["operationHash"] == api._agent_deliver_operation_hash(
            context["release"]["name"],
            60,
        )
    assert api.get_vault_settings()["settings"]["last_grant_ttl"] == "one-time"


def test_agent_bindings_batch_rejects_mixed_duration_fields(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "agent-pk", "fingerprint": "agent-fp"}))
    api.create_vault_secret(
        {
            "name": "PROTECTED_MIXED_TTL",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    with api._vault_engine().begin() as conn:
        request = vault_service.create_access_request(conn, "PROTECTED_MIXED_TTL")

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_agent_bindings_batch(
            {
                "request_id": request["id"],
                "grant_duration": 300,
                "ttl_seconds": 900,
            }
        )

    assert exc.value.code == "invalid_request"


def test_agent_bindings_batch_retries_replace_stale_approval_records(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "agent-pk", "fingerprint": "agent-fp"}))
    for name in ("PROTECTED_BATCH_RETRY_A", "PROTECTED_BATCH_RETRY_B"):
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["batch-retry"],
                "sealed": {"ciphertext": f"ct-{name}", "nonce": "n", "wrap_meta": "wm"},
            }
        )
    request = api.request_vault_access(
        {"source_selector": {"tags": ["batch-retry"]}, "session_id": "ses_batch_retry"}
    )["request"]

    for _index in range(5):
        api.create_vault_agent_bindings_batch({"request_id": request["id"], "grant_duration": 300})

    with api._vault_engine().connect() as conn:
        stored = vault_service.get_request(conn, request["id"], audience=vault_service.REQUEST_AUDIENCE_AGENT)
    records = stored["delivery"]["agent_binding_approvals"]
    assert len(records) == 1
    assert [item["name"] for item in records[0]["items"]] == ["PROTECTED_BATCH_RETRY_A", "PROTECTED_BATCH_RETRY_B"]


def test_legacy_agent_binding_records_all_protected_members(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "agent-pk", "fingerprint": "agent-fp"}))
    agent_grant = Mock(return_value={"granted": 25, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    names = [f"PROTECTED_BULK_{index:02d}" for index in range(25)]
    for name in names:
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["bulk"],
                "sealed": {"ciphertext": f"ct-{name}", "nonce": "n", "wrap_meta": "wm"},
            }
        )
    requested = api.request_vault_access({"source_selector": {"tags": ["bulk"]}, "session_id": "ses_bulk"})
    option = requested["request"]["card"]["grant_options"][0]

    deks = []
    agent_pubkey = None
    for name in option["member_snapshot"]:
        binding = api.create_vault_agent_binding(
            {
                "request_id": requested["request"]["id"],
                "grant_id": option["grant_id"],
                "name": name,
                "grant_duration": 300,
            }
        )
        agent_pubkey = binding["agent_pubkey"]
        deks.append({"name": name, "dek_blindbox": _agent_dek_blindbox(enc=f"enc-{name}"), "approval": binding["approval"]})

    created = api.create_vault_grant(
        {
            "session_id": "ses_bulk",
            "request_id": requested["request"]["id"],
            "agent_pubkey": agent_pubkey,
            "deks": deks,
        }
    )

    assert created["grant"]["member_snapshot"] == names
    assert agent_grant.call_count == 1
    with api._vault_engine().connect() as conn:
        request = vault_service.get_request(conn, requested["request"]["id"], audience=vault_service.REQUEST_AUDIENCE_AGENT)
    assert len(request["delivery"]["agent_binding_approvals"]) == 25


def test_legacy_agent_binding_retries_replace_stale_approval_records(monkeypatch, avault_p2):
    names = [f"PROTECTED_RETRY_{index}" for index in range(3)]
    for name in names:
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["retry"],
                "sealed": {"ciphertext": f"ct-{name}", "nonce": "n", "wrap_meta": "wm"},
            }
        )
    requested = api.request_vault_access({"source_selector": {"tags": ["retry"]}, "session_id": "ses_retry"})
    option = requested["request"]["card"]["grant_options"][0]

    for name in names:
        api.create_vault_agent_binding(
            {"request_id": requested["request"]["id"], "grant_id": option["grant_id"], "name": name, "grant_duration": 300}
        )
    for _index in range(5):
        api.create_vault_agent_binding(
            {
                "request_id": requested["request"]["id"],
                "grant_id": option["grant_id"],
                "name": names[0],
                "grant_duration": 300,
            }
        )

    with api._vault_engine().connect() as conn:
        request = vault_service.get_request(conn, requested["request"]["id"], audience=vault_service.REQUEST_AUDIENCE_AGENT)
    records = request["delivery"]["agent_binding_approvals"]
    record_names = [record["items"][0]["name"] for record in records]
    assert len(records) == len(names)
    assert record_names.count(names[0]) == 1
    assert set(record_names) == set(names)


def test_vault_settings_roundtrip_and_policy():
    defaults = api.get_vault_settings()
    assert defaults["settings"] == {
        "unlock_window_seconds": 600,
        "strict_approvals": False,
        "last_grant_ttl": 300,
    }
    assert defaults["policy"] == {
        "windowSeconds": 600,
        "strictApprovals": False,
        "parentValueSealAllowed": True,
    }

    saved = api.save_vault_settings(
        {
            "unlock_window_seconds": 1800,
            "strict_approvals": True,
            "last_grant_ttl": "one-time",
        }
    )

    assert saved["settings"]["last_grant_ttl"] == "one-time"
    assert saved["policy"]["windowSeconds"] == 1800
    assert saved["policy"]["strictApprovals"] is True
    assert api.get_vault_settings()["settings"] == saved["settings"]
    with pytest.raises(api.VaultApiError) as exc:
        api.save_vault_settings({"strict_approvals": "false"})
    assert exc.value.code == "invalid_request"


def test_create_vault_reveal_context_signs_named_protected_secret(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "PROTECTED_REVEAL",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    result = api.create_vault_reveal_context("PROTECTED_REVEAL", {"session_label": "Workbench - vaults"})

    assert result["ok"] is True
    context = _verified_signed_context(result["context"])
    assert context["v"] == 2
    assert context["purpose"] == "reveal"
    assert context["requestId"].startswith("vrl_")
    assert context["display"]["secrets"] == [{"name": "PROTECTED_REVEAL", "kind": "static"}]
    assert context["display"]["sessionLabel"] == "Workbench - vaults"
    assert result["envelope"] == {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}
    assert context["release"] == {
        "name": "PROTECTED_REVEAL",
        "envelopeHash": api._envelope_hash(result["envelope"]),
    }
    assert not ({"plaintext", "plain_text", "dek", "deks", "secret_unlock_material", "unlock_material"} & _payload_keys(result))
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "PROTECTED_REVEAL")
        audit_rows = conn.execute(
            select(vault_audit.c.request_id, vault_audit.c.delivery).where(
                vault_audit.c.secret_name == "PROTECTED_REVEAL",
                vault_audit.c.event == "revealed",
            )
        ).mappings().all()
    assert meta["use_count"] == 0
    assert meta["last_used_at"] is None
    assert audit_rows == []


def test_create_vault_reveal_context_rejects_protected_keypair(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "PROTECTED_REVEAL_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_reveal_context("PROTECTED_REVEAL_KEY")

    assert exc.value.code == "keypair_not_value_deliverable"


def test_protected_create_establishing_vmk_rejects_second_init(monkeypatch):
    _establish_protected_secret_with_factor("FIRST_PROTECTED")
    with api._vault_engine().connect() as conn:
        assert vault_service.get_secret_meta(conn, "FIRST_PROTECTED")["protection"] == "protected"

    # A second "establishing" create (concurrent first-time setup) must be rejected so
    # the vault key history can't split under a different VMK.
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "SECOND_PROTECTED",
                "protection": "protected",
                "sealed": {"ciphertext": "ct2", "nonce": "n2", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d2"}},
                "establishing_vmk": True,
            }
        )
    assert exc.value.code == "vault_already_initialized"
    assert exc.value.status == 409

    # Adding a secret under the already-established VMK (not establishing) still works.
    more = api.create_vault_secret(
        {
            "name": "THIRD_PROTECTED",
            "protection": "protected",
            "sealed": {"ciphertext": "ct3", "nonce": "n3", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d3"}},
        }
    )
    assert more["ok"] is True


def test_get_vault_vmk_returns_latest_protected_wrap_meta(monkeypatch):
    from unittest.mock import Mock

    assert api.get_vault_vmk() == {"ok": True, "exists": False, "wrap_meta": None}

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    assert api.get_vault_vmk() == {"ok": True, "exists": False, "wrap_meta": None}

    api.create_vault_secret(
        {
            "name": "PROTECTED_OLD",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-old", "nonce": "n-old", "wrap_meta": '{"vmk":"old"}'},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_NEW",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-new", "nonce": "n-new", "wrap_meta": '{"vmk":"new"}'},
        }
    )
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "PROTECTED_OLD")
            .values(updated_at="2026-01-01T00:00:00+00:00")
        )
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "PROTECTED_NEW")
            .values(updated_at="2026-01-02T00:00:00+00:00")
        )

    assert api.get_vault_vmk() == {"ok": True, "exists": True, "wrap_meta": '{"vmk":"new"}'}


def test_create_protected_rejects_non_string_envelope_fields(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("should-not-use"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "BAD_ENVELOPE",
                "protection": "protected",
                "sealed": {"ciphertext": None, "nonce": "browser-n", "wrap_meta": "wm"},
            }
        )

    assert exc.value.code == "invalid_envelope"
    seal.assert_not_called()
    assert api.get_vault_secrets()["secrets"] == []


def test_agent_pubkey_reports_upgrade_required_when_managed_pin_lacks_p2(monkeypatch):
    # An installed avault that predates the P2 surface (below AVAULT_P2_MIN_VERSION),
    # paired with a managed pin that also lacks P2.
    installed_pre_p2 = "0.1.2"
    assert not api._version_at_least(installed_pre_p2, api.AVAULT_P2_MIN_VERSION)
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": installed_pre_p2})
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: False)

    with pytest.raises(api.VaultApiError) as exc:
        api.get_vault_agent_pubkey()

    assert exc.value.code == "avault_upgrade_required"
    assert exc.value.status == 409
    assert api.AVAULT_VERSION in str(exc.value)
    assert api.AVAULT_P2_MIN_VERSION in str(exc.value)


def test_pubkey_wrapper_parses_avault(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(
        api,
        "_run_avault",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"public_key":"pk","fingerprint":"fp"}', stderr=b"")),
    )
    assert api.avault_pubkey() == {"public_key": "pk", "fingerprint": "fp"}


def test_avault_args_uses_file_store_only_on_linux_without_tpm(monkeypatch, tmp_path):
    missing_tpm = tmp_path / "missing-tpm0"
    existing_tpm = tmp_path / "tpmrm0"
    existing_tpm.touch()
    monkeypatch.setattr(api, "_AVAULT_LINUX_TPM_DEVICE_PATHS", (missing_tpm,))
    monkeypatch.setattr(api.platform, "system", lambda: "Linux")
    assert api._avault_args(["pubkey"]) == ["--store", "file", "pubkey"]

    monkeypatch.setattr(api, "_AVAULT_LINUX_TPM_DEVICE_PATHS", (existing_tpm,))
    assert api._avault_args(["pubkey"]) == ["pubkey"]

    monkeypatch.setattr(api.platform, "system", lambda: "Darwin")
    assert api._avault_args(["pubkey"]) == ["pubkey"]


def test_one_shot_avault_uses_file_store_for_linux_without_tpm(monkeypatch, tmp_path):
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(api, "_require_avault_path", lambda: "/tmp/avault")
    monkeypatch.setattr(api, "_command_env_for", lambda path: {"AVAULT_PATH": path})
    monkeypatch.setattr(api.platform, "system", lambda: "Linux")
    monkeypatch.setattr(api, "_AVAULT_LINUX_TPM_DEVICE_PATHS", (tmp_path / "missing-tpm0",))
    monkeypatch.setattr(api.subprocess, "run", fake_run)

    api._run_avault(["pubkey"], stdin=b"{}", timeout=3)

    assert seen["argv"] == ["/tmp/avault", "--store", "file", "pubkey"]
    kwargs = seen["kwargs"]
    assert kwargs["input"] == b"{}"
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 3
    assert kwargs["env"] == {"AVAULT_PATH": "/tmp/avault"}


def test_deliver_run_uses_file_store_for_linux_without_tpm(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    class FakeStdin:
        def write(self, payload):
            seen["stdin"] = payload

        def close(self):
            seen["stdin_closed"] = True

    class FakeProcess:
        stdin = FakeStdin()

        def wait(self):
            return 7

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(api, "_require_avault_path", lambda: "/tmp/avault")
    monkeypatch.setattr(api, "_command_env_for", lambda path: {"AVAULT_PATH": path})
    monkeypatch.setattr(api.platform, "system", lambda: "Linux")
    monkeypatch.setattr(api, "_AVAULT_LINUX_TPM_DEVICE_PATHS", (tmp_path / "missing-tpm0",))
    monkeypatch.setattr(api.subprocess, "Popen", fake_popen)

    result = api.avault_deliver_run(
        [{"name": "API_KEY", "env": "API_KEY", "envelope": _sealed("api")}],
        ["python3", "-c", "pass"],
    )

    assert result == {"exit_code": 7, "delivered": True}
    assert seen["argv"] == [
        "/tmp/avault",
        "--store",
        "file",
        "deliver",
        "run",
        "--",
        "python3",
        "-c",
        "pass",
    ]
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] == api.subprocess.PIPE
    assert kwargs["env"] == {"AVAULT_PATH": "/tmp/avault"}
    assert seen["stdin_closed"] is True
    assert json.loads(seen["stdin"]) == [
        {
            "name": "API_KEY",
            "env": "API_KEY",
            "envelope": {"ciphertext": "ct-api", "nonce": "n-api", "wrap_meta": "wm-api"},
        }
    ]


def test_blind_box_wrapper_relays_json_to_avault(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"ciphertext":"ct","nonce":"n","wrap_meta":"wm"}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    sealed = api.avault_seal_blind_box("API_KEY", {"scheme": "s", "enc": "e", "ct": "c"})
    assert sealed == Sealed(ciphertext="ct", nonce="n", wrap_meta="wm")
    args, kwargs = run.call_args
    assert args[0] == ["seal", "--name", "API_KEY", "--blind-box"]
    assert json.loads(kwargs["stdin"]) == {"scheme": "s", "enc": "e", "ct": "c"}


def test_blind_box_wrapper_single_object_strips_request_metadata(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"ciphertext":"ct","nonce":"n","wrap_meta":"wm"}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    api.avault_seal_blind_box({"name": "API_KEY", "scheme": "s", "enc": "e", "ct": "c"})
    assert json.loads(run.call_args.kwargs["stdin"]) == {"scheme": "s", "enc": "e", "ct": "c"}


def test_sign_wrapper_sends_name_and_envelope_to_avault(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"signature":"abcd","recovery_id":1}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    result = api.avault_sign(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    assert result == {"signature": "abcd", "recovery_id": 1}
    body = json.loads(run.call_args.kwargs["stdin"])
    assert body["name"] == "ETH_KEY"
    assert body["key_envelope"] == {"ciphertext": "ct-key", "nonce": "n-key", "wrap_meta": "wm-key"}
    assert "dek_blindbox" not in body
    assert "approval" not in body


def test_standard_keypair_signs_via_avault(monkeypatch):
    sign = Mock(return_value={"signature": "sig", "recovery_id": 1})
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})
    assert result == {"ok": True, "signature": {"signature": "sig", "recovery_id": 1}}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "ETH_KEY")
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None


def test_standard_keypair_sign_returns_signature_when_usage_audit_fails(monkeypatch):
    sign = Mock(return_value={"signature": "sig", "recovery_id": 1})
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    monkeypatch.setattr(vault_service, "record_signing_use", Mock(side_effect=RuntimeError("audit write failed")))

    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})

    assert result == {"ok": True, "signature": {"signature": "sig", "recovery_id": 1}}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")


def test_agent_access_request_and_standard_always_ask_grant_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    agent_grant = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"always_ask": True},
        }
    )

    requested = api.request_vault_access(
        {
            "name": "STANDARD_KEY",
            "session_id": "ses_1",
            "command": "python sync.py",
            "egress": "local child process",
        }
    )

    assert requested["ok"] is True
    assert requested["request"]["request_type"] == "access"
    assert requested["request"]["card"]["grant_options"]
    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
        }
    )
    assert created["grant"]["member_snapshot"] == ["STANDARD_KEY"]
    assert created["grant"]["delivery_ready"] is True
    assert created["grant"]["delivery_status"] == "standard_ready"
    assert created["grant"]["one_shot"] is True
    agent_grant.assert_not_called()
    with api._vault_engine().begin() as conn:
        grant_row = conn.execute(
            select(vault_service.vault_grants).where(vault_service.vault_grants.c.id == created["grant"]["id"])
        ).mappings().one()
        release_scopes = vault_service.agent_release_scopes_after_rows(conn, [dict(grant_row)])
    assert int(grant_row["agent_ready"] or 0) == 0
    assert release_scopes == []

    fetched = api.get_vault_request(requested["request"]["id"])
    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_agent_access_request_and_tag_standard_always_ask_grant_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    agent_grant = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    for name in ("ASK_A", "ASK_B"):
        api.create_vault_secret(
            {
                "name": name,
                "tags": ["deploy"],
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "policy": {"always_ask": True},
            }
        )
    api.create_vault_secret(
        {
            "name": "NORMAL_DEPLOY",
            "tags": ["deploy"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})

    assert requested["ok"] is True
    option = requested["request"]["card"]["grant_options"][0]
    assert option["source_selector"] == {"tags": ["deploy"]}
    assert option["member_snapshot"] == ["ASK_A", "ASK_B"]
    assert option["one_shot"] is True
    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
        }
    )
    assert created["grant"]["member_snapshot"] == ["ASK_A", "ASK_B"]
    assert created["grant"]["delivery_status"] == "standard_ready"
    assert created["grant"]["one_shot"] is True
    agent_grant.assert_not_called()


def test_agent_access_request_does_not_return_protected_tag_unlock_material(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "tags": ["crypto"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["crypto"]}, "session_id": "ses_1"})

    _assert_no_unlock_material(requested["request"]["card"])
    assert requested["request"]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_KEY"]


def test_agent_access_request_ignores_client_source_for_hydration(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "tags": ["crypto"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["crypto"]}, "session_id": "ses_1", "source": "ui"})

    assert requested["request"]["requester"]["source"] == "agent-cli"
    _assert_no_unlock_material(requested["request"]["card"])


def test_request_vault_access_malformed_selector_returns_vault_api_error():
    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"source_selector": {"tags": "deploy"}, "session_id": "ses_1"})

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_request_vault_access_reports_missing_selector_member_name():
    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"source_selector": {"env": ["MISSING_KEY"]}, "session_id": "ses_1"})

    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404
    assert "MISSING_KEY" in str(exc.value)


def test_agent_access_sibling_request_result_returns_covering_grant(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: dict(TEST_AGENT_PUBKEY))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    api.create_vault_secret(
        {
            "name": "A_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"},
        }
    )
    req_a = api.request_vault_access({"name": "A_KEY", "session_id": "ses_1"})
    req_b = api.request_vault_access({"name": "A_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(req_a["request"]["id"])
    created = api.create_vault_grant(
        {
            "request_id": req_a["request"]["id"],
            "session_id": "ses_1",
            **issued,
        }
    )

    fetched = api.get_vault_request(req_b["request"]["id"])

    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_agent_access_selector_sibling_request_result_returns_covering_grant(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: dict(TEST_AGENT_PUBKEY))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 2, "ttl_secs": 300}))
    for name in ("A_KEY", "B_KEY"):
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["deploy"],
                "sealed": {
                    "ciphertext": f"ct-{name.lower()}",
                    "nonce": f"n-{name.lower()}",
                    "wrap_meta": f"wm-{name.lower()}",
                },
            }
        )
    req_a = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})
    req_b = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(
        req_a["request"]["id"],
        blindboxes={
            "A_KEY": _agent_dek_blindbox("enc-a", "ct-a"),
            "B_KEY": _agent_dek_blindbox("enc-b", "ct-b"),
        },
    )
    created = api.create_vault_grant(
        {
            "request_id": req_a["request"]["id"],
            "session_id": "ses_1",
            **issued,
        }
    )

    fetched = api.get_vault_request(req_b["request"]["id"])

    assert req_b["request"]["secret_name"] is None
    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_get_vault_request_expires_timed_out_pending_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1", "request_ttl_seconds": 1})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    fetched = api.get_vault_request(requested["request"]["id"])

    assert fetched["request"]["status"] == "expired"
    assert "result" not in fetched
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_get_vault_requests_expires_and_commits_timed_out_pending_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1", "request_ttl_seconds": 1})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    listed = api.get_vault_requests(status="pending")

    assert listed["requests"] == []
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_get_vault_request_does_not_hydrate_protected_pending_unlock_material(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    with api._vault_engine().begin() as conn:
        request = vault_service.create_access_request(
            conn,
            "PROTECTED_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    fetched = api.get_vault_request(request["id"])

    assert fetched["request"]["status"] == "pending"
    _assert_no_unlock_material(fetched["request"]["card"])


def test_agent_access_request_accepts_protected_static(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("protected")))
    api.create_vault_secret({"name": "PROTECTED_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})

    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    assert requested["ok"] is True
    assert requested["request"]["secret_name"] == "PROTECTED_KEY"
    assert requested["request"]["card"]["protection"] == "protected"
    _assert_no_unlock_material(requested["request"])


def test_deny_vault_request_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    denied = api.deny_vault_request(requested["request"]["id"], {"reason": "not now", "requester": {"source": "test"}})

    assert denied["ok"] is True
    assert denied["request"]["status"] == "denied"
    with pytest.raises(api.VaultApiError) as exc:
        api.deny_vault_request(requested["request"]["id"])
    assert exc.value.code == "invalid_request"


def test_agent_sign_request_approved_via_avault_sign(monkeypatch):
    sign = Mock(return_value={"signature": "ab" * 64, "recovery_id": 1})
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    completed = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
        }
    )

    assert completed["ok"] is True
    assert completed["request"]["status"] == "approved"
    assert completed["request"]["delivery"]["signature"] == {"signature": "ab" * 64, "recovery_id": 1}
    assert api.get_vault_request(requested["request"]["id"])["result"]["signature"] == {"signature": "ab" * 64, "recovery_id": 1}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    with api._vault_engine().connect() as conn:
        audit_row = conn.execute(
            select(vault_audit.c.delivery).where(
                vault_audit.c.request_id == requested["request"]["id"],
                vault_audit.c.event == "signed",
            )
        ).scalar_one()
    assert json.loads(audit_row)["browser_signed"] is False


def test_vault_sign_standard_key_without_request_signs_headlessly(monkeypatch):
    signature = {"signature": "ab" * 64, "recovery_id": 1}
    sign = Mock(return_value=signature)
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})

    assert result == {"ok": True, "signature": signature}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    assert api.get_vault_requests()["requests"] == []


def test_vault_sign_standard_always_ask_without_request_requires_approval(monkeypatch):
    sign = Mock(return_value={"signature": "ab" * 64, "recovery_id": 1})
    notify = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    monkeypatch.setattr(api, "_notify_vault_request_created", notify)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "policy": {"always_ask": True},
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "session_id": "ses_1",
            "skill": "wallet-sign",
        }
    )

    assert result["ok"] is False
    assert result["code"] == "approval_required"
    request = result["request"]
    assert request["secret_name"] == "ETH_KEY"
    assert request["status"] == "pending"
    assert request["requester"]["session_id"] == "ses_1"
    assert request["requester"]["skill"] == "wallet-sign"
    assert request["delivery"]["session_id"] == "ses_1"
    assert request["delivery"]["skill"] == "wallet-sign"
    assert request["card"]["protection"] == "standard"
    assert "signature" not in result
    sign.assert_not_called()
    notify.assert_called_once_with(request)
    with api._vault_engine().connect() as conn:
        [row] = list(conn.execute(vault_requests.select()).mappings())
        assert row["id"] == request["id"]
        assert row["callback_status"] is None
        assert vault_service._request_session_id(dict(row)) == "ses_1"


def test_agent_sign_claims_request_before_avault_sign(monkeypatch):
    def sign_after_attempted_deny(*_args, **_kwargs):
        with api._vault_engine().begin() as conn:
            status = conn.execute(
                select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.secret_name == "ETH_KEY")
            ).scalar_one()
            assert status == "signing"
            request_id = conn.execute(
                select(vault_service.vault_requests.c.id).where(vault_service.vault_requests.c.secret_name == "ETH_KEY")
            ).scalar_one()
            with pytest.raises(vault_service.InvalidRequestError):
                vault_service.deny_request(conn, request_id)
        return {"signature": "ab" * 64, "recovery_id": 1}

    monkeypatch.setattr(api, "avault_sign", Mock(side_effect=sign_after_attempted_deny))
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    completed = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
        }
    )

    assert completed["request"]["status"] == "approved"


def test_agent_sign_request_accepts_protected_browser_signature(monkeypatch):
    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct-key", "nonce": "n-key", "wrap_meta": "wm-key"},
            "public_meta": public_meta,
        }
    )
    requested = api.request_vault_sign(
        {
            "name": "PROTECTED_ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "session_id": "ses_1",
            "signing_context": _signing_context(digest),
        }
    )

    completed = api.vault_sign(
        {
            "name": "PROTECTED_ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
            "signature": signature,
        }
    )

    assert requested["ok"] is True
    assert requested["request"]["card"]["protection"] == "protected"
    assert requested["request"]["card"]["secret_unlock_material"] == {
        "name": "PROTECTED_ETH_KEY",
        "kind": "keypair",
        "envelope": {"ciphertext": "ct-key", "nonce": "n-key", "wrap_meta": "wm-key"},
    }
    assert requested["request"]["delivery"]["signing_context"] == _signing_context(digest)
    assert requested["request"]["delivery"]["operation_context"] == requested["request"]["card"]["operation_context"]
    assert completed["ok"] is True
    assert completed["signature"] == signature
    assert api.get_vault_request(requested["request"]["id"])["result"] == {"type": "signature", "signature": signature}
    sign.assert_not_called()
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "PROTECTED_ETH_KEY")
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == requested["request"]["id"])).mappings().one()
    assert meta["use_count"] == 1
    assert row["callback_status"] == "pending"
    plan = vault_service.resolve_request_callback(dict(row))
    assert plan is not None
    assert plan.session_id == "ses_1"
    assert plan.message.strip()
    with api._vault_engine().connect() as conn:
        assert vault_service.request_callback_ready(conn, dict(row)) is True


def test_agent_sign_marks_claimed_request_failed_when_signature_validation_rejects(monkeypatch):
    monkeypatch.setattr(api, "avault_sign", Mock(return_value={"signature": "ab", "recovery_id": 1}))
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": "00" * 32,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    fetched = api.get_vault_request(requested["request"]["id"])
    assert fetched["request"]["status"] == "failed"
    assert fetched["request"]["delivery"]["failure"] == {"request_type": "sign", "reason": "signature_rejected"}


def test_vault_sign_rejects_non_keypair_secret(monkeypatch):
    from unittest.mock import Mock

    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "STATIC_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "STATIC_KEY", "digest": "00" * 32})
    assert exc.value.code == "not_signing_key"
    sign.assert_not_called()


def test_vault_sign_rejects_non_local_signer(monkeypatch):
    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "WALLET_KEY",
            "kind": "keypair",
            "signer_kind": "external",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "WALLET_KEY", "digest": "00" * 32})

    assert exc.value.code == "unsupported_signer_kind"
    sign.assert_not_called()


def test_create_and_revoke_grant_api(monkeypatch, avault_p2):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])
    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "ttl_seconds": 300,
            "request_id": req["id"],
            **issued,
        }
    )
    assert created["grant"]["runtime_member_count"] == 1
    assert created["grant"]["delivery_ready"] is True
    assert 1 <= agent_grant.call_args.kwargs["ttl_secs"] <= 300
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "GRANT_KEY",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": issued["deks"][0]["approval"],
        }
    ]
    grants = api.get_vault_grants()["grants"]
    assert grants[0]["id"] == created["grant"]["id"]
    revoked = api.revoke_vault_grant(created["grant"]["id"])
    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(grant_id=created["grant"]["id"])


def test_fulfill_access_request_relays_only_browser_dek_blindbox(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    validate_pubkey = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "validate_avault_agent_pubkey", validate_pubkey)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})
    grant_id = requested["request"]["card"]["grant_options"][0]["grant_id"]
    issued = _issued_agent_dek_payload(requested["request"]["id"])

    fulfilled = api.fulfill_vault_access_request(
        requested["request"]["id"],
        {
            "grant_id": grant_id,
            "session_id": "ses_1",
            "ttl_seconds": 300,
            **issued,
        },
    )

    assert fulfilled["ok"] is True
    assert fulfilled["result"]["type"] == "grant"
    assert fulfilled["grant"]["id"] == grant_id
    assert agent_grant.call_args.kwargs["grant_id"] == grant_id
    assert fulfilled["grant"]["delivery_ready"] is True
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "PROTECTED_KEY",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": issued["deks"][0]["approval"],
        }
    ]
    with api._vault_engine().connect() as conn:
        resolved = vault_service.resolve_secret_access(conn, "PROTECTED_KEY", session_id="ses_1", create_request=False)
    assert resolved["status"] == "agent_delivery_ready"
    validate_pubkey.assert_called_once_with(TEST_AGENT_PUBKEY)
    encoded = json.dumps({"fulfilled": fulfilled, "agent_deks": agent_grant.call_args.kwargs["deks"]})
    assert "raw-dek-must-not-cross-python-agent-boundary" not in encoded
    assert "plaintext-must-not-cross-python-agent-boundary" not in encoded


def test_fulfill_protected_always_ask_access_uses_one_shot_resident_agent_grant(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    validate_pubkey = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "validate_avault_agent_pubkey", validate_pubkey)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ASK",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_ASK", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"], grant_duration="one-time")

    fulfilled = api.fulfill_vault_access_request(
        requested["request"]["id"],
        {
            "session_id": "ses_1",
            **issued,
        },
    )

    assert requested["request"]["card"]["one_shot"] is True
    assert requested["request"]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_ASK"]
    assert fulfilled["grant"]["one_shot"] is True
    assert fulfilled["grant"]["source_selector"] == {"env": ["PROTECTED_ASK"]}
    assert fulfilled["grant"]["delivery_ready"] is True
    validate_pubkey.assert_called_once_with(TEST_AGENT_PUBKEY)
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "PROTECTED_ASK",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": issued["deks"][0]["approval"],
        }
    ]


def test_always_ask_grant_api_maps_stale_request_preflight_to_json_error(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "policy": {"always_ask": True},
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})
    api.deny_vault_request(requested["request"]["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_always_ask_grant_api_commits_expired_preflight_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "policy": {"always_ask": True},
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_fulfill_access_request_rejects_dek_or_plaintext_material(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks": [
                    {
                        "name": "PROTECTED_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                        "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        "value": "plaintext-must-not-cross-python-agent-boundary",
                    }
                ],
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_raw_material_in_deks_by_secret(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks_by_secret": {
                    "PROTECTED_KEY": {
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                        "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        "value": "plaintext-must-not-cross-python-agent-boundary",
                    }
                },
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_raw_material_nested_in_dek_blindbox(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks": [
                    {
                        "name": "PROTECTED_KEY",
                        "dek_blindbox": {
                            "scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1",
                            "enc": "enc",
                            "ct": "ct",
                            "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        },
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_protected_keypair_value_delivery(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"name": "PROTECTED_ETH_KEY", "session_id": "ses_1"})

    assert exc.value.code == "keypair_not_value_deliverable"
    agent_grant.assert_not_called()


def test_revoke_grant_releases_only_that_grant_id(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2)

    revoked = api.revoke_vault_grant(grant_1["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    with api._vault_engine().connect() as conn:
        assert vault_service.find_active_grant_for_secret(conn, "GRANT_KEY", session_id="ses_2")["id"] == grant_2["id"]


def test_consume_one_shot_releases_only_that_grant_id(monkeypatch):
    published = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    monkeypatch.setattr("vibe.internal_client.publish_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2)

    published.clear()
    api.consume_one_shot_grants([grant_1], reason="test")

    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    assert (
        "vaults.updated",
        {
            "scope": "grant",
            "request_id": grant_1["request_id"],
            "grant_id": grant_1["id"],
            "grant_status": "expired",
        },
    ) in published
    with api._vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_service.vault_grants.c.id, vault_service.vault_grants.c.status).where(
                    vault_service.vault_grants.c.id.in_([grant_1["id"], grant_2["id"]])
                )
            ).mappings()
        }
        active = vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="ses_2")
    assert statuses[grant_1["id"]] == "expired"
    assert statuses[grant_2["id"]] == "active"
    assert active["id"] == grant_2["id"]
    assert active["delivery_ready"] is True


def test_forced_agent_grant_cleanup_treats_reserved_grant_as_live(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2, session_id="ses_2")
        reserved = vault_service.resolve_secret_access(
            conn,
            "ASK_KEY",
            session_id="ses_2",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
            reserve_one_shot=True,
        )

    assert reserved["grant"]["id"] == grant_2["id"]
    assert reserved["grant"]["status"] == "reserved"

    api._cleanup_failed_agent_grant(
        engine=api._vault_engine(),
        grant=grant_1,
        reason="test",
        force_release_scope=True,
    )

    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    with api._vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_service.vault_grants.c.id, vault_service.vault_grants.c.status).where(
                    vault_service.vault_grants.c.id.in_([grant_1["id"], grant_2["id"]])
                )
            ).mappings()
        }
    assert statuses == {grant_1["id"]: "expired", grant_2["id"]: "reserved"}


def test_revoke_tag_grant_releases_its_grant_id(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "A_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_narrow = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"session_id": "ses_narrow"},
            delivery={"session_id": "ses_narrow"},
        )
        _grant_from_request(conn, req_narrow, session_id="ses_narrow")
        vault_service.create_secret(conn, name="B_KEY", protection="protected", tags=["crypto"], sealed=_sealed("b"))
        req_tag = vault_service.create_access_request(
            conn,
            source_selector={"tags": ["crypto"]},
            requester={"session_id": "ses_tag"},
            delivery={"session_id": "ses_tag"},
        )
        tag_grant = _grant_from_request(conn, req_tag, session_id="ses_tag")

    revoked = api.revoke_vault_grant(tag_grant["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(grant_id=tag_grant["id"])


def test_delete_protected_secret_releases_agent_scope(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    _establish_protected_secret_with_factor("GRANT_KEY")
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant = _grant_from_request(conn, req, session_id="ses_1")

    removed = api.delete_vault_secret("GRANT_KEY")

    assert removed["removed"] is True
    agent_release.assert_called_once_with(grant_id=grant["id"])


def test_release_agent_scope_fail_closed_resets_and_quarantines_socket(monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-release-", dir="/tmp")) / "s"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant = _grant_from_request(conn, req)

    class Manager:
        def __init__(self) -> None:
            self.socket_path = socket_path
            self.reset = Mock()

    manager = Manager()
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: manager)
    monkeypatch.setattr(api, "avault_agent_release", Mock(side_effect=api.AvaultError("timed out waiting for release")))
    try:
        api.release_vault_agent_scopes([{"grant_id": grant["id"]}], reason="test")
    finally:
        listener.close()

    manager.reset.assert_called_once()
    assert not socket_path.exists()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_grants.c.status).where(vault_service.vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"


def test_release_agent_scope_ignores_absent_agent(monkeypatch):
    manager = Mock()
    manager.socket_path = Path("/tmp/missing-avault.sock")
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: manager)
    monkeypatch.setattr(
        api,
        "avault_agent_release",
        Mock(side_effect=api.AvaultError("failed to connect to avault agent: [Errno 2] No such file or directory")),
    )

    api.release_vault_agent_scopes([{"grant_id": "vgr_missing"}], reason="test")

    manager.reset.assert_not_called()


def test_agent_grant_rejects_pubkey_mismatch(monkeypatch):
    agent_client = Mock()
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_client", lambda: agent_client)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})

    with pytest.raises(api.AvaultError, match="fingerprint mismatch"):
        api.avault_agent_grant(
            grant_id="vgr_grant",
            purpose="run",
            ttl_secs=300,
            deks=[
                {
                    "name": "GRANT_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
            expected_pubkey={"public_key": "old-pk", "fingerprint": "old-fp"},
        )

    agent_client.grant.assert_not_called()


def test_agent_grant_uses_deliver_custody_purpose(monkeypatch):
    agent_client = Mock()
    agent_client.grant.return_value = {"granted": 1}
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_client", lambda: agent_client)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})

    assert api.avault_agent_grant(
        grant_id="vgr_grant",
        purpose="run",
        ttl_secs=300,
        deks=[
            {
                "name": "GRANT_KEY",
                "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
            }
        ],
    ) == {"granted": 1}
    assert agent_client.grant.call_args.kwargs["purpose"] == "deliver"


def test_agent_deliver_run_reuses_resident_agent_socket(monkeypatch):
    seen_timeout = []
    seen_kwargs = []

    class FakeClient:
        def deliver_run(self, **kwargs):
            seen_kwargs.append(kwargs)
            return {"exit_code": 7}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_run(
        grant_id="vgr_grant",
        secrets=[{"name": "GRANT_KEY", "env": "GRANT_KEY", "envelope": _sealed(), "tier": "protected"}],
        command=["python3", "-c", "pass"],
    )

    assert result == {"exit_code": 7}
    assert seen_timeout == [None]
    assert seen_kwargs == [
        {
            "grant_id": "vgr_grant",
            "command": ["python3", "-c", "pass"],
            "secrets": [
                {
                    "name": "GRANT_KEY",
                    "env": "GRANT_KEY",
                    "envelope": {"ciphertext": "ct-1", "nonce": "n-1", "wrap_meta": "wm-1"},
                    "tier": "protected",
                }
            ],
            "context": None,
        }
    ]


def test_agent_deliver_run_treats_connect_failure_as_pre_handoff(monkeypatch):
    from vibe.avault_agent import AvaultAgentError

    class FakeManager:
        def client(self, *, timeout=None):
            raise AvaultAgentError("failed to connect to avault agent: [Errno 2] No such file or directory")

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    with pytest.raises(api.AvaultPreHandoffError):
        api.avault_agent_deliver_run(
            grant_id="vgr_grant",
            secrets=[{"name": "GRANT_KEY", "env": "GRANT_KEY", "envelope": _sealed()}],
            command=["python3", "-c", "pass"],
        )


def test_agent_deliver_fetch_uses_finite_timeout(monkeypatch):
    seen_timeout = []

    class FakeClient:
        def deliver_fetch(self, **kwargs):
            return {"status": 200, "headers": {}, "body": "ok"}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_fetch(
        grant_id="vgr_grant",
        name="GRANT_KEY",
        sealed=_sealed(),
        request={"method": "GET", "url": "https://example.com", "allowed_hosts": ["example.com"], "inject": {"type": "bearer"}},
    )

    assert result["status"] == 200
    assert seen_timeout == [api._AVAULT_FETCH_TIMEOUT_SECONDS]


def test_create_grant_api_requires_resident_agent_deks(monkeypatch, avault_p2):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
            }
        )

    assert exc.value.code == "invalid_grant"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
    assert status == "pending"


def test_create_grant_api_rejects_mismatched_deks_before_claiming_request(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                "deks": [
                    {
                        "name": "WRONG_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_create_grant_api_rejects_unissued_agent_dek_approval(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    requested = api.request_vault_access({"name": "GRANT_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"])
    tampered_deks = [dict(issued["deks"][0])]
    tampered_deks[0]["approval"] = {**tampered_deks[0]["approval"], "nonce": "tampered"}

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
                "agent_pubkey": issued["agent_pubkey"],
                "deks": tampered_deks,
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])).scalar_one()
    assert status == "pending"


def test_create_grant_api_rejects_binding_duration_mismatch(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    requested = api.request_vault_access({"name": "GRANT_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"], grant_duration="one-time")

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
                "grant_duration": 300,
                **issued,
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_grant.assert_not_called()


def test_create_grant_api_accepts_fingerprint_only_agent_pubkey(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    requested = api.request_vault_access({"name": "GRANT_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"])

    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
            "agent_pubkey": {"fingerprint": issued["agent_pubkey"]["fingerprint"]},
            "deks": issued["deks"],
        }
    )

    assert created["grant"]["status"] == "active"
    agent_grant.assert_called_once()
    assert agent_grant.call_args.kwargs["expected_pubkey"] == {"fingerprint": issued["agent_pubkey"]["fingerprint"]}


def test_create_grant_api_caps_grant_and_relay_by_binding_expiry(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 60})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    requested = api.request_vault_access({"name": "GRANT_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"])
    binding_expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    with api._vault_engine().begin() as conn:
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == requested["request"]["id"])).mappings().one()
        delivery = json.loads(row["delivery"])
        delivery["agent_binding_approvals"][0]["expires_at"] = api._isoformat_z(binding_expires_at)
        conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == requested["request"]["id"])
            .values(delivery=json.dumps(delivery))
        )

    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
            **issued,
        }
    )

    grant_expires_at = datetime.fromisoformat(created["grant"]["expires_at"]).astimezone(timezone.utc)
    assert grant_expires_at <= binding_expires_at
    assert 1 <= agent_grant.call_args.kwargs["ttl_secs"] <= 60
    assert agent_grant.call_args.kwargs["ttl_secs"] < 300


def test_create_grant_api_rejects_expired_binding_before_claiming_request(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    requested = api.request_vault_access({"name": "GRANT_KEY", "session_id": "ses_1"})
    issued = _issued_agent_dek_payload(requested["request"]["id"])
    binding_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with api._vault_engine().begin() as conn:
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == requested["request"]["id"])).mappings().one()
        delivery = json.loads(row["delivery"])
        delivery["agent_binding_approvals"][0]["expires_at"] = api._isoformat_z(binding_expires_at)
        conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == requested["request"]["id"])
            .values(delivery=json.dumps(delivery))
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
                **issued,
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_create_grant_api_relay_runs_after_grant_commit(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    def relay(**kwargs):
        with api._vault_engine().connect() as conn:
            status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
            grants = vault_service.list_grants(conn, status="active")
        assert status == "approved"
        assert len(grants) == 1
        assert grants[0]["member_snapshot"] == ["GRANT_KEY"]
        assert grants[0]["delivery_ready"] is False
        return {"granted": 1, "ttl_secs": kwargs["ttl_secs"]}

    monkeypatch.setattr(api, "avault_agent_grant", relay)
    issued = _issued_agent_dek_payload(req["id"])

    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": req["id"],
            **issued,
        }
    )

    assert created["grant"]["status"] == "active"


def test_create_grant_api_rejects_stale_agent_pubkey_before_claiming_request(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                "agent_pubkey": {"public_key": "old-pk", "fingerprint": "old-fp"},
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "avault_failed"
    assert "fingerprint mismatch" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_create_grant_api_expires_grant_when_agent_grant_fails(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(side_effect=api.AvaultError("grant is missing"))
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                **issued,
            }
        )

    assert exc.value.code == "avault_failed"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


def test_create_grant_api_rejects_partial_agent_cache(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 0, "ttl_secs": 300}))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                **issued,
            }
        )

    assert exc.value.code == "avault_failed"
    assert "cached fewer DEKs" in str(exc.value)
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


def test_grant_ttl_uses_remaining_lifetime():
    now = datetime.now(timezone.utc)
    ttl = api._grant_ttl_seconds(
        {
            "created_at": (now - timedelta(seconds=900)).isoformat(),
            "expires_at": (now + timedelta(seconds=120)).isoformat(),
        }
    )

    assert 1 <= ttl <= 120


def test_create_grant_api_releases_scope_when_mark_ready_fails(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    monkeypatch.setattr(vault_service, "mark_grant_agent_ready", Mock(side_effect=vault_service.GrantNotActiveError("vgr_raced")))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                **issued,
            }
        )

    assert exc.value.code == "invalid_grant"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


def test_create_grant_retry_reuses_expired_failed_grant_id(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(side_effect=[api.AvaultError("DEK blind-box open failed"), {"granted": 1, "ttl_secs": 300}])
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "avault_agent_release", Mock(return_value={"released": True}))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])
    payload = {
        "session_id": "ses_1",
        "request_id": req["id"],
        **issued,
    }

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(payload)
    assert exc.value.code == "avault_failed"

    created = api.create_vault_grant(payload)

    with api._vault_engine().connect() as conn:
        grants = vault_service.list_grants(conn, status=None)
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
    assert status == "approved"
    assert len(grants) == 1
    assert grants[0]["id"] == created["grant"]["id"]
    assert grants[0]["status"] == "active"
    assert grants[0]["delivery_ready"] is True
    assert agent_grant.call_count == 2


def test_create_grant_api_releases_failed_grant_id_without_touching_existing_grant(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(side_effect=api.AvaultError("grant is missing")))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        existing_req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_existing"},
            delivery={"session_id": "ses_existing"},
        )
        existing_grant = _grant_from_request(conn, existing_req, session_id="ses_existing")
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": req["id"],
                **issued,
            }
        )

    assert exc.value.code == "avault_failed"
    with api._vault_engine().connect() as conn:
        grants = {grant["id"]: grant for grant in vault_service.list_grants(conn, status=None)}
    assert grants[existing_grant["id"]]["status"] == "active"
    expired = [grant for grant in grants.values() if grant["id"] != existing_grant["id"]]
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"
    agent_release.assert_called_once_with(grant_id=expired[0]["id"])


def test_create_grant_api_preserves_unbound_session_choice(monkeypatch, avault_p2):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    issued = _issued_agent_dek_payload(req["id"])

    created = api.create_vault_grant(
        {
            "request_id": req["id"],
            "this_session_only": False,
            **issued,
        }
    )

    assert created["grant"]["session_id"] is None


def test_create_grant_api_binds_one_shot_to_request_session_when_unbound_requested(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("ask")))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"always_ask": True},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})

    created = api.create_vault_grant(
        {
            "request_id": requested["request"]["id"],
            "this_session_only": False,
        }
    )

    assert created["grant"]["one_shot"] is True
    assert created["grant"]["session_id"] == "ses_1"
    with api._vault_engine().begin() as conn:
        assert vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="other") is None
        assert vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="ses_1")["id"] == created["grant"]["id"]


def test_protected_sign_requires_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    digest = "00" * 32
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})
    assert exc.value.code == "missing_signing_context"

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )
    assert result["ok"] is False
    assert result["code"] == "browser_signature_required"
    assert result["request"]["card"]["request_type"] == "sign"
    assert result["request"]["card"]["grant_options"] == []
    assert result["request"]["card"]["secret_unlock_material"] == {
        "name": "ETH_KEY",
        "kind": "keypair",
        "envelope": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
    }
    assert result["request"]["delivery"]["signing_context"] == _signing_context(digest)
    signed_context = result["request"]["delivery"]["operation_context"]
    assert result["request"]["card"]["operation_context"] == signed_context
    context = _verified_signed_context(signed_context)
    assert context["v"] == 2
    assert context["purpose"] == "sign"
    assert context["requestId"] == result["request"]["id"]
    assert context["display"]["secrets"] == [{"name": "ETH_KEY", "kind": "keypair"}]


def test_protected_sign_agent_audience_keeps_unlock_material_omitted(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_AGENT_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct-agent", "nonce": "n-agent", "wrap_meta": "wm-agent"},
            "public_meta": public_meta,
        }
    )
    digest = "00" * 32

    result = api.vault_sign(
        {
            "name": "ETH_AGENT_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
            "requester": {"source": "agent-cli", "session_id": "ses_agent"},
            "delivery": {"session_id": "ses_agent"},
        }
    )

    assert result["ok"] is False
    assert result["code"] == "browser_signature_required"
    assert "secret_unlock_material" not in json.dumps(result["request"])
    assert "unlock_material" not in json.dumps(result["request"])
    signed_context = result["request"]["delivery"]["operation_context"]
    assert result["request"]["card"]["operation_context"] == signed_context
    context = _verified_signed_context(signed_context)
    assert context["purpose"] == "sign"
    assert context["display"]["secrets"] == [{"name": "ETH_AGENT_KEY", "kind": "keypair"}]


def test_protected_sign_request_requires_pinned_signing_public_key(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": "00" * 32,
                "scheme": "ecdsa-secp256k1-recoverable",
                "signing_context": _signing_context("00" * 32),
            }
        )

    assert exc.value.code == "invalid_request"
    assert "per-use signable" in str(exc.value)
    assert api.get_vault_requests()["requests"] == []


def test_protected_sign_rejects_unsupported_scheme_before_request(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "not-a-real-scheme"})

    assert exc.value.code == "invalid_request"
    assert api.get_vault_requests()["requests"] == []


def test_protected_sign_completion_requires_matching_request(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable", "signature": {"signature": "sig"}})
    assert exc.value.code == "missing_request_id"

    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )
    request_id = pending["request"]["id"]
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": "11" * 32,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": request_id,
                "signature": {"signature": "sig"},
            }
        )
    assert exc.value.code == "invalid_request"

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": request_id,
            "signature": signature,
        }
    )
    assert result["ok"] is True
    assert result["request"]["status"] == "approved"
    with api._vault_engine().connect() as conn:
        audit_row = conn.execute(
            select(vault_audit.c.delivery).where(vault_audit.c.request_id == request_id, vault_audit.c.event == "signed")
        ).scalar_one()
    assert json.loads(audit_row)["browser_signed"] is True


def test_protected_sign_completion_rejects_malformed_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    digest = "00" * 32
    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": {"signature": "not-hex", "recovery_id": 1},
            }
        )

    assert exc.value.code == "invalid_request"


def test_protected_sign_completion_rejects_signature_extra_fields(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": {**signature, "browser_signed": True, "private_key": "raw", "dek": "raw"},
            }
        )

    assert exc.value.code == "invalid_request"
    assert "unsupported fields" in str(exc.value)
    with api._vault_engine().connect() as conn:
        row = conn.execute(
            select(vault_service.vault_requests).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).mappings().one()
    assert row["status"] == "pending"
    assert "signature" not in json.loads(row["delivery"])


def test_protected_sign_completion_rejects_signature_that_does_not_verify(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, _valid_signature = _browser_ecdsa_signature_for_digest(digest, key_value=1)
    _other_public_meta, invalid_signature = _browser_ecdsa_signature_for_digest(digest, key_value=2)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": invalid_signature,
            }
        )

    assert exc.value.code == "invalid_request"
    assert "does not verify" in str(exc.value)
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).scalar_one()
    assert status == "pending"


def test_protected_sign_completion_rejects_wrong_recovery_id(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest, key_value=1)
    bad_signature = dict(signature)
    bad_signature["recovery_id"] = (int(signature["recovery_id"]) + 1) % 4
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "signing_context": _signing_context(digest),
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": bad_signature,
            }
        )

    assert exc.value.code == "invalid_request"
    assert "recovery_id" in str(exc.value)
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).scalar_one()
    assert status == "pending"


def test_protected_sign_completion_verifies_schnorr_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    signature = {
        "signature": "931a3386e9ec69fe1471ba85933640948c0296a79ce2d3801ad5a4d9353550aeb0a5e80358b68088bda70b46e6b77a640c1216826f96292e5799ba2bb7bf1342",
    }
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": {
                "signing_public_key": {
                    "curve": "secp256k1",
                    "public_key": "024e3b81af9c2234cad09d679ce6035ed1392347ce64ce405f5dcd36228a25de6e",
                }
            },
        }
    )
    pending = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "schnorr-secp256k1-bip340",
            "signing_context": _signing_context(digest),
        }
    )

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "schnorr-secp256k1-bip340",
            "request_id": pending["request"]["id"],
            "signature": signature,
        }
    )

    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


def test_signing_key_meta_exposes_addresses_not_public_key(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    # privkey = 1 → generator point G, compressed. Reference addresses are pinned in
    # tests/test_vault_addresses.py.
    created = api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": {
                "signing_public_key": {
                    "curve": "secp256k1",
                    "public_key": "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
                }
            },
        }
    )
    secret = created["secret"]
    # Decision: agents and the UI see derived addresses, never the raw public key.
    assert "signing_public_key" not in secret
    assert "signing_public_key" not in json.dumps(secret)
    addresses = secret["signing_addresses"]
    assert set(addresses) == {"eth", "btc_legacy", "btc_segwit", "btc_taproot"}
    assert addresses["eth"] == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    assert addresses["btc_segwit"] == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert addresses["btc_taproot"].startswith("bc1p")


def test_vault_sign_rejects_malformed_digest_before_request_or_avault(monkeypatch):
    from unittest.mock import Mock

    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": "not-hex", "scheme": "ecdsa-secp256k1-recoverable"})

    assert exc.value.code == "invalid_digest"
    sign.assert_not_called()
    assert api.get_vault_requests()["requests"] == []
