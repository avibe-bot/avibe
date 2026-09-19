"""Takeover discovers and cleans all supported precedence layers as one unit."""

import asyncio
import json

import pytest

from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
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
    service, store, adapter = _service(tmp_path)
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
    service, _, adapter = _service(tmp_path)
    ids = [item["id"] for item in service.migration_scan()["items"]]
    assert len(ids) == 2
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids[:1]))
    assert adapter.provisioned == []
    assert asyncio.run(service.migration_apply(ids))["applied"] == 2
    assert len(adapter.keys) == 2
    assert json.loads(auth_path.read_text()) == {}


def test_unsupported_provider_blocks_only_its_cli_before_any_cleanup(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    path = home / ".local/share/opencode/auth.json"
    _write(path, json.dumps({
        "openai": {"type": "api", "key": "fixture-key"},
        "github-copilot": {"type": "oauth", "refresh": "fixture-refresh"},
    }))
    before = path.read_bytes()
    service, _, adapter = _service(tmp_path)
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
    service, _, adapter = _service(tmp_path)
    service.migration_project_roots = lambda: (project,)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    _write(path, json.dumps({"env": {"ANTHROPIC_API_KEY": "fixture-second"}}))
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert adapter.provisioned == []
    assert json.loads(path.read_text())["env"]["ANTHROPIC_API_KEY"] == "fixture-second"


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
    service, _, adapter = _service(tmp_path)
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
    service, _, adapter = _service(tmp_path)
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
