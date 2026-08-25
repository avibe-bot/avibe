from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.memory_adapter import (
    DisabledMemoryAdapter,
    EnabledMemoryAdapter,
    TurnAccepted,
)


class _Admission:
    def release(self) -> None:
        return None


def _controller(*, reservation: object, capture: object) -> SimpleNamespace:
    return SimpleNamespace(
        reserve_memory_capture_capacity=Mock(return_value=reservation),
        reserve_memory_attachment_capture=Mock(return_value=reservation),
        capture_user_memory=Mock(side_effect=capture),
        session_turns=SimpleNamespace(
            acquire_lifecycle_admission=Mock(
                side_effect=lambda _session_id: asyncio.sleep(0, result=_Admission())
            ),
            session_lifecycle_snapshot_matches=Mock(return_value=True),
        ),
        memory_runtime=SimpleNamespace(offer_barrier=Mock()),
    )


async def _wait_for_captures(adapter: EnabledMemoryAdapter) -> None:
    while adapter.capture_tasks:
        await asyncio.gather(*tuple(adapter.capture_tasks), return_exceptions=True)
        await asyncio.sleep(0)


def test_disabled_adapter_does_not_retain_attachment_lease() -> None:
    lease = Mock()

    DisabledMemoryAdapter().offer(
        TurnAccepted(object(), "caption", "session-1", 0, lease)
    )

    lease.retain.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_adapter_retains_attachment_before_scheduling_capture() -> None:
    retained = Mock()
    lease = Mock(retain=Mock(return_value=retained))
    reservation = SimpleNamespace(
        capacity_blocked=False,
        config_generation=7,
        release=Mock(),
    )
    captured: list[dict[str, object]] = []

    async def capture(*_args, **kwargs) -> None:
        captured.append(kwargs)

    controller = _controller(reservation=reservation, capture=capture)
    adapter = EnabledMemoryAdapter(controller)

    adapter.offer(TurnAccepted(object(), "caption", "session-1", 0, lease))
    lease.retain.assert_called_once_with()
    assert adapter.capture_tasks
    await _wait_for_captures(adapter)

    assert captured == [
        {
            "attachment_reservation": reservation,
            "attachment_config_generation": 7,
            "attachment_lease": retained,
        }
    ]
    retained.release.assert_called_once_with()


@pytest.mark.asyncio
async def test_enabled_adapter_retain_failure_falls_back_to_text_only() -> None:
    lease = Mock()
    lease.retain.side_effect = RuntimeError("pin unavailable")
    reservation = SimpleNamespace(
        capacity_blocked=False,
        config_generation=3,
        release=Mock(),
    )
    captured: list[dict[str, object]] = []

    async def capture(*_args, **kwargs) -> None:
        captured.append(kwargs)

    adapter = EnabledMemoryAdapter(
        _controller(reservation=reservation, capture=capture)
    )

    adapter.offer(TurnAccepted(object(), "caption", "session-1", 0, lease))
    await _wait_for_captures(adapter)

    assert captured == [
        {
            "attachment_reservation": reservation,
            "attachment_config_generation": 3,
            "attachment_text_only": True,
        }
    ]


def test_enabled_adapter_releases_blocked_capacity_without_retaining() -> None:
    lease = Mock()
    reservation = SimpleNamespace(
        capacity_blocked=True,
        config_generation=None,
        release=Mock(),
    )
    controller = _controller(reservation=reservation, capture=Mock())
    adapter = EnabledMemoryAdapter(controller)

    adapter.offer(TurnAccepted(object(), "caption", "session-1", 0, lease))

    reservation.release.assert_called_once_with()
    lease.retain.assert_not_called()
    controller.capture_user_memory.assert_not_called()
    assert adapter.capture_tasks == set()


def test_enabled_adapter_rejects_stale_turn_before_reserving_or_retaining() -> None:
    lease = Mock()
    reservation = SimpleNamespace(config_generation=1, release=Mock())
    controller = _controller(reservation=reservation, capture=Mock())
    controller.session_turns.session_lifecycle_snapshot_matches.return_value = False
    adapter = EnabledMemoryAdapter(controller)

    adapter.offer(TurnAccepted(object(), "caption", "session-1", 0, lease))

    controller.reserve_memory_attachment_capture.assert_not_called()
    lease.retain.assert_not_called()
    controller.capture_user_memory.assert_not_called()
    assert adapter.capture_tasks == set()
