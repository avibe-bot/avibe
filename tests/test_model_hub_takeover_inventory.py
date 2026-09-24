"""Takeover discovers and cleans all supported precedence layers as one unit."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

import pytest

from config import paths
from config.v2_config import ModelHubConfig, V2Config
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
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 2
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
        asyncio.run(service.migration_apply(ids[:1], clean_api_keys=True))
    assert adapter.provisioned == []
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 2
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
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert len(adapter.oauth_provisioned) == 1
    assert json.loads(path.read_text()) == {"mcpOAuth": {"provider": "preserved"}}
    assert json.loads(keychain.items[locator][0]) == {"mcpOAuth": {"provider": "preserved"}}
    reads_after_takeover = len(keychain.read_calls)
    assert service.migration_scan()["items"] == []
    assert len(keychain.read_calls) == reads_after_takeover
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    # A subsequent backend migration must not forget the verified clean store.
    _write(home / ".config/opencode/opencode.json", json.dumps({
        "provider": {"openai": {"options": {"apiKey": "fixture-other-key"}}},
    }))
    next_ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(next_ids, clean_api_keys=True))["applied"] == 1
    assert service.migration_scan()["items"] == []
    assert len(keychain.read_calls) == reads_after_takeover
    # A changed metadata revision is never suppressed by that receipt.
    keychain.items[locator] = (json.dumps(material), "fixture-new-login")
    keychain.mdates[locator] += 1
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["claude"]
    assert len(keychain.read_calls) == reads_after_takeover


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_unchanged_unrelated_keychain_container_completes_without_import(monkeypatch, tmp_path, backend):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    monkeypatch.setenv("USER", "fixture-user")
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    if backend == "claude":
        locator = ("Claude Code-credentials", "fixture-user")
    else:
        _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
        account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
        locator = ("Codex Auth", account)
    keychain.items[locator] = ('{"mcpOAuth":{"unrelated":"保留"}}', "fixture-original")
    original = dict(keychain.items)
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents[backend].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    assert not keychain.read_calls
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert store.config.agents[backend].mode == "hub"
    assert not store.config.sources
    assert not adapter.provisioned and not adapter.oauth_provisioned
    assert keychain.items == original
    assert not keychain.write_calls and not keychain.delete_calls
    reads = len(keychain.read_calls)
    assert reads > 0
    assert service.migration_scan()["items"] == []
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert len(keychain.read_calls) == reads
    keychain.items[locator] = (original[locator][0], "fixture-new-revision")
    keychain.mdates[locator] += 1
    assert len(service.migration_scan()["items"]) == 1


@pytest.mark.parametrize("with_api_key", [False, True])
def test_empty_codex_container_never_authorizes_deleting_dormant_file_grant(
    monkeypatch, tmp_path, with_api_key,
):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    config = 'cli_auth_credentials_store = "keyring"\n'
    if with_api_key:
        config += (
            'model_provider = "fixture"\n[model_providers.fixture]\n'
            'base_url = "https://fixture.example/v1"\n'
            'experimental_bearer_token = "fixture-key"\n'
        )
    _write(home / ".codex/config.toml", config)
    account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
    locator = ("Codex Auth", account)
    keychain.items[locator] = ('{"mcpOAuth":{"keep":"保留"}}', "fixture-original")
    dormant = home / ".codex/auth.json"
    payload = {"tokens": {"access_token": "fixture-dormant", "refresh_token": "fixture-dormant-refresh"}}
    if with_api_key:
        payload["OPENAI_API_KEY"] = "fixture-key"
    _write(dormant, json.dumps(payload))
    before = dormant.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    if with_api_key:
        # The key migrates; the dormant file (and equal bytes it holds) is never touched.
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
        assert dormant.read_bytes() == before
        assert store.config.agents["codex"].mode == "hub"
        assert len(adapter.provisioned) == len(store.config.sources) == 1
        assert not adapter.oauth_provisioned
        assert not keychain.delete_calls and not keychain.write_calls
        assert service.migration_journal.load() is None
        return
    else:
        assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
        assert store.config.agents["codex"].mode == "hub"
    assert dormant.read_bytes() == before
    assert not store.config.sources
    assert not adapter.provisioned and not adapter.oauth_provisioned
    assert not keychain.delete_calls and not keychain.write_calls
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("boundary", ["permission", "install", "start"])
def test_empty_container_evidence_cannot_hide_denial_or_an_intervening_login(
    monkeypatch, tmp_path, boundary,
):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store
    from core.handlers.model_hub.adapter import EngineEnsureResult, EngineHealth, EngineStatus

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    monkeypatch.setenv("USER", "fixture-user")
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    locator = ("Claude Code-credentials", "fixture-user")
    keychain.items[locator] = ('{"mcpOAuth":{"keep":"保留"}}', "fixture-original")
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["claude"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    status = EngineStatus(EngineHealth.OK, "fixture", True, "127.0.0.1", 32199, None)

    def login():
        keychain.items[locator] = (json.dumps({
            "claudeAiOauth": {"accessToken": "fixture-new", "refreshToken": "fixture-refresh"},
            "mcpOAuth": {"keep": "保留"},
        }), "fixture-new-login")
        keychain.mdates[locator] += 1

    async def install(**kwargs):
        if boundary == "install":
            login()
        return EngineEnsureResult(status, False)

    async def start():
        if boundary == "start":
            login()
        return status

    adapter.ensure_installed = install
    adapter.start = start
    keychain.read_denied = boundary == "permission"
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    assert not store.config.sources
    assert not adapter.oauth_provisioned and not adapter.provisioned
    assert not keychain.delete_calls and not keychain.write_calls
    assert service.migration_journal.completed() is None
    assert service.migration_journal.load() is None
    assert store.config.agents["claude"].mode == "direct"
    assert not service.migration_blocked_backends
    if boundary != "permission":
        assert "fixture-new" in keychain.items[locator][0]
        new_ids = [row["id"] for row in service.migration_scan()["items"]]
        assert new_ids != ids
        # A new, explicit consent can now take over the new login.
        adapter.ensure_installed = lambda **kwargs: _return_async(EngineEnsureResult(status, False))
        adapter.start = lambda: _return_async(status)
        assert asyncio.run(service.migration_apply(new_ids, clean_api_keys=True))["applied"] == 1
        assert len(adapter.oauth_provisioned) == 1
        assert service.migration_journal.load() is None


async def _return_async(value):
    return value


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
    asyncio.run(service.migration_apply(
        [row["id"] for row in rows if row["proposed_action"] == "import"], clean_api_keys=True,
    ))
    # The OAuth provider the Hub cannot carry stays exactly as it was.
    assert json.loads(path.read_bytes()) == {
        "github-copilot": json.loads(before)["github-copilot"],
    }
    assert len(adapter.provisioned) == 1


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
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
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
        asyncio.run(service.migration_apply(ids, clean_api_keys=True))
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
    asyncio.run(service.migration_apply(ids, clean_api_keys=True))
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
    result = asyncio.run(service.migration_apply(ids, clean_api_keys=True))

    assert result["applied"] == 1
    loaded = V2Config.load(config_path=config_path)
    assert loaded.model_hub.to_payload() == previous_hub
    assert loaded.agents.claude.api_key is None
    assert loaded.agents.claude.auth_mode == "oauth"
    assert len(adapter.provisioned) == 1  # No duplicate credential or Source.
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("accepted", [False, True])
def test_reused_hub_key_requires_current_proof_before_native_cleanup(monkeypatch, tmp_path, accepted):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    asyncio.run(service.create_source({
        "kind": "api_key", "vendor": "anthropic", "key": "fixture-key",
        "display_name": "Existing source",
    }))
    store.config.agents["claude"].mode = "direct"
    native = home / ".claude/settings.json"
    _write(native, '{"env":{"ANTHROPIC_API_KEY":"fixture-key"},"mcp":{"keep":true}}')
    before = native.read_bytes()
    previous = store.config.to_payload()
    observed = len(adapter.observed)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    if not accepted:
        adapter.unproven_observation_vendor = "anthropic"
        with pytest.raises(ModelHubError) as failure:
            asyncio.run(service.migration_apply(ids, clean_api_keys=True))
        assert failure.value.code == "migration_item_conflict"
        assert native.read_bytes() == before
        assert store.config.to_payload() == previous
    else:
        assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
        assert store.config.to_payload()["sources"] == previous["sources"]
        assert json.loads(native.read_text()) == {"env": {}, "mcp": {"keep": True}}
    assert len(adapter.observed) == observed + 1
    assert len(adapter.provisioned) == 1
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
    result = asyncio.run(service.migration_apply([row["id"] for row in rows], clean_api_keys=True))
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
        asyncio.run(service.migration_apply([rows[0]["id"]], clean_api_keys=True))
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
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
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
        first = asyncio.create_task(service.migration_apply(ids, clean_api_keys=True))
        await entered.wait()
        second = asyncio.create_task(service.migration_apply(ids, clean_api_keys=True))
        shutdown = asyncio.create_task(service.stop())
        await asyncio.sleep(0)
        assert not second.done() and not shutdown.done()
        release.set()
        assert (await first)["applied"] == (await second)["applied"] == 1
        await shutdown
        assert stopped == [True]

    asyncio.run(exercise())
    assert len(adapter.provisioned) == 1


def test_kept_keychain_codex_key_is_not_offered_again(monkeypatch, tmp_path):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
    account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
    locator = ("Codex Auth", account)
    keychain.items[locator] = ('{"OPENAI_API_KEY":"fixture-key-123456"}', "fixture-original")
    original = dict(keychain.items)
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert ids
    asyncio.run(service.migration_apply(ids))
    assert store.config.agents["codex"].mode == "hub"
    assert len(adapter.provisioned) == 1
    assert keychain.items == original
    assert not keychain.write_calls and not keychain.delete_calls
    assert service.migration_scan()["items"] == []
    # The store still holds the kept key: it is hidden only while the copy lives.
    [copy] = service.migration_journal.completed()["retained_native_ids"].values()
    payload = store.config.to_payload()
    payload["sources"] = [source for source in payload["sources"] if source["id"] != copy["source_id"]]
    for agent in payload["agents"].values():
        agent["sources"]["order"] = [value for value in agent["sources"]["order"] if value != copy["source_id"]]
        agent["routes"] = {}
    store.config = ModelHubConfig.from_payload(payload)
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["codex"]


def test_rejected_oauth_completion_still_binds_a_kept_keychain_key(monkeypatch, tmp_path):
    from core.handlers.model_hub.adapter import OAuthCredentialRejectedError
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
    account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
    locator = ("Codex Auth", account)
    keychain.items[locator] = (json.dumps({
        "OPENAI_API_KEY": "fixture-key-123456",
        "tokens": {
            "access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "account_id": "acct_fixture",
        },
    }), "fixture-original")
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    validate = adapter.validate_oauth_credential

    async def reject(ref):
        await validate(ref)
        raise OAuthCredentialRejectedError()

    adapter.validate_oauth_credential = reject
    with pytest.raises(ModelHubError) as failure:
        asyncio.run(service.migration_apply(ids))
    assert failure.value.code == "migration_credentials_invalid"
    assert json.loads(keychain.items[locator][0]) == {"OPENAI_API_KEY": "fixture-key-123456"}
    assert service.migration_scan()["items"] == []
    [copy] = service.migration_journal.completed()["retained_native_ids"].values()
    payload = store.config.to_payload()
    payload["sources"] = [source for source in payload["sources"] if source["id"] != copy["source_id"]]
    for agent in payload["agents"].values():
        agent["sources"]["order"] = [value for value in agent["sources"]["order"] if value != copy["source_id"]]
        agent["routes"] = {}
    store.config = ModelHubConfig.from_payload(payload)
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["codex"]


def test_cleanup_keeps_an_unimported_avibe_saved_key(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write(home / ".claude/settings.json", json.dumps({
        "env": {"ANTHROPIC_API_KEY": "fixture-native-key"},
    }))
    config_path = tmp_path / "avibe-config.json"
    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    service, memory, _adapter = _service(tmp_path, migration_home=home)
    config = V2Config.default()
    config.model_hub = memory.config
    config.agents.claude.auth_mode = "api_key"
    config.agents.claude.api_key = "fixture-saved-key"
    config.agents.claude.base_url = "ftp://saved.example/v1"
    config.save(config_path=config_path)
    service.store = V2ModelHubConfigStore()
    scan = service.migration_scan()["items"]
    assert sorted(row["proposed_action"] for row in scan) == ["import", "reauth"]
    ids = [row["id"] for row in scan if row["proposed_action"] == "import"]
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    loaded = V2Config.load(config_path=config_path)
    assert loaded.agents.claude.api_key == "fixture-saved-key"
    assert loaded.agents.claude.base_url == "ftp://saved.example/v1"


def _keychain_codex_key_migrated(monkeypatch, tmp_path):
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
    account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
    locator = ("Codex Auth", account)
    keychain.items[locator] = ('{"OPENAI_API_KEY":"fixture-key-123456"}', "fixture-original")
    service, store, _adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    asyncio.run(service.migration_apply(ids))
    assert service.migration_scan()["items"] == []
    return home, keychain, locator, service


def test_kept_keychain_key_is_offered_again_after_codex_routing_changes(monkeypatch, tmp_path):
    home, _keychain, _locator, service = _keychain_codex_key_migrated(monkeypatch, tmp_path)
    _write(home / ".codex/config.toml", (
        'cli_auth_credentials_store = "keyring"\nmodel_provider = "Relay"\n\n'
        '[model_providers.Relay]\nbase_url = "https://relay.example/v1"\nwire_api = "responses"\n'
    ))
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["codex"]


def test_retained_receipt_binds_only_the_verified_store_revision(monkeypatch, tmp_path):
    from core.handlers.model_hub.migration import _record_retained_store_revisions

    home, keychain, locator, service = _keychain_codex_key_migrated(monkeypatch, tmp_path)
    record = {
        "retained_native_ids": {"key_fixture": {
            "source_id": "src_fixture", "credential_ref": "cred_fixture",
            "store_backend": "codex", "store_routing": "",
        }},
        "verified_store_revisions": {"codex": "codex:keychain:verified-before-external-write"},
    }
    keychain.items[locator] = ('{"OPENAI_API_KEY":"fixture-other-key"}', "fixture-external")
    keychain.mdates[locator] += 1
    asyncio.run(_record_retained_store_revisions(service, record))
    assert "store_revision" not in record["retained_native_ids"]["key_fixture"]


_RELAY_AND_OTHER = (
    'cli_auth_credentials_store = "file"\nmodel_provider = "Relay"\n\n[model_providers.Relay]\n'
    'base_url = "ftp://relay.example/v1"\nwire_api = "responses"\n\n'
    '[model_providers.Other]\nbase_url = "https://other.example/v1"\nwire_api = "responses"\n'
    'experimental_bearer_token = "{token}"\n'
)


def _clean_importable(service):
    scan = service.migration_scan()["items"]
    assert any(row["proposed_action"] == "reauth" for row in scan)
    ids = [row["id"] for row in scan if row["proposed_action"] == "import"]
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == len(ids)


@pytest.mark.parametrize("with_login", [False, True])
def test_cleanup_keeps_routing_and_store_of_an_uncarried_codex_key(monkeypatch, tmp_path, with_login):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    auth = {"OPENAI_API_KEY": "fixture-store-key-123456"}
    if with_login:
        auth["tokens"] = {
            "access_token": "fixture-access", "refresh_token": "fixture-refresh", "account_id": "acct",
        }
    _write(home / ".codex/auth.json", json.dumps(auth))
    config = home / ".codex/config.toml"
    _write(config, _RELAY_AND_OTHER.format(token="fixture-other-key-654321"))
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    _clean_importable(service)
    assert json.loads((home / ".codex/auth.json").read_text()) == {"OPENAI_API_KEY": "fixture-store-key-123456"}
    text = config.read_text()
    assert 'model_provider = "Relay"' in text
    assert "ftp://relay.example/v1" in text
    assert 'cli_auth_credentials_store = "file"' in text
    assert "fixture-other-key-654321" not in text


def test_cleanup_keeps_a_codex_store_key_equal_to_a_carried_provider_key(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    shared = "fixture-shared-key-123456"
    _write(home / ".codex/auth.json", json.dumps({"OPENAI_API_KEY": shared}))
    _write(home / ".codex/config.toml", _RELAY_AND_OTHER.format(token=shared))
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    _clean_importable(service)
    assert json.loads((home / ".codex/auth.json").read_text()) == {"OPENAI_API_KEY": shared}


def test_cleanup_keeps_a_claude_layer_key_equal_to_a_carried_one(monkeypatch, tmp_path):
    home, project = tmp_path / "native", tmp_path / "project"
    _isolate_native_home(monkeypatch, home)
    shared = "fixture-shared-anthropic-key"
    _write(home / ".claude/settings.json", json.dumps({"env": {"ANTHROPIC_API_KEY": shared}}))
    project_settings = project / ".claude/settings.json"
    _write(project_settings, json.dumps({
        "env": {"ANTHROPIC_API_KEY": shared, "ANTHROPIC_BASE_URL": "ftp://relay.example"},
    }))
    before = project_settings.read_bytes()
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: (project,)
    _clean_importable(service)
    assert project_settings.read_bytes() == before
    assert "ANTHROPIC_API_KEY" not in (home / ".claude/settings.json").read_text()


def test_cleanup_keeps_an_opencode_layer_key_equal_to_a_carried_one(monkeypatch, tmp_path):
    home, project = tmp_path / "native", tmp_path / "project"
    _isolate_native_home(monkeypatch, home)
    shared = "fixture-shared-opencode-key"
    global_path = home / ".config/opencode/opencode.jsonc"
    _write(global_path, json.dumps({"provider": {"openai": {"options": {"apiKey": shared}}}}))
    project_path = project / ".opencode/opencode.json"
    _write(project_path, json.dumps({"provider": {"openai": {
        "options": {"apiKey": shared, "baseURL": "ftp://relay.example/v1"},
    }}}))
    before = project_path.read_bytes()
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: (project,)
    _clean_importable(service)
    assert project_path.read_bytes() == before


def test_reauth_terminal_completion_still_binds_a_kept_keychain_key(monkeypatch, tmp_path):
    from core.handlers.model_hub.oauth import OAuthFlowState
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    keychain = FakeKeychain()
    monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
    _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
    account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
    locator = ("Codex Auth", account)
    keychain.items[locator] = (json.dumps({
        "OPENAI_API_KEY": "fixture-key-123456",
        "tokens": {
            "access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "account_id": "acct_fixture",
        },
    }), "fixture-original")
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["codex"].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]

    async def inconclusive(ref):
        raise RuntimeError("fixture inconclusive")

    adapter.validate_oauth_credential = inconclusive
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert service.migration_journal.load()["phase"] == "exposed"
    oauth_source = next(source for source in store.config.sources if source.kind == "subscription")

    async def start(source_id, vendor):
        return OAuthFlowState(
            flow_id="oaf_fixture123", source_id=source_id, vendor=vendor,
            state="awaiting_action", auth_url="https://fixture.example/authorize", device_code=None,
            expects=None, instructions_key=None, error_key=None,
            expires_at_iso="2099-01-01T00:00:00+00:00", credential_ref=None,
        )

    adapter.start_oauth = start
    asyncio.run(service.reauth_source(oauth_source.id, {"acknowledge_irreversible": True}))
    assert json.loads(keychain.items[locator][0]) == {"OPENAI_API_KEY": "fixture-key-123456"}
    [copy] = service.migration_journal.completed()["retained_native_ids"].values()
    payload = store.config.to_payload()
    payload["sources"] = [source for source in payload["sources"] if source["id"] != copy["source_id"]]
    for agent in payload["agents"].values():
        agent["sources"]["order"] = [value for value in agent["sources"]["order"] if value != copy["source_id"]]
        agent["routes"] = {}
    store.config = ModelHubConfig.from_payload(payload)
    assert [row["backend"] for row in service.migration_scan()["items"]] == ["codex"]


def test_key_cleanup_keeps_an_unselected_codex_login(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    tokens = {"access_token": "fixture-access-only"}
    _write(home / ".codex/auth.json", json.dumps({"OPENAI_API_KEY": "fixture-key-123456", "tokens": tokens}))
    _write(home / ".codex/config.toml", 'cli_auth_credentials_store = "file"\n')
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    scan = service.migration_scan()["items"]
    assert {row["kind"]: row["proposed_action"] for row in scan} == {
        "oauth_native": "keep_native", "api_key": "import",
    }
    ids = [row["id"] for row in scan if row["proposed_action"] == "import"]
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert json.loads((home / ".codex/auth.json").read_text())["tokens"] == tokens


def test_cleanup_retires_a_receipt_copied_under_an_earlier_route(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    auth, config = home / ".codex/auth.json", home / ".codex/config.toml"
    route_a = 'cli_auth_credentials_store = "file"\n'
    route_b = route_a + (
        'model_provider = "Relay"\n\n[model_providers.Relay]\n'
        'base_url = "https://relay.example/v1"\nwire_api = "responses"\n'
    )
    _write(auth, json.dumps({"OPENAI_API_KEY": "fixture-key-123456"}))
    _write(config, route_a)
    service, _store, _adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert service.migration_scan()["items"] == []
    _write(config, route_b)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert not auth.exists() or "OPENAI_API_KEY" not in json.loads(auth.read_text())
    # The key the user cleaned comes back under route A: it is offered again.
    _write(auth, json.dumps({"OPENAI_API_KEY": "fixture-key-123456"}))
    _write(config, route_a)
    assert [row["kind"] for row in service.migration_scan()["items"]] == ["api_key"]


def test_pending_journal_from_before_the_cleanup_option_resumes(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    _write_claude_oauth(home)
    service, _store, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    validate = adapter.validate_oauth_credential

    async def inconclusive(ref):
        raise RuntimeError("fixture inconclusive")

    adapter.validate_oauth_credential = inconclusive
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    record = service.migration_journal.load()
    assert record["phase"] != "complete"
    record.pop("clean_api_keys")
    service.migration_journal.save(record)
    adapter.validate_oauth_credential = validate
    assert asyncio.run(service.migration_apply(ids))["applied"] == len(ids)


@pytest.mark.parametrize("restore", ["same_row", "same_receipt_identity", "new_bytes"])
def test_a_key_restored_after_cleanup_can_be_copied_and_stays_hidden(monkeypatch, tmp_path, restore):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    auth, config = home / ".codex/auth.json", home / ".codex/config.toml"
    _write(auth, json.dumps({"OPENAI_API_KEY": "fixture-key-123456"}))
    _write(config, 'cli_auth_credentials_store = "file"\n')
    service, store, _adapter = _service(tmp_path, migration_home=home)
    first = [row["id"] for row in service.migration_scan()["items"]]
    assert asyncio.run(service.migration_apply(first, clean_api_keys=True))["applied"] == 1
    # A sync restores the key: the same row, the same receipt identity under
    # another file snapshot (cleanup dropped the store selector), or new bytes.
    indent = 2 if restore == "new_bytes" else None
    _write(auth, json.dumps({"OPENAI_API_KEY": "fixture-key-123456"}, indent=indent))
    if restore != "same_receipt_identity":
        _write(config, 'cli_auth_credentials_store = "file"\n')
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1 and (ids == first) == (restore == "same_row")
    assert asyncio.run(service.migration_apply(ids))["applied"] == 1
    assert json.loads(auth.read_text()) == {"OPENAI_API_KEY": "fixture-key-123456"}
    assert len(store.config.sources) == 1
    assert service.migration_scan()["items"] == []
