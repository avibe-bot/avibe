from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    controller.scheduled_task_service = SimpleNamespace(
        start=Mock(),
        notify_transport_ready=Mock(),
        recover_processing_requests=Mock(),
    )
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(return_value=[]),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller.model_hub_service = SimpleNamespace(
        reconcile_runtime_installation=AsyncMock()
    )
    controller._get_idle_cleanup_timeouts = Mock(return_value=(0, 0))
    controller.cleanup_task = None
    controller._delivery_recovery_complete = asyncio.Event()
    # This is the only test here that gets past the readiness boundary inside
    # `_on_runtime_ready()`, and the announcement asks the IM runtime whether it
    # died first. Publishing itself is a no-op in a process that does not hold
    # the service lock, which no test process does.
    controller._im_run_exception = None

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
    controller.scheduled_task_service.recover_processing_requests.assert_called_once_with()
    controller.runtime_work_supervisor.activate.assert_awaited_once_with()
    controller.model_hub_service.reconcile_runtime_installation.assert_awaited_once_with()
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
    controller.session_turns = SimpleNamespace(
        notify_transport_ready=AsyncMock(return_value=0)
    )
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.runtime_work_supervisor = SimpleNamespace(notify=Mock())
    controller._delivery_recovery_complete = asyncio.Event()
    controller._delivery_recovery_complete.set()

    asyncio.run(controller._on_im_ready(platform="discord"))

    opencode_agent.restore_active_polls.assert_awaited_once_with({"discord", ""})
    controller.scheduled_task_service.notify_transport_ready.assert_called_once_with("discord")
    # Interruption reports held during recovery are owed to this transport.
    controller.session_turns.notify_transport_ready.assert_awaited_once_with("discord")
    controller.update_checker.notify_transport_ready.assert_called_once_with("discord")
    controller.update_checker.check_and_send_post_update_notification.assert_awaited_once_with(
        ready_platform="discord"
    )
    controller.update_checker.start.assert_not_called()
    controller.scheduled_task_service.start.assert_not_called()
    controller.watch_service.start.assert_not_called()
    controller.runtime_command_watcher.start.assert_not_awaited()
    controller.runtime_work_supervisor.notify.assert_not_called()


def test_transport_ready_registers_polls_before_owner_recovery() -> None:
    async def exercise() -> None:
        controller = Controller.__new__(Controller)
        restore_entered = asyncio.Event()

        async def restore_active_polls(platforms: set[str]) -> int:
            assert platforms == {"slack", ""}
            restore_entered.set()
            return 1

        controller.agent_service = SimpleNamespace(
            agents={
                "opencode": SimpleNamespace(
                    restore_active_polls=AsyncMock(side_effect=restore_active_polls)
                )
            }
        )
        controller.primary_platform = "slack"
        controller.update_checker = SimpleNamespace(
            check_and_send_post_update_notification=AsyncMock(return_value=True),
            notify_transport_ready=Mock(),
        )
        controller.scheduled_task_service = SimpleNamespace(
            notify_transport_ready=Mock()
        )
        controller.session_turns = SimpleNamespace(
            notify_transport_ready=AsyncMock(return_value=0)
        )
        controller._delivery_recovery_complete = asyncio.Event()

        ready = asyncio.create_task(controller._on_im_ready(platform="slack"))
        await restore_entered.wait()

        assert not ready.done()
        controller.session_turns.notify_transport_ready.assert_not_awaited()
        controller.scheduled_task_service.notify_transport_ready.assert_not_called()
        controller.update_checker.notify_transport_ready.assert_not_called()
        controller.update_checker.check_and_send_post_update_notification.assert_not_awaited()

        controller._delivery_recovery_complete.set()
        await ready

        controller.scheduled_task_service.notify_transport_ready.assert_called_once_with(
            "slack"
        )
        controller.session_turns.notify_transport_ready.assert_awaited_once_with("slack")
        controller.update_checker.notify_transport_ready.assert_called_once_with("slack")

    asyncio.run(exercise())


def test_runtime_owner_recovery_fails_closed_after_delivery_failure() -> None:
    controller = Controller.__new__(Controller)
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(side_effect=RuntimeError("owner recovery failed")),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller.scheduled_task_service = SimpleNamespace(
        recover_processing_requests=Mock()
    )
    controller._delivery_recovery_complete = asyncio.Event()

    with pytest.raises(RuntimeError, match="owner recovery failed"):
        asyncio.run(controller._recover_runtime_owners())

    controller.session_turns.recover_durable_delivery_state.assert_awaited_once_with(
        service_restart=True
    )
    controller.session_turns.recover_persisted_agent_run_queue.assert_not_awaited()
    controller.scheduled_task_service.recover_processing_requests.assert_not_called()
    controller.runtime_work_supervisor.activate.assert_not_awaited()
    assert not controller._delivery_recovery_complete.is_set()


def test_runtime_owner_recovery_isolates_optional_model_hub_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    controller = Controller.__new__(Controller)
    controller.model_hub_service = SimpleNamespace(
        reconcile_runtime_installation=AsyncMock(
            side_effect=OSError("runtime directory is unwritable")
        )
    )
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(return_value=[]),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.scheduled_task_service = SimpleNamespace(
        recover_processing_requests=Mock()
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller._delivery_recovery_complete = asyncio.Event()

    with caplog.at_level("ERROR"):
        asyncio.run(controller._recover_runtime_owners())

    controller.model_hub_service.reconcile_runtime_installation.assert_awaited_once_with()
    controller.session_turns.recover_durable_delivery_state.assert_awaited_once_with(
        service_restart=True
    )
    controller.session_turns.recover_persisted_agent_run_queue.assert_awaited_once_with()
    controller.scheduled_task_service.recover_processing_requests.assert_called_once_with()
    controller.runtime_work_supervisor.activate.assert_awaited_once_with()
    assert controller._delivery_recovery_complete.is_set()
    assert "Model Hub runtime recovery failed" in caplog.text


def test_runtime_ready_fails_closed_after_queue_recovery_failure() -> None:
    controller = Controller.__new__(Controller)
    controller.agent_service = SimpleNamespace(agents={})
    controller.primary_platform = "discord"
    controller.update_checker = SimpleNamespace(
        check_and_send_post_update_notification=AsyncMock(),
        start=Mock(),
    )
    controller.scheduled_task_service = SimpleNamespace(
        start=Mock(),
        recover_processing_requests=Mock(),
    )
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(return_value=[]),
        recover_persisted_agent_run_queue=AsyncMock(
            side_effect=RuntimeError("queue recovery failed")
        ),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller._delivery_recovery_complete = asyncio.Event()
    controller._shutdown_requested = False
    controller._loop = None

    with pytest.raises(RuntimeError, match="queue recovery failed"):
        asyncio.run(controller._on_runtime_ready())

    controller.runtime_work_supervisor.activate.assert_not_awaited()
    controller.scheduled_task_service.recover_processing_requests.assert_not_called()
    controller.scheduled_task_service.start.assert_not_called()
    controller.watch_service.start.assert_not_called()
    controller.runtime_command_watcher.start.assert_not_awaited()
    controller.update_checker.start.assert_not_called()
    assert not controller._delivery_recovery_complete.is_set()
    assert controller._shutdown_requested is True


def test_runtime_ready_fails_closed_after_fallback_recovery_failure() -> None:
    controller = Controller.__new__(Controller)
    controller.agent_service = SimpleNamespace(agents={})
    controller.primary_platform = "discord"
    controller.update_checker = SimpleNamespace(
        check_and_send_post_update_notification=AsyncMock(),
        start=Mock(),
    )
    controller.scheduled_task_service = SimpleNamespace(
        start=Mock(),
        recover_processing_requests=Mock(
            side_effect=RuntimeError("fallback recovery failed")
        ),
    )
    controller.watch_service = SimpleNamespace(start=Mock())
    controller.runtime_command_watcher = SimpleNamespace(start=AsyncMock())
    controller.session_turns = SimpleNamespace(
        recover_durable_delivery_state=AsyncMock(return_value=[]),
        recover_persisted_agent_run_queue=AsyncMock(return_value=[]),
    )
    controller.runtime_work_supervisor = SimpleNamespace(activate=AsyncMock())
    controller._delivery_recovery_complete = asyncio.Event()
    controller._shutdown_requested = False
    controller._loop = None

    with pytest.raises(RuntimeError, match="fallback recovery failed"):
        asyncio.run(controller._on_runtime_ready())

    controller.runtime_work_supervisor.activate.assert_not_awaited()
    controller.scheduled_task_service.start.assert_not_called()
    controller.watch_service.start.assert_not_called()
    controller.runtime_command_watcher.start.assert_not_awaited()
    controller.update_checker.start.assert_not_called()
    assert not controller._delivery_recovery_complete.is_set()
    assert controller._shutdown_requested is True
