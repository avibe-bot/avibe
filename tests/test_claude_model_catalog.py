from __future__ import annotations

from vibe.claude_model_catalog import (
    FALLBACK_CLAUDE_MODELS,
    RETIRED_CLAUDE_MODELS,
    infer_models_from_bundle,
    load_catalog_models,
    sort_catalog_models,
)


def test_fable_is_tracked_in_catalog_and_fallback():
    assert "claude-fable-5" in load_catalog_models()
    assert "claude-fable-5" in FALLBACK_CLAUDE_MODELS


def test_fable_5_1_is_tracked_in_catalog_and_fallback():
    assert "claude-fable-5-1" in load_catalog_models()
    assert "claude-fable-5-1" in FALLBACK_CLAUDE_MODELS


def test_sonnet_5_is_tracked_in_catalog_and_fallback():
    assert "claude-sonnet-5" in load_catalog_models()
    assert "claude-sonnet-5" in FALLBACK_CLAUDE_MODELS


def test_opus_5_is_tracked_in_catalog_and_fallback():
    assert "claude-opus-5" in load_catalog_models()
    assert "claude-opus-5" in FALLBACK_CLAUDE_MODELS


def test_opus_5_5_is_tracked_in_catalog_and_fallback():
    assert "claude-opus-5-5" in load_catalog_models()
    assert "claude-opus-5-5" in FALLBACK_CLAUDE_MODELS


def test_opus_5_5_sorts_before_opus_5_in_every_tracked_listing():
    for listing in (load_catalog_models(), list(FALLBACK_CLAUDE_MODELS)):
        assert listing.index("claude-opus-5-5") < listing.index("claude-opus-5")


def test_retired_models_leave_the_tracked_catalog_and_fallback():
    """Anthropic retired these on 2026-06-15, or never shipped them under this id."""

    models = load_catalog_models()
    for model in (
        "claude-opus-4",
        "claude-sonnet-4",
        "claude-haiku-4",
        "claude-sonnet-4-0",
        "claude-sonnet-4-20250514",
        "claude-sonnet-3-7",
        "claude-haiku-3-5",
    ):
        assert model not in models, model
        assert model not in FALLBACK_CLAUDE_MODELS, model


def test_bundle_inference_drops_retired_models(tmp_path):
    bundle = tmp_path / "cli.js"
    bundle.write_bytes(b";".join(f'"{model}"'.encode() for model in ("claude-opus-5", *RETIRED_CLAUDE_MODELS)))

    assert infer_models_from_bundle(bundle) == ["claude-opus-5"]


def test_live_legacy_models_survive_the_retirement_sweep():
    """Ids that Claude Code still resolves must stay selectable."""

    models = load_catalog_models()
    for model in (
        "claude-opus-4-0",
        "claude-opus-4-1",
        "claude-opus-4-1-20250805",
        "claude-opus-4-20250514",
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ):
        assert model in models, model


def test_catalog_excludes_dated_4_6_and_later_internal_ids():
    models = load_catalog_models()

    assert "claude-opus-4-6-20251101" not in models
    assert "claude-sonnet-4-6-20251114" not in models
    assert "claude-sonnet-5-20260630" not in sort_catalog_models(["claude-sonnet-5-20260630"])
    assert "claude-fable-5-20260609" not in sort_catalog_models(["claude-fable-5-20260609"])
    assert "claude-fable-5-1-20260901" not in sort_catalog_models(["claude-fable-5-1-20260901"])
    assert "claude-opus-5-20260724" not in sort_catalog_models(["claude-opus-5-20260724"])
    assert "claude-opus-5-5-20260922" not in sort_catalog_models(["claude-opus-5-5-20260922"])


def test_dateless_model_ids_sort_before_matching_snapshots():
    models = load_catalog_models()
    ordered = sort_catalog_models(models)
    positions = {model: index for index, model in enumerate(ordered)}

    matching_pairs = []
    for model in models:
        parts = model.split("-")
        if len(parts[-1]) != 8 or not parts[-1].isdigit():
            continue
        dateless_model = "-".join(parts[:-1])
        if dateless_model in positions:
            matching_pairs.append((dateless_model, model))

    assert matching_pairs
    assert all(positions[dateless] < positions[snapshot] for dateless, snapshot in matching_pairs)


def test_fable_sorts_above_other_families():
    ordered = sort_catalog_models(
        [
            "claude-haiku-4-5",
            "claude-opus-4-8",
            "claude-opus-5-5",
            "claude-fable-5-1",
            "claude-fable-5",
            "claude-sonnet-4-6",
        ]
    )
    # Fable is the Mythos-class tier and must lead the catalog ordering.
    assert ordered[0] == "claude-fable-5-1"
    assert ordered.index("claude-fable-5-1") < ordered.index("claude-fable-5")
    assert ordered.index("claude-fable-5") < ordered.index("claude-opus-5-5")
    assert ordered.index("claude-opus-5-5") < ordered.index("claude-opus-4-8")


def test_bundle_inference_detects_fable_and_skips_mythos_preview(tmp_path):
    bundle = tmp_path / "cli.js"
    bundle.write_bytes(
        b'pick("claude-fable-5-1");previous="claude-fable-5";fallback="claude-opus-4-8";'
        b'"claude-opus-5-5";"claude-opus-5";"claude-sonnet-5";"claude-opus-4-6-20251101";'
        b'"claude-sonnet-4-6-20251114";"claude-sonnet-5-20260630";'
        b'"claude-fable-5-20260609";"claude-fable-5-1-20260901";'
        b'"claude-opus-5-20260724";"claude-opus-5-5-20260922";"claude-mythos-preview"'
    )

    models = infer_models_from_bundle(bundle)

    assert models == [
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
    ]
    # `claude-mythos-preview` carries no version segment and is not a publicly
    # callable model, so it must not leak into the catalog.
    assert "claude-mythos-preview" not in models
    # Claude 4.6+ public IDs are dateless. Claude Code bundles may contain
    # dated internal identifiers, but Avibe must not surface them as choices.
    assert "claude-opus-4-6-20251101" not in models
    assert "claude-sonnet-4-6-20251114" not in models
    assert "claude-sonnet-5-20260630" not in models
    assert "claude-fable-5-20260609" not in models
    assert "claude-fable-5-1-20260901" not in models
    assert "claude-opus-5-20260724" not in models
    assert "claude-opus-5-5-20260922" not in models
