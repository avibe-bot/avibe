from __future__ import annotations

import copy
import json
import re
from dataclasses import fields
from itertools import product
from pathlib import Path
from typing import NamedTuple

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

from config.v2_config import (
    MODEL_HUB_ENABLED_ENV,
    MODEL_HUB_LEGACY_CREATED_AT,
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    is_model_hub_enabled,
)
from core.services.settings import default_config
from core.handlers.model_hub.adapter import (
    OBSERVATION_TERMINAL_RULES,
    ObservationDiscovery,
    ObservationOutcome,
    SOURCE_PROTOCOLS,
    SourceObservation,
    validate_source_observation,
)
from scripts.check_model_hub_authorities import check as check_model_hub_authorities
from scripts.check_model_hub_ui_states import ROOT, SPEC
from scripts.check_model_hub_ui_states import check as check_model_hub_ui_states
from vibe import api

CONTRACTS = Path("docs/plans/model-hub-contracts")
UI_MODEL_CONSUMERS = Path("ui/src/components/settings/models")


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_valid(name: str, payload: dict) -> None:
    errors = sorted(
        Draft7Validator(_schema(name), format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


def test_protocol_vocabulary_matches_authority_and_rejects_removed_alias():
    protocols = tuple(_schema("source.schema.json")["properties"]["protocol"]["enum"])
    assert SOURCE_PROTOCOLS == protocols

    type_source = (UI_MODEL_CONSUMERS / "types.ts").read_text(encoding="utf-8")
    type_match = re.search(
        r"export type SourceProtocol\s*=\s*(.*?);",
        type_source,
        re.DOTALL,
    )
    assert type_match is not None
    assert tuple(re.findall(r"'([^']+)'", type_match.group(1))) == protocols

    retired_alias = "openai" + "_compatible"
    for filename in (
        "types.ts",
        "vendorMeta.ts",
        "modelsApi.ts",
        "mockData.ts",
        "modelRows.test.ts",
    ):
        assert retired_alias not in (UI_MODEL_CONSUMERS / filename).read_text(
            encoding="utf-8"
        )

    example = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    example["protocol"] = retired_alias
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("source.schema.json")).validate(example)
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(example)


def test_unsaved_observation_schema_closes_all_terminal_shapes():
    schema = _schema("observation-result.schema.json")
    assert schema["properties"]["contract_version"]["const"] == 5
    assert tuple(schema["properties"]["outcome"]["enum"]) == tuple(
        member.value for member in ObservationOutcome
    )
    assert tuple(schema["properties"]["discovery"]["enum"]) == tuple(
        member.value for member in ObservationDiscovery
    )
    for example in schema["examples"]:
        _assert_valid("observation-result.schema.json", example)
    invalid = copy.deepcopy(schema["examples"][0])
    invalid["protocol"] = "openai" + "_compatible"
    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(invalid)


def test_observation_terminal_authority_and_schema_accept_the_same_products():
    schema = Draft7Validator(_schema("observation-result.schema.json"))
    reachable_domain = frozenset({True, False, None})
    authenticated_domain = frozenset({True, False, None})
    protocol_domain = frozenset({None, *SOURCE_PROTOCOLS})
    discovery_domain = frozenset(ObservationDiscovery)

    def payload(observation: SourceObservation) -> dict:
        return {
            "contract_version": 5,
            "outcome": observation.outcome.value,
            "reachable": observation.reachable,
            "authenticated": (
                "authenticated"
                if observation.authenticated is True
                else "rejected"
                if observation.authenticated is False
                else "unknown"
            ),
            "protocol": observation.protocol,
            "discovery": observation.discovery.value,
            "models": list(observation.model_ids),
        }

    assert set(OBSERVATION_TERMINAL_RULES) == set(ObservationOutcome)
    for outcome, rule in OBSERVATION_TERMINAL_RULES.items():
        legal_products = product(
            rule.reachable,
            rule.authenticated,
            rule.protocols,
            rule.discoveries,
        )
        for reachable, authenticated, protocol, discovery in legal_products:
            observation = SourceObservation(
                outcome=outcome,
                reachable=reachable,
                authenticated=authenticated,
                protocol=protocol,
                discovery=discovery,
                model_ids=(),
            )
            assert validate_source_observation(observation) is observation
            schema.validate(payload(observation))

        baseline = SourceObservation(
            outcome=outcome,
            reachable=next(iter(rule.reachable)),
            authenticated=next(iter(rule.authenticated)),
            protocol=next(iter(rule.protocols)),
            discovery=next(iter(rule.discoveries)),
            model_ids=(),
        )
        invalid_fields = {
            "reachable": reachable_domain - rule.reachable,
            "authenticated": authenticated_domain - rule.authenticated,
            "protocol": protocol_domain - rule.protocols,
            "discovery": discovery_domain - rule.discoveries,
        }
        for field_name, rejected_values in invalid_fields.items():
            for rejected in rejected_values:
                invalid_observation = SourceObservation(
                    **{**baseline.__dict__, field_name: rejected}
                )
                with pytest.raises(ValueError):
                    validate_source_observation(invalid_observation)
                with pytest.raises(ValidationError):
                    schema.validate(payload(invalid_observation))

        if rule.models_must_be_empty:
            invalid_inventory = SourceObservation(
                **{**baseline.__dict__, "model_ids": ("model-id",)}
            )
            with pytest.raises(ValueError):
                validate_source_observation(invalid_inventory)
            with pytest.raises(ValidationError):
                schema.validate(payload(invalid_inventory))
        else:
            succeeded = SourceObservation(
                **{
                    **baseline.__dict__,
                    "discovery": ObservationDiscovery.SUCCEEDED,
                    "model_ids": ("model-id",),
                }
            )
            assert validate_source_observation(succeeded) is succeeded
            schema.validate(payload(succeeded))
            failed_with_inventory = SourceObservation(
                **{
                    **baseline.__dict__,
                    "discovery": ObservationDiscovery.FAILED,
                    "model_ids": ("model-id",),
                }
            )
            with pytest.raises(ValueError):
                validate_source_observation(failed_with_inventory)
            with pytest.raises(ValidationError):
                schema.validate(payload(failed_with_inventory))


def test_final_model_validator_requires_explicit_credential_free_efforts():
    example = copy.deepcopy(_schema("source.schema.json")["examples"][0]["models"][0])
    example.pop("reasoning_efforts")
    with pytest.raises(ValueError):
        ModelHubModelConfig.from_payload(example)
    example["reasoning_efforts"] = ["authorization: sk-test-credential-material"]
    with pytest.raises(ValueError):
        ModelHubModelConfig.from_payload(example)


def test_source_validator_enforces_final_cross_field_and_inventory_rules():
    native = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    native["models"] = [native["models"][0], copy.deepcopy(native["models"][0])]
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(native)

    manual = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    manual["models"][0]["discovered_at"] = "2026-08-09T00:00:00Z"
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(manual)

    hub_without_ref = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    hub_without_ref["credential_ref"] = None
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(hub_without_ref)

    duplicate_native = ModelHubConfig().to_payload()
    native_example = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    duplicate_native["sources"] = [native_example, {**native_example, "id": "src_claudepro2"}]
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(duplicate_native)

    credential_url = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    credential_url["base_url"] = "https://relay.example/v1?api_key=secret"
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(credential_url)

    safe_query_url = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    safe_query_url["base_url"] = "https://relay.example/v1/?api-version=2026-07-23"
    canonical_vendor = copy.deepcopy(safe_query_url)
    canonical_vendor["vendor"] = " OpenAI "
    assert ModelHubSourceConfig.from_payload(canonical_vendor).vendor == "openai"
    parsed = ModelHubSourceConfig.from_payload(safe_query_url)
    assert parsed.base_url == "https://relay.example/v1?api-version=2026-07-23"


def test_persisted_model_hub_identifiers_share_the_canonical_validation_boundary():
    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["menu"]["checked"] = ["gpt-5"]
    opencode["routes"]["gpt-5"] = {"hops": []}
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)

    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["routes"]["custom/authorization: sk-test-credential-material"] = {
        "hops": []
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)

    payload = ModelHubConfig().to_payload()
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    payload["sources"] = [source]
    codex_route = next(iter(payload["agents"]["codex"]["routes"].values()))
    codex_route["hops"] = [
        {
            "source_id": source["id"],
            "model_id": "authorization: sk-test-credential-material",
        }
    ]
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_pointer(document: dict, pointer: str):
    value = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _vocabulary(schema: dict, paths: list[str], *, exclude=()) -> set:
    values = set()
    for pointer in paths:
        node = _json_pointer(schema, pointer)
        values.update(node["enum"] if "enum" in node else [node["const"]])
    return values - set(exclude)


def _validate_mirror_entry(entry: dict, schemas: dict[str, dict]) -> None:
    rule = entry["rule"]
    if rule == "none":
        assert entry["reason"]
        return
    if rule == "equality":
        normalized = []
        for item in entry["sets"]:
            actual = _vocabulary(
                schemas[item["schema"]],
                item["paths"],
                exclude=item.get("exclude", ()),
            )
            extras = set(item.get("extras", ()))
            normalized.append(actual - extras)
            assert actual == (actual - extras) | extras
        assert all(values == normalized[0] for values in normalized[1:])
        return
    if rule == "mapping":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        targets = entry.get("targets")
        if targets is None:
            targets = [{**entry["target"], "mapping": entry["mapping"]}]
        for item in targets:
            target = _vocabulary(
                schemas[item["schema"]],
                [item["path"]],
                exclude=item.get("exclude", ()),
            )
            mapping = item["mapping"]
            assert home == set(mapping)
            assert target == set(mapping.values())
        return
    if rule == "partition":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        member = _vocabulary(
            schemas[entry["member"]["schema"]],
            [entry["member"]["path"]],
        )
        exclusions = set(entry["exclusions"])
        for item in entry["exclusion_sets"]:
            exclusions |= _vocabulary(schemas[item["schema"]], [item["path"]])
        assert not (member & exclusions)
        assert home == member | exclusions
        return
    if rule == "bijection":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
        )
        target = set()
        for item in entry["target_sets"]:
            target |= _vocabulary(schemas[item["schema"]], item["paths"])
        assert set(entry["pairs"]) <= home
        assert len(set(entry["pairs"].values())) == len(entry["pairs"])
        assert target == set(entry["pairs"].values())
        return
    if rule == "projection":
        home = _vocabulary(
            schemas[entry["home"]["schema"]],
            [entry["home"]["path"]],
            exclude=entry["home"].get("exclude", ()),
        )
        for item in entry["targets"]:
            target = _vocabulary(
                schemas[item["schema"]],
                [item["path"]],
                exclude=item.get("exclude", ()),
            )
            assert target == home - set(item["drop_from_home"])
        return
    raise AssertionError(f"unknown mirror rule: {rule}")


def _mirror_schemas(registry: dict) -> dict[str, dict]:
    names = {
        value["schema"]
        for entry in registry["entries"]
        for key in ("sets", "target_sets", "targets", "exclusion_sets")
        for value in entry.get(key, [])
    }
    for entry in registry["entries"]:
        for key in ("home", "target", "member"):
            if key in entry:
                names.add(entry[key]["schema"])
    return {name: _schema(name) for name in names}


def test_frozen_source_and_agent_examples_round_trip_byte_faithfully():
    assert Path("core/handlers/model_hub/adapter.py").read_bytes() == (CONTRACTS / "adapter-interface.py").read_bytes()

    for example in _schema("source.schema.json")["examples"]:
        serialized = ModelHubSourceConfig.from_payload(example).to_payload()
        expected = json.loads(json.dumps(example))
        expected.setdefault("created_at", MODEL_HUB_LEGACY_CREATED_AT)
        if "usage" in expected:
            expected["usage"].setdefault("projected_exhaust_at", None)
        assert _canonical(serialized) == _canonical(expected)
        _assert_valid("source.schema.json", serialized)

    for raw_example in _schema("agent-supply.schema.json")["examples"]:
        example = {
            key: value
            for key, value in raw_example.items()
            if key not in {"builtin_models", "standard_vendors"}
        }
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        # `builtin_models` and `standard_vendors` are read-only endpoint
        # projections (v1.2), not persisted config — reconstruct them
        # the way `_agent_payload` merges them onto to_payload().
        serialized = {
            **agent.to_payload(),
            "builtin_models": raw_example.get("builtin_models"),
            "standard_vendors": raw_example.get("standard_vendors"),
        }
        if "routes" not in raw_example:
            serialized.pop("routes", None)
        if "sources" not in raw_example:
            serialized.pop("sources")
        else:
            serialized["sources"]["eligibility"] = raw_example["sources"].get("eligibility")
        assert _canonical(serialized) == _canonical(raw_example)
        _assert_valid("agent-supply.schema.json", serialized)


def test_every_frozen_schema_example_is_valid_and_json_round_trips():
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for example in schema.get("examples", []):
            validator.validate(example)
            assert _canonical(json.loads(_canonical(example))) == _canonical(example)


def test_agent_supply_contract_accepts_unmapped_native_alias_selection():
    payload = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "selected_by_agent": None,
        "selected_model_id": "claude-opus-4-5",
        "selected_model_explicit": True,
        "sources": {
            "order": ["src_anthropic1"],
            "eligibility": [
                {
                    "source_id": "src_anthropic1",
                    "eligible": True,
                    "reason_key": None,
                    "in_current_model_chain": True,
                    "process_availability_reason": None,
                }
            ],
        },
        "supply_status": "ok",
        "menu": None,
        "model_supply": [
            {"model_id": "claude-opus-4-5", "chain_length": 1}
        ],
        "builtin_models": ["claude-opus-4-5"],
        "standard_vendors": None,
        "named_agents": [],
    }

    _assert_valid("agent-supply.schema.json", payload)


def test_v5_mirror_registry_is_executable_and_complete():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    assert registry["contract_version"] == 5
    ids = [entry["id"] for entry in registry["entries"]]
    assert ids
    assert len(ids) == len(set(ids))
    for entry in registry["entries"]:
        _validate_mirror_entry(entry, schemas)


def test_v5_mirror_registry_mutation_probes_detect_every_comparable_drift():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    for entry in registry["entries"]:
        if entry["rule"] == "none":
            continue
        if entry["rule"] == "bijection":
            locations = [(location, location["paths"][0]) for location in entry["target_sets"]]
        elif entry["rule"] == "equality":
            locations = [(location, pointer) for location in entry["sets"] for pointer in location["paths"][:1]]
        elif entry["rule"] == "mapping":
            targets = entry.get("targets")
            if targets is None:
                targets = [entry["target"]]
            locations = [
                (entry["home"], entry["home"]["path"]),
                *[(location, location["path"]) for location in targets],
            ]
        elif entry["rule"] == "partition":
            locations = [
                (entry["home"], entry["home"]["path"]),
                (entry["member"], entry["member"]["path"]),
            ]
        else:
            locations = [
                (entry["home"], entry["home"]["path"]),
                *[(location, location["path"]) for location in entry["targets"]],
            ]

        for location, pointer in locations:
            mutated = copy.deepcopy(schemas)
            node = _json_pointer(mutated[location["schema"]], pointer)
            if "enum" in node:
                node["enum"].append(f"mutation_{entry['id'].lower()}")
            else:
                assert "const" in node, entry["id"]
                node["const"] = f"mutation_{entry['id'].lower()}"

            with pytest.raises(AssertionError):
                _validate_mirror_entry(entry, mutated)


def test_model_hub_authority_closure_is_generated_from_live_files():
    result = check_model_hub_authorities(Path.cwd())
    assert result["input_mode"] == "same_run_live_files"
    assert result["input_fingerprint"]
    assert result["ok"], result["findings"]


def test_model_hub_ui_state_completeness_is_generated_from_live_files():
    result = check_model_hub_ui_states(Path.cwd())
    assert result["input_mode"] == "same_run_live_files"
    assert result["input_fingerprint"]
    # A gate that scans nothing reports green. Assert the extractors reached the
    # document before trusting the verdict they produce.
    assert not result["empty_inventories"], result["empty_inventories"]
    assert result["input_scale"]["register rows"] > 50
    # A count is the only thing that notices an extractor going quietly narrow.
    # Class B's copy citations were read for one round by a single test — the
    # first segment has to be a declared namespace — which is true of the way
    # half this document cites copy and false of the other half, where a frame
    # cites its own table's rows bare. The gate stayed green throughout, because
    # what it had stopped reading it also stopped judging. This floor sits under
    # the 157 the document currently holds, far enough not to be churn and close
    # enough that dropping a family of citations trips it.
    assert result["input_scale"]["prose key references"] > 120
    # The document and every authority came from one place, and the run says
    # which. A verdict that does not name its authority origin cannot be told
    # apart from one that read the wrong revision's contracts.
    assert result["authority_origin"] == "this checkout"
    # A declared range nobody reads is a comment wearing a constraint's clothes.
    assert not result["unread_scopes"], result["unread_scopes"]
    assert result["ok"], result["findings"]


class GateCase(NamedTuple):
    """One reintroduced defect, filed by what it proves rather than by its bug.

    `cls` is the gate class expected to report it, `universe` the named
    inventory the comparison runs against (None when the case exercises a
    class's own logic rather than the shared comparator), and `rule` one of the
    comparator's four structural rules — or `arm`, for a class-logic case that
    predates them and still has to keep working.

    `within` is the section the anchor is read inside; see `_region`.
    """

    cls: str
    universe: str | None
    rule: str
    label: str
    before: str
    after: str
    says: str
    within: str


# The three rules, restated as what a mutation has to show:
#
#   token      — a *near miss*: a name a substring, prefix or suffix matcher
#                would have credited. This is the one that regressed twice.
#                A cell filled with one direction is not filled: prefix and
#                suffix fail differently, and set intersection catches the
#                prefix for free while an unbounded extraction reads the suffix
#                as a hit. Every direction the extraction admits needs a case,
#                or the grid reads full over a rule that was never exercised.
#                Enumerating directions does not terminate, though — a boundary
#                written against the reported `G-15x` still credited `G-15.1`.
#                So the cases here exist to hold a rule that is stated
#                positively (what may end a name) rather than to chase the
#                spellings that break a negative one.
#   empty      — a *total miss*: nothing resolves, and the gate reports it
#                instead of skipping the comparison in silence.
#   duplicate  — one canonical token declared twice with different content.
#
# Filed by (class, universe, rule) rather than by bug, because the defect this
# suite exists to prevent is not any single bug: it is a new class arriving with
# a comparison of its own and nobody noticing which rules it forgot.
GATE_MUTATIONS: tuple[GateCase, ...] = (
    # --- A: routes, states, treatments ------------------------------------
    # Anchored on a route exactly one register row names, which is what the
    # case needs and is not a property of any particular route: round 20 gave
    # `POST /api/models/oauth/cancel` a second namer — §1.4's *OAuth failed* row
    # now issues the same cleanup — and this case went on passing on the class-E
    # half while the arm it was written for stopped firing. The anchor is the
    # whole cell for the reason stated at the `treatment removed` case below.
    GateCase(
        "A", "routes", "token",
        "register row drifts onto a longer route",
        "重新拉取 pressed — `POST /api/models/sources/<source_id>/refresh`, guarded",
        "重新拉取 pressed — `POST /api/models/sources/<source_id>/refreshes`, guarded",
        "is named by no §0.8 row",
        within="0.8",
    ),
    GateCase(
        "A", "states", "token",
        "an exit points at a prefix of a real state",
        "| → Unreachable / Sources unread / Partial | — |",
        "| → Unreach / Sources unread / Partial | — |",
        "names no F1–F5 and no known state",
        within="0.8",
    ),
    GateCase(
        "A", "states", "empty",
        "an exit points at a state nobody wrote",
        "| → Unreachable / Sources unread / Partial | — |",
        "| → Nowhere at all / Sources unread / Partial | — |",
        "names no F1–F5 and no known state",
        within="0.8",
    ),
    GateCase(
        "A", "states", "duplicate",
        "two rows in one frame under one state name",
        "| §1.0 | Impaired |",
        "| §1.0 | Ready |",
        "`1.0 · Ready` is defined twice in states",
        within="0.8",
    ),
    GateCase(
        "A", "treatments", "token",
        "the treatment a cell names becomes a prefix of the one defined",
        "| F1 | Retry in place |",
        "| F10 | Retry in place |",
        "names F1, which §0.8's closed set does not define",
        within="0.8",
    ),
    GateCase(
        "A", "treatments", "empty",
        "treatment misnamed",
        "| F4 — `POST /api/models/oauth",
        "| F9 — `POST /api/models/oauth",
        "which §0.8's closed set does not define",
        within="0.8",
    ),
    GateCase(
        "A", "treatments", "duplicate",
        "§0.8's closed set defines one number twice",
        "| F2 | Keep the last good result |",
        "| F1 | Keep the last good result |",
        "`F1` is defined twice in treatments",
        within="0.8",
    ),
    # The defect this gate was built for: a state issues a call and no row
    # states what happens when it fails. This is the shape that survived review
    # at f22c2a59 and had to be found by a human.
    #
    # The anchor is the whole cell. Written as a fragment, the replacement left
    # the rest of the sentence in place, class A fired for an unrelated reason —
    # the route was no longer covered — and the case passed a class-only
    # assertion while the arm it was written for never ran.
    GateCase(
        "A", None, "arm",
        "treatment removed",
        "| F4 — `POST /api/models/oauth/cancel` is issued as the dialog closes "
        "and its result is not awaited (D-15) |",
        "| — |",
        "states no failure treatment",
        within="0.8",
    ),
    # `[contract-gap]` is also the marker that tells a checker to stop asking.
    # Pointed at a number no §0.5 row defines, it must silence nothing at all.
    GateCase(
        "A", "gaps", "empty",
        "gap marker cites an unregistered number",
        "`[contract]` `[contract-gap]` G-15 carries",
        "`[contract]` `[contract-gap]` G-99 carries",
        "is named by no §0.8 row",
        within="1.5",
    ),
    # The same rule read from the registry side: a row that stops parsing as a
    # row stops being a registration, and the route it was excusing goes back to
    # being a contracted call this document reaches from nowhere. Both halves of
    # the silencer are one comparison, but only this half exercises the parse
    # that decides what a registration *is* — and it is a parse the citation
    # cases cannot reach, because they never look at a row.
    #
    # The anchor is G-12 because G-12's two routes are named *by* that row and
    # nowhere else, so breaking it strands exactly what the row was registering.
    # It used to be G-13, which stranded `PUT /api/models/agents/<backend>/chain`
    # — a route G-13 does not register and only mentions as evidence. That made
    # the case pass for the wrong reason and hid the arm's real defect: an
    # excusing row silences every route token anywhere inside it. R29 gave the
    # chain `PUT` a §0.4 row of its own, the incidental excuse stopped being
    # load-bearing, and this case went red and said so.
    GateCase(
        "A", "gaps", "empty",
        "a gap row loses the number that makes it a registration",
        "| G-12 |",
        "| gap 12 |",
        "is contracted and reached by no §0.8 row, no §0.5 gap and no §0.4 row",
        within="0.5",
    ),
    # The same row, with its route left in place and moved one column right. A
    # register accounts for what it declares missing; the column beside it is
    # where the row argues the absence is real, and argument names whatever it
    # needs to name. Reading the whole row made those two the same act, which is
    # how §0.5's G-13 — a row about a bulk re-apply that does not exist, citing
    # the chain `PUT` only to say nothing bulk-rewrites stored chains — excused
    # the one contracted mutation this document reached from nowhere. The defect
    # survived twenty-nine review rounds and was found by hand, not here.
    GateCase(
        "A", "gaps", "arm",
        "a gap row's route moves from what it registers into why it is true",
        "by sending the contracted `DELETE /api/models/sources/<id>` | the only 移除",
        " | `api.md` contracts `DELETE /api/models/sources/<id>`, and the only 移除",
        "is contracted and reached by no §0.8 row, no §0.5 gap and no §0.4 row",
        within="0.5",
    ),
    # And the register whose declared column cannot be found at all. A column is
    # located by its header, so renaming one is the one edit that could quietly
    # turn the whole arm off — and the failure has to be that every route §0.5
    # was covering comes back, never that §0.5 goes on covering them by position.
    GateCase(
        "A", "gaps", "arm",
        "the gap register renames the column that says what is missing",
        "| # | Surface | Missing | Verified absent at `ceace07f` |",
        "| # | Surface | What is absent | Verified absent at `ceace07f` |",
        "is contracted and reached by no §0.8 row, no §0.5 gap and no §0.4 row",
        within="0.5",
    ),
    # The same marker pointed at a *prefix* of a registered number. Set
    # intersection got this right by accident and would have gone on getting it
    # right; the case is here because the cell is, and because the silencer is
    # now resolved like every other name.
    GateCase(
        "A", "gaps", "token",
        "gap marker cites a prefix of a registered number",
        "`[contract]` `[contract-gap]` G-15 carries",
        "`[contract]` `[contract-gap]` G-1 carries",
        "is named by no §0.8 row",
        within="1.5",
    ),
    # The other direction, and the one the cell above was standing in for: the
    # extraction stopped at the last digit it wanted rather than at the end of
    # the number, so `G-15x` was read as `G-15` and a route stayed silenced by a
    # registration written about something else. A prefix resolves to nothing
    # and fails loudly; a suffix resolved to a real row, which is why filling
    # this cell with the prefix alone left the rule untested for two rounds.
    GateCase(
        "A", "gaps", "token",
        "gap marker cites a suffix of a registered number",
        "`[contract]` `[contract-gap]` G-15 carries",
        "`[contract]` `[contract-gap]` G-15x carries",
        "is named by no §0.8 row",
        within="1.5",
    ),
    # The same near miss reached through a joiner instead of a letter. The first
    # boundary rejected `G-15x` and still credited `G-15.1`, because it was
    # written against the one direction a reviewer had named. A dot is the joiner
    # the document actually contains — it also ends citations as a full stop —
    # so it is the one direction the rule has to get right in both readings.
    GateCase(
        "A", "gaps", "token",
        "gap marker joins a registered number to a sub-number",
        "`[contract]` `[contract-gap]` G-15 carries",
        "`[contract]` `[contract-gap]` G-15.1 carries",
        "is named by no §0.8 row",
        within="1.5",
    ),
    # Reviewer's repro, round 14: a route first mentioned inside a
    # registered-gap paragraph and then drawn as an unmarked affordance. The
    # dedupe ran before the verdict, so the first mention's excuse covered the
    # second, and the gate returned zero findings.
    GateCase(
        "A", "routes", "arm",
        "an unmarked affordance repeats a route a gap paragraph excused",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\n06's header carries a 移除来源 "
        "button that sends `DELETE /api/models/sources/<id>` and returns to 01.",
        "is named by no §0.8 row",
        within="1.4",
    ),
    # The mirror of class A, and the generator behind five findings across two
    # heads: a contracted mutation no surface reaches. Out of scope is a
    # legitimate answer; not saying anything is not.
    GateCase(
        "A", "routes", "arm",
        "a contracted mutation stops being accounted for",
        "| `POST /api/models/migration/scan` | The migration surface.",
        "| `POST /api/models/migration/scans` | The migration surface.",
        "is contracted and reached by no §0.8 row",
        within="0.4",
    ),
    # The defect that ran for nineteen rounds: §1.6 attributed §1.3's order save
    # to a bare `PUT`, which names no token, so every route arm read the sentence
    # as making no claim and it went on contradicting §0.8 in plain sight. The
    # innocent half of this rule is exercised by the unmutated spec, which writes
    # two dozen method words that name no other frame and stays green.
    GateCase(
        "A", "routes", "arm",
        "another frame's request named by method word",
        "because it is not yet a request. It moves a row",
        "because it is not yet a `PUT`. It moves a row",
        "with no path, so no route arm can read the claim",
        within="1.6",
    ),
    # Reviewer's finding, round 1 of #1276: the arm above read `§1.3` and nothing
    # else, while every other arm in this gate resolves a frame through its
    # aliases — display number and node id included. So the identical sentence
    # written "Frame 03's whole-order `PUT`" named a frame the arm could not see,
    # made no claim it could read, and passed. An arm that recognises one of a
    # frame's three names is not checking frames; it is checking one spelling.
    GateCase(
        "A", "frames", "arm",
        "another frame's request named by method word and display number",
        "**The question it answers:** *where do my tokens come from, and who is using\n"
        "which one right now?*",
        "**The question it answers:** *where do my tokens come from, and who is using\n"
        "which one right now?* Frame 03's whole-order `PUT` is not this frame's.",
        "names §1.3's request as `PUT` with no path",
        within="1.1",
    ),
    # One predicate written twice, eight hundred lines apart: the frame that owns
    # a failure disperses it into a set of its own states, and every other frame
    # that defers to it restates that set. Round 21 landed two findings out of
    # this shape and round 20 one more; each time a destination was added on one
    # side and the other kept the old list, and each reading stayed locally
    # plausible. Both directions get a case because the arm answers them with
    # different halves of one sentence, and a suite that only ever deleted a
    # destination would leave the other half unexecuted.
    #
    # Two frames now restate §1.0's set — §1.1 from the module and §1.8 from the
    # direct home — so each anchor carries the clause that says which of the two
    # it is. A mutation case is only a mutation if it names one edit; round 27
    # gave §1.8 the dispersal §1.1 already had and three anchors stopped being
    # unique the same afternoon.
    GateCase(
        "A", None, "arm",
        "a deferral to another frame's set loses a destination that set has",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "→ §1.0 Unreachable / §1.0 Sources unread — the same three "
        "§1.0 disperses first paint into, because this row is",
        "defers its failure to §1.0's set and does not name Partial",
        within="0.8",
    ),
    GateCase(
        "A", None, "arm",
        "a deferral names a landing the owning frame does not disperse into",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial / §1.0 Empty "
        "(no sources) — the same three §1.0 disperses first paint into, because "
        "this row is",
        "names Empty (no sources), which §1.0 does not disperse into",
        within="0.8",
    ),
    # Reviewer's finding, round 1 of #1276: both cases above compare one set of
    # destinations against another, and both arrive *after* the early return that
    # accepts any cell holding an arrow to a numbered section. That return read
    # neither half of `§1.0 Unreachable` — not the frame, not the state — so a
    # deferral could name a section the document does not have, or a state the
    # named frame does not file, and the dispersal arm would then skip it for
    # being unresolved. A destination nobody resolved is not a destination; it is
    # a sentence that looks like one, which is the failure this whole arm exists
    # to catch. Both halves get a case because they fail through different
    # universes and a single one would leave the other reading untested.
    GateCase(
        "A", "frames", "empty",
        "a deferral to a section number no frame carries",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "→ §1.99 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "defers to §1.99, which is no frame",
        within="0.8",
    ),
    GateCase(
        "A", "states", "empty",
        "a deferral to a state the frame it names does not file",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "→ §1.0 Unreachably / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this row is",
        "defers to 「Unreachably」 in §1.0, which files no such state",
        within="0.8",
    ),
    # Reviewer's finding, round 2 of #1276: a failure cell says how the failure
    # is treated *and* where it lands — 「F1 → Install failed」 is both — and the
    # arm returned as soon as it had read the treatment. The landing went unread
    # on every mixed cell in the register, which is most of them.
    GateCase(
        "A", "states", "arm",
        "a named treatment lands in a state nobody files",
        "F1 → Install failed",
        "F1 → Vanished forever",
        "treats its failure with F1 and then lands nowhere",
        within="0.8",
    ),
    # --- C: frames ----------------------------------------------------------
    # Reviewer's finding, round 1 of #1276: the N arm asked whether a landing
    # *contained* a registered state name, so 「Not startedness」 vouched for
    # itself by containing 「Not started」 and the mismatch was suppressed. A
    # substring test answers a question nobody asked — is this name written
    # somewhere inside that text — and reads as agreement precisely when the
    # destination is a near-miss, which is the shape a typo takes.
    GateCase(
        "C", None, "arm",
        "a dispatch destination that merely contains a state's name",
        "`not_started` → Not started, `degraded` → Impaired",
        "`not_started` → Not startedness, `degraded` → Impaired",
        "which §1.0 files as no state",
        within="0.8",
    ),
    # --- B: copy, slots -----------------------------------------------------
    # Reviewer's finding, round 2 of #1276: an i18next plural is one key written
    # on two rows, and a stem left with one of them fell through as an ordinary
    # single-row key. The cardinality the survivor does not cover renders nothing
    # at runtime, and the gate read the family as complete.
    GateCase(
        "B", "copy", "arm",
        "a plural family with one of its two forms deleted",
        "| `gateway.modelCount_other` | {{count}} 个型号 | {{count}} models |\n",
        "",
        "is written as a plural family and declares no `_other` row",
        within="1.0",
    ),
    # Reviewer's finding, round 2 of #1276: a citation was admitted only when its
    # first segment was a namespace some copy table declares, so a misspelt
    # namespace was dropped before the universe could answer — the same silence a
    # correct citation produces. `shell` is declared; `shel` is the typo.
    GateCase(
        "B", "copy", "empty",
        "a copy citation whose namespace is misspelt",
        "Tooltip: `shell.gatewayInfo.body`",
        "Tooltip: `shel.gatewayInfo.body`",
        "key `shel.gatewayInfo.body` is cited and never defined",
        within="1.0",
    ),
    GateCase(
        "B", "copy", "token",
        "a citation truncated to a prefix two keys share",
        "`shell.notStarted` | Run pill",
        "`shell.not` | Run pill",
        "key `shell.not` is cited and never defined",
        within="0.8",
    ),
    GateCase(
        "B", "copy", "empty",
        "key cited, never defined",
        "`shell.notStarted` | Run pill",
        "`shell.notStartd` | Run pill",
        "is cited and never defined",
        within="0.8",
    ),
    # Two rows in one table under one key ship whichever the loader read last.
    # The key is legitimately re-used across namespaces — every table has a
    # `title` — so only the qualified spelling collides.
    GateCase(
        "B", "copy", "duplicate",
        "one qualified key defined twice",
        "| `tiers.addFirst` | + 添加档位 | + Add tier |",
        "| `tiers.add` | + 添加档位 | + Add tier |",
        "is defined twice in copy",
        within="1.6",
    ),
    GateCase(
        "B", "slots", "token",
        "a declared slot truncated to a prefix of the one strings use",
        "| `{{host}}` | The source's host",
        "| `{{hos}}` | The source's host",
        "interpolates `{{host}}` with no §0.9 row",
        within="0.9",
    ),
    GateCase(
        "B", "slots", "empty",
        "undeclared slot",
        "| `install.retry` `[derived]` | 重试 | Try again |",
        "| `install.retry` `[derived]` | 重试 {{attempt}} | Try again {{attempt}} |",
        "with no §0.9 row",
        within="1.0",
    ),
    GateCase(
        "B", "slots", "duplicate",
        "§0.9 declares one slot twice",
        "| `{{source}}` | A source's display name. | Always present |",
        "| `{{host}}` | A source's display name. | Always present |",
        "`host` is defined twice in slots",
        within="0.9",
    ),
    # A key that would ship with no English string.
    GateCase(
        "B", None, "arm",
        "English column dropped",
        "| `install.progress` `[derived]` | 正在安装… | Installing… |",
        "| `install.progress` `[derived]` | 正在安装… |  |",
        "has no English column",
        within="1.0",
    ),
    GateCase(
        "B", None, "arm",
        "Chinese column dropped",
        "| `shell.title` | 模型 | Models |",
        "| `shell.title` |  | Models |",
        "has no Chinese column",
        within="1.0",
    ),
    # §0.9 names its consumers and the copy tables interpolate: one set, written
    # twice, by two authors who cannot see each other. Both directions get a
    # case, because they fail differently and only one of them is loud. A
    # declared key nobody interpolates is a stale row — visible to a reader who
    # goes looking. An interpolating key the row omits is the one that matters:
    # it is a consumer whose meaning was never checked against the sentence it
    # borrowed, which is how `{{status}}` came to mean an HTTP code for three
    # keys and supply health for a fourth.
    GateCase(
        "B", "slots", "arm",
        "§0.9 lists a consumer that interpolates nothing",
        "| `addSub.title`, `adopt.effects.1` |",
        "| `addSub.title`, `adopt.effects.1`, `order.title` |",
        "and no such copy row does",
        within="0.9",
    ),
    GateCase(
        "B", "slots", "arm",
        "a key interpolates a slot §0.9's row does not list",
        "| `addSub.title`, `adopt.effects.1` |",
        "| `addSub.title` |",
        "and §0.9's row does not list it",
        within="0.9",
    ),
    # The same generator one grain further out: a vocabulary declared as copy
    # keys and enumerated again as the strings those keys render. §1.0's mapping
    # gained a sixth status word and §2's ink rule went on listing four, which
    # nineteen rounds of reading by hand did not catch. `GATE_INNOCENT` holds
    # the other half of this rule — the part that says a mention is not an
    # enumeration.
    GateCase(
        "B", "copy", "arm",
        "a restated vocabulary loses the value that was just added",
        "网关 · 暂时全部在冷却, 网关 · 无可用来源, 网关 · 未选型号",
        "网关 · 暂时全部在冷却, 网关 · 未选型号",
        "is missing",
        within="1.9",
    ),
    # --- C: frames ----------------------------------------------------------
    GateCase(
        "C", "frames", "token",
        "a register row filed under a prefix of a real section",
        "| §1.0 | Impaired |",
        "| §1 | Impaired |",
        "filed under §1, which is no §1 section",
        within="0.8",
    ),
    GateCase(
        "C", "frames", "empty",
        "a register row filed under a section that does not exist",
        "| §1.0 | Impaired |",
        "| §1.60 | Impaired |",
        "filed under §1.60, which is no §1 section",
        within="0.8",
    ),
    GateCase(
        "C", "frames", "duplicate",
        "two §1 headings claim one number",
        "### 1.9 Frame 10 `g7MOA4`",
        "### 1.8 Frame 10 `g7MOA4`",
        "`1.8` is defined twice in frames",
        within="1.9",
    ),
    # A state a user can enter and not leave.
    GateCase(
        "C", None, "arm",
        "exit removed",
        "| 取消 / 关闭 / Escape → close, discarding uncommitted moves; 保存顺序 → Saving |",
        "| — |",
        "has no exit",
        within="0.8",
    ),
    # A section that answers its other failures for both origins, with one
    # failure left written once — the reader cannot tell whether pulling from a
    # stopped engine fails the way adding does.
    GateCase(
        "C", None, "arm",
        "origin half deleted",
        "| ⑥′ Engine unavailable",
        "| ⑥″ Engine unavailable",
        "has no ′ row",
        within="0.8",
    ),
    # The other half of the same arm: a step only one origin performs is
    # admissible, and what makes it admissible is the sentence naming the twin
    # it does not have. Strike the sentence and the row is indistinguishable
    # from one whose second half was forgotten — which is the reading the arm
    # must take, since it is the one that can be wrong.
    GateCase(
        "C", None, "arm",
        "a single-origin failure stops declaring the twin it does not have",
        "There is no ⑦′, because Pull origin persists nothing",
        "Pull origin persists nothing",
        "does not say 「no ⑦′」 either",
        within="0.8",
    ),
    # A recovery exit that re-reads a collection and branches on the answer is a
    # dispatch, and owes every reading a landing. The arm used to find its
    # dispatcher only in *failure* cells, where exactly one frame has one — so
    # this frame, mapping table and all, was skipped whole. Anchored on §1.6
    # because it is the frame that proves the widening: nothing here disperses a
    # failure, and before the fix no row in it was ever read as a router.
    GateCase(
        "C", None, "arm",
        "a routing exit drops one of its field's readings",
        "`needs_action` → Needs action, `error`",
        "`error`",
        "a row that routes this frame by `Source.state.status`",
        within="0.8",
    ),
    # §0.8 files the guarded refusal under the frames that reach it, and §1 prose
    # answers the same question again whenever it explains who opens the shared
    # confirm. §0.8 is the definition, so prose is the side that gets checked —
    # there is no second direction to write here, because a register compared
    # against itself reports itself. The innocent half is live in the unmutated
    # spec: §1.1 names §1.6 correctly, and this case is that same sentence
    # pointed one frame off.
    GateCase(
        "C", "frames", "arm",
        "prose attributes the guarded refusal to a frame §0.8 does not file it under",
        "exactly as §0.9 and §1.6 rule the same hole",
        "exactly as §0.9 and §1.3 rule the same hole",
        "reaches the guarded refusal",
        within="1.1",
    ),
    # The same misattribution in the document's other habit. A frame heading
    # gives the frame three names, and §1 prose uses all of them — "Frames 09
    # and 10 draw the header", "Deltas from 01". An arm that recognises only the
    # register's spelling reports the case above and lets this one through, so
    # the two cases differ by nothing except which name the claim is written in.
    GateCase(
        "C", "frames", "arm",
        "the same misattribution, written with the frame's display number",
        "exactly as §0.9 and §1.6 rule the same hole",
        "exactly as §0.9 and 03 rule the same hole",
        "reaches the guarded refusal",
        within="1.1",
    ),
    # --- D: copy ------------------------------------------------------------
    # Condition keys are stored bare and cited namespace-qualified, so the
    # citation test has to resolve both spellings to one copy row. Matching on
    # prefixes instead let a citation drift onto a longer key and still vouch
    # for the row it left behind.
    GateCase(
        "D", "copy", "token",
        "a citation drifts onto a longer key",
        "`sourceDetail.fail.tier`, `sourceDetail.retry`",
        "`sourceDetail.fail.tiers`, `sourceDetail.retry`",
        "condition key `fail.tier` is cited by no §0.8 row",
        within="0.8",
    ),
    GateCase(
        "D", "copy", "empty",
        "the row a live citation names is renamed out from under it",
        "| `fail.tier` `[derived]` | 档位没保存上",
        "| `fail.tierZ` `[derived]` | 档位没保存上",
        "condition key `fail.tierZ` is cited by no §0.8 row",
        within="1.6",
    ),
    # --- E: routes, schema files, schema fields, repo symbols ---------------
    GateCase(
        "E", "routes", "token",
        "a route extended past a real one by one segment",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/sources/order` with `{order: string[]}` `[contract]`",
        "is contracted by no `api.md` route row",
        within="0.8",
    ),
    # A route the spec names that the contract does not have. This is the class
    # the round-10 review found by hand, six times.
    GateCase(
        "E", "routes", "empty",
        "route literal drifts off api.md",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/source-order` with `{order: string[]}` `[contract]`",
        "is contracted by no `api.md` route row",
        within="0.8",
    ),
    GateCase(
        "E", "schema files", "token",
        "a schema citation extended past a real filename",
        "`source.schema.json` pins it non-null",
        "`agent-source.schema.json` pins it non-null",
        "`agent-source.schema.json` is not a file in",
        within="1.0",
    ),
    # Reviewer's finding, round 2 of #1276: the citation pattern admitted only
    # lowercase letters and hyphens, so the two commonest near-misses of a real
    # filename — a digit, an underscore — matched nothing and disappeared,
    # taking the field attributed to them along. What the extractor cannot spell
    # it cannot report, so the shape is now everything a filename may look like
    # and the universe is what says the name is unknown.
    GateCase(
        "E", "schema files", "arm",
        "a schema citation misspelt with a digit",
        "| `runtime-dependency.schema.json` → `status.health` `[contract]` |",
        "| `runtime1-dependency.schema.json` → `status.health` `[contract]` |",
        "`runtime1-dependency.schema.json` is not a file in",
        within="1.0",
    ),
    GateCase(
        "E", "schema files", "arm",
        "a schema citation misspelt with an underscore",
        "| `runtime-dependency.schema.json` → `status.health` `[contract]` |",
        "| `runtime_dependency.schema.json` → `status.health` `[contract]` |",
        "`runtime_dependency.schema.json` is not a file in",
        within="1.0",
    ),
    GateCase(
        "E", "schema files", "empty",
        "a schema citation naming no file",
        "`source.schema.json` pins it non-null",
        "`sources.schema.json` pins it non-null",
        "`sources.schema.json` is not a file in",
        within="1.0",
    ),
    # The mapping table no longer saying which of two same-named declarations it
    # renders. `supply_status` exists per backend and per named Agent, and
    # reading one as the other was the substitution round 10 opened on.
    GateCase(
        "E", "schema fields", "token",
        "mapping table stops naming which declaration it renders",
        "| `AgentSupply.supply_status` `[contract]` | Subtitle | Key |",
        "| `supply_status` `[contract]` | Subtitle | Key |",
        "independent places",
        within="1.0",
    ),
    # A `[contract]` header asserts that some schema owns the vocabulary below
    # it. When the field name resolves to no declaration, the set comparison has
    # nothing to compare and used to skip in silence — the one outcome a gate
    # that exists to catch drift may not have.
    GateCase(
        "E", "schema fields", "empty",
        "mapping table names a field no schema declares",
        "| `AgentSupply.supply_status` `[contract]` | Subtitle | Key |",
        "| `AgentSupply.supply_stat` `[contract]` | Subtitle | Key |",
        "the table maps no contracted field",
        within="1.0",
    ),
    GateCase(
        "E", "repo symbols", "token",
        "a cited symbol truncated to a prefix of the real one",
        "service.py:list_agents",
        "service.py:list_agent",
        "defines no `list_agent`",
        within="0.5",
    ),
    GateCase(
        "E", "repo symbols", "empty",
        "cited symbol no longer exists",
        "service.py:list_agents",
        "service.py:list_agent_rows",
        "defines no `list_agent_rows`",
        within="0.5",
    ),
    # Reviewer's finding, round 14: `ast.walk` credited every `Store` name in the
    # file, so any local variable vouched for a citation. `cancelled` is one —
    # a name assigned inside a method body, addressable by nobody.
    GateCase(
        "E", "repo symbols", "token",
        "a cited symbol is a name local to some function body",
        "service.py:list_agents",
        "service.py:cancelled",
        "defines no `cancelled`",
        within="0.5",
    ),
    # Reviewer's finding, round 1 of #1276: the inventory was a set of bare
    # names, each registered with the *file* as its content. So one file
    # defining `load` four times declared one token four times identically, the
    # duplicate rule could not fire on this universe however wrong the file got,
    # and a citation to any of the four resolved as though it named one thing.
    # Keyed on the qualified name with the bare name as an alias, both halves
    # come back: the qualified name carries the line, so two bodies under one
    # name contradict, and the bare name resolves to as many symbols as there
    # are — which is what this case reads. `service.py` really does define
    # `load` twice, under two owners; the citation is the thing at fault,
    # because it promised the reader one place to go.
    GateCase(
        "E", "repo symbols", "arm",
        "a citation names a symbol the file defines in more than one place",
        "service.py:list_agents",
        "service.py:load",
        "defines `load` in 2 places",
        within="0.5",
    ),
    # The §0.5 registry, read as a universe. Its three rules are exercised where
    # the marker is spent: class A's route coverage (above) and class E's claim
    # check (here). E owns the duplicate rule because a gap row is a claim about
    # what the contract does not have, and that is the class that checks those.
    #
    # Reviewer's repro, round 14: a contradicting second `G-19`. Built by hand,
    # the registry kept the later row and every reference went on resolving.
    GateCase(
        "E", "gaps", "duplicate",
        "a second row answers one gap number differently",
        "| G-19 | 05 add-by-key, 取消 pressed while a persisting add is in flight |",
        # Four cells, which is what §0.5's header declares. Written with five
        # until this round, when the malformed rule arrived and reported the
        # decoy for its cell count instead of registering it — a case that had
        # been proving the duplicate rule against a row the reader now declines
        # to read at all.
        "| G-19 | 05 add-by-key, an unrelated surface | a different missing behaviour "
        "| Contradicting evidence. |\n"
        "| G-19 | 05 add-by-key, 取消 pressed while a persisting add is in flight |",
        "is defined twice in gaps with different content",
        within="0.5",
    ),
    # A number no row defines silences nothing. This case used to be reached
    # from the registry side — de-number G-9's row and watch the 409 it excused
    # come back — and round 17 withdrew G-9, because the missing 409 was the
    # contract agreeing with itself rather than a debt. Nothing in the document
    # is silenced by a gap on E's side any more, so the claim is written by the
    # mutation, exactly as the three `token` cases below write theirs. The
    # registry side kept its coverage and moved to class A, where a de-numbered
    # row still costs a route its excuse: see the G-13 case above.
    GateCase(
        "E", "gaps", "empty",
        "a silenced claim cites a number no row defines",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA 409 conflict answer to "
        "`PUT /api/models/agents/<backend>/sources` is `[contract-gap]` G-99.",
        "a 409 branch is claimed for",
        within="1.4",
    ),
    # E's own use of the marker, against a near miss: the claim cites `G-1`,
    # which is a prefix of `G-15` and a row nobody wrote. Citing a registered
    # number silences the same sentence, which is the half that makes this a
    # test of identity rather than of the marker being ignored.
    GateCase(
        "E", "gaps", "token",
        "a silenced claim cites a prefix of a registered number",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA 409 conflict answer to "
        "`PUT /api/models/agents/<backend>/sources` is `[contract-gap]` G-1.",
        "a 409 branch is claimed for",
        within="1.4",
    ),
    # The same suffix miss on the other arm that honours the marker. Both ask
    # through one comparison, so one boundary fixes both — and one direction
    # tested on one arm proves neither, which is how a cell that reads full in
    # two classes at once can still be describing a rule nobody ran.
    GateCase(
        "E", "gaps", "token",
        "a silenced claim cites a suffix of a registered number",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA 409 conflict answer to "
        "`PUT /api/models/agents/<backend>/sources` is `[contract-gap]` G-9x.",
        "a 409 branch is claimed for",
        within="1.4",
    ),
    GateCase(
        "E", "gaps", "token",
        "a silenced claim joins a registered number to a sub-number",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA 409 conflict answer to "
        "`PUT /api/models/agents/<backend>/sources` is `[contract-gap]` G-9.1.",
        "a 409 branch is claimed for",
        within="1.4",
    ),
    # Reviewer's finding, round 2 of #1276: an unresolved marker only dropped out
    # of the list of active exemptions. A row that needed no other exemption
    # therefore spent a number nobody registered and nothing said so — the marker
    # is a citation of §0.5, and a citation that resolves to nothing is reported
    # whether or not ignoring it happens to redden some other arm.
    GateCase(
        "E", "gaps", "empty",
        "a marker spends a gap number no row registers",
        "An install confirm was accepted `[contract-gap]` G-10",
        "An install confirm was accepted `[contract-gap]` G-99",
        "`[contract-gap] G-99` names no §0.5 row",
        within="0.8",
    ),
    # Reviewer's finding, round 2 of #1276: the guarded envelope was every literal
    # written outside the route table, unioned, so a key from one section vouched
    # for a claim about another route entirely. `recovered` is real — it is in the
    # OAuth completion example — and the chain route never returns it.
    GateCase(
        "E", "routes", "arm",
        "a response claim borrows a key from another section's example",
        "on success it returns `{chain, removed_hops, interrupted}`",
        "on success it returns `{chain, removed_hops, interrupted, recovered}`",
        "names recovered — not contracted for",
        within="0.5",
    ),
    # Reviewer's finding, round 2 of #1276: an attributed field was reduced to its
    # last segment before it was resolved, so a real leaf under the wrong parent
    # answered for it. `health` exists in that schema; `manifest` does not, and
    # the sentence naming the wrong object read as verified.
    GateCase(
        "E", "schema fields", "arm",
        "an attributed field path hangs under a parent the schema lacks",
        "v5's `status.health` runs",
        "v5's `manifest.health` runs",
        "declares no `manifest.health`",
        within="0.5",
    ),
    # A total rendering of a contracted vocabulary that quietly drops a row. The
    # author cannot see the schema while writing the table, so nothing but a set
    # comparison catches this.
    GateCase(
        "E", "schema fields", "arm",
        "mapping table drops a contracted value",
        "| `waiting` | 网关 · 暂时全部在冷却 |",
        "| `wating` | 网关 · 暂时全部在冷却 |",
        "renders",
        within="1.0",
    ),
    GateCase(
        "E", "schema fields", "arm",
        "mapping table field header is malformed",
        "| `Source.state.status` `[contract]` | Ink | Key |",
        "| `Source.state-status` `[contract]` | Ink | Key |",
        "mapping header `Source.state-status` is not a valid field citation",
        within="1.6",
    ),
    GateCase(
        "E", "schema fields", "arm",
        "mapping table row has an unowned cell",
        "| `standby` | `$--muted` | `upstream.state.standby` |",
        "| `standby` | `$--muted` | `upstream.state.standby` | extra |",
        "mapping row has 4 cells where its header declares 3",
        within="1.6",
    ),
    # Reviewer's finding, round 1 of #1276: a gap registration says one field is
    # absent, and the excusal read that as "this whole row is unverifiable" —
    # every attributed-field claim inside it, including the *evidence*. G-3's
    # evidence cell cites `source.schema.json`'s `models`, which the file really
    # does declare and which the row does not claim is missing. So the one place
    # a gap row's own reasoning is written down was the one place nothing checked
    # it, and a mistyped citation there is the mistake a reader has no way to
    # catch: the row is where they go to find out whether the gap is real.
    GateCase(
        "E", "schema fields", "arm",
        "a gap row's evidence citation, mistyped",
        "`source.schema.json`'s `models` carries no per-model retained flag",
        "`source.schema.json`'s `models_typo` carries no per-model retained flag",
        "declares no `models_typo`",
        within="0.5",
    ),
    # The right route and the wrong body: `{hops}` belongs to the per-model
    # chain save, and sending it here was a real finding.
    GateCase(
        "E", "routes", "arm",
        "body key belongs to another route",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/sources` with `{hops: string[]}` `[contract]`",
        "not contracted for",
        within="0.8",
    ),
    GateCase(
        "E", "routes", "arm",
        "empty body omits a required member",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/sources` with `{}` `[contract]`",
        "`{}` omits order — required for",
        within="0.8",
    ),
    GateCase(
        "E", "routes", "arm",
        "body member declares the wrong type",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/sources` with `{order: integer[]}` `[contract]`",
        "declares `order` as integer[]",
        within="0.8",
    ),
    GateCase(
        "E", "routes", "arm",
        "required body member is described as optional",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}` `[contract]`",
        "`PUT /api/models/agents/<backend>/sources` with `{order?: string[]}` `[contract]`",
        "marks order optional — required for",
        within="0.8",
    ),
    GateCase(
        "E", None, "arm",
        "api authority line is outside the file",
        "`api.md:212`",
        "`api.md:9999`",
        "is outside `docs/plans/model-hub-contracts/api.md`",
        within="0.5",
    ),
    GateCase(
        "E", None, "arm",
        "revision authority line is outside the cited file",
        "`ceace07f:2197`",
        "`ceace07f:9999`",
        "is outside `core/handlers/model_hub/service.py`",
        within="0.5",
    ),
    GateCase(
        "E", None, "arm",
        "revision line no longer points at the cited symbol",
        "`ceace07f:2197`",
        "`ceace07f:2198`",
        "does not point at `core/handlers/model_hub/service.py:set_agent_mode`",
        within="0.5",
    ),
    # `api.md` puts request and response in one cell, and a check that unions
    # the two sides accepts the answer's vocabulary as a legal request body.
    # Dropping the word that introduces this body as an answer must move it to
    # the request side and fail there.
    GateCase(
        "E", "routes", "arm",
        "a response body written as the request",
        "and returns `{agent: AgentSupply}`",
        "and sends `{agent: AgentSupply}`",
        "not contracted for",
        within="1.3",
    ),
    # --- the four defects that spent the exit clause -----------------------
    #
    # Round 18's findings were all in the checker rather than in the spec, which
    # is what the clause is for: the gate left the PR it was written to guard,
    # and its cases came with it. Each mutation below was run against the
    # *pre-fix* checker before it was written down — that checker reports none
    # of them and stays green on the unmutated spec — because a case that fails
    # either way proves the mutation, not the fix.
    #
    # Three of the four are one sentence: a gap marker is a statement about one
    # named hole, and the checker read it as amnesty for whatever paragraph it
    # landed in. §0.5 already said so about its own withdrawn row — 「the row
    # names no route and quotes no body, so there is nothing left in it for a
    # checker to excuse」 — and the checker was not reading it that way.
    GateCase(
        "E", "gaps", "arm",
        "a claim is silenced by a row §0.5 has withdrawn",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA refusal body "
        "`{ok, reason_key}` is `[contract-gap]` G-9.",
        "an unbound body claim cannot be checked",
        within="1.4",
    ),
    # A withdrawn number still resolves — the row is kept, struck through, so
    # the register says what happened to it — so the `empty` case above cannot
    # reach this and the number has to be spent on a claim to show it.
    GateCase(
        "A", "gaps", "arm",
        "a route is excused by a marker whose row is about another route",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nRetiring an entry issues "
        "`PATCH /api/models/sources/<source_id>/retire` `[contract-gap]` G-15.",
        "is named by no §0.8 row",
        within="1.4",
    ),
    # G-15 is live and names `PATCH /api/models/sources/<id>`. Position cannot
    # decide this — §1.4's G-17 sits two characters from a route it is not
    # about — so the row's own text is the authority, on both arms that honour
    # the marker.
    GateCase(
        "E", "gaps", "arm",
        "a 409 claim is excused by a marker whose row is about another route",
        "the surface of truth when it does not.",
        "the surface of truth when it does not.\n\nA 409 conflict answer to "
        "`PUT /api/models/agents/<backend>/sources` is `[contract-gap]` G-15.",
        "a 409 branch is claimed for",
        within="1.4",
    ),
    # The shared envelopes are answers. Unioned into a request allowance they
    # widened it by every key any response anywhere carries, so a metadata edit
    # could post `source` — a field that route only ever returns.
    GateCase(
        "E", "routes", "arm",
        "a request body borrows a response-only field",
        "`{display_name?, base_url?}`",
        "`{display_name?, base_url?, source?}`",
        "not contracted for",
        within="0.5",
    ),
    # Two of `api.md`'s rows spell no answer in the cell: they name it — "OAuth
    # result" — and a section below spells it out, as three readings selected by
    # `OAuthFlow.intent`. Left unread, those rows contracted no answer at all,
    # and every claim about the terminal shape failed alike, the true ones
    # included; the document wrote the shape as prose because a body could not
    # be written. Read as one flat vocabulary it would accept exactly what the
    # section forbids, which is this case: the reauth fields on the create
    # terminal. `GATE_INNOCENT` holds the other half — the true body, in the
    # same sentence, staying green.
    GateCase(
        "E", "routes", "arm",
        "the create terminal is claimed to answer with the reauth reading",
        "the `create` terminal answers with `flow`, `source`, `added_to` and `adopted_by`",
        "the `create` terminal answers with `{flow, source, recovered, interrupted_pairs}`",
        "not contracted for",
        within="0.8",
    ),
    # A schema citation used to confirm the file and never the field, so a
    # misspelling sat behind a citation that looked verified. Two cues state
    # ownership — the possessive and the preposition — and a case on one proves
    # nothing about the other, which is the rule this suite already holds its
    # `token` cells to.
    GateCase(
        "E", "schema files", "arm",
        "a possessive citation names a field the schema does not declare",
        "`source.schema.json`'s `models` describes",
        "`source.schema.json`'s `model_list` describes",
        "declares no `model_list`",
        within="0.7",
    ),
    GateCase(
        "E", "schema files", "arm",
        "a prepositional citation names a field the schema does not declare",
        "`masked_credential` are each",
        "`masked_key` are each",
        "declares no `masked_key`",
        within="1.1",
    ),
    # Reviewer's finding, round 1 of #1276: the counted-vocabulary arm knew the
    # spellings `two`…`fifteen` and nothing else, so a claim written in digits
    # or in a larger word was not skipped — it was never seen, and the arm
    # reported a clean zero over a document that held three of them. An
    # extractor narrower than the document reads exactly like agreement, which
    # is the one thing a target-zero class cannot afford. Both spellings get a
    # case here and both get a `SELF_TEST` fixture, because this is the arm
    # whose verdict *is* its zero.
    GateCase(
        "E", "schema files", "arm",
        "a counted claim written in digits",
        "`agent-supply.schema.json`'s `model_supply` rows require exactly",
        "`agent-supply.schema.json` has 16 properties, and its `model_supply` rows "
        "require exactly",
        "has 13 properties, not 16",
        within="0.5",
    ),
    GateCase(
        "E", "schema files", "arm",
        "a counted claim spelled past the vocabulary the arm used to hold",
        "`agent-supply.schema.json`'s `model_supply` rows require exactly",
        "`agent-supply.schema.json` has sixteen properties, and its `model_supply` rows "
        "require exactly",
        "has 13 properties, not 16",
        within="0.5",
    ),
    # §0.9 declares which keys interpolate each slot; §1.0 enumerates the same
    # set four hundred lines away, in prose. Two enumerations of one set, and
    # the far one is what nobody re-reads when the table changes — so it has to
    # be wrong in both directions before either is checked. This is the
    # leaves-out direction: a key gains the slot, the table gets the row, the
    # sentence does not.
    GateCase(
        "B", "slots", "arm",
        "a prose enumeration of a slot's consumers drops a live one",
        "`gateway.modelCount`, `gateway.collapse`, `addKey.pull.result`, `guard.count`,",
        "`gateway.modelCount`, `gateway.collapse`, `addKey.pull.result`,",
        "and leaves out `guard.count`",
        within="1.0",
    ),
    # The other direction, and it needs its own case because the arm answers it
    # with a different comparison. A dropped key is caught against §0.9; a key
    # that was *retired* cannot be, because retiring it took its whole
    # namespace with it and left nothing for a namespace check to miss. Only
    # the enumeration still claims it exists. This case reinstates the exact
    # corpse round 20 found — `chain.derived.hops`, named by a sentence whose
    # subject is "the keys that do X", with no copy row anywhere behind it.
    GateCase(
        "B", "slots", "arm",
        "a prose enumeration of a slot's consumers names a retired key",
        "`sourceDetail.refetch.removed` and `takeover.pill` — nine",
        "`sourceDetail.refetch.removed`, `chain.derived.hops` and `takeover.pill` — nine",
        "enumerates `chain.derived.hops` among the keys",
        within="1.0",
    ),
    # A frame's element inventory quotes the line an implementer is to draw,
    # which is a copy row's string written a second time in the section that
    # instructs. `{{health}}` split off from `{{status}}` and the inventory kept
    # the word the split took away, so the instruction rendered an HTTP status
    # where a supply-health word belongs. Substitution is the only way to be
    # wrong here — deletion is legal, and the green half below is what says so.
    GateCase(
        "B", "copy", "arm",
        "a frame inventory quotes a line with one slot substituted for another",
        "and one `{{mode}} · {{health}}` line",
        "and one `{{mode}} · {{status}}` line",
        "and no copy row renders it",
        within="1.1",
    ),
    # The same generator one grain further in: a frame's mapping table says what
    # each value of a field is drawn as, and a §0.8 row keyed on one of those
    # values says it again in its copy column. The table gains a qualifier, the
    # row keeps the old key, and the product ships two renderings for one value —
    # which is exactly how `not_installed` came to mean both Not installed and
    # Unsupported host with one key between them.
    #
    # Three refusals keep this arm from over-firing, and the shipped document
    # holds all three down, each of them biting: loosen one and the green-spec
    # test goes red before any case here does.
    #   - A value two of the frame's tables own is not resolved at all. §1.0's
    #     Ready enters on `ok`, which is a value of `RuntimeDependency.status.
    #     health` *and* of `AgentSupply.supply_status`, drawn `shell.running` by
    #     one and `gateway.group.status.ok` by the other; Impaired is the same
    #     story for `degraded`. Resolve last-wins and both rows report.
    #   - A row citing more than one key draws a composite and asserts no single
    #     pairing. §1.0's Not installed cites three, of which two are the confirm
    #     dialog's. Ask that every cited key be in the table's set and it reports.
    #   - A value the table draws two ways is satisfied by either. §1.0's
    #     Unsupported host cites the second of `not_installed`'s two renderings.
    #     Tighten membership to equality and it reports.
    GateCase(
        "B", None, "arm",
        "a register row keeps a key its frame's mapping table has moved off",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.starting` | Run pill → Starting |",
        "enters on `not_started` and renders `shell.starting`, but §1.0's own mapping of",
        within="0.8",
    ),
    # Arm N reads the exit cell against the frame's own value table, and the
    # defect arrives from either side: the dispatch narrows, or a state starts
    # claiming a reading the dispatch was never told about. Both cases below are
    # a single cell, and both leave every other cell locally true — which is the
    # whole reason the shape survived two review rounds on the shipped file.
    GateCase(
        "C", None, "arm",
        "the dispatching row drops one of its frame's drawn readings",
        "`not_started` → Not started, `degraded` → Impaired",
        "`degraded` → Impaired",
        "reading `not_started` as 「Not started」",
        within="0.8",
    ),
    GateCase(
        "C", None, "arm",
        "a state claims a drawn reading the dispatching row never lands in",
        "| An install confirm was accepted `[contract-gap]` G-10 |",
        "| An install confirm was accepted, and supply reads `waiting` "
        "`[contract-gap]` G-10 |",
        "reading `waiting` as 「Installing」",
        within="0.8",
    ),
    # And the third side, which membership could not see: the dispatch names
    # every reading and sends one of them to the wrong state. The head this arm
    # was written for shipped 「`standby` or `active` → Ready」 over a frame that
    # drew `standby` as Not supplying; the value was spoken, the landing counted
    # as reached, and the contradiction stood. Both cases below are a single
    # arrow, one on each router this document has.
    GateCase(
        "C", None, "arm",
        "the dispatching row sends a reading to a state its own frame keys elsewhere",
        "`not_started` → Not started",
        "`not_started` → Ready",
        "sends `not_started` to 「Ready」, and §1.0 keys 「Not started」",
        within="0.8",
    ),
    GateCase(
        "C", None, "arm",
        "a routing exit swaps two of its field's landings",
        "`cooldown` → Cooling",
        "`cooldown` → Needs action",
        "sends `cooldown` to 「Needs action」, and §1.6 keys 「Cooling」",
        within="0.8",
    ),
    # --- the fourth rule: a row this reader cannot read ----------------------
    # Every case below deletes a cell, and every one of them used to pass. Six
    # review rounds found them one reader at a time — 「the register drops a
    # row」, 「the copy table drops a row」, 「the slot row drops its consumers」
    # — which is the shape of a defect that is not six defects: a reader met
    # input it could not parse and declined to say so, and whatever the reader
    # was supposed to declare simply never entered the comparison. The rule is
    # here so the tiling test asks every reader the same question in advance.
    GateCase(
        "A", "states", "malformed",
        "a register row loses a cell",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` |",
        "has 5 cells where the register's header declares 6",
        within="0.8",
    ),
    GateCase(
        "A", "treatments", "malformed",
        "a §0.8 treatment row loses its meaning cell",
        "| F5 | No request | The state issues nothing",
        "| F5 | The state issues nothing",
        "has 2 cells where the table declares 3",
        within="0.8",
    ),
    GateCase(
        "B", "copy", "malformed",
        "a copy row loses its English cell",
        "| `pill_one` | {{count}} 处接管中 | {{count}} takeover active |",
        "| `pill_one` | {{count}} 处接管中 |",
        "has 2 cells where a copy table declares 3",
        within="1.7",
    ),
    GateCase(
        "B", "copy", "malformed",
        "a copy row key uses a malformed spelling",
        "| `shell.title` | 模型 | Models |",
        "| `shell-title` | 模型 | Models |",
        "copy row `shell-title` has malformed key `shell-title`",
        within="1.0",
    ),
    GateCase(
        "B", "slots", "malformed",
        "a §0.9 slot row loses its consumer cell",
        "| `{{vendor}}` | The upstream vendor's product name, as the user chose it. "
        "| Always present |",
        "| `{{vendor}}` | The upstream vendor's product name, as the user chose it. |",
        "has 3 cells where the table declares 4",
        within="0.9",
    ),
    GateCase(
        "E", "gaps", "malformed",
        "a §0.5 gap row loses its 「what is missing」 cell",
        "| G-11 | 09 direct-only home, zero backends | an installation flag per agent "
        "backend, and the payload that carries it |",
        "| G-11 | 09 direct-only home, zero backends |",
        "has 3 cells where the registry declares 4",
        within="0.5",
    ),
    # --- the arms the same round hardened ------------------------------------
    # Each of these is a reader that used to drop its input quietly rather than
    # hand it to the comparator: an extraction pattern narrow enough to be an
    # admissibility test, and a name it declined to match was a name nobody
    # checked. The rule is `arm` rather than `token` because what regressed is
    # the extraction, not the comparison — the comparator was never asked.
    GateCase(
        "E", "gaps", "arm",
        "a gap marker whose number is misspelt with a letter",
        "An install confirm was accepted `[contract-gap]` G-10",
        "An install confirm was accepted `[contract-gap]` G-1O",
        "`[contract-gap] G-1O` names no §0.5 row",
        within="0.8",
    ),
    GateCase(
        "E", "gaps", "arm",
        "a gap marker uses an underscore in place of its separator",
        "An install confirm was accepted `[contract-gap]` G-10",
        "An install confirm was accepted `[contract-gap]` G_10",
        "`[contract-gap] G_10` names no §0.5 row",
        within="0.8",
    ),
    GateCase(
        "E", "repo symbols", "arm",
        "a symbol cited by its qualified name, misspelt",
        "core/handlers/model_hub/service.py:set_agent_mode",
        "core/handlers/model_hub/service.py:ModelHubService.set_agent_moed",
        "defines no `ModelHubService.set_agent_moed`",
        within="0.5",
    ),
    GateCase(
        "E", "repo symbols", "arm",
        "a symbol citation contains a hyphen",
        "core/handlers/model_hub/service.py:list_agents",
        "core/handlers/model_hub/service.py:list-agents",
        "defines no `list-agents`",
        within="0.5",
    ),
    GateCase(
        "E", "repo symbols", "arm",
        "a symbol citation starts with a digit",
        "core/handlers/model_hub/service.py:list_agents",
        "core/handlers/model_hub/service.py:1list_agents",
        "defines no `1list_agents`",
        within="0.5",
    ),
    GateCase(
        "B", "copy", "arm",
        "a frame cites a key of its own table, misspelt, without the namespace",
        "`legend.unavailable` **lost a 暂 to make that true.**",
        "`legend.unavailabl` **lost a 暂 to make that true.**",
        "key `legend.unavailabl` is cited and never defined",
        within="1.7",
    ),
    GateCase(
        "E", "routes", "arm",
        "a body member spelt with a hyphen",
        "and answers `{source, added_to, adopted_by}`",
        "and answers `{source, added-to, adopted_by}`",
        "names added-to — not contracted for POST /api/models/sources",
        within="1.5",
    ),
    GateCase(
        "E", "routes", "arm",
        "a body that omits a member its route requires",
        "`POST /api/models/oauth/submit` as `{flow_id, value}`",
        "`POST /api/models/oauth/submit` as `{flow_id}`",
        "omits value — required for POST /api/models/oauth/submit",
        within="1.4",
    ),
    # The exit column, resolved. Every other cell of a register row has been
    # compared against something for twenty rounds; the cell that says where the
    # state *goes* was read as prose. Both cases are the same sentence with one
    # letter moved, and the pair is the point: a name that overshoots and a name
    # that stops short fail differently under any matcher that is not exact.
    GateCase(
        "C", "states", "empty",
        "a named success exit that lands on no state its frame files",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Startingg |",
        "exits to 「Startingg」, which opens with no state §1.0 files",
        within="0.8",
    ),
    GateCase(
        "C", "states", "token",
        "a success exit truncated to a prefix of the state it means",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Start |",
        "exits to 「Start」, which opens with no state §1.0 files",
        within="0.8",
    ),
)


# A cell with no case, and the reason it has none. An exemption is written here
# or the tiling test fails; it is never an omission nobody had to justify.
UNREACHABLE_BY_CLASS: dict[tuple[str, str], str] = {
    ("D", "duplicate"): (
        "class D defines nothing. It reads the copy universe class B fills, and a "
        "copy key declared twice is reported once, by B — the (B, copy, duplicate) case."
    ),
    ("D", "malformed"): (
        "same reason, one rule over: D reads no file and parses no row. A copy row it "
        "cannot read is B's to report — the (B, copy, malformed) case — and D asks its "
        "question about whatever B managed to declare."
    ),
}

UNREACHABLE_BY_UNIVERSE: dict[tuple[str, str], str] = {
    (name, "duplicate"): (
        f"`{name}` is built from the frozen contract files, which this harness must not "
        "edit. Its duplicate rule is proved directly instead, by "
        "test_authority_side_universes_report_a_duplicate_definition."
    )
    for name in ("routes", "schema files", "schema fields", "repo symbols")
} | {
    (name, "malformed"): (
        f"`{name}` is built from the frozen contract files, which this harness must not "
        "edit, so the mutation this rule needs — a contract row or file the reader "
        "cannot parse — cannot be written as a spec mutation. Proved directly instead, "
        "by test_authority_side_universes_report_input_they_cannot_read."
    )
    for name in ("routes", "schema files", "repo symbols")
} | {
    ("schema fields", "malformed"): (
        "a schema file that stops parsing is one defect and is reported once, by the "
        "universe whose family the file is — `schema files`. `schema fields` is filled "
        "from files that parsed; there is no separate row of it to break. Its owner's "
        "case covers both."
    ),
    ("frames", "malformed"): (
        "a frame is declared by a §1 heading, not by a table row. A heading either "
        "matches `#{2,3} <number> ` and declares a frame or does not and declares "
        "nothing — there is no half-read heading for this rule to be about. What a "
        "heading can get wrong is its number, which is the (C, frames, empty) and "
        "(C, frames, token) cases."
    ),
}

# The finest grain: one class's use of one universe. Two projections can both be
# full while a cell is empty — every class had a `token` case and every universe
# had one, and `A`'s reading of the gap registry still had none, which is where
# three of round 14's findings lived. So the grid is tiled per arm, and an arm
# that cannot reach a rule says why here.
UNREACHABLE_BY_ARM: dict[tuple[str, str, str], str] = {
    ("A", "routes", "empty"): (
        "class A never resolves a citation against `routes`. It reads the universe as "
        "an inventory — which contracted mutations does nothing reach — and asks its "
        "questions as set arithmetic over tokens `normalize_route` has already "
        "canonicalised. A citation that resolves to nothing is E's verdict, and E has "
        "the case. What A can get wrong is the canonicalisation, which is the "
        "(A, routes, token) case."
    ),
    ("A", "gaps", "duplicate"): (
        "one canonical token declared twice is reported once, by the universe's owner. "
        "The registry's owner is E — a gap row is a claim about what the contract does "
        "not have — so the case is (E, gaps, duplicate), reached through the same "
        "registry object A reads."
    ),
    ("A", "frames", "token"): (
        "A cites a frame only by its section number, written out in full — `§1.0` — "
        "and the universe answers that spelling exactly or through a declared alias. "
        "There is no prefix to truncate and no near-spelling that lands on a "
        "different frame: a wrong number resolves to nothing, which is the "
        "(A, frames, empty) case. The alias half is what C's (C, frames, token) "
        "case reads, over the same universe object."
    ),
    ("A", "frames", "duplicate"): (
        "two §1 headings under one number is one document defect, reported once, by "
        "the universe's owner — C, in the (C, frames, duplicate) case. A resolves "
        "against the same object and would report the second copy of a finding the "
        "reader has already been given."
    ),
    ("C", "states", "duplicate"): (
        "one canonical token declared twice is reported once, by the universe's owner. "
        "The register's owner is A — a state is declared by a §0.8 row — so the case is "
        "(A, states, duplicate), reached through the same universe object C resolves "
        "its exits against."
    ),
    ("C", "states", "malformed"): (
        "same object, same owner: a §0.8 row C cannot read is reported by A, in the "
        "(A, states, malformed) case. What C does with such a row is resolve exits "
        "*against* it — the row's identity survives its broken cells precisely so this "
        "arm keeps working — and reporting here would hand the reader the same broken "
        "row a second time."
    ),
    ("A", "gaps", "malformed"): (
        "the registry is read once, by `registered_gaps`, and a row it cannot read is "
        "reported by the universe's owner — E, in the (E, gaps, malformed) case. A "
        "resolves against the same object; reporting there too would give the reader "
        "the same broken row twice."
    ),
}


SECTION_HEAD_RE = re.compile(r"^#{2,3} (\d+(?:\.\d+)?) ", re.M)


def _region(spec: str, section: str, label: str) -> tuple[int, int]:
    """Where `section` runs — its heading to the next one, subsections included.

    An anchor is a quotation, and a quotation is only ever unique somewhere. Read
    document-wide it is hostage to every other section: round 26 broke four cases
    at once by giving a second frame the same exit sentence, and the correct
    reading of that failure — the cases are right, the spec is right, the anchors
    were written too wide — took a transcript to recover. So each case names the
    section it is quoting from, and the anchor has to be unique only there.

    The pattern insists on a heading that begins with a number, because §1.1's
    body carries a fenced pseudocode block whose comments start with `# 0.` — a
    terminator that stopped at those would cut the section in half.
    """
    assert section, f"{label}: every case must name the section its anchor is read inside"
    head = re.search(rf"^#{{2,3}} {re.escape(section)} ", spec, re.M)
    assert head, f"{label}: the spec has no section {section}"
    nxt = SECTION_HEAD_RE.search(spec, head.end())
    return head.start(), nxt.start() if nxt else len(spec)


def _mutate(spec: str, case) -> str:
    """`case.before` → `case.after`, once, inside the section the case names."""
    start, end = _region(spec, case.within, case.label)
    region = spec[start:end]
    assert region.count(case.before) == 1, (
        f"{case.label}: anchor is not unique in §{case.within} — it matches "
        f"{region.count(case.before)} times. Re-anchor on what identifies the "
        "target (the row's own cell, the sentence's own subject) or move the case "
        "to the section it now belongs in; do not widen the quotation to make it "
        "unique again, and do not scope it by hand at the call site."
    )
    return spec[:start] + region.replace(case.before, case.after, 1) + spec[end:]


def test_an_anchor_is_read_inside_its_own_section_only():
    """Round 26, in miniature: a twin elsewhere is not this case's business.

    Both halves matter. Without the first, editing one frame can redden cases
    written against another, and the suite starts teaching that anchors should be
    long rather than specific. Without the second, a case could silently mutate a
    neighbouring row it was never about.
    """
    case = GateCase("A", "routes", "token", "probe", "`POST /x`", "`POST /y`", "", "0.8")

    twinned = "### 0.8 Register\n\n`POST /x` here\n\n### 1.6 Frame 06\n\n`POST /x` there\n"
    assert _mutate(twinned, case) == twinned.replace("`POST /x`", "`POST /y`", 1)

    doubled = "### 0.8 Register\n\n`POST /x` here\n\n`POST /x` again\n"
    with pytest.raises(AssertionError, match=r"not unique in §0\.8"):
        _mutate(doubled, case)


@pytest.mark.parametrize("case", GATE_MUTATIONS, ids=lambda c: f"{c.cls}/{c.rule}/{c.label}")
def test_model_hub_ui_state_gate_fails_on_a_reintroduced_defect(tmp_path, case: GateCase):
    """A gate nobody has watched fail is a gate that reports green.

    Each case reintroduces one defect into the live spec and asserts the checker
    names it — the right class *and* the right sentence. Class alone is not
    enough: a mutation that trips some other arm of the same class would pass
    while the rule it was written for stayed dead.
    """
    spec = Path("docs/plans/model-hub-ui-spec.md").read_text(encoding="utf-8")

    mutated = tmp_path / "mutated.md"
    mutated.write_text(_mutate(spec, case), encoding="utf-8")
    # A document written to a temporary directory is in no checkout, so it has no
    # authorities of its own. Every case here mutates the spec side, and the
    # authorities it is checked against are this repository's — said out loud
    # because a borrowed authority is the one thing a green result may not hide.
    result = check_model_hub_ui_states(mutated, authorities=ROOT)
    assert result["authority_origin"] == "this checkout"

    assert not result["ok"], f"{case.label}: the gate did not notice"
    said = [f["message"] for f in result["findings"] if f["class"] == case.cls]
    assert any(case.says in m for m in said), (case.label, case.says, result["findings"])


# An arm that reads a *shape* rather than a token has a second way to be wrong,
# and the mutation suite above cannot see it: firing on text that is fine. Every
# case there asserts red, so an arm that reported everything would pass all of
# them. Only the arms whose rule draws a line inside otherwise-legal prose need
# this — the rest compare tokens against a register and have nothing to overfire
# on.
class InnocentCase(NamedTuple):
    """Legal text inserted into the spec; the gate must stay green.

    `before`/`after` place the text, `within` the section they are read inside,
    and `pins` names the rule it holds down.
    """

    label: str
    pins: str
    before: str
    after: str
    within: str


GATE_INNOCENT: tuple[InnocentCase, ...] = (
    # Reviewer's finding, round 1 of #1276, written as the reviewer wrote it: one
    # paragraph, two routes, and a `409` that belongs to the first. Held against
    # every route in the scope, this reports the unguarded runtime start as
    # claiming a guard it does not have — a true sentence turned into a finding,
    # and a reader sent to argue with `api.md` about a route nobody made a claim
    # about. The binder measures to the nearest mention on either side, so the
    # prose has to keep the guarded route the nearest thing to the number, which
    # is also how the sentence reads out loud.
    InnocentCase(
        "a 409 written next to its own route, in a paragraph that names another",
        "class E binds a guarded-status claim to its nearest route, not to its container",
        "which is why the second caller costs no new frame.",
        "which is why the second caller costs no new frame. The same envelope read "
        "from the other end: `POST /api/models/sources/<source_id>/refresh` may come "
        "back `409`, which is why that surface confirms before it resends; the press "
        "a user makes once one succeeds is `POST /api/models/runtime/start`, and that "
        "route is contracted with no guard of its own.",
        within="0.5",
    ),
    # The sentence the widened count vocabulary reported first, restored as the
    # green case it always was. G-20's row names two schema files and counts one
    # of them, and the count is exactly right — the arm held it against both and
    # reported the other, which is a true sentence turned into a finding and a
    # reader sent to the wrong file. Both halves are pinned here at once: the
    # count binds to the file it is written next to, and the property names the
    # row goes on to cite are citations rather than a botched enumeration of the
    # thirteen. Widen either reading back and this goes red while every red case
    # above stays red.
    InnocentCase(
        "a counted claim in a row that names a second schema too",
        "class E binds a count to its own subject and reads a cited property as a citation",
        "`agent-supply.schema.json` declares no `adopted_by`",
        "`agent-supply.schema.json`'s 13 properties do not include `adopted_by`",
        within="0.5",
    ),
    InnocentCase(
        "two members of a vocabulary named as examples",
        "class B's restated-vocabulary arm reads an enumeration, not a mention",
        "*Why:* a wire describes a *relation between two things*;",
        "Two of those readings, 正常, 降级, are the ones a user sees most.\n"
        "*Why:* a wire describes a *relation between two things*;",
        within="1.9",
    ),
    # The slot-consumer arm has a threshold of its own: three keys joined by
    # nothing but list punctuation. Below it a paragraph is discussing keys, not
    # enumerating them — and this document discusses keys constantly. Five
    # paragraphs of the shipped file name one slot and three or more keys
    # without listing any of them; read mentions instead of runs and those five
    # return twenty findings, none of them a restatement of anything. All three
    # red cases above stay red through that change, which is why only a green
    # case can hold the line.
    InnocentCase(
        "three keys and one slot, discussed rather than listed",
        "class B's slot-consumer arm reads a list run, not a paragraph's mentions",
        "**Slot-bearing keys** `[derived]`.",
        "A count is not the only promise a key makes about a number. "
        "`sourceDetail.summary`\nreports the size of one table, so its zero case is a "
        "state rather than a number.\n`gateway.modelCount` reports that size one level "
        "up and never reaches zero, because a\ngroup with nothing in it is drawn by "
        "`gateway.group.emptyModels` instead. Both\ninterpolate `{{count}}`, and neither "
        "sentence is enumerating anything.\n\n"
        "**Slot-bearing keys** `[derived]`.",
        within="1.0",
    ),
    # The quoted-shape arm has to let a *shorter* line through, because this
    # document has a named absence rule: a slot with nothing to fill it drops
    # its segment, so a legal quote of a row's string can be missing pieces of
    # it. What is never legal is a slot swapped for a different slot. The line
    # separating the two is subsequence, and only a green case can pin it —
    # tighten the arm to equality and the red case above is still red.
    InnocentCase(
        "a quoted line the absence rule has shortened",
        "class B's quoted-shape arm accepts a subsequence and rejects a substitution",
        "**`undetermined.hint` used to contradict AC-27, and now states it.**",
        "The same absence rule reaches the other consumers of `{{status}}`, and the "
        "shortest\nthing it can leave is still that row's own string. `adopt.fail.detail` "
        "carries no\nprotocol segment at all, so a transport failure there renders "
        "`{{request}} · {{reason}}`\n— two segments of three, with nothing substituted "
        "for the one that went missing.\n\n"
        "**`undetermined.hint` used to contradict AC-27, and now states it.**",
        within="1.5",
    ),
    # The cross-frame dispersal arm compares two sets of *states*, and states are
    # written here the way prose wants them: a register row is titled Unreachable
    # (engine down) and cited as Unreachable four hundred lines later, in whatever
    # order the sentence needed. So the comparison has to run on resolved states
    # rather than on the text, and this case says so in both directions at once —
    # one destination spelled by its full registered title, the set written in a
    # different order from the one it restates. Compare strings instead and it
    # reports two drifts against a restatement that is exactly right.
    #
    # The arm's other two refusals are held by construction rather than by a
    # case, and both bite. A single landing is not a set: §1.0's own Starting row
    # goes 「→ Unreachable」 and claims nothing about §1.0's other exits, and
    # dropping the arity floor makes it a second own-frame dispersal for §1.0 —
    # which trips the third refusal, because a frame with more than one own-frame
    # dispersal states no single set to compare against. §1.0 is the only frame
    # anything defers to, so that decline takes every comparison with it and
    # `cross-frame dispersals compared` goes to zero: a declared inventory
    # reading empty, which is already a failure. Neither can be pinned green.
    InnocentCase(
        "a restatement that resolves to the same set through different words",
        "class A's cross-frame dispersal arm compares resolved states, not spellings",
        "→ §1.0 Unreachable / §1.0 Sources unread / §1.0 Partial — the same three "
        "§1.0 disperses first paint into, because this is",
        "→ §1.0 Unreachable (engine down) / §1.0 Partial / §1.0 Sources unread — the "
        "same three §1.0 disperses first paint into, because this is",
        within="0.8",
    ),
    # Arm N asks that every reading a frame draws be somewhere the load can
    # land, and a dispatch may say so in either of two vocabularies: the value
    # the payload carries, or the state the frame gives that value. Requiring
    # the value token is the tempting reading — it is what the shipped exit
    # happens to use for all four health readings — and it makes a dispatch that
    # routes by description into four findings against text that reaches every
    # state it owes. Both red cases above stay red through that tightening,
    # which is why the line needs a green case.
    #
    # The arm's other two refusals sit either side of this one and are held by
    # construction rather than by a case. A frame with no unique own-frame
    # dispersal has no row that owns where a load goes, and §1.0 is the only
    # frame that has one at all — taking it away zeroes `dispatch landings
    # compared`, which is a declared inventory reading empty and already a
    # failure. And a value keyed by two rows is declined for the same reason arm
    # M declines one: `not_installed` is the entry of both Not installed and
    # Unsupported host, so the shipped file exercises that path on every run.
    InnocentCase(
        "a dispatch that names the state instead of the value it reads",
        "class C's dispatch arm accepts either vocabulary, the reading or its state",
        "`not_started` → Not started, `degraded` → Impaired",
        "a runtime that has never been started → Not started, `degraded` → Impaired",
        within="0.8",
    ),
    # The correspondence half of the same arm needs its own line, and this is
    # where it falls: a reading a frame splits by a condition lands in two
    # states, and both are right. §1.6 already sends 「an `active` nothing
    # adopts」 and 「an adopted `active`」 to different states; a second such
    # split has to stay as legal as the first, or the arm buys correspondence by
    # forbidding the one thing a dispatch legitimately does with a reading whose
    # meaning depends on more than itself.
    InnocentCase(
        "a reading the frame splits by a condition, landing in two states",
        "class C's dispatch arm declines a value its exit writes more than once",
        "`standby` or an `active` nothing adopts → Not supplying",
        "`standby` or an `active` nothing adopts → Not supplying, and once the engine "
        "resumes `standby` → Ready",
        within="0.8",
    ),
    # The green half of the OAuth-terminal case. The same sentence, written as
    # the body it is describing, has to pass — otherwise the section is read as
    # forbidding its own contents and the document is pushed back into prose,
    # where no arm can check it. Which is what the unread section did: this text
    # and the red case above were reported identically, four fields each.
    # The green half of the widened copy admission. A citation now reaches class
    # B when the copy universe answers to it as written *or* under a namespace
    # the document declares — which is what admits `fail.title` from the frame
    # that owns the table, and what reports `shel.gatewayInfo.body`. Neither
    # clause may be read as 「a dotted name in backticks is copy」: this document
    # backticks contract field paths in frame prose in exactly that shape, and
    # they belong to class E, which has the schema to judge them against.
    InnocentCase(
        "a contract field path backticked in the middle of frame prose",
        "class B admits a citation the copy universe answers to, not one that looks like copy",
        "Tooltip: `shell.gatewayInfo.body`",
        "Tooltip: `shell.gatewayInfo.body` — the pill beside it reads `status.health`",
        within="1.0",
    ),
    InnocentCase(
        "the create terminal's own fields, written as a body",
        "class E reads a named answer from the section that spells it, by reading",
        "the `create` terminal answers with `flow`, `source`, `added_to` and `adopted_by`",
        "the `create` terminal answers with `{flow, source, added_to, adopted_by}`",
        within="0.8",
    ),
    # The three green halves of class C's newly-resolved exit column. The column
    # names where a state goes, and for thirty rounds nothing resolved it — so
    # the first thing to prove about resolving it is what it must *not* claim.
    # An exit cell is written for a person: it names a state, but it also names
    # gestures, consequences and alternatives, and a rule that reads every word
    # after the arrow as a citation buys `Startingg` at the price of reporting
    # the document's own prose.
    InnocentCase(
        "an exit that continues into a lower-case consequence",
        "class C reads a state name after the arrow, not every word after the arrow",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting; dismiss → the pill is gone |",
        within="0.8",
    ),
    InnocentCase(
        "an exit offering two states as alternatives",
        "class C resolves each side of a slash, rather than the pair as one name",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting / Impaired |",
        within="0.8",
    ),
    InnocentCase(
        "an exit landing on a different state of the same frame",
        "class C resolves an exit against the register, not against one row's neighbours",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Starting |",
        "| §1.0 | Not started | `health` reads `not_started` `[contract]` | F5 | "
        "`shell.notStarted` | Run pill → Impaired |",
        within="0.8",
    ),
    # The green halves of the two widened citation readings. A qualified symbol
    # citation is now resolved rather than believed, and a bare local copy key is
    # now sent through resolution rather than admitted on sight — both of which
    # only pay if the correct spelling of each still passes. Otherwise the rule
    # has bought a misspelling by forbidding the notation the document uses.
    InnocentCase(
        "a symbol citation qualified by its own class, spelled right",
        "class E resolves a qualified name against the file, rather than trusting the dot",
        "core/handlers/model_hub/service.py:set_agent_mode",
        "core/handlers/model_hub/service.py:ModelHubService.set_agent_mode",
        within="0.5",
    ),
    InnocentCase(
        "a bare local copy key, spelled right",
        "class B resolves a frame-local key against that frame's table, and finds it",
        "`legend.unavailable` **lost a 暂 to make that true.**",
        "`legend.unavailable` **lost one 暂 to make that true.**",
        within="1.7",
    ),
)


@pytest.mark.parametrize("case", GATE_INNOCENT, ids=lambda c: c.label)
def test_gate_stays_green_on_text_that_only_looks_like_a_defect(tmp_path, case: InnocentCase):
    """The half of a shape rule that says what it does *not* claim.

    §2's colour rule names two status words inside a legend for a different
    vocabulary. That is not a restatement of the status set, and a first cut of
    the arm that read it as one fired thirty-three times across this document —
    on every paragraph that happened to mention a Cancel button. The threshold
    that separates the two is load-bearing and invisible: lower it and every
    red case above still passes.
    """
    spec = Path("docs/plans/model-hub-ui-spec.md").read_text(encoding="utf-8")

    mutated = tmp_path / "mutated.md"
    mutated.write_text(_mutate(spec, case), encoding="utf-8")
    result = check_model_hub_ui_states(mutated, authorities=ROOT)
    assert result["authority_origin"] == "this checkout"
    assert result["ok"], (case.pins, result["findings"], result["empty_inventories"])


def test_gate_mutation_suite_is_tiled_over_every_arm_and_every_rule():
    """Every (class, universe, rule) is answered — by a case, or by a stated reason.

    The gate grew one class at a time, each writing its own comparison, so each
    new class was a fresh chance to repeat the same mistake — and the tests grew
    the same way, one case per bug a reviewer happened to find. Tiling is what
    stops that: an arm added without its cases fails here, before it can ship a
    comparison nobody has watched fail.

    Tiled per *arm*, because round 14 showed two full projections hiding an empty
    cell. Every class had a `token` case and every universe had one, so both
    2-D views read full, while class A's reading of the §0.5 registry had no case
    for any rule and three findings came back out of it. The grid is the product
    now, and the exemptions are read at three grains: this class everywhere, this
    universe everywhere, or this one arm.
    """
    from scripts.check_model_hub_ui_states import (
        CLASS_UNIVERSES,
        CLASSES,
        RULES,
        UNIVERSES,
    )

    for name in sorted({u for us in CLASS_UNIVERSES.values() for u in us}):
        assert name in UNIVERSES, f"{name} is consulted by a class and declared by nothing"
    for case in GATE_MUTATIONS:
        assert case.cls in CLASSES, f"{case.label}: class {case.cls} is not a gate class"
        assert case.rule in (*RULES, "arm"), f"{case.label}: {case.rule} is not a rule"
        assert case.universe is None or case.universe in UNIVERSES, (
            f"{case.label}: {case.universe} is not a universe the gate declares"
        )
        if case.universe:
            assert case.universe in CLASS_UNIVERSES[case.cls], (
                f"{case.label}: class {case.cls} does not declare that it reads {case.universe}"
            )

    covered = {(c.cls, c.universe, c.rule) for c in GATE_MUTATIONS if c.universe}

    def why(cls: str, name: str, rule: str) -> str | None:
        return (
            UNREACHABLE_BY_ARM.get((cls, name, rule))
            or UNREACHABLE_BY_CLASS.get((cls, rule))
            or UNREACHABLE_BY_UNIVERSE.get((name, rule))
        )

    missing = [
        (cls, name, rule)
        for cls, names in sorted(CLASS_UNIVERSES.items())
        for name in names
        for rule in RULES
        if (cls, name, rule) not in covered and not why(cls, name, rule)
    ]
    assert not missing, f"arms with no case and no declared reason: {missing}"

    # An exemption that has been overtaken by a real case is a lie the next
    # reader would believe, and a reason nobody wrote is not an exemption.
    exemptions = {**UNREACHABLE_BY_ARM, **UNREACHABLE_BY_CLASS, **UNREACHABLE_BY_UNIVERSE}
    for cell, reason in exemptions.items():
        assert reason.strip(), f"{cell} is exempted with no reason"
    for cell in UNREACHABLE_BY_ARM:
        assert cell not in covered, f"{cell} is exempted and also covered"
    for cls, rule in UNREACHABLE_BY_CLASS:
        clash = [c for c in covered if c[0] == cls and c[2] == rule]
        assert not clash, f"({cls}, {rule}) is exempted for the whole class and also covered: {clash}"
    for name, rule in UNREACHABLE_BY_UNIVERSE:
        clash = [c for c in covered if c[1] == name and c[2] == rule]
        assert not clash, f"({name}, {rule}) is exempted for the whole universe and also covered: {clash}"

    # What this guard does not prove: that a case is a *good* case. It asserts the
    # cell has one and that the gate names the right class and sentence, not that
    # the mutation is the worst one available. `arm` cases are outside the grid
    # entirely — they answer no cell and are only required to keep working.


def test_every_universe_the_gate_builds_is_declared():
    """The declaration is checked against the universes, not maintained beside them.

    A list of names is a list of names: the tiling suite used to hold its own,
    written out in this file, so the §0.5 registry — built by a dict
    comprehension outside the comparator — was missing from the gate's idea of
    "every universe" and from the test's, independently. Reading the built
    objects back makes the two impossible to disagree: a universe built and not
    declared fails here, and one declared and never built fails too.
    """
    from scripts.check_model_hub_ui_states import (
        UNIVERSE_SIDES,
        Document,
        Origin,
        Universe,
        authority_claims,
        load_authorities,
        parse,
        registered_gaps,
    )

    here = Origin.tree_at(ROOT)
    doc = Document((ROOT / SPEC).read_text(encoding="utf-8"))
    auth = load_authorities(here)
    # `repo symbols` is filled while class E runs, so the run is what declares it.
    authority_claims(doc, auth, here, [], registered_gaps(doc))

    built = {u.name: u.side for u in parse(doc)["universes"].values()}
    built |= {k: v.side for k, v in auth.items() if isinstance(v, Universe)}
    assert built == UNIVERSE_SIDES


def test_authority_side_universes_report_a_duplicate_definition():
    """The rule the spec cannot exercise, proved on the comparator itself.

    `routes`, `schema files`, `schema fields` and `repo symbols` are filled from
    the frozen contract files, so no spec mutation can make one of them declare a
    token twice. Prove it can fail — two rows, one token, different content — and
    prove it passes on the real files, which is the half a fixture alone leaves
    out.
    """
    from scripts.check_model_hub_ui_states import (
        Origin,
        Universe,
        defined_symbols,
        load_authorities,
    )

    for side in ("routes", "schema files", "schema fields", "repo symbols"):
        u = Universe(side, "authority", "E")
        u.define("t", {"a": 1}, content="first", where="one.md")
        u.define("t", {"a": 2}, content="second", where="two.md")
        assert u.duplicates == [("t", "one.md", "two.md")], side
        # The same token with the same content is one row read twice, not two
        # answers — restating a route in a second table may not become a gap.
        u.define("t", {"a": 1}, content="first", where="three.md")
        assert len(u.duplicates) == 1, side

    auth = load_authorities(Origin.tree_at(ROOT))
    for side in ("routes", "schema files", "schema fields"):
        assert not auth[side].duplicates, (side, auth[side].duplicates)

    # `repo symbols` is filled inside class E's arm rather than by
    # `load_authorities`, and the exemption above said its duplicate rule was
    # proved here while the inventory had quietly removed it: every symbol was
    # registered under its bare name with the *file* as its content, so a file
    # defining `load` twice declared one token twice identically and nothing
    # could contradict. Keyed on the qualified name it can, and the real file is
    # the case that says so — unique qualified, colliding bare.
    got = defined_symbols((ROOT / "core/handlers/model_hub/service.py").read_text())
    qualified = [q for q, _bare, _line in got]
    assert len(qualified) == len(set(qualified)), sorted(
        q for q in qualified if qualified.count(q) > 1
    )
    bare = [b for _q, b, _line in got]
    assert len(bare) > len(set(bare)), (
        "this file is why the inventory is keyed on the qualified name: it defines "
        "several names twice, under different owners. If that stops being true the "
        "ambiguity case in GATE_MUTATIONS is testing a condition the repo no longer has."
    )


def test_authority_side_universes_report_input_they_cannot_read(tmp_path):
    """The fourth rule, on the side no spec mutation can reach.

    Three readers on the authority side had the door the spec side had, and two
    of them were worse than silent: an `api.md` row whose shape cell is gone
    dropped a route from the contracted-mutation inventory — so class A stopped
    asking whether anything reaches it and every spec claim about it reported as
    uncontracted — while a `.schema.json` or a cited `.py` that stopped parsing
    ended the whole run in a traceback, which is loud but is not a verdict and
    takes the other thirteen universes down with it.

    Proved on a fixture checkout rather than by mutating the frozen contract
    files, and each assertion is two-sided: the reader reports the row it cannot
    read, *and* the run survives to produce the findings it can.
    """
    from scripts.check_model_hub_ui_states import Origin, check, load_authorities

    root = _fixture_checkout(tmp_path)
    api = root / "docs/plans/model-hub-contracts/api.md"
    broken_route = "| POST `/api/models/decoy` |\n"
    api.write_text(api.read_text(encoding="utf-8") + broken_route, encoding="utf-8")

    schema = root / "docs/plans/model-hub-contracts/source.schema.json"
    schema.write_text('{"type": "object",,}', encoding="utf-8")

    auth = load_authorities(Origin.tree_at(root))
    said = [text for _where, text in auth["routes"].unreadable]
    assert any("names a route and carries no shape cell" in t for t in said), said
    said = [text for _where, text in auth["schema files"].unreadable]
    assert any("is a schema file this run cannot parse" in t for t in said), said

    # The whole run, not just the loader: a file it cannot read is a finding
    # among findings, and every other universe still answers.
    result = check(root)
    messages = [f["message"] for f in result["findings"]]
    assert any("carries no shape cell" in m for m in messages), messages[:10]
    assert any("cannot parse" in m for m in messages), messages[:10]
    assert result["input_scale"]["register rows"] > 50, "the run stopped instead of reporting"


def test_the_gate_reads_no_file_outside_the_checkout_it_was_given(tmp_path):
    """Reviewer's finding, round 3 of #1276: a citation is not a path grant.

    Every tree read goes through `Origin`, which exists so a run against an
    authority checkout cannot answer out of the working tree. A citation still
    names its own path, and `../` in one reached back out — so a document could
    quote a file the selected checkout does not contain and be told it agrees
    with it. Containment is asserted where the read happens, and the refusal is
    a report rather than an exception: what the citation names is not readable
    *here*, which is exactly the sentence a wrong checkout should produce.
    """
    from scripts.check_model_hub_ui_states import Origin

    root = _fixture_checkout(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def escaped():\n    return 1\n", encoding="utf-8")

    here = Origin.tree_at(root)
    assert here.read("core/handlers/model_hub/service.py") is not None
    for escape in ("../outside.py", "../../outside.py", str(outside)):
        assert here.read(escape) is None, escape


def test_a_citation_resolves_to_everything_answering_to_it():
    """Reviewer's finding, round 2 of #1276, proved on the comparator itself.

    One spelling may reach a universe twice — once as a token, once as an alias
    of something else — and `resolve` used to stop at the token. `service.py:load`
    is the live shape: a module-level `load` registers that citation as a token,
    and `ConfigStore.load` registers it as an alias, so the reader who followed
    it had two places to go and was told there was one. The two halves are
    unioned now, and the ambiguity is reported.

    Nothing is manufactured by the union: `define` never records a self-alias, so
    a token that also lists its own spelling as an alias still resolves to one.
    This case is written here rather than in `GATE_MUTATIONS` because it needs a
    repo file with both shapes, and that suite mutates the spec.
    """
    from scripts.check_model_hub_ui_states import Universe

    u = Universe("repo symbols", "authority", "E")
    u.define("service.py:load", {"line": 10}, content="module", where="service.py:10")
    u.define(
        "service.py:ConfigStore.load", {"line": 40},
        content="method", where="service.py:40",
        aliases=("service.py:load",),
    )
    hit = u.resolve("service.py:load")
    assert set(hit.hits) == {"service.py:load", "service.py:ConfigStore.load"}
    assert hit.ambiguous, "a citation two declarations answer to is ambiguous"

    solo = Universe("repo symbols", "authority", "E")
    solo.define("service.py:only", {"line": 3}, content="one", where="service.py:3",
                aliases=("service.py:only",))
    assert solo.resolve("service.py:only").hits == ("service.py:only",)
    assert not solo.resolve("service.py:only").ambiguous


def test_model_hub_ui_gate_target_zero_classes_prove_their_own_zero():
    """A class whose right answer is 0 cannot read 0 as evidence of anything.

    Every other inventory gets a free liveness signal: empty means the extractor
    broke. The restatement classes give that up by design — the document is
    meant to hold none of them — so they carry fixtures instead, one that must
    still be caught and one that must still pass. This asserts the gate refuses
    to report a self-tested zero when the arm behind it has stopped working.
    """
    from scripts.check_model_hub_ui_states import (
        TARGET_ZERO,
        Origin,
        load_authorities,
        self_test,
    )

    assert TARGET_ZERO, "a target-zero class list nobody populates tests nothing"
    here = Origin.tree_at(ROOT)
    assert not self_test(load_authorities(here), here)

    result = check_model_hub_ui_states(Path.cwd())
    assert not result["broken_arms"], result["broken_arms"]
    for name in TARGET_ZERO:
        assert result["input_scale"][name] == 0, (name, result["input_scale"][name])


def test_model_hub_ui_gate_reads_a_symbolic_revision():
    """The gate has to be runnable against the head under review, by name.

    Deciding path-or-revision by spelling meant `HEAD`, a branch and a tag were
    all read as paths and died on a missing-file error — which reads as *the
    document moved* when what happened is *the revision was never resolved*.
    """
    result = check_model_hub_ui_states("HEAD")
    assert result["input_mode"] == "same_run_git_rev"
    assert result["input_scale"]["register rows"] > 50
    # And the authorities came from that revision too. Reading the spec at `HEAD`
    # while resolving its citations against the working tree compares two
    # different revisions and reports the answer as one — a diff on either side
    # alone would move the verdict.
    assert result["authority_origin"] == "git rev HEAD"


# --- the loader: one origin for every input, one declared range per arm --------
#
# Two review rounds found the same defect on two different heads: an arm that
# decided for itself where to read. Once it was the spec, read from a revision
# while the contracts came from the working tree; once it was a row shaped like a
# gap registration, credited from anywhere in three thousand lines. The gate now
# resolves one `Origin` per run and hands every arm a declared slice, and the two
# rules that closed it are tested the way the five classes are — tiled over
# everything they govern, because a rule proved on one arm holds on one arm.


class ScopeTrap(NamedTuple):
    """One decoy, placed inside a declared range and then outside it.

    The pair is the whole test. A decoy the gate catches proves the arm reads its
    range; the *same* decoy elsewhere proves it reads no further — and only the
    second half can fail when an arm quietly goes back to scanning the document.

    `polarity` says which way the range cuts. A `collect` range is where a defect
    counts, so inside must report and outside must not. An `excuse` range is
    where a written exemption counts, so `setup` breaks the document first and
    inside must silence it while outside must leave it standing.
    """

    scope: str  # the key in SCOPES whose declared range is under test
    polarity: str  # collect | excuse
    label: str
    setup: tuple[str, str]  # (before, after), applied before the decoy is placed
    decoy: str  # lines placed at the declared range, then at OUTSIDE_SECTION
    says: str
    line_anchor: str = ""  # if set, `{line}` in setup/decoy resolves to the spec line carrying it
    table_anchor: str = ""  # if set, the decoy is prefixed with that row's table header


# A section no scope declares, so "outside every range" has somewhere to be. The
# tiling test asserts that, rather than trusting this comment.
OUTSIDE_SECTION = "0.7"

# §0.4's row excusing a route no frame draws. The `scope note` trap deletes it and
# puts it back in two places. What is written down here is the route the row names,
# never the sentence explaining it: the trap is aimed at the row, and a row whose
# prose is edited has not moved.
_SCOPE_NOTE_ANCHOR = "| `POST /api/models/migration/scan` |"

# Any §0.5 row, used only to find that table's header. Both excusing registers
# declare their object in a named column, so a row cut loose from its header
# declares nothing — a trap that moves a registration has to move enough of the
# table for it to still be one, or it proves the arm ignores §0.7 by handing it
# something no section would have read either.
_GAP_REGISTRY_ANCHOR = "| G-12 |"

SCOPE_TRAPS: tuple[ScopeTrap, ...] = (
    # The decoy carries its own header, the way the treatments and copy decoys
    # below do. §0.8 holds two tables, and a reader that reports rows it cannot
    # read has to be sure a row is its own — so the register is bounded by its
    # own `| Frame | State |` header, and a loose six-cell row in §0.8 is no
    # longer a register row. Handing this arm a headerless row would test the
    # bound rather than the range.
    ScopeTrap(
        "register", "collect", "a state row",
        ("", ""),
        "| Frame | State | Entry | Failure | Copy | Exit |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| §1.0 | Decoy state | 无 | F1 | `shell.title` | — |",
        "「Decoy state」 has no exit",
    ),
    ScopeTrap(
        "treatments", "collect", "a second definition of F1",
        ("", ""),
        "| # | Treatment |\n| --- | --- |\n| F1 | A decoy redefinition | with other content |",
        "is defined twice in treatments",
    ),
    ScopeTrap(
        "slots", "collect", "a second definition of {{count}}",
        ("", ""),
        "| `{{count}}` | A decoy redefinition. | Decoy |",
        "is defined twice in slots",
    ),
    ScopeTrap(
        "copy", "collect", "a second definition of shell.title",
        ("", ""),
        "| Key | 中文 | English |\n| --- | --- | --- |\n| `shell.title` | 诱饵 | Decoy |",
        "is defined twice in copy",
    ),
    ScopeTrap(
        "frame prose", "collect", "a citation of a key nothing defines",
        ("", ""),
        "A decoy sentence citing `shell.decoyMissing`.",
        "key `shell.decoyMissing` is cited and never defined",
    ),
    ScopeTrap(
        "mapping tables", "collect", "a [contract] rendering of a field no schema declares",
        ("", ""),
        "| `decoy_status` `[contract]` | Rendering |\n| --- | --- |\n| `alpha` | one |",
        "the table maps no contracted field",
    ),
    ScopeTrap(
        "gap registry", "excuse", "the registration that silences a drawn-by-nothing route",
        (
            "### 1.0 Shared shell",
            "### 1.0 Shared shell\n\nA decoy: `POST /api/models/decoy` is contracted and "
            "drawn by nothing `[contract-gap]` `G-99`.\n",
        ),
        "| G-99 | A decoy registration | `POST /api/models/decoy` | none |",
        "POST /api/models/decoy is named by no §0.8 row",
        table_anchor=_GAP_REGISTRY_ANCHOR,
    ),
    ScopeTrap(
        "scope note", "excuse", "the row putting a contracted route on another surface",
        ("{line}\n", ""),
        "{line}",
        "POST /api/models/migration/scan is contracted and reached by no §0.8 row",
        _SCOPE_NOTE_ANCHOR,
        _SCOPE_NOTE_ANCHOR,
    ),
)

# A scope with no trap, and why it cannot have one.
UNTRAPPED_SCOPES: dict[str, str] = {
    "claims": (
        "declared `*` on purpose — a restated authority is wrong wherever it is written — "
        "so there is no outside to place a decoy in. What binds this one is the declaration "
        "itself, asserted by test_every_arm_declares_a_range_the_module_declares."
    ),
    "key names": (
        "declared `*` for the same reason and with the same consequence — a set enumerated "
        "twice is wrong wherever the second copy is written, and this document writes them "
        "four hundred lines from the tables they restate, which is the whole defect. What a "
        "trap would have to prove instead is the arm's *threshold*, and that is not a range: "
        "it is held by the red and green cases in the mutation suite."
    ),
    "rendered shapes": (
        "declared `*` because a copy row's string is quoted wherever an implementer is told "
        "what to draw — a frame's element inventory, a slot's absence rule, a design note — "
        "and no section owns that. Its one in-range exclusion is the copy tables themselves, "
        "which is a line the arm draws by row shape rather than by section, so a decoy placed "
        "by section could not reach it."
    ),
}


def _spec_heading(text: str, where: str) -> str:
    """The heading line opening the section a scope declares.

    Derived from `SCOPES`, never written down here: a scope that moves takes its
    trap with it, instead of leaving one aimed at the section it used to name.
    """
    stem = re.escape(where.rstrip("."))
    pattern = rf"^### {stem}(?:\.\d+)? .*$" if where.endswith(".") else rf"^### {stem} .*$"
    found = re.search(pattern, text, re.M)
    assert found, f"no §{where} heading to place a decoy in"
    return found.group(0)


def _spec_line(text: str, anchor: str) -> str:
    """The one line carrying `anchor`, read out of the document rather than quoted.

    Written for the same reason as `_spec_heading`: a trap that pins a whole row
    goes red the next time that row's sentence is edited, while the row it is
    aimed at has not moved and the gate is not wrong. The anchor identifies the
    row; the prose around it is free to change.
    """
    found = [line for line in text.splitlines() if anchor in line]
    assert len(found) == 1, f"{anchor!r} names {len(found)} lines, not one"
    return found[0]


def _table_head(text: str, anchor: str) -> str:
    """The header and separator of the table whose row carries `anchor`.

    Read out of the document for the same reason `_spec_line` reads the row out
    of it: a column that is renamed moves the trap with it, instead of leaving
    it aimed at a header nobody writes any more.
    """
    lines = text.splitlines()
    row = _spec_line(text, anchor)
    at = lines.index(row)
    rule = max(i for i in range(at) if re.fullmatch(r"\|[\s:|-]+\|", lines[i].strip()))
    return "\n".join(lines[rule - 1 : rule + 1])


def _place(text: str, section: str, decoy: str) -> str:
    heading = _spec_heading(text, section)
    assert text.count(heading) == 1, f"§{section}'s heading is not unique"
    return text.replace(heading, f"{heading}\n{decoy}", 1)


def _checked(tmp_path: Path, text: str, name: str) -> dict:
    document = tmp_path / f"{name}.md"
    document.write_text(text, encoding="utf-8")
    return check_model_hub_ui_states(document, authorities=ROOT)


@pytest.mark.parametrize("trap", SCOPE_TRAPS, ids=lambda t: f"{t.scope}/{t.label}")
def test_gate_arm_reads_only_its_declared_range(tmp_path, trap: ScopeTrap):
    from scripts.check_model_hub_ui_states import SCOPES

    spec = (ROOT / SPEC).read_text(encoding="utf-8")
    before, after = trap.setup
    decoy = trap.decoy
    if trap.line_anchor:
        line = _spec_line(spec, trap.line_anchor)
        before, after, decoy = (part.format(line=line) for part in (before, after, decoy))
    if trap.table_anchor:
        decoy = f"{_table_head(spec, trap.table_anchor)}\n{decoy}"
    if before:
        assert spec.count(before) == 1, f"{trap.label}: setup anchor is not unique"
        spec = spec.replace(before, after, 1)
    where = SCOPES[trap.scope].where

    inside = _checked(tmp_path, _place(spec, where, decoy), "inside")
    outside = _checked(tmp_path, _place(spec, OUTSIDE_SECTION, decoy), "outside")
    said = lambda result: [f["message"] for f in result["findings"]]  # noqa: E731

    if trap.polarity == "collect":
        assert any(trap.says in m for m in said(inside)), (
            f"{trap.scope}: a decoy inside §{where} was not read", said(inside)
        )
        assert not any(trap.says in m for m in said(outside)), (
            f"{trap.scope}: the arm reached past §{where} into §{OUTSIDE_SECTION}",
            said(outside),
        )
    else:
        assert any(trap.says in m for m in said(_checked(tmp_path, spec, "setup"))), (
            f"{trap.scope}: the setup did not break the document, so the excuse "
            f"has nothing to excuse"
        )
        assert not any(trap.says in m for m in said(inside)), (
            f"{trap.scope}: an excuse written in §{where} did not count", said(inside)
        )
        assert any(trap.says in m for m in said(outside)), (
            f"{trap.scope}: an excuse written in §{OUTSIDE_SECTION} counted anyway",
            said(outside),
        )


class OriginCase(NamedTuple):
    """One input, edited in a copied checkout the gate is then pointed at.

    The spec-side mutations above cannot ask this question: they borrow this
    repository's authorities, so nothing proves the authorities *could* have come
    from anywhere else. These cases edit an authority — which the harness above
    may never do in place — and the gate has to read the edit.
    """

    arm: str  # the input kind in LOADER_ARMS["origin"]
    rel: str  # repo-relative path inside the copied checkout
    before: str
    after: str
    says: str


ORIGIN_CASES: tuple[OriginCase, ...] = (
    OriginCase(
        "spec", str(SPEC),
        "重新拉取 pressed — `POST /api/models/sources/<source_id>/refresh`, guarded",
        "重新拉取 pressed — `POST /api/models/sources/<source_id>/refreshes`, guarded",
        "POST /api/models/sources/<>/refresh is named by no §0.8 row",
    ),
    OriginCase(
        "api.md", "docs/plans/model-hub-contracts/api.md",
        "| POST `/api/models/agents/<backend>/probe` |",
        "| POST `/api/models/agents/<backend>/probed` |",
        "is contracted by no `api.md` route row",
    ),
    OriginCase(
        "schema", "docs/plans/model-hub-contracts/runtime-dependency.schema.json",
        '["ok", "degraded", "down", "not_started", "not_installed"]',
        '["ok", "down", "not_started", "not_installed"]',
        "`RuntimeDependency.status.health` renders",
    ),
    OriginCase(
        "python", "core/handlers/model_hub/service.py",
        "def list_agents", "def list_agents_renamed",
        "defines no `list_agents`",
    ),
)


def _fixture_checkout(tmp_path: Path) -> Path:
    """A copy of everything the gate reads, editable without touching the repo."""
    import shutil

    from scripts.check_model_hub_ui_states import CONTRACTS

    checkout = tmp_path / "checkout"
    (checkout / SPEC).parent.mkdir(parents=True)
    shutil.copy(ROOT / SPEC, checkout / SPEC)
    shutil.copytree(ROOT / CONTRACTS, checkout / CONTRACTS)
    for case in ORIGIN_CASES:
        source = ROOT / case.rel
        if source.suffix == ".py":
            (checkout / case.rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, checkout / case.rel)
    # The spec cites this file too, and a checkout missing it would fail for the
    # wrong reason — an absent input reads exactly like a mutated one.
    gate = "scripts/check_model_hub_ui_states.py"
    (checkout / gate).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / gate, checkout / gate)
    return checkout


def test_fixture_checkout_passes_before_anything_is_mutated(tmp_path):
    """The control the origin cases are read against.

    Without it, a case that reports the wrong finding — or fails because the
    fixture is short an input — is indistinguishable from a case that works.
    """
    checkout = _fixture_checkout(tmp_path)
    result = check_model_hub_ui_states(checkout)
    assert result["authority_origin"] == str(checkout)
    assert result["ok"], result["findings"]


@pytest.mark.parametrize("case", ORIGIN_CASES, ids=lambda c: c.arm)
def test_gate_reads_every_input_from_the_target_origin(tmp_path, case: OriginCase):
    """Point the gate at a checkout and all four inputs come from that checkout.

    One input reading from somewhere else is not a smaller version of this bug —
    it is a gate comparing two revisions and reporting the answer as one, which
    is green whenever the two happen to agree.
    """
    checkout = _fixture_checkout(tmp_path)
    edited = checkout / case.rel
    text = edited.read_text(encoding="utf-8")
    assert text.count(case.before) == 1, f"{case.arm}: anchor is not unique in {case.rel}"
    edited.write_text(text.replace(case.before, case.after, 1), encoding="utf-8")

    result = check_model_hub_ui_states(checkout)
    assert not result["ok"], f"{case.arm}: the gate read the repository, not the target"
    assert any(case.says in f["message"] for f in result["findings"]), (
        case.arm, case.says, result["findings"]
    )

    if case.arm != "spec":
        # And the borrowing works the other way: told to use this repository's
        # authorities, the same broken checkout passes. Without this half, an arm
        # hard-wired to the repository would still satisfy the assertion above
        # whenever the fixture and the repository disagree for any reason.
        borrowed = check_model_hub_ui_states(checkout, authorities=ROOT)
        assert borrowed["authority_origin"] == "this checkout"
        assert borrowed["ok"], borrowed["findings"]


def test_loader_suite_is_tiled_over_every_rule_and_every_arm():
    """Both loader rules, over everything each one governs.

    The same tiling `CLASS_UNIVERSES` gets, and for the same reason: the defect
    is not any one arm reading from the wrong place, it is a new arm arriving
    with a reading of its own and nobody noticing which rule it skipped.
    """
    from scripts.check_model_hub_ui_states import LOADER_ARMS, LOADER_RULES, SCOPES

    assert set(LOADER_ARMS) == set(LOADER_RULES)
    assert LOADER_ARMS["scope"] == tuple(SCOPES), "the scope arms are the declared ranges"

    covered = {"origin": {c.arm for c in ORIGIN_CASES}, "scope": {t.scope for t in SCOPE_TRAPS}}
    exempt = {"origin": {}, "scope": UNTRAPPED_SCOPES}
    for rule in LOADER_RULES:
        missing = [
            arm
            for arm in LOADER_ARMS[rule]
            if arm not in covered[rule] and arm not in exempt[rule]
        ]
        assert not missing, f"{rule}: arms with no case and no declared reason: {missing}"
        for arm, why in exempt[rule].items():
            assert arm in LOADER_ARMS[rule], f"{arm} is exempted and is not an arm"
            assert why.strip(), f"{arm} is exempted with no reason"
            assert arm not in covered[rule], f"{arm} is exempted and also covered"

    # A trap in every polarity, and an outside that really is outside — otherwise
    # every "the arm read no further" half is asserting nothing.
    assert {t.polarity for t in SCOPE_TRAPS} == {"collect", "excuse"}
    assert OUTSIDE_SECTION not in {s.where for s in SCOPES.values()}


def test_only_the_origin_class_reads_a_file():
    """One place opens a file, so there is one place a revision can be honoured.

    Read as structure rather than as a promise in a docstring: an arm that reads
    on its own is the defect, and it is invisible in a passing run — the numbers
    look right until the two revisions differ.
    """
    import ast

    source = (ROOT / "scripts/check_model_hub_ui_states.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reading = {"open", "read_text", "read_bytes", "glob", "rglob", "iterdir", "run"}

    def verbs(node) -> list[str]:
        return [
            (c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id)
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, (ast.Attribute, ast.Name))
            and (c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id) in reading
        ]

    outside = [
        f"{node.name}: {sorted(set(found))}"
        for node in tree.body
        if not (isinstance(node, ast.ClassDef) and node.name == "Origin")
        and (found := verbs(node))
    ]
    assert not outside, f"these read a file without going through Origin: {outside}"
    assert verbs(next(n for n in tree.body if getattr(n, "name", "") == "Origin")), (
        "Origin reads nothing, so this test would pass on a module that reads nowhere"
    )


def test_every_arm_declares_a_range_the_module_declares():
    """A range is asked for by name, and the name is written down.

    Both halves matter. A computed scope name cannot be checked against `SCOPES`
    at all, and a literal that is not in `SCOPES` is an arm that would read
    everything the moment the `KeyError` were softened into a default.
    """
    import ast

    from scripts.check_model_hub_ui_states import Document, SCOPES

    source = (ROOT / "scripts/check_model_hub_ui_states.py").read_text(encoding="utf-8")
    asked = [
        node.args[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "scope"
        and node.args
    ]
    assert asked, "no arm asks for a range, so this test is watching nothing"
    for arg in asked:
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"a scope is asked for by a computed name at line {arg.lineno}"
        )
        assert arg.value in SCOPES, f"line {arg.lineno} reads undeclared scope {arg.value!r}"

    with pytest.raises(KeyError):
        Document("").scope("a range nobody declared")


def test_a_document_outside_every_checkout_names_no_authority_by_default(tmp_path):
    """The one case with nothing to default to says so, instead of guessing.

    A document in a temporary directory has no `api.md` above it. Falling back to
    this repository's contracts would make every such run green against
    authorities the caller never chose — the failure mode this whole loader
    exists to prevent, wearing the friendliest possible face.
    """
    stray = tmp_path / "stray.md"
    stray.write_text((ROOT / SPEC).read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        check_model_hub_ui_states(stray)
    # Both remedies, because a caller who is told only that it failed will reach
    # for whichever one they guess.
    assert "authorities=" in str(refused.value)
    assert "revision" in str(refused.value)

    assert check_model_hub_ui_states(stray, authorities=ROOT)["ok"]


def test_targeted_permission_denial_contract_is_request_scoped_and_mirrored():
    event_schema = _schema("resolution-event.schema.json")
    event_validator = Draft7Validator(event_schema)
    permission_event = next(
        example
        for example in event_schema["examples"]
        if example["reason"] == "permission_denied"
    )
    event_validator.validate(permission_event)

    invalid_cooldown = {
        **permission_event,
        "kind": "cooldown",
        "to_source": None,
    }
    with pytest.raises(ValidationError):
        event_validator.validate(invalid_cooldown)

    provenance_schema = _schema("turn-provenance.schema.json")
    assert provenance_schema["properties"]["contract_version"]["const"] == 5
    permission_record = next(
        example
        for example in provenance_schema["examples"]
        if any(
            attempt["reason"] == "permission_denied"
            for attempt in example["failed_attempts"]
        )
    )
    Draft7Validator(provenance_schema).validate(permission_record)

    registry = json.loads(
        (CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8")
    )
    schemas = _mirror_schemas(registry)
    failed_reasons = _json_pointer(
        schemas["turn-provenance.schema.json"],
        "/properties/failed_attempts/items/properties/reason",
    )["enum"]
    failed_reasons.remove("permission_denied")
    with pytest.raises(AssertionError):
        _validate_mirror_entry(
            next(entry for entry in registry["entries"] if entry["id"] == "M3"),
            schemas,
        )


def test_v5_shape_amendments_reject_the_false_states_they_replace():
    supply_schema = _schema("agent-supply.schema.json")
    supply_validator = Draft7Validator(supply_schema)
    base_supply = {
        "backend": "claude",
        "mode": "hub",
        "menu_kind": "fixed",
        "selected_by_agent": None,
        "selected_model_id": None,
        "selected_model_explicit": False,
        "sources": {"order": [], "eligibility": []},
        "supply_status": None,
        "menu": None,
        "model_supply": [],
        "named_agents": [],
        "builtin_models": [],
        "standard_vendors": None,
    }
    supply_validator.validate(base_supply)

    invented = {
        **base_supply,
        "selected_model_id": None,
        "current": {
            "model_id": "claude-opus-4-6",
            "source_id": "src_anthkey01",
            "channel": "hub",
        },
    }
    with pytest.raises(ValidationError):
        supply_validator.validate(invented)

    invalid_explicitness = {
        **base_supply,
        "selected_model_explicit": True,
    }
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_explicitness)

    invalid_reason = copy.deepcopy(base_supply)
    invalid_reason["sources"]["eligibility"] = [
        {
            "source_id": "src_anthkey01",
            "eligible": False,
            "reason_key": "models.eligibility.subscription_wrong_clint",
        }
    ]
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_reason)

    signal_supply = copy.deepcopy(base_supply)
    signal_supply.update(
        {
            "selected_model_id": "claude-opus-4-6",
            "supply_status": "degraded",
        }
    )
    signal_supply["sources"] = {
        "order": ["src_anthkey01"],
        "eligibility": [
            {
                "source_id": "src_claudepro1",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": True,
                "process_availability_reason": "native_cli_unavailable",
            },
            {
                "source_id": "src_anthkey01",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": True,
                "process_availability_reason": None,
            },
            {
                "source_id": "src_otherkey1",
                "eligible": True,
                "reason_key": None,
                "in_current_model_chain": False,
                "process_availability_reason": None,
            },
        ],
    }
    supply_validator.validate(signal_supply)

    invalid_availability = copy.deepcopy(signal_supply)
    invalid_availability["sources"]["eligibility"][0]["process_availability_reason"] = "gateway_down"
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_availability)

    invalid_membership = copy.deepcopy(signal_supply)
    invalid_membership["sources"]["eligibility"][2]["in_current_model_chain"] = "not_supplying"
    with pytest.raises(ValidationError):
        supply_validator.validate(invalid_membership)

    event_validator = Draft7Validator(_schema("resolution-event.schema.json"))
    source_wide = _schema("resolution-event.schema.json")["examples"][1]
    event_validator.validate(source_wide)
    invalid_event = {**source_wide, "agent": "system", "kind": "supply_interrupted"}
    with pytest.raises(ValidationError):
        event_validator.validate(invalid_event)

    probe = copy.deepcopy(_schema("probe-result.schema.json")["examples"][0])
    probe["source_id"] = "direct"
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("probe-result.schema.json")).validate(probe)

    canceled = next(
        example
        for example in _schema("turn-provenance.schema.json")["examples"]
        if example["outcome"] == "canceled"
    )
    Draft7Validator(_schema("turn-provenance.schema.json")).validate(canceled)
    invented_failure = copy.deepcopy(canceled)
    invented_failure["canceled_attempt"]["reason"] = "server_error"
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("turn-provenance.schema.json")).validate(invented_failure)

    chain_schema = _schema("agent-chain.schema.json")
    chain_validator = Draft7Validator(chain_schema)
    for example in chain_schema["examples"]:
        chain_validator.validate(example)
    exact_hop = {
        "contract_version": 5,
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "chain": [{
            "source_id": "src_claudepro1",
            "model_id": "claude-opus-4-6",
            "channel": "native_cli",
            "health": "healthy",
            "runnable": True,
            "reason": None,
            "retry_at": None,
        }],
        "current": {"source_id": "src_claudepro1", "model_id": "claude-opus-4-6"},
        "supply_state": "ok",
    }
    chain_validator.validate(exact_hop)
    for retired in ("via_mapping", "resolved_model_id"):
        invalid = copy.deepcopy(exact_hop)
        invalid["chain"][0][retired] = False if retired == "via_mapping" else "old"
        with pytest.raises(ValidationError):
            chain_validator.validate(invalid)
    invalid_reason = copy.deepcopy(exact_hop)
    invalid_reason["chain"][0]["reason"] = "invented"
    with pytest.raises(ValidationError):
        chain_validator.validate(invalid_reason)

    probe_schema = _schema("probe-result.schema.json")
    probe_validator = Draft7Validator(probe_schema)
    native_ready = copy.deepcopy(probe_schema["examples"][-2])
    probe_validator.validate(native_ready)
    native_ready["latency_ms"] = 12
    with pytest.raises(ValidationError):
        probe_validator.validate(native_ready)

    native_not_ready = copy.deepcopy(probe_schema["examples"][-1])
    probe_validator.validate(native_not_ready)
    native_not_ready_without_reason = copy.deepcopy(native_not_ready)
    native_not_ready_without_reason["error"] = None
    with pytest.raises(ValidationError):
        probe_validator.validate(native_not_ready_without_reason)

    timed_native_not_ready = copy.deepcopy(native_not_ready)
    timed_native_not_ready["latency_ms"] = 12
    with pytest.raises(ValidationError):
        probe_validator.validate(timed_native_not_ready)


def test_model_hub_config_round_trip_and_serializer_completeness(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_example = {
        **_schema("source.schema.json")["examples"][0],
        "supply_channel": "hub",
        "credential_ref": "cred_serializer_test",
    }
    hub_payload = {
        "sources": [source_example],
        "agents": {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub").to_payload()
            for backend in ("claude", "codex", "opencode")
        },
    }
    config = default_config()
    config.model_hub = ModelHubConfig.from_payload(hub_payload)
    config.save()

    loaded = V2Config.load()
    disk_payload = json.loads(Path(tmp_path, "config", "config.json").read_text(encoding="utf-8"))
    api_payload = api.config_to_payload(loaded)
    expected_root = {field.name for field in fields(ModelHubConfig)}
    source_fields = {field.name for field in fields(ModelHubSourceConfig)}
    source_state_fields = {field.name for field in fields(ModelHubSourceStateConfig)}
    source_usage_fields = {field.name for field in fields(ModelHubSourceUsageConfig)}
    source_model_fields = {field.name for field in fields(ModelHubModelConfig)}
    source_model_fields.discard("provenance")
    source_model_fields.add("origin")
    agent_fields = {field.name for field in fields(ModelHubAgentSupplyConfig)}
    agent_sources_fields = {field.name for field in fields(ModelHubAgentSourcesConfig)}
    route_fields = {field.name for field in fields(ModelHubRouteConfig)}
    menu_fields = {field.name for field in fields(ModelHubMenuConfig)}

    assert expected_root == set(api_payload["model_hub"])
    assert expected_root == set(disk_payload["model_hub"])
    for label, serialized_hub in (
        ("config_to_payload", api_payload["model_hub"]),
        ("V2Config.save", disk_payload["model_hub"]),
    ):
        serialized_source = serialized_hub["sources"][0]
        assert source_fields == set(serialized_source), label
        assert serialized_source["last_discovered_at"] == source_example["last_discovered_at"], label
        assert source_state_fields == set(serialized_source["state"]), label
        assert source_usage_fields == set(serialized_source["usage"]), label
        assert source_model_fields == set(serialized_source["models"][0]), label
        assert agent_fields == set(serialized_hub["agents"]["claude"]), label
        assert agent_sources_fields == set(serialized_hub["agents"]["claude"]["sources"]), label
        assert route_fields == set(ModelHubRouteConfig().to_payload()), label
        assert menu_fields == set(serialized_hub["agents"]["opencode"]["menu"]), label

    stale_hub_payload = json.loads(json.dumps(api_payload["model_hub"]))
    stale_hub_payload["priority_order"] = ["legacy-is-dropped"]
    updated = api.save_config({"show_duration": True, "model_hub": stale_hub_payload})
    assert updated.model_hub.to_payload() == loaded.model_hub.to_payload()
    assert api.config_to_payload(updated)["model_hub"] == api_payload["model_hub"]


def test_legacy_and_fresh_configs_both_default_direct():
    payload = api.config_to_payload(default_config(), include_secrets=True)
    payload.pop("model_hub")
    legacy = V2Config.from_payload(payload)

    assert {agent.mode for agent in legacy.model_hub.agents.values()} == {"direct"}
    assert {agent.mode for agent in default_config().model_hub.agents.values()} == {"direct"}


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_model_hub_release_capability_accepts_explicit_truthy_values(value):
    assert is_model_hub_enabled({MODEL_HUB_ENABLED_ENV: value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "unexpected"])
def test_model_hub_release_capability_defaults_and_fails_closed(value):
    assert is_model_hub_enabled({MODEL_HUB_ENABLED_ENV: value}) is False


def test_final_config_rejects_retired_consent_metadata():
    source = _schema("source.schema.json")["examples"][0]
    source = {**source, "supply_channel": "hub"}
    payload = {
        "sources": [source],
        "agents": {},
    }

    payload["sources"][0]["experimental_consent_at"] = "2026-07-23T03:00:00Z"
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def test_final_config_rejects_retired_global_priority_key():
    payload = ModelHubConfig().to_payload()
    payload["priority_order"] = {"legacy": "shape-does-not-matter"}

    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def test_agent_source_orders_validate_existence_eligibility_and_uniqueness():
    source = {
        **_schema("source.schema.json")["examples"][0],
        "created_at": "2026-07-29T01:00:00Z",
    }
    base = ModelHubConfig().to_payload()
    base["sources"] = [source]
    base["agents"]["claude"]["sources"] = {
        "order": [source["id"]],
    }

    ModelHubConfig.from_payload(base)

    for invalid_order in (
        [source["id"], source["id"]],
        ["src_missing001"],
    ):
        invalid = json.loads(json.dumps(base))
        invalid["agents"]["claude"]["sources"]["order"] = invalid_order
        with pytest.raises(ValueError):
            ModelHubConfig.from_payload(invalid)

    ineligible = json.loads(json.dumps(base))
    ineligible["agents"]["codex"]["sources"] = {
        "order": [source["id"]],
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(ineligible)


def test_route_hops_allow_one_source_to_supply_distinct_models():
    route = ModelHubRouteConfig.from_payload(
        {
            "hops": [
                {"source_id": "src_same0001", "model_id": "model-a"},
                {"source_id": "src_same0001", "model_id": "model-b"},
            ]
        }
    )

    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        ("src_same0001", "model-a"),
        ("src_same0001", "model-b"),
    ]
    source = ModelHubSourceConfig.from_payload(
        _schema("source.schema.json")["examples"][0]
    )
    source.models.append(
        ModelHubModelConfig(id="claude-opus-4-5", provenance="manual")
    )
    config = ModelHubConfig(sources=[source])
    config.agents["claude"].sources.order = [source.id]
    config.agents["claude"].routes["claude-opus-4-6"] = ModelHubRouteConfig.from_payload(
        {
            "hops": [
                {"source_id": source.id, "model_id": "claude-opus-4-6"},
                {"source_id": source.id, "model_id": "claude-opus-4-5"},
            ]
        }
    )

    canonical = ModelHubConfig.from_payload(config.to_payload())

    assert [hop.model_id for hop in canonical.agents["claude"].routes["claude-opus-4-6"].hops] == [
        "claude-opus-4-6",
        "claude-opus-4-5",
    ]
    with pytest.raises(ValueError, match="unique pairs"):
        ModelHubRouteConfig.from_payload(
            {
                "hops": [
                    {"source_id": "src_same0001", "model_id": "model-a"},
                    {"source_id": "src_same0001", "model_id": "model-a"},
                ]
            }
        )


def _ordering_source(
    source_id: str,
    *,
    kind: str,
    vendor: str,
    channel: str,
    created_at: str = MODEL_HUB_LEGACY_CREATED_AT,
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol="anthropic" if vendor == "anthropic" else "openai_responses",
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[],
        created_at=created_at,
    )


def test_recommended_order_is_backend_native_then_created_at_and_id():
    native = _ordering_source(
        "src_nativeaaa",
        kind="subscription",
        vendor="anthropic",
        channel="native_cli",
    )
    hub_subscription = _ordering_source(
        "src_hubsubaaa",
        kind="subscription",
        vendor="anthropic",
        channel="hub",
    )
    wrong_vendor_subscription = _ordering_source(
        "src_openaisub",
        kind="subscription",
        vendor="openai",
        channel="native_cli",
    )
    legacy_b = _ordering_source(
        "src_legacybbb",
        kind="api_key",
        vendor="openai",
        channel="hub",
    )
    legacy_a = _ordering_source(
        "src_legacyaaa",
        kind="api_key",
        vendor="anthropic",
        channel="hub",
    )
    newer = _ordering_source(
        "src_newer0001",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T03:00:00Z",
    )
    same_time_b = _ordering_source(
        "src_tiebbbb1",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T04:00:00Z",
    )
    same_time_a = _ordering_source(
        "src_tieaaaa1",
        kind="api_key",
        vendor="custom",
        channel="hub",
        created_at="2026-07-29T04:00:00Z",
    )
    config = ModelHubConfig(
        sources=[
            same_time_b,
            newer,
            wrong_vendor_subscription,
            legacy_b,
            hub_subscription,
            same_time_a,
            native,
            legacy_a,
        ],
    )

    assert config.recommended_source_order("claude") == [
        native.id,
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("codex") == [
        wrong_vendor_subscription.id,
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("opencode") == [
        hub_subscription.id,
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]


def test_persisted_hub_config_requires_explicit_complete_route_rows():
    payload = ModelHubConfig().to_payload()

    missing = json.loads(json.dumps(payload))
    del missing["agents"]["claude"]["routes"]
    with pytest.raises(ValueError, match="routes.*required"):
        ModelHubConfig.from_payload(missing)

    non_object = json.loads(json.dumps(payload))
    non_object["agents"]["claude"]["routes"] = []
    with pytest.raises(ValueError, match="routes.*object"):
        ModelHubConfig.from_payload(non_object)

    incomplete = json.loads(json.dumps(payload))
    incomplete["agents"]["claude"]["routes"].pop("claude-opus-4-6")
    with pytest.raises(ValueError, match="missing menu model"):
        ModelHubConfig.from_payload(incomplete)

    extra = json.loads(json.dumps(payload))
    extra["agents"]["claude"]["routes"]["claude-hidden-model"] = {
        "hops": []
    }
    with pytest.raises(ValueError, match="contains non-menu model"):
        ModelHubConfig.from_payload(extra)

    dynamic = json.loads(json.dumps(payload))
    dynamic["agents"]["opencode"]["mode"] = "hub"
    dynamic["agents"]["opencode"]["menu"] = {
        "view": "featured",
        "checked": ["custom/model"],
    }
    dynamic["agents"]["opencode"]["routes"] = {}
    with pytest.raises(ValueError, match="missing menu model 'custom/model'"):
        ModelHubConfig.from_payload(dynamic)


def test_config_reload_migrates_fixed_routes_when_bundled_catalog_changes(monkeypatch, tmp_path):
    payload = api.config_to_payload(default_config())
    original_ids = tuple(payload["model_hub"]["agents"]["claude"]["routes"])
    removed_id = original_ids[0]
    added_id = "claude-catalog-added-after-save"
    stale_id = "claude-catalog-removed-after-save"
    routes = payload["model_hub"]["agents"]["claude"]["routes"]
    routes.pop(removed_id)
    routes[stale_id] = {"hops": []}

    def changed_catalog_ids(backend: str) -> tuple[str, ...]:
        if backend == "claude":
            return (*original_ids[1:], added_id)
        return tuple(payload["model_hub"]["agents"][backend]["routes"])

    monkeypatch.setattr("config.v2_config.model_hub_fixed_menu_ids", changed_catalog_ids)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    migrated_routes = loaded.model_hub.agents["claude"].routes

    assert set(migrated_routes) == set(changed_catalog_ids("claude"))
    assert migrated_routes[added_id].hops == ()
    assert removed_id not in migrated_routes
    assert stale_id not in migrated_routes


@pytest.mark.parametrize(
    ("status", "retry_at", "detail_key"),
    [
        ("standby", "2026-07-29T03:00:00Z", None),
        ("cooldown", None, "models.source.cooldown.network"),
        ("needs_action", None, "models.source.cooldown.network"),
        ("error", None, None),
    ],
)
def test_source_state_rejects_invalid_status_correlations(status, retry_at, detail_key):
    with pytest.raises(ValueError):
        ModelHubSourceStateConfig.from_payload(
            {
                "status": status,
                "retry_at": retry_at,
                "detail_key": detail_key,
            }
        )


def test_source_optional_fields_reject_schema_invalid_values():
    source = _schema("source.schema.json")["examples"][0]
    invalid_sources = []

    invalid = json.loads(json.dumps(source))
    invalid["models"][0]["display_name"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["models"][0]["discovered_at"] = "2026-07-23T03:00:00"
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["last_discovered_at"] = "2026-07-23T03:00:00"
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["state"]["detail_key"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["usage"]["currency"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["experimental_consent_at"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["account_label"] = 1
    invalid_sources.append(invalid)

    invalid = json.loads(json.dumps(source))
    invalid["masked_credential"] = 1
    invalid_sources.append(invalid)

    for invalid in invalid_sources:
        try:
            ModelHubSourceConfig.from_payload(invalid)
        except ValueError:
            continue
        raise AssertionError(f"schema-invalid optional field was accepted: {invalid}")
