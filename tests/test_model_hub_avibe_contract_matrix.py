"""MH-AVIBE-004: schemas cannot certify a transport Avibe cannot use.

Positive Hub payloads miss illegal discriminator/channel combinations. The
producer-owned audit table drives refusals and must cover newly declared shapes.
"""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator, ValidationError
from referencing import Registry, Resource

from config.v2_config import ModelHubRouteConfig, ModelHubRouteHopConfig
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_avibe_consumer import CONTRACTS, _avibe_service, _source


MATRIX = json.loads((CONTRACTS / "avibe-boundary-matrix.json").read_text())["boundaries"]


def _schema(row):
    resources = {}
    for path in CONTRACTS.glob("*.schema.json"):
        value = json.loads(path.read_text())
        resources[value["$id"]] = Resource.from_contents(value)
    root = json.loads((CONTRACTS / row["schema"]).read_text())
    return Draft7Validator(
        {"$ref": root["$id"] + "#" + row["pointer"]},
        registry=Registry().with_resources(resources.items()),
    )


def _fixture(kind):
    identity = {
        "source_id": "src_fixture01", "configured_model_id": "fixture-model", "channel": "hub",
        "origin": {"provider": "anthropic", "api": "anthropic", "model": "fixture-model"},
    }
    if kind == "supply":
        return {"backend": "avibe", "mode": "hub", "menu_kind": "fixed", "cli_present": False}
    if kind == "catalog":
        return {"backend": "avibe", "mode": "hub", "catalog_models": []}
    if kind == "probe":
        # Start from a valid native response so no Hub latency constraint can
        # accidentally reject the Avibe/native pairing for an unrelated cause.
        return {
            "contract_version": 12, "backend": "claude", "channel": "native_cli", "reachable": True,
            "source_id": "src_fixture01", "model_id": "fixture-model", "latency_ms": None,
            "error": None,
        }
    if kind == "chain":
        return {
            "contract_version": 12, "backend": "avibe", "model_id": "fixture-model",
            "manual_override": None, "route_origin": "automatic",
            "chain": [{"source_id": "src_fixture01", "model_id": "fixture-model", "channel": "hub",
                       "health": "healthy", "runnable": True, "reason": None, "retry_at": None}],
            "current": {"source_id": "src_fixture01", "model_id": "fixture-model"}, "supply_state": "ok",
        }
    if kind == "migration":
        return {
            "id": "fixture", "backend": "claude", "kind": "api_key", "masked_detail": "fixture",
            "proposed_action": "import", "selected": False,
        }
    return {
        "contract_version": 12, "turn_id": "turn-fixture", "ts": "2026-10-02T07:00:00Z",
        "agent": "avibe", "requested_model_id": "fixture-model", "outcome": kind,
        "failed_attempts": [{**identity, "reason": "rate_limited"}] if kind == "exhausted" else [],
        "served": identity if kind == "served" else None,
        "terminal_error": (
            {**identity, "reason": "protocol_error", "stream_started": False}
            if kind == "failed_terminal" else None
        ),
        "canceled_attempt": identity if kind == "canceled" else None,
        "model_supply_state": None, "blockers": [],
    }


def test_avibe_matrix_covers_every_declared_backend_shape():
    def declared(node, pointer=""):
        if not isinstance(node, dict):
            return
        for field in ("backend", "agent"):
            enum = node.get("properties", {}).get(field, {}).get("enum", [])
            if {"claude", "codex", "opencode", "avibe"} & set(enum):
                yield pointer, field
        # Traverse shape definitions, not if/then discriminator constraints.
        for field in ("properties", "definitions", "$defs"):
            for name, child in node.get(field, {}).items():
                yield from declared(child, f"{pointer}/{field}/{name}")
        if "items" in node:
            yield from declared(node["items"], f"{pointer}/items")
        for field in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(node.get(field, [])):
                yield from declared(child, f"{pointer}/{field}/{index}")

    sites = {
        (path.name, pointer, field)
        for path in CONTRACTS.glob("*.schema.json")
        for pointer, field in declared(json.loads(path.read_text()))
    }
    assert {(row["schema"], row["pointer"], row["backend"]) for row in MATRIX} == sites
    assert len(MATRIX) == len(sites)
    for row in MATRIX:
        assert row["policy"] in {"hub-only", "native-only", "reference"}
        assert row["runtime"]
        assert bool(row.get("refusals")) == (row["policy"] != "reference")


@pytest.mark.parametrize(
    ("row", "refusal"),
    [(row, refusal) for row in MATRIX for refusal in row.get("refusals", [])],
    ids=[
        f'{row["schema"]}:{refusal["fixture"]}:{refusal["path"]}'
        for row in MATRIX for refusal in row.get("refusals", [])
    ],
)
def test_avibe_channel_matrix_rejects_impossible_shapes_without_changing_native(row, refusal):
    validator = _schema(row)
    payload = _fixture(refusal["fixture"])
    validator.validate(payload)
    invalid = copy.deepcopy(payload)
    target = invalid
    for key in refusal["path"][:-1]:
        target = target[key]
    target[refusal["path"][-1]] = refusal["value"]
    with pytest.raises(ValidationError):
        validator.validate(invalid)
    if row["policy"] != "native-only":
        invalid[row["backend"]] = "claude"
        validator.validate(invalid)


def test_avibe_local_terminal_and_pre_admission_cancel_remain_nullable():
    row = next(row for row in MATRIX if row["schema"] == "turn-provenance.schema.json")
    validator = _schema(row)
    payload = _fixture("failed_terminal")
    payload["terminal_error"] = {
        "source_id": None, "configured_model_id": None, "channel": None,
        "reason": "engine_down", "stream_started": False,
    }
    validator.validate(payload)
    payload = _fixture("canceled")
    payload["canceled_attempt"] = None
    validator.validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [case for row in MATRIX for case in row.get("runtime_cases", [])])
async def test_avibe_runtime_matrix_refuses_native_admission(tmp_path, case):
    """Admission, not just serialization, must refuse impossible transports."""
    native = _source("src_native001", "Native", channel="native_cli", vendor="anthropic", protocol="anthropic")
    service = _avibe_service(tmp_path, [native])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    route = ModelHubRouteConfig((ModelHubRouteHopConfig(native.id, "shared-model"),))
    try:
        if case in {"native_attempt", "attempt_channel"}:
            # The recording boundary must not let an accidental future caller
            # manufacture native Avibe provenance after normal routing refused.
            if case == "native_attempt":
                with pytest.raises(ValueError, match="avibe.*hub"):
                    gateway.correlation.begin_native_attempt(
                        backend="avibe", process_scope="avibe:matrix", turn_id="turn-matrix",
                        requested_model_id="menu-alias", source_id=native.id,
                        resolved_model_id="shared-model", via_mapping=False,
                    )
            else:
                await gateway.endpoint(
                    "avibe", process_scope="avibe:matrix", turn_id="turn-matrix",
                    requested_model_id="menu-alias", source_id=native.id, resolved_model_id="shared-model",
                )
                with pytest.raises(ValueError, match="avibe.*hub"):
                    gateway.correlation.begin_attempt(
                        "turn-matrix", source_id=native.id, resolved_model_id="shared-model",
                        channel="native_cli", via_mapping=False,
                    )
        else:
            with pytest.raises(ModelHubError) as refusal:
                if case == "mode":
                    await service.set_agent_mode("avibe", "direct")
                elif case == "source_order":
                    await service.set_agent_sources("avibe", {"order": [native.id]})
                elif case == "route_write":
                    await service.set_agent_chain("avibe", "menu-alias", route.to_payload())
                else:
                    # Corrupt in-process route still cannot bypass live
                    # admission (persisted config rejects it on load).
                    service.store.config.agents["avibe"].routes["menu-alias"] = route
                    if case == "probe":
                        await service.probe_agent("avibe", "menu-alias")
                    elif case == "resolve_hop":
                        await router.resolve_hop("menu-alias", process_scope="avibe:matrix")
                    elif case in {"resolve", "recovery"}:
                        service.recovery.window_seconds = 120 if case == "recovery" else 0
                        await service.resolve_with_recovery(
                            backend="avibe", model_id="menu-alias", request={}, stream=False,
                        )
                    else:
                        raise AssertionError(f"Unhandled runtime boundary: {case}")
            assert refusal.value.code in {
                "mode_switch_blocked", "invalid_source_order", "mapping_target_unavailable",
                "probe_no_candidate", "no_candidate",
            }
        assert service.adapter.invocations == []
    finally:
        await gateway.close()
