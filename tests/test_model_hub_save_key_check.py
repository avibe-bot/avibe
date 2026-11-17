"""Save-time key evidence is independent of inventory and never an inference call.

The old save-first path only listed once and admitted rejected keys. Exercise the
service with the real runtime witness and HTTP model-list parser, including the
stored-reference path for edits; existing observation tests do not cover saves.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import ApiKeyModelsCheck
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import _assert_valid, _service, app, csrf_headers, ui_server
from tests.test_model_hub_unverified import _draft
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.state import EngineStateStore


@pytest.mark.parametrize("operation", ["create", "replace_key", "base_url"])
@pytest.mark.parametrize("outcome", [
    "confirmed", "rejected_401", "rejected_403", "rejected_401_slow_body", "public", "unreachable",
    "timeout", "keyless_timeout", "keyless_unreachable", "connection_refused", "empty_confirmed",
    "keyless_slow_401",
])
def test_save_time_models_witness(tmp_path, monkeypatch, operation, outcome):
    """Confirmed clears only pending; unknown saves; rejection is atomic."""
    service, store, adapter = _service(tmp_path)
    state = EngineStateStore(tmp_path / "engine-state")
    runtime = CLIProxyEngineAdapter(
        supervisor=Mock(lifecycle=None, _published_release=None), state_store=state,
    )
    for method in (
        "check_api_key_models", "provision_credential", "retarget_api_key_credential",
        "credential_auth_scheme", "revoke_credential", "revoke_api_key_credential",
    ):
        monkeypatch.setattr(adapter, method, AsyncMock(wraps=getattr(runtime, method)))
    # Tiny per-request budgets exercise real cancellation without slowing the suite.
    monkeypatch.setattr("core.handlers.model_hub.service._SOURCE_DISCOVERY_TIMEOUT_SECONDS", 0.1)

    async def scenario():
        requests = []
        mode = "public"
        expected_key = "unverified-test-key"

        async def models(request):
            requests.append((request.method, request.path, bool(request.headers.get("Authorization"))))
            has_key = bool(request.headers.get("Authorization"))
            if has_key:
                assert request.headers["Authorization"] == f"Bearer {expected_key}"
            if mode == "rejected_401_slow_body" or (mode == "keyless_slow_401" and not has_key):
                response = web.StreamResponse(status=401)
                await response.prepare(request)
                await asyncio.sleep(0.3)
                return response
            if mode.startswith("rejected"):
                return web.Response(status=int(mode.split("_")[1]))
            if mode == "unreachable" or (mode == "keyless_unreachable" and not has_key):
                return web.Response(status=503)
            if mode == "timeout" or (mode == "keyless_timeout" and not has_key):
                await asyncio.sleep(0.3)
            if mode in {"confirmed", "empty_confirmed"} and not has_key:
                return web.Response(status=401)
            return web.json_response({"data": [] if mode == "empty_confirmed" else [
                {"id": "listed-model", "supported_parameters": ["reasoning_effort"]},
            ]})

        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_get("/new/v1/models", models)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        root = f"http://127.0.0.1:{port}"
        try:
            before = None
            old_ref = None
            if operation != "create":
                initial = await service.create_source(_draft(base_url=f"{root}/v1"))
                before = store.config.to_payload()
                source_id = initial["source"]["id"]
                old_ref = initial["source"]["credential_ref"]
            requests.clear()
            adapter.provision_credential.reset_mock()
            mode = outcome
            if outcome == "connection_refused":
                await site.stop()
            if operation == "create":
                action = service.create_source(_draft(base_url=f"{root}/v1"))
            elif operation == "replace_key":
                expected_key = "replacement-test-key"
                action = service.replace_credential(source_id, {"key": expected_key})
            else:
                action = service.patch_source(source_id, {"base_url": f"{root}/new/v1"})
            if outcome.startswith("rejected"):
                with pytest.raises(ModelHubError) as error:
                    await action
                assert error.value.code == "discovery_failed"
                assert error.value.status == 422
                assert error.value.detail == "modelHub.errors.authentication_failed"
                assert len(requests) == 1
                if operation == "create":
                    assert not store.config.sources
                    adapter.provision_credential.assert_not_awaited()
                    # The reservation is released, so the corrected attempt can save.
                    requests.clear()
                    mode = "confirmed"
                    await service.create_source(_draft(base_url=f"{root}/v1"))
                    assert len(requests) == 2
                else:
                    assert store.config.to_payload() == before
                    assert state.credential_metadata_if_present(old_ref) is not None
                    revoke = adapter.revoke_api_key_credential if operation == "replace_key" else adapter.revoke_credential
                    # Rollback may use the generic revoke for either API-key path.
                    revoked = [call.args[0] for call in revoke.await_args_list]
                    if not revoked:
                        revoked = [call.args[0] for call in adapter.revoke_credential.await_args_list]
                    assert len(revoked) == 1 and revoked[0] != old_ref
                    assert state.credential_metadata_if_present(revoked[0]) is None
            else:
                result = await action
                source = result["source"]
                _assert_valid("source.schema.json", source)
                confirmed = outcome in {"confirmed", "empty_confirmed", "keyless_slow_401"}
                assert ("verification_pending" not in source) is confirmed
                assert (store.config.sources[0].verification_pending is None) is confirmed
                assert source["credential_generation"]
                if not confirmed:
                    assert source["verification_pending"] == source["credential_generation"]
                assert [m["id"] for m in source["models"]] == (
                    [] if outcome == "empty_confirmed" or (
                        operation == "create" and outcome in {"unreachable", "timeout", "connection_refused"}
                    ) else ["listed-model"]
                )
                if operation != "create":
                    assert source["credential_ref"] != old_ref
                    assert state.credential_metadata_if_present(old_ref) is None
                    if outcome in {"unreachable", "timeout", "connection_refused"}:
                        persisted = store.config.to_payload()["sources"][0]
                        assert persisted["models"] == before["sources"][0]["models"]
                        assert persisted["last_discovered_at"] == before["sources"][0]["last_discovered_at"]
                expected_requests = (
                    0 if outcome == "connection_refused" else
                    1 if outcome in {"unreachable", "timeout"} else 2
                )
                assert len(requests) == expected_requests
                if requests:
                    assert requests[0][2] is True
                if len(requests) == 2:
                    assert requests[1][2] is False
            assert all(method == "GET" for method, _, _ in requests)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_rejected_save_has_the_existing_typed_http_error(tmp_path, monkeypatch):
    """The HTTP serializer must retain the auth detail rather than generic discovery copy."""
    service, store, adapter = _service(tmp_path)
    adapter.check_api_key_models = AsyncMock(return_value=ApiKeyModelsCheck(authenticated=False))
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    origin = "http://127.0.0.1:15131"
    response = client.post(
        "/api/models/sources", json=_draft(),
        headers=csrf_headers(client, origin), base_url=origin,
    )
    assert response.status_code == 422
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"] == "discovery_failed"
    assert body["detail"] == "modelHub.errors.authentication_failed"
    assert "source" not in body
    assert not store.config.sources and adapter.credential_count == 0
    assert "unverified-test-key" not in str(body)
