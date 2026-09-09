from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import UPSTREAM_MACHINE_ERROR_CODES, ResolutionDecision, classify_outcome
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore, TurnCorrelationRegistry, produce_turn_outcome, render_turn_outcome_copy,
)
from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
from core.handlers.model_hub.service import ModelHubError
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT, SETTLED_BY_STOPPED
from modules.agents.model_hub import ModelHubLaunch, ModelHubRuntimeRouter, bind_launch
from tests.test_model_hub_resolution import _config, _service, _source
from tests.test_model_hub_routing_modes import MODEL, _loaded_catalog_config, _sparse_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import model_hub_client, ui_server


def _live_registry(tmp_path, callback=None):
    registry = TurnCorrelationRegistry(
        BoundedProvenanceStore(tmp_path / "records.json"), on_recovery_changed=callback,
    )
    token = registry.credentials("codex", "fixture", "turn-live")
    registry.begin_gateway_request(backend="codex", token=token, requested_model_id="shared-model")
    return registry


@pytest.mark.parametrize("guard", [
    "valid", "wrong_backend", "missing", "pending", "ambiguous", "poisoned",
    "closed", "stopped", "served", "canceled", "settled",
])
def test_live_terminal_projection_requires_exact_unfrozen_owner(tmp_path, guard):
    registry = _live_registry(tmp_path)
    projection = produce_turn_outcome("turn.engine_down")
    registry.record_turn_outcome("turn-live", projection)
    if guard == "pending":
        registry.begin_attempt(
            "turn-live", source_id="src_primary01", resolved_model_id="shared-model",
            channel="hub", via_mapping=False,
        )
    elif guard == "ambiguous":
        registry._traces["turn-live"].ambiguous = True
    elif guard == "poisoned":
        registry._scopes[("codex", "fixture")].untracked_use = True
    elif guard in {"closed", "stopped"}:
        registry.close_turn_admission(
            "turn-live", settled_by=SETTLED_BY_STOPPED if guard == "stopped" else SETTLED_BY_TERMINAL_RESULT,
        )
    elif guard in {"served", "canceled"}:
        registry.record_turn_outcome("turn-live", produce_turn_outcome(f"turn.{guard}"))
    elif guard == "settled":
        registry.settle("turn-live", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert registry.terminal_projection(
        "missing" if guard == "missing" else "turn-live",
        backend="claude" if guard == "wrong_backend" else "codex",
    ) == (projection if guard in {"valid", "closed"} else None)


def test_recovery_callbacks_are_material_post_lock_and_snapshots_are_copies(tmp_path):
    calls = []
    registry = _live_registry(tmp_path)

    def changed(turn_id):
        assert not registry._lock._is_owned()
        calls.append(registry.recovery_snapshot(turn_id))

    registry.on_recovery_changed = changed
    snapshot = {
        "phase": "waiting", "attempt_count": 1, "source_id": "src_primary01",
        "reason": "network", "started_at": "2026-09-09T00:00:00+00:00",
        "next_eligible_at": None, "window_end": "2026-09-09T00:02:00+00:00",
    }
    registry.update_recovery("turn-live", backend="codex", request_id="one", snapshot=snapshot)
    registry.update_recovery("turn-live", backend="codex", request_id="one", snapshot=dict(snapshot))
    registry.update_recovery("turn-live", backend="claude", request_id="wrong", snapshot=snapshot)
    assert len(calls) == 1
    external = registry.recovery_snapshot("turn-live")
    external[0]["phase"] = "corrupted"
    assert registry.recovery_snapshot("turn-live")[0]["phase"] == "waiting"
    registry.update_recovery("turn-live", backend="codex", request_id="two", snapshot=snapshot)
    registry.update_recovery("turn-live", backend="codex", request_id="one", snapshot=None)
    assert [item["request_id"] for item in registry.recovery_snapshot("turn-live")] == ["two"]
    registry.update_recovery("turn-live", backend="codex", request_id="two", snapshot=None)
    registry.update_recovery("turn-live", backend="codex", request_id="two", snapshot=None)
    assert len(calls) == 4 and calls[-1] == []
    assert not registry.store.path.exists()


def test_native_hub_failure_keeps_the_gateway_terminal_projection_readable(tmp_path):
    registry = _live_registry(tmp_path)
    projection = produce_turn_outcome("turn.engine_down")
    registry.record_turn_outcome("turn-live", projection)
    registry.fail_hub_attempt("turn-live")
    assert registry.terminal_projection("turn-live", backend="codex") == projection
    assert not registry._traces["turn-live"].outcome_frozen
    registry.close_turn_admission("turn-live", settled_by=SETTLED_BY_STOPPED)
    registry.fail_hub_attempt("turn-live")
    assert registry.terminal_projection("turn-live", backend="codex") is None


@pytest.mark.parametrize("terminal", ["exhausted", "no_candidate"])
def test_native_failure_callback_consumes_exact_hub_terminal_copy(tmp_path, terminal):
    async def run():
        source = _source("src_callback01", (MODEL,), status="cooldown")
        service, store, _ = _service(tmp_path, _config([source], model=MODEL))
        source.state.retry_at = (service.now() + timedelta(seconds=300)).isoformat()
        registry = TurnCorrelationRegistry(service.provenance)
        token = registry.credentials("claude", "fixture", "turn-callback")
        registry.begin_gateway_request(backend="claude", token=token, requested_model_id=MODEL)
        config, resolution = service._inspect_terminal_chain(backend="claude", model_id=MODEL)
        projection = produce_turn_outcome(f"turn.{terminal}" if terminal == "exhausted" else "turn.no_candidate.blocked",
                                          config=config, resolution=resolution)
        registry.record_turn_outcome("turn-callback", projection)
        expected_text = render_turn_outcome_copy(projection, "en")
        before = store.config.to_payload()
        router = ModelHubRuntimeRouter(
            service=service, turn_gateway=SimpleNamespace(correlation=registry),
            overlay_path=tmp_path / "overlay.json",
        )
        context = SimpleNamespace(platform_specific={"turn_token": "turn-callback"})
        bind_launch(context, ModelHubLaunch(
            backend="claude", channel="hub", requested_model=MODEL,
            target_model=MODEL, runtime_model=MODEL, source_id=source.id,
        ))
        assert await router.record_native_failure(context, "HTTP 424 opaque native exception") is False
        authoritative = registry.terminal_projection("turn-callback", backend="claude")
        assert authoritative == projection
        assert render_turn_outcome_copy(authoritative, "en") == expected_text
        assert expected_text and "opaque" not in expected_text and "424" not in expected_text
        assert store.config.to_payload() == before
        assert registry.terminal_projection("turn-callback", backend="codex") is None
        registry.close_turn_admission("turn-callback", settled_by=SETTLED_BY_STOPPED)
        assert registry.terminal_projection("turn-callback", backend="claude") is None
    asyncio.run(run())


@pytest.mark.parametrize("status", [100, 503, 599, None, True, 99, 600])
def test_failed_attempt_http_status_is_additive_strict_and_history_safe(tmp_path, status):
    import jsonschema

    registry = _live_registry(tmp_path)
    registry.begin_attempt(
        "turn-live", source_id="src_primary01", resolved_model_id=MODEL, channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        "turn-live",
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR, http_status=status,
            error_code=None, redacted_message="not retained", stream_started=False,
            model_id=MODEL, source_id="src_primary01",
        ),
        decision=ResolutionDecision("fallback", reason="server_error", cooldown_seconds=30),
    )
    registry.settle("turn-live", settled_by=SETTLED_BY_TERMINAL_RESULT)
    record = registry.store.get("turn-live")
    attempt = record["failed_attempts"][0]
    expected = {"source_id": "src_primary01", "configured_model_id": MODEL, "channel": "hub", "reason": "server_error"}
    if type(status) is int and 100 <= status <= 599:
        expected["http_status"] = status
    assert attempt == expected
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/plans/model-hub-contracts/turn-provenance.schema.json").read_text()
    )
    validator = jsonschema.Draft7Validator(schema)
    assert not list(validator.iter_errors(record))
    for invalid in [True, None, 99, 600, "503"]:
        attempt["http_status"] = invalid
        assert list(validator.iter_errors(record))


@pytest.mark.parametrize("guard", ["ambiguous", "poisoned", "closed", "stopped", "settled"])
def test_live_recovery_snapshots_never_revive_invalid_or_terminal_owners(tmp_path, guard):
    registry = _live_registry(tmp_path)
    registry.update_recovery("turn-live", backend="codex", request_id="one", snapshot={"phase": "waiting"})
    if guard == "ambiguous":
        registry._traces["turn-live"].ambiguous = True
    elif guard == "poisoned":
        registry._scopes[("codex", "fixture")].untracked_use = True
    elif guard == "settled":
        registry.settle("turn-live", settled_by=SETTLED_BY_TERMINAL_RESULT)
    else:
        registry.close_turn_admission(
            "turn-live", settled_by=SETTLED_BY_STOPPED if guard == "stopped" else SETTLED_BY_TERMINAL_RESULT,
        )
    registry.update_recovery("turn-live", backend="codex", request_id="one", snapshot={"phase": "attempting"})
    assert registry.recovery_snapshot("turn-live") == []


def _record(turn_id, *, backend="claude", model=MODEL, outcome="failed_terminal"):
    identity = {"source_id": "src_deleted01", "configured_model_id": "unknown-model", "channel": "hub"}
    return {
        "contract_version": 10,
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


@pytest.mark.parametrize("version", range(5, 11))
def test_persisted_versions_remain_readable_without_rewriting(tmp_path, version):
    record = {**_record("historical"), "contract_version": version}
    path = tmp_path / "provenance.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    before = path.read_bytes()
    store = BoundedProvenanceStore(path)
    assert store.get("historical") == record
    assert store.latest_for_model("claude", MODEL) == record
    assert path.read_bytes() == before


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


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_latest_history_reads_exact_persisted_legacy_catalog_identity(tmp_path, backend):
    legacy = "legacy-" + "模型🧪" * 3000
    service, store, adapter = _service(tmp_path, _loaded_catalog_config(backend, legacy))
    before = store.config.to_payload()
    assert service.get_model_provenance(backend, legacy) is None
    record = _record("legacy-long", backend=backend, model=legacy)
    service.provenance.put(record)
    assert service.get_model_provenance(backend, legacy) == record
    with pytest.raises(ModelHubError):
        service.get_model_provenance(backend, f" {legacy} ")
    with pytest.raises(ModelHubError):
        service.get_model_provenance(backend, "new-" + "x" * 256)
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
    assert response.get_json() == {"ok": True, "contract_version": 10, "provenance": record}
    assert store.config.to_payload() == before
    assert adapter.synced == []
