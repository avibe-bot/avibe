from __future__ import annotations

from vibe.claude_model_catalog import (
    FALLBACK_CLAUDE_MODELS,
    infer_models_from_bundle,
    load_catalog_models,
    sort_catalog_models,
)


def test_fable_is_tracked_in_catalog_and_fallback():
    assert "claude-fable-5" in load_catalog_models()
    assert "claude-fable-5" in FALLBACK_CLAUDE_MODELS


def test_sonnet_5_is_tracked_in_catalog_and_fallback():
    assert "claude-sonnet-5" in load_catalog_models()
    assert "claude-sonnet-5" in FALLBACK_CLAUDE_MODELS


def test_catalog_excludes_dated_4_6_and_later_internal_ids():
    models = load_catalog_models()

    assert "claude-opus-4-6-20251101" not in models
    assert "claude-sonnet-4-6-20251114" not in models
    assert "claude-sonnet-5-20260630" not in sort_catalog_models(["claude-sonnet-5-20260630"])
    assert "claude-fable-5-20260609" not in sort_catalog_models(["claude-fable-5-20260609"])


def test_fable_sorts_above_other_families():
    ordered = sort_catalog_models(
        [
            "claude-haiku-4-5",
            "claude-opus-4-8",
            "claude-fable-5",
            "claude-sonnet-4-6",
        ]
    )
    # Fable is the Mythos-class tier and must lead the catalog ordering.
    assert ordered[0] == "claude-fable-5"
    assert ordered.index("claude-fable-5") < ordered.index("claude-opus-4-8")


def test_bundle_inference_detects_fable_and_skips_mythos_preview(tmp_path):
    bundle = tmp_path / "cli.js"
    bundle.write_bytes(
        b'pick("claude-fable-5");fallback="claude-opus-4-8";'
        b'"claude-sonnet-5";"claude-opus-4-6-20251101";'
        b'"claude-sonnet-4-6-20251114";"claude-sonnet-5-20260630";'
        b'"claude-fable-5-20260609";"claude-mythos-preview"'
    )

    models = infer_models_from_bundle(bundle)

    assert models == ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"]
    # `claude-mythos-preview` carries no version segment and is not a publicly
    # callable model, so it must not leak into the catalog.
    assert "claude-mythos-preview" not in models
    # Claude 4.6+ public IDs are dateless. Claude Code bundles may contain
    # dated internal identifiers, but Avibe must not surface them as choices.
    assert "claude-opus-4-6-20251101" not in models
    assert "claude-sonnet-4-6-20251114" not in models
    assert "claude-sonnet-5-20260630" not in models
    assert "claude-fable-5-20260609" not in models
