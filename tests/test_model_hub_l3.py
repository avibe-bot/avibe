from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.events import build_resolution_event
from core.handlers.model_hub.provenance import (
    AttemptIdentity,
    BoundedProvenanceStore,
    TurnCorrelationRegistry,
)
from core.run_settlement import SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT


CONTRACTS = Path("docs/plans/model-hub-contracts")


def valid(schema_name: str, payload: dict):
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert not errors, [error.message for error in errors]


def outcome(*, stream_started=False):
    return RawCallOutcome(
        kind=RawOutcomeKind.SUCCESS,
        http_status=200,
        error_code=None,
        redacted_message=None,
        stream_started=stream_started,
        model_id="concrete-upstream-id",
        source_id="src_l3000001",
    )


def test_attempt_identity_contains_concrete_configured_model_only():
    payload = AttemptIdentity(
        source_id="src_l3000001",
        resolved_model_id="concrete-upstream-id",
        channel="hub",
    ).payload()
    assert payload == {
        "source_id": "src_l3000001",
        "configured_model_id": "concrete-upstream-id",
        "channel": "hub",
    }
    assert "via_mapping" not in payload
    assert "resolved_model_id" not in payload


def test_cancellation_settles_pending_attempt_as_canceled(tmp_path):
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    registry.credentials("claude", "scope", "turn-cancel")
    registry.begin_native_attempt(
        backend="claude",
        process_scope="scope",
        turn_id="turn-cancel",
        requested_model_id="menu-model",
        source_id="src_l3000001",
        resolved_model_id="concrete-upstream-id",
        via_mapping=False,
    )
    registry.settle("turn-cancel", settled_by=SETTLED_BY_STOPPED, ts="2026-08-09T00:00:00+00:00")
    payload = store.get("turn-cancel")
    assert payload is not None
    assert payload["outcome"] == "canceled"
    assert payload["canceled_attempt"]["configured_model_id"] == "concrete-upstream-id"
    assert payload["canceled_attempt"].get("reason") is None
    valid("turn-provenance.schema.json", payload)


def test_terminal_result_records_served_exact_model(tmp_path):
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    registry.credentials("codex", "scope", "turn-served")
    registry.begin_native_attempt(
        backend="codex",
        process_scope="scope",
        turn_id="turn-served",
        requested_model_id="gpt-5",
        source_id="src_l3000001",
        resolved_model_id="gpt-5",
        via_mapping=False,
    )
    registry.settle("turn-served", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-08-09T00:00:00+00:00")
    payload = store.get("turn-served")
    assert payload["outcome"] == "served"
    assert payload["served"]["configured_model_id"] == "gpt-5"
    valid("turn-provenance.schema.json", payload)


def test_resolution_event_vocabulary_has_no_mapping_event():
    event = build_resolution_event(
        agent="claude",
        kind="switch",
        model_id="claude-opus-4-6",
        reason="rate_limited",
        from_source="src_l3000001",
        to_source="src_l3000002",
        from_label="Primary",
        to_label="Backup",
    )
    payload = event.to_payload()
    assert payload["kind"] == "switch"
    assert "mapping_applied" not in payload
    valid("resolution-event.schema.json", payload)


def test_resolution_event_uses_empty_chain_kind_for_structural_failure():
    event = build_resolution_event(
        agent="claude",
        kind="supply_interrupted",
        model_id="claude-opus-4-6",
        reason="no_enabled_source",
    )
    payload = event.to_payload()
    assert payload["from_source"] is None
    assert payload["to_source"] is None
    valid("resolution-event.schema.json", payload)
