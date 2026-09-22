"""Migration admission preserves native auth semantics; fixtures never use accounts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from config.v2_config import ModelHubSourceConfig, normalize_model_hub_base_url
from core.handlers.model_hub import migration
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
)
from tests.test_model_hub_migration_bearer import _adapter
from vibe.model_hub_runtime import api_key_vendors


KEY = "fixture-static-key"
CUSTOM = "https://fixture-relay.example"
OFFICIAL = "https://api.anthropic.com"
BLOCKED = "settings.models.migration.blocked.transport"
ENTRIES = (
    "claude-settings", "claude-backup", "claude-project", "claude-project-local",
    "shell", "opencode-config", "opencode-auth", "opencode-env",
    "opencode-custom", "opencode-catalog", "opencode-shadow", "legacy",
)


@pytest.fixture
def native(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setattr(migration, "read_native_oauth", lambda *args, **kwargs: None)
    return home


def _seed(home, entry, *, base_url=CUSTOM, secret=KEY, bearer=False):
    paths = []
    roots = ()
    legacy = None

    def write(path, payload):
        _write(path, payload if isinstance(payload, str) else json.dumps(payload))
        paths.append(path)

    if entry.startswith("claude"):
        root = home
        if "project" in entry:
            root = home / "project"
            roots = (root,)
        filename = "settings.local.json" if entry.endswith("local") else "settings.json"
        if entry == "claude-backup":
            filename = ".avibe-oauth-settings-env-backup.json"
        env = {"ANTHROPIC_AUTH_TOKEN" if bearer else "ANTHROPIC_API_KEY": secret}
        if base_url is not None:
            env["ANTHROPIC_BASE_URL"] = base_url
        write(root / ".claude" / filename, {"env": env, "unrelated": "保留"})
    elif entry == "shell":
        name = "ANTHROPIC_AUTH_TOKEN" if bearer else "ANTHROPIC_API_KEY"
        write(home / ".bashrc", (
            "# 保留终端设置\r\n"
            f"export {name}='{secret}'\r\n"
            + (f"export ANTHROPIC_BASE_URL='{base_url}'\r\n" if base_url is not None else "")
        ))
    elif entry == "legacy":
        legacy = {"claude": {"api_key": secret, "base_url": base_url}}
    else:
        provider = "custom" if entry in {"opencode-custom", "opencode-catalog"} else "anthropic"
        options = {"apiKey": secret}
        if base_url is not None:
            options["baseURL"] = base_url
        definition = {"options": options}
        if entry == "opencode-env":
            write(home / ".profile", f"export FIXTURE_API_KEY='{secret}'\n")
            options["apiKey"] = "{env:FIXTURE_API_KEY}"
        if entry == "opencode-custom":
            definition["npm"] = "@ai-sdk/anthropic"
        if entry == "opencode-catalog":
            options.pop("baseURL", None)
            write(home / ".cache/opencode/models.json", {
                provider: {"npm": "@ai-sdk/anthropic", "api": base_url},
            })
        if entry in {"opencode-auth", "opencode-shadow"}:
            if entry == "opencode-auth":
                options.pop("apiKey")
            write(home / ".local/share/opencode/auth.json", {
                provider: {"type": "api", "key": secret if entry == "opencode-auth" else secret + "-shadow"},
            })
        write(home / ".config/opencode/opencode.json", {"provider": {provider: definition}})
    return paths, roots, legacy


def _scan(service, roots=(), legacy=None):
    service.migration_project_roots = lambda: roots
    return migration.scan_native_configs(
        service.store.load(), home=service.migration_home,
        mask_credential=lambda value: "masked-fixture",
        validate_base_url=normalize_model_hub_base_url,
        project_roots=roots, legacy_auth=legacy,
    )


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("base_url,secret", [
    (CUSTOM, KEY), (OFFICIAL, "fixture-sk-ant-oat-static"),
])
def test_all_anthropic_native_producers_refuse_unpreservable_transport(
    tmp_path, native, entry, base_url, secret,
):
    paths, roots, legacy = _seed(native, entry, base_url=base_url, secret=secret)
    before = {path: path.read_bytes() for path in paths}
    service, store, adapter = _service(tmp_path, migration_home=native)
    if legacy:
        store.native_auth_snapshot = lambda backends: legacy
    rows = _scan(service, roots, legacy)
    assert rows
    assert all(row.proposed_action == "reauth" and not row.selected for row in rows)
    assert all(row.notes_key == BLOCKED for row in rows)
    assert secret not in json.dumps([row.to_payload() for row in rows])
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row.id for row in rows]))
    assert {path: path.read_bytes() for path in paths} == before
    assert not store.config.sources
    assert not adapter.provisioned and not adapter.transient_refs
    assert not adapter.observed
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("entry", ENTRIES)
def test_official_api_key_still_migrates_from_every_native_producer(tmp_path, native, entry):
    _, roots, legacy = _seed(native, entry, base_url=OFFICIAL)
    service, store, adapter = _service(tmp_path, migration_home=native)
    adapter.observed_protocols["custom"] = "anthropic"
    if legacy:
        store.native_auth_snapshot = lambda backends: legacy
    rows = _scan(service, roots, legacy)
    assert rows and all(row.proposed_action == "import" for row in rows)
    result = asyncio.run(service.migration_apply([row.id for row in rows]))
    assert result["applied"] == len(rows)
    assert all(source.protocol == "anthropic" for source in store.config.sources)
    assert all(scheme is None for scheme in adapter.auth_schemes.values())


@pytest.mark.parametrize("url,allowed", [
    (None, True), (OFFICIAL, True), ("https://API.ANTHROPIC.COM:443/v1", True),
    ("https://api.anthropic.com:/v1", True),
    ("https://api.anthropic.com:0443", False),
    ("https://api.anthropic.com:8443", False), ("http://api.anthropic.com", False),
    ("https://api.anthropic.com.", False),
    ("https://api.anthropic.com.fixture.example", False),
    ("https://fixture.example/api.anthropic.com", False),
    ("https://fixture@api.anthropic.com", False),
    ("https://%61pi.anthropic.com", False), ("https://api.anthropic.com:wrong", False),
    ("https://api.anthropic.com\\fixture", False), ("https://api.anthropic.com\n", False),
    ("https://ａｐｉ.anthropic.com", False),
])
def test_migration_gate_uses_same_validated_cpa_origin_as_explicit_bearer(url, allowed):
    validate = api_key_vendors.validate_migration_api_key_transport
    if allowed:
        assert validate("anthropic", "anthropic", url, KEY, None) is None
    else:
        with pytest.raises(ValueError, match="^unsupported native API key transport$"):
            validate("anthropic", "anthropic", url, KEY, None)
    # The existing public/credential-store default stays permissive.
    assert api_key_vendors.validate_api_key_auth_scheme("anthropic", "anthropic", url, KEY, None) is None


@pytest.mark.parametrize("secret", ["sk-ant-oat-fixture", "fixture-sk-ant-oat-inside"])
def test_migration_oauth_heuristic_is_never_reinterpreted_as_static(secret):
    validate = api_key_vendors.validate_migration_api_key_transport
    for url, scheme in ((OFFICIAL, None), (CUSTOM, None), (CUSTOM, "bearer")):
        with pytest.raises(ValueError, match="^unsupported native API key transport$"):
            validate("anthropic", "anthropic", url, secret, scheme)
    assert validate("openai", "openai_chat", CUSTOM, secret, None) is None


@pytest.mark.parametrize("vendor,url,scheme,allowed", [
    ("anthropic", CUSTOM, "bearer", True),
    ("anthropic", "https://api.anthropic.com:0443", "bearer", True),
    ("anthropic", OFFICIAL, "bearer", False),
    ("anthropic", CUSTOM, "unknown", False),
    ("custom", CUSTOM, "bearer", False),
    ("minimax", None, None, False),
    ("custom", None, None, False),
    ("custom", OFFICIAL, None, True),
])
def test_migration_validation_reuses_explicit_scheme_and_catalog_target(vendor, url, scheme, allowed):
    validate = api_key_vendors.validate_migration_api_key_transport
    if allowed:
        assert validate(vendor, "anthropic", url, KEY, scheme) is None
    else:
        with pytest.raises(ValueError, match="^unsupported native API key transport$"):
            validate(vendor, "anthropic", url, KEY, scheme)


def _source(ref, *, vendor="anthropic", protocol="anthropic", base_url=CUSTOM):
    return ModelHubSourceConfig.from_payload({
        "id": "src_transportfixture", "kind": "api_key", "vendor": vendor,
        "display_name": "Transport fixture", "protocol": protocol,
        "base_url": base_url, "supply_channel": "hub", "billing": "metered",
        "state": {"status": "standby"}, "models": [], "credential_ref": ref,
    })


@pytest.mark.parametrize("reuse", [False, True])
def test_apply_entry_revalidates_before_proof_or_source_reuse(tmp_path, native, reuse):
    _seed(native, "claude-settings", base_url=OFFICIAL)
    service, store, adapter = _service(tmp_path, migration_home=native)
    [item] = _scan(service)
    item = replace(item, base_url=CUSTOM)
    if reuse:
        store.config.sources.append(_source("cred_existingfixture"))
    adapter.matches_api_key_credential = AsyncMock(return_value=True)
    service._require_proven_source_payload = AsyncMock()
    service._require_proven_observation = AsyncMock()
    with pytest.raises(migration.MigrationConflictError):
        asyncio.run(migration._prepare_takeover(
            service, store.config, [item], mask_credential=lambda value: "masked-fixture",
            validate_base_url=normalize_model_hub_base_url,
        ))
    adapter.matches_api_key_credential.assert_not_awaited()
    service._require_proven_source_payload.assert_not_awaited()
    service._require_proven_observation.assert_not_awaited()
    assert not adapter.provisioned
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("native_protocol,observed", [
    ("anthropic", "openai_chat"), ("openai_chat", "anthropic"),
])
def test_observation_cannot_change_native_auth_semantics(
    tmp_path, native, reuse, native_protocol, observed,
):
    _seed(native, "opencode-custom", base_url=OFFICIAL)
    path = native / ".config/opencode/opencode.json"
    if native_protocol == "openai_chat":
        _write(path, json.dumps({"provider": {"custom": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"apiKey": KEY, "baseURL": OFFICIAL},
        }}}))
    before = path.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=native)
    adapter.observed_protocols["custom"] = observed
    if reuse:
        ref = asyncio.run(adapter.provision_credential("custom", native_protocol, KEY, OFFICIAL))
        store.config.sources.append(_source(ref, vendor="custom", protocol=native_protocol, base_url=OFFICIAL))
        adapter.provisioned.clear()
    [row] = service.migration_scan()["items"]
    assert row["proposed_action"] == "import"
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"]]))
    assert path.read_bytes() == before
    assert not adapter.provisioned
    assert service.migration_journal.load() is None


def test_observation_may_change_openai_dialect_without_changing_bearer_auth(tmp_path, native):
    path = native / ".config/opencode/opencode.json"
    _write(path, json.dumps({"provider": {"custom": {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"apiKey": KEY, "baseURL": CUSTOM},
    }}}))
    service, store, adapter = _service(tmp_path, migration_home=native)
    adapter.observed_protocols["custom"] = "openai_responses"
    [row] = service.migration_scan()["items"]
    assert asyncio.run(service.migration_apply([row["id"]]))["applied"] == 1
    assert store.config.sources[0].protocol == "openai_responses"


@asynccontextmanager
async def _upstream(scheme):
    requests = []

    async def consume(request):
        headers = {key.lower(): value for key, value in request.headers.items()}
        requests.append((request.path, headers))
        valid = (
            headers.get("x-api-key") == KEY and "authorization" not in headers
            if scheme == "x-api-key" else
            headers.get("authorization") == f"Bearer {KEY}" and "x-api-key" not in headers
        )
        if not valid:
            return web.json_response({"type": "error", "error": {
                "type": "authentication_error", "message": "invalid credential",
            }}, status=401)
        if request.method == "GET":
            return web.json_response({"data": [{"id": "claude-fixture"}]})
        body = await request.json()
        if "model" not in body:
            return web.json_response({"type": "error", "error": {
                "type": "invalid_request_error", "message": "model: Field required",
            }}, status=400)
        return web.json_response({"type": "message", "content": [{"type": "text", "text": "fixture-ok"}]})

    app = web.Application()
    app.router.add_post("/v1/messages", consume)
    app.router.add_get("/v1/models", consume)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        yield origin, requests
    finally:
        await runner.cleanup()


def _real_http_adapter(service, tmp_path):
    runtime = _adapter(tmp_path)
    for method in (
        "provision_credential", "provision_transient_credential", "matches_api_key_credential",
        "credential_auth_scheme", "observe_source", "discover_models", "revoke_credential",
        "sync_sources",
    ):
        setattr(service.adapter, method, getattr(runtime, method))
    return runtime


@pytest.mark.parametrize("entry", ["claude-settings", "shell", "opencode-custom"])
@pytest.mark.parametrize("reuse", [False, True])
def test_strict_custom_x_api_key_never_becomes_successful_migration(
    tmp_path, native, entry, reuse,
):
    async def scenario():
        async with _upstream("x-api-key") as (origin, requests):
            paths, _, _ = _seed(native, entry, base_url=origin)
            before = {path: path.read_bytes() for path in paths}
            service, store, _ = _service(tmp_path, migration_home=native)
            runtime = _real_http_adapter(service, tmp_path)
            if reuse:
                vendor = "custom" if entry == "opencode-custom" else "anthropic"
                ref = await runtime.provision_credential(vendor, "anthropic", KEY, origin)
                store.config.sources.append(_source(ref, vendor=vendor, base_url=origin))
            config_before = store.config.to_payload()
            credentials_before = set((runtime.state_store.root / "credentials").glob("*.json"))
            # Actual consuming HTTP witness: this upstream accepts the native
            # header, and rejects the pinned CPA custom-origin Bearer header.
            async with aiohttp.ClientSession() as client:
                for headers, status in (({"x-api-key": KEY}, 200), ({"Authorization": f"Bearer {KEY}"}, 401)):
                    async with client.post(origin + "/v1/messages", headers=headers, json={"model": "claude-fixture"}) as response:
                        assert response.status == status
            requests.clear()
            # Demonstrate the old observation path can prove this key using
            # x-api-key. That proof must not license destructive migration.
            proof = await service._require_proven_source_payload({
                "vendor": "anthropic", "key": KEY, "base_url": origin,
            })
            assert proof.authenticated and proof.protocol == "anthropic"
            assert any(headers.get("x-api-key") == KEY for _, headers in requests)
            requests.clear()
            rows = service.migration_scan()["items"]
            with pytest.raises(ModelHubError):
                await service.migration_apply([row["id"] for row in rows])
            assert all(row["notes_key"] == BLOCKED for row in rows)
            assert requests == []
            assert {path: path.read_bytes() for path in paths} == before
            assert store.config.to_payload() == config_before
            assert set((runtime.state_store.root / "credentials").glob("*.json")) == credentials_before
            assert service.migration_journal.load() is None

    asyncio.run(scenario())


def test_real_observation_cannot_promote_custom_key_to_anthropic_transport(tmp_path, native):
    async def scenario():
        async with _upstream("x-api-key") as (origin, requests):
            path = native / ".config/opencode/opencode.json"
            _write(path, json.dumps({"provider": {"custom": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"apiKey": KEY, "baseURL": origin},
            }}}))
            before = path.read_bytes()
            service, store, _ = _service(tmp_path, migration_home=native)
            runtime = _real_http_adapter(service, tmp_path)
            [row] = service.migration_scan()["items"]
            assert row["proposed_action"] == "import"
            with pytest.raises(ModelHubError):
                await service.migration_apply([row["id"]])
            assert any(headers.get("x-api-key") == KEY for _, headers in requests)
            assert path.read_bytes() == before
            assert not store.config.sources
            assert not list((runtime.state_store.root / "credentials").glob("*.json"))
            assert service.migration_journal.load() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["claude-settings", "shell"])
def test_custom_bearer_keeps_actual_proof_discovery_and_cleanup(tmp_path, native, entry):
    async def scenario():
        async with _upstream("bearer") as (origin, requests):
            paths, _, _ = _seed(native, entry, base_url=origin, bearer=True)
            service, store, _ = _service(tmp_path, migration_home=native)
            runtime = _real_http_adapter(service, tmp_path)
            rows = service.migration_scan()["items"]
            result = await service.migration_apply([row["id"] for row in rows])
            assert result["applied"] == 1
            [source] = store.config.sources
            assert await runtime.credential_auth_scheme(source.credential_ref) == "bearer"
            assert [model.id for model in source.models] == ["claude-fixture"]
            assert all("x-api-key" not in headers for _, headers in requests)
            assert any(headers.get("authorization") == f"Bearer {KEY}" for _, headers in requests)
            assert any("authorization" not in headers for _, headers in requests)
            assert all(KEY.encode() not in path.read_bytes() for path in paths)
            assert service.migration_scan()["items"] == []

    asyncio.run(scenario())
