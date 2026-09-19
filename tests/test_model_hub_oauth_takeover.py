from __future__ import annotations

import json
import errno
import os
from pathlib import Path
from typing import Any

import pytest

import vibe.model_hub_runtime.adapter as runtime_adapter
from core.handlers.model_hub.adapter import OAuthCredentialRejectedError
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore


def _oauth_material(kind: str = "codex") -> dict[str, str]:
    return {
        "type": kind,
        "access_token": "access-token-fixture",
        "refresh_token": "refresh-token-fixture",
        "account_id": "account-fixture",
    }


def test_oauth_stage_binds_prefix_and_accepts_cpa_rotation(
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
    staged = json.loads((store.oauth_staging_dir / f"{ref}.json").read_text())
    prefix = str(store.credential_metadata(ref)["prefix"])
    assert staged["prefix"] == prefix

    auth_name, payload, activated_prefix, already_active = store.activate_oauth_auth_file(ref)
    assert auth_name == "avibe-migration-fixture.json"
    assert payload["type"] == "codex"
    assert activated_prefix == prefix
    assert payload["prefix"] == prefix
    assert already_active is False
    assert not (store.oauth_staging_dir / f"{ref}.json").exists()

    store.mark_oauth_engine_published(ref)
    store.mark_oauth_prefix_published(ref)
    live_path = store.auth_dir / auth_name
    rotated_payload = {
        "type": "codex",
        "access_token": "rotated-access-fixture",
        "refresh_token": "rotated-refresh-fixture",
        "prefix": prefix,
    }
    rotated = (json.dumps(rotated_payload, sort_keys=True) + "\n").encode()
    live_path.write_bytes(rotated)
    live_path.chmod(0o600)

    _, current, _, already_active = store.activate_oauth_auth_file(ref)
    assert already_active is True
    assert current == rotated_payload
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
        self.on_restart: Any = None
        self.on_inventory: Any = None

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
            if self.on_inventory is not None:
                callback, self.on_inventory = self.on_inventory, None
                callback()
            return {"files": list(self.files)}
        if method == "POST" and path == "/auth-files":
            raise AssertionError("OAuth activation must not re-upload a watched file")
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

    def restart_if_running(self) -> None:
        if self.client_value.on_restart is not None:
            self.client_value.on_restart()
            return
        auth_files = list(self.state_store.auth_dir.glob("*.json"))
        if len(auth_files) != 1:
            return
        payload = json.loads(auth_files[0].read_text())
        self.client_value.files = [
            {
                "id": auth_files[0].name,
                "name": auth_files[0].name,
                "provider": payload["type"],
                "auth_index": "0",
            }
        ]


@pytest.mark.asyncio
async def test_adapter_activation_is_idempotent_and_does_not_reupload(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    def reconcile_from_watched_file() -> None:
        auth_files = list(store.auth_dir.glob("*.json"))
        assert len(auth_files) == 1
        payload = json.loads(auth_files[0].read_text())
        client.files = [
            {
                "id": auth_files[0].name,
                "name": auth_files[0].name,
                "provider": payload["type"],
                "auth_index": "0",
            }
        ]
    client.on_restart = reconcile_from_watched_file
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
        ("GET", "/auth-files"),
    ]

    await adapter.activate_oauth_credential(ref)
    assert client.calls == first_calls + [("GET", "/auth-files")]

    auth_name = str(store.credential_metadata(ref)["auth_name"])
    live_path = store.auth_dir / auth_name
    prefix = str(store.credential_metadata(ref)["prefix"])
    rotated = (
        json.dumps(
            {
                "type": "codex",
                "refresh_token": "rotated-fixture",
                "prefix": prefix,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    live_path.write_bytes(rotated)
    live_path.chmod(0o600)
    await adapter.activate_oauth_credential(ref)
    assert client.calls == first_calls + [("GET", "/auth-files")] * 2
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
async def test_activation_retry_after_rotation_keeps_live_r1_and_never_reuploads(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    rotate_and_fail = True

    def reconcile_with_failure() -> None:
        nonlocal rotate_and_fail
        if rotate_and_fail:
            rotate_and_fail = False
            auth_path = next(store.auth_dir.glob("*.json"))
            prefix = str(store.credential_metadata(ref)["prefix"])
            auth_path.write_text(
                json.dumps(
                    {
                        "type": "codex",
                        "access_token": "r1-access",
                        "refresh_token": "r1-refresh",
                        "prefix": prefix,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            auth_path.chmod(0o600)
            raise RuntimeError("simulated reconcile uncertainty")
        auth_path = next(store.auth_dir.glob("*.json"))
        payload = json.loads(auth_path.read_text())
        client.files = [
            {
                "id": auth_path.name,
                "name": auth_path.name,
                "provider": payload["type"],
                "auth_index": "0",
            }
        ]

    client.on_restart = reconcile_with_failure
    adapter = CLIProxyEngineAdapter(
        supervisor=_FakeSupervisor(store, client),  # type: ignore[arg-type]
        state_store=store,
    )
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )

    with pytest.raises(EngineStateError, match="could not reconcile engine"):
        await adapter.activate_oauth_credential(ref)

    live_path = next(store.auth_dir.glob("*.json"))
    r1 = live_path.read_bytes()
    await adapter.activate_oauth_credential(ref)

    assert live_path.read_bytes() == r1
    assert all(path != "/auth-files" or method != "POST" for method, path in client.calls)
    assert store.credential_metadata(ref)["engine_published"] is True


@pytest.mark.asyncio
async def test_rotation_during_engine_binding_is_accepted_by_identity(
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
    auth_name, payload, _, _ = store.activate_oauth_auth_file(ref)
    client.files = [
        {
            "id": auth_name,
            "name": auth_name,
            "provider": payload["type"],
            "auth_index": "0",
        }
    ]

    def rotate_live_file() -> None:
        auth_path = next(store.auth_dir.glob("*.json"))
        prefix = str(store.credential_metadata(ref)["prefix"])
        auth_path.write_text(
            json.dumps(
                {
                    "type": "codex",
                    "access_token": "r1-access",
                    "refresh_token": "r1-refresh",
                    "prefix": prefix,
                },
                sort_keys=True,
            )
            + "\n"
        )
        auth_path.chmod(0o600)

    client.on_inventory = rotate_live_file
    await adapter.activate_oauth_credential(ref)
    assert json.loads((store.auth_dir / str(store.credential_metadata(ref)["auth_name"])).read_text())[
        "refresh_token"
    ] == "r1-refresh"


@pytest.mark.asyncio
async def test_startup_retry_reconciles_published_file_without_stale_upload(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "engine")
    client = _FakeEngineClient()
    supervisor = _FakeSupervisor(store, client)
    adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)  # type: ignore[arg-type]
    ref = await adapter.provision_oauth_credential(
        "src_fixture123",
        "openai",
        _oauth_material(),
    )
    auth_name, _, _, _ = store.activate_oauth_auth_file(ref)
    live_path = store.auth_dir / auth_name
    prefix = str(store.credential_metadata(ref)["prefix"])
    live_path.write_text(
        json.dumps(
            {
                "type": "codex",
                "access_token": "r1-access",
                "refresh_token": "r1-refresh",
                "prefix": prefix,
            },
            sort_keys=True,
        )
        + "\n"
    )
    live_path.chmod(0o600)

    def reconcile_from_watched_file() -> None:
        payload = json.loads(live_path.read_text())
        client.files = [
            {
                "id": auth_name,
                "name": auth_name,
                "provider": payload["type"],
                "auth_index": "0",
            }
        ]

    client.on_restart = reconcile_from_watched_file
    await adapter.activate_oauth_credential(ref)

    assert live_path.read_text() == (
        json.dumps(
            {
                "type": "codex",
                "access_token": "r1-access",
                "refresh_token": "r1-refresh",
                "prefix": prefix,
            },
            sort_keys=True,
        )
        + "\n"
    )
    assert ("POST", "/auth-files") not in client.calls


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
        match="OAuth credential validation failed",
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
            "OAuth credential validation failed",
        ),
        (
            401,
            '{"error":{"type":"invalid_token"}}',
            "OAuth credential validation failed",
        ),
        (
            401,
            '{"error":{"type":"token_expired"}}',
            "OAuth credential validation failed",
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


@pytest.mark.asyncio
async def test_validate_raises_canonical_rejection_only_for_cpa_refresh_state(
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
    await adapter.activate_oauth_credential(ref)
    client.files[0].update(
        {
            "status": "error",
            "status_message": "invalid_grant",
            "unavailable": True,
        }
    )

    with pytest.raises(OAuthCredentialRejectedError) as caught:
        await adapter.validate_oauth_credential(ref)
    assert "refresh-token-fixture" not in str(caught.value)


@pytest.mark.skipif(not getattr(os, "O_DIRECTORY", 0), reason="platform has no directory fsync")
def test_publication_directory_sync_error_cannot_be_reported_as_success(tmp_path, monkeypatch):
    def unavailable(_descriptor):
        raise OSError(errno.EIO, "fixture disk error")

    monkeypatch.setattr(os, "fsync", unavailable)
    with pytest.raises(OSError) as failure:
        EngineStateStore._fsync_directory(tmp_path)
    assert failure.value.errno == errno.EIO
