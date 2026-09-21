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

import asyncio
import copy
import json

import pytest

from config.v2_config import ModelHubConfig, V2Config
from core.handlers.model_hub.address_repair import (
    payload_carries_credential_address,
    repair_credential_addresses,
)
from core.handlers.model_hub.identifiers import (
    model_id_without_credential_address,
    normalized_model_id,
    usage_ledger_key,
)
from core.services.settings import default_config
from vibe import api
from core.handlers.model_hub.adapter import SourceBinding
from tests.test_model_hub_api import FakeAdapter, _service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter, _discovered_models
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


# --- The repair for files an earlier release already addressed -------------
#
# Config stores the name it is given, so the repair belongs where both halves
# of it are answerable at once: the runtime can prove which address belongs to
# which Source, and the whole config is in hand, so every collection keyed by a
# model id moves together. These tests hold that boundary.


def _agent_payload(
    *,
    routes: dict | None = None,
    models: list[dict] | None = None,
    removed: list[str] | None = None,
    checked: list[str] | None = None,
) -> dict:
    agent: dict = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "sources": {"order": [SOURCE_ID]},
        "routes": routes if routes is not None else {},
        "models": models if models is not None else [],
        "removed_model_ids": removed if removed is not None else [],
    }
    if checked is not None:
        agent["menu"] = {"view": "featured", "checked": list(checked)}
    return agent


def _hub_payload(
    models: list[dict],
    agent: dict | None = None,
    *,
    source_id: str = SOURCE_ID,
) -> dict:
    return {
        "sources": [
            {
                "id": source_id,
                "credential_ref": "cred_fixture123",
                "models": copy.deepcopy(models),
            }
        ],
        "agents": {"claude": agent if agent is not None else _agent_payload()},
    }


def test_the_repair_moves_an_id_in_every_place_it_is_a_join_key() -> None:
    """One rename, or none. A half-moved id names a model nothing can serve.

    The inventory row is only the first of six collections that key on a model
    id. Rename it alone and the route pointing at it, the route key the Agent
    resolves, the menu row the user sees, the checked entry, and the
    hidden-model list all keep naming something that no longer exists.
    """

    payload = _hub_payload(
        [_model_payload(f"{ADDRESS}/gpt-5.5"), _model_payload("x-ai/grok-4.6-latest")],
        _agent_payload(
            routes={
                f"{ADDRESS}/gpt-5.5": {
                    "hops": [{"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}]
                }
            },
            models=[{"id": f"{ADDRESS}/gpt-5.5", "origin": "provider"}],
            removed=[f"{ADDRESS}/gpt-6-astra"],
            checked=[f"{ADDRESS}/gpt-5.5", "x-ai/grok-4.6-latest"],
        ),
    )

    repair = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    assert repair.changed
    agent = repair.payload["agents"]["claude"]
    assert [model["id"] for model in repair.payload["sources"][0]["models"]] == [
        "gpt-5.5",
        "x-ai/grok-4.6-latest",
    ]
    assert list(agent["routes"]) == ["gpt-5.5"]
    assert agent["routes"]["gpt-5.5"]["hops"] == [
        {"source_id": SOURCE_ID, "model_id": "gpt-5.5"}
    ]
    assert [model["id"] for model in agent["models"]] == ["gpt-5.5"]
    assert agent["removed_model_ids"] == ["gpt-6-astra"]
    assert agent["menu"]["checked"] == ["gpt-5.5", "x-ai/grok-4.6-latest"]
    assert (repair.models, repair.hops, repair.routes, repair.menu_entries) == (1, 1, 1, 3)


def test_the_repair_renames_nothing_it_cannot_prove_is_an_address() -> None:
    """Only an address this installation minted for this Source comes off.

    Without the proof, the only thing left to go on is the spelling — and a
    leading segment that merely looks minted belongs to whoever named the model.
    A Source whose credential could not be read is therefore left exactly as the
    previous release left it, which is a state the product already tolerates.
    """

    models = [
        _model_payload(f"{OTHER_ADDRESS}/gpt-5.5"),
        _model_payload("x-ai/grok-4.6-latest"),
        _model_payload("accounts/fireworks/models/mixtral"),
    ]
    payload = _hub_payload(models)

    unprovable = repair_credential_addresses(payload, {})
    foreign = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    assert not unprovable.changed
    assert not foreign.changed
    for result in (unprovable, foreign):
        assert [model["id"] for model in result.payload["sources"][0]["models"]] == [
            f"{OTHER_ADDRESS}/gpt-5.5",
            "x-ai/grok-4.6-latest",
            "accounts/fireworks/models/mixtral",
        ]


def test_the_repair_keeps_the_row_the_product_has_been_using() -> None:
    """A repaired row landing on a name already held gives way to the holder.

    Both name one model. The row already spelled bare is the one the tab lists
    and the ledger meters, and the repaired one carries nothing it does not.
    """

    payload = _hub_payload(
        [
            _model_payload("gpt-5.5", display_name="Kept"),
            _model_payload(f"{ADDRESS}/gpt-5.5", display_name="Dropped"),
            _model_payload(f"{ADDRESS}/{ADDRESS}/gpt-5.5", display_name="Also dropped"),
        ]
    )

    repair = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    assert [
        (model["id"], model["display_name"])
        for model in repair.payload["sources"][0]["models"]
    ] == [("gpt-5.5", "Kept")]
    assert repair.models == 2


def test_the_repair_keeps_the_first_of_two_rows_that_meet_only_here() -> None:
    """With no bare row to defer to, first wins — discovery's own policy."""

    payload = _hub_payload(
        [
            _model_payload(f"{ADDRESS}/gpt-5.5", display_name="First"),
            _model_payload(f"{ADDRESS}/{ADDRESS}/gpt-5.5", display_name="Second"),
        ]
    )

    repair = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    assert [
        (model["id"], model["display_name"])
        for model in repair.payload["sources"][0]["models"]
    ] == [("gpt-5.5", "First")]


def test_the_repair_merges_two_routes_that_meet_on_one_model() -> None:
    """Two keys, one model. The bare one is what the Agent already resolves.

    So it stays the route, in its own hop order, and gains only the hops the
    addressed key reached that it did not. A route dict cannot hold the same key
    twice, and a hop list cannot hold the same pair twice, so both collapse here
    or the repaired config does not load.
    """

    payload = _hub_payload(
        [_model_payload("gpt-5.5")],
        _agent_payload(
            routes={
                f"{ADDRESS}/gpt-5.5": {
                    "hops": [
                        {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"},
                        {"source_id": "src_backup", "model_id": "gpt-5.5-mini"},
                    ]
                },
                "gpt-5.5": {"hops": [{"source_id": SOURCE_ID, "model_id": "gpt-5.5"}]},
            },
        ),
    )

    repair = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    assert list(repair.payload["agents"]["claude"]["routes"]) == ["gpt-5.5"]
    assert repair.payload["agents"]["claude"]["routes"]["gpt-5.5"]["hops"] == [
        {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
        {"source_id": "src_backup", "model_id": "gpt-5.5-mini"},
    ]


def test_the_repair_unwraps_a_hop_against_the_source_that_hop_names() -> None:
    """A hop says which Source it reaches, so it is checked against that one.

    A chain crosses Sources, and each Source has its own address. Reading a hop
    against the wrong one would either miss the address it does carry or strip a
    name that only looks addressed.
    """

    payload = _hub_payload(
        [_model_payload("gpt-5.5")],
        _agent_payload(
            routes={
                "gpt-5.5": {
                    "hops": [
                        {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"},
                        {"source_id": "src_backup", "model_id": f"{ADDRESS}/gpt-5.5"},
                        {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
                    ]
                }
            },
        ),
    )

    repair = repair_credential_addresses(payload, {SOURCE_ID: ADDRESS})

    # The first hop is repaired; the second names a Source this address does not
    # belong to and is left alone; the third then collides with the first and is
    # dropped, because a route cannot list one pair twice.
    assert repair.payload["agents"]["claude"]["routes"]["gpt-5.5"]["hops"] == [
        {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
        {"source_id": "src_backup", "model_id": f"{ADDRESS}/gpt-5.5"},
    ]
    assert repair.hops == 1


def test_a_repaired_config_still_loads() -> None:
    """The terminal property: whatever the repair writes must parse again.

    Every collection it touches is uniqueness-checked on load, so a rename that
    creates a collision it does not absorb turns a loadable file into one that
    fails config load — strictly worse than the addressed id it set out to fix.
    """

    hub = _hub_payload(
        [
            _model_payload("gpt-5.5"),
            _model_payload(f"{ADDRESS}/gpt-5.5"),
            _model_payload(f"{ADDRESS}/gpt-6-astra", "manual"),
        ],
        _agent_payload(
            routes={
                f"{ADDRESS}/gpt-5.5": {
                    "hops": [
                        {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"},
                        {"source_id": SOURCE_ID, "model_id": "gpt-5.5"},
                    ]
                },
                "gpt-5.5": {"hops": []},
            },
            models=[
                {"id": "gpt-5.5", "origin": "provider"},
                {"id": f"{ADDRESS}/gpt-5.5", "origin": "provider"},
            ],
            removed=[f"{ADDRESS}/gpt-6-astra", "gpt-6-astra"],
        ),
    )
    hub["sources"][0].update(
        {
            "kind": "api_key",
            "vendor": "openai",
            "display_name": "Fixture",
            "protocol": "openai_responses",
            "base_url": None,
            "supply_channel": "hub",
            "billing": "metered",
            "state": {"status": "standby"},
        }
    )

    repair = repair_credential_addresses(hub, {SOURCE_ID: ADDRESS})
    reloaded = ModelHubConfig.from_payload(repair.payload)

    assert repair.changed
    assert [model.id for model in reloaded.sources[0].models] == ["gpt-5.5", "gpt-6-astra"]
    agent = reloaded.agents["claude"]
    assert list(agent.routes) == ["gpt-5.5"]
    assert [(hop.source_id, hop.model_id) for hop in agent.routes["gpt-5.5"].hops] == [
        (SOURCE_ID, "gpt-5.5")
    ]
    assert [model.id for model in agent.models] == ["gpt-5.5"]
    assert agent.removed_model_ids == ["gpt-6-astra"]
    assert not payload_carries_credential_address(repair.payload)


@pytest.mark.parametrize(
    ("hub", "carries"),
    [
        (_hub_payload([_model_payload(f"{ADDRESS}/gpt-5.5")]), True),
        (_hub_payload([_model_payload("x-ai/grok-4.6-latest")]), False),
        (
            _hub_payload(
                [_model_payload("gpt-5.5")],
                _agent_payload(checked=[f"{OTHER_ADDRESS}/gpt-5.5"]),
            ),
            True,
        ),
        (
            _hub_payload(
                [_model_payload("gpt-5.5")],
                _agent_payload(removed=[f"{ADDRESS}/gpt-6-astra"]),
            ),
            True,
        ),
        (
            _hub_payload(
                [_model_payload("gpt-5.5")],
                _agent_payload(
                    routes={
                        "gpt-5.5": {
                            "hops": [
                                {"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}
                            ]
                        }
                    }
                ),
            ),
            True,
        ),
        ({}, False),
    ],
)
def test_the_precheck_sees_every_collection_the_repair_would_rewrite(
    hub: dict,
    carries: bool,
) -> None:
    """A repair costs a credential read per Source; this decides whether to pay.

    It answers from the minted shape, which is exactly what a rename may not do
    — so it may only ever say "look closer", never "rename this". A collection
    it cannot see is one whose addressed id is never noticed at all, so it walks
    the same list the repair does.
    """

    assert payload_carries_credential_address(hub) is carries


def test_custody_is_what_proves_an_address(tmp_path) -> None:
    """The one answer that turns a spelling into a fact.

    Custody mints the address and is the only place it is written down. A
    credential it has no record of yields nothing, which is the honest answer:
    a caller that cannot prove ownership must leave the id alone.
    """

    store, credential_ref = _bound_store(tmp_path)
    adapter = CLIProxyEngineAdapter.__new__(CLIProxyEngineAdapter)
    adapter.state_store = store

    address = asyncio.run(adapter.credential_address(credential_ref))

    assert address == store.credential_metadata(credential_ref)["prefix"]
    assert asyncio.run(adapter.credential_address("cred_absent0000")) is None


class _AddressingAdapter(FakeAdapter):
    """An engine that knows which address belongs to which credential."""

    def __init__(self, addresses: dict[str, str]):
        super().__init__()
        self.addresses = addresses

    async def credential_address(self, credential_ref):
        return self.addresses.get(credential_ref)


def _addressed_service(tmp_path, addresses: dict[str, str]):
    service, store, adapter = _service(tmp_path, _AddressingAdapter(addresses))
    hub = _hub_payload(
        [_model_payload(f"{ADDRESS}/gpt-5.5"), _model_payload("x-ai/grok-4.6-latest")],
        _agent_payload(
            routes={
                f"{ADDRESS}/gpt-5.5": {
                    "hops": [{"source_id": SOURCE_ID, "model_id": f"{ADDRESS}/gpt-5.5"}]
                }
            },
            models=[{"id": f"{ADDRESS}/gpt-5.5", "origin": "provider"}],
        ),
    )
    hub["sources"][0].update(
        {
            "kind": "api_key",
            "vendor": "openai",
            "display_name": "Fixture",
            "protocol": "openai_responses",
            "base_url": None,
            "supply_channel": "hub",
            "billing": "metered",
            "state": {"status": "standby"},
        }
    )
    store.config = ModelHubConfig.from_payload(hub)
    return service, store, adapter


def test_the_runtime_repairs_a_stored_address_before_the_engine_sees_it(tmp_path) -> None:
    """An older release's file is healed on the way to the engine, and stays healed.

    This is the seam because it is the only one where both halves are in hand:
    the engine can prove the address, and the whole config is loaded, so every
    collection keyed by that id moves in one write. The engine is then sent the
    repaired projection, so it never sees the addressed spelling and never gets
    a second address composed onto one — which is the failure this fixes.
    """

    service, store, adapter = _addressed_service(tmp_path, {"cred_fixture123": ADDRESS})

    asyncio.run(service._prepare_engine_for_demand())

    assert [model.id for model in store.config.sources[0].models] == [
        "gpt-5.5",
        "x-ai/grok-4.6-latest",
    ]
    agent = store.config.agents["claude"]
    assert list(agent.routes) == ["gpt-5.5"]
    assert [(hop.source_id, hop.model_id) for hop in agent.routes["gpt-5.5"].hops] == [
        (SOURCE_ID, "gpt-5.5")
    ]
    assert [model.id for model in agent.models] == ["gpt-5.5"]
    assert adapter.synced[-1][0].model_ids == ("gpt-5.5", "x-ai/grok-4.6-latest")


def test_the_runtime_leaves_an_id_alone_when_the_address_is_unprovable(tmp_path) -> None:
    """No proof, no rename — the file is left exactly as the last release left it.

    An engine that cannot answer for the credential is the ordinary case during
    recovery, and a Source whose address is unknown is one whose ids cannot be
    told apart from a vendor's own slashed name. Guessing there would rename a
    model that was never addressed, so the repair declines and the startup it
    runs inside carries on.
    """

    service, store, adapter = _addressed_service(tmp_path, {})

    asyncio.run(service._prepare_engine_for_demand())

    assert [model.id for model in store.config.sources[0].models] == [
        f"{ADDRESS}/gpt-5.5",
        "x-ai/grok-4.6-latest",
    ]
    assert list(store.config.agents["claude"].routes) == [f"{ADDRESS}/gpt-5.5"]
