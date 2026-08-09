from __future__ import annotations

import copy
import json
import re
from dataclasses import fields
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
from scripts.check_model_hub_ui_states import ROOT, SPEC
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
    comparator's three structural rules — or `arm`, for a class-logic case that
    predates them and still has to keep working.
    """

    cls: str
    universe: str | None
    rule: str
    label: str
    before: str
    after: str
    says: str


# The three rules, restated as what a mutation has to show:
#
#   token      — a *near miss*: a name a substring, prefix or suffix matcher
#                would have credited. This is the one that regressed twice.
#                A cell filled with one direction is not filled: prefix and
#                suffix fail differently, and set intersection catches the
#                prefix for free while an unbounded extraction reads the suffix
#                as a hit. Every direction the extraction admits needs a case,
#                or the grid reads full over a rule that was never exercised.
#   empty      — a *total miss*: nothing resolves, and the gate reports it
#                instead of skipping the comparison in silence.
#   duplicate  — one canonical token declared twice with different content.
#
# Filed by (class, universe, rule) rather than by bug, because the defect this
# suite exists to prevent is not any single bug: it is a new class arriving with
# a comparison of its own and nobody noticing which rules it forgot.
GATE_MUTATIONS: tuple[GateCase, ...] = (
    # --- A: routes, states, treatments ------------------------------------
    GateCase(
        "A", "routes", "token",
        "register row drifts onto a longer route",
        "F4 — `POST /api/models/oauth/cancel` is issued as the dialog",
        "F4 — `POST /api/models/oauth/cancellation` is issued as the dialog",
        "is named by no §0.8 row",
    ),
    GateCase(
        "A", "states", "token",
        "an exit points at a prefix of a real state",
        "| → Unreachable | — | Payload arrives → Ready |",
        "| → Unreach | — | Payload arrives → Ready |",
        "names no F1–F5 and no known state",
    ),
    GateCase(
        "A", "states", "empty",
        "an exit points at a state nobody wrote",
        "| → Unreachable | — | Payload arrives → Ready |",
        "| → Nowhere at all | — | Payload arrives → Ready |",
        "names no F1–F5 and no known state",
    ),
    GateCase(
        "A", "states", "duplicate",
        "two rows in one frame under one state name",
        "| §1.0 | Impaired |",
        "| §1.0 | Ready |",
        "`1.0 · Ready` is defined twice in states",
    ),
    GateCase(
        "A", "treatments", "token",
        "the treatment a cell names becomes a prefix of the one defined",
        "| F1 | Retry in place |",
        "| F10 | Retry in place |",
        "names F1, which §0.8's closed set does not define",
    ),
    GateCase(
        "A", "treatments", "empty",
        "treatment misnamed",
        "| F4 — `POST /api/models/oauth",
        "| F9 — `POST /api/models/oauth",
        "which §0.8's closed set does not define",
    ),
    GateCase(
        "A", "treatments", "duplicate",
        "§0.8's closed set defines one number twice",
        "| F2 | Keep the last good result |",
        "| F1 | Keep the last good result |",
        "`F1` is defined twice in treatments",
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
    ),
    # `[contract-gap]` is also the marker that tells a checker to stop asking.
    # Pointed at a number no §0.5 row defines, it must silence nothing at all.
    GateCase(
        "A", "gaps", "empty",
        "gap marker cites an unregistered number",
        "`[contract]` `[contract-gap]` G-15 carries",
        "`[contract]` `[contract-gap]` G-99 carries",
        "is named by no §0.8 row",
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
    ),
    # --- B: copy, slots -----------------------------------------------------
    GateCase(
        "B", "copy", "token",
        "a citation truncated to a prefix two keys share",
        "`shell.notStarted` | Run pill",
        "`shell.not` | Run pill",
        "key `shell.not` is cited and never defined",
    ),
    GateCase(
        "B", "copy", "empty",
        "key cited, never defined",
        "`shell.notStarted` | Run pill",
        "`shell.notStartd` | Run pill",
        "is cited and never defined",
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
    ),
    GateCase(
        "B", "slots", "token",
        "a declared slot truncated to a prefix of the one strings use",
        "| `{{host}}` | The source's host",
        "| `{{hos}}` | The source's host",
        "interpolates `{{host}}` with no §0.9 row",
    ),
    GateCase(
        "B", "slots", "empty",
        "undeclared slot",
        "| `install.retry` `[derived]` | 重试 | Try again |",
        "| `install.retry` `[derived]` | 重试 {{attempt}} | Try again {{attempt}} |",
        "with no §0.9 row",
    ),
    GateCase(
        "B", "slots", "duplicate",
        "§0.9 declares one slot twice",
        "| `{{source}}` | A source's display name. | Always present |",
        "| `{{host}}` | A source's display name. | Always present |",
        "`host` is defined twice in slots",
    ),
    # A key that would ship with no English string.
    GateCase(
        "B", None, "arm",
        "English column dropped",
        "| `install.progress` `[derived]` | 正在安装… | Installing… |",
        "| `install.progress` `[derived]` | 正在安装… |  |",
        "has no English column",
    ),
    # --- C: frames ----------------------------------------------------------
    GateCase(
        "C", "frames", "token",
        "a register row filed under a prefix of a real section",
        "| §1.0 | Impaired |",
        "| §1 | Impaired |",
        "filed under §1, which is no §1 section",
    ),
    GateCase(
        "C", "frames", "empty",
        "a register row filed under a section that does not exist",
        "| §1.0 | Impaired |",
        "| §1.60 | Impaired |",
        "filed under §1.60, which is no §1 section",
    ),
    GateCase(
        "C", "frames", "duplicate",
        "two §1 headings claim one number",
        "### 1.9 Frame 10 `g7MOA4`",
        "### 1.8 Frame 10 `g7MOA4`",
        "`1.8` is defined twice in frames",
    ),
    # A state a user can enter and not leave.
    GateCase(
        "C", None, "arm",
        "exit removed",
        "| 取消 / 关闭 / Escape → close, discarding uncommitted moves; 保存顺序 → Saving |",
        "| — |",
        "has no exit",
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
    ),
    GateCase(
        "D", "copy", "empty",
        "the row a live citation names is renamed out from under it",
        "| `fail.tier` `[derived]` | 档位没保存上",
        "| `fail.tierZ` `[derived]` | 档位没保存上",
        "condition key `fail.tierZ` is cited by no §0.8 row",
    ),
    # --- E: routes, schema files, schema fields, repo symbols ---------------
    GateCase(
        "E", "routes", "token",
        "a route extended past a real one by one segment",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`",
        "`PUT /api/models/agents/<backend>/sources/order` with `{order: string[]}`",
        "is contracted by no `api.md` route row",
    ),
    # A route the spec names that the contract does not have. This is the class
    # the round-10 review found by hand, six times.
    GateCase(
        "E", "routes", "empty",
        "route literal drifts off api.md",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`",
        "`PUT /api/models/agents/<backend>/source-order` with `{order: string[]}`",
        "is contracted by no `api.md` route row",
    ),
    GateCase(
        "E", "schema files", "token",
        "a schema citation extended past a real filename",
        "`source.schema.json` pins it non-null",
        "`agent-source.schema.json` pins it non-null",
        "`agent-source.schema.json` is not a file in",
    ),
    GateCase(
        "E", "schema files", "empty",
        "a schema citation naming no file",
        "`source.schema.json` pins it non-null",
        "`sources.schema.json` pins it non-null",
        "`sources.schema.json` is not a file in",
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
    ),
    GateCase(
        "E", "repo symbols", "token",
        "a cited symbol truncated to a prefix of the real one",
        "service.py:list_agents",
        "service.py:list_agent",
        "defines no `list_agent`",
    ),
    GateCase(
        "E", "repo symbols", "empty",
        "cited symbol no longer exists",
        "service.py:list_agents",
        "service.py:list_agent_rows",
        "defines no `list_agent_rows`",
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
        "| G-19 | 05 add-by-key, an unrelated surface | a different missing behaviour "
        "| Contradicting evidence. | pending |\n"
        "| G-19 | 05 add-by-key, 取消 pressed while a persisting add is in flight |",
        "is defined twice in gaps with different content",
    ),
    # A row that stops being a registration stops excusing its own claim: G-9's
    # row states a 409 branch for a route `api.md` does not guard, which is the
    # gap it registers, and which E reports the moment the row is not one.
    GateCase(
        "E", "gaps", "empty",
        "a gap row loses the number that makes it a registration",
        "| G-9 | 03 order save that drops sources |",
        "| gap 9 | 03 order save that drops sources |",
        "a 409 branch is claimed for",
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
    ),
    # A total rendering of a contracted vocabulary that quietly drops a row. The
    # author cannot see the schema while writing the table, so nothing but a set
    # comparison catches this.
    GateCase(
        "E", "schema fields", "arm",
        "mapping table drops a contracted value",
        "| `waiting` | 网关 · 等待重试 |",
        "| `wating` | 网关 · 等待重试 |",
        "renders",
    ),
    # The right route and the wrong body: `{hops}` belongs to the per-model
    # chain save, and sending it here was a real finding.
    GateCase(
        "E", "routes", "arm",
        "body key belongs to another route",
        "`PUT /api/models/agents/<backend>/sources` with `{order: string[]}`",
        "`PUT /api/models/agents/<backend>/sources` with `{hops: string[]}`",
        "not contracted for",
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
    ),
)


# A cell with no case, and the reason it has none. An exemption is written here
# or the tiling test fails; it is never an omission nobody had to justify.
UNREACHABLE_BY_CLASS: dict[tuple[str, str], str] = {
    ("D", "duplicate"): (
        "class D defines nothing. It reads the copy universe class B fills, and a "
        "copy key declared twice is reported once, by B — the (B, copy, duplicate) case."
    ),
}

UNREACHABLE_BY_UNIVERSE: dict[tuple[str, str], str] = {
    (name, "duplicate"): (
        f"`{name}` is built from the frozen contract files, which this harness must not "
        "edit. Its duplicate rule is proved directly instead, by "
        "test_authority_side_universes_report_a_duplicate_definition."
    )
    for name in ("routes", "schema files", "schema fields", "repo symbols")
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
}


@pytest.mark.parametrize("case", GATE_MUTATIONS, ids=lambda c: f"{c.cls}/{c.rule}/{c.label}")
def test_model_hub_ui_state_gate_fails_on_a_reintroduced_defect(tmp_path, case: GateCase):
    """A gate nobody has watched fail is a gate that reports green.

    Each case reintroduces one defect into the live spec and asserts the checker
    names it — the right class *and* the right sentence. Class alone is not
    enough: a mutation that trips some other arm of the same class would pass
    while the rule it was written for stayed dead.
    """
    spec = Path("docs/plans/model-hub-ui-spec.md").read_text(encoding="utf-8")
    assert spec.count(case.before) == 1, f"{case.label}: anchor no longer unique in the spec"

    mutated = tmp_path / "mutated.md"
    mutated.write_text(spec.replace(case.before, case.after, 1), encoding="utf-8")
    # A document written to a temporary directory is in no checkout, so it has no
    # authorities of its own. Every case here mutates the spec side, and the
    # authorities it is checked against are this repository's — said out loud
    # because a borrowed authority is the one thing a green result may not hide.
    result = check_model_hub_ui_states(mutated, authorities=ROOT)
    assert result["authority_origin"] == "this checkout"

    assert not result["ok"], f"{case.label}: the gate did not notice"
    said = [f["message"] for f in result["findings"] if f["class"] == case.cls]
    assert any(case.says in m for m in said), (case.label, case.says, result["findings"])


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
    from scripts.check_model_hub_ui_states import Origin, Universe, load_authorities

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


# A section no scope declares, so "outside every range" has somewhere to be. The
# tiling test asserts that, rather than trusting this comment.
OUTSIDE_SECTION = "0.7"

# §0.4's row excusing a route no frame draws. The `scope note` trap deletes it and
# puts it back in two places.
_SCOPE_NOTE_ROW = (
    "| `POST /api/models/migration/scan` | The migration surface. Neither of these ten "
    "frames offers an import, and a scan with nothing to show it is not a screen |"
)

SCOPE_TRAPS: tuple[ScopeTrap, ...] = (
    ScopeTrap(
        "register", "collect", "a state row",
        ("", ""),
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
    ),
    ScopeTrap(
        "scope note", "excuse", "the row putting a contracted route on another surface",
        (_SCOPE_NOTE_ROW + "\n", ""),
        _SCOPE_NOTE_ROW,
        "POST /api/models/migration/scan is contracted and reached by no §0.8 row",
    ),
)

# A scope with no trap, and why it cannot have one.
UNTRAPPED_SCOPES: dict[str, str] = {
    "claims": (
        "declared `*` on purpose — a restated authority is wrong wherever it is written — "
        "so there is no outside to place a decoy in. What binds this one is the declaration "
        "itself, asserted by test_every_arm_declares_a_range_the_module_declares."
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
    if before:
        assert spec.count(before) == 1, f"{trap.label}: setup anchor is not unique"
        spec = spec.replace(before, after, 1)
    where = SCOPES[trap.scope].where

    inside = _checked(tmp_path, _place(spec, where, trap.decoy), "inside")
    outside = _checked(tmp_path, _place(spec, OUTSIDE_SECTION, trap.decoy), "outside")
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
        "F4 — `POST /api/models/oauth/cancel` is issued as the dialog",
        "F4 — `POST /api/models/oauth/cancellation` is issued as the dialog",
        "POST /api/models/oauth/cancel is named by no §0.8 row",
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
