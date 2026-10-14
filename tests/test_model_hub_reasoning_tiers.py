from __future__ import annotations

import pytest

from core.handlers.model_hub import reasoning_tiers
from core.handlers.model_hub.reasoning_tiers import resolve_reasoning_tiers
from vibe import backend_model_catalog
from vibe.backend_model_catalog import PROTOCOL_REASONING_EFFORT_DEFAULTS


@pytest.mark.parametrize(
    ("protocol", "expected"),
    tuple(PROTOCOL_REASONING_EFFORT_DEFAULTS.items()),
)
def test_upstream_reasoning_signal_uses_only_the_protocol_default(
    protocol: str,
    expected: tuple[str, ...],
) -> None:
    # A model no catalog row describes: nothing bounds what its protocol allows.
    resolution = resolve_reasoning_tiers(
        protocol=protocol,
        model_id="relay-reasoning-model",
        supported_parameters=("temperature", "reasoning"),
        existing_efforts=("user-only",),
        existing_source="user",
    )

    assert resolution.efforts == expected
    assert resolution.source == "upstream"


@pytest.mark.parametrize(
    ("protocol", "model_id", "reported", "efforts", "source"),
    (
        # A model's own row bounds the protocol defaults: Opus and Sonnet 4.6 have max but no xhigh.
        ("anthropic", "claude-opus-4-6", ("reasoning",), ("low", "medium", "high", "max"), "upstream"),
        ("anthropic", "claude-sonnet-4-6-20260301", ("reasoning",), ("low", "medium", "high", "max"), "upstream"),
        ("openai_responses", "gpt-5.6-sol", ("reasoning_effort",), ("low", "medium", "high", "xhigh"), "upstream"),
        # Mythos 5 and 5.1 have every Anthropic level, which is exactly the protocol default list, so no row is
        # needed for them.
        ("anthropic", "claude-mythos-5-1", ("reasoning",), ("low", "medium", "high", "xhigh", "max"), "upstream"),
        # A snapshot, a context tag or a provider prefix names the same row.
        ("anthropic", "claude-opus-5-20260724", None, ("low", "medium", "high", "xhigh", "max"), "catalog"),
        ("anthropic", "anthropic/claude-opus-5[1m]", None, ("low", "medium", "high", "xhigh", "max"), "catalog"),
        # ... and the row's client presets with it: a snapshot of gpt-6-astra still never offers Codex's ultra.
        ("openai_responses", "gpt-6-astra-2026-09-01", None, ("low", "medium", "high", "xhigh", "max"), "catalog"),
    ),
)
def test_the_hop_declares_exactly_its_models_levels(protocol, model_id, reported, efforts, source) -> None:
    """MH-HOSTED-006: one owner of effort levels. A snapshot or qualified id finds its model's catalog row, and that
    row bounds the protocol defaults a generic reasoning parameter stands for."""
    resolution = resolve_reasoning_tiers(protocol=protocol, model_id=model_id, supported_parameters=reported)

    assert (resolution.efforts, resolution.source) == (efforts, source)


def test_catalog_row_outranks_user_and_drops_its_client_presets() -> None:
    """MH-HOSTED-006 (#2539): Codex's ``ultra`` is a client preset, never a Source's wire tier."""
    resolution = resolve_reasoning_tiers(
        protocol="openai_responses",
        model_id="gpt-5.6-sol",
        existing_efforts=("user-only",),
        existing_source="user",
    )

    assert resolution.efforts == ("low", "medium", "high", "xhigh", "max")
    assert resolution.source == "catalog"


@pytest.mark.parametrize("protocol", ("openai_responses", "openai_chat"))
def test_codex_only_ultra_does_not_become_an_api_reasoning_tier(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    monkeypatch.setattr(
        backend_model_catalog,
        "_read_codex_models_cache",
        lambda: [{
            "id": "gpt-6.1-sol",
            "visibility": "list",
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        }],
    )
    native = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)
    api = resolve_reasoning_tiers(protocol=protocol, model_id="gpt-6.1-sol")

    assert [option["value"] for option in native["reasoning_options"]["gpt-6.1-sol"]] == [
        "__default__", "low", "medium", "high", "xhigh", "max", "ultra",
    ]
    assert api.efforts == ("low", "medium", "high", "xhigh", "max")
    assert api.source == "catalog"


@pytest.mark.parametrize(
    ("model_id", "tiers"),
    [
        # Anthropic's effort documentation: every level on Claude Haiku 5.5, no xhigh on Claude Sonnet 4.6.
        ("claude-haiku-5-5", ("low", "medium", "high", "xhigh", "max")),
        ("claude-sonnet-4-6", ("low", "medium", "high", "max")),
    ],
)
def test_an_anthropic_model_that_reports_no_parameters_gets_its_documented_tiers(
    model_id: str, tiers: tuple[str, ...]
) -> None:
    """Without upstream parameters, the bundled row is the only source of a hop's efforts: a model missing from it
    would declare none, and the loop would never send the effort the user picked."""
    resolution = resolve_reasoning_tiers(protocol="anthropic", model_id=model_id, supported_parameters=())

    assert resolution.efforts == tiers
    assert resolution.source == "catalog"


def test_catalog_row_is_not_filtered_through_the_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reasoning_tiers,
        "bundled_catalog_wire_efforts_by_model",
        lambda: pytest.fail("injected catalog index was ignored"),
    )

    resolution = resolve_reasoning_tiers(
        protocol="openai_chat",
        model_id="future-model",
        catalog_efforts_by_model={"future-model": ("future-effort",)},
    )

    assert resolution.efforts == ("future-effort",)
    assert resolution.source == "catalog"


def test_user_tiers_survive_only_when_no_managed_rung_applies() -> None:
    preserved = resolve_reasoning_tiers(
        protocol="anthropic",
        model_id="relay-model",
        supported_parameters=("temperature",),
        existing_efforts=("careful", "turbo"),
        existing_source="user",
    )
    empty = resolve_reasoning_tiers(
        protocol="anthropic",
        model_id="relay-model",
        supported_parameters=(),
    )

    assert preserved.efforts == ("careful", "turbo")
    assert preserved.source == "user"
    assert empty.efforts == ()
    assert empty.source is None


@pytest.mark.parametrize("parameter", ("reasoning", "reasoning_effort"))
def test_upstream_reasoning_parameter_matching_is_case_and_space_tolerant(
    parameter: str,
) -> None:
    resolution = resolve_reasoning_tiers(
        protocol="openai_chat",
        model_id="relay-model",
        supported_parameters=(f" {parameter.upper()} ",),
    )

    assert resolution.source == "upstream"


@pytest.mark.parametrize(
    ("provenance", "expected"),
    (
        ("catalog", ["low", "medium", "high", "xhigh", "max"]),
        ("user", ["low", "medium", "high", "xhigh", "max", "ultra"]),
    ),
)
def test_a_persisted_catalog_list_offers_no_client_preset_to_vibey(tmp_path, provenance, expected) -> None:
    """MH-HOSTED-006 (#2539): a released build persisted Codex's ``ultra`` in catalog lists; only user lists keep it."""
    from config.v2_config import ModelHubModelConfig, ModelHubSourceConfig, ModelHubSourceStateConfig
    from tests.test_model_hub_api import _service

    service, store, _adapter = _service(tmp_path)
    store.config.sources = [
        ModelHubSourceConfig(
            id="src_relay0001",
            kind="api_key",
            vendor="openai",
            display_name="OpenAI relay",
            protocol="openai_responses",
            supply_channel="hub",
            billing="metered",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[
                ModelHubModelConfig(
                    id="gpt-5.6-sol",
                    provenance="discovered",
                    reasoning_efforts=["low", "medium", "high", "xhigh", "max", "ultra"],
                    reasoning_efforts_source=provenance,
                )
            ],
            credential_ref="cred_relay",
        )
    ]
    store.config.agents["vibey"].sources.order = ["src_relay0001"]

    providers = service.agent_model_candidates("vibey")["providers"]

    [row] = [row for row in providers if row["id"] == "gpt-5.6-sol"]
    assert row["reasoning_efforts"] == expected


@pytest.mark.asyncio
async def test_rows_saved_by_3_2_0_are_derived_again_once_at_startup(tmp_path) -> None:
    """MH-HOSTED-006: 3.2.0 found no row for a snapshot id and declared xhigh for Opus and Sonnet 4.6. The fixture
    was written by v3.2.0's own ladder; the startup reconcile derives those rows again, and only those, and an agent's
    rows that took their tiers from them follow (#2629)."""
    import json
    from pathlib import Path

    from config.v2_config import ModelHubConfig
    from tests.test_model_hub_api import _service

    section = json.loads(
        (Path(__file__).parent / "fixtures/model_hub/released_3_2_0_source_tiers.json").read_text(encoding="utf-8")
    )
    service, store, _adapter = _service(tmp_path)
    store.config = ModelHubConfig.from_payload(section)

    changed = await service.reconcile_source_reasoning_tiers()

    rows = {
        (source.id, model.id): (model.reasoning_efforts, model.reasoning_efforts_source)
        for source in store.load().sources
        for model in source.models
    }
    assert rows == {
        ("src_tiers3201", "claude-opus-4-6"): (["low", "medium", "high", "max"], "upstream"),
        ("src_tiers3201", "claude-sonnet-4-6-20260301"): (["low", "medium", "high", "max"], "upstream"),
        ("src_tiers3201", "claude-opus-5-20260724"): (["low", "medium", "high", "xhigh", "max"], "catalog"),
        # No catalog row describes it: its upstream list stays as saved.
        ("src_tiers3201", "relay-house-model"): (["low", "medium", "high", "xhigh", "max"], "upstream"),
        # A user's list waits for discovery, which reports when the catalog outranks it.
        ("src_tiers3201", "claude-haiku-4-5-20251001"): (["low", "high"], "user"),
        ("src_tiers3202", "gpt-6-astra-2026-09-01"): (["low", "medium", "high", "xhigh", "max"], "catalog"),
    }
    assert sorted(changed) == [
        ("src_tiers3201", "claude-opus-4-6"),
        ("src_tiers3201", "claude-opus-5-20260724"),
        ("src_tiers3201", "claude-sonnet-4-6-20260301"),
        ("src_tiers3202", "gpt-6-astra-2026-09-01"),
    ]
    # Vibey sends only the tiers its own row lists: a row that still holds what its suppliers gave follows them.
    assert {row.id: row.reasoning_efforts for row in store.load().agents["vibey"].models} == {
        "gpt-6-astra-2026-09-01": ["low", "medium", "high", "xhigh", "max"],
        "claude-opus-4-6": ["low", "medium", "high", "max"],
        # A list its suppliers never gave was set apart, and a typed row is the user's.
        "claude-sonnet-4-6-20260301": ["low", "high"],
        "relay-house-model": [],
    }
    # Saved once: the next start finds nothing to change.
    assert await service.reconcile_source_reasoning_tiers() == []
