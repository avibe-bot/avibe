"""Live recovery changes status only; it never emits a chat notification."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.model_hub_progress import (
    publish_recovery_changed,
    recovery_snapshot,
    recovery_status_text,
)
from core.session_turns import SessionTurnManager, Turn


NOW = datetime(2026, 9, 9, 4, 30, tzinfo=timezone.utc)


def snapshot(**overrides):
    return {
        "request_id": "request-exact",
        "phase": "waiting",
        "attempt_count": 1,
        "started_at": (NOW - timedelta(seconds=5)).isoformat(),
        "window_end": (NOW + timedelta(seconds=115)).isoformat(),
        "source_id": "src_unprinted",
        "reason": "server_error",
        "next_eligible_at": (NOW + timedelta(seconds=25)).isoformat(),
        **overrides,
    }


def controller_for(rows):
    return SimpleNamespace(
        model_hub_turn_gateway=SimpleNamespace(
            correlation=SimpleNamespace(recovery_snapshot=Mock(return_value=rows)),
        ),
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_progress_uses_relative_time_after_debounce(language):
    controller = controller_for([snapshot()])
    text = recovery_status_text(controller, "turn-exact", language, now=NOW)
    assert "25" in text
    assert "src_unprinted" not in text and "2026" not in text
    assert recovery_status_text(
        controller, "turn-exact", language, now=NOW - timedelta(milliseconds=1),
    ) is None
    # Reload reads the original start rather than starting a new debounce.
    assert recovery_status_text(
        controller, "turn-exact", language, now=NOW + timedelta(seconds=10),
    ) is not None


@pytest.mark.parametrize("deadline", [None, "invalid", (NOW - timedelta(seconds=1)).isoformat()])
def test_deadline_expiry_is_not_recovery(deadline):
    controller = controller_for([snapshot(next_eligible_at=deadline)])
    text = recovery_status_text(controller, "turn-exact", "en", now=NOW)
    assert text == "Waiting for the next model attempt…"
    assert "recovered" not in text


def test_attempting_and_cleared_progress():
    controller = controller_for([snapshot(phase="attempting", next_eligible_at=None)])
    assert recovery_status_text(controller, "turn-exact", "en", now=NOW) == (
        "Retrying the model request…"
    )
    controller.model_hub_turn_gateway.correlation.recovery_snapshot.return_value = []
    assert recovery_status_text(controller, "turn-exact", "en", now=NOW) is None


@pytest.mark.parametrize("row", [
    snapshot(started_at="invalid"),
    snapshot(started_at=None),
    snapshot(started_at=(NOW + timedelta(seconds=30)).isoformat()),
    snapshot(phase="unknown"),
])
def test_unusable_progress_keeps_normal_working_indicator(row):
    assert recovery_status_text(controller_for([row]), "turn-exact", "en", now=NOW) is None


def test_no_turn_or_gateway_has_no_snapshot():
    controller = controller_for([snapshot()])
    assert recovery_snapshot(controller, None) == []
    controller.model_hub_turn_gateway.correlation.recovery_snapshot.assert_not_called()
    assert recovery_snapshot(SimpleNamespace(), "turn-exact") == []


def test_only_the_exact_live_session_gets_read_invalidation():
    def entry(turn_id, done=False):
        return SimpleNamespace(
            context=SimpleNamespace(platform_specific={"turn_token": turn_id}),
            task=SimpleNamespace(done=lambda: done),
        )

    controller = SimpleNamespace(session_turns=SimpleNamespace(in_flight={
        "ses-live": entry("turn-exact"),
        "ses-peer": entry("turn-peer"),
        "ses-old": entry("turn-exact", done=True),
    }))
    with patch("core.inbox_events.bus.publish") as publish:
        publish_recovery_changed(controller, "turn-exact")
    publish.assert_called_once_with(
        "session.activity", {"session_id": "ses-live", "event": "model_recovery"},
    )


def test_turn_state_reads_recovery_only_for_its_current_live_owner():
    rows = [snapshot()]
    controller = controller_for(rows)
    context = SimpleNamespace(platform_specific={
        "turn_token": "turn-exact",
        "agent_session_target": {"agent_backend": "codex"},
    })
    manager = SessionTurnManager(controller=controller)
    manager._engine = SimpleNamespace(begin=lambda: nullcontext(object()))
    manager.in_flight["ses-live"] = Turn(
        task=SimpleNamespace(done=lambda: False), context=context,
    )
    with patch("core.session_turns.delivery_store.list_queued", return_value=[]):
        state = manager.turn_state("ses-live")
    assert state["in_flight"] is True
    assert state["model_recovery"] == rows
    reader = controller.model_hub_turn_gateway.correlation.recovery_snapshot
    reader.assert_called_once_with("turn-exact")

    manager.in_flight.clear()
    reader.reset_mock()
    with patch("core.session_turns.delivery_store.list_queued", return_value=[]):
        state = manager.turn_state("ses-live")
    assert "model_recovery" not in state
    reader.assert_not_called()
