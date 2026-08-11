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
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api  # noqa: E402


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


def test_save_codex_auth_blocks_recovery_before_external_mutation(monkeypatch) -> None:
    fake_config = types.SimpleNamespace(load_warnings=("recovery required",))
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    applied: list[dict] = []
    monkeypatch.setattr(
        "vibe.codex_config.apply_codex_auth",
        lambda **kwargs: applied.append(kwargs),
    )

    result = api.save_codex_auth(
        {"auth_mode": "api_key", "api_key": "sk-new", "base_url": "https://example.invalid"}
    )

    assert result["ok"] is False
    assert "recovery warnings" in result["message"]
    assert applied == []


def test_remove_codex_api_key_blocks_recovery_before_external_mutation(monkeypatch) -> None:
    fake_config = types.SimpleNamespace(load_warnings=("recovery required",))
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    applied: list[dict] = []
    monkeypatch.setattr(
        "vibe.codex_config.apply_codex_auth",
        lambda **kwargs: applied.append(kwargs),
    )

    result = api.remove_backend_api_key("codex")

    assert result == {
        "ok": False,
        "error": "config_recovery",
        "message": "Config was loaded with recovery warnings; repair the backed-up config before changing backend credentials",
    }
    assert applied == []
