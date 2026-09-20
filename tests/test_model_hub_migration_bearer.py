"""Static migration transport remains private, immutable and credential-gated."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from aiohttp import web

from config.v2_config import ModelHubSourceConfig
from core.handlers.model_hub.adapter import SourceBinding
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_api import _service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.api_key_vendors import validate_api_key_auth_scheme
from vibe.model_hub_runtime.client import probe_models
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, RuntimeSecrets


KEY = "fixture-bearer-value"
CUSTOM = "https://relay.example"


def _adapter(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    supervisor = SimpleNamespace(
        state_store=store, client_if_running=lambda: None, restart_if_running=lambda: None,
        invalidate_configs=store.clear_runtime_configs,
    )
    return CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)


def _binding(ref, url=CUSTOM):
    return SourceBinding(
        source_id="src_bearerfixture", vendor="anthropic", protocol="anthropic",
        base_url=url, credential_ref=ref, allowed_origins=(),
        model_ids=("claude-sonnet-4-6",),
    )


def _service_with_custody(tmp_path):
    runtime = _adapter(tmp_path)
    service, config, adapter = _service(tmp_path)
    for name in (
        "provision_credential", "provision_transient_credential", "credential_auth_scheme",
        "retarget_api_key_credential", "matches_api_key_credential",
        "observe_source", "discover_models", "revoke_credential", "revoke_api_key_credential", "sync_sources",
    ):
        setattr(adapter, name, getattr(runtime, name))
    return service, config, runtime


@asynccontextmanager
async def _relay(*, public_models=False, protocol_success=False, scheme="bearer"):
    requests = []
    accepted = {KEY}

    def supplied(request):
        requests.append((request.method, request.path, dict(request.headers)))
        assert request.headers.get("anthropic-version") == "2023-06-01"
        if scheme == "bearer":
            assert "x-api-key" not in request.headers
            return request.headers.get("Authorization", "").removeprefix("Bearer ")
        assert "Authorization" not in request.headers
        return request.headers.get("x-api-key", "")

    async def messages(request):
        key = supplied(request)
        body = await request.json()
        assert "model" not in body
        if key not in accepted:
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error", "message": "invalid token"}},
                status=401,
            )
        if protocol_success:
            return web.json_response({"type": "message", "content": []})
        return web.json_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "model: Field required"}},
            status=400,
        )

    async def models(request):
        key = supplied(request)
        if not public_models and key not in accepted:
            return web.json_response({"error": "authentication required"}, status=401)
        return web.json_response({"data": [{"id": "claude-sonnet-4-6"}]})

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        yield origin, requests, accepted
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("changes", [
    {"auth_scheme": "auto"}, {"auth_scheme": "x-api-key"}, {"auth_scheme": ""},
    {"auth_scheme": []}, {"vendor": "custom"}, {"vendor": "openai"},
    {"protocol": "openai_chat"}, {"protocol": "openai_responses"},
    {"base_url": None}, {"base_url": "https://api.anthropic.com"},
    {"base_url": "https://API.ANTHROPIC.COM:443/v1"},
    {"base_url": "https://ａｐｉ.anthropic.com"},
    {"base_url": "https://fixture@relay.example"}, {"base_url": "https://relay.example:wrong"},
    {"base_url": "https://relay.example:65536"}, {"base_url": "https://relay.example:0"},
    {"base_url": "https://%61pi.anthropic.com"}, {"base_url": "https://relay.example\\evil"},
    {"base_url": "https://relay.example\n"}, {"base_url": "file:///fixture"},
    {"secret": "before-sk-ant-oat-after"}, {"secret": ""},
    {"secret": "fixture\r\nAuthorization: extra"},
])
def test_explicit_scheme_refuses_unsupported_transport_without_secret_errors(changes):
    args = dict(vendor="anthropic", protocol="anthropic", base_url=CUSTOM, secret=KEY, auth_scheme="bearer")
    args.update(changes)
    with pytest.raises(ValueError, match="^unsupported API key authentication scheme$"):
        validate_api_key_auth_scheme(**args)


@pytest.mark.parametrize("url", [
    CUSTOM, "https://relay.example/api.anthropic.com",
    "https://api.anthropic.com.relay.example", "http://127.0.0.1:54321",
    "http://[::1]:54321", "https://relay.example:8443",
    "https://api.anthropic.com:8443", "http://api.anthropic.com",
    "https://api.anthropic.com.",
])
def test_custom_authority_is_parsed_and_transient_protocol_can_be_unknown(url):
    assert validate_api_key_auth_scheme("anthropic", None, url, KEY, "bearer") == "bearer"
    assert validate_api_key_auth_scheme("anthropic", "anthropic", url, KEY, "bearer") == "bearer"


def test_invalid_scheme_is_refused_before_reservation_or_write(tmp_path):
    store = EngineStateStore(tmp_path / "absent")
    reserved = []
    with pytest.raises(EngineStateError, match="unsupported API key authentication scheme"):
        store.store_api_key(
            KEY, vendor="anthropic", protocol="anthropic", base_url=CUSTOM,
            auth_scheme="other", on_reserved=reserved.append,
        )
    assert not reserved
    assert not store.root.exists()


def test_metadata_roundtrip_reuse_and_legacy_absence(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        explicit = await adapter.provision_credential(
            "anthropic", "anthropic", KEY, CUSTOM, auth_scheme="bearer",
        )
        legacy = await adapter.provision_credential("anthropic", "anthropic", KEY, CUSTOM)
        assert "auth_scheme" not in adapter.state_store.credential_metadata(legacy)
        assert await adapter.credential_auth_scheme(legacy) is None
        assert await adapter.credential_auth_scheme(explicit) == "bearer"
        assert not await adapter.credential_supports_refresh(explicit)
        for ref, scheme, matches in (
            (explicit, "bearer", True), (explicit, None, False),
            (legacy, "bearer", False), (legacy, None, True),
        ):
            assert await adapter.matches_api_key_credential(
                ref, "anthropic", "anthropic", KEY, CUSTOM, auth_scheme=scheme,
            ) is matches
        reopened = EngineStateStore(adapter.state_store.root)
        assert reopened.credential_metadata(explicit)["auth_scheme"] == "bearer"
        assert reopened.read_api_key(legacy) == KEY
        bindings = [_binding(explicit)]
        reopened.sync_sources(bindings)
        assert "auth_scheme" not in json.loads((reopened.root / "sources.json").read_text())["sources"][0]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"auth_scheme": "unknown"}, {"vendor": "openai"}, {"protocol": "openai_chat"},
    {"base_url": "https://api.anthropic.com"}, {"value": "sk-ant-oat-fixture"},
    {"value": None}, {"protocol": None},
])
def test_malformed_explicit_metadata_cannot_supply_transport_or_readiness(tmp_path, change):
    store = EngineStateStore(tmp_path / "engine")
    ref = store.store_api_key(KEY, vendor="anthropic", protocol="anthropic", base_url=CUSTOM, auth_scheme="bearer")
    path = store.root / "credentials" / f"{ref}.json"
    original = json.loads(path.read_text())
    path.write_text(json.dumps({**original, **change}))
    with pytest.raises(EngineStateError, match="unsupported API key authentication scheme"):
        store.credential_metadata(ref)
    with pytest.raises(EngineStateError):
        store.sync_sources([_binding(ref)])
    assert not store.has_current_source_credential(
        ref, source_id="src_bearerfixture", kind="api_key",
        vendor="anthropic", protocol="anthropic", base_url=CUSTOM,
    )


def test_retarget_keeps_scheme_and_refuses_official_before_new_custody(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        ref = await adapter.provision_credential("anthropic", "anthropic", KEY, CUSTOM, auth_scheme="bearer")
        before = adapter.state_store.credential_metadata(ref)
        replacement = await adapter.retarget_api_key_credential(
            ref, "anthropic", "anthropic", "https://another-relay.example",
        )
        assert replacement != ref
        assert await adapter.credential_auth_scheme(replacement) == "bearer"
        assert adapter.state_store.credential_metadata(ref) == before
        paths = set((adapter.state_store.root / "credentials").iterdir())
        with pytest.raises(EngineStateError):
            await adapter.retarget_api_key_credential(ref, "anthropic", "anthropic", "https://api.anthropic.com")
        assert set((adapter.state_store.root / "credentials").iterdir()) == paths

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol_success,public_models,key,outcome", [
    (False, False, KEY, "observed"),
    (False, False, "fixture-wrong-key", "authentication_failed"),
    (False, True, KEY, "adapter_error"),
    (True, False, KEY, "observed"),
])
def test_real_http_observation_discovery_and_credentialless_contrast(
    tmp_path, protocol_success, public_models, key, outcome,
):
    async def scenario():
        adapter = _adapter(tmp_path)
        async with _relay(protocol_success=protocol_success, public_models=public_models) as (origin, requests, _):
            ref = await adapter.provision_transient_credential("anthropic", key, origin, auth_scheme="bearer")
            with pytest.raises(ValueError, match="unsupported API key authentication scheme"):
                await adapter.observe_source("anthropic", origin, ref, ("openai_chat", "anthropic"))
            assert requests == []
            result = await adapter.observe_source("anthropic", origin, ref, ("anthropic",))
            assert result.outcome.value == outcome
            assert (result.authenticated is True) == (outcome == "observed")
            if outcome == "observed":
                models = await adapter.discover_models("anthropic", "anthropic", origin, ref)
                assert [model.id for model in models] == ["claude-sonnet-4-6"]
            assert requests[0][:2] == ("POST", "/v1/messages")
            assert requests[0][2]["Authorization"] == f"Bearer {key}"
            if not protocol_success and key == KEY:
                assert requests[1][2]["Authorization"] == f"Bearer {KEY}"
                assert "Authorization" not in requests[2][2]
            for _, _, headers in requests:
                assert "x-api-key" not in {name.lower() for name in headers}

    asyncio.run(scenario())


@pytest.mark.parametrize("public_models,key", [(False, KEY), (True, KEY), (False, "fixture-wrong-key")])
def test_private_service_observation_provisions_and_cleans_same_scheme(tmp_path, public_models, key):
    async def scenario():
        service, _, runtime = _service_with_custody(tmp_path)
        reserved = []
        async with _relay(public_models=public_models) as (origin, requests, _):
            pending = service._require_proven_source_payload(
                {"vendor": "anthropic", "base_url": origin, "key": key},
                auth_scheme="bearer", on_reserved=reserved.append,
            )
            if not public_models and key == KEY:
                assert (await pending).authenticated is True
            else:
                with pytest.raises(ModelHubError):
                    await pending
            assert len(reserved) == 1
            assert runtime.state_store.credential_metadata_if_present(reserved[0]) is None
            assert len(requests) == (3 if key == KEY else 1)

    asyncio.run(scenario())


def test_public_payloads_cannot_select_private_scheme(tmp_path):
    async def scenario():
        service, _, adapter = _service(tmp_path)
        adapter.provision_transient_credential = AsyncMock(side_effect=AssertionError("unexpected custody"))
        payload = {"vendor": "anthropic", "base_url": CUSTOM, "key": KEY, "auth_scheme": "bearer"}
        with pytest.raises(ModelHubError):
            await service._observe_source_payload(payload)
        with pytest.raises(ModelHubError):
            await service.create_source({"kind": "api_key", **payload})
        adapter.provision_transient_credential.assert_not_awaited()

    asyncio.run(scenario())


def test_legacy_callers_keep_original_header_and_optional_keyword_absence(tmp_path):
    async def scenario():
        async with _relay(scheme="legacy") as (origin, requests, _):
            result = await probe_models(vendor="anthropic", protocol="anthropic", base_url=origin, secret=KEY)
            assert result[0].id == "claude-sonnet-4-6"
            assert requests[0][2]["x-api-key"] == KEY
        service, _, adapter = _service(tmp_path)
        # The existing fake deliberately has no auth_scheme keyword.
        result = await service._require_proven_source_payload({"vendor": "anthropic", "key": KEY})
        assert result.authenticated is True
        assert adapter.revoked

    asyncio.run(scenario())


@pytest.mark.parametrize("scheme", [None, "bearer"])
def test_service_replace_preserves_private_scheme_through_actual_discovery(tmp_path, scheme):
    async def scenario():
        service, config, runtime = _service_with_custody(tmp_path)
        async with _relay(scheme=scheme or "legacy") as (origin, requests, accepted):
            ref = await runtime.provision_credential("anthropic", "anthropic", KEY, origin, auth_scheme=scheme)
            source = ModelHubSourceConfig.from_payload({
                "id": "src_bearerfixture", "kind": "api_key", "vendor": "anthropic",
                "display_name": "Fixture", "protocol": "anthropic", "base_url": origin,
                "supply_channel": "hub", "billing": "metered",
                "state": {"status": "standby"}, "models": [], "credential_ref": ref,
            })
            config.config.sources.append(source)
            runtime.state_store.sync_sources([_binding(ref, origin)])
            if scheme:
                old_config = config.config.to_payload()
                old_metadata = runtime.state_store.credential_metadata(ref)
                with pytest.raises(ModelHubError):
                    await service.replace_credential(source.id, {"key": "sk-ant-oat-fixture"})
                assert config.config.to_payload() == old_config
                assert runtime.state_store.credential_metadata(ref) == old_metadata
                assert requests == []
            accepted.add("fixture-replacement")
            answer = await service.replace_credential(source.id, {"key": "fixture-replacement"})
            replacement = answer["source"]["credential_ref"]
            assert replacement != ref
            assert await runtime.credential_auth_scheme(replacement) == scheme
            assert runtime.state_store.credential_metadata_if_present(ref) is None
            assert "auth_scheme" not in answer["source"]
            assert requests[0][2].get("Authorization" if scheme else "x-api-key") == (
                "Bearer fixture-replacement" if scheme else "fixture-replacement"
            )

    asyncio.run(scenario())


def test_engine_config_keeps_supported_cpa_transport_and_refuses_reinterpretation(tmp_path):
    store = EngineStateStore(tmp_path / "engine")
    ref = store.store_api_key(KEY, vendor="anthropic", protocol="anthropic", base_url=CUSTOM, auth_scheme="bearer")
    source = store.sync_sources([_binding(ref)])[0]
    path = tmp_path / "instance" / "config.yaml"

    def write(record):
        write_engine_config(
            path, host="127.0.0.1", port=12345, auth_dir=store.auth_dir,
            runtime_secrets=RuntimeSecrets("fixture-management", "fixture-gateway"),
            sources=[record], state_store=store,
        )

    write(source)
    entry = yaml.safe_load(path.read_text())["claude-api-key"][0]
    assert entry["api-key"] == KEY
    assert entry["base-url"] == CUSTOM
    assert "headers" not in entry
    before = path.read_bytes()
    with pytest.raises(EngineStateError):
        write(replace(source, base_url="https://api.anthropic.com"))
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
