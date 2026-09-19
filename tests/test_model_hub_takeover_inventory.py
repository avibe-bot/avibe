"""Takeover discovers and cleans all supported precedence layers as one unit."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from config import paths
from config.v2_config import V2Config
from core.handlers.model_hub.migration import MigrationConflictError
from core.handlers.model_hub.service import ModelHubError, V2ModelHubConfigStore
from vibe.native_oauth_store import NativeOAuthSnapshot
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
    _write_claude_oauth,
)


def test_global_and_project_keys_are_all_imported_and_unrelated_data_survives(monkeypatch, tmp_path):
    home, project = tmp_path / "native", tmp_path / "project"
    _isolate_native_home(monkeypatch, home)
    global_path = home / ".config/opencode/opencode.jsonc"
    project_path = project / ".opencode/opencode.json"
    for path, key in ((global_path, "fixture-global-key"), (project_path, "fixture-project-key")):
        _write(path, json.dumps({
            "provider": {"openai": {"options": {"apiKey": key}, "name": "保留"}},
            "mcp": {"notes": {"command": ["fixture"]}},
        }))
    service, store, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: (project,)
    ids = [item["id"] for item in service.migration_scan()["items"]]
    assert len(ids) == 2
    assert asyncio.run(service.migration_apply(ids))["applied"] == 2
    assert len(adapter.provisioned) == 2
    assert store.config.agents["opencode"].mode == "hub"
    for path in (global_path, project_path):
        remaining = json.loads(path.read_text())
        assert remaining["provider"]["openai"] == {"name": "保留"}
        assert remaining["mcp"]["notes"]["command"] == ["fixture"]


def test_shadowed_opencode_auth_key_is_not_silently_deleted(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write(home / ".config/opencode/opencode.json", json.dumps({
        "provider": {"openai": {"options": {"apiKey": "fixture-config-key"}}},
    }))
    auth_path = home / ".local/share/opencode/auth.json"
    _write(auth_path, json.dumps({"openai": {"type": "api", "key": "fixture-auth-key"}}))
    service, _, adapter = _service(tmp_path, migration_home=home)
    ids = [item["id"] for item in service.migration_scan()["items"]]
    assert len(ids) == 2
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids[:1]))
    assert adapter.provisioned == []
    assert asyncio.run(service.migration_apply(ids))["applied"] == 2
    assert len(adapter.keys) == 2
    assert json.loads(auth_path.read_text()) == {}


def test_keychain_takeover_preserves_mcp_without_offering_it_as_a_new_login(monkeypatch, tmp_path):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    monkeypatch.setenv("USER", "fixture-user")
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    locator = ("Claude Code-credentials", "fixture-user")
    material = {
        "claudeAiOauth": {
            "accessToken": "fixture-access", "refreshToken": "fixture-refresh",
            "expiresAt": 1893456000000,
        },
        "mcpOAuth": {"provider": "preserved"},
    }
    keychain.items[locator] = (json.dumps(material), "fixture-original")
    path = home / ".claude/.credentials.json"
    _write(path, json.dumps(material))
    service, _, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert keychain.read_calls == []  # Metadata-only consent.
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert len(adapter.oauth_provisioned) == 1
    assert json.loads(path.read_text()) == {"mcpOAuth": {"provider": "preserved"}}
    assert json.loads(keychain.items[locator][0]) == {"mcpOAuth": {"provider": "preserved"}}
    reads_after_takeover = len(keychain.read_calls)
    assert service.migration_scan()["items"] == []
    assert len(keychain.read_calls) == reads_after_takeover
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    # A subsequent backend migration must not forget the verified clean store.
    _write(home / ".config/opencode/opencode.json", json.dumps({
        "provider": {"openai": {"options": {"apiKey": "fixture-other-key"}}},
    }))
    next_ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(next_ids))["applied"] == 1
    assert service.migration_scan()["items"] == []
    assert len(keychain.read_calls) == reads_after_takeover
    # A changed metadata revision is never suppressed by that receipt.
    keychain.items[locator] = (json.dumps(material), "fixture-new-login")
    keychain.mdates[locator] += 1
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["claude"]
    assert len(keychain.read_calls) == reads_after_takeover


def test_unsupported_provider_blocks_only_its_cli_before_any_cleanup(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    path = home / ".local/share/opencode/auth.json"
    _write(path, json.dumps({
        "openai": {"type": "api", "key": "fixture-key"},
        "github-copilot": {"type": "oauth", "refresh": "fixture-refresh"},
    }))
    before = path.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert {row["proposed_action"] for row in rows} == {"import", "reauth"}
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"] for row in rows if row["proposed_action"] == "import"]))
    assert path.read_bytes() == before
    assert adapter.provisioned == []


def test_project_key_changed_after_consent_refuses_without_import(monkeypatch, tmp_path):
    home, project = tmp_path / "native", tmp_path / "project"
    _isolate_native_home(monkeypatch, home)
    path = project / ".claude/settings.local.json"
    _write(path, json.dumps({"env": {"ANTHROPIC_API_KEY": "fixture-first"}}))
    service, _, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: (project,)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    _write(path, json.dumps({"env": {"ANTHROPIC_API_KEY": "fixture-second"}}))
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert adapter.provisioned == []
    assert json.loads(path.read_text())["env"]["ANTHROPIC_API_KEY"] == "fixture-second"


def test_keychain_container_consent_also_binds_routing_after_drain(monkeypatch, tmp_path):
    from core.handlers.model_hub import migration

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    path = home / ".codex/config.toml"
    config = '''
model_provider = "fixture"
[model_providers.fixture]
name = "Fixture"
base_url = "https://first.example/v1"
wire_api = "responses"
'''
    _write(path, config)

    def read(backend, *, home, allow_secret=False):
        if backend != "codex":
            return None
        return NativeOAuthSnapshot(
            backend="codex", revision="fixture-unchanged-container",
            payload={"OPENAI_API_KEY": "fixture-key"} if allow_secret else {
                "store": "keychain", "status": "metadata_only",
            },
            exportable=allow_secret,
        )

    monkeypatch.setattr(migration, "read_native_oauth", read)
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1

    @asynccontextmanager
    async def guard(backends):
        path.write_text(config.replace("first.example", "other.example"))

        async def verify():
            pass

        yield verify

    service.migration_guard = guard
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(service.migration_apply(ids))
    assert failure.value.code == "migration_item_conflict"
    assert not adapter.provisioned and not adapter.oauth_provisioned
    assert store.config.agents["codex"].mode == "direct"
    assert "other.example" in path.read_text()
    assert service.migration_journal.load() is None


def test_existing_hub_key_still_clears_legacy_auth_when_hub_config_is_unchanged(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write(home / ".claude/settings.json", json.dumps({
        "env": {"ANTHROPIC_API_KEY": "fixture-key"},
    }))
    service, memory, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    asyncio.run(service.migration_apply(ids))
    config_path = tmp_path / "avibe-config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    config = V2Config.default()
    config.model_hub = memory.config
    config.agents.claude.auth_mode = "api_key"
    config.agents.claude.api_key = "fixture-key"
    config.save(config_path=config_path)
    previous_hub = config.model_hub.to_payload()
    service.store = V2ModelHubConfigStore()

    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    result = asyncio.run(service.migration_apply(ids))

    assert result["applied"] == 1
    loaded = V2Config.load(config_path=config_path)
    assert loaded.model_hub.to_payload() == previous_hub
    assert loaded.agents.claude.api_key is None
    assert loaded.agents.claude.auth_mode == "oauth"
    assert len(adapter.provisioned) == 1  # No duplicate credential or Source.
    assert service.migration_journal.load() is None


def test_config_takeover_cas_preserves_a_concurrent_hub_edit(monkeypatch, tmp_path):
    config_path = tmp_path / "avibe-config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    config = V2Config.default()
    config.save(config_path=config_path)
    expected = config.model_hub.to_payload()
    config.model_hub.agents["codex"].mode = "direct"
    config.save(config_path=config_path)
    concurrent = config_path.read_bytes()
    store = V2ModelHubConfigStore()
    with pytest.raises(MigrationConflictError):
        store.save_takeover(config.model_hub, {}, {}, expected_hub=(expected,))
    assert config_path.read_bytes() == concurrent


def test_interrupted_claude_auth_backup_is_imported_and_cannot_restore_old_key(monkeypatch, tmp_path):
    from vibe.claude_config import (
        get_claude_oauth_settings_backup_path,
        read_claude_oauth_settings_backup,
        write_claude_oauth_settings_backup,
    )

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_claude_oauth(home)
    _write(home / ".claude/settings.json", json.dumps({
        "env": {"ANTHROPIC_API_KEY": "fixture-active-key"},
        "mcp": {"keep": "保留"},
    }))
    write_claude_oauth_settings_backup(
        {"ANTHROPIC_API_KEY": "fixture-recovery-key"},
        home=home,
    )
    service, _, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert len(rows) == 3
    result = asyncio.run(service.migration_apply([row["id"] for row in rows]))
    assert result["applied"] == 3
    assert len(adapter.oauth_provisioned) == 1
    assert {target[2] for target in adapter.keys.values()} == {
        "fixture-active-key", "fixture-recovery-key",
    }
    assert not get_claude_oauth_settings_backup_path(home).exists()
    assert read_claude_oauth_settings_backup(home) is None
    assert json.loads((home / ".claude/settings.json").read_text()) == {
        "env": {}, "mcp": {"keep": "保留"},
    }


@pytest.mark.parametrize("metadata", [
    {"client_id": "fixture-custom-client"},
    {"scopes": ["user:inference"]},
    {"scope": "user:profile user:inference"},
    {"scopes": None},
])
def test_explicit_incompatible_native_grant_is_blocked_before_custody(monkeypatch, tmp_path, metadata):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_claude_oauth(home)
    path = home / ".claude/.credentials.json"
    payload = json.loads(path.read_text())
    payload["claudeAiOauth"].update(metadata)
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    service, _, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert len(rows) == 1 and rows[0]["proposed_action"] == "keep_native"
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([rows[0]["id"]]))
    assert path.read_bytes() == original
    assert not adapter.oauth_provisioned


def test_ordinary_native_grant_with_extra_scopes_remains_importable(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_claude_oauth(home)
    path = home / ".claude/.credentials.json"
    payload = json.loads(path.read_text())
    payload["claudeAiOauth"].update({
        "clientId": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "scopes": [
            "user:profile", "user:inference", "user:sessions:claude_code",
            "user:mcp_servers", "user:file_upload", "user:plugins",
        ],
    })
    path.write_text(json.dumps(payload))
    service, _, _ = _service(tmp_path, migration_home=home)
    assert service.migration_scan()["items"][0]["proposed_action"] == "import"


def test_codex_embedded_bearer_key_is_migrated_without_deleting_provider_preferences(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    path = home / ".codex/config.toml"
    _write(path, '''
model_provider = "fixture"
[model_providers.fixture]
name = "Private relay"
base_url = "https://relay.example/v1"
wire_api = "responses"
experimental_bearer_token = "fixture-bearer-key"
request_max_retries = 3
''')
    service, _, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert len(adapter.provisioned) == 1
    content = path.read_text()
    assert "fixture-bearer-key" not in content
    assert "base_url" not in content
    assert 'name = "Private relay"' in content
    assert "request_max_retries = 3" in content


def test_duplicate_apply_joins_owned_operation_and_shutdown_waits(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write(home / ".claude/settings.json", json.dumps({"env": {"ANTHROPIC_API_KEY": "fixture-key"}}))
    service, _, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original_sync = adapter.sync_sources
        stopped = []

        async def paused_sync(bindings):
            entered.set()
            await release.wait()
            await original_sync(bindings)

        async def stop():
            assert release.is_set()
            stopped.append(True)

        adapter.sync_sources, adapter.stop = paused_sync, stop
        first = asyncio.create_task(service.migration_apply(ids))
        await entered.wait()
        second = asyncio.create_task(service.migration_apply(ids))
        shutdown = asyncio.create_task(service.stop())
        await asyncio.sleep(0)
        assert not second.done() and not shutdown.done()
        release.set()
        assert (await first)["applied"] == (await second)["applied"] == 1
        await shutdown
        assert stopped == [True]

    asyncio.run(exercise())
    assert len(adapter.provisioned) == 1
