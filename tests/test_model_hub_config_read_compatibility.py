"""A newer config observer must not invalidate an older running Hub reader."""

from __future__ import annotations

import json
import stat

import pytest

from config import paths
from config.v2_config import ModelHubConfig, ModelHubRouteConfig, ModelHubRouteHopConfig, V2Config
from core.services import settings
from tests.test_model_hub_resolution import _service, _source
from vibe import api, cli, runtime, upgrade


@pytest.fixture(autouse=True)
def forbid_live_boundaries(monkeypatch):
    import socket
    import subprocess

    import psutil

    from vibe.native_oauth_store import _SecurityKeychainStore

    def forbidden(*args, **kwargs):
        raise AssertionError("No real process, network, or native credentials in config fixtures")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for method in ("Process", "pids", "process_iter"):
        monkeypatch.setattr(psutil, method, forbidden)
    for method in ("metadata", "read", "write", "delete"):
        monkeypatch.setattr(_SecurityKeychainStore, method, forbidden)


def legacy_config(*, enabled=True):
    config = V2Config.default()
    model_id = config.model_hub.agents["codex"].models[0].id
    source = _source("src_fixture409", (model_id,), vendor="openai")
    source.display_name = "升级前的合成来源"
    config.model_hub.sources = [source]
    config.model_hub.agents["codex"].sources.order = [source.id]
    config.model_hub.agents["codex"].routes[model_id] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, model_id),),
    )
    path = paths.get_config_path()
    config.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_hub"].pop("runtime_default_applied")
    payload["model_hub"]["enabled"] = enabled
    payload["unrelated_fixture"] = {"keep": "共享文件不能被观察者改写"}
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, model_id


def legacy_service_read(path, monkeypatch):
    """Retain the shipped 3.1.0rc3 top-level Hub field contract.

    Only the old parser's rejection rule is substituted; disk recovery and the
    invocation consumer remain real. An installed historical-reader replay
    additionally checks the full old package outside the portable unit suite.
    """
    parse = ModelHubConfig.from_payload

    def old_parse(cls, payload, **kwargs):
        if isinstance(payload, dict) and set(payload) - {"enabled", "sources", "agents"}:
            raise ValueError("Config 'model_hub' contains unknown fields")
        return parse(payload, **kwargs)

    with monkeypatch.context() as old:
        old.setattr(ModelHubConfig, "from_payload", classmethod(old_parse))
        return V2Config.load(path, persist_migrations=False)


READERS = (
    "loader",
    "settings",
    "settings_or_default",
    "cli_platform",
    "cli_enabled_platforms",
    "cli_show_target",
    "api",
    "runtime_ensure",
    "upgrade_preflight",
)


def observe(reader, monkeypatch):
    if reader == "loader":
        V2Config.load()
    elif reader == "settings":
        settings.load_config()
    elif reader == "settings_or_default":
        settings.load_config_or_default()
    elif reader == "cli_platform":
        cli._primary_platform()
    elif reader == "cli_enabled_platforms":
        cli._supported_task_platforms()
    elif reader == "cli_show_target":
        monkeypatch.setattr(runtime, "read_status", lambda: {})
        cli._local_show_events_targets("ses_fixture")
    elif reader == "api":
        api.load_config()
    elif reader == "runtime_ensure":
        runtime.ensure_config()
    else:
        assert reader == "upgrade_preflight"
        upgrade.configured_memory_enabled()


@pytest.mark.parametrize("reader", READERS)
def test_new_observer_keeps_old_invocation_resolvable(tmp_path, monkeypatch, reader):
    path, model_id = legacy_config()
    original = path.read_bytes()
    before = legacy_service_read(path, monkeypatch)
    assert before.load_warnings == ()
    service, _, _ = _service(tmp_path, before.model_hub)
    first = service._invocation_resolution(before.model_hub, "codex", model_id)
    assert first.source.id == "src_fixture409"

    observe(reader, monkeypatch)

    after = legacy_service_read(path, monkeypatch)
    resumed = service._invocation_resolution(after.model_hub, "codex", model_id)
    assert resumed.source.id == first.source.id
    assert resumed.target_model == first.target_model
    assert after.load_warnings == ()
    assert path.read_bytes() == original
    assert not list(path.parent.glob("config.json.bak-*"))


@pytest.mark.parametrize("reader", READERS)
def test_new_observer_does_not_commit_legacy_disabled_intent(monkeypatch, reader):
    path, _ = legacy_config(enabled=False)
    original = path.read_bytes()

    observe(reader, monkeypatch)

    assert path.read_bytes() == original
    assert not list(path.parent.glob("config.json.bak-*"))


@pytest.mark.parametrize("shape", ("enabled", "disabled", "missing-enabled", "missing-hub"))
def test_readers_do_not_activate_an_uncommitted_runtime_default(shape):
    from core.handlers.model_hub.service import V2ModelHubConfigStore

    path, _ = legacy_config(enabled=shape == "enabled")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if shape == "missing-enabled":
        payload["model_hub"].pop("enabled")
    elif shape == "missing-hub":
        payload.pop("model_hub")
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()

    assert V2Config.load().model_hub.enabled is (shape == "enabled")
    assert V2ModelHubConfigStore().load().enabled is (shape == "enabled")
    assert path.read_bytes() == original


def test_failed_startup_commit_cannot_activate_on_next_store_read(monkeypatch):
    import main

    from config import v2_config
    from core.handlers.model_hub.service import V2ModelHubConfigStore

    path, _ = legacy_config(enabled=False)
    original = path.read_bytes()
    monkeypatch.setattr(v2_config, "_backup_config_file", lambda *args, **kwargs: None)

    startup = main.load_config()

    assert startup.load_warnings
    assert startup.model_hub.enabled is False
    assert V2ModelHubConfigStore().load().enabled is False
    assert path.read_bytes() == original


def test_service_boot_explicitly_commits_once_and_preserves_stop(monkeypatch):
    import main

    from config.v2_config import update_config_fields

    path, _ = legacy_config(enabled=False)
    original = path.read_bytes()
    loaded = main.load_config()
    assert loaded.model_hub.enabled is True
    assert loaded.model_hub.runtime_default_applied is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["model_hub"]["runtime_default_applied"] is True
    assert persisted["unrelated_fixture"] == json.loads(original)["unrelated_fixture"]
    [backup] = path.parent.glob("config.json.bak-model-hub-migration-*")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    update_config_fields(lambda config: setattr(config.model_hub, "enabled", False))
    stopped = path.read_bytes()
    for _ in range(2):
        assert V2Config.load().model_hub.enabled is False
        assert main.load_config().model_hub.enabled is False
        assert path.read_bytes() == stopped
    assert len(list(path.parent.glob("config.json.bak-*"))) == 1


def test_rejected_service_cannot_reach_migration(monkeypatch):
    import main

    path, _ = legacy_config()
    original = path.read_bytes()

    def occupied():
        raise runtime.ServiceAlreadyRunningError(lock_path=path.parent / "fixture.lock", holder_pid=42)

    monkeypatch.setattr(main, "acquire_service_instance_lock", occupied)
    with pytest.raises(SystemExit):
        main.main()
    assert path.read_bytes() == original
    assert not list(path.parent.glob("config.json.bak-*"))
