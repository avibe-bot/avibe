"""Live recovery changes status only; it never emits a chat notification."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.model_hub_progress import (
    publish_recovery_changed,
    recovery_snapshot,
    recovery_status_text,
)
from core.session_turns import SessionTurnManager, Turn
from tests.test_model_hub_l3 import _outcome
from tests.test_model_hub_retry_policy import clock_service


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
    """MH-RETRY-PROGRESS-001: material recovery updates invalidate only their live Session."""
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


@pytest.mark.parametrize("ending", ["success", "stop"])
async def test_http_recovery_reaches_live_turn_state_and_clears_without_chat(tmp_path, ending):
    """MH-RETRY-PROGRESS-001: gateway producer -> registry callback -> Session read."""
    service, clock = clock_service(tmp_path, outcomes=[
        _outcome(RawOutcomeKind.HTTP_ERROR, source_id="src_recovery01", status=503),
        _outcome(RawOutcomeKind.SUCCESS, source_id="src_recovery01"),
    ])
    waiting, release = asyncio.Event(), asyncio.Event()

    async def controlled_wait(delay):
        clock.advance(5)
        waiting.set()
        await release.wait()
        clock.advance(delay - 5)

    service.recovery.sleep = controlled_wait
    gateway = ModelHubTurnGateway(service)
    controller = SimpleNamespace(model_hub_turn_gateway=gateway, emit_agent_message=AsyncMock())
    manager = SessionTurnManager(controller=controller)
    controller.session_turns = manager
    manager._engine = SimpleNamespace(begin=lambda: nullcontext(object()))
    context = SimpleNamespace(platform_specific={
        "turn_token": "turn-progress", "agent_session_target": {"agent_backend": "codex"},
    })
    gateway.correlation.on_recovery_changed = lambda turn: publish_recovery_changed(controller, turn)
    base, token = await gateway.endpoint(
        "codex", process_scope="fixture-progress", turn_id="turn-progress",
        requested_model_id="shared-model", resolved_model_id="shared-model", source_id="src_recovery01",
    )
    request_task = None
    try:
        with (
            patch("core.inbox_events.bus.publish") as publish,
            patch("core.session_turns.delivery_store.list_queued", return_value=[]),
        ):
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=5)) as client:
                async def post():
                    async with client.post(
                        f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                        json={"model": "shared-model", "stream": False},
                    ) as response:
                        await response.read()
                        return response.status

                request_task = asyncio.create_task(post())
                manager.in_flight["ses-live"] = Turn(task=request_task, context=context)
                await asyncio.wait_for(waiting.wait(), 2)
                row, = manager.turn_state("ses-live")["model_recovery"]
                assert row["phase"] == "waiting" and row["attempt_count"] == 1
                assert row["request_id"] and row["source_id"] == "src_recovery01"
                assert recovery_status_text(controller, "turn-progress", "en", now=clock.now()) == (
                    "Waiting to retry the model request in about 25s"
                )
                assert not request_task.done() and len(service.adapter.invocations) == 1
                if ending == "success":
                    release.set()
                    assert await asyncio.wait_for(request_task, 2) == 200
                else:
                    drain = gateway.finalize_turn("turn-progress", settled_by="stopped", finish=lambda: None)
                    assert drain is not None
                    await asyncio.wait_for(drain, 2)
                    assert len(service.adapter.invocations) == 1
                    await asyncio.gather(request_task, return_exceptions=True)
                assert gateway.correlation.recovery_snapshot("turn-progress") == []
                assert len(publish.call_args_list) >= 2
                for call in publish.call_args_list:
                    assert call.args == (
                        "session.activity", {"session_id": "ses-live", "event": "model_recovery"},
                    )
                controller.emit_agent_message.assert_not_awaited()
    finally:
        if request_task is not None:
            if not request_task.done():
                request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await gateway.close()
