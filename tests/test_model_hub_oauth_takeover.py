from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import vibe.model_hub_runtime.adapter as runtime_adapter
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore


def _oauth_material(kind: str = "codex") -> dict[str, str]:
    return {
        "type": kind,
        "access_token": "access-token-fixture",
        "refresh_token": "refresh-token-fixture",
        "account_id": "account-fixture",
    }


def test_oauth_stage_stays_outside_auth_dir_and_refuses_rotation_overwrite(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = store.stage_oauth_credential(
        "src_fixture123",
        "openai",
        "avibe-migration-fixture.json",
        _oauth_material(),
    )

    assert not (store.auth_dir / "avibe-migration-fixture.json").exists()
    assert (store.oauth_staging_dir / f"{ref}.json").exists()

    auth_name, payload, _prefix, already_active = store.activate_oauth_auth_file(ref)
    assert auth_name == "avibe-migration-fixture.json"
    assert payload["type"] == "codex"
    assert already_active is False
    assert not (store.oauth_staging_dir / f"{ref}.json").exists()

    store.mark_oauth_engine_published(ref)
    store.mark_oauth_prefix_published(ref)
    live_path = store.auth_dir / auth_name
    rotated = b'{"type":"codex","refresh_token":"rotated-fixture"}\n'
    live_path.write_bytes(rotated)
    live_path.chmod(0o600)

    with pytest.raises(EngineStateError, match="changed after activation"):
        store.activate_oauth_auth_file(ref)
    assert live_path.read_bytes() == rotated


def test_matches_api_key_checks_target_and_keeps_unreadable_ref_fail_closed(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    ref = store.store_api_key(
        "密钥-fixture",
        vendor="anthropic",
        protocol="anthropic",
        base_url=None,
    )

    assert store.matches_api_key_credential(
        ref,
        "anthropic",
        "anthropic",
        "密钥-fixture",
        None,
    )
    assert not store.matches_api_key_credential(
        ref,
        "anthropic",
        "anthropic",
        "other-fixture",
        None,
    )
    assert not store.matches_api_key_credential(
        ref,
        "anthropic",
        "openai_chat",
        "密钥-fixture",
        None,
    )

    with pytest.raises(EngineStateError, match="unavailable"):
        store.matches_api_key_credential(
            "cred_" + "f" * 32,
            "anthropic",
            "anthropic",
            "密钥-fixture",
            None,
        )


class _FakeEngineClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.files: list[dict[str, Any]] = []
        self.api_call_payloads: list[dict[str, Any]] = []
        self.api_call_response = '{"object":"response"}'
        self.api_call_status_code = 200

    def management_request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        self.calls.append((method, path))
        if method == "GET" and path == "/auth-files":
            return {"files": list(self.files)}
        if method == "POST" and path == "/auth-files":
            assert payload is not None
            self.files.append(
                {
                    "id": "uploaded-fixture",
                    "name": str((query or {}).get("name") or ""),
                    "provider": payload["type"],
                    "auth_index": "0",
                }
            )
            return {"status": "ok"}
        if method == "POST" and path == "/api-call":
            assert payload is not None
            self.api_call_payloads.append(payload)
            return {
                "status_code": self.api_call_status_code,
                "body": self.api_call_response,
            }
        if method == "PATCH" and path == "/auth-files/fields":
            return {"status": "ok"}
        raise AssertionError((method, path, payload))


class _FakeSupervisor:
    def __init__(self, store: EngineStateStore, client: _FakeEngineClient) -> None:
        self.state_store = store
        self.client_value = client

    def client_if_running(self) -> _FakeEngineClient:
        return self.client_value

    def client(self) -> _FakeEngineClient:
        return self.client_value


@pytest.mark.asyncio
async def test_adapter_activation_is_idempotent_and_does_not_reupload(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )

    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    assert not list(store.auth_dir.glob("*.json"))
    await adapter.activate_oauth_credential(ref)
    first_calls = list(client.calls)
    assert first_calls == [
        ("GET", "/auth-files"),
        ("POST", "/auth-files"),
        ("PATCH", "/auth-files/fields"),
    ]

    await adapter.activate_oauth_credential(ref)
    assert client.calls == first_calls

    auth_name = str(store.credential_metadata(ref)["auth_name"])
    live_path = store.auth_dir / auth_name
    rotated = b'{"type":"codex","refresh_token":"rotated-fixture"}\n'
    live_path.write_bytes(rotated)
    live_path.chmod(0o600)
    with pytest.raises(EngineStateError, match="changed after activation"):
        await adapter.activate_oauth_credential(ref)
    assert client.calls == first_calls
    assert live_path.read_bytes() == rotated


@pytest.mark.asyncio
async def test_adapter_does_not_claim_same_name_for_a_different_provider(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )

    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    auth_name = str(store.credential_metadata(ref)["auth_name"])
    client.files = [
        {
            "id": auth_name,
            "name": auth_name,
            "provider": "claude",
            "auth_index": "0",
        }
    ]

    with pytest.raises(EngineStateError, match="auth record conflicts"):
        await adapter.activate_oauth_credential(ref)
    assert client.calls == [("GET", "/auth-files")]
    assert store.credential_metadata(ref)["engine_published"] is False


@pytest.mark.asyncio
async def test_validate_uses_credential_specific_probe_not_model_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    await adapter.activate_oauth_credential(ref)

    observed: list[tuple[str, str]] = []

    def probe(**kwargs: Any) -> runtime_adapter._ProtocolEvidence:
        observed.append((kwargs["auth"].auth_index, kwargs["protocol"]))
        return runtime_adapter._ProtocolEvidence(
            protocol=runtime_adapter._ProtocolProof.UNPROVEN,
            authentication=runtime_adapter._AuthenticationEvidence.ACCEPTED,
        )

    monkeypatch.setattr(runtime_adapter, "_probe_oauth_protocol_response", probe)
    await adapter.validate_oauth_credential(ref)

    assert observed == [("0", "openai_responses")]
    assert ("GET", "/auth-files/models") not in client.calls


@pytest.mark.asyncio
async def test_validate_rejects_staged_or_denied_grants_without_secret_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    with pytest.raises(EngineStateError, match="not active"):
        await adapter.validate_oauth_credential(ref)

    await adapter.activate_oauth_credential(ref)

    def reject(**_kwargs: Any) -> runtime_adapter._ProtocolEvidence:
        return runtime_adapter._ProtocolEvidence(
            protocol=runtime_adapter._ProtocolProof.UNPROVEN,
            authentication=runtime_adapter._AuthenticationEvidence.REJECTED,
        )

    monkeypatch.setattr(runtime_adapter, "_probe_oauth_protocol_response", reject)
    with pytest.raises(
        EngineStateError,
        match="oauth credential requires reauthentication",
    ) as caught:
        await adapter.validate_oauth_credential(ref)
    assert "refresh-token-fixture" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vendor", "material_type", "protocol", "url", "response"),
    (
        (
            "openai",
            "codex",
            "openai_responses",
            "https://chatgpt.com/backend-api/codex/responses",
            '{"object":"response"}',
        ),
        (
            "anthropic",
            "claude",
            "anthropic",
            "https://api.anthropic.com/v1/messages?beta=true",
            '{"type":"message"}',
        ),
    ),
)
async def test_validate_sends_model_free_credential_scoped_management_probe(
    tmp_path: Path,
    vendor: str,
    material_type: str,
    protocol: str,
    url: str,
    response: str,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    client.api_call_response = response
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        vendor,
        _oauth_material(material_type),
    )
    await adapter.activate_oauth_credential(ref)

    await adapter.validate_oauth_credential(ref)

    assert len(client.api_call_payloads) == 1
    probe = client.api_call_payloads[0]
    assert probe["auth_index"] == "0"
    assert probe["method"] == "POST"
    assert probe["url"] == url
    assert probe["header"]["Authorization"] == "Bearer $TOKEN$"
    assert "model" not in json.loads(probe["data"])
    assert ("GET", "/auth-files/models") not in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "response", "expected"),
    (
        (
            401,
            '{"error":{"type":"invalid_api_key"}}',
            "oauth credential requires reauthentication",
        ),
        (
            403,
            '{"error":{"type":"permission_error","message":"profile access denied"}}',
            "OAuth credential validation failed",
        ),
    ),
)
async def test_validate_distinguishes_definitive_invalid_grant_from_unknown_denial(
    tmp_path: Path,
    status: int,
    response: str,
    expected: str,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    client.api_call_status_code = status
    client.api_call_response = response
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    await adapter.activate_oauth_credential(ref)

    with pytest.raises(EngineStateError, match=f"^{expected}$"):
        await adapter.validate_oauth_credential(ref)
