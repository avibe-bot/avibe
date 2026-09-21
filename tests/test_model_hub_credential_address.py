"""A credential's address never survives into stored or displayed model names.

The engine reaches one credential through the model field of one outbound
request and through no other field, so a call spells its target
``<source prefix>/<model>``. That prefix addresses a credential; it is not part
of what the model is called. These tests hold the boundary in both directions:
nothing the product stores, shows, or matches against carries an address, and a
file an earlier release wrote with one still loads.

The address is removed where its owner is known, so what gets removed is a
credential's own address and never a name that merely looks like one. Discovery
knows the credential it is discovering and an engine record names the prefix it
is addressed by. A config file names no prefix, so the config layer renames
nothing: it stores the name it is given, and a file an earlier release addressed
is repaired by the runtime, which holds the credential and can move an id in
every place it is a join key at once. See
``docs/plans/model-hub-credential-address-repair.md``.
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
from core.handlers.model_hub.adapter import SourceBinding
from vibe.model_hub_runtime.adapter import _discovered_models
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, SourceRecord

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


def test_discovery_coalesces_two_rows_the_engine_answers_for_one_model() -> None:
    """Removing an address can land two of the engine's rows on one name.

    The inventory holds one row per name, so the rows are coalesced rather than
    the later one dropped. What the engine reported about a model may not depend
    on which position it reported it in — and the inventory writer refuses a
    duplicate outright, so dropping one silently is not an option either.
    """

    models = _discovered_models(
        {
            "models": [
                {"id": f"{ADDRESS}/gpt-5.5", "supported_parameters": ["reasoning"]},
                {"id": "gpt-5.5", "supported_parameters": ["tools", "reasoning"]},
            ]
        },
        ADDRESS,
    )

    assert [model.id for model in models] == ["gpt-5.5"]
    assert models[0].supported_parameters == ("reasoning", "tools")


def test_discovery_keeps_metadata_only_a_later_row_carries() -> None:
    """Order decides nothing: the row that knew something is the one believed."""

    models = _discovered_models(
        {
            "models": [
                {"id": f"{ADDRESS}/gpt-5.5"},
                {"id": "gpt-5.5", "supported_parameters": ["reasoning"]},
            ]
        },
        ADDRESS,
    )

    assert [(model.id, model.supported_parameters) for model in models] == [
        ("gpt-5.5", ("reasoning",))
    ]


def test_discovery_leaves_a_name_the_engine_listed_twice_to_its_first_answer() -> None:
    """Coalescing is for the merge this boundary makes, not for the engine's own.

    One spelling listed twice is the engine contradicting itself, and the first
    answer has always been the one kept. Removing an address does not change
    that; it only means two names the engine kept apart can now meet.
    """

    models = _discovered_models(
        {
            "models": [
                {"id": "gpt-5.5", "supported_parameters": ["reasoning"]},
                {"id": "gpt-5.5", "supported_parameters": ["tools"]},
            ]
        },
        ADDRESS,
    )

    assert [(model.id, model.supported_parameters) for model in models] == [
        ("gpt-5.5", ("reasoning",))
    ]


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


def _bound_store(tmp_path) -> tuple[EngineStateStore, str]:
    store = EngineStateStore(tmp_path / "state")
    store.prepare_instance("install-1")
    (store.auth_dir / "claude-account.json").write_text("{}", encoding="utf-8")
    credential_ref = store.bind_oauth_credential(
        "src_fixture123",
        "anthropic",
        "claude-account.json",
    )
    return store, credential_ref


def _sync_binding(credential_ref: str, **overrides: object) -> SourceBinding:
    payload = {
        "source_id": "src_fixture123",
        "vendor": "anthropic",
        "protocol": "anthropic",
        "base_url": None,
        "credential_ref": credential_ref,
        "allowed_origins": ("claude",),
        "model_ids": ("gpt-5.5",),
    }
    payload.update(overrides)
    return SourceBinding(**payload)  # type: ignore[arg-type]


def test_a_source_holding_both_spellings_still_reaches_the_engine(tmp_path) -> None:
    """Config keeps both spellings, so the engine projection must survive them.

    A Source whose file still carries an addressed row next to the bare name
    projects one reasoning entry per row, and both name one model once the
    address is gone. Refusing that pair would abort the whole sync and leave the
    engine holding nothing — so it collapses onto the first, exactly as the
    model ids beside it and a stored record on load already do.
    """

    store, credential_ref = _bound_store(tmp_path)
    # The address this credential is actually reached by, which is the only one
    # the projection removes.
    address = store.credential_metadata(credential_ref)["prefix"]

    records = store.sync_sources(
        [
            _sync_binding(
                credential_ref,
                model_ids=(f"{address}/gpt-5.5", "gpt-5.5"),
                model_reasoning_efforts=(
                    (f"{address}/gpt-5.5", ("high",)),
                    ("gpt-5.5", ()),
                ),
            )
        ]
    )

    assert records[0].model_ids == ("gpt-5.5",)
    assert records[0].model_reasoning_efforts == (("gpt-5.5", ("high",)),)


def test_one_model_named_twice_by_one_spelling_is_still_refused(tmp_path) -> None:
    """Collapsing absorbs the merge this layer makes, not a caller's own repeat."""

    store, credential_ref = _bound_store(tmp_path)

    with pytest.raises(EngineStateError, match="duplicate reasoning model id"):
        store.sync_sources(
            [
                _sync_binding(
                    credential_ref,
                    model_reasoning_efforts=(("gpt-5.5", ("high",)), ("gpt-5.5", ("low",))),
                )
            ]
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


def test_the_config_layer_stores_the_name_it_is_given(tmp_path) -> None:
    """Config renames nothing — not what a caller sends, not what a file holds.

    A config file names no prefix, so this layer cannot prove which segment of a
    model id addresses a credential; it would have to guess from the spelling,
    and a guess renames an upstream identity that happens to be spelled like
    one. An id is also the join key across the inventory, route hops, route
    keys, the backend menus, and the engine records — so renaming one from here
    moves it in a single place and leaves every other naming something gone.

    Repair belongs where both are answerable: the runtime holds the credential,
    and the service writes every one of those collections together. Until then
    an addressed row loads exactly as written, and one refresh of its Source
    replaces it.
    """

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            _config_payload(
                [
                    _model_payload(f"{ADDRESS}/gpt-5.5"),
                    _model_payload(f"{ADDRESS}/gpt-6-astra", "manual"),
                    _model_payload("x-ai/grok-4.6-latest"),
                ],
                [{"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}],
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    assert config_path.read_bytes() == original
    assert [model.id for model in loaded.model_hub.sources[0].models] == [
        f"{ADDRESS}/gpt-5.5",
        f"{ADDRESS}/gpt-6-astra",
        "x-ai/grok-4.6-latest",
    ]
    hops = loaded.model_hub.agents["claude"].routes["claude-opus-4-6"].hops
    assert [(hop.source_id, hop.model_id) for hop in hops] == [(SOURCE_ID, f"{ADDRESS}/gpt-5.5")]
