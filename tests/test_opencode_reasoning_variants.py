"""OpenCode must accept every reasoning tier the unified vocabulary declares.

Regression guard for #1840. The OpenCode save allowlist was a hand-kept copy of
the Model Hub vocabulary and fell behind when ``ultra`` was added for the
gpt-5.6-sol / gpt-5.6-terra catalog rows, so a catalog-declared tier could not
be persisted through the OpenCode provider form at all.

The tests below are written as properties over the exported vocabulary rather
than over a literal tier list: a tier added to the vocabulary later is covered
without editing this file, which is the only form that would have caught the
original drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from modules.agents.opencode import utils as opencode_utils
from vibe import backend_model_catalog
from vibe.backend_model_catalog import REASONING_EFFORT_VOCABULARY
from vibe.opencode_config import (
    OPENCODE_REASONING_VARIANTS,
    get_opencode_config_paths,
    upsert_opencode_provider_model,
)


def _written_model(home: Path, provider_id: str, model_id: str) -> dict:
    config = json.loads(get_opencode_config_paths(home)[0].read_text(encoding="utf-8"))
    return config["provider"][provider_id]["models"][model_id]


def _catalog_declared_tiers() -> dict[str, tuple[str, ...]]:
    """Every bundled-catalog row that names its own tiers, keyed by model id."""

    return dict(backend_model_catalog.bundled_catalog_reasoning_efforts_by_model())


def test_opencode_variants_mirror_the_unified_vocabulary() -> None:
    # OpenCode adds exactly one token of its own: `none`, the opt-out.
    assert OPENCODE_REASONING_VARIANTS == ("none", *REASONING_EFFORT_VOCABULARY)


def test_every_vocabulary_tier_projects_to_an_openai_variant(tmp_path: Path) -> None:
    upsert_opencode_provider_model(
        "deepseek",
        "relay-model",
        reasoning_efforts=list(OPENCODE_REASONING_VARIANTS),
        home=tmp_path,
    )

    assert _written_model(tmp_path, "deepseek", "relay-model")["variants"] == {
        effort: {"reasoningEffort": effort} for effort in OPENCODE_REASONING_VARIANTS
    }


def test_every_vocabulary_tier_projects_to_an_anthropic_variant(tmp_path: Path) -> None:
    upsert_opencode_provider_model(
        "anthropic",
        "relay-model",
        reasoning_efforts=list(OPENCODE_REASONING_VARIANTS),
        home=tmp_path,
    )

    assert _written_model(tmp_path, "anthropic", "relay-model")["variants"] == {
        effort: {"thinking": {"type": "enabled", "effort": effort}}
        for effort in OPENCODE_REASONING_VARIANTS
    }


def test_catalog_declared_tiers_round_trip_through_the_opencode_overlay(
    tmp_path: Path,
) -> None:
    """Rung 2 applies a catalog row verbatim, so the overlay must take it whole.

    Seeding every declaring row is complete by construction: a future row that
    names a tier OpenCode rejects fails here without anyone remembering to add
    a case for it.
    """

    declared = _catalog_declared_tiers()
    assert declared, "bundled catalog declares no reasoning tiers"

    for model_id, efforts in declared.items():
        upsert_opencode_provider_model(
            "deepseek",
            model_id,
            reasoning_efforts=list(efforts),
            home=tmp_path,
        )

    for model_id, efforts in declared.items():
        assert _written_model(tmp_path, "deepseek", model_id)["variants"] == {
            effort: {"reasoningEffort": effort} for effort in efforts
        }


def test_family_default_suggestions_never_claim_ultra() -> None:
    """Family defaults are rung-1 guesses about a model nobody has enumerated.

    Claiming a tier the upstream does not have turns into a 400 on the next
    turn, so the defaults stay a strict subset of the vocabulary even though
    the vocabulary itself is the superset.
    """

    assert "ultra" in REASONING_EFFORT_VOCABULARY

    protocol_defaults = backend_model_catalog.PROTOCOL_REASONING_EFFORT_DEFAULTS
    assert protocol_defaults
    assert all("ultra" not in efforts for efforts in protocol_defaults.values())

    backend_defaults = backend_model_catalog._DEFAULT_REASONING_EFFORTS
    assert backend_defaults
    assert all("ultra" not in efforts for efforts in backend_defaults.values())

    codex_options = opencode_utils.build_codex_reasoning_options()
    assert "ultra" not in {option["value"] for option in codex_options}

    claude_options = opencode_utils.build_claude_reasoning_options("claude-opus-5")
    assert "ultra" not in {option["value"] for option in claude_options}


def test_opencode_variant_display_tables_enumerate_the_shared_vocabulary() -> None:
    assert tuple(opencode_utils._REASONING_VARIANT_ORDER) == OPENCODE_REASONING_VARIANTS
    assert set(opencode_utils._REASONING_VARIANT_LABELS) == set(
        OPENCODE_REASONING_VARIANTS
    )


def test_reasoning_dropdown_renders_every_vocabulary_tier_in_order() -> None:
    catalog = {
        "providers": [
            {
                "id": "avibe-openai",
                "name": "Avibe · OpenAI",
                "models": {
                    "gpt-5.6-sol": {
                        "id": "gpt-5.6-sol",
                        "name": "GPT-5.6-Sol",
                        "variants": {
                            effort: {"reasoningEffort": effort}
                            for effort in reversed(OPENCODE_REASONING_VARIANTS)
                        },
                        "vibe_remote": {"model_hub_projected": True},
                    }
                },
            }
        ],
        "default": {},
    }

    options = opencode_utils.build_reasoning_effort_options(catalog, "gpt-5.6-sol")

    assert options[0] == {"value": "__default__", "label": "(Default)"}
    assert tuple(option["value"] for option in options[1:]) == OPENCODE_REASONING_VARIANTS
    # Every tier carries a curated label; none falls through to `capitalize()`.
    assert [option["label"] for option in options[1:]] == [
        opencode_utils._REASONING_VARIANT_LABELS[effort]
        for effort in OPENCODE_REASONING_VARIANTS
    ]
