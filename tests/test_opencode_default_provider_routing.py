"""Regression tests for OpenCode bare-model provider routing.

Pins two invariants that landed via Codex round 4 review on PR #282:

1. ``OpenCodeConfig.default_provider`` defaults to ``None`` so legacy installs
   (Ollama/OpenAI/etc.) are not silently rerouted to Anthropic on upgrade.
2. The OpenCodeAgent bare-model fallback only injects ``providerID`` when a
   non-empty default provider has been explicitly chosen.
"""

from __future__ import annotations

import dataclasses

from config.v2_config import OpenCodeConfig
from modules.agents.opencode.agent import resolve_opencode_model_dict
from modules.agents.opencode.utils import (
    build_opencode_model_option_items,
    build_reasoning_effort_options,
    resolve_opencode_model_id,
    resolve_opencode_provider_preferences,
)


def test_native_defaults_cannot_change_model_choices_or_provider_order() -> None:
    catalog = {
        "providers": [
            {"id": "openai", "models": {"gpt-fixture": {"name": "GPT Fixture"}}},
            {"id": "anthropic", "models": {"claude-fixture": {"name": "Claude Fixture"}}},
        ],
    }
    expected = build_opencode_model_option_items(catalog, max_total=25)
    for model in ("openai/gpt-fixture", "anthropic/claude-fixture"):
        native = {
            "model": model,
            "agent": {"build": {"model": model}},
            "providers": {"openai": {}, "anthropic": {}},
        }
        provider, model_id = model.split("/", 1)
        assert build_opencode_model_option_items(
            {**catalog, "default": {provider: model_id}}, max_total=25
        ) == expected
        assert resolve_opencode_provider_preferences(native, "openai/gpt-fixture") == ["openai", "anthropic"]


def test_model_hub_picker_keeps_bare_identity_and_hides_transport_provider() -> None:
    catalog = {
        "providers": [
            {
                "id": "avibe-openai",
                "name": "Avibe · OpenAI",
                "models": {
                    "gpt-5": {
                        "id": "gpt-5",
                        "name": "GPT-5",
                        "variants": {"high": {"reasoningEffort": "high"}},
                        "vibe_remote": {"model_hub_projected": True},
                    }
                },
            }
        ],
        "default": {},
    }

    assert build_opencode_model_option_items(catalog, 10) == [
        {"label": "GPT-5", "value": "gpt-5"}
    ]
    assert build_reasoning_effort_options(catalog, "gpt-5") == [
        {"value": "__default__", "label": "(Default)"},
        {"value": "high", "label": "High"}
    ]


def test_model_hub_picker_keeps_slash_bearing_id_whole_for_variants() -> None:
    catalog = {
        "providers": [
            {
                "id": "avibe-openai",
                "name": "Avibe · OpenAI",
                "models": {
                    "moonshotai/kimi-k2": {
                        "id": "moonshotai/kimi-k2",
                        "name": "Kimi K2",
                        "variants": {"xhigh": {"reasoningEffort": "xhigh"}},
                        "vibe_remote": {"model_hub_projected": True},
                    }
                },
            }
        ],
        "default": {},
    }

    assert build_opencode_model_option_items(catalog, 10) == [
        {"label": "Kimi K2", "value": "moonshotai/kimi-k2"}
    ]
    assert build_reasoning_effort_options(catalog, "moonshotai/kimi-k2") == [
        {"value": "__default__", "label": "(Default)"},
        {"value": "xhigh", "label": "Extra High"},
    ]


def test_opencode_config_default_provider_is_unset_by_default() -> None:
    cfg = OpenCodeConfig()
    assert cfg.default_provider is None

    fields = {f.name: f for f in dataclasses.fields(OpenCodeConfig)}
    assert "default_provider" in fields
    # Hard-pin the default so a future refactor cannot reintroduce
    # ``"anthropic"`` as a silent fallback for unconfigured installs.
    assert fields["default_provider"].default is None


def test_bare_model_with_no_default_provider_returns_none() -> None:
    # Pre-upgrade behaviour: OpenCode owns routing for bare model IDs.
    assert resolve_opencode_model_dict("kimi-k2", default_provider=None) is None


def test_bare_model_with_blank_default_provider_returns_none() -> None:
    assert resolve_opencode_model_dict("kimi-k2", default_provider="   ") is None
    assert resolve_opencode_model_dict("kimi-k2", default_provider="") is None


def test_bare_model_with_explicit_default_provider_injects_provider_id() -> None:
    assert resolve_opencode_model_dict("kimi-k2", default_provider="ollama") == {
        "providerID": "ollama",
        "modelID": "kimi-k2",
    }


def test_bare_model_strips_default_provider_whitespace() -> None:
    assert resolve_opencode_model_dict("kimi-k2", default_provider="  ollama  ") == {
        "providerID": "ollama",
        "modelID": "kimi-k2",
    }


def test_prefixed_model_ignores_default_provider() -> None:
    # Explicit ``provider/model`` always wins, even if the user configured a
    # different default.
    assert resolve_opencode_model_dict("openai/gpt-5", default_provider="ollama") == {
        "providerID": "openai",
        "modelID": "gpt-5",
    }


def test_model_id_uses_catalog_casing_for_unique_match() -> None:
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {"id": "glm-5.2"},
                },
            }
        ]
    }

    assert resolve_opencode_model_id(catalog, "glm", "GLM-5.2") == "glm-5.2"


def test_model_id_preserves_exact_catalog_match() -> None:
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "GLM-5.2": {"id": "GLM-5.2"},
                },
            }
        ]
    }

    assert resolve_opencode_model_id(catalog, "glm", "GLM-5.2") == "GLM-5.2"


def test_model_id_uses_uppercase_catalog_casing_for_unique_match() -> None:
    catalog = {
        "providers": [
            {
                "id": "vendor",
                "models": {
                    "GLM-5.2": {"id": "GLM-5.2"},
                },
            }
        ]
    }

    assert resolve_opencode_model_id(catalog, "vendor", "glm-5.2") == "GLM-5.2"


def test_model_id_does_not_guess_ambiguous_case_match() -> None:
    catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {"id": "glm-5.2"},
                    "GLM-5.2": {"id": "GLM-5.2"},
                },
            }
        ]
    }

    assert resolve_opencode_model_id(catalog, "glm", "Glm-5.2") == "Glm-5.2"
