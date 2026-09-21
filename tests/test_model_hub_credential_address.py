"""A credential's address never survives into stored or displayed model names.

The engine reaches one credential through the model field of one outbound
request and through no other field, so a call spells its target
``<source prefix>/<model>``. That prefix addresses a credential; it is not part
of what the model is called. These tests hold the boundary in both directions:
nothing the product stores, shows, or matches against carries an address, and a
file an earlier release wrote with one still loads.
"""

import pytest

from config.v2_config import ModelHubModelConfig, ModelHubRouteConfig, ModelHubSourceConfig
from core.handlers.model_hub.identifiers import (
    model_id_without_credential_address,
    normalized_model_id,
    usage_ledger_key,
)
from vibe.model_hub_runtime.adapter import _discovered_models
from vibe.model_hub_runtime.state import EngineStateError, SourceRecord

ADDRESS = "avibe-dc0395516c77a0ee4e62e01e"


@pytest.mark.parametrize(
    ("stored", "identity"),
    [
        (f"{ADDRESS}/gpt-5.5", "gpt-5.5"),
        (f"{ADDRESS}/{ADDRESS}/gpt-5.5", "gpt-5.5"),
        # An upstream identity may carry a slash of its own. Unwrapping by
        # leading segment rather than by the minted spelling would rename these.
        ("anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4"),
        ("meta-llama/Llama-3-70b-instruct", "meta-llama/Llama-3-70b-instruct"),
        ("accounts/fireworks/models/mixtral", "accounts/fireworks/models/mixtral"),
        # Neither is the minted spelling: twelve random bytes, lowercase hex.
        ("avibe-short/gpt-5.5", "avibe-short/gpt-5.5"),
        (f"{ADDRESS.upper()}/gpt-5.5", f"{ADDRESS.upper()}/gpt-5.5"),
        ("avibe-openai/gpt-5.5", "avibe-openai/gpt-5.5"),
        # An address with nothing behind it names no model to recover.
        (f"{ADDRESS}/", f"{ADDRESS}/"),
        (ADDRESS, ADDRESS),
    ],
)
def test_only_the_minted_address_is_unwrapped(stored: str, identity: str) -> None:
    assert model_id_without_credential_address(stored) == identity


def test_usage_ledger_spelling_is_left_alone() -> None:
    """Unwrapping is not folded into the spelling a ledger key is derived from.

    ``usage_ledger_key`` hashes the canonical identity and its historical
    encoding is fixed, so a row written before this change must keep deriving
    the key it was written under.
    """

    addressed = f"{ADDRESS}/gpt-5.5"
    assert normalized_model_id(addressed) == addressed
    assert usage_ledger_key(addressed) == addressed


def test_persisted_source_model_loads_without_its_address() -> None:
    model = ModelHubModelConfig.from_payload(
        {"id": f"{ADDRESS}/gpt-5.5", "origin": "discovered", "reasoning_efforts": []}
    )
    assert model.id == "gpt-5.5"


def test_persisted_route_hop_loads_without_its_address() -> None:
    route = ModelHubRouteConfig.from_payload(
        {"hops": [{"source_id": "src_fixture123", "model_id": f"{ADDRESS}/gpt-5.5"}]},
        repairing=True,
    )
    assert [hop.model_id for hop in route.hops] == ["gpt-5.5"]


def _source_payload(model_ids: list[str]) -> dict:
    return {
        "id": "src_fixture123",
        "kind": "api_key",
        "vendor": "openai",
        "display_name": "Fixture",
        "protocol": "openai_responses",
        "base_url": None,
        "supply_channel": "hub",
        "billing": "metered",
        "state": {"status": "standby"},
        "credential_ref": "cred_fixture123",
        "models": [
            {"id": model_id, "origin": "discovered", "reasoning_efforts": []}
            for model_id in model_ids
        ],
    }


def test_addressed_and_bare_rows_collapse_when_repairing_a_file() -> None:
    """Both spellings name one model, and a file that loads must keep loading."""

    source = ModelHubSourceConfig.from_payload(
        _source_payload([f"{ADDRESS}/gpt-5.5", "gpt-5.5"]), repairing=True
    )
    assert [model.id for model in source.models] == ["gpt-5.5"]


def test_addressed_and_bare_rows_are_refused_from_a_caller() -> None:
    with pytest.raises(ValueError, match="duplicate ids"):
        ModelHubSourceConfig.from_payload(_source_payload([f"{ADDRESS}/gpt-5.5", "gpt-5.5"]))


def _record_payload(**overrides: object) -> dict:
    payload = {
        "source_id": "src_fixture123",
        "vendor": "openai",
        "protocol": "openai_responses",
        "base_url": None,
        "credential_ref": "cred_fixture123",
        "allowed_origins": ["main"],
        "model_ids": [f"{ADDRESS}/gpt-5.5"],
        "route_model_ids": [f"{ADDRESS}/gpt-5.5"],
        "prefix": ADDRESS,
        "model_reasoning_efforts": [[f"{ADDRESS}/gpt-5.5", ["low", "high"]]],
    }
    payload.update(overrides)
    return payload


def test_engine_record_rekeys_reasoning_efforts_with_its_model_ids() -> None:
    """The reasoning map is keyed by model id, so unwrapping must move the key.

    Unwrapping the ids alone would leave every effort list addressed to a model
    no longer in the inventory, and each model would silently fall back to its
    default effort.
    """

    record = SourceRecord.from_payload(_record_payload())

    assert record.model_ids == ("gpt-5.5",)
    assert record.route_model_ids == ("gpt-5.5",)
    assert record.model_reasoning_efforts == (("gpt-5.5", ("low", "high")),)
    assert record.prefix == ADDRESS


def test_engine_record_collapses_an_addressed_duplicate() -> None:
    record = SourceRecord.from_payload(
        _record_payload(
            model_ids=[f"{ADDRESS}/gpt-5.5", "gpt-5.5"],
            route_model_ids=[f"{ADDRESS}/gpt-5.5", "gpt-5.5"],
            model_reasoning_efforts=[
                [f"{ADDRESS}/gpt-5.5", ["high"]],
                ["gpt-5.5", ["low"]],
            ],
        )
    )

    assert record.model_ids == ("gpt-5.5",)
    assert record.route_model_ids == ("gpt-5.5",)
    assert record.model_reasoning_efforts == (("gpt-5.5", ("high",)),)


def test_engine_record_still_refuses_an_unreadable_reasoning_map() -> None:
    """Healing reads what it recognizes; the strict parse still reports the rest."""

    with pytest.raises(EngineStateError, match="invalid engine source reasoning state"):
        SourceRecord.from_payload(
            _record_payload(model_reasoning_efforts=[["unregistered-model", ["high"]]])
        )


def test_discovery_drops_the_address_the_engine_answers_with() -> None:
    """The management API answers with the name it addresses the credential by."""

    models = _discovered_models(
        {
            "models": [
                {"id": f"{ADDRESS}/gpt-5.5", "supported_parameters": ["reasoning"]},
                {"id": f"{ADDRESS}/gpt-6-astra"},
                {"id": "gpt-5.5"},
            ]
        }
    )

    assert [model.id for model in models] == ["gpt-5.5", "gpt-6-astra"]
    assert models[0].supported_parameters == ("reasoning",)
