"""A credential's address never survives into stored or displayed model names.

The engine reaches one credential through the model field of one outbound
request and through no other field, so a call spells its target
``<source prefix>/<model>``. That prefix addresses a credential; it is not part
of what the model is called. These tests hold the boundary in both directions:
nothing the product stores, shows, or matches against carries an address, and a
file an earlier release wrote with one still loads.

The address is removed where its owner is known, so what gets removed is a
credential's own address and never a name that merely looks like one. Discovery
knows the credential it is discovering, an engine record names the prefix it is
addressed by, and a config file — which records no prefix at all — is repaired
once on load, for the rows discovery produced, by the minted spelling.
"""

import copy
import json

import pytest

from config.v2_config import V2Config
from core.handlers.model_hub.identifiers import (
    model_id_without_credential_address,
    normalized_model_id,
    usage_ledger_key,
)
from core.services.settings import default_config
from vibe import api
from vibe.model_hub_runtime.adapter import _discovered_models
from vibe.model_hub_runtime.state import EngineStateError, SourceRecord

ADDRESS = "avibe-dc0395516c77a0ee4e62e01e"
# Minted the same way, for some other credential. Nothing here may treat the two
# as interchangeable: removing one credential's address from another's model
# would leave a name no source can serve.
OTHER_ADDRESS = "avibe-4f21ab90c3de5871ba0c6d17"


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
        ("x-ai/grok-4.6-latest", "x-ai/grok-4.6-latest"),
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
    """Without an owner to compare against, the minted spelling is the whole rule."""

    assert model_id_without_credential_address(stored) == identity


@pytest.mark.parametrize(
    ("prefix", "stored", "identity"),
    [
        (ADDRESS, f"{ADDRESS}/gpt-5.5", "gpt-5.5"),
        (ADDRESS, f"{ADDRESS}/{ADDRESS}/gpt-5.5", "gpt-5.5"),
        # Minted, but minted for someone else. A name this credential cannot be
        # proven to have addressed is left as whoever named it spelled it.
        (ADDRESS, f"{OTHER_ADDRESS}/gpt-5.5", f"{OTHER_ADDRESS}/gpt-5.5"),
        (ADDRESS, "anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4"),
        (ADDRESS, "gpt-5.5", "gpt-5.5"),
        # Provenance authorizes the removal, not the spelling: a credential
        # addressed by something the mint would not produce today still owns it.
        ("legacy-prefix", "legacy-prefix/gpt-5.5", "gpt-5.5"),
    ],
)
def test_only_the_owning_address_is_unwrapped(prefix: str, stored: str, identity: str) -> None:
    assert model_id_without_credential_address(stored, prefix) == identity


def test_usage_ledger_spelling_is_left_alone() -> None:
    """Unwrapping is not folded into the spelling a ledger key is derived from.

    ``usage_ledger_key`` hashes the canonical identity and its historical
    encoding is fixed, so a row written before this change must keep deriving
    the key it was written under.
    """

    addressed = f"{ADDRESS}/gpt-5.5"
    assert normalized_model_id(addressed) == addressed
    assert usage_ledger_key(addressed) == addressed


def test_discovery_drops_the_address_the_engine_answers_with() -> None:
    """The management API answers with the name it addresses the credential by.

    This is the boundary the address enters through, so it is where the address
    leaves. Discovery holds the credential, so it removes that credential's own
    address and nothing else.
    """

    models = _discovered_models(
        {
            "models": [
                {"id": f"{ADDRESS}/gpt-5.5", "supported_parameters": ["reasoning"]},
                {"id": f"{ADDRESS}/gpt-6-astra"},
                {"id": "gpt-5.5"},
            ]
        },
        ADDRESS,
    )

    assert [model.id for model in models] == ["gpt-5.5", "gpt-6-astra"]
    assert models[0].supported_parameters == ("reasoning",)


def test_discovery_keeps_a_name_it_cannot_prove_is_an_address() -> None:
    models = _discovered_models(
        {"models": [{"id": f"{OTHER_ADDRESS}/gpt-5.5"}, {"id": "x-ai/grok-4.6-latest"}]},
        ADDRESS,
    )

    assert [model.id for model in models] == [f"{OTHER_ADDRESS}/gpt-5.5", "x-ai/grok-4.6-latest"]


def test_discovery_without_a_known_address_falls_back_to_the_minted_shape() -> None:
    """A credential whose metadata carries no prefix still yields bare names."""

    models = _discovered_models({"models": [{"id": f"{ADDRESS}/gpt-5.5"}]})

    assert [model.id for model in models] == ["gpt-5.5"]


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


def test_engine_record_keeps_a_name_addressed_to_another_credential() -> None:
    """A record removes the address it is reached by, not every minted spelling."""

    record = SourceRecord.from_payload(
        _record_payload(
            model_ids=[f"{OTHER_ADDRESS}/gpt-5.5"],
            route_model_ids=[f"{OTHER_ADDRESS}/gpt-5.5"],
            model_reasoning_efforts=[[f"{OTHER_ADDRESS}/gpt-5.5", ["high"]]],
        )
    )

    assert record.model_ids == (f"{OTHER_ADDRESS}/gpt-5.5",)
    assert record.route_model_ids == (f"{OTHER_ADDRESS}/gpt-5.5",)
    assert record.model_reasoning_efforts == ((f"{OTHER_ADDRESS}/gpt-5.5", ("high",)),)


def test_engine_record_still_refuses_an_unreadable_reasoning_map() -> None:
    """Healing reads what it recognizes; the strict parse still reports the rest."""

    with pytest.raises(EngineStateError, match="invalid engine source reasoning state"):
        SourceRecord.from_payload(
            _record_payload(model_reasoning_efforts=[["unregistered-model", ["high"]]])
        )


SOURCE_ID = "src_fixture123"


def _model_payload(model_id: str, origin: str = "discovered", **overrides: object) -> dict:
    payload = {
        "id": model_id,
        "display_name": None,
        "origin": origin,
        "reasoning_efforts": [],
    }
    if origin == "discovered":
        payload["discovered_at"] = "2026-07-23T03:00:00Z"
    payload.update(overrides)
    return payload


def _config_payload(models: list[dict], hops: list[dict] | None = None) -> dict:
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"]["sources"] = [
        {
            "id": SOURCE_ID,
            "kind": "api_key",
            "vendor": "openai",
            "display_name": "Fixture",
            "protocol": "openai_responses",
            "base_url": None,
            "supply_channel": "hub",
            "billing": "metered",
            "state": {"status": "standby"},
            "credential_ref": "cred_fixture123",
            "models": copy.deepcopy(models),
        }
    ]
    if hops is not None:
        route = payload["model_hub"]["agents"]["claude"]["routes"].setdefault(
            "claude-opus-4-6", {"hops": []}
        )
        route["hops"] = copy.deepcopy(hops)
    return payload


def _load(tmp_path, payload: dict) -> V2Config:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return V2Config.load(config_path=config_path)


def test_a_file_an_earlier_release_addressed_loads_without_the_address(tmp_path) -> None:
    """One repair on load, for a file written before discovery stopped storing one."""

    loaded = _load(
        tmp_path,
        _config_payload(
            [_model_payload(f"{ADDRESS}/gpt-5.5"), _model_payload(f"{ADDRESS}/gpt-6-astra")],
            [{"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}],
        ),
    )

    assert loaded.load_warnings == ()
    assert [model.id for model in loaded.model_hub.sources[0].models] == ["gpt-5.5", "gpt-6-astra"]
    hops = loaded.model_hub.agents["claude"].routes["claude-opus-4-6"].hops
    assert [hop.model_id for hop in hops] == ["gpt-5.5"]


def test_a_model_a_person_added_keeps_the_name_they_gave_it(tmp_path) -> None:
    """Only discovery could have stored an address, so only its rows are repaired.

    A hand-added model spelled like an address is a name someone chose. Renaming
    it would point the row at a model the source may not serve at all.
    """

    loaded = _load(tmp_path, _config_payload([_model_payload(f"{ADDRESS}/gpt-5.5", "manual")]))

    assert [model.id for model in loaded.model_hub.sources[0].models] == [f"{ADDRESS}/gpt-5.5"]


def test_a_repaired_row_yields_to_the_manual_row_already_holding_its_name(tmp_path) -> None:
    """The shape left behind by working around this bug by hand.

    Both rows name one model once the address is gone. The manual row is the one
    a person made — it carries their display name and their provenance — and the
    discovered row adds no identity the surviving row does not already have.
    """

    loaded = _load(
        tmp_path,
        _config_payload(
            [
                _model_payload(f"{ADDRESS}/gpt-5.5"),
                _model_payload("gpt-5.5", "manual", display_name="Added by hand"),
            ]
        ),
    )

    assert loaded.load_warnings == ()
    models = loaded.model_hub.sources[0].models
    assert [(model.id, model.provenance, model.display_name) for model in models] == [
        ("gpt-5.5", "manual", "Added by hand")
    ]


def test_two_spellings_of_one_hop_collapse_onto_the_model_they_now_name(tmp_path) -> None:
    loaded = _load(
        tmp_path,
        _config_payload(
            [_model_payload(f"{ADDRESS}/gpt-5.5"), _model_payload("gpt-5.5")],
            [
                {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"},
                {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
            ],
        ),
    )

    assert loaded.load_warnings == ()
    assert [model.id for model in loaded.model_hub.sources[0].models] == ["gpt-5.5"]
    hops = loaded.model_hub.agents["claude"].routes["claude-opus-4-6"].hops
    assert [(hop.source_id, hop.model_id) for hop in hops] == [(SOURCE_ID, "gpt-5.5")]


def test_a_hop_this_repair_did_not_rename_is_left_where_it_points(tmp_path) -> None:
    """A hop follows its source's rows; it is never redirected on its own.

    Nothing in the file shows this hop meant ``gpt-5.5``: the source holds no row
    that was renamed to it. Rewriting the hop anyway would move a route to a
    model on a guess, so the hop stays and the ordinary unavailable-target path
    reports it.
    """

    loaded = _load(
        tmp_path,
        _config_payload(
            [_model_payload(f"{ADDRESS}/gpt-6-astra")],
            [{"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}],
        ),
    )

    assert [model.id for model in loaded.model_hub.sources[0].models] == ["gpt-6-astra"]
    hops = loaded.model_hub.agents["claude"].routes["claude-opus-4-6"].hops
    assert [(hop.source_id, hop.model_id) for hop in hops] == [(SOURCE_ID, f"{ADDRESS}/gpt-5.5")]


def test_a_file_carrying_no_address_is_not_rewritten(tmp_path) -> None:
    """The repair is a repair: a file with nothing to fix is left byte for byte."""

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            _config_payload(
                [_model_payload("gpt-5.5"), _model_payload("x-ai/grok-4.6-latest")],
                [{"source_id": SOURCE_ID, "model_id": "x-ai/grok-4.6-latest"}],
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    loaded = V2Config.load(config_path=config_path)

    assert config_path.read_bytes() == original
    assert [model.id for model in loaded.model_hub.sources[0].models] == [
        "gpt-5.5",
        "x-ai/grok-4.6-latest",
    ]


def test_a_repaired_config_serializes_to_one_this_product_loads_again(tmp_path) -> None:
    """The terminal property: repairing may not produce a file that will not load.

    Collapsing two spellings into one identity is where that could break — write
    the surviving name twice and the next load refuses the file it just wrote.
    """

    loaded = _load(
        tmp_path,
        _config_payload(
            [_model_payload(f"{ADDRESS}/gpt-5.5"), _model_payload("gpt-5.5")],
            [
                {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"},
                {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
            ],
        ),
    )
    rewritten = tmp_path / "rewritten.json"
    rewritten.write_text(
        json.dumps(
            api.config_to_payload(loaded, include_secrets=True, include_internal=True),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reloaded = V2Config.load(config_path=rewritten)

    assert reloaded.load_warnings == ()
    assert reloaded.model_hub.to_payload() == loaded.model_hub.to_payload()
