from __future__ import annotations

import ast
import copy
import inspect
import json
import re
import stat
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

import config.v2_config as v2_config
from config.v2_config import (
    MODEL_HUB_ENABLED_ENV,
    MODEL_HUB_LEGACY_CREATED_AT,
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
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
from core.handlers.model_hub.service import CONTRACT_VERSION
from core.services.settings import default_config
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    OBSERVATION_TERMINAL_RULES,
    ObservationDiscovery,
    ObservationOutcome,
    SOURCE_PROTOCOLS,
    SourceObservation,
    validate_source_observation,
)
from scripts.check_model_hub_authorities import (
    AuthorityInput,
    check as check_model_hub_authorities,
)
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
    tuple_match = re.search(
        r"export const SOURCE_PROTOCOLS\s*=\s*\[(.*?)\]\s*as const",
        type_source,
        re.DOTALL,
    )
    assert tuple_match is not None
    ui_protocols = tuple(re.findall(r"'([^']+)'", tuple_match.group(1)))
    assert frozenset(ui_protocols) == frozenset(protocols)
    assert len(ui_protocols) == len(set(ui_protocols))

    type_match = re.search(
        r"export type SourceProtocol\s*=\s*(.*?);",
        type_source,
        re.DOTALL,
    )
    assert type_match is not None
    assert re.sub(r"\s+", "", type_match.group(1)) == "(typeofSOURCE_PROTOCOLS)[number]"

    retired_alias = "openai" + "_compatible"
    for filename in (
        "types.ts",
        "vendorMeta.ts",
        "modelsApi.ts",
        "mockData.ts",
        "modelRows.test.ts",
    ):
        assert retired_alias not in (UI_MODEL_CONSUMERS / filename).read_text(encoding="utf-8")

    example = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    example["protocol"] = retired_alias
    with pytest.raises(ValidationError):
        Draft7Validator(_schema("source.schema.json")).validate(example)
    with pytest.raises(ValueError):
        ModelHubSourceConfig.from_payload(example)


def test_unsaved_observation_schema_closes_all_terminal_shapes():
    schema = _schema("observation-result.schema.json")
    assert schema["properties"]["contract_version"]["const"] == 10
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
            "contract_version": 10,
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
            "model_metadata": [
                {
                    "id": model.id,
                    "supported_parameters": (
                        list(model.supported_parameters)
                        if model.supported_parameters is not None
                        else None
                    ),
                }
                for model in observation.models
            ],
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
                models=(),
            )
            assert validate_source_observation(observation) is observation
            schema.validate(payload(observation))

        baseline = SourceObservation(
            outcome=outcome,
            reachable=next(iter(rule.reachable)),
            authenticated=next(iter(rule.authenticated)),
            protocol=next(iter(rule.protocols)),
            discovery=next(iter(rule.discoveries)),
            models=(),
        )
        invalid_fields = {
            "reachable": reachable_domain - rule.reachable,
            "authenticated": authenticated_domain - rule.authenticated,
            "protocol": protocol_domain - rule.protocols,
            "discovery": discovery_domain - rule.discoveries,
        }
        for field_name, rejected_values in invalid_fields.items():
            for rejected in rejected_values:
                invalid_observation = SourceObservation(**{**baseline.__dict__, field_name: rejected})
                with pytest.raises(ValueError):
                    validate_source_observation(invalid_observation)
                with pytest.raises(ValidationError):
                    schema.validate(payload(invalid_observation))

        if rule.models_must_be_empty:
            invalid_inventory = SourceObservation(
                **{
                    **baseline.__dict__,
                    "models": (DiscoveredModel(id="model-id"),),
                }
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
                    "models": (
                        DiscoveredModel(
                            id="model-id",
                            supported_parameters=("reasoning",),
                        ),
                    ),
                }
            )
            assert validate_source_observation(succeeded) is succeeded
            schema.validate(payload(succeeded))
            failed_with_inventory = SourceObservation(
                **{
                    **baseline.__dict__,
                    "discovery": ObservationDiscovery.FAILED,
                    "models": (DiscoveredModel(id="model-id"),),
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


def test_source_model_reasoning_effort_provenance_round_trips_current_shape():
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["models"][0]["reasoning_efforts"] = ["low", "high"]
    source["models"][0]["reasoning_efforts_source"] = "catalog"

    parsed = ModelHubSourceConfig.from_payload(source)
    serialized = parsed.to_payload()

    assert serialized["models"][0]["reasoning_efforts_source"] == "catalog"
    _assert_valid("source.schema.json", serialized)


def test_released_v6_model_shape_infers_user_or_null_tier_provenance():
    fixture = json.loads(
        Path(
            "tests/fixtures/model_hub/released_v6_reasoning_efforts.json"
        ).read_text(encoding="utf-8")
    )

    models = [
        ModelHubModelConfig.from_payload(model) for model in fixture["models"]
    ]

    assert models[0].reasoning_efforts_source == "user"
    assert models[1].reasoning_efforts_source is None
    assert models[0].to_payload()["reasoning_efforts_source"] == "user"
    assert models[1].to_payload()["reasoning_efforts_source"] is None


@pytest.mark.parametrize(
    ("efforts", "source"),
    [
        (["high"], None),
        ([], "user"),
        ([], "upstream"),
        (["high"], "unknown"),
    ],
)
def test_source_model_rejects_incoherent_tier_provenance(efforts, source):
    model = {
        "id": "model-a",
        "display_name": None,
        "origin": "manual",
        "reasoning_efforts": efforts,
        "reasoning_efforts_source": source,
        "discovered_at": None,
    }

    with pytest.raises(ValueError):
        ModelHubModelConfig.from_payload(model)


def test_discovered_model_retirement_is_persisted_without_rewriting_legacy_rows():
    example = copy.deepcopy(_schema("source.schema.json")["examples"][0]["models"][0])

    legacy = ModelHubModelConfig.from_payload(example)
    assert "retired" not in legacy.to_payload()

    retired = ModelHubModelConfig.from_payload({**example, "retired": True})
    assert retired.to_payload()["retired"] is True

    manual = {**example, "origin": "manual", "discovered_at": None, "retired": True}
    with pytest.raises(ValueError, match="retired"):
        ModelHubModelConfig.from_payload(manual)


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


def test_opencode_persisted_rows_use_bare_canonical_ids_and_require_native_protocol():
    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["menu"]["checked"] = ["gpt-5"]
    opencode["routes"]["gpt-5"] = {"hops": []}
    opencode["models"] = [
        ModelHubBackendModelConfig(
            id="gpt-5",
            native_protocol="openai_responses",
        ).to_payload()
    ]
    parsed = ModelHubConfig.from_payload(payload)
    assert parsed.agents["opencode"].models[0].id == "gpt-5"

    pre_v4 = copy.deepcopy(payload)
    pre_v4["agents"]["opencode"]["models"][0].pop("native_protocol")
    with pytest.raises(ValueError, match="native_protocol"):
        ModelHubConfig.from_payload(pre_v4)

    missing_menu = copy.deepcopy(payload)
    missing_menu["agents"]["opencode"].pop("menu")
    with pytest.raises(ValueError, match="menu.*required"):
        ModelHubConfig.from_payload(missing_menu)

    payload = ModelHubConfig().to_payload()
    opencode = payload["agents"]["opencode"]
    opencode["routes"][" authorization: sk-test-credential-material "] = {
        "hops": []
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)

    payload = ModelHubConfig().to_payload()
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    payload["sources"] = [source]
    codex_route = payload["agents"]["codex"]["routes"].setdefault("gpt-5.6", {"hops": []})
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
            key: value for key, value in raw_example.items() if key != "builtin_models"
        }
        if raw_example["backend"] == "opencode":
            example["models"] = [
                ModelHubBackendModelConfig(
                    id=model_id,
                    native_protocol=(
                        "anthropic"
                        if model_id.startswith("claude-")
                        else "openai_responses"
                    ),
                ).to_payload()
                for model_id in raw_example["menu"]["checked"]
            ]
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        # `builtin_models` is a read-only endpoint projection, not persisted
        # config. Reconstruct it
        # the way `_agent_payload` merges them onto to_payload().
        serialized = {
            **agent.to_payload(),
            "builtin_models": raw_example.get("builtin_models"),
        }
        serialized.pop("models", None)
        serialized.pop("removed_model_ids", None)
        if "routes" not in raw_example:
            serialized.pop("routes", None)
        if "sources" not in raw_example:
            serialized.pop("sources")
        else:
            serialized["sources"]["eligibility"] = raw_example["sources"].get("eligibility")
        assert _canonical(serialized) == _canonical(raw_example)
        _assert_valid("agent-supply.schema.json", serialized)


def test_source_client_nonce_round_trips_and_is_unique():
    first_payload = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    first_payload["client_nonce"] = "scn_01j5w8z7p4n6q2rt"
    first = ModelHubSourceConfig.from_payload(first_payload)

    assert first.client_nonce == first_payload["client_nonce"]
    assert first.to_payload()["client_nonce"] == first_payload["client_nonce"]
    _assert_valid("source.schema.json", first.to_payload())

    for invalid in (None, "scn_short", "SCN_01j5w8z7p4n6q2rt", "scn_01j5w8z7p4n6q2r!"):
        with pytest.raises(ValueError, match="client_nonce"):
            ModelHubSourceConfig.from_payload({**first_payload, "client_nonce": invalid})

    second_payload = copy.deepcopy(first_payload)
    second_payload["id"] = "src_relay9c2x"
    duplicate = ModelHubConfig(sources=[first, ModelHubSourceConfig.from_payload(second_payload)])
    with pytest.raises(ValueError, match="duplicate client nonces"):
        ModelHubConfig.from_payload(duplicate.to_payload())


def test_every_frozen_schema_example_is_valid_and_json_round_trips():
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        for example in schema.get("examples", []):
            validator.validate(example)
            assert _canonical(json.loads(_canonical(example))) == _canonical(example)


def test_source_create_unavailable_inventory_consent_is_explicit_and_total():
    schema = _schema("source-create.schema.json")
    validator = Draft7Validator(schema)
    field = schema["properties"]["accept_unavailable_inventory"]
    assert field["type"] == "boolean"
    assert field["default"] is False

    clean = copy.deepcopy(schema["examples"][0])
    assert "accept_unavailable_inventory" not in clean
    validator.validate(clean)

    explicit_false = {**clean, "accept_unavailable_inventory": False}
    validator.validate(explicit_false)

    accepted_failure = copy.deepcopy(schema["examples"][1])
    assert accepted_failure["accept_unavailable_inventory"] is True
    validator.validate(accepted_failure)

    for invalid in (None, "true", 1, {}):
        with pytest.raises(ValidationError):
            validator.validate({**clean, "accept_unavailable_inventory": invalid})

    api_contract = (CONTRACTS / "api.md").read_text(encoding="utf-8")
    consent_table = api_contract.split("### Source-create unavailable-inventory consent", 1)[1].split(
        "When `client_nonce` is present", 1
    )[0]
    result_rows = [line for line in consent_table.splitlines() if line.startswith("| Protocol ")]
    assert len(result_rows) == 4

    succeeded = next(line for line in result_rows if "`discovery: succeeded`" in line)
    assert "omitted, `false`, or `true`" in succeeded
    assert "Ordinary create" in succeeded
    assert "legitimately empty" in succeeded

    failed_rows = [
        line for line in result_rows if line.startswith("| Protocol established; `discovery: failed` |")
    ]
    assert len(failed_rows) == 2
    rejected = next(line for line in failed_rows if "omitted or `false`" in line)
    accepted = next(line for line in failed_rows if "| `true` |" in line)
    assert "`discovery_failed`" in rejected
    assert "no Source or committed credential" in rejected
    assert "AC-26 cleanup" in rejected
    assert "Commit exactly one Source" in accepted
    assert "`models: []`" in accepted
    assert "established protocol" in accepted

    unproved = next(line for line in result_rows if line.startswith("| Protocol not established"))
    assert "omitted, `false`, or `true`" in unproved
    assert "no Source is committed" in unproved
    assert "cannot authorize" in unproved

    implementation_plan = (CONTRACTS.parent / "model-hub-implementation.md").read_text(encoding="utf-8")
    ac_54 = implementation_plan.split("### AC-54", 1)[1].split("### K4 open contract-gap registry", 1)[0]
    for required in (
        "accept_unavailable_inventory",
        "K5 round 2",
        "After K4, #1312, and K6 merge",
        "I4's second increment",
        "tests/test_model_hub_{config,api}.py",
    ):
        assert required in ac_54


def test_sparse_override_contract_has_one_default_order_owner():
    contract = (CONTRACTS.parent / "model-hub-routing-modes.md").read_text(encoding="utf-8")
    assert "sources.order` is the sole backend-default" in contract
    assert "Key absence or an empty hop array means inherited routing" in contract
    assert "Restoring automatic deletes the key" in contract
    assert "Never reorder manual arrays" in contract
    assert "inference from nonempty array equality" in contract


def test_guard_refusal_error_requires_its_corresponding_nonempty_plan_array():
    schema = _schema("guard-refusal.schema.json")
    assert schema["required"] == [
        "ok",
        "contract_version",
        "error",
        "detail",
        "would_remove_hops",
        "would_interrupt",
    ]
    validator = Draft7Validator(schema)
    hop = {
        "backend": "claude",
        "menu_model": "claude-opus-4-6",
        "source_id": "src_anthkey01",
        "model_id": "claude-opus-4-6",
        "position": 1,
    }
    gap = {
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "agents": ["pm"],
    }

    route_refusal = {
        "ok": False,
        "contract_version": 10,
        "error": "source_in_route_chain",
        "detail": "modelHub.errors.source_in_route_chain",
        "would_remove_hops": [hop],
        "would_interrupt": [],
    }
    model_refusal = {**route_refusal, "error": "source_model_in_route_chain"}
    backend_refusal = {**route_refusal, "error": "backend_model_in_route"}
    supplier_refusal = {
        **route_refusal,
        "error": "source_last_supplier",
        "would_remove_hops": [],
        "would_interrupt": [gap],
    }
    candidate_refusal = {
        "ok": False,
        "contract_version": 10,
        "error": "candidate_suppliers_changed",
        "detail": "modelHub.errors.candidate_suppliers_changed",
        "changed": {
            "claude-sonnet-4-6": [
                {
                    "source_id": "src_anthkey01",
                    "model_id": "claude-sonnet-4-6",
                }
            ]
        },
    }
    for payload in (
        route_refusal,
        model_refusal,
        backend_refusal,
        supplier_refusal,
    ):
        validator.validate(payload)

    response_schema = _schema("api-response.schema.json")
    candidate_schema = copy.deepcopy(response_schema["definitions"]["CandidateSuppliersChangedResponse"])
    candidate_schema["properties"]["changed"]["additionalProperties"]["items"] = response_schema["definitions"][
        "ExpectedSupplier"
    ]
    candidate_validator = Draft7Validator(candidate_schema)
    candidate_validator.validate(candidate_refusal)

    for payload in (
        {**route_refusal, "would_remove_hops": [], "would_interrupt": [gap]},
        {**model_refusal, "would_remove_hops": [], "would_interrupt": [gap]},
        {**backend_refusal, "would_remove_hops": [], "would_interrupt": [gap]},
        {**supplier_refusal, "would_remove_hops": [hop], "would_interrupt": []},
        {**route_refusal, "would_remove_hops": [hop, hop]},
        {**supplier_refusal, "would_interrupt": [gap, gap]},
    ):
        with pytest.raises(ValidationError):
            validator.validate(payload)

    for payload in (
        {**candidate_refusal, "changed": {}},
        {key: value for key, value in candidate_refusal.items() if key != "detail"},
        {**candidate_refusal, "would_remove_hops": []},
    ):
        with pytest.raises(ValidationError):
            candidate_validator.validate(payload)


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
        "model_supply": [{"model_id": "claude-opus-4-5", "chain_length": 1, "route_origin": "automatic"}],
        "builtin_models": ["claude-opus-4-5"],
        "named_agents": [],
    }

    _assert_valid("agent-supply.schema.json", payload)


def test_v8_mirror_registry_is_executable_and_complete():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    schemas = _mirror_schemas(registry)

    assert registry["contract_version"] == 10
    ids = [entry["id"] for entry in registry["entries"]]
    assert ids
    assert len(ids) == len(set(ids))
    for entry in registry["entries"]:
        _validate_mirror_entry(entry, schemas)


def _versioned_nodes(node):
    """Yield every `contract_version` subschema, however deeply a branch nests it."""

    if isinstance(node, dict):
        declared = node.get("properties")
        if isinstance(declared, dict) and isinstance(declared.get("contract_version"), dict):
            yield declared["contract_version"]
        for value in node.values():
            yield from _versioned_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _versioned_nodes(item)


def test_every_versioned_object_ends_at_the_terminal_version_the_code_writes():
    # One number spans all versioned objects, so a bump has to move every one of
    # them at once — round 4 shipped it half-applied because nothing compared the
    # schemas with each other or with the writer. Reading the accepted values out
    # of whatever schemas the directory holds, rather than listing the ones that
    # carry a version, covers an object added later without editing this test.
    #
    # TurnProvenance is the one exception, and it earns it by being written to
    # disk: the same bump that republishes an envelope would strand every record
    # a released build persisted, so it accepts the released values and ends at
    # the terminal one. The `== [terminal]` branch is what keeps that from
    # spreading to objects that never outlive their request.
    registry = json.loads(
        (CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8")
    )
    terminal = registry["contract_version"]
    assert CONTRACT_VERSION == terminal
    persisted = registry["contract_version_closure"][
        "persisted_schema_version_floors"
    ]
    checked = set()
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        for node in _versioned_nodes(json.loads(path.read_text(encoding="utf-8"))):
            checked.add(path.name)
            accepted = [node["const"]] if "const" in node else list(node["enum"])
            assert accepted == sorted(set(accepted)), path.name
            assert accepted[-1] == terminal, path.name
            if path.name in persisted:
                assert accepted == list(
                    range(persisted[path.name], terminal + 1)
                ), path.name
            else:
                assert accepted == [terminal], path.name
    assert set(persisted) <= checked
    assert "api-response.schema.json" in checked

    # The schemas above are structured, so their versions are read from the shape.
    # Every other file beside them publishes the same number as text, and nothing
    # compared those: `api.md` carried the terminal value in eighteen envelopes
    # while three of its own declarations still named the previous one, which is
    # two review rounds spent on one stale sentence at a time. Matching the token
    # a consumer reads, rather than a list of sentences, is what makes the next
    # declaration fail here instead of in review — and it is why a claim about the
    # current value is written as `contract_version <n>` while a bare `vN` names
    # the generation a sentence was authored in.
    stated = 0
    for path in sorted(CONTRACTS.iterdir()):
        if not path.is_file() or path.name.endswith(".schema.json"):
            continue
        for value in re.findall(r"contract_version[^0-9]{0,12}(\d+)", path.read_text(encoding="utf-8")):
            stated += 1
            assert int(value) == terminal, path.name
    assert stated

    # The UI declares the same number as a literal type, and `tsc` is the only
    # thing that would have caught it drifting — one language boundary away from
    # every check above, which is where this bump went half-applied a second time.
    # A regex over the declaration is cheap; noticing in review is not.
    declared = re.search(
        r"^export const CONTRACT_VERSION = (\d+) as const;$",
        Path("ui/src/components/settings/models/types.ts").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert declared is not None
    assert int(declared.group(1)) == terminal

    ui_types = Path("ui/src/components/settings/models/types.ts").read_text(encoding="utf-8")
    persisted_versions = re.search(
        r"export const PERSISTED_TURN_CONTRACT_VERSIONS\s*=\s*(\[[^\]]*\])\s+as const;",
        ui_types,
    )
    assert persisted_versions is not None
    provenance_schema = json.loads((CONTRACTS / "turn-provenance.schema.json").read_text(encoding="utf-8"))
    assert json.loads(persisted_versions.group(1)) == provenance_schema["properties"]["contract_version"]["enum"]
    assert re.search(
        r"export type TurnProvenance\s*=\s*\{\s*contract_version:\s*"
        r"\(typeof PERSISTED_TURN_CONTRACT_VERSIONS\)\[number\];",
        ui_types,
    )


def test_contracts_readme_indexes_every_file_beside_it():
    # The index is what a reader consults to learn a contract exists at all, so a
    # file missing from it is invisible even though it ships. Both this PR's
    # `usage-summary.schema.json` and the older `api-response.schema.json` had gone
    # unlisted, which is what a hand-maintained list does on a long enough timeline.
    # Comparing against the directory rather than against a second list means the
    # next file added is caught by this test instead of by whoever needed it.
    readme = (CONTRACTS / "README.md").read_text(encoding="utf-8")
    indexed = set(re.findall(r"^\| `([^`]+)` \|", readme, flags=re.MULTILINE))
    present = {path.name for path in CONTRACTS.iterdir() if path.is_file()}
    assert present - indexed == set()
    assert indexed - present == set()


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


def test_d21_live_backoff_reason_consumers_match_authority_individually():
    registry = json.loads((CONTRACTS / "mirror-registry.json").read_text(encoding="utf-8"))
    decision = next(item for item in registry["decision_tables"] if item["id"] == "D21")
    authority_spec = decision["authority"]
    authority = _vocabulary(_schema(authority_spec["schema"]), authority_spec["paths"])

    observed = []
    for consumer in decision["consumers"]:
        if consumer["kind"] == "schema_vocabulary":
            observed.append(
                _vocabulary(
                    _schema(consumer["schema"]),
                    consumer["paths"],
                    exclude=consumer.get("exclude", ()),
                )
            )
        elif consumer["kind"] == "marker_tail":
            values = set()
            for line in Path(consumer["file"]).read_text(encoding="utf-8").splitlines():
                if consumer["marker"] in line:
                    tail = line.split(consumer["marker"], 1)[1].split("-->", 1)[0]
                    values.update(tail.split())
            observed.append(values)
        elif consumer["kind"] == "json_object_keys":
            payload = json.loads(Path(consumer["file"]).read_text(encoding="utf-8"))
            keys = _json_pointer(payload, consumer["path"])
            observed.append({f"{consumer.get('prefix', '')}{key}" for key in keys})
        else:
            raise AssertionError(consumer)

    assert observed
    assert all(values == authority for values in observed)

    mutated_probe = _schema("probe-result.schema.json")
    mutated_probe["properties"]["error"]["enum"].remove("models.source.backoff.connection_failed")
    probe_spec = next(item for item in decision["consumers"] if item["kind"] == "schema_vocabulary")
    assert (
        _vocabulary(
            mutated_probe,
            probe_spec["paths"],
            exclude=probe_spec.get("exclude", ()),
        )
        != authority
    )


def test_model_hub_authority_closure_is_generated_from_live_files():
    result = check_model_hub_authorities(Path.cwd())
    assert result["input_mode"] == "same_run_live_files"
    assert result["input_fingerprint"]
    assert result["ok"], result["findings"]


def test_model_hub_authority_closure_anchors_the_persisted_version_floor(monkeypatch):
    original_json = AuthorityInput.json

    def without_first_released_version(self, relative):
        payload = original_json(self, relative)
        if relative == "docs/plans/model-hub-contracts/turn-provenance.schema.json":
            payload = copy.deepcopy(payload)
            payload["properties"]["contract_version"]["enum"].remove(5)
        return payload

    monkeypatch.setattr(AuthorityInput, "json", without_first_released_version)

    result = check_model_hub_authorities(Path.cwd())

    assert not result["ok"]
    assert {
        "kind": "contract_version_schema_drift",
        "domain": "V1",
        "file": "docs/plans/model-hub-contracts/turn-provenance.schema.json",
        "values": [6, 7, 8, 9, 10],
        "expected": [5, 6, 7, 8, 9, 10],
    } in result["findings"]


def test_turn_provenance_contract_preserves_an_empty_stripped_effort():
    payload = copy.deepcopy(_schema("turn-provenance.schema.json")["examples"][0])
    payload["served"]["stripped_reasoning_efforts"] = [""]
    payload["served"]["declared_reasoning_efforts"] = []

    _assert_valid("turn-provenance.schema.json", payload)


def test_permission_denial_contract_has_no_fallback_member():
    retired_reason = "permission_denied"
    event_schema = _schema("resolution-event.schema.json")
    provenance_schema = _schema("turn-provenance.schema.json")
    assert retired_reason not in event_schema["properties"]["reason"]["enum"]
    assert (
        retired_reason
        not in (provenance_schema["properties"]["failed_attempts"]["items"]["properties"]["reason"]["enum"])
    )
    assert all(event.get("reason") != retired_reason for event in event_schema["examples"])
    assert all(
        attempt.get("reason") != retired_reason
        for record in provenance_schema["examples"]
        for attempt in record["failed_attempts"]
    )


def test_oauth_nonce_explicit_cancel_totality_is_closed():
    api_contract = (CONTRACTS / "api.md").read_text(encoding="utf-8")
    model_hub_plan = (CONTRACTS.parent / "model-hub.md").read_text(encoding="utf-8")
    implementation_plan = (CONTRACTS.parent / "model-hub-implementation.md").read_text(encoding="utf-8")

    cancel_route = next(line for line in api_contract.splitlines() if "POST `/api/models/oauth/cancel`" in line)
    assert "Cancels and forgets the flow." not in api_contract
    assert "committed flow with `client_nonce`" in cancel_route
    assert '`state: "cancelled"`' in cancel_route
    assert "existing `expires_at`" in cancel_route
    assert "flow without a nonce is forgotten" in cancel_route

    api_oauth = api_contract.split("## OAuth completion", 1)[1].split("## Chain and probe", 1)[0]
    plan_oauth = model_hub_plan.split("**OAuth-start nonce state machine", 1)[1].split("**Protocol observation", 1)[0]
    for section in (api_oauth, plan_oauth):
        assert section.count("| `oauth_nonce.released` |") == 1
        assert section.count("| `oauth_nonce.in_flight` |") == 1
        assert section.count("| `oauth_nonce.committed` |") == 1
        assert "retained canceled flow" in section
        assert "existing `expires_at`" in section
        assert 'retained `state: "cancelled"` flow' in section
        assert "none" in next(line for line in section.splitlines() if line.startswith("| `oauth_nonce.committed` |"))

    nonce_description = _schema("oauth-flow.schema.json")["properties"]["client_nonce"]["description"]
    assert "Explicit cancellation retains that committed flow" in nonce_description
    assert "starts no provider on a same-tuple retry" in nonce_description

    oauth_schema = _schema("oauth-flow.schema.json")
    oauth_validator = Draft7Validator(oauth_schema, format_checker=FormatChecker())
    nonce_with_expiry = copy.deepcopy(oauth_schema["examples"][0])
    oauth_validator.validate(nonce_with_expiry)

    nonce_without_expiry = copy.deepcopy(nonce_with_expiry)
    nonce_without_expiry["expires_at"] = None
    with pytest.raises(ValidationError):
        oauth_validator.validate(nonce_without_expiry)

    ordinary_without_expiry = copy.deepcopy(nonce_without_expiry)
    ordinary_without_expiry.pop("client_nonce")
    oauth_validator.validate(ordinary_without_expiry)

    ac_48 = implementation_plan.split("### AC-48", 1)[1].split("### AC-49", 1)[0]
    for fixture_pattern in (
        r"same-tuple canceled replay with zero provider calls",
        r"existing-expiry\s+release followed by one fresh provider start",
        r"no-nonce\s+forget",
    ):
        assert re.search(fixture_pattern, ac_48)


def test_v5_shape_amendments_reject_the_false_states_they_replace():
    api_contract = (CONTRACTS / "api.md").read_text(encoding="utf-8")
    credential_contract = api_contract.split("## Credential replacement and reauth", 1)[1].split(
        "## Source refresh and blocked-source recovery", 1
    )[0]
    assert "{source, recovered, interrupted_pairs}" not in credential_contract
    assert '"recovered"' not in credential_contract
    assert '"interrupted_pairs"' not in credential_contract
    assert api_contract.count("| `oauth_terminal.reauth_success` |") == 1
    reauth_contract = api_contract.split("## Credential replacement and reauth", 1)[1].split(
        "## Source refresh and blocked-source recovery", 1
    )[0]
    assert "Hub-channel repair does not require this acknowledgement" not in reauth_contract
    assert "For both Hub OAuth and\n   `native_cli` Sources" in reauth_contract
    assert re.search(
        r"Missing or false returns\s+`reauth_confirmation_required` before any OAuth adapter call",
        reauth_contract,
    )
    assert "Transactional API-key repair through `PUT …/credential` remains outside" in reauth_contract

    runtime_schema = _schema("runtime-dependency.schema.json")
    runtime_validator = Draft7Validator(runtime_schema)
    installing_runtime = copy.deepcopy(runtime_schema["examples"][0])
    installing_runtime["status"].update(
        {
            "installed_version": None,
            "verified": False,
            "listening": None,
            "health": "installing",
            "error_key": None,
        }
    )
    runtime_validator.validate(installing_runtime)
    for field, contradiction in (
        ("installed_version", "v7.2.95"),
        ("verified", True),
        ("listening", {"host": "127.0.0.1", "port": 15220}),
    ):
        invalid_installing = copy.deepcopy(installing_runtime)
        invalid_installing["status"][field] = contradiction
        with pytest.raises(ValidationError):
            runtime_validator.validate(invalid_installing)
    missing_installing_shape = copy.deepcopy(installing_runtime)
    del missing_installing_shape["status"]["listening"]
    with pytest.raises(ValidationError):
        runtime_validator.validate(missing_installing_shape)

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

    probe_schema = _schema("probe-result.schema.json")
    probe_validator = Draft7Validator(probe_schema)
    connection_failure = next(
        copy.deepcopy(example)
        for example in probe_schema["examples"]
        if example["error"] == "models.source.backoff.connection_failed"
    )
    probe_validator.validate(connection_failure)
    for contradiction in (
        {"channel": "native_cli"},
        {"latency_ms": 1},
    ):
        invalid_connection_failure = {**connection_failure, **contradiction}
        with pytest.raises(ValidationError):
            probe_validator.validate(invalid_connection_failure)

    probe = copy.deepcopy(probe_schema["examples"][0])
    probe["source_id"] = "direct"
    with pytest.raises(ValidationError):
        probe_validator.validate(probe)

    chain_schema = _schema("agent-chain.schema.json")
    chain_validator = Draft7Validator(chain_schema)
    ordinary_backoff = next(
        copy.deepcopy(example)
        for example in chain_schema["examples"]
        if example["chain"] and example["chain"][0]["reason"] == "models.source.backoff.connection_failed"
    )
    for blocker in (
        {
            "health": "needs_action",
            "reason": "models.source.needs_action.credential_revoked",
            "retry_at": None,
        },
        {
            "health": "healthy",
            "reason": "model_unsupported",
            "retry_at": None,
        },
    ):
        blocked = copy.deepcopy(ordinary_backoff)
        blocked["chain"][0].update(blocker)
        blocked["supply_state"] = "interrupted"
        chain_validator.validate(blocked)
        mislabeled_waiting = {**blocked, "supply_state": "waiting"}
        with pytest.raises(ValidationError):
            chain_validator.validate(mislabeled_waiting)

    canceled = next(
        example for example in _schema("turn-provenance.schema.json")["examples"] if example["outcome"] == "canceled"
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
    for waiting in (example for example in chain_schema["examples"] if example["supply_state"] == "waiting"):
        interrupted = copy.deepcopy(waiting)
        interrupted["supply_state"] = "interrupted"
        with pytest.raises(ValidationError):
            chain_validator.validate(interrupted)
    exact_hop = {
        "contract_version": 10,
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "manual_override": None,
        "route_origin": "automatic",
        "chain": [
            {
                "source_id": "src_claudepro1",
                "model_id": "claude-opus-4-6",
                "channel": "native_cli",
                "health": "healthy",
                "runnable": True,
                "reason": None,
                "retry_at": None,
            }
        ],
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

    model_supply_validator = Draft7Validator(supply_schema["properties"]["model_supply"]["items"])
    empty_model_supply = {
        "model_id": "claude-opus-4-6",
        "chain_length": 0,
        "has_runnable_hop": False,
        "route_origin": None,
    }
    model_supply_validator.validate(empty_model_supply)
    with pytest.raises(ValidationError):
        model_supply_validator.validate({**empty_model_supply, "has_runnable_hop": True})

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


def test_oauth_terminal_materialization_matrix_and_handoffs_are_total():
    api_contract = (CONTRACTS / "api.md").read_text(encoding="utf-8")
    model_hub_plan = (CONTRACTS.parent / "model-hub.md").read_text(encoding="utf-8")
    implementation_plan = (CONTRACTS.parent / "model-hub-implementation.md").read_text(encoding="utf-8")
    mirror_registry = _schema("mirror-registry.json")

    decisions = (
        "oauth_terminal.flow_only",
        "oauth_terminal.create_success",
        "oauth_terminal.reauth_success",
        "oauth_terminal.materialization_interrupted",
        "oauth_terminal.materialization_plain_error",
    )
    api_oauth = api_contract.split("## OAuth completion", 1)[1].split("## Chain and probe", 1)[0]
    plan_oauth = model_hub_plan.split("**OAuth terminal response and materialization-error matrix", 1)[1].split(
        "**Protocol observation", 1
    )[0]
    for section in (api_oauth, plan_oauth):
        for decision in decisions:
            assert section.count(f"| `{decision}` |") == 1

        reauth_success = next(
            line for line in section.splitlines() if line.startswith("| `oauth_terminal.reauth_success` |")
        )
        interrupted_error = next(
            line for line in section.splitlines() if line.startswith("| `oauth_terminal.materialization_interrupted` |")
        )
        plain_error = next(
            line for line in section.splitlines() if line.startswith("| `oauth_terminal.materialization_plain_error` |")
        )
        assert "may be empty" in reauth_success
        assert "present and nonempty" in interrupted_error
        assert "no `flow`" in interrupted_error or "`flow` is absent" in interrupted_error
        assert "absent" in plain_error
        assert "empty placeholder" in plain_error

    d22 = next(rule for rule in mirror_registry["decision_tables"] if rule["id"] == "D22")
    assert d22["authority"]["heading"] == ("OAuth terminal response and materialization-error matrix")
    assert d22["consumers"] == [
        {
            "kind": "marker",
            "file": "docs/plans/model-hub-contracts/api.md",
            "prefix": "oauth_terminal.",
        }
    ]

    ac_52 = implementation_plan.split("### AC-52", 1)[1].split("### AC-53", 1)[0]
    i3_lane = next(
        line
        for line in implementation_plan.splitlines()
        if line.startswith("| **I3 subscription custody and native import**")
    )
    auth_setup_landing = next(
        line for line in implementation_plan.splitlines() if line.startswith("| `tests/scenarios/auth_setup/")
    )
    for section in (ac_52, i3_lane, auth_setup_landing):
        assert "AUTH-SETUP-109" in section
    for path in (
        "tests/scenarios/auth_setup/catalog.yaml",
        "tests/scenarios/auth_setup/test_auth_setup_scenarios.py",
    ):
        assert path in ac_52
    assert "After K4, #1312, and K6 merge" in ac_52
    assert "earlier K4 + #1312 edge" in ac_52
    assert "missing and false acknowledgement" in ac_52
    assert "before any adapter/provider call" in ac_52
    assert "starts exactly one Hub flow" in ac_52
    assert "terminal status and the\nrepair read projection agree" in ac_52

    ac_53 = implementation_plan.split("### AC-53", 1)[1].split("### AC-54", 1)[0]
    assert "all five rows" in ac_53
    for path in (
        "core/handlers/model_hub/{service,errors}.py",
        "vibe/{ui_server,model_hub_client}.py",
        "tests/test_model_hub_api.py",
        "ui/src/components/settings/models/{modelsApi.ts,OAuthConnectDialog.tsx,apiFailure.test.ts,oauthResult.test.ts}",
    ):
        assert path in ac_53
    assert "After K4, #1312, and K6 merge" in ac_53
    assert "After K4 merges, K5 round 2" in ac_53
    assert "After\nthat K5 round and the I7 payload fixtures freeze" in ac_53
    assert "same-error/no-gap negative fixture" in ac_53


def test_model_hub_config_round_trip_and_serializer_completeness(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_example = {
        **_schema("source.schema.json")["examples"][0],
        "supply_channel": "hub",
        "credential_ref": "cred_serializer_test",
        "client_nonce": "scn_01j5w8z7p4n6q2rt",
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
        assert source_model_fields - {"retired"} == set(serialized_source["models"][0]), label
        assert serialized_source["models"][0].get("retired", False) is False, label
        assert agent_fields == set(serialized_hub["agents"]["claude"]), label
        assert agent_sources_fields == set(serialized_hub["agents"]["claude"]["sources"]), label
        assert route_fields == set(ModelHubRouteConfig().to_payload()), label
        assert menu_fields == set(serialized_hub["agents"]["opencode"]["menu"]), label

    stale_hub_payload = json.loads(json.dumps(api_payload["model_hub"]))
    stale_hub_payload["priority_order"] = ["legacy-is-dropped"]
    updated = api.save_config({"show_duration": True, "model_hub": stale_hub_payload})
    assert updated.model_hub.to_payload() == loaded.model_hub.to_payload()
    assert api.config_to_payload(updated)["model_hub"] == api_payload["model_hub"]


def test_backend_catalog_without_removed_ids_loads_empty_set(tmp_path):
    payload = api.config_to_payload(
        default_config(),
        include_secrets=True,
        include_internal=True,
    )
    fixture = json.loads(
        Path("tests/fixtures/model_hub/backend_catalog_without_removed_ids.json").read_text(
            encoding="utf-8"
        )
    )
    payload["model_hub"]["agents"]["codex"] = fixture
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    original = config_path.read_bytes()

    loaded = V2Config.load(config_path=config_path)

    assert config_path.read_bytes() == original
    agent = loaded.model_hub.agents["codex"]
    assert agent.removed_model_ids == []
    assert [model.to_payload() for model in agent.models] == fixture["models"]


def _legacy_model_hub_payload(current: dict) -> dict:
    """Build the released Claude/Codex v3.0.9 shape from a current fixture."""

    agents = {}
    for backend, agent in current["agents"].items():
        if backend == "opencode":
            continue
        agents[backend] = {
            "backend": agent["backend"],
            "mode": agent["mode"],
            "menu_kind": agent["menu_kind"],
            "sources": {"policy": "follow", "order": []},
            "mappings": [],
            "menu": agent.get("menu"),
        }
    return {
        "sources": [],
        "priority_order": [],
        "agents": agents,
        "subscription_hub_experimental": False,
    }


def test_config_reload_rejects_pre_v4_opencode_shape_on_invalid_config_path(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    opencode = payload["model_hub"]["agents"]["opencode"]
    opencode["menu"] = {"view": "featured", "checked": ["openai/gpt-5"]}
    opencode["models"] = [
        {
            "id": "openai/gpt-5",
            "display_name": "GPT-5",
            "origin": "manual",
            "models_dev_id": None,
            "context_window": None,
            "max_output_tokens": None,
            "input_modalities": [],
            "output_modalities": [],
            "supports_tools": None,
            "supports_reasoning": None,
            "reasoning_efforts": [],
        }
    ]
    opencode["routes"] = {"openai/gpt-5": {"hops": []}}
    opencode["mappings"] = [
        {
            "builtin_id": "openai/gpt-5",
            "target_model_id": "gpt-5",
            "enabled": True,
        }
    ]
    payload["migration_sentinel"] = {"keep": True}
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original
    backups = list(config_path.parent.glob("config.json.bak-recovery-*"))
    assert backups
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    migration_source = inspect.getsource(v2_config._migrate_legacy_model_hub_payload)
    assert "opencode" not in migration_source


def test_config_reload_recovers_malformed_legacy_collections(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    malformed_values = [
        42,
        [dict(source, models=42)],
    ]

    for index, malformed_sources in enumerate(malformed_values):
        payload = copy.deepcopy(current)
        payload["show_duration"] = True
        malformed = copy.deepcopy(legacy)
        malformed["sources"] = malformed_sources
        payload["model_hub"] = malformed
        config_path = tmp_path / f"config-{index}.json"
        original = json.dumps(payload)
        config_path.write_text(original, encoding="utf-8")

        loaded = V2Config.load(config_path=config_path)

        assert loaded.show_duration is True
        assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
        assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
        assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "invalid_platform",
    [
        "enabled",
        "enabled-type",
        "enabled-entry-type",
        "primary-type",
        "legacy",
        "legacy-type",
    ],
)
def test_config_reload_recovers_invalid_platform_metadata_only(monkeypatch, tmp_path, invalid_platform):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    if invalid_platform == "enabled":
        payload["platforms"]["enabled"] = ["not-a-platform"]
        payload["platforms"]["primary"] = "not-a-platform"
    elif invalid_platform == "enabled-type":
        payload["platforms"]["enabled"] = {"slack": True}
    elif invalid_platform == "enabled-entry-type":
        payload["platforms"]["enabled"] = [{}]
    elif invalid_platform == "primary-type":
        payload["platforms"]["primary"] = {}
    elif invalid_platform == "legacy":
        payload.pop("platforms", None)
        payload["platform"] = "not-a-platform"
    else:
        payload.pop("platforms", None)
        payload["platform"] = {}
    config_path = tmp_path / f"{invalid_platform}-platform.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platforms.enabled == []
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and "platforms" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("platform", "invalid_fields"),
    [
        ("slack", {"bot_token": "invalid"}),
        ("slack", {"bot_token": 123}),
        ("slack", {"app_token": 123}),
        ("discord", {"thread_auto_archive_minutes": 1}),
        ("discord", {"bot_token": {}}),
        ("telegram", {"bot_token": "invalid"}),
        ("lark", {"domain": "invalid"}),
        ("lark", {"app_id": 123}),
    ],
)
def test_config_reload_recovers_invalid_platform_adapter_only(
    monkeypatch,
    tmp_path,
    platform,
    invalid_fields,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["platform"] = platform
    payload["platforms"] = {"enabled": [platform], "primary": platform}
    platform_payload = dict(payload.get(platform) or {})
    platform_payload.update(invalid_fields)
    payload[platform] = platform_payload
    config_path = tmp_path / f"{platform}-adapter.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platforms.enabled == []
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and platform in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_ignores_stale_legacy_platform_when_platforms_are_valid(monkeypatch):
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["platform"] = "removed-platform"
    payload["platforms"] = {"enabled": ["slack"], "primary": "slack"}

    loaded = V2Config.from_payload(payload)

    assert loaded.platform == "slack"
    assert loaded.platforms.enabled == ["slack"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", {}),
        ("ack_mode", []),
    ],
)
def test_config_reload_recovers_invalid_scalar_enum_only(monkeypatch, tmp_path, field, value):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload[field] = value
    config_path = tmp_path / f"{field}-scalar.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platform == payload["platform"]
    assert loaded.load_warnings and field in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_backup_deduplication_repairs_permissions(tmp_path):
    config_path = tmp_path / "config.json"
    content = b"sensitive config"
    config_path.write_bytes(content)
    backup = config_path.with_name("config.json.bak-test-existing")
    backup.write_bytes(content)
    backup.chmod(0o644)

    result = v2_config._backup_config_file(config_path, "test", content=content)

    assert result == backup
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_config_reload_does_not_replace_file_when_migration_backup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(v2_config, "_backup_config_file", lambda *args, **kwargs: None)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() != payload["model_hub"]
    assert config_path.read_text(encoding="utf-8") == original
    assert loaded.load_warnings and "could not be backed up" in loaded.load_warnings[0]


def test_config_reload_recovery_backs_up_original_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = {"sources": "invalid", "agents": {}}
    config_path = tmp_path / "config.json"
    original = json.dumps(payload).encode("utf-8")
    config_path.write_bytes(original)
    original_backup = v2_config._backup_config_file

    def replace_before_backup(path, label, *, content=None):
        path.write_text(json.dumps(api.config_to_payload(default_config())), encoding="utf-8")
        return original_backup(path, label, content=content)

    monkeypatch.setattr(v2_config, "_backup_config_file", replace_before_backup)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings
    backups = list(config_path.parent.glob("config.json.bak-recovery-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_config_reload_migrates_legacy_mapping_to_exact_route_hop(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "native_cli"
    source["models"][0]["provenance"] = source["models"][0].pop("origin")
    model_id = source["models"][0]["id"]
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["claude"]["mappings"] = [
        {
            "builtin_id": model_id,
            "target_model_id": model_id,
            "enabled": True,
        }
    ]
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes[model_id]
    assert route.hops[0].source_id == source["id"]
    assert route.hops[0].model_id == model_id
    assert loaded.load_warnings == ()
    assert loaded.model_hub.sources[0].models[0].reasoning_efforts == []


def test_config_reload_spells_route_hops_like_the_inventory_they_name(monkeypatch, tmp_path):
    # A hop names a model in a source's inventory, and `inspect_exact_hop` decides
    # membership by comparing the two identifiers exactly, so the chain only
    # survives an upgrade while one rule spells both sides — the set equality below
    # is that same comparison, taken over the whole route. The file seeds a model
    # *and* the hop naming it in every spelling a persisted config can carry rather
    # than listing the spellings that are exempt: a spelling admitted later is
    # covered here without editing this test, which is what an enumeration misses.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    spellings = (
        lambda base: base,
        lambda base: f" {base}",
        lambda base: f"{base} ",
        lambda base: f"  {base}  ",
        lambda base: f"\t{base}\n",
        lambda base: "   ",
    )
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "native_cli"
    source["models"] = [
        {
            "id": spelling(f"claude-opus-4-{index}"),
            "display_name": None,
            "origin": "discovered",
            "reasoning_efforts": [],
            "discovered_at": "2026-07-23T03:00:00Z",
        }
        for index, spelling in enumerate(spellings)
    ]
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    current["model_hub"]["sources"] = [source]
    menu_model = "claude-opus-4-6"
    route = current["model_hub"]["agents"]["claude"]["routes"].setdefault(menu_model, {"hops": []})
    route["hops"] = [{"source_id": source["id"], "model_id": model["id"]} for model in source["models"]]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    hub = loaded.model_hub
    hops = hub.agents["claude"].routes[menu_model].hops
    assert {hop.model_id for hop in hops} == {model.id for model in hub.sources[0].models}


def test_loading_a_persisted_config_yields_one_this_product_can_load_again(monkeypatch, tmp_path):
    # The terminal property behind two rounds of findings, stated once instead of
    # per collection: whatever `load` returns for a file a released build wrote,
    # serializing it has to produce a file `load` accepts. Normalization is what
    # threatened it — a many-to-one map applied in the leaf validator while the
    # uniqueness check sits in the parent, so both spellings survive the load,
    # `to_payload` writes one spelling twice, and the *next* load raises. That
    # second load is the assertion, and it does not care which collections exist.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    padded = " claude-opus-4-6 "
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "native_cli"
    source["models"] = [
        {
            "id": spelling,
            "display_name": None,
            "origin": "discovered",
            "reasoning_efforts": [],
            "discovered_at": "2026-07-23T03:00:00Z",
        }
        for spelling in (padded.strip(), padded)
    ]
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    current["model_hub"]["sources"] = [source]
    menu_model = "claude-opus-4-6"
    route = current["model_hub"]["agents"]["claude"]["routes"].setdefault(menu_model, {"hops": []})
    route["hops"] = [{"source_id": source["id"], "model_id": spelling} for spelling in (padded.strip(), padded)]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    assert loaded.load_warnings == ()

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


def test_every_normalized_identifier_collection_collapses_through_one_owner():
    # Naming the class rather than its third member. Each collection whose leaf
    # validator settles a spelling needs its parent to collapse on the settled
    # value, and the two that exist cost one review round each because nothing
    # tied the two halves together. Counting them does: a normalization added
    # without its collapse fails here instead of arriving as a finding.
    source = Path("config/v2_config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    normalizing = set()
    collapsing = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id == "normalized_model_id":
                normalizing.add(node.name)
            elif call.func.id == "_collapse_settled_duplicates":
                collapsing.add(node.name)
    assert normalizing == {"ModelHubModelConfig", "ModelHubRouteHopConfig"}
    assert collapsing == {"ModelHubSourceConfig", "ModelHubRouteConfig"}
    assert len(collapsing) == len(normalizing)


def test_collapsing_a_settled_duplicate_is_a_repair_no_caller_gets_by_default():
    # The other half of the collapse: *who* asked. Repairing a file a released
    # build wrote has to keep loading, and admitting a request that names one
    # model twice has to say so — the same pair, two answers, so the parser cannot
    # infer it and the caller has to state it. This asserts the default rather
    # than the two call sites, because the default is what a call site added later
    # inherits without deciding: an ordinary `from_payload` collapses nothing.
    for owner in (ModelHubSourceConfig, ModelHubRouteConfig, ModelHubAgentSupplyConfig, ModelHubConfig):
        signature = inspect.signature(owner.from_payload)
        assert signature.parameters["repairing"].default is False, owner.__name__

    settled = inspect.signature(v2_config._collapse_settled_duplicates)
    assert settled.parameters["repairing"].default is inspect.Parameter.empty
    assert settled.parameters["repairing"].kind is inspect.Parameter.KEYWORD_ONLY


def test_a_live_chain_is_refused_the_duplicate_a_persisted_chain_is_repaired():
    """MH-USAGE-012: one payload, both doors, opposite answers.

    Which is the property, and the reason it is one test. The chain names one
    upstream model in two spellings, so it is one ledger row either way; what
    differs is whether the caller gets to learn that before it becomes one.
    """

    hops = [
        {"source_id": "src_same0001", "model_id": "model-a"},
        {"source_id": "src_same0001", "model_id": " model-a "},
    ]

    with pytest.raises(ValueError, match="unique pairs"):
        ModelHubRouteConfig.from_payload({"hops": copy.deepcopy(hops)})

    repaired = ModelHubRouteConfig.from_payload({"hops": copy.deepcopy(hops)}, repairing=True)
    assert [hop.model_id for hop in repaired.hops] == ["model-a"]


def test_config_reload_recovers_dangling_legacy_custom_source_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": ["src_missing123"],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "dangling-source-order.json"
    original = json.dumps(current)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_recovers_backend_ineligible_legacy_custom_source_order(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["codex"]["mode"] = "hub"
    legacy["agents"]["codex"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "ineligible-source-order.json"
    original = json.dumps(current)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_keeps_legacy_hub_subscription_backend_specific(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = "hub"
    source["credential_ref"] = "cred_anthropic_hub"
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["codex"]["mode"] = "hub"
    legacy["agents"]["codex"]["sources"] = {
        "policy": "follow",
        "order": [],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["claude"].sources.order == [source["id"]]
    assert loaded.model_hub.agents["codex"].sources.order == []
    assert loaded.load_warnings == ()


@pytest.mark.parametrize("supply_channel", ["native_cli", "hub"])
def test_config_reload_preserves_legacy_claude_alias_resolution(
    monkeypatch,
    tmp_path,
    supply_channel,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["supply_channel"] = supply_channel
    if supply_channel == "hub":
        source["credential_ref"] = "cred_anthropic_hub"
    older = source["models"][0]
    newer = {
        **older,
        "id": "claude-opus-5-20260724",
        "display_name": "Opus 5",
    }
    source["models"] = [older, newer]
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes["opus"]
    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        (source["id"], newer["id"]),
    ]
    assert loaded.load_warnings == ()


def test_config_reload_defaults_omitted_legacy_backend_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["agents"].pop("codex")
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.agents["codex"].mode == "direct"
    assert loaded.load_warnings == ()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "codex" in persisted["model_hub"]["agents"]


def test_config_reload_uses_first_enabled_duplicate_legacy_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    first_target = source["models"][0]["id"]
    later_target = "claude-opus-5-20260724"
    source["models"].append(
        {
            **source["models"][0],
            "id": later_target,
            "display_name": "Opus 5",
        }
    )
    for model in source["models"]:
        model["provenance"] = model.pop("origin")
    current = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(current["model_hub"])
    legacy["sources"] = [source]
    legacy["agents"]["claude"]["mode"] = "hub"
    legacy["agents"]["claude"]["sources"] = {
        "policy": "custom",
        "order": [source["id"]],
    }
    legacy["agents"]["claude"]["mappings"] = [
        {"builtin_id": "opus", "target_model_id": later_target, "enabled": False},
        {"builtin_id": "opus", "target_model_id": first_target, "enabled": True},
        {"builtin_id": "opus", "target_model_id": later_target, "enabled": True},
    ]
    current["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(current), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    route = loaded.model_hub.agents["claude"].routes["opus"]
    assert [(hop.source_id, hop.model_id) for hop in route.hops] == [
        (source["id"], first_target),
    ]


@pytest.mark.parametrize(
    "invalid_invariant",
    [
        "healthy-detail",
        "hub-credential",
        "manual-discovered-at",
        "subscription-api-key",
        "opencode-identity",
    ],
)
def test_config_reload_recovers_inner_model_hub_invariant_only(
    monkeypatch,
    tmp_path,
    invalid_invariant,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    if invalid_invariant == "healthy-detail":
        source["state"]["detail_key"] = "models.source.invalid"
    elif invalid_invariant == "hub-credential":
        source["supply_channel"] = "hub"
        source["credential_ref"] = None
    elif invalid_invariant == "manual-discovered-at":
        source["models"][0]["origin"] = "manual"
    elif invalid_invariant == "subscription-api-key":
        source["base_url"] = "https://api.anthropic.com"
    else:
        payload["model_hub"]["agents"]["opencode"]["models"] = [
            {
                "id": "invalid-opencode-identity",
                "display_name": None,
                "origin": "manual",
                "models_dev_id": None,
                "context_window": None,
                "max_output_tokens": None,
                "input_modalities": [],
                "output_modalities": [],
                "supports_tools": None,
                "supports_reasoning": None,
                "reasoning_efforts": [],
            }
        ]
    if invalid_invariant != "opencode-identity":
        payload["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
    persisted = json.loads(config_path.read_text(encoding="utf-8"))["model_hub"]
    if invalid_invariant == "opencode-identity":
        assert persisted["agents"]["opencode"]["models"][0]["id"] == ("invalid-opencode-identity")
    else:
        assert persisted["sources"] == [source]


def test_config_reload_allows_empty_target_on_disabled_legacy_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(payload["model_hub"])
    legacy["agents"]["claude"]["mappings"] = [{"builtin_id": "opus", "target_model_id": "", "enabled": False}]
    payload["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    assert "opus" not in loaded.model_hub.agents["claude"].routes


def test_invalid_json_recovery_backs_up_the_original_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    malformed = b'{"mode": '
    config_path.write_bytes(malformed)
    original_backup = v2_config._backup_config_file

    def replace_before_backup(path, label, *, content=None):
        path.write_text(json.dumps(api.config_to_payload(default_config())), encoding="utf-8")
        return original_backup(path, label, content=content)

    monkeypatch.setattr(v2_config, "_backup_config_file", replace_before_backup)

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings and "JSON" in loaded.load_warnings[0]
    backups = list(config_path.parent.glob("config.json.bak-invalid-json-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == malformed


def test_config_reload_does_not_overwrite_config_changed_during_migration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    concurrent_payload = {**payload, "show_duration": True}
    original_persist = v2_config._persist_migrated_config_payload

    def persist_after_concurrent_save(path, expected_raw, migrated_payload):
        path.write_text(json.dumps(concurrent_payload), encoding="utf-8")
        return original_persist(path, expected_raw, migrated_payload)

    monkeypatch.setattr(
        v2_config,
        "_persist_migrated_config_payload",
        persist_after_concurrent_save,
    )

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["show_duration"] is True
    assert set(persisted["model_hub"]) == {"enabled", "sources", "agents"}
    assert persisted["model_hub"]["enabled"] is False
    assert loaded.load_warnings and "changed during load" in loaded.load_warnings[0]


def test_config_reload_does_not_overwrite_config_changed_before_replace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = _legacy_model_hub_payload(payload["model_hub"])
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")
    concurrent_payload = {**payload, "show_duration": True}
    original_write = v2_config._write_config_payload_if_unchanged

    def write_after_concurrent_save(path, migrated_payload, expected_raw):
        path.write_text(json.dumps(concurrent_payload), encoding="utf-8")
        return original_write(path, migrated_payload, expected_raw)

    monkeypatch.setattr(
        v2_config,
        "_write_config_payload_if_unchanged",
        write_after_concurrent_save,
    )

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["show_duration"] is True
    assert set(persisted["model_hub"]) == {"enabled", "sources", "agents"}
    assert persisted["model_hub"]["enabled"] is False
    assert loaded.load_warnings and "before replacement" in " ".join(loaded.load_warnings)
    backups = list(config_path.parent.glob("config.json.bak-model-hub-migration-*"))
    assert backups and any(backup.read_text(encoding="utf-8") == original for backup in backups)


def test_config_reload_recovers_invalid_optional_section_without_overwriting_file(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["model_hub"] = {"sources": "invalid", "agents": {}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]
    recovery = api.client_config_payload(loaded)["config_recovery"]
    assert recovery["required"] is True
    assert recovery["warnings"]
    assert recovery["warnings"] != list(loaded.load_warnings)
    assert json.loads(config_path.read_text(encoding="utf-8"))["model_hub"]["sources"] == "invalid"
    assert list(config_path.parent.glob("config.json.bak-recovery-*"))
    V2Config.load(config_path=config_path)
    assert len(list(config_path.parent.glob("config.json.bak-recovery-*"))) == 1

    monkeypatch.setattr(api, "load_config", lambda: loaded)
    with pytest.raises(ValueError, match="recovery warnings"):
        api.save_config({"show_duration": True})
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda source: source["state"].update({"status": {}}),
        lambda source: source.update({"kind": []}),
        lambda source: source.update({"protocol": {}}),
    ],
    ids=["state-status", "source-kind", "source-protocol"],
)
def test_config_reload_recovers_non_scalar_model_hub_enums(monkeypatch, tmp_path, mutate):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    mutate(source)
    payload["model_hub"]["sources"] = [source]
    payload["show_duration"] = True
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)


@pytest.mark.parametrize("malformed_source", ([], {}), ids=("array", "object"))
def test_config_reload_recovers_malformed_reasoning_tier_provenance_only(
    monkeypatch,
    tmp_path,
    malformed_source,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(
        default_config(),
        include_secrets=True,
        include_internal=True,
    )
    source = copy.deepcopy(_schema("source.schema.json")["examples"][0])
    source["models"][0]["reasoning_efforts_source"] = malformed_source
    payload["model_hub"]["sources"] = [source]
    payload["show_duration"] = True
    payload["platform"] = "slack"
    payload["platforms"] = {"enabled": ["slack"], "primary": "slack"}
    payload["agents"]["codex"]["enabled"] = True
    payload["agents"]["codex"]["cli_path"] = "/preserved/codex"
    config_path = tmp_path / f"malformed-tier-source-{type(malformed_source).__name__}.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.platform == "slack"
    assert loaded.platforms.enabled == ["slack"]
    assert loaded.agents.codex.enabled is True
    assert loaded.agents.codex.cli_path == "/preserved/codex"
    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.recovered_sections == ("model_hub",)
    assert loaded.whole_config_recovery is False
    assert loaded.load_warnings and "model_hub" in loaded.load_warnings[0]


def test_client_config_recovery_projection_redacts_validator_details(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["platforms"] = {"enabled": ["sk-leaked-platform-token"], "primary": "avibe"}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    raw_warnings = json.dumps(loaded.load_warnings)
    projected = api.client_config_payload(loaded)

    assert "sk-leaked-platform-token" in raw_warnings
    assert "sk-leaked-platform-token" not in json.dumps(projected)
    assert projected["config_recovery"]["warnings"]


def test_config_reload_recovers_runtime_with_the_canonical_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["runtime"] = {"log_level": "DEBUG"}
    payload["show_duration"] = True
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.runtime.default_cwd == str(Path.home() / "work")
    assert loaded.runtime.log_level == "INFO"
    assert loaded.show_duration is True
    assert loaded.load_warnings and "runtime" in loaded.load_warnings[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda hub: hub["agents"]["claude"].update({"sources": "invalid"}),
        lambda hub: hub["agents"]["claude"].update({"sources": {"policy": "custom", "order": "invalid"}}),
        lambda hub: hub["agents"]["claude"].update({"sources": {"order": ["src_missing001"]}}),
        lambda hub: hub["agents"]["claude"].update({"sources": {"policy": {}, "order": []}}),
        lambda hub: hub.update({"priority_order": {"invalid": True}}),
        lambda hub: hub.update({"priority_order": ["src_missing001"]}),
        lambda hub: hub.update({"subscription_hub_experimental": "false"}),
        lambda hub: hub["sources"].append({"experimental_consent_at": 1}),
        lambda hub: hub["sources"].append(
            {
                "models": [
                    {
                        "id": "legacy-model",
                        "origin": "manual",
                        "provenance": "discovered",
                        "reasoning_efforts": [],
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update({"mappings": ["invalid"]}),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update({"mode": "hbu"}),
        lambda hub: hub["agents"]["claude"].update({"mode": []}),
        lambda hub: hub["agents"].update({"future-backend": {"mode": "hub"}}),
        lambda hub: hub["agents"]["claude"].update({"future-field": True}),
        lambda hub: hub["agents"]["claude"].update({"backend": "codex"}),
        lambda hub: hub["agents"]["claude"].pop("backend"),
        lambda hub: hub["agents"]["claude"].update({"menu_kind": "open"}),
        lambda hub: hub["agents"]["claude"].pop("menu_kind"),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "opus",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": True,
                        "future_field": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update({"routes": "invalid"}),
        lambda hub: hub["agents"]["claude"].update({"mappings": [{"builtin_id": "opus", "enabled": True}]}),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "retired-model",
                        "target_model_id": "claude-opus-4-6",
                        "enabled": True,
                    }
                ]
            }
        ),
        lambda hub: hub["agents"]["claude"].update(
            {
                "mappings": [
                    {
                        "builtin_id": "opus",
                        "target_model_id": "claude-opus-4-5",
                        "enabled": "yes",
                    }
                ]
            }
        ),
    ],
    ids=[
        "sources-not-object",
        "custom-order-not-array",
        "order-only-dangling",
        "policy-not-scalar",
        "priority-order-not-array",
        "priority-order-dangling",
        "subscription-hub-experimental-not-bool",
        "experimental-consent-not-date-time",
        "provenance-conflict",
        "mapping-not-object",
        "mapping-empty-builtin",
        "agent-invalid-mode",
        "agent-non-scalar-mode",
        "agent-unknown-backend",
        "agent-unknown-field",
        "agent-backend-mismatch",
        "agent-backend-missing",
        "agent-menu-kind-mismatch",
        "agent-menu-kind-missing",
        "mapping-unknown-field",
        "routes-not-object",
        "mapping-missing-target",
        "mapping-retired-menu",
        "mapping-enabled-not-boolean",
    ],
)
def test_config_reload_does_not_infer_malformed_legacy_source_order(
    monkeypatch,
    tmp_path,
    mutate,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    legacy = _legacy_model_hub_payload(payload["model_hub"])
    mutate(legacy)
    payload["model_hub"] = legacy
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.model_hub.to_payload() == V2Config.default().model_hub.to_payload()
    assert loaded.load_warnings and "model_hub" in " ".join(loaded.load_warnings)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(config_path.parent.glob("config.json.bak-recovery-*"))


def test_config_reload_recovers_invalid_codex_agent_with_disabled_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["agents"]["codex"] = "invalid"
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.agents.codex.enabled is False
    assert loaded.agents.claude.enabled is True
    assert loaded.load_warnings and "agents.codex" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_load_degrades_codex_relay_marker_shapes(monkeypatch, tmp_path):
    """Persisted-shape rule for the new ``oauth_relay_marker`` field:
    a config written before the field existed loads with ``None``
    (released-shape compatibility), a well-formed marker round-trips,
    and a corrupt value is filtered out at consumption
    (``read_codex_relay_marker``) rather than breaking startup."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["agents"]["codex"].pop("oauth_relay_marker", None)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    legacy = V2Config.load(config_path=config_path)
    assert legacy.agents.codex.oauth_relay_marker is None

    legacy.agents.codex.oauth_relay_marker = {"provider_id": "OpenAI", "base_url": "https://relay.example/v1"}
    legacy.save()
    # ``save()`` persists to the canonical AVIBE_HOME config; reload from
    # the same place to prove the marker round-trips through JSON.
    reloaded = V2Config.load()
    assert reloaded.agents.codex.oauth_relay_marker == {
        "provider_id": "OpenAI",
        "base_url": "https://relay.example/v1",
    }


def test_config_reload_recovers_invalid_agents_with_canonical_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["agents"] = "invalid"
    config_path = tmp_path / "config.json"
    original = json.dumps(payload)
    config_path.write_text(original, encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.show_duration is True
    assert loaded.agents.opencode.enabled is True
    assert loaded.agents.claude.enabled is True
    assert loaded.agents.codex.enabled is False
    assert loaded.agents.avault.cli_path == "avault"
    assert loaded.load_warnings and "agents" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == original


def test_config_reload_recovers_invalid_json_with_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_text('{"mode": ', encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.mode == "self_host"
    assert loaded.platform == "avibe"
    assert loaded.load_warnings and "JSON" in loaded.load_warnings[0]
    assert config_path.read_text(encoding="utf-8") == '{"mode": '
    assert list(config_path.parent.glob("config.json.bak-invalid-json-*"))


def test_config_reload_recovers_invalid_encoding_with_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b"\xff\xfe")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.mode == "self_host"
    assert loaded.load_warnings and "UTF-8" in loaded.load_warnings[0]
    assert config_path.read_bytes() == b"\xff\xfe"
    assert list(config_path.parent.glob("config.json.bak-invalid-encoding-*"))


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


def test_config_reload_drops_valid_retired_consent_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    source = copy.deepcopy(_schema("source.schema.json")["examples"][1])
    source["experimental_consent_at"] = "2026-07-23T03:00:00Z"
    payload["model_hub"]["sources"] = [source]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)

    assert loaded.load_warnings == ()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "experimental_consent_at" not in persisted["model_hub"]["sources"][0]


def test_final_config_rejects_retired_global_priority_key():
    payload = ModelHubConfig().to_payload()
    payload["priority_order"] = {"legacy": "shape-does-not-matter"}

    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(payload)


def test_model_hub_enabled_defaults_off_for_older_configs():
    payload = ModelHubConfig().to_payload()
    payload.pop("enabled")

    loaded = ModelHubConfig.from_payload(payload)

    assert loaded.enabled is False
    assert loaded.to_payload()["enabled"] is False


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_model_hub_enabled_requires_a_boolean(value):
    payload = ModelHubConfig().to_payload()
    payload["enabled"] = value

    with pytest.raises(ValueError, match="model_hub.enabled.*boolean"):
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
    source = ModelHubSourceConfig.from_payload(_schema("source.schema.json")["examples"][0])
    source.models.append(ModelHubModelConfig(id="claude-opus-4-5", provenance="manual"))
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
    # Two hops that only become one pair once spelling is settled are a legacy
    # file naming one upstream model twice, not a malformed payload: the second
    # was already unreachable past the first, so it collapses and the chain loads.
    # Raising here would instead fail a load the persisted-shape rule requires
    # to succeed.
    collapsed = ModelHubRouteConfig.from_payload(
        {
            "hops": [
                {"source_id": "src_same0001", "model_id": "model-a"},
                {"source_id": "src_same0001", "model_id": " model-a "},
            ]
        },
        repairing=True,
    )
    assert [(hop.source_id, hop.model_id) for hop in collapsed.hops] == [
        ("src_same0001", "model-a"),
    ]


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


def test_persisted_hub_config_requires_sparse_override_object():
    payload = ModelHubConfig().to_payload()

    missing = json.loads(json.dumps(payload))
    del missing["agents"]["claude"]["routes"]
    with pytest.raises(ValueError, match="routes.*required"):
        ModelHubConfig.from_payload(missing)

    non_object = json.loads(json.dumps(payload))
    non_object["agents"]["claude"]["routes"] = []
    with pytest.raises(ValueError, match="routes.*object"):
        ModelHubConfig.from_payload(non_object)

    sparse = ModelHubConfig.from_payload(payload)
    assert sparse.agents["claude"].routes == {}

    extra = json.loads(json.dumps(payload))
    extra["agents"]["claude"]["routes"]["claude-hidden-model"] = {"hops": []}
    with pytest.raises(ValueError, match="contains non-menu model"):
        ModelHubConfig.from_payload(extra)

    dynamic = json.loads(json.dumps(payload))
    dynamic["agents"]["opencode"]["mode"] = "hub"
    dynamic["agents"]["opencode"]["menu"] = {
        "view": "featured",
        "checked": ["model"],
    }
    dynamic["agents"]["opencode"]["models"] = [
        {
            "id": "model",
            "native_protocol": "openai_responses",
            "display_name": None,
            "origin": "manual",
            "models_dev_id": None,
            "context_window": None,
            "max_output_tokens": None,
            "input_modalities": [],
            "output_modalities": [],
            "supports_tools": None,
            "supports_reasoning": None,
            "reasoning_efforts": [],
        }
    ]
    dynamic["agents"]["opencode"]["routes"] = {}
    assert ModelHubConfig.from_payload(dynamic).agents["opencode"].routes == {}

    dormant = json.loads(json.dumps(payload))
    dormant["agents"]["opencode"]["routes"]["dormant-model"] = {"hops": []}
    restored = ModelHubConfig.from_payload(dormant)
    assert restored.agents["opencode"].models == []
    assert restored.agents["opencode"].routes == {}


def test_backend_model_modalities_match_the_contract_directions():
    input_pdf = ModelHubBackendModelConfig.from_payload({"id": "custom-model", "input_modalities": ["pdf"]})
    assert input_pdf.input_modalities == ["pdf"]

    with pytest.raises(ValueError, match="output_modalities"):
        ModelHubBackendModelConfig.from_payload({"id": "custom-model", "output_modalities": ["pdf"]})


def test_config_reload_preserves_legacy_routes_without_seeding_missing_models(tmp_path):
    payload = api.config_to_payload(default_config())
    claude = payload["model_hub"]["agents"]["claude"]
    original_ids = tuple(model["id"] for model in claude["models"])
    claude["routes"] = {model_id: {"hops": []} for model_id in original_ids}
    removed_id = original_ids[0]
    stale_id = "claude-catalog-removed-after-save"
    routes = claude["routes"]
    routes.pop(removed_id)
    routes[stale_id] = {"hops": []}
    claude.pop("models")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = V2Config.load(config_path=config_path)
    migrated_routes = loaded.model_hub.agents["claude"].routes
    migrated_models = loaded.model_hub.agents["claude"].models

    assert migrated_routes == {}
    assert removed_id not in migrated_routes
    assert {*original_ids, stale_id} <= {model.id for model in migrated_models}
    assert next(model for model in migrated_models if model.id == stale_id).origin == "manual"


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_v2_empty_route_normalizes_on_load_without_writing_until_save(tmp_path, backend):
    from tests.test_model_hub_routing_modes import MODEL, _loaded_catalog_config
    from tests.test_model_hub_resolution import _source

    payload = api.config_to_payload(default_config())
    payload["model_hub"] = _loaded_catalog_config(backend, MODEL, _source("src_v2empty01", (MODEL,))).to_payload()
    payload["model_hub"]["agents"][backend]["routes"][MODEL] = {"hops": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    loaded = V2Config.load(config_path=path)
    assert loaded.load_warnings == ()
    assert loaded.model_hub.agents[backend].routes == {}
    assert path.read_bytes() == before
    assert list(tmp_path.glob("config.json.bak-*")) == []
    loaded.save(config_path=path)
    expected = copy.deepcopy(payload["model_hub"])
    expected["agents"][backend]["routes"] = {}
    assert json.loads(path.read_text())["model_hub"] == expected


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("bad_key", ["", "authorization=sk-unsafe-fixture"])
def test_invalid_empty_route_key_keeps_v2_recovery_fence(tmp_path, backend, bad_key):
    payload = api.config_to_payload(default_config())
    payload["model_hub"]["agents"][backend]["routes"][bad_key] = {"hops": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    loaded = V2Config.load(config_path=path)
    assert loaded.load_warnings
    assert "model_hub" in loaded.recovered_sections
    assert loaded.model_hub.sources == []
    assert all(agent.mode == "direct" for agent in loaded.model_hub.agents.values())
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path=path)


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
