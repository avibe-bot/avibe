"""Persisted migration boundaries; never inspect real stores or launch a CLI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.handlers.model_hub import migration
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
)


@pytest.fixture(autouse=True)
def isolated_native_boundary(monkeypatch, tmp_path):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(migration, "read_native_oauth", lambda *args, **kwargs: None)
    return home


def write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )


def test_runtime_auth_alone_is_not_a_saved_configuration(monkeypatch, tmp_path):
    service, store, adapter = _service(tmp_path, migration_home=None)
    service.migration_home = None
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "fixture-runtime-not-an-import")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:12345")
    assert all(agent.mode == "hub" for agent in store.load().agents.values())
    assert service.migration_scan()["items"] == []
    assert adapter.provisioned == []


def test_mh_mig_005_runtime_auth_cannot_disable_saved_opencode_keys(
    monkeypatch, tmp_path, isolated_native_boundary,
):
    """MH-MIG-005: runtime auth cannot supply or block saved file migration."""
    home = isolated_native_boundary
    path = home / ".config/opencode/opencode.json"
    write(path, {"provider": {
        "anthropic": {"options": {"apiKey": "fixture-saved-anthropic"}},
        "openai": {"options": {"apiKey": "fixture-saved-openai"}},
    }})
    service, _, adapter = _service(tmp_path, migration_home=None)
    service.migration_home = None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-runtime-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-runtime-openai")
    rows = service.migration_scan()["items"]
    assert len(rows) == 2
    assert all(row["proposed_action"] == "import" and row["selected"] for row in rows)
    assert all(row["source_paths"] == [str(path)] for row in rows)
    result = asyncio.run(service.migration_apply([row["id"] for row in rows]))
    assert result["applied"] == 2
    assert len(adapter.provisioned) == 2
    assert "fixture-runtime" not in json.dumps(adapter.keys)
    assert all(not provider.get("options") for provider in json.loads(path.read_text())["provider"].values())
    assert os.environ["ANTHROPIC_API_KEY"] == "fixture-runtime-anthropic"
    assert os.environ["OPENAI_API_KEY"] == "fixture-runtime-openai"


def test_shell_saved_key_takeover_keeps_unrelated_bytes_and_runtime(
    monkeypatch, tmp_path, isolated_native_boundary,
):
    home = isolated_native_boundary
    profile = home / ".bashrc"
    retained = "# 我的终端设置\r\nexport EDITOR='vim'\r\n"
    write(profile, retained + (
        "export ANTHROPIC_AUTH_TOKEN='fixture-shell-key'\r\n"
        "export ANTHROPIC_BASE_URL='https://fixture.example'\r\n"
    ))
    service, store, adapter = _service(tmp_path, migration_home=home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-runtime-preserved")
    rows = service.migration_scan()["items"]
    assert len(rows) == 1 and rows[0]["proposed_action"] == "import"
    assert rows[0]["source_paths"] == [str(profile)]
    assert "fixture-shell-key" not in json.dumps(rows)
    ids = [row["id"] for row in rows]
    asyncio.run(service.migration_apply(ids))
    assert profile.read_bytes() == retained.encode()
    assert len(store.load().sources) == 1
    assert store.load().sources[0].base_url == "https://fixture.example"
    assert list(adapter.keys.values())[0][2] == "fixture-shell-key"
    assert service.migration_scan()["items"] == []
    asyncio.run(service.migration_apply(ids))
    assert len(adapter.provisioned) == 1
    assert os.environ["ANTHROPIC_API_KEY"] == "fixture-runtime-preserved"


def test_new_shell_layer_during_provision_cannot_escape_consent(
    monkeypatch, tmp_path, isolated_native_boundary,
):
    home = isolated_native_boundary
    profile = home / ".bashrc"
    write(profile, "export ANTHROPIC_API_KEY='fixture-original-key'\n")
    original = profile.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    provision = adapter.provision_credential

    async def new_layer(*args, **kwargs):
        ref = await provision(*args, **kwargs)
        write(home / ".profile", "export ANTHROPIC_API_KEY='fixture-new-key'\n")
        return ref

    monkeypatch.setattr(adapter, "provision_credential", new_layer)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert profile.read_bytes() == original
    assert "fixture-new-key" in (home / ".profile").read_text()
    assert not store.load().sources
    assert adapter.revoked


def test_shared_shell_key_requires_both_backend_consents(
    tmp_path, isolated_native_boundary,
):
    home = isolated_native_boundary
    profile = home / ".profile"
    write(profile, "export OPENAI_API_KEY='fixture-shared-key'\n")
    config_path = home / ".config/opencode/opencode.json"
    write(config_path, {"provider": {"openai": {"options": {"apiKey": "{env:OPENAI_API_KEY}"}}}})
    service, store, adapter = _service(tmp_path, migration_home=home)
    rows = service.migration_scan()["items"]
    assert {row["backend"] for row in rows} == {"codex", "opencode"}
    assert all(set(row["required_backends"]) == {"codex", "opencode"} for row in rows)
    before = profile.read_bytes(), config_path.read_bytes()
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply([row["id"] for row in rows if row["backend"] == "opencode"]))
    assert adapter.provisioned == []
    assert (profile.read_bytes(), config_path.read_bytes()) == before
    asyncio.run(service.migration_apply([row["id"] for row in rows]))
    assert profile.read_bytes() == b""
    assert "apiKey" not in json.dumps(json.loads(config_path.read_text()))
    assert len(store.load().sources) == 1
    source = store.load().sources[0]
    assert all(source.id in store.load().agents[backend].sources.order for backend in ("codex", "opencode"))


@pytest.mark.parametrize("has_grant", [False, True])
@pytest.mark.parametrize("boundary", ["install", "start"])
def test_external_noop_guard_change_never_owns_rollback_bytes(
    monkeypatch, tmp_path, isolated_native_boundary, has_grant, boundary,
):
    from core.handlers.model_hub.adapter import EngineEnsureResult, EngineHealth, EngineStatus
    from tests.test_native_oauth_store import FakeKeychain
    from vibe import native_oauth_store

    home = isolated_native_boundary
    settings = home / ".claude/settings.json"
    if has_grant:
        write(settings, {"env": {"ANTHROPIC_API_KEY": "fixture-before-exposure"}})
    else:
        keychain = FakeKeychain()
        monkeypatch.setenv("USER", "fixture-user")
        keychain.items[("Claude Code-credentials", "fixture-user")] = (
            '{"mcpOAuth":{"keep":"fixture"}}', "fixture-original",
        )
        monkeypatch.setattr(native_oauth_store, "_KEYCHAIN_STORE", keychain)
        monkeypatch.setattr(migration, "read_native_oauth", native_oauth_store.read_native_oauth)
    profile = home / ".zshrc"
    write(profile, "# original preference\n")
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.agents["claude"].mode = "direct"
    before_settings = settings.read_bytes() if settings.exists() else None
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    status = EngineStatus(EngineHealth.OK, "fixture", True, "127.0.0.1", 32199, None)

    async def install(**kwargs):
        if boundary == "install":
            profile.write_text("# external preference during install\n")
        return EngineEnsureResult(status, False)

    async def start():
        if boundary == "start":
            profile.write_text("# external preference during start\n")
        return status

    adapter.ensure_installed = install
    adapter.start = start
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert profile.read_text() == f"# external preference during {boundary}\n"
    assert service.migration_journal.completed() is None
    if has_grant and boundary == "start":
        assert service.migration_journal.load()["phase"] == "exposed"
        assert len(store.config.sources) == 1
        assert settings.read_bytes() != before_settings
        assert "claude" in service.migration_blocked_backends
    else:
        assert service.migration_journal.load() is None
        assert not store.config.sources
        assert store.config.agents["claude"].mode == "direct"
        assert not service.migration_blocked_backends
        assert (settings.read_bytes() if settings.exists() else None) == before_settings


@pytest.mark.parametrize("changed_identity", ["new-grant", "new-target", "replaced-hub-ref"])
def test_receipt_replay_requires_same_grant_target_and_current_hub_ref(
    monkeypatch, tmp_path, isolated_native_boundary, changed_identity,
):
    home = isolated_native_boundary
    path = home / ".claude/settings.json"
    payload = {"env": {
        "ANTHROPIC_AUTH_TOKEN": "fixture-original",
        "ANTHROPIC_BASE_URL": "https://fixture-original.example",
    }}
    write(path, payload)
    service, store, adapter = _service(tmp_path, migration_home=home)
    [original] = service.migration_scan()["items"]
    asyncio.run(service.migration_apply([original["id"]]))
    original_ref = store.config.sources[0].credential_ref
    if changed_identity == "new-grant":
        payload["env"]["ANTHROPIC_AUTH_TOKEN"] = "fixture-new"
    elif changed_identity == "new-target":
        payload["env"]["ANTHROPIC_BASE_URL"] = "https://fixture-changed.example"
    else:
        store.config.sources[0].credential_ref = "cred_changedfixture"
    write(path, payload)
    [current] = service.migration_scan()["items"]
    if changed_identity == "replaced-hub-ref":
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply([current["id"]]))
        assert len(adapter.provisioned) == 1
        assert json.loads(path.read_text()) == payload
    else:
        assert current["id"] != original["id"]
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply([original["id"]]))
        asyncio.run(service.migration_apply([current["id"]]))
        assert len(adapter.provisioned) == 2
        assert store.config.sources[0].credential_ref == original_ref
        assert len(store.config.sources) == 2
