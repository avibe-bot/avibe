"""Regression tests for ``vibe/codex_config.py`` TOML round-tripping.

The Codex CLI's ``config.toml`` carries arbitrary user-owned blocks
(``[projects."/abs/path"]`` scopes, deeply nested settings, arrays of
tables). When we save Codex auth state we must preserve those unrelated
sections rather than silently dropping them. These tests pin the
round-trip behavior of the emitter.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore[no-redef]

from vibe import codex_config


def _round_trip(data: dict) -> dict:
    return tomllib.loads(codex_config._dump_toml(data))


def test_round_trip_quoted_keys() -> None:
    """Keys outside the bare-key character class must be emitted quoted."""
    data = {
        "model_provider": "openai",
        "projects": {
            "/Users/alex/code/vibe-remote": {"trust_level": "trusted"},
            "/tmp/another path": {"trust_level": "ask"},
        },
    }
    rendered = codex_config._dump_toml(data)
    assert '"/Users/alex/code/vibe-remote"' in rendered
    assert '"/tmp/another path"' in rendered
    assert _round_trip(data) == data


def test_round_trip_deep_nesting() -> None:
    """Nesting deeper than two levels must survive the rewrite."""
    data = {
        "a": {"b": {"c": {"x": 1, "y": "hello", "z": True}}},
        "top_level": "kept",
    }
    assert _round_trip(data) == data


def test_round_trip_arrays_of_tables() -> None:
    """``[[plugin]]`` array-of-tables entries must round-trip intact."""
    data = {
        "plugin": [
            {"name": "first", "enabled": True},
            {"name": "second", "settings": {"timeout": 30}},
        ],
    }
    assert _round_trip(data) == data


def test_apply_api_key_mode_preserves_unrelated_blocks(tmp_path: Path) -> None:
    """Switching to api_key mode must not drop user-owned config blocks."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        'model = "gpt-5"\n'
        "\n"
        '[projects."/Users/alex/code/vibe-remote"]\n'
        'trust_level = "trusted"\n'
        "\n"
        "[a.b.c]\n"
        "x = 1\n"
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")

    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-test-1234567890",
        base_url="https://api.example.com/v1",
        home=home,
    )

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"
    assert parsed["model_provider"] == codex_config.MANAGED_PROVIDER_ID
    # Managed section now lives under the non-reserved id since
    # newer Codex versions refuse to load configs that override the
    # built-in ``openai`` provider.
    assert (
        parsed["model_providers"][codex_config.MANAGED_PROVIDER_ID]["base_url"]
        == "https://api.example.com/v1"
    )
    assert "openai" not in parsed.get("model_providers", {})
    assert parsed["projects"]["/Users/alex/code/vibe-remote"]["trust_level"] == "trusted"
    assert parsed["a"]["b"]["c"]["x"] == 1

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-test-1234567890"


def test_apply_purges_legacy_reserved_openai_section(tmp_path: Path) -> None:
    """Older releases wrote our managed shape under
    ``[model_providers.openai]``. Newer Codex versions reject that
    (built-in provider), so any save / oauth-switch must purge the
    legacy section even when we'd otherwise leave provider blocks
    alone."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        'model_provider = "openai"\n'
        "\n"
        "[model_providers.OpenAI]\n"  # user's custom (TitleCase) relay
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n'
        "\n"
        "[model_providers.openai]\n"  # garbage we wrote previously
        'name = "OpenAI"\n'
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")

    codex_config.apply_codex_auth(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        home=home,
    )
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    providers = parsed.get("model_providers", {})
    # Reserved-name section is gone; user's TitleCase relay survives.
    assert "openai" not in providers
    assert providers["OpenAI"]["base_url"] == "https://relay.example/v1"
    # Our previously-set top-level pointer (``model_provider = "openai"``)
    # is no longer ours to own — reverted because it pointed at the
    # legacy managed id, freeing the user to choose their relay name.
    assert "model_provider" not in parsed


def test_apply_preserves_user_owned_model_provider(tmp_path: Path) -> None:
    """When the user has aimed ``model_provider`` at their own custom
    section (e.g. a TitleCase relay), an api_key save must not overwrite
    that pointer — overriding it would silently bypass their relay
    config and route through Codex's built-in ``openai`` provider."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        'model_provider = "OpenAI"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://relay.example/v1"\n'
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")

    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-test-userowned",
        base_url=None,
        home=home,
    )
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    # User's pointer + section survive intact.
    assert parsed["model_provider"] == "OpenAI"
    assert parsed["model_providers"]["OpenAI"]["base_url"] == "https://relay.example/v1"


def test_apply_api_key_with_base_url_pins_supports_websockets_off(tmp_path: Path) -> None:
    """Custom relays don't speak Codex's WSS responses protocol.

    Newer Codex versions otherwise dispatch the WebSocket transport via
    the built-in OpenAI provider's default ``wss://api.openai.com/...``
    URL — silently bypassing the configured relay and producing 401s.
    """
    home = tmp_path
    (home / ".codex").mkdir()

    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-relay-key",
        base_url="https://relay.example.com",
        home=home,
    )

    parsed = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    managed = parsed["model_providers"][codex_config.MANAGED_PROVIDER_ID]
    assert managed["base_url"] == "https://relay.example.com"
    assert managed["supports_websockets"] is False


def test_apply_api_key_without_base_url_strips_supports_websockets(tmp_path: Path) -> None:
    """Clearing a previously-set relay must remove our WSS pin.

    Otherwise a user who switches back to the official OpenAI endpoint
    would stay on the HTTP path forever, losing the WSS transport that
    works correctly against ``api.openai.com``.
    """
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        f'model_provider = "{codex_config.MANAGED_PROVIDER_ID}"\n'
        "\n"
        f"[model_providers.{codex_config.MANAGED_PROVIDER_ID}]\n"
        'base_url = "https://relay.example.com"\n'
        "supports_websockets = false\n"
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")

    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-no-relay",
        base_url=None,
        home=home,
    )

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    managed = parsed["model_providers"][codex_config.MANAGED_PROVIDER_ID]
    assert "base_url" not in managed
    assert "supports_websockets" not in managed


def test_round_trip_datetime_scalars() -> None:
    """``tomllib`` returns datetime/date/time for temporal TOML values; the
    emitter must round-trip them unquoted instead of crashing on the
    JSON fallback (datetimes are not JSON-serializable)."""
    data = {
        "created_at": _dt.datetime(2024, 1, 15, 9, 30, 0),
        "expires_on": _dt.date(2025, 12, 31),
        "daily_window": _dt.time(9, 0, 0),
    }
    parsed = _round_trip(data)
    assert parsed == data


def test_get_codex_home_honors_env(monkeypatch, tmp_path: Path) -> None:
    """``CODEX_HOME`` points directly at the Codex data dir (matches the
    rest of the integration in modules/agents/codex/agent.py)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "alt"))
    assert codex_config.get_codex_home() == tmp_path / "alt"
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # When ``home`` is injected (test fixture path), CODEX_HOME must not
    # override it — fixture isolation has to win.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored"))
    assert codex_config.get_codex_home(tmp_path) == tmp_path / ".codex"


def test_apply_codex_auth_respects_codex_home(monkeypatch, tmp_path: Path) -> None:
    """End-to-end: with CODEX_HOME set, apply_codex_auth writes there and
    leaves ``~/.codex`` untouched. This is the bug the live Codex process
    would otherwise hit — saved key in one dir, read from another."""
    alt_home = tmp_path / "alt-codex"
    monkeypatch.setenv("CODEX_HOME", str(alt_home))
    # Sanity: the real $HOME/.codex must not exist or, if it does, we
    # leave the test environment alone — we only assert against alt_home.
    codex_config.apply_codex_auth(auth_mode="api_key", api_key="sk-env", base_url=None)
    assert (alt_home / "auth.json").exists()
    assert (alt_home / "config.toml").exists()
    auth = json.loads((alt_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["OPENAI_API_KEY"] == "sk-env"


def test_read_codex_api_key_returns_stored_value(tmp_path: Path) -> None:
    """The base-URL-only update path in vibe/api.py depends on this."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-from-disk"}), encoding="utf-8"
    )
    assert codex_config.read_codex_api_key(home=home) == "sk-from-disk"


def test_read_codex_api_key_missing_returns_none(tmp_path: Path) -> None:
    assert codex_config.read_codex_api_key(home=tmp_path) is None


def test_round_trip_mixed_array_with_inline_tables() -> None:
    """``contributors = ["foo", { name = "bar" }]`` and similar mixed
    arrays must keep the dict element as a TOML inline table — falling
    through to ``json.dumps`` previously converted it into a quoted
    JSON string, silently corrupting valid user-owned config."""
    data = {
        "contributors": ["foo", {"name": "bar", "role": "maintainer"}],
        "plugins": [{"name": "first"}, {"name": "second", "enabled": True}, "raw-string"],
    }
    assert _round_trip(data) == data


def test_round_trip_nested_inline_table() -> None:
    """Inline tables can nest other inline tables."""
    data = {"settings": ["a", {"nested": {"x": 1, "y": "two"}}]}
    assert _round_trip(data) == data


def test_apply_api_key_pins_credentials_store_to_file(tmp_path: Path) -> None:
    """Codex's default ``cli_auth_credentials_store`` is ``auto`` (keyring-
    preferred). When the UI writes an API key, the live process must
    actually read from ``auth.json`` — pin it to ``file`` explicitly."""
    home = tmp_path
    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-test",
        base_url=None,
        home=home,
    )
    parsed = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert parsed[codex_config.CREDENTIALS_STORE_KEY] == codex_config.CREDENTIALS_STORE_FILE

    state = codex_config.read_codex_auth_state(home=home)
    assert state["credentials_store"] == "file"
    assert state["file_store_active"] is True


def test_apply_api_key_mode_drops_oauth_tokens(tmp_path: Path) -> None:
    """API-key mode is mutually exclusive with ChatGPT OAuth tokens."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"id_token": "abc"},
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    codex_config.apply_codex_auth(
        auth_mode="api_key",
        api_key="sk-test",
        base_url=None,
        home=home,
    )

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["auth_mode"] == "apikey"
    assert auth["OPENAI_API_KEY"] == "sk-test"
    assert "tokens" not in auth
    assert "last_refresh" not in auth


def test_apply_oauth_leaves_credentials_store_untouched(tmp_path: Path) -> None:
    """Switching back to OAuth must not flip the user's chosen store —
    ``codex login`` may legitimately want keyring storage."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'{codex_config.CREDENTIALS_STORE_KEY} = "keyring"\n', encoding="utf-8"
    )
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-old"}), encoding="utf-8"
    )
    codex_config.apply_codex_auth(auth_mode="oauth", api_key=None, base_url=None, home=home)
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed[codex_config.CREDENTIALS_STORE_KEY] == "keyring"


def test_read_state_reports_default_store_as_auto(tmp_path: Path) -> None:
    """Absent ``cli_auth_credentials_store`` means Codex's documented
    ``auto`` default; the UI gate on ``file_store_active`` depends on
    this being reported faithfully rather than silently masked as
    ``file``."""
    state = codex_config.read_codex_auth_state(home=tmp_path)
    assert state["credentials_store"] == "auto"
    assert state["file_store_active"] is False


def test_apply_oauth_mode_clears_managed_base_url(tmp_path: Path) -> None:
    """Switching back to oauth strips the managed base_url and api_key."""
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-old", "tokens": {"id_token": "abc"}}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[model_providers.openai]\nbase_url = "https://api.example.com/v1"\n',
        encoding="utf-8",
    )

    codex_config.apply_codex_auth(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        home=home,
    )

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert "OPENAI_API_KEY" not in auth
    assert auth["tokens"] == {"id_token": "abc"}

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert "model_providers" not in parsed


def test_apply_oauth_clears_user_owned_relay_pointer_with_base_url(tmp_path: Path) -> None:
    """OAuth tokens don't validate against custom relays — clear the pointer.

    Real-world failure mode: user originally set up API-key auth through a
    relay (``model_provider = "OpenAI"`` → ``[model_providers.OpenAI]``
    with ``base_url = "https://ai-relay.example.com"``). They switch to
    OAuth via the UI; we previously preserved the user-owned pointer,
    which sent OAuth bearer tokens to the relay. The relay returned
    ``401 INVALID_API_KEY`` (it only accepts API keys), bricking the
    save. Now: when switching to OAuth, clear the pointer if it targets
    a section with a custom base_url. The section itself stays for the
    user to re-point later (or for switching back to api_key mode).
    """
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        'model_provider = "OpenAI"\n'
        'cli_auth_credentials_store = "file"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'name = "OpenAI"\n'
        'base_url = "https://ai-relay.example.com"\n'
        'wire_api = "responses"\n'
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-old", "tokens": {"id_token": "abc"}}),
        encoding="utf-8",
    )

    result = codex_config.apply_codex_auth(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        home=home,
    )

    # The pointer is gone — Codex now falls back to the built-in
    # ``openai`` provider with the default endpoint, where OAuth tokens
    # actually validate.
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert "model_provider" not in parsed
    # The user's section is left intact so they can manually re-point
    # later or re-use it when switching back to api_key mode.
    assert parsed["model_providers"]["OpenAI"]["base_url"] == "https://ai-relay.example.com"
    assert parsed["model_providers"]["OpenAI"]["wire_api"] == "responses"

    notices = result["notices"]
    assert len(notices) == 1
    assert notices[0]["code"] == "cleared_custom_relay_pointer"
    assert notices[0]["provider_id"] == "OpenAI"
    assert notices[0]["base_url"] == "https://ai-relay.example.com"


def test_apply_oauth_leaves_user_owned_pointer_when_no_base_url(tmp_path: Path) -> None:
    """A pointer to a section without ``base_url`` isn't a relay — leave it.

    Some users wire ``model_provider`` at a built-in alias (e.g. one that
    inherits OpenAI's default endpoint via an inherited ``[provider]``
    block) for naming reasons. Without a ``base_url`` there is no relay
    conflict — OAuth tokens go to the default endpoint and validate.
    """
    home = tmp_path
    codex_home = home / ".codex"
    codex_home.mkdir()
    seed = (
        'model_provider = "CustomLabel"\n'
        "\n"
        "[model_providers.CustomLabel]\n"
        'name = "Custom"\n'
        'wire_api = "responses"\n'
    )
    (codex_home / "config.toml").write_text(seed, encoding="utf-8")

    result = codex_config.apply_codex_auth(
        auth_mode="oauth",
        api_key=None,
        base_url=None,
        home=home,
    )

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["model_provider"] == "CustomLabel"
    assert result["notices"] == []
