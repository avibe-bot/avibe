"""Unit tests for the OpenCode provider ``baseURL`` config helpers.

The Settings → Backends → OpenCode page exposes a Base URL input per
provider; without these helpers the input is a no-op because OpenCode's
own ``PUT /auth/{provider_id}`` endpoint has no field for it. These
tests pin the round-trip (upsert → read → remove) and the prune
behaviour so a future refactor cannot silently regress the UI back into
"save success, value lost on reload" — the bug Codex flagged in
``PR #282`` round 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe.opencode_config import (
    get_opencode_custom_provider_adapter,
    get_opencode_config_paths,
    read_opencode_provider_base_url,
    read_opencode_provider_user_models,
    remove_opencode_provider_base_url,
    remove_opencode_custom_provider,
    remove_opencode_provider_model,
    read_opencode_custom_providers,
    upsert_opencode_provider_base_url,
    upsert_opencode_provider_api_key,
    upsert_opencode_custom_provider,
    upsert_opencode_provider_model,
)


def _read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_upsert_base_url_writes_canonical_path(tmp_path: Path) -> None:
    target = upsert_opencode_provider_base_url(
        "openai",
        "https://ai-relay.example/v1",
        home=tmp_path,
    )
    assert target == get_opencode_config_paths(tmp_path)[0]
    config = _read_config(target)
    assert config["provider"]["openai"]["options"]["baseURL"] == "https://ai-relay.example/v1"
    assert config["$schema"] == "https://opencode.ai/config.json"


def test_upsert_base_url_coexists_with_api_key(tmp_path: Path) -> None:
    upsert_opencode_provider_api_key("openai", "sk-xxx", home=tmp_path)
    upsert_opencode_provider_base_url("openai", "https://gw.example/v1", home=tmp_path)
    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    options = config["provider"]["openai"]["options"]
    assert options["apiKey"] == "sk-xxx"
    assert options["baseURL"] == "https://gw.example/v1"


def test_upsert_base_url_overrides_previous_value(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://old.example/v1", home=tmp_path)
    upsert_opencode_provider_base_url("openai", "https://new.example/v1", home=tmp_path)
    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert config["provider"]["openai"]["options"]["baseURL"] == "https://new.example/v1"


def test_read_base_url_returns_none_when_unset(tmp_path: Path) -> None:
    # No config file yet → nothing to read.
    assert read_opencode_provider_base_url("openai", home=tmp_path) is None

    # Config exists but provider does not.
    upsert_opencode_provider_api_key("anthropic", "sk-anth", home=tmp_path)
    assert read_opencode_provider_base_url("openai", home=tmp_path) is None


def test_read_base_url_returns_persisted_value(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url(
        "openai",
        "https://ai-relay.example/v1",
        home=tmp_path,
    )
    assert (
        read_opencode_provider_base_url("openai", home=tmp_path)
        == "https://ai-relay.example/v1"
    )


def test_remove_base_url_prunes_empty_options(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://gw.example/v1", home=tmp_path)
    remove_opencode_provider_base_url("openai", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    # With both apiKey and baseURL gone, the provider block prunes
    # itself; with no provider blocks left, the ``provider`` key is
    # dropped entirely. The ``$schema`` planted by upsert stays.
    assert "provider" not in config
    assert config.get("$schema") == "https://opencode.ai/config.json"


def test_remove_base_url_preserves_api_key(tmp_path: Path) -> None:
    upsert_opencode_provider_api_key("openai", "sk-xxx", home=tmp_path)
    upsert_opencode_provider_base_url("openai", "https://gw.example/v1", home=tmp_path)
    remove_opencode_provider_base_url("openai", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    options = config["provider"]["openai"]["options"]
    assert options == {"apiKey": "sk-xxx"}


def test_remove_base_url_is_idempotent(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://gw.example/v1", home=tmp_path)
    remove_opencode_provider_base_url("openai", home=tmp_path)
    # Second call must be a no-op rather than raise.
    remove_opencode_provider_base_url("openai", home=tmp_path)
    assert read_opencode_provider_base_url("openai", home=tmp_path) is None


@pytest.mark.parametrize(
    "base_url_value",
    ["   ", ""],
)
def test_read_base_url_ignores_blank_values(tmp_path: Path, base_url_value: str) -> None:
    # Write a value that contains no useful content; the helper should
    # treat it as "not configured" so the UI does not show whitespace as
    # the persisted override.
    upsert_opencode_provider_base_url("openai", "https://x.example", home=tmp_path)
    target = get_opencode_config_paths(tmp_path)[0]
    config = _read_config(target)
    config["provider"]["openai"]["options"]["baseURL"] = base_url_value
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    assert read_opencode_provider_base_url("openai", home=tmp_path) is None


def test_remove_then_reupsert_round_trip(tmp_path: Path) -> None:
    # Once ``remove`` has pruned the entire ``provider`` block (the only
    # provider had its last option removed), a subsequent ``upsert`` has
    # to scaffold the JSON structure back from scratch — exercise that
    # path so a future refactor doesn't regress to a KeyError.
    upsert_opencode_provider_base_url("openai", "https://old.example", home=tmp_path)
    remove_opencode_provider_base_url("openai", home=tmp_path)
    upsert_opencode_provider_base_url("openai", "https://new.example", home=tmp_path)
    assert (
        read_opencode_provider_base_url("openai", home=tmp_path)
        == "https://new.example"
    )


def test_remove_one_provider_keeps_other_untouched(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://a.example", home=tmp_path)
    upsert_opencode_provider_base_url("anthropic", "https://b.example", home=tmp_path)
    upsert_opencode_provider_api_key("anthropic", "sk-anth", home=tmp_path)

    remove_opencode_provider_base_url("openai", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert "openai" not in config.get("provider", {})
    # Anthropic still has both apiKey and baseURL untouched.
    assert config["provider"]["anthropic"]["options"] == {
        "apiKey": "sk-anth",
        "baseURL": "https://b.example",
    }


def test_upsert_provider_model_writes_user_model_variants(tmp_path: Path) -> None:
    upsert_opencode_provider_model(
        "deepseek",
        "deepseek-v4-flash",
        reasoning_efforts=["low", "high"],
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    model = config["provider"]["deepseek"]["models"]["deepseek-v4-flash"]
    assert model["id"] == "deepseek-v4-flash"
    assert model["name"] == "deepseek-v4-flash"
    assert model["variants"] == {
        "low": {"reasoningEffort": "low"},
        "high": {"reasoningEffort": "high"},
    }
    assert model["vibe_remote"] == {"user_model": True}
    assert read_opencode_provider_user_models("deepseek", home=tmp_path).keys() == {
        "deepseek-v4-flash"
    }


def test_read_provider_user_models_keeps_legacy_vibe_rows(tmp_path: Path) -> None:
    config_path = get_opencode_config_paths(tmp_path)[0]
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "deepseek": {
                        "models": {
                            "manual-legacy-model": {
                                "id": "manual-legacy-model",
                                "name": "manual-legacy-model",
                            },
                            "deepseek-chat": {
                                "variants": {"high": {"reasoningEffort": "high"}},
                            },
                        }
                    }
                }
            }
        )
    )

    assert read_opencode_provider_user_models("deepseek", home=tmp_path).keys() == {
        "manual-legacy-model"
    }


def test_upsert_provider_model_writes_anthropic_thinking_variants(tmp_path: Path) -> None:
    upsert_opencode_provider_model(
        "anthropic",
        "claude-sonnet-4-5",
        reasoning_efforts=["high", "max"],
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    model = config["provider"]["anthropic"]["models"]["claude-sonnet-4-5"]
    assert model["variants"] == {
        "high": {"thinking": {"type": "enabled", "effort": "high"}},
        "max": {"thinking": {"type": "enabled", "effort": "max"}},
    }


def test_upsert_custom_anthropic_provider_model_writes_thinking_variants(tmp_path: Path) -> None:
    upsert_opencode_custom_provider(
        "anthropic-relay",
        "Anthropic Relay",
        "anthropic-compatible",
        "https://anthropic.example",
        home=tmp_path,
    )

    upsert_opencode_provider_model(
        "anthropic-relay",
        "claude-sonnet-4-5",
        reasoning_efforts=["high"],
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    model = config["provider"]["anthropic-relay"]["models"]["claude-sonnet-4-5"]
    assert model["variants"] == {
        "high": {"thinking": {"type": "enabled", "effort": "high"}},
    }


def test_upsert_provider_model_rejects_duplicated_provider_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider prefix"):
        upsert_opencode_provider_model("deepseek", "deepseek/deepseek-v4-flash", home=tmp_path)


def test_upsert_provider_model_allows_provider_native_slash(tmp_path: Path) -> None:
    upsert_opencode_provider_model(
        "openrouter",
        "anthropic/claude-sonnet-4",
        home=tmp_path,
    )

    assert "anthropic/claude-sonnet-4" in read_opencode_provider_user_models(
        "openrouter",
        home=tmp_path,
    )


def test_remove_provider_model_keeps_empty_model_tombstone_and_options(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("deepseek", "https://api.deepseek.com", home=tmp_path)
    upsert_opencode_provider_model("deepseek", "deepseek-v4-flash", home=tmp_path)

    remove_opencode_provider_model("deepseek", "deepseek-v4-flash", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert config["provider"]["deepseek"]["models"] == {}
    assert config["provider"]["deepseek"]["options"]["baseURL"] == "https://api.deepseek.com"


def test_upsert_custom_openai_compatible_provider_writes_opencode_shape(tmp_path: Path) -> None:
    upsert_opencode_custom_provider(
        "my-relay",
        "My Relay",
        "openai-compatible",
        "https://relay.example/v1",
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    provider = config["provider"]["my-relay"]
    assert provider["name"] == "My Relay"
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://relay.example/v1"
    assert provider["models"] == {}
    assert provider["vibe_remote"]["custom"] is True
    assert provider["vibe_remote"]["adapter"] == "openai-compatible"
    assert "my-relay" in read_opencode_custom_providers(home=tmp_path)


def test_custom_provider_reader_accepts_opencode_preserved_shape_without_meta(tmp_path: Path) -> None:
    config_path = get_opencode_config_paths(tmp_path)[0]
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "my-relay": {
                        "name": "My Relay",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://relay.example/v1",
                            "apiKey": "sk-relay",
                        },
                        "models": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert get_opencode_custom_provider_adapter(
        "my-relay",
        _read_config(config_path)["provider"]["my-relay"],
    ) == "openai-compatible"
    assert "my-relay" in read_opencode_custom_providers(home=tmp_path)


def test_custom_provider_reader_does_not_treat_builtin_override_as_custom(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://relay.example/v1", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])

    assert get_opencode_custom_provider_adapter("openai", config["provider"]["openai"]) is None
    assert read_opencode_custom_providers(home=tmp_path) == {}


def test_remove_custom_provider_accepts_opencode_preserved_shape_without_meta(tmp_path: Path) -> None:
    config_path = get_opencode_config_paths(tmp_path)[0]
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "my-relay": {
                        "name": "My Relay",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://relay.example/v1",
                            "apiKey": "sk-relay",
                        },
                        "models": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    remove_opencode_custom_provider("my-relay", home=tmp_path)

    assert "provider" not in _read_config(config_path)


def test_upsert_custom_anthropic_compatible_provider_writes_adapter(tmp_path: Path) -> None:
    upsert_opencode_custom_provider(
        "anthropic-relay",
        "Anthropic Relay",
        "anthropic-compatible",
        "https://anthropic.example",
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert config["provider"]["anthropic-relay"]["npm"] == "@ai-sdk/anthropic"


def test_upsert_api_key_restores_custom_provider_meta(tmp_path: Path) -> None:
    config_path = get_opencode_config_paths(tmp_path)[0]
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "my-relay": {
                        "name": "My Relay",
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "https://relay.example/v1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    upsert_opencode_provider_api_key("my-relay", "sk-relay", home=tmp_path)

    provider = _read_config(config_path)["provider"]["my-relay"]
    assert provider["options"]["apiKey"] == "sk-relay"
    assert provider["vibe_remote"]["custom"] is True
    assert provider["vibe_remote"]["adapter"] == "openai-compatible"


def test_upsert_custom_provider_allows_documented_dotted_id(tmp_path: Path) -> None:
    upsert_opencode_custom_provider(
        "llama.cpp",
        "llama.cpp",
        "openai-compatible",
        "http://127.0.0.1:8080/v1",
        home=tmp_path,
    )

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert config["provider"]["llama.cpp"]["name"] == "llama.cpp"


def test_upsert_custom_provider_refuses_existing_builtin_block(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://relay.example/v1", home=tmp_path)

    with pytest.raises(ValueError, match="provider_id already exists"):
        upsert_opencode_custom_provider(
            "openai",
            "OpenAI Override",
            "openai-compatible",
            "https://other.example/v1",
            home=tmp_path,
        )


def test_remove_custom_provider_deletes_only_custom_block(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://relay.example/v1", home=tmp_path)
    upsert_opencode_custom_provider(
        "my-relay",
        "My Relay",
        "openai-compatible",
        "https://relay.example/v1",
        home=tmp_path,
    )

    remove_opencode_custom_provider("my-relay", home=tmp_path)

    config = _read_config(get_opencode_config_paths(tmp_path)[0])
    assert "my-relay" not in config["provider"]
    assert "openai" in config["provider"]


def test_remove_custom_provider_rejects_builtin_block(tmp_path: Path) -> None:
    upsert_opencode_provider_base_url("openai", "https://relay.example/v1", home=tmp_path)

    with pytest.raises(ValueError, match="Only custom providers"):
        remove_opencode_custom_provider("openai", home=tmp_path)
