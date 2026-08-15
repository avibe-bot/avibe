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


def test_save_codex_auth_rescues_disk_base_url_when_cache_empty(
    monkeypatch, tmp_path: Path
) -> None:
    """Auth-mode round-trip regression. The oauth pass clears the
    managed relay pointer, and installs that configured their relay
    directly in ``config.toml`` never carry a ``base_url`` in V2Config.
    A later api_key save without ``base_url`` in the payload must keep
    using the disk relay URL — dropping it sends the relay key to
    ``api.openai.com`` and every turn 401s until the user repairs
    ``config.toml`` by hand. Disk must win over an empty cache.

    The orphaned variant (OAuth flow cleared the ``model_provider``
    pointer too, so the relay section is unpointed) is recovered by the
    same read path — see the sibling tests in
    ``test_settings_disk_fallback.py``."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model_provider = "OpenAI"\n'
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-any", base_url=None
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    # No base_url in the payload: the disk relay URL must survive.
    payload = {"auth_mode": "api_key", "api_key": "sk-relay"}
    result = api.save_codex_auth(payload)
    assert result.get("ok") is True

    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'base_url = "https://relay.example/v1"' in toml
    # The rescued value is also mirrored into the V2Config cache so the
    # next save (and the Settings form) starts from it.
    assert fake_codex.base_url == "https://relay.example/v1"


def test_save_codex_auth_prefers_disk_base_url_over_stale_cache(
    monkeypatch, tmp_path: Path
) -> None:
    """A relay URL hand-edited in ``config.toml`` outranks a stale
    V2Config cache, mirroring the api-key disk-first contract."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model_provider = "OpenAI"\n'
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://fresh.example/v1"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-any", base_url="https://stale.example/v1"
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-relay"})
    assert result.get("ok") is True

    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'base_url = "https://fresh.example/v1"' in toml
    assert fake_codex.base_url == "https://fresh.example/v1"


def test_save_codex_auth_restores_relay_from_oauth_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """Post-OAuth recovery via the explicit transition marker. The OAuth
    transition cleared the provider pointer and dropped the managed
    section (disk chain empty) but recorded the relay identity in
    ``oauth_relay_marker``. An api_key save omitting ``base_url`` must
    restore the relay from the marker — this is the exact Settings
    round-trip that used to 401 — and consume (clear) the marker."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": "x"}, "auth_mode": "chatgpt"}),
        encoding="utf-8",
    )
    # Pointer cleared by the OAuth pass; the relay section survives
    # orphaned so the restore can re-point at it. The file credential
    # store is pinned by the API-key save that configured the relay and
    # survives the OAuth transition — without it the live mode is
    # unknowable and the marker is gated off.
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://relay.example/v1"},
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-relay"})
    assert result.get("ok") is True

    # The captured user-owned provider is restored (pointer + its own
    # settings survive); the one-shot recovery record is spent.
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    pointer = [line for line in toml.splitlines() if line.startswith("model_provider")]
    assert pointer == ['model_provider = "OpenAI"']
    assert 'base_url = "https://relay.example/v1"' in toml
    assert fake_codex.oauth_relay_marker is None
    assert fake_codex.base_url == "https://relay.example/v1"


def test_save_codex_auth_ignores_marker_after_external_api_key_switch(
    monkeypatch, tmp_path: Path
) -> None:
    """``codex login --with-api-key`` outside Avibe puts a live API key
    on disk; from that moment the live disk configuration is
    authoritative and a leftover OAuth marker must not reroute the
    freshly logged-in key to the relay it remembers."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-official"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://relay.example/v1"},
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    state = api.get_codex_auth()
    assert state["base_url"] is None

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-official"})
    assert result.get("ok") is True
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "https://relay.example/v1" not in toml


def test_get_codex_auth_uses_oauth_marker_when_disk_chain_empty(
    monkeypatch, tmp_path: Path
) -> None:
    """The Settings form pre-populates from ``get_codex_auth``; after an
    OAuth transition the disk chain is empty, so the explicit marker is
    what keeps the Base URL field (and therefore the next explicit save
    payload) carrying the relay."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    # Live OAuth credentials on disk (file store): the marker gate
    # requires them.
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": "x"}, "auth_mode": "chatgpt"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\nmodel = "gpt-5.4"\n', encoding="utf-8"
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "openai-managed", "base_url": "https://relay.example/v1"},
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)

    state = api.get_codex_auth()
    assert state["base_url"] == "https://relay.example/v1"

    # No marker → dormant sections and stale caches surface nothing.
    fake_codex.oauth_relay_marker = None
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    state = api.get_codex_auth()
    assert state["base_url"] is None


def test_get_codex_auth_ignores_stale_cache_without_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """A plain cached ``base_url`` is a preference, not a recovery
    record: when the disk no longer carries the relay the cache must not
    resurrect it. Only the explicit OAuth-transition marker recovers."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url="https://removed.example/v1",
        oauth_relay_marker=None,
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)

    state = api.get_codex_auth()
    assert state["base_url"] is None


def test_save_codex_auth_blocks_recovery_before_external_mutation(monkeypatch) -> None:
    fake_config = types.SimpleNamespace(load_warnings=("recovery required",), language="zh")
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
    assert result["error"] == "config_recovery"
    assert "配置加载时发生了恢复" in result["message"]
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


def test_remove_backend_api_key_clears_codex_relay_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """Remove key is an explicit OAuth choice: the relay marker must go
    with the key, or the next Settings refresh repopulates the abandoned
    relay and a freshly entered official key gets rerouted to it."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-official"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key",
        api_key="sk-official",
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://stale.example/v1"},
    )
    fake_agents = types.SimpleNamespace(codex=fake_codex)
    fake_config = types.SimpleNamespace(agents=fake_agents, save=lambda: None)
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.remove_backend_api_key("codex")
    assert result.get("ok") is True
    assert fake_codex.api_key is None
    assert fake_codex.oauth_relay_marker is None


def test_marker_ignored_when_credentials_may_live_in_keyring(
    monkeypatch, tmp_path: Path
) -> None:
    """With ``cli_auth_credentials_store`` at ``auto``/``keyring`` an
    external official-key switch leaves no ``OPENAI_API_KEY`` in
    ``auth.json`` (it lives in the OS keychain), so the file-only
    ``has_api_key`` gate can't see it. The marker must not be consumed
    while the store hides the live mode — otherwise Settings
    pre-populates the abandoned relay and sends a pasted official key
    to it."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    # Keyring store: no file key, no tokens — live mode unknowable.
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "auto"\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://stale.example/v1"},
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    state = api.get_codex_auth()
    assert state["base_url"] is None

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-official"})
    assert result.get("ok") is True
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "https://stale.example/v1" not in toml


def test_marker_ignored_after_external_logout_clears_tokens(
    monkeypatch, tmp_path: Path
) -> None:
    """#1453: ``codex logout`` during the OAuth window clears the tokens;
    with no key and no tokens on disk the user has signed out of the
    relay entirely, and the marker must surface nothing in either
    consumer."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    # Signed out: no key, no tokens; file store still pinned.
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://stale.example/v1"},
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    state = api.get_codex_auth()
    assert state["base_url"] is None

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-official"})
    assert result.get("ok") is True
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "https://stale.example/v1" not in toml


def test_save_codex_auth_oauth_captures_relay_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """#1449: an ``auth_mode="oauth"`` save through the Settings API
    (the non-React client path) captures the live relay identity before
    the cleanup destroys it, with controller-path retention semantics."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-relay"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model_provider = "OpenAI"\n'
        'cli_auth_credentials_store = "file"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-relay", base_url=None, oauth_relay_marker=None
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})
    persisted: list[dict | None] = []
    import vibe.codex_config as codex_config_module

    monkeypatch.setattr(
        codex_config_module,
        "persist_codex_relay_marker",
        lambda marker: persisted.append(marker) or True,
    )

    result = api.save_codex_auth({"auth_mode": "oauth"})
    assert result.get("ok") is True

    # Durable pre-persist fired before the cleanup…
    assert persisted == [{"provider_id": "OpenAI", "base_url": "https://relay.example/v1"}]
    # …and the owning V2Config write mirrors it.
    assert fake_codex.oauth_relay_marker == {
        "provider_id": "OpenAI",
        "base_url": "https://relay.example/v1",
    }
    # The cleanup did run (pointer cleared).
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert not [line for line in toml.splitlines() if line.startswith("model_provider")]


def test_save_codex_auth_oauth_official_key_transition_clears_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """Retention semantics on the direct path: an OAuth save while the
    disk shows an official API key with no relay clears the stale
    marker."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-official"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\nmodel = "gpt-5.4"\n', encoding="utf-8"
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://stale.example/v1"},
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.save_codex_auth({"auth_mode": "oauth"})
    assert result.get("ok") is True
    assert fake_codex.oauth_relay_marker is None


def test_save_codex_auth_keeps_live_provider_when_urls_match_marker(
    monkeypatch, tmp_path: Path
) -> None:
    """Same-URL provider swap: the user activates provider B (same relay
    URL as the marker's A) during the OAuth window. The disk chain
    supplies the URL, so the restore hint must NOT fire — B's pointer
    and settings stay."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": "x"}, "auth_mode": "chatgpt"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model_provider = "ProviderB"\n'
        'cli_auth_credentials_store = "file"\n'
        "\n"
        "[model_providers.ProviderB]\n"
        'name = "B"\n'
        'base_url = "https://relay.example/v1"\n'
        'wire_api = "chat"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )

    fake_codex = types.SimpleNamespace(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        oauth_relay_marker={"provider_id": "OpenAI", "base_url": "https://relay.example/v1"},
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=lambda: None
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.save_codex_auth({"auth_mode": "api_key", "api_key": "sk-relay"})
    assert result.get("ok") is True
    toml = (codex_home / "config.toml").read_text(encoding="utf-8")
    pointer = [line for line in toml.splitlines() if line.startswith("model_provider")]
    assert pointer == ['model_provider = "ProviderB"']


def test_remove_backend_api_key_reports_v2_clear_failure(
    monkeypatch, tmp_path: Path
) -> None:
    """#1451: when the post-removal V2Config save fails, the response
    must say so instead of silently claiming a clean wipe."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-official"}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    def boom():
        raise OSError("disk full")

    fake_codex = types.SimpleNamespace(
        auth_mode="api_key", api_key="sk-official", base_url=None, oauth_relay_marker=None
    )
    fake_config = types.SimpleNamespace(
        agents=types.SimpleNamespace(codex=fake_codex), save=boom
    )
    monkeypatch.setattr(api, "load_config", lambda: fake_config)
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": True})

    result = api.remove_backend_api_key("codex")
    assert result.get("ok") is True
    assert result["notices"][0]["code"] == "v2_clear_failed"
    assert "disk full" in result["notices"][0]["detail"]
    # The disk key removal itself did happen.
    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert "OPENAI_API_KEY" not in auth
