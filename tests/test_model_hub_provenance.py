from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import UPSTREAM_MACHINE_ERROR_CODES, classify_outcome
from core.handlers.model_hub.identifiers import MODEL_ID_MAX_LENGTH
from core.handlers.model_hub.provenance import BoundedProvenanceStore, TurnCorrelationRegistry
from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
from core.handlers.model_hub.service import ModelHubError
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from tests.test_model_hub_resolution import _service
from tests.test_model_hub_routing_modes import MODEL, _loaded_catalog_config, _sparse_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import model_hub_client, ui_server


def _record(turn_id, *, backend="claude", model=MODEL, outcome="failed_terminal"):
    identity = {"source_id": "src_deleted01", "configured_model_id": "unknown-model", "channel": "hub"}
    return {
        "contract_version": 9,
        "turn_id": turn_id,
        "ts": "2026-09-06T00:00:00Z",
        "agent": backend,
        "requested_model_id": model,
        "outcome": outcome,
        "failed_attempts": [],
        "served": identity if outcome == "served" else None,
        "terminal_error": {**identity, "reason": "invalid_parameter", "stream_started": False}
        if outcome == "failed_terminal"
        else None,
        "canceled_attempt": None,
        "model_supply_state": None,
        "blockers": [],
    }


def test_latest_record_is_backend_model_isolated_bounded_and_not_latest_error(tmp_path):
    path = tmp_path / "provenance.json"
    store = BoundedProvenanceStore(path, max_entries=3)
    assert store.latest_for_model("claude", MODEL) is None
    assert not path.exists()
    first = _record("first")
    store.put(first)
    store.put(_record("other-backend", backend="codex"))
    store.put(_record("other-model", model="claude-other"))
    assert store.latest_for_model("claude", MODEL) == first
    before = path.read_bytes()
    assert BoundedProvenanceStore(path).latest_for_model("claude", MODEL) == first
    assert path.read_bytes() == before
    success = _record("later-success", outcome="served")
    success["ts"] = "2025-01-01T00:00:00Z"
    store.put(success)
    assert store.latest_for_model("claude", MODEL) == success
    assert store.get("first") is None
    assert len(json.loads(path.read_text())) == 3


@pytest.mark.parametrize("status,expected_status", [(404, 404), (True, None), (99, None), (600, None), (None, None)])
@pytest.mark.parametrize(
    "code,expected_code", [("model_not_found", "model_not_found"), ("arbitrary credential=secret", None), (None, None)]
)
def test_new_terminal_records_retain_only_safe_recognized_error_metadata(
    tmp_path, status, expected_status, code, expected_code
):
    store = BoundedProvenanceStore(tmp_path / "records.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "fixture", "turn-error")
    turn_id = registry.begin_gateway_request(backend="claude", token=token, requested_model_id=MODEL)
    registry.begin_attempt(
        turn_id, source_id="src_deleted01", resolved_model_id="unknown-model", channel="hub", via_mapping=False
    )
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.HTTP_ERROR,
        http_status=status,
        error_code=code,
        redacted_message="model not found; arbitrary upstream text",
        stream_started=False,
        model_id="unknown-model",
        source_id="src_deleted01",
    )
    registry.finish_attempt(turn_id, outcome=outcome, decision=classify_outcome(outcome))
    registry.settle("turn-error", settled_by=SETTLED_BY_TERMINAL_RESULT)
    record = store.latest_for_model("claude", MODEL)
    assert record["terminal_error"] == {
        "source_id": "src_deleted01",
        "configured_model_id": "unknown-model",
        "channel": "hub",
        "reason": "invalid_parameter",
        "stream_started": False,
        "http_status": expected_status,
        "upstream_error_code": expected_code,
    }
    serialized = store.path.read_text()
    assert "arbitrary" not in serialized
    assert "credential" not in serialized


def test_model_history_read_preserves_legacy_records_and_current_route_state(tmp_path):
    service, store, adapter = _service(tmp_path, _sparse_config())
    legacy = _record("legacy")
    legacy["contract_version"] -= 1
    service.provenance.put(legacy)
    before = store.config.to_payload()
    history_bytes = service.provenance.path.read_bytes()
    assert service.get_model_provenance("claude", MODEL) == legacy
    assert service.get_turn_provenance("legacy") == legacy
    assert store.config.to_payload() == before
    assert service.provenance.path.read_bytes() == history_bytes
    assert adapter.synced == []


def test_terminal_diagnostic_schema_uses_the_production_machine_code_authority():
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/plans/model-hub-contracts/turn-provenance.schema.json").read_text()
    )
    enum = schema["properties"]["terminal_error"]["properties"]["upstream_error_code"]["enum"]
    assert set(enum) - {None} == UPSTREAM_MACHINE_ERROR_CODES
    assert "engine_down" not in enum


@pytest.mark.parametrize(
    "error_type,expected_code,expected_error",
    [
        ("invalid_request_error", "model_not_found", "upstream_request_invalid"),
        ("permission_error", "permission_error", "request_incompatible"),
        ("arbitrary credential=secret", "model_not_found", "upstream_request_invalid"),
    ],
)
def test_specific_model_diagnostic_does_not_rerank_classification(tmp_path, error_type, expected_code, expected_error):
    store = BoundedProvenanceStore(tmp_path / "records.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "fixture", "turn-error")
    turn_id = registry.begin_gateway_request(backend="claude", token=token, requested_model_id=MODEL)
    registry.begin_attempt(
        turn_id, source_id="src_deleted01", resolved_model_id="unknown-model", channel="hub", via_mapping=False
    )
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.HTTP_ERROR,
        http_status=404,
        error_code="model_not_found",
        error_type=error_type,
        redacted_message="arbitrary credential=secret",
        stream_started=False,
        model_id="unknown-model",
        source_id="src_deleted01",
    )
    decision = classify_outcome(outcome)
    assert decision.error_code == expected_error
    registry.finish_attempt(turn_id, outcome=outcome, decision=decision)
    registry.settle("turn-error", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn-error")["terminal_error"]["upstream_error_code"] == expected_code
    assert classify_outcome(outcome) == decision
    assert "arbitrary" not in store.path.read_text()


@pytest.mark.parametrize(
    "backend,model", [("unknown", MODEL), ("claude", " unknown "), ("claude", "not-in-catalog"), ("claude", None)]
)
def test_model_history_validates_backend_and_canonical_catalog_id(tmp_path, backend, model):
    service, _, _ = _service(tmp_path, _sparse_config())
    with pytest.raises(ModelHubError):
        service.get_model_provenance(backend, model)


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_latest_history_reads_exact_persisted_legacy_catalog_identity(tmp_path, backend):
    legacy = "legacy-" + "x" * MODEL_ID_MAX_LENGTH
    service, store, adapter = _service(tmp_path, _loaded_catalog_config(backend, legacy))
    before = store.config.to_payload()
    assert service.get_model_provenance(backend, legacy) is None
    record = _record("legacy-long", backend=backend, model=legacy)
    service.provenance.put(record)
    assert service.get_model_provenance(backend, legacy) == record
    with pytest.raises(ModelHubError):
        service.get_model_provenance(backend, f" {legacy} ")
    with pytest.raises(ModelHubError):
        service.get_model_provenance(backend, "new-" + "x" * MODEL_ID_MAX_LENGTH)
    assert store.config.to_payload() == before
    assert adapter.synced == []


def test_model_history_client_rpc_http_share_nullable_read_only_result(monkeypatch, tmp_path):
    service, store, adapter = _service(tmp_path, _sparse_config())
    before = store.config.to_payload()

    def rpc(operation, payload=None):
        return asyncio.run(dispatch_model_hub_rpc(service, operation, payload or {}))

    monkeypatch.setattr(model_hub_client, "_rpc_sync", rpc)
    remote = model_hub_client.ModelHubRemoteService()
    assert remote.get_model_provenance("claude", MODEL) is None
    record = _record("latest")
    service.provenance.put(record)
    assert remote.get_model_provenance("claude", MODEL) == record
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: remote)
    client = ui_server.app.test_client()
    origin = "http://127.0.0.1:15131"
    response = client.get(
        f"/api/models/agents/claude/provenance?model={MODEL}", headers=csrf_headers(client, origin), base_url=origin
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "contract_version": 9, "provenance": record}
    assert store.config.to_payload() == before
    assert adapter.synced == []
