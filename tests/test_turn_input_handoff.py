"""An accepted steer must not disappear when its target Turn is stopped."""
import asyncio

import pytest
from unittest.mock import AsyncMock

from core.services.agent_steering import SteerOutcome, result as steer_result
from core.session_turns import DeliveryRequest
from storage import message_deliveries as store
from tests.test_session_delivery_fsm import _activate, _context, _fsm_schema_template, managers  # noqa: F401


def test_stopped_turn_hands_off_only_accepted_collateral(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="selected work"))
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    for text in ("first collateral", "second collateral"):
        accepted = asyncio.run(manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content=text), context=_context(),
        ))
        assert accepted.state == "accepted"
    for _ in range(2):
        with engine.begin() as conn:
            store.terminalize_turn(conn, turn_id, outcome="canceled", settled_by="stopped", evidence_kind="terminal_result")
    with engine.connect() as conn:
        backlog = store.list_queued(conn, "ses_fsm")
        text = "\n".join(row["dispatch_text"] for row in backlog)
        assert text.count("first collateral") == text.count("second collateral") == 1
        assert text.index("first collateral") < text.index("second collateral")
        assert "selected work" not in text
        assert all(row["state"] == "accepted" for row in store.deliveries_for_turn(conn, turn_id))


@pytest.mark.parametrize("kind,carry_primary", [
    ("server", True), ("overloaded", True), ("network", True), ("stalled", True),
    ("auth", False), ("rate_limit", False), ("context_overflow", False), (None, False),
])
def test_failure_carries_transient_primary_but_always_protects_collateral(managers, kind, carry_primary):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    receipt = asyncio.run(manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p1", content="collateral report"), context=_context(),
    ))
    assert receipt.state == "accepted"
    evidence = {"output_provenance": {"error_kind": kind}}
    with engine.begin() as conn:
        store.terminalize_turn(conn, turn_id, outcome="failed", settled_by="terminal_result",
                               evidence_kind="terminal_result", evidence=evidence)
        queue = store.list_queued(conn, "ses_fsm")
        assert [item["dispatch_text"] for item in queue] == (["initial request"] if carry_primary else []) + ["collateral report"]
        successor = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=successor, session_id="ses_fsm", backend="codex",
                                deliveries=[store.get_delivery(conn, item["id"]) for item in queue],
                                dispatch_text="\n".join(item["dispatch_text"] for item in queue))
        turn = store.get_turn(conn, successor)
        store.bind_native_start(conn, successor, expected_version=turn["version"], runtime_key="retry",
                                runtime_turn_id="retry", native_turn_id="retry")
        store.materialize_start_acceptance(conn, turn_id=successor, evidence={})
        store.terminalize_turn(conn, successor, outcome="failed", settled_by="terminal_result",
                               evidence_kind="terminal_result", evidence=evidence)
        assert not store.list_queued(conn, "ses_fsm")


def test_failed_output_does_not_settle_carried_run_before_or_after_terminal(managers):
    from pathlib import Path
    from sqlalchemy import select
    from storage.background import SQLiteBackgroundTaskStore
    from storage.models import agent_runs

    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    with engine.begin() as conn:
        initial = store.get_turn(conn, turn_id)["initial_delivery_id"]
        conn.execute(agent_runs.insert().values(
            id="carried", run_type="agent_run", session_id="ses_fsm", status="running",
            created_at=store.utc_now_iso(), updated_at=store.utc_now_iso(), delivery_id=initial,
            cancel_requested=0, metadata_json="{}",
        ))
    background = SQLiteBackgroundTaskStore(Path(engine.url.database))
    provenance = {"turn_id": turn_id, "error_kind": "overloaded"}
    try:
        for phase in ("before", "after"):
            if phase == "after":
                with engine.begin() as conn:
                    store.terminalize_turn(conn, turn_id, outcome="failed", settled_by="terminal_result",
                                           evidence_kind="terminal_result", evidence={"output_provenance": provenance})
            background.record_turn_terminal_outputs(["carried"], output_id="terminal", text="overload",
                                                    terminal_status="failed", provenance=provenance)
            with engine.connect() as conn:
                row = conn.execute(select(agent_runs).where(agent_runs.c.id == "carried")).mappings().one()
                assert row["status"] == "running", phase
                assert row["completed_at"] is None
                if phase == "after":
                    assert row["delivery_id"] == store.list_queued(conn, "ses_fsm")[0]["id"]
    finally:
        background.close()
