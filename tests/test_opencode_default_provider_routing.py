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
from modules.agents.opencode.utils import resolve_opencode_model_id


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
