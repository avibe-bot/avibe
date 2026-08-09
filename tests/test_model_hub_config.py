from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker, ValidationError

from config.v2_config import (
    MODEL_HUB_ENABLED_ENV,
    MODEL_HUB_LEGACY_CREATED_AT,
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
    is_model_hub_enabled,
)
from core.services.settings import default_config
from scripts.check_model_hub_authorities import check as check_model_hub_authorities
from scripts.check_model_hub_ui_states import check as check_model_hub_ui_states
from vibe import api

CONTRACTS = Path("docs/plans/model-hub-contracts")


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_valid(name: str, payload: dict) -> None:
    errors = sorted(
        Draft7Validator(_schema(name), format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


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

    for example in _schema("agent-supply.schema.json")["examples"]:
        agent = ModelHubAgentSupplyConfig.from_payload(example)
        # `builtin_models` and `standard_vendors` are read-only endpoint
        # projections (v1.2), not persisted config — reconstruct them
        # the way `_agent_payload` merges them onto to_payload().
        serialized = {
            **agent.to_payload(),
            "builtin_models": example.get("builtin_models"),
            "standard_vendors": example.get("standard_vendors"),
        }
        # AgentSupply is the API projection; the dormant pre-I1 config object
        # still carries its old mapping list behind the default-off gate.
        serialized.pop("mappings", None)
        if "sources" not in example:
            serialized.pop("sources")
        else:
            serialized["sources"]["eligibility"] = example["sources"].get("eligibility")
        assert _canonical(serialized) == _canonical(example)
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
    assert result["ok"], result["findings"]


@pytest.mark.parametrize(
    "label,before,after,expect",
    [
        # The defect this gate was built for: a state issues a call and no row
        # states what happens when it fails. This is the shape that survived
        # review at f22c2a59 and had to be found by a human.
        (
            "treatment removed",
            "F4 — `POST /api/models/oauth/cancel` is issued as the dialog",
            "—",
            "A",
        ),
        # The same row, treatment misnamed rather than missing.
        ("treatment misnamed", "| F4 — `POST /api/models/oauth", "| F9 — `POST /api/models/oauth", "A"),
        # A register row pointing at copy nobody wrote.
        ("key cited, never defined", "`shell.notStarted` | Run pill", "`shell.notStartd` | Run pill", "B"),
        # A key that would ship with no English string.
        (
            "English column dropped",
            "| `install.progress` `[derived]` | 正在安装… | Installing… |",
            "| `install.progress` `[derived]` | 正在安装… |  |",
            "B",
        ),
        # A slot promised by a string with nothing declaring what fills it.
        (
            "undeclared slot",
            "| `install.retry` `[derived]` | 重试 | Try again |",
            "| `install.retry` `[derived]` | 重试 {{attempt}} | Try again {{attempt}} |",
            "B",
        ),
        # A state a user can enter and not leave.
        (
            "exit removed",
            "| 取消 / 关闭 / Escape → close, discarding uncommitted moves; 保存顺序 → Saving |",
            "| — |",
            "C",
        ),
        # The substring comparison this gate used to do answered yes for every
        # prefix, so a register row could drift onto a route that does not exist
        # and still vouch for the real one. Exact tokens are what closes it.
        (
            "register row drifts onto a longer route",
            "F4 — `POST /api/models/oauth/cancel` is issued as the dialog",
            "F4 — `POST /api/models/oauth/cancellation` is issued as the dialog",
            "A",
        ),
        # A route the spec names that the contract does not have. This is the
        # class the round-10 review found by hand, six times.
        (
            "route literal drifts off api.md",
            "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`",
            "`PUT /api/models/agents/<backend>/source-order` with `{order: string[]}`",
            "E",
        ),
        # The same sentence, right route and wrong body: `{hops}` belongs to the
        # per-model chain save, and sending it here was a real finding.
        (
            "body key belongs to another route",
            "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`",
            "`PUT /api/models/agents/<backend>/sources` with `{hops: string[]}`",
            "E",
        ),
        # A total rendering of a contracted vocabulary that quietly drops a row.
        # The author cannot see the schema while writing the table, so nothing
        # but a set comparison catches this.
        (
            "mapping table drops a contracted value",
            "| `waiting` | 网关 · 等待重试 |",
            "| `wating` | 网关 · 等待重试 |",
            "E",
        ),
        # The same table, no longer saying which of two same-named declarations
        # it renders. `supply_status` exists per backend and per named Agent,
        # and reading one as the other was the substitution round 10 opened on.
        (
            "mapping table stops naming which declaration it renders",
            "| `AgentSupply.supply_status` `[contract]` | Subtitle | Key |",
            "| `supply_status` `[contract]` | Subtitle | Key |",
            "E",
        ),
        # A repo symbol cited as evidence that no longer exists at that path.
        ("cited symbol no longer exists", "service.py:list_agents", "service.py:list_agent_rows", "E"),
        # A section that answers its other failures for both origins, with one
        # failure left written once — the reader cannot tell whether pulling
        # from a stopped engine fails the way adding does.
        ("origin half deleted", "| ⑥′ Engine unavailable", "| ⑥″ Engine unavailable", "C"),
        # `[contract-gap]` is also the marker that tells a checker to stop
        # asking. Pointed at a number no §0.5 row defines, it must silence
        # nothing at all.
        (
            "gap marker cites an unregistered number",
            "`[contract]` `[contract-gap]` G-15 carries",
            "`[contract]` `[contract-gap]` G-99 carries",
            "A",
        ),
        # Round 11. `api.md` puts request and response in one cell, and a check
        # that unions the two sides accepts the answer's vocabulary as a legal
        # request body. Dropping the word that introduces this body as an answer
        # must move it to the request side and fail there.
        (
            "a response body written as the request",
            "and returns `{agent: AgentSupply}`",
            "and sends `{agent: AgentSupply}`",
            "E",
        ),
        # Round 11. A `[contract]` header asserts that some schema owns the
        # vocabulary below it. When the field name resolves to no declaration,
        # the set comparison has nothing to compare and used to skip in silence
        # — the one outcome a gate that exists to catch drift may not have.
        (
            "mapping table names a field no schema declares",
            "| `AgentSupply.supply_status` `[contract]` | Subtitle | Key |",
            "| `AgentSupply.supply_stat` `[contract]` | Subtitle | Key |",
            "E",
        ),
        # Round 11. Condition keys are stored bare and cited namespace-qualified,
        # so the citation test has to resolve both spellings to one copy row.
        # Matching on prefixes instead let a citation drift onto a longer key and
        # still vouch for the row it left behind.
        (
            "a citation drifts onto a longer key",
            "`sourceDetail.fail.tier`, `sourceDetail.retry`",
            "`sourceDetail.fail.tiers`, `sourceDetail.retry`",
            "D",
        ),
        # Round 11. Two rows in one table under one key ship whichever the
        # loader read last. The key is legitimately re-used across namespaces —
        # every table has a `title` — so only the qualified spelling collides.
        (
            "one qualified key defined twice",
            "| `tiers.addFirst` | + 添加档位 | + Add tier |",
            "| `tiers.add` | + 添加档位 | + Add tier |",
            "B",
        ),
        # Round 11. The mirror of class A, and the generator behind five
        # findings across two heads: a contracted mutation no surface reaches.
        # Out of scope is a legitimate answer; not saying anything is not.
        (
            "a contracted mutation stops being accounted for",
            "| `POST /api/models/migration/scan` | The migration surface.",
            "| `POST /api/models/migration/scans` | The migration surface.",
            "A",
        ),
    ],
)
def test_model_hub_ui_state_gate_fails_on_a_reintroduced_defect(tmp_path, label, before, after, expect):
    """A gate nobody has watched fail is a gate that reports green.

    Each case reintroduces one defect class into the live spec and asserts the
    checker names it. Without this, `ok is True` above proves only that the
    script ran.
    """
    spec = Path("docs/plans/model-hub-ui-spec.md").read_text(encoding="utf-8")
    assert spec.count(before) == 1, f"{label}: anchor no longer unique in the spec"

    mutated = tmp_path / "mutated.md"
    mutated.write_text(spec.replace(before, after, 1), encoding="utf-8")
    result = check_model_hub_ui_states(mutated)

    assert not result["ok"], f"{label}: the gate did not notice"
    assert expect in {f["class"] for f in result["findings"]}, (label, result["findings"])


def test_model_hub_ui_gate_target_zero_classes_prove_their_own_zero():
    """A class whose right answer is 0 cannot read 0 as evidence of anything.

    Every other inventory gets a free liveness signal: empty means the extractor
    broke. The restatement classes give that up by design — the document is
    meant to hold none of them — so they carry fixtures instead, one that must
    still be caught and one that must still pass. This asserts the gate refuses
    to report a self-tested zero when the arm behind it has stopped working.
    """
    from scripts.check_model_hub_ui_states import ROOT, TARGET_ZERO, load_authorities, self_test

    assert TARGET_ZERO, "a target-zero class list nobody populates tests nothing"
    assert not self_test(load_authorities(ROOT), ROOT)

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
    native_alias = next(
        example
        for example in chain_schema["examples"]
        if example["model_id"] == "claude-opus-4-5"
        and example["chain"]
        and example["chain"][0]["resolved_model_id"] is not None
        and example["chain"][0]["via_mapping"] is False
    )
    chain_validator.validate(native_alias)
    invalid_native_alias = copy.deepcopy(native_alias)
    invalid_native_alias["chain"][0]["resolved_model_id"] = "glm-5.2"
    with pytest.raises(ValidationError):
        chain_validator.validate(invalid_native_alias)

    native_unavailable = copy.deepcopy(chain_schema["examples"][-1])
    chain_validator.validate(native_unavailable)

    unavailable_needs_action = copy.deepcopy(native_unavailable)
    unavailable_needs_action["chain"][0]["health"] = "needs_action"
    unavailable_needs_action["chain"][0]["retry_at"] = None
    chain_validator.validate(unavailable_needs_action)

    unmarked_healthy_unavailable = copy.deepcopy(chain_schema["examples"][-2])
    unmarked_healthy_unavailable["chain"][0]["reason"] = None
    with pytest.raises(ValidationError):
        chain_validator.validate(unmarked_healthy_unavailable)

    mislabeled_waiting = copy.deepcopy(native_unavailable)
    mislabeled_waiting["supply_state"] = "waiting"
    with pytest.raises(ValidationError):
        chain_validator.validate(mislabeled_waiting)

    unavailable_hub = copy.deepcopy(native_unavailable)
    unavailable_hub["chain"][0]["channel"] = "hub"
    with pytest.raises(ValidationError):
        chain_validator.validate(unavailable_hub)

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
        "experimental_consent_at": "2026-07-23T03:00:00Z",
        "credential_ref": "cred_serializer_test",
    }
    hub_payload = {
        "sources": [source_example],
        "agents": {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub").to_payload()
            for backend in ("claude", "codex", "opencode")
        },
        "subscription_hub_experimental": True,
    }
    config = default_config()
    config.model_hub = ModelHubConfig.from_payload(hub_payload)
    config.save()

    loaded = V2Config.load()
    disk_payload = json.loads(Path(tmp_path, "config", "config.json").read_text(encoding="utf-8"))
    api_payload = api.config_to_payload(loaded)
    expected_root = {field.name for field in fields(ModelHubConfig)}
    source_fields = {field.name for field in fields(ModelHubSourceConfig)}
    # Retired consent metadata remains an internal compatibility attribute for
    # in-memory fixtures but is intentionally absent from the final payload.
    source_fields.discard("experimental_consent_at")
    source_state_fields = {field.name for field in fields(ModelHubSourceStateConfig)}
    source_usage_fields = {field.name for field in fields(ModelHubSourceUsageConfig)}
    source_model_fields = {field.name for field in fields(ModelHubModelConfig)}
    source_model_fields.discard("provenance")
    source_model_fields.add("origin")
    agent_fields = {field.name for field in fields(ModelHubAgentSupplyConfig)}
    agent_sources_fields = {field.name for field in fields(ModelHubAgentSourcesConfig)}
    mapping_fields = {field.name for field in fields(ModelHubMappingConfig)}
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
        assert mapping_fields == set(ModelHubMappingConfig("builtin", "target", True).to_payload()), label
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


def test_hub_subscription_load_ignores_retired_consent_metadata():
    source = _schema("source.schema.json")["examples"][0]
    source = {**source, "supply_channel": "hub"}
    payload = {
        "sources": [source],
        "agents": {},
        "subscription_hub_experimental": True,
    }

    loaded = ModelHubConfig.from_payload(payload)
    assert loaded.sources[0].supply_channel == "hub"


def test_legacy_global_priority_key_is_dropped_without_validation():
    payload = ModelHubConfig().to_payload()
    payload["priority_order"] = {"legacy": "shape-does-not-matter"}

    loaded = ModelHubConfig.from_payload(payload)

    assert "priority_order" not in loaded.to_payload()


def test_agent_source_orders_validate_existence_eligibility_and_uniqueness():
    source = {
        **_schema("source.schema.json")["examples"][0],
        "created_at": "2026-07-29T01:00:00Z",
    }
    base = ModelHubConfig().to_payload()
    base["sources"] = [source]
    base["agents"]["claude"]["sources"] = {
        "policy": "custom",
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
        "policy": "custom",
        "order": [source["id"]],
    }
    with pytest.raises(ValueError):
        ModelHubConfig.from_payload(ineligible)


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
        experimental_consent_at=("2026-07-29T02:00:00Z" if kind == "subscription" and channel == "hub" else None),
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
        subscription_hub_experimental=True,
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
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]
    assert config.recommended_source_order("opencode") == [
        legacy_a.id,
        legacy_b.id,
        newer.id,
        same_time_a.id,
        same_time_b.id,
    ]


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
