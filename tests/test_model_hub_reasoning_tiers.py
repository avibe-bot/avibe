from __future__ import annotations

import pytest

from core.handlers.model_hub import reasoning_tiers
from core.handlers.model_hub.reasoning_tiers import resolve_reasoning_tiers
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


def test_catalog_row_outranks_user_and_is_returned_verbatim() -> None:
    resolution = resolve_reasoning_tiers(
        protocol="openai_responses",
        model_id="gpt-5.6-sol",
        existing_efforts=("user-only",),
        existing_source="user",
    )

    assert resolution.efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    )
    assert resolution.source == "catalog"


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
