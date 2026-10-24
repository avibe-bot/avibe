from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT, SETTLED_BY_STOPPED, SETTLED_BY_NO_TERMINAL_RESULT
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
    projection = replace(
        produce_turn_outcome("turn.engine_down"),
        local_error_detail="[Errno 28] No space left on device",
    )
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


def test_backend_callback_without_an_attempt_does_not_freeze_gateway_evidence(tmp_path):
    registry = _live_registry(tmp_path)
    registry.fail_hub_attempt("turn-live", diagnostic="backend exited before attribution")
    projection = produce_turn_outcome("turn.engine_down")
    registry.record_turn_outcome("turn-live", projection)
    assert registry.terminal_projection("turn-live", backend="codex") == projection
    assert not registry._traces["turn-live"].outcome_frozen


@pytest.mark.parametrize("with_projection", [False, True])
def test_new_attempt_retires_previous_request_terminal_before_fsm_drop(tmp_path, with_projection):
    registry = _live_registry(tmp_path)
    registry.begin_attempt(
        "turn-live", source_id="src_previous1", resolved_model_id=MODEL, channel="hub", via_mapping=False,
    )
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.HTTP_ERROR if with_projection else RawOutcomeKind.PROTOCOL_ERROR,
        http_status=400 if with_projection else 200,
        error_code="invalid_request_error" if with_projection else None,
        redacted_message=None, stream_started=False, model_id=MODEL, source_id="src_previous1",
    )
    registry.finish_attempt("turn-live", outcome=outcome, decision=classify_outcome(outcome))
    if with_projection:
        registry.record_turn_outcome("turn-live", produce_turn_outcome("turn.request_nonfallback"))
    registry.begin_attempt(
        "turn-live", source_id="src_successor1", resolved_model_id=MODEL, channel="hub", via_mapping=False,
    )
    assert registry._traces["turn-live"].terminal_outcome is None
    registry.settle("turn-live", settled_by=SETTLED_BY_NO_TERMINAL_RESULT)
    terminal = registry.store.get("turn-live")["terminal_error"]
    assert terminal["source_id"] == "src_successor1"
    assert terminal["reason"] == "stream_interrupted"


@pytest.mark.parametrize("late_outcome", [RawOutcomeKind.SUCCESS, RawOutcomeKind.NETWORK_ERROR])
@pytest.mark.parametrize("closure", [SETTLED_BY_TERMINAL_RESULT, SETTLED_BY_STOPPED])
def test_native_hub_failure_keeps_the_gateway_terminal_projection_readable(tmp_path, late_outcome, closure):
    registry = _live_registry(tmp_path)
    registry.begin_attempt(
        "turn-live", source_id="src_primary01", resolved_model_id=MODEL, channel="hub", via_mapping=False,
    )
    failure = RawCallOutcome(
        kind=RawOutcomeKind.HTTP_ERROR, http_status=400, error_code="invalid_request_error",
        redacted_message=None, stream_started=False, model_id=MODEL, source_id="src_primary01",
    )
    registry.finish_attempt("turn-live", outcome=failure, decision=classify_outcome(failure))
    projection = produce_turn_outcome("turn.request_nonfallback")
    registry.record_turn_outcome("turn-live", projection)
    terminal = dict(registry._traces["turn-live"].terminal_error)
    registry.fail_hub_attempt("turn-live")
    assert registry.terminal_projection("turn-live", backend="codex") == projection

    # After the backend has acknowledged the terminal, late admission cannot
    # clear it, and late success/interruption cannot contradict the notice.
    registry.begin_attempt(
        "turn-live", source_id="src_late0001", resolved_model_id=MODEL, channel="hub", via_mapping=False,
    )
    late = replace(failure, kind=late_outcome, error_code=None, stream_started=True, source_id="src_late0001")
    registry.finish_attempt("turn-live", outcome=late, decision=classify_outcome(late))
    registry.record_turn_outcome("turn-live", produce_turn_outcome("turn.served"))
    assert registry.terminal_projection("turn-live", backend="codex") == projection
    assert registry.terminal_projection("turn-live", backend="claude") is None
    registry.close_turn_admission("turn-live", settled_by=closure)
    assert registry.terminal_projection("turn-live", backend="codex") == (
        None if closure == SETTLED_BY_STOPPED else projection
    )
    registry.fail_hub_attempt("turn-live")
    registry.settle("turn-live", settled_by=closure)
    record = registry.store.get("turn-live")
    assert record["outcome"] == "failed_terminal"
    assert record["terminal_error"] == terminal


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
        "contract_version": 21,
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


@pytest.mark.parametrize("version", range(5, 21))
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
    # The record keeps only safe, recognized metadata whatever the outcome carried; which class the classifier
    # assigns is not under test here (classification reads machine codes, never this text).
    decision = ResolutionDecision("surface", error_code="upstream_request_invalid", downstream_status=400)
    registry.finish_attempt(turn_id, outcome=outcome, decision=decision)
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


def test_response_correlation_ids_stay_on_the_exact_failed_attempt_and_terminal_error(tmp_path):
    registry = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "records.json"))
    token = registry.credentials("claude", "fixture", "turn-correlation")
    turn_id = registry.begin_gateway_request(
        backend="claude", token=token, requested_model_id=MODEL,
    )
    registry.begin_attempt(
        turn_id, source_id="src_primary01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            http_status=429,
            error_code="rate_limit_error",
            redacted_message=None,
            stream_started=False,
            model_id=MODEL,
            source_id="src_primary01",
            upstream_detail="first upstream detail",
            response_correlation_ids=(
                ("x-request-id", "relay-first"),
                ("request-id", "provider-first"),
            ),
        ),
        decision=ResolutionDecision("fallback", reason="rate_limited", cooldown_seconds=30),
    )
    registry.begin_attempt(
        turn_id, source_id="src_terminal01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            http_status=400,
            error_code="invalid_request_error",
            redacted_message=None,
            stream_started=False,
            model_id=MODEL,
            source_id="src_terminal01",
            upstream_detail="terminal upstream detail",
            response_correlation_ids=(("request-id", "provider-terminal"),),
        ),
        decision=ResolutionDecision("surface", error_code="upstream_request_invalid", downstream_status=400),
    )
    registry.settle(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT)

    record = registry.store.get(turn_id)
    assert record is not None
    assert record["failed_attempts"][0]["response_correlation_ids"] == [
        {"header": "request-id", "value": "provider-first"},
        {"header": "x-request-id", "value": "relay-first"},
    ]
    assert record["terminal_error"]["response_correlation_ids"] == [
        {"header": "request-id", "value": "provider-terminal"},
    ]
    assert record["failed_attempts"][0]["upstream_detail"] == "first upstream detail"
    assert record["terminal_error"]["upstream_detail"] == "terminal upstream detail"


def test_served_response_correlation_ids_belong_to_the_served_attempt(tmp_path):
    registry = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "records.json"))
    token = registry.credentials("claude", "fixture", "turn-served-correlation")
    turn_id = registry.begin_gateway_request(
        backend="claude", token=token, requested_model_id=MODEL,
    )
    registry.begin_attempt(
        turn_id, source_id="src_first0001", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            http_status=503,
            error_code="server_error",
            redacted_message=None,
            stream_started=False,
            model_id=MODEL,
            source_id="src_first0001",
            response_correlation_ids=(("x-request-id", "failed-request"),),
        ),
        decision=ResolutionDecision("fallback", reason="server_error", cooldown_seconds=30),
    )
    registry.begin_attempt(
        turn_id, source_id="src_served01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS,
            http_status=200,
            error_code=None,
            redacted_message=None,
            stream_started=True,
            model_id=MODEL,
            source_id="src_served01",
            response_correlation_ids=(("x-request-id", "served-request"),),
        ),
        decision=ResolutionDecision("return"),
    )
    registry.settle(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT)

    record = registry.store.get(turn_id)
    assert record is not None
    assert record["failed_attempts"] == [
        {
            "source_id": "src_first0001",
            "configured_model_id": MODEL,
            "channel": "hub",
            "reason": "server_error",
            "http_status": 503,
            "response_correlation_ids": [
                {"header": "x-request-id", "value": "failed-request"},
            ],
        },
    ]
    assert record["served"] == {
        "source_id": "src_served01",
        "configured_model_id": MODEL,
        "channel": "hub",
        "response_correlation_ids": [
            {"header": "x-request-id", "value": "served-request"},
        ],
    }
    assert record["terminal_error"] is None


def test_backend_failure_drops_response_correlation_ids_from_prior_served_response(tmp_path):
    registry = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "records.json"))
    token = registry.credentials("codex", "fixture", "turn-backend-failure-correlation")
    turn_id = registry.begin_gateway_request(
        backend="codex", token=token, requested_model_id=MODEL,
    )
    registry.begin_attempt(
        turn_id, source_id="src_served01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS,
            http_status=200,
            error_code=None,
            redacted_message=None,
            stream_started=True,
            model_id=MODEL,
            source_id="src_served01",
            response_correlation_ids=(("x-request-id", "served-request"),),
        ),
        decision=ResolutionDecision("return"),
    )
    registry.fail_hub_attempt(turn_id, error_kind="stalled", diagnostic="backend rejected the response")

    trace = registry._traces[turn_id]
    assert trace.terminal_error is not None
    assert trace.terminal_error["reason"] == "backend_error"
    assert "response_correlation_ids" not in trace.terminal_error


def test_response_correlation_ids_are_sanitized_before_provenance_persistence(tmp_path):
    registry = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "records.json"))
    token = registry.credentials("claude", "fixture", "turn-correlation-safe")
    turn_id = registry.begin_gateway_request(
        backend="claude", token=token, requested_model_id=MODEL,
    )
    registry.begin_attempt(
        turn_id, source_id="src_primary01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            http_status=400,
            error_code="invalid_request_error",
            redacted_message=None,
            stream_started=False,
            model_id=MODEL,
            source_id="src_primary01",
            response_correlation_ids=(
                ("x-request-id", "bad\nvalue"),
                ("request-id", "Bearer secret-token-value"),
                ("Set-Cookie", "session=secret"),
                ("Request-Id", "provider-safe"),
            ),
        ),
        decision=ResolutionDecision("surface", error_code="upstream_request_invalid", downstream_status=400),
    )
    registry.settle(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT)

    record = registry.store.get(turn_id)
    assert record is not None
    assert record["terminal_error"]["response_correlation_ids"] == [
        {"header": "request-id", "value": "provider-safe"},
    ]
    serialized = registry.store.path.read_text()
    assert "secret-token" not in serialized
    assert "Set-Cookie" not in serialized


def test_engine_down_terminal_error_keeps_safe_response_correlation_ids(tmp_path):
    registry = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "records.json"))
    token = registry.credentials("claude", "fixture", "turn-engine-down-correlation")
    turn_id = registry.begin_gateway_request(
        backend="claude", token=token, requested_model_id=MODEL,
    )
    registry.begin_attempt(
        turn_id, source_id="src_engine01", resolved_model_id=MODEL,
        channel="hub", via_mapping=False,
    )
    registry.finish_attempt(
        turn_id,
        outcome=RawCallOutcome(
            kind=RawOutcomeKind.NETWORK_ERROR,
            http_status=502,
            error_code="engine_down",
            redacted_message=None,
            stream_started=False,
            model_id=MODEL,
            source_id="src_engine01",
            response_correlation_ids=(
                ("x-request-id", "relay-engine-down"),
            ),
        ),
        decision=ResolutionDecision("surface", error_code="engine_down", downstream_status=502),
    )
    registry._terminalize_gateway_exit(turn_id, stream_started=False, force=True)
    registry.settle(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT)

    record = registry.store.get(turn_id)
    assert record is not None
    assert record["terminal_error"] == {
        "source_id": None,
        "configured_model_id": None,
        "channel": None,
        "reason": "engine_down",
        "stream_started": False,
        "response_correlation_ids": [
            {"header": "x-request-id", "value": "relay-engine-down"},
        ],
    }

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
    # Each code is listed once; the set comparison below would collapse a duplicate.
    assert len(enum) == len(set(enum))
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
    assert response.get_json() == {"ok": True, "contract_version": 21, "provenance": record}
    assert store.config.to_payload() == before
    assert adapter.synced == []
