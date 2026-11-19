"""An accepted steer must not disappear when its target Turn is stopped."""
import asyncio
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
