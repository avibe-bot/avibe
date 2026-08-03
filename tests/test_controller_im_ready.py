from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.controller import Controller


def test_runtime_services_start_when_post_update_notification_fails() -> None:
    controller = Controller.__new__(Controller)
    opencode_agent = SimpleNamespace(restore_active_polls=AsyncMock(return_value=0))
    controller.agent_service = SimpleNamespace(agents={"opencode": opencode_agent})
    controller.primary_platform = "discord"
    controller.update_checker = SimpleNamespace(
        check_and_send_post_update_notification=AsyncMock(side_effect=ConnectionError("transport unavailable")),
        start=Mock(),
    )
    controller.scheduled_task_service = SimpleNamespace(start=Mock(), notify_transport_ready=Mock())
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(return_value=[]),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller._get_idle_cleanup_timeouts = Mock(return_value=(0, 0))
    controller.cleanup_task = None
    controller._delivery_recovery_complete = asyncio.Event()

    asyncio.run(controller._on_runtime_ready())

    opencode_agent.restore_active_polls.assert_awaited_once_with({"avibe"})
    controller.update_checker.check_and_send_post_update_notification.assert_awaited_once_with(
        ready_platform="avibe"
    )
    controller.update_checker.start.assert_called_once_with()
    controller.scheduled_task_service.start.assert_called_once_with()
    controller.watch_service.start.assert_called_once_with()
    controller.runtime_command_watcher.start.assert_awaited_once_with()
    controller.session_turns.recover_durable_delivery_state.assert_awaited_once_with(
        service_restart=True
    )
    controller.session_turns.recover_persisted_agent_run_queue.assert_awaited_once_with()
    controller.runtime_work_supervisor.activate.assert_awaited_once_with()
    assert controller._delivery_recovery_complete.is_set()


def test_transport_ready_restores_only_its_state() -> None:
    controller = Controller.__new__(Controller)
    opencode_agent = SimpleNamespace(restore_active_polls=AsyncMock(return_value=1))
    controller.agent_service = SimpleNamespace(agents={"opencode": opencode_agent})
    controller.primary_platform = "discord"
    controller.update_checker = SimpleNamespace(
        check_and_send_post_update_notification=AsyncMock(return_value=True),
        notify_transport_ready=Mock(),
        start=Mock(),
    )
    controller.scheduled_task_service = SimpleNamespace(start=Mock(), notify_transport_ready=Mock())
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.runtime_work_supervisor = SimpleNamespace(notify=Mock())

    asyncio.run(controller._on_im_ready(platform="discord"))

    opencode_agent.restore_active_polls.assert_awaited_once_with({"discord", ""})
    controller.scheduled_task_service.notify_transport_ready.assert_called_once_with("discord")
    controller.update_checker.notify_transport_ready.assert_called_once_with("discord")
    controller.update_checker.check_and_send_post_update_notification.assert_awaited_once_with(
        ready_platform="discord"
    )
    controller.update_checker.start.assert_not_called()
    controller.scheduled_task_service.start.assert_not_called()
    controller.watch_service.start.assert_not_called()
    controller.runtime_command_watcher.start.assert_not_awaited()
    controller.runtime_work_supervisor.notify.assert_not_called()


def test_runtime_owner_recovery_skips_queue_after_delivery_failure() -> None:
    controller = Controller.__new__(Controller)
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(side_effect=RuntimeError("owner recovery failed")),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller._delivery_recovery_complete = asyncio.Event()

    asyncio.run(controller._recover_runtime_owners())

    controller.session_turns.recover_durable_delivery_state.assert_awaited_once_with(
        service_restart=True
    )
    controller.session_turns.recover_persisted_agent_run_queue.assert_not_awaited()
    controller.runtime_work_supervisor.activate.assert_awaited_once_with()
    assert controller._delivery_recovery_complete.is_set()
