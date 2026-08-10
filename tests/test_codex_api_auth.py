"""Tests for ``vibe.api.get_codex_auth`` and ``save_codex_auth``.

Pins two contracts:

- ``get_codex_auth`` must forward the credentials-store fields so the
  Settings UI can render the keyring warning correctly.
- ``save_codex_auth`` must prefer the API key on disk over the
  V2Config cache when no key is supplied in the payload. Reversing this
  would let a stale cache silently overwrite a freshly-rotated key
  (e.g. one written by ``codex login --with-api-key`` outside our flow).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api  # noqa: E402


def _save_codex_base_url(
    avibe_home: str,
    base_url: str,
    attempting,
    finished,
) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    from storage.lock import MigrationFileLock
    from vibe import api as child_api
    from vibe import codex_config

    original_acquire = MigrationFileLock.acquire

    def signal_acquire(lock) -> None:
        attempting.set()
        original_acquire(lock)

    MigrationFileLock.acquire = signal_acquire
    codex_config.apply_codex_auth = lambda **kwargs: {}
    child_api.restart_backend = lambda name, **kwargs: {"ok": True}
    child_api.save_codex_auth({"auth_mode": "oauth", "base_url": base_url})
    finished.set()


def _save_codex_api_key(
    avibe_home: str,
    codex_home: str,
    api_key: str,
    attempting,
    finished,
) -> None:
    os.environ["AVIBE_HOME"] = avibe_home
    os.environ["CODEX_HOME"] = codex_home
    from storage.lock import MigrationFileLock
    from vibe import api as child_api

    original_acquire = MigrationFileLock.acquire

    def signal_acquire(lock) -> None:
        attempting.set()
        original_acquire(lock)

    MigrationFileLock.acquire = signal_acquire
    child_api.restart_backend = lambda name, **kwargs: {"ok": True}
    child_api.save_codex_auth(
        {
            "auth_mode": "api_key",
            "api_key": api_key,
            "base_url": "https://new.example.test/v1",
        }
    )
    finished.set()


def _seed_disk(home: Path, *, api_key: str | None, store: str | None) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    auth: dict = {}
    if api_key is not None:
        auth["OPENAI_API_KEY"] = api_key
    (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    toml = ""
    if store is not None:
        toml = f'cli_auth_credentials_store = "{store}"\n'
    (codex_home / "config.toml").write_text(toml, encoding="utf-8")


def test_get_codex_auth_forwards_credentials_store_fields(monkeypatch, tmp_path: Path) -> None:
    """The keyring-warning gate in SettingsCodexProviderPage reads both
    fields; dropping them silently caused incorrect warnings even when
    the store was already ``file``."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    _seed_disk(tmp_path, api_key="sk-disk", store="file")

    monkeypatch.setattr(api, "load_config", lambda: types.SimpleNamespace(agents=None))

    state = api.get_codex_auth()
    assert state["credentials_store"] == "file"
    assert state["file_store_active"] is True
    assert state["has_api_key"] is True


def test_get_codex_auth_defaults_store_to_auto_when_unset(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    _seed_disk(tmp_path, api_key=None, store=None)
    monkeypatch.setattr(api, "load_config", lambda: types.SimpleNamespace(agents=None))

    state = api.get_codex_auth()
    assert state["credentials_store"] == "auto"
    assert state["file_store_active"] is False


def test_save_codex_auth_prefers_disk_over_v2config_cache(monkeypatch, tmp_path: Path) -> None:
    """When the user clicks Save with only base_url filled, we reuse the
    stored key. If the cached V2Config key is stale (user rotated the
    key via ``codex login --with-api-key``), trusting the cache writes
    the old key back into ``auth.json`` — silently reverting working
    credentials. Disk must win."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    _seed_disk(tmp_path, api_key="sk-fresh-on-disk", store="file")

    # Cached V2Config carries a stale key.
    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-stale-from-cache", base_url=None
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    # Don't actually restart the backend in unit tests.
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    payload = {"auth_mode": "api_key", "api_key": None, "base_url": "https://example/v1"}
    result = api.save_codex_auth(payload)
    assert result.get("ok") is True

    # The disk write is authoritative — assert the key on disk is still
    # the freshly-rotated one, not the stale cache value.
    auth = json.loads((tmp_path / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-fresh-on-disk"
    # And the V2Config write should reflect the same (disk-sourced) key.
    assert fake_codex.api_key == "sk-fresh-on-disk"


def test_save_codex_auth_falls_back_to_v2config_when_disk_empty(
    monkeypatch, tmp_path: Path
) -> None:
    """Legacy installs may have never written ``auth.json``. The
    V2Config cache is still a valid fallback in that case."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    _seed_disk(tmp_path, api_key=None, store="file")

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-from-cache", base_url=None
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    payload = {"auth_mode": "api_key", "api_key": None, "base_url": "https://example/v1"}
    result = api.save_codex_auth(payload)
    assert result.get("ok") is True

    auth = json.loads((tmp_path / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-from-cache"


def test_save_codex_auth_preserved_fields_share_final_config_transaction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.services.settings import default_config
    from storage.lock import MigrationFileLock
    from vibe import codex_config

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    config = default_config()
    config.agents.codex.auth_mode = "oauth"
    config.agents.codex.base_url = "https://old.example.test/v1"
    config.save()
    context = multiprocessing.get_context("spawn")
    attempting = context.Event()
    finished = context.Event()
    concurrent = context.Process(
        target=_save_codex_base_url,
        args=(
            str(tmp_path),
            "https://new.example.test/v1",
            attempting,
            finished,
        ),
    )
    apply_entered = False
    observed_base_urls: list[str | None] = []

    def apply_codex_auth(**kwargs):
        nonlocal apply_entered
        apply_entered = True
        observed_base_urls.append(kwargs["base_url"])
        concurrent.start()
        assert attempting.wait(timeout=5)
        return {}

    original_acquire = MigrationFileLock.acquire

    def wait_for_concurrent_writer(lock) -> None:
        if apply_entered:
            assert finished.wait(timeout=5)
        original_acquire(lock)

    monkeypatch.setattr(codex_config, "apply_codex_auth", apply_codex_auth)
    monkeypatch.setattr(MigrationFileLock, "acquire", wait_for_concurrent_writer)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})
    try:
        result = api.save_codex_auth({"auth_mode": "oauth"})
        assert finished.wait(timeout=5)
    finally:
        concurrent.join(timeout=5)
        if concurrent.is_alive():
            concurrent.terminate()
            concurrent.join(timeout=5)

    assert result["ok"] is True
    assert concurrent.exitcode == 0
    assert observed_base_urls == ["https://old.example.test/v1"]
    assert api.load_config().agents.codex.base_url == "https://new.example.test/v1"


def test_remove_codex_api_key_serializes_credential_and_config_mutations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.services.settings import default_config
    from storage.lock import MigrationFileLock
    from vibe import codex_config

    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config = default_config()
    config.agents.codex.auth_mode = "api_key"
    config.agents.codex.api_key = "sk-old"
    config.agents.codex.base_url = "https://old.example.test/v1"
    config.save()
    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-old",
        base_url="https://old.example.test/v1",
    )
    context = multiprocessing.get_context("spawn")
    attempting = context.Event()
    finished = context.Event()
    concurrent = context.Process(
        target=_save_codex_api_key,
        args=(str(tmp_path), str(codex_home), "sk-new", attempting, finished),
    )
    original_apply = codex_config.apply_codex_auth
    original_acquire = MigrationFileLock.acquire
    interleaved = False

    def remove_then_interleave(**kwargs):
        nonlocal interleaved
        result = original_apply(**kwargs)
        interleaved = True
        concurrent.start()
        assert attempting.wait(timeout=5)
        return result

    def wait_for_concurrent_writer(lock) -> None:
        if interleaved:
            assert finished.wait(timeout=5)
        original_acquire(lock)

    monkeypatch.setattr(codex_config, "apply_codex_auth", remove_then_interleave)
    monkeypatch.setattr(MigrationFileLock, "acquire", wait_for_concurrent_writer)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})
    try:
        result = api.remove_backend_api_key("codex")
        assert finished.wait(timeout=5)
    finally:
        concurrent.join(timeout=5)
        if concurrent.is_alive():
            concurrent.terminate()
            concurrent.join(timeout=5)

    persisted = api.load_config()
    assert result["ok"] is True
    assert concurrent.exitcode == 0
    assert codex_config.read_codex_api_key() == "sk-new"
    assert persisted.agents.codex.auth_mode == "api_key"
    assert persisted.agents.codex.api_key == "sk-new"
    assert persisted.agents.codex.base_url == "https://new.example.test/v1"
