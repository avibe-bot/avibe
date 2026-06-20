"""Contract tests for ``core.services.settings``.

C3 of Plan 1. Pins the public surface so the CLI and UI server can both
import from one place and the legacy ``V2Config.load`` / ``SettingsStore.
get_instance`` divergence stays gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SettingsStore
from config.v2_config import V2Config
from core.services import settings as settings_service


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    yield tmp_path
    SettingsStore.reset_instance()


def test_public_surface_is_stable():
    expected = {
        "default_config",
        "load_config",
        "load_config_or_default",
        "get_settings_store",
        "reload_settings_store",
        "reset_settings_store",
    }
    assert set(settings_service.__all__) == expected
    for name in expected:
        assert callable(getattr(settings_service, name))


def test_load_config_requires_file_by_default(isolated_state):
    with pytest.raises(FileNotFoundError):
        settings_service.load_config()


def test_default_config_is_fresh_and_needs_setup():
    # The shared fresh-install factory must never look like a finished setup:
    # no credentials, ``setup_completed`` False, ``needs_setup`` True.
    config = settings_service.default_config()
    assert config.mode == "self_host"
    assert config.setup_completed is False
    assert config.configured_platforms() == []
    assert config.setup_state()["needs_setup"] is True
    # Workbench-only first-run state: no external IM enabled, primary anchored
    # to the workbench. Guards against the PlatformsConfig ["slack"] dataclass
    # default leaking in and persisting a phantom Slack transport on skip.
    assert config.platforms.enabled == []
    assert config.platforms.primary == "avibe"


def test_load_config_or_default_returns_default_without_persisting(isolated_state, tmp_path):
    # The read-side default backs GET /api/config on a fresh install: the
    # setup wizard (and the reused provider-config modal that calls
    # getConfig()) must load before any config file exists — without a raise
    # and without turning the read into a write.
    target = tmp_path / "config.json"
    assert not target.exists()

    config = settings_service.load_config_or_default(target)

    assert config.setup_state()["needs_setup"] is True
    assert config.setup_completed is False
    assert not target.exists(), "reading a missing config must not create the file"


def test_load_config_or_default_reads_disk_when_present(isolated_state, tmp_path):
    # Once a real config exists, the on-disk value wins (here: a completed
    # setup is reported as completed, never overwritten by the fresh default).
    target = tmp_path / "config.json"
    seeded = settings_service.default_config()
    seeded.setup_completed = True
    seeded.save(target)

    config = settings_service.load_config_or_default(target)

    assert config.setup_completed is True
    assert config.setup_state()["needs_setup"] is False


def test_load_config_seeds_default_when_factory_given(isolated_state, tmp_path):
    target = tmp_path / "config.json"

    def _factory() -> V2Config:
        # Minimal valid V2Config — mirrors what CLI's _default_config does.
        from config.v2_config import (
            AgentsConfig,
            ClaudeConfig,
            CodexConfig,
            OpenCodeConfig,
            RuntimeConfig,
            SlackConfig,
        )

        return V2Config(
            mode="self_host",
            version="v2",
            slack=SlackConfig(bot_token="", app_token=""),
            runtime=RuntimeConfig(default_cwd=str(tmp_path / "work")),
            agents=AgentsConfig(
                default_backend="opencode",
                opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
                claude=ClaudeConfig(enabled=True, cli_path="claude"),
                codex=CodexConfig(enabled=False, cli_path="codex"),
            ),
        )

    assert not target.exists()
    config = settings_service.load_config(target, default_factory=_factory)
    assert target.exists(), "factory result should be persisted to disk"
    assert config.version == "v2"
    # Reload returns the persisted file, not a fresh factory invocation.
    again = settings_service.load_config(target)
    assert again.version == "v2"


def test_get_settings_store_returns_singleton(isolated_state):
    a = settings_service.get_settings_store()
    b = settings_service.get_settings_store()
    assert a is b


def test_reset_settings_store_drops_singleton(isolated_state):
    a = settings_service.get_settings_store()
    settings_service.reset_settings_store()
    b = settings_service.get_settings_store()
    assert a is not b, "reset must release the previous singleton"


def test_reload_settings_store_returns_same_instance(isolated_state):
    a = settings_service.get_settings_store()
    b = settings_service.reload_settings_store()
    assert a is b
