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
    resolution = resolve_reasoning_tiers(
        protocol=protocol,
        model_id="gpt-5.6-sol",
        supported_parameters=("temperature", "reasoning"),
        existing_efforts=("user-only",),
        existing_source="user",
    )

    assert resolution.efforts == expected
    assert resolution.source == "upstream"


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


def test_catalog_row_is_not_filtered_through_the_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reasoning_tiers,
        "bundled_catalog_reasoning_efforts_for_model",
        lambda _model_id: pytest.fail("injected catalog index was ignored"),
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
