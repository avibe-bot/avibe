from __future__ import annotations

import sys
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.message_dispatcher as message_dispatcher_module
from core.message_dispatcher import (
    ActivityOutputDeliveryError,
    ConsolidatedMessageDispatcher,
)
from core.message_output import MessageOutput
from core.session_activities import SessionActivityRegistry, activity_completion_output
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.session_activities import SQLiteSessionActivityStore


class _StubIMClient:
    def __init__(self):
        self.sent = []
        self._next_id = 1

    def should_use_thread_for_reply(self):
        return False

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent.append((context.channel_id, context.thread_id, text))
        message_id = f"bot-msg-{self._next_id}"
        self._next_id += 1
        return message_id

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        message_id = f"bot-msg-{self._next_id}"
        self._next_id += 1
        return message_id


class _FailingIMClient(_StubIMClient):
    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        raise RuntimeError("send failed")

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        raise RuntimeError("button send failed")


class _StubSettingsManager:
    def _canonicalize_message_type(self, message_type):
        return message_type

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class _StubSessionHandler:
    def __init__(self):
        self.calls = []

    def finalize_scheduled_delivery(self, context, sent_message_id):
        self.calls.append((context.channel_id, context.thread_id, sent_message_id))


class _StubController:
    def __init__(self):
        self.config = type("Config", (), {"platform": "slack", "reply_enhancements": False})()
        self.session_handler = _StubSessionHandler()
        self.im_client = _StubIMClient()

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"slack::{context.channel_id}"

    def get_settings_manager_for_context(self, context):
        return _StubSettingsManager()

    def get_im_client_for_context(self, context):
        return self.im_client

    def mark_turn_complete(self, context):
        pass


class MessageDispatcherScheduledTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonterminal_agent_output_records_run_activity(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-live",
            },
        )
        calls = []

        class _Store:
            def record_run_activity(self, run_ids):
                calls.append(list(run_ids))

        controller.scheduled_task_service = SimpleNamespace(request_store=_Store())
        await dispatcher.emit_agent_message(context, "system", "working")

        self.assertEqual(calls, [["run-live"]])

    async def test_detached_output_uses_explicit_run_lineage_over_receiver_context(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda run_id: False),
            emit_matches_runtime_turn=lambda context: False,
            release_runtime_turn=lambda context: self.fail("detached output released current Turn"),
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-stale-receiver",
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "older-turn",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                calls.append((run_id, kwargs["terminal_status"]))

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
        ):
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "background work finished",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=True,
                    detached=True,
                    idempotency_key="activity-complete",
                    run_id="run-origin",
                    metadata={"run_ids": ["run-origin", "run-coalesced"]},
                ),
            )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("run-origin", "succeeded"),
                ("run-coalesced", "succeeded"),
            ],
        )

    async def test_empty_detached_result_can_complete_origin_run_without_current_turn(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda run_id: False),
            emit_matches_runtime_turn=lambda context: False,
            release_runtime_turn=lambda context: self.fail("detached output released current Turn"),
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-empty",
                "agent_runtime_turn_key": "runtime-1",
                "agent_runtime_turn_token": "older-turn",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                calls.append((run_id, kwargs["text"], kwargs["terminal_status"]))

            def close(self):
                pass

        with patch.object(
            message_dispatcher_module,
            "SQLiteBackgroundTaskStore",
            return_value=_Store(),
        ):
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=True,
                    detached=True,
                    idempotency_key="empty-terminal",
                ),
            )

        self.assertIsNone(message_id)
        self.assertEqual(calls, [("run-empty", "", "succeeded")])

    async def test_terminal_failure_records_explicit_run_error_without_result_text(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: False),
            emit_matches_runtime_turn=lambda _context: True,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-failed",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                calls.append((run_id, kwargs))

            def close(self):
                pass

        with patch.object(
            message_dispatcher_module,
            "SQLiteBackgroundTaskStore",
            return_value=_Store(),
        ):
            await dispatcher.emit_agent_message(
                context,
                "result",
                "",
                is_error=True,
                level="silent",
                output=MessageOutput(completes_turn=True, completes_run=True),
                terminal_error="provider unavailable",
            )

        self.assertEqual(len(calls), 1)
        run_id, payload = calls[0]
        self.assertEqual(run_id, "run-failed")
        self.assertEqual(payload["text"], "")
        self.assertEqual(payload["terminal_status"], "failed")
        self.assertEqual(payload["error"], "provider unavailable")

    async def test_blocking_activity_defers_terminal_error_with_status(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: True),
            emit_matches_runtime_turn=lambda _context: True,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-failed",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def defer_run_terminal(self, run_id, **kwargs):
                calls.append(("defer", run_id, kwargs))

            def record_run_output(self, run_id, **kwargs):
                calls.append(("output", run_id, kwargs))

            def close(self):
                pass

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            await dispatcher.emit_agent_message(
                context,
                "result",
                "",
                is_error=True,
                level="silent",
                output=MessageOutput(completes_turn=True, completes_run=True),
                terminal_error="provider unavailable",
            )

        self.assertEqual(
            calls[0],
            (
                "defer",
                "run-failed",
                {
                    "terminal_status": "failed",
                    "result_text": "",
                    "error": "provider unavailable",
                },
            ),
        )
        self.assertEqual(calls[1][0:2], ("output", "run-failed"))
        self.assertIsNone(calls[1][2]["terminal_status"])
        self.assertEqual(calls[1][2]["error"], "provider unavailable")

    async def test_notify_output_uses_stable_persistence_identity(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        persisted_ids = set()
        persisted = []

        def exists(_context, native_message_id):
            return native_message_id in persisted_ids

        def persist(_context, message_type, text, **kwargs):
            native_message_id = kwargs["native_message_id"]
            persisted_ids.add(native_message_id)
            persisted.append((message_type, text, kwargs))
            return {"id": "row-1"}

        output = MessageOutput(
            idempotency_key="backend-failure:turn-1",
            metadata={"backend": "codex", "event": "backend_failure"},
        )
        with (
            patch.object(message_dispatcher_module, "agent_message_exists", side_effect=exists),
            patch.object(message_dispatcher_module, "persist_agent_message", side_effect=persist),
        ):
            first = await dispatcher.emit_agent_message(
                context,
                "notify",
                "Codex failed",
                output=output,
            )
            second = await dispatcher.emit_agent_message(
                context,
                "notify",
                "Codex failed",
                output=output,
            )

        self.assertEqual(first, "bot-msg-1")
        self.assertTrue(str(second).startswith("agent-output:codex:"))
        self.assertEqual(controller.im_client.sent, [("C123", None, "Codex failed")])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0][0:2], ("notify", "Codex failed"))
        self.assertEqual(persisted[0][2]["metadata"]["event"], "backend_failure")

    async def test_activity_run_settlement_waits_for_successful_delivery(self):
        controller = _StubController()
        controller.im_client = _FailingIMClient()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: False),
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        recorded = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                recorded.append((run_id, kwargs))

            def close(self):
                pass

        with (
            patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
            patch.object(message_dispatcher_module, "persist_agent_message") as persist,
        ):
            with self.assertRaisesRegex(RuntimeError, "not durably persisted") as raised:
                await dispatcher.emit_agent_message(
                    context,
                    "result",
                    "background work finished",
                    output=MessageOutput(
                        completes_turn=False,
                        completes_run=True,
                        detached=True,
                        idempotency_key="activity-output",
                        run_id="run-origin",
                        requires_delivery_for_run_settlement=True,
                    ),
                )

        self.assertIs(raised.exception.delivered, False)
        self.assertEqual(recorded, [])
        persist.assert_not_called()

    async def test_delivered_activity_local_failure_settles_runs_then_origin_turn(self):
        events = []
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: False),
            emit_matches_runtime_turn=lambda _context: True,
            release_runtime_turn=lambda _context: None,
        )
        done = asyncio.Event()
        sink = {
            "on_chunk": unittest.mock.AsyncMock(side_effect=lambda _chunk: events.append("turn")),
            "done_event": done,
            "turn_token": "origin-turn",
        }
        controller.get_turn_sink = lambda _session_key: sink
        controller.session_turns = SimpleNamespace(
            on_terminal_result=lambda *_args, **_kwargs: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={"turn_token": "origin-turn"},
        )

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                events.append(("run", run_id, kwargs["terminal_status"]))

            def close(self):
                pass

        def persist(*_args, **_kwargs):
            events.append("persist")
            return None

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                return_value=False,
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                side_effect=persist,
            ),
        ):
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "background work finished",
                output=MessageOutput(
                    completes_turn=True,
                    completes_run=True,
                    idempotency_key="activity-output",
                    run_id="run-origin",
                    metadata={"run_ids": ["run-origin", "run-linked"]},
                    requires_delivery_for_run_settlement=True,
                ),
            )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertTrue(done.is_set())
        self.assertEqual(
            events,
            [
                "persist",
                ("run", "run-origin", "succeeded"),
                ("run", "run-linked", "succeeded"),
                "turn",
            ],
        )

    async def test_activity_run_store_failure_propagates_after_message_persistence(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: False),
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        events = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **_kwargs):
                events.append(("run", run_id))
                raise RuntimeError("run store unavailable")

            def close(self):
                pass

        def persist(*_args, **_kwargs):
            events.append(("message", "persisted"))
            return {"id": "message-row"}

        with (
            patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
            patch.object(message_dispatcher_module, "persist_agent_message", side_effect=persist),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "linked Run settlement failed",
            ) as raised:
                await dispatcher.emit_agent_message(
                    context,
                    "result",
                    "background work finished",
                    output=MessageOutput(
                        completes_turn=False,
                        completes_run=True,
                        detached=True,
                        idempotency_key="activity-output",
                        run_id="run-origin",
                        requires_delivery_for_run_settlement=True,
                    ),
                )

        self.assertIs(raised.exception.delivered, True)
        self.assertIs(raised.exception.durable, True)
        self.assertEqual(str(raised.exception.cause), "run store unavailable")
        self.assertEqual(events, [("message", "persisted"), ("run", "run-origin")])

    async def test_recovered_activity_mirror_failure_sends_once_across_drains(self):
        from core.scheduled_tasks import ScheduledTaskService

        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        activity = registry.start(
            backend="claude",
            runtime_key="runtime-recovered",
            session_id="sess-recovered",
            activity_id="activity-recovered",
            kind="local_agent",
            run_id="run-origin",
        )
        activity = registry.complete(
            backend="claude",
            runtime_key="runtime-recovered",
            activity_id="activity-recovered",
            status="completed",
            metadata={"summary": "Recovered output"},
            expects_output=True,
        )
        self.assertIsNotNone(activity)
        activity_key = registry._activity_key(activity)
        with registry._lock:
            registry._add_recovered_output_id(activity_key, activity)

        service = object.__new__(ScheduledTaskService)
        service.controller = controller
        service._activity_registry = lambda: registry
        service._settle_pending_recovered_activity_terminals = lambda: None

        async def deliver_recovered(claimed):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                    platform_specific={"task_trigger_kind": "activity_recovery"},
                ),
                "result",
                "Recovered output",
                output=activity_completion_output(
                    claimed,
                    detached=True,
                    completes_turn=False,
                ),
            )
            if message_id is None:
                raise RuntimeError("recovered Activity output was not delivered")
            registry.ack_completed_output(claimed)

        service._deliver_recovered_activity_output = deliver_recovered

        class _Store:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                return None

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(message_dispatcher_module, "persist_agent_message", return_value=None),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
        ):
            await service._drain_recovered_activity_outputs()
            await service._drain_recovered_activity_outputs()

        self.assertEqual(
            controller.im_client.sent,
            [("C123", None, "Recovered output")],
        )
        self.assertFalse(
            registry.has_completed_output("claude", "runtime-recovered")
        )

    async def test_durable_activity_retries_run_settlement_without_second_send(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-durable",
            session_id="sess-durable",
            activity_id="activity-durable",
            kind="local_agent",
            run_id="run-origin",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-durable",
            activity_id="activity-durable",
            status="completed",
            metadata={"summary": "Durable output"},
            expects_output=True,
        )
        activity = registry.claim_completed_output("claude", "runtime-durable")
        self.assertIsNotNone(activity)
        output = activity_completion_output(
            activity,
            detached=True,
            completes_turn=False,
        )
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        durable = False
        run_attempts = []

        def persist(*_args, **_kwargs):
            nonlocal durable
            durable = True
            return {"id": "message-row"}

        class _Store:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **_kwargs):
                run_attempts.append(run_id)
                if len(run_attempts) == 1:
                    raise RuntimeError("run store unavailable")

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                side_effect=persist,
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=lambda *_args: durable,
            ),
        ):
            with self.assertRaises(ActivityOutputDeliveryError) as raised:
                await dispatcher.emit_agent_message(
                    context,
                    "result",
                    "Durable output",
                    output=output,
                )
            self.assertTrue(raised.exception.delivered)
            self.assertTrue(raised.exception.durable)
            self.assertTrue(registry.requeue_completed_output(activity))

            retried = registry.claim_completed_output("claude", "runtime-durable")
            self.assertIsNotNone(retried)
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "Durable output",
                output=activity_completion_output(
                    retried,
                    detached=True,
                    completes_turn=False,
                ),
            )
            self.assertFalse(registry.ack_completed_output(retried))

        self.assertTrue(str(message_id).startswith("agent-output:claude:"))
        self.assertEqual(controller.im_client.sent, [("C123", None, "Durable output")])
        self.assertEqual(run_attempts, ["run-origin", "run-origin"])
        self.assertEqual(activity.status, "completed")
        self.assertFalse(registry.has_completed_output("claude", "runtime-durable"))

    async def test_duplicate_error_activity_uses_accepted_payload_and_status(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-canonical",
            session_id="sess-canonical",
            activity_id="activity-canonical",
            kind="local_agent",
            turn_id="turn-canonical",
            run_id="run-canonical",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-canonical",
            activity_id="activity-canonical",
            status="completed",
            metadata={"summary": "Short summary"},
            expects_output=True,
        )
        activity = registry.claim_completed_output("claude", "runtime-canonical")
        self.assertIsNotNone(activity)
        output = activity_completion_output(
            activity,
            detached=True,
            completes_turn=False,
        )
        accepted = {
            "id": "message-row",
            "type": "error",
            "native_message_id": output.native_message_id(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                )
            ),
            "text": "Exact accepted assistant result",
            "content": {"result_footer": "3.4s | 812 tok"},
            "metadata": {
                "activity_ids": [activity.id],
                "run_ids": ["run-canonical"],
            },
        }
        recorded = []

        class _Store:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                recorded.append((run_id, kwargs))

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                return_value=accepted,
            ),
        ):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                ),
                "result",
                "Short summary",
                result_footer="wrong reconstructed footer",
                output=output,
            )

        self.assertEqual(message_id, accepted["native_message_id"])
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(
            recorded[0][1]["text"],
            "Exact accepted assistant result\n\n3.4s | 812 tok",
        )
        self.assertEqual(recorded[0][1]["terminal_status"], "failed")
        self.assertFalse(registry.has_completed_output("claude", "runtime-canonical"))

    async def test_failed_registry_batch_settlement_is_a_delivered_incomplete_outcome(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
            on_activity_terminal=lambda _activity: False,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-incomplete",
            session_id="sess-incomplete",
            activity_id="activity-incomplete",
            kind="local_agent",
            run_id="run-incomplete",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-incomplete",
            activity_id="activity-incomplete",
            status="completed",
            expects_output=True,
        )
        activity = registry.claim_completed_output("claude", "runtime-incomplete")
        self.assertIsNotNone(activity)

        class _Store:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                raise RuntimeError("run store unavailable")

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_Store(),
            ),
            patch.object(message_dispatcher_module, "persist_agent_message", return_value=None),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=None),
        ):
            with self.assertRaises(ActivityOutputDeliveryError) as raised:
                await dispatcher.emit_agent_message(
                    MessageContext(
                        user_id="scheduled",
                        channel_id="C123",
                        platform="slack",
                    ),
                    "result",
                    "Delivered but locally incomplete",
                    output=activity_completion_output(
                        activity,
                        detached=True,
                        completes_turn=False,
                    ),
                )

        self.assertTrue(raised.exception.delivered)
        self.assertFalse(raised.exception.durable)
        self.assertEqual(
            controller.im_client.sent,
            [("C123", None, "Delivered but locally incomplete")],
        )

    async def test_recovered_durable_message_retries_only_local_settlement_after_restart(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.record = None

            def upsert_activity(self, activity, *, phase):
                self.record = {"activity": dict(activity), "phase": phase}

            def delete_activity(self, **_kwargs):
                self.record = None

            def list_activities(self):
                return [self.record] if self.record is not None else []

        activity_store = _ActivityStore()
        original = SessionActivityRegistry(activity_store)
        original.start(
            backend="claude",
            runtime_key="runtime-restarted",
            session_id="sess-restarted",
            activity_id="activity-restarted",
            kind="local_agent",
            run_id="run-origin",
        )
        original.complete(
            backend="claude",
            runtime_key="runtime-restarted",
            activity_id="activity-restarted",
            status="completed",
            metadata={"summary": "Recovered durable output"},
            expects_output=True,
        )
        self.assertEqual(activity_store.record["phase"], "awaiting_output")

        settlement_calls = []

        class _RecordingRegistry(SessionActivityRegistry):
            def settle_completed_output_batch(self, output, **kwargs):
                settlement_calls.append(
                    (output.activity_ids, kwargs["accepted_message_exists"])
                )
                return super().settle_completed_output_batch(output, **kwargs)

        recovered = _RecordingRegistry(activity_store)
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=recovered,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        service = object.__new__(ScheduledTaskService)
        service.controller = controller
        service._activity_registry = lambda: recovered
        service._settle_pending_recovered_activity_terminals = lambda: None
        run_attempts = []
        observed_native_ids = []

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **_kwargs):
                run_attempts.append(run_id)
                if len(run_attempts) == 1:
                    raise RuntimeError("run store unavailable")

            def close(self):
                pass

        async def deliver_recovered(activity):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                    platform_specific={"task_trigger_kind": "activity_recovery"},
                ),
                "result",
                "Recovered durable output",
                output=activity_completion_output(
                    activity,
                    detached=True,
                    completes_turn=False,
                ),
            )
            self.assertIsNotNone(message_id)
            recovered.ack_completed_output(activity)

        service._deliver_recovered_activity_output = deliver_recovered

        def accepted_message_exists(_context, native_message_id):
            observed_native_ids.append(native_message_id)
            return True

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=accepted_message_exists,
            ),
            patch.object(message_dispatcher_module, "persist_agent_message") as persist,
        ):
            await service._drain_recovered_activity_outputs()
            self.assertTrue(
                recovered.has_completed_output("claude", "runtime-restarted")
            )
            await service._drain_recovered_activity_outputs()

        self.assertEqual(controller.im_client.sent, [])
        persist.assert_not_called()
        self.assertEqual(run_attempts, ["run-origin", "run-origin"])
        self.assertTrue(observed_native_ids)
        self.assertEqual(len(set(observed_native_ids)), 1)
        self.assertIn(
            ":activity-batch:claude:runtime-restarted:batch:",
            observed_native_ids[0],
        )
        self.assertIn(
            ":claude-activity-output:claude:runtime-restarted:batch:",
            observed_native_ids[0],
        )
        self.assertTrue(observed_native_ids[0].endswith(":completion"))
        self.assertIn((("activity-restarted",), True), settlement_calls)
        self.assertFalse(
            recovered.has_completed_output("claude", "runtime-restarted")
        )
        self.assertEqual(activity_store.list_activities(), [])

    async def test_real_recovery_drains_one_complete_persisted_batch_once(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.records = {}

            def upsert_activity(self, activity, *, phase):
                self.records[activity["id"]] = {
                    "activity": dict(activity),
                    "phase": phase,
                }

            def upsert_activities(self, activities, *, phase):
                for activity in activities:
                    self.upsert_activity(activity, phase=phase)

            def delete_activity(self, *, activity_id, **_kwargs):
                self.records.pop(activity_id, None)

            def list_activities(self):
                return list(self.records.values())

        activity_store = _ActivityStore()
        original = SessionActivityRegistry(activity_store)
        for activity_id, run_id, summary in (
            ("activity-a", "run-a", "Earlier summary"),
            ("activity-b", "run-b", "Canonical batch summary"),
        ):
            original.start(
                backend="claude",
                runtime_key="runtime-recovery-batch",
                session_id="sess-recovery-batch",
                activity_id=activity_id,
                kind="local_agent",
                turn_id="turn-recovery-batch",
                run_id=run_id,
            )
            original.complete(
                backend="claude",
                runtime_key="runtime-recovery-batch",
                activity_id=activity_id,
                status="completed",
                metadata={"summary": summary},
                expects_output=True,
            )
        bound = original.claim_completed_output_batch(
            "claude",
            "runtime-recovery-batch",
        )
        self.assertEqual([activity.id for activity in bound], ["activity-a", "activity-b"])

        recovered = SessionActivityRegistry(activity_store)
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=recovered,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        controller.emit_agent_message = dispatcher.emit_agent_message
        service = object.__new__(ScheduledTaskService)
        service.controller = controller
        service._activity_registry = lambda: recovered
        service._settle_pending_recovered_activity_terminals = lambda: None
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        service._build_context = AsyncMock(return_value=context)
        accepted_by_native_id = {}
        accepted = []
        run_settlements = []

        def persist_message(_context, _message_type, text, **kwargs):
            message = {
                "id": "accepted-recovery-batch",
                "native_message_id": kwargs["native_message_id"],
                "text": text,
                "content": {"result_footer": kwargs["result_footer"]},
                "metadata": kwargs["metadata"],
            }
            accepted_by_native_id[kwargs["native_message_id"]] = message
            accepted.append(message)
            return message

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                run_settlements.append((run_id, kwargs["text"]))

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=lambda _context, identity: accepted_by_native_id.get(
                    identity
                ),
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                side_effect=persist_message,
            ),
            patch(
                "core.scheduled_tasks.resolve_session_id_target",
                return_value=SimpleNamespace(
                    session_key=SimpleNamespace(platform="slack"),
                    agent_name="claude",
                ),
            ),
        ):
            await service._drain_recovered_activity_outputs()
            await service._drain_recovered_activity_outputs()

        self.assertEqual(
            controller.im_client.sent,
            [("C123", None, "Canonical batch summary")],
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["text"], "Canonical batch summary")
        self.assertEqual(
            accepted[0]["metadata"]["activity_ids"],
            ["activity-a", "activity-b"],
        )
        self.assertEqual(
            accepted[0]["metadata"]["run_ids"],
            ["run-a", "run-b"],
        )
        self.assertEqual(
            run_settlements,
            [
                ("run-a", "Canonical batch summary"),
                ("run-b", "Canonical batch summary"),
            ],
        )
        self.assertFalse(
            recovered.has_completed_output("claude", "runtime-recovery-batch")
        )
        self.assertEqual(activity_store.list_activities(), [])

    async def test_sqlite_recovery_preserves_canonical_batch_order(self):
        from core.scheduled_tasks import ScheduledTaskService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state" / "vibe.sqlite"
            ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
            engine = create_sqlite_engine(db_path)
            activity_store = SQLiteSessionActivityStore(engine)
            original = SessionActivityRegistry(activity_store)

            for activity_id, run_id in (
                ("activity-a", "run-a"),
                ("activity-b", "run-b"),
            ):
                original.start(
                    backend="claude",
                    runtime_key="runtime-sqlite-recovery-order",
                    session_id="sess-sqlite-recovery-order",
                    activity_id=activity_id,
                    kind="local_agent",
                    turn_id="turn-sqlite-recovery-order",
                    run_id=run_id,
                )

            for activity_id, summary in (
                ("activity-b", "Earlier completed summary"),
                ("activity-a", "Canonical live batch summary"),
            ):
                original.complete(
                    backend="claude",
                    runtime_key="runtime-sqlite-recovery-order",
                    activity_id=activity_id,
                    status="completed",
                    metadata={"summary": summary},
                    expects_output=True,
                )

            bound = original.claim_completed_output_batch(
                "claude",
                "runtime-sqlite-recovery-order",
            )
            self.assertEqual(
                [activity.id for activity in bound],
                ["activity-b", "activity-a"],
            )
            live_output = activity_completion_output(
                bound[-1],
                activities=bound,
                detached=True,
                completes_turn=False,
            )

            recovered = SessionActivityRegistry(activity_store)
            controller = _StubController()
            controller.agent_service = SimpleNamespace(
                activities=recovered,
                emit_matches_runtime_turn=lambda _context: False,
                release_runtime_turn=lambda _context: None,
            )
            dispatcher = ConsolidatedMessageDispatcher(controller)
            controller.emit_agent_message = dispatcher.emit_agent_message
            service = object.__new__(ScheduledTaskService)
            service.controller = controller
            service._activity_registry = lambda: recovered
            service._settle_pending_recovered_activity_terminals = lambda: None
            context = MessageContext(
                user_id="scheduled",
                channel_id="C123",
                platform="slack",
            )
            service._build_context = AsyncMock(return_value=context)
            accepted_by_native_id = {}
            accepted = []
            run_settlements = []

            def persist_message(_context, _message_type, text, **kwargs):
                message = {
                    "id": "accepted-sqlite-recovery-order",
                    "native_message_id": kwargs["native_message_id"],
                    "text": text,
                    "content": {"result_footer": kwargs["result_footer"]},
                    "metadata": kwargs["metadata"],
                }
                accepted_by_native_id[kwargs["native_message_id"]] = message
                accepted.append(message)
                return message

            class _RunStore:
                def get_run(self, _run_id):
                    return {"status": "running"}

                def record_run_output(self, run_id, **kwargs):
                    run_settlements.append((run_id, kwargs["text"]))

                def close(self):
                    pass

            with (
                patch.object(
                    message_dispatcher_module,
                    "SQLiteBackgroundTaskStore",
                    return_value=_RunStore(),
                ),
                patch.object(
                    message_dispatcher_module,
                    "agent_message_exists",
                    side_effect=lambda _context, identity: accepted_by_native_id.get(
                        identity
                    ),
                ),
                patch.object(
                    message_dispatcher_module,
                    "persist_agent_message",
                    side_effect=persist_message,
                ),
                patch(
                    "core.scheduled_tasks.resolve_session_id_target",
                    return_value=SimpleNamespace(
                        session_key=SimpleNamespace(platform="slack"),
                        agent_name="claude",
                    ),
                ),
            ):
                await service._drain_recovered_activity_outputs()
                await service._drain_recovered_activity_outputs()

            self.assertEqual(
                controller.im_client.sent,
                [("C123", None, "Canonical live batch summary")],
            )
            self.assertEqual(len(accepted), 1)
            self.assertEqual(
                accepted[0]["native_message_id"],
                live_output.native_message_id(context),
            )
            self.assertEqual(
                accepted[0]["metadata"]["activity_ids"],
                ["activity-b", "activity-a"],
            )
            self.assertEqual(
                accepted[0]["metadata"]["run_ids"],
                ["run-b", "run-a"],
            )
            self.assertEqual(
                run_settlements,
                [
                    ("run-b", "Canonical live batch summary"),
                    ("run-a", "Canonical live batch summary"),
                ],
            )
            self.assertFalse(
                recovered.has_completed_output(
                    "claude",
                    "runtime-sqlite-recovery-order",
                )
            )
            self.assertEqual(activity_store.list_activities(), [])
            engine.dispose()

    async def test_silent_recovered_batch_settles_every_linked_run(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.records = {}
                self.fail_cleanup = False

            def upsert_activity(self, activity, *, phase):
                if self.fail_cleanup and phase == "terminal":
                    raise RuntimeError("terminal Activity storage unavailable")
                self.records[activity["id"]] = {
                    "activity": dict(activity),
                    "phase": phase,
                }

            def upsert_activities(self, activities, *, phase):
                for activity in activities:
                    self.upsert_activity(activity, phase=phase)

            def delete_activity(self, *, activity_id, **_kwargs):
                if self.fail_cleanup:
                    raise RuntimeError("Activity deletion unavailable")
                self.records.pop(activity_id, None)

            def list_activities(self):
                return list(self.records.values())

        activity_store = _ActivityStore()
        original = SessionActivityRegistry(activity_store)
        for activity_id, run_id in (
            ("activity-a", "run-a"),
            ("activity-b", "run-b"),
        ):
            original.start(
                backend="claude",
                runtime_key="runtime-silent-batch",
                session_id="sess-silent-batch",
                activity_id=activity_id,
                kind="local_agent",
                turn_id="turn-silent-batch",
                run_id=run_id,
            )
            original.complete(
                backend="claude",
                runtime_key="runtime-silent-batch",
                activity_id=activity_id,
                status="completed",
                metadata={"summary": ""},
                expects_output=True,
            )
        original.claim_completed_output_batch("claude", "runtime-silent-batch")

        recovered = SessionActivityRegistry(activity_store)
        released = []
        recovered.set_output_settled_callback(
            lambda activity: released.append(activity.id)
        )
        activity_store.fail_cleanup = True
        deferred = []
        settled = []

        class _RequestStore:
            def defer_run_terminal(self, run_id, *, terminal_status):
                deferred.append((run_id, terminal_status))

            def settle_deferred_run(self, run_id):
                settled.append(run_id)
                return True

        service = object.__new__(ScheduledTaskService)
        service.controller = SimpleNamespace(
            agent_service=SimpleNamespace(activities=recovered)
        )
        service.request_store = _RequestStore()
        service._drain_dirty = False
        service._activity_registry = lambda: recovered
        service._settle_pending_recovered_activity_terminals = lambda: None

        await service._drain_recovered_activity_outputs()
        await service._drain_recovered_activity_outputs()

        self.assertCountEqual(
            deferred,
            [("run-a", "succeeded"), ("run-b", "succeeded")],
        )
        self.assertEqual(len(deferred), 2)
        self.assertCountEqual(settled, ["run-a", "run-b"])
        self.assertEqual(len(settled), 2)
        self.assertCountEqual(released, ["activity-a", "activity-b"])
        self.assertEqual(len(released), 2)
        self.assertFalse(
            recovered.has_completed_output("claude", "runtime-silent-batch")
        )
        self.assertEqual(
            {record["phase"] for record in activity_store.list_activities()},
            {"awaiting_output"},
        )

    async def test_legacy_recovered_receipt_uses_accepted_message_without_resend(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.records = {}

            def upsert_activity(self, activity, *, phase):
                self.records[activity["id"]] = {
                    "activity": dict(activity),
                    "phase": phase,
                }

            def delete_activity(self, *, activity_id, **_kwargs):
                self.records.pop(activity_id, None)

            def list_activities(self):
                return list(self.records.values())

        activity_store = _ActivityStore()
        original = SessionActivityRegistry(activity_store)
        original.start(
            backend="claude",
            runtime_key="runtime-legacy",
            session_id="sess-legacy",
            activity_id="activity-legacy",
            kind="local_agent",
            run_id="run-legacy",
        )
        original.complete(
            backend="claude",
            runtime_key="runtime-legacy",
            activity_id="activity-legacy",
            status="completed",
            metadata={"summary": "Legacy accepted output"},
            expects_output=True,
        )

        recovered = SessionActivityRegistry(activity_store)
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=recovered,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        controller.emit_agent_message = dispatcher.emit_agent_message
        service = object.__new__(ScheduledTaskService)
        service.controller = controller
        service._activity_registry = lambda: recovered
        service._settle_pending_recovered_activity_terminals = lambda: None
        service._build_context = AsyncMock(
            return_value=MessageContext(
                user_id="scheduled",
                channel_id="C123",
                platform="slack",
            )
        )
        legacy_native_id = (
            "agent-output:claude:run-legacy:"
            "claude-task:runtime-legacy:activity-legacy:completion"
        )
        looked_up = []
        run_settlements = []
        accepted = {
            "id": "accepted-legacy",
            "type": "result",
            "native_message_id": legacy_native_id,
            "text": "Legacy accepted output",
            "metadata": {
                "activity_ids": ["activity-legacy"],
                "run_ids": ["run-legacy"],
                "output_id": (
                    "claude-task:runtime-legacy:activity-legacy:completion"
                ),
            },
        }

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                run_settlements.append((run_id, kwargs["output_id"]))

            def close(self):
                pass

        def find_accepted(_context, native_message_id):
            looked_up.append(native_message_id)
            return accepted if native_message_id == legacy_native_id else None

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=find_accepted,
            ),
            patch.object(message_dispatcher_module, "persist_agent_message") as persist,
            patch(
                "core.scheduled_tasks.resolve_session_id_target",
                return_value=SimpleNamespace(
                    session_key=SimpleNamespace(platform="slack"),
                    agent_name="claude",
                ),
            ),
        ):
            await service._drain_recovered_activity_outputs()
            await service._drain_recovered_activity_outputs()

        self.assertEqual(controller.im_client.sent, [])
        persist.assert_not_called()
        self.assertEqual(looked_up[-1], legacy_native_id)
        self.assertEqual(
            run_settlements,
            [
                (
                    "run-legacy",
                    "claude-task:runtime-legacy:activity-legacy:completion",
                )
            ],
        )
        self.assertFalse(
            recovered.has_completed_output("claude", "runtime-legacy")
        )
        self.assertEqual(activity_store.list_activities(), [])

    async def test_incomplete_recovered_receipt_is_requeued_without_a_send(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.records = {}

            def upsert_activity(self, activity, *, phase):
                self.records[activity["id"]] = {
                    "activity": dict(activity),
                    "phase": phase,
                }

            def upsert_activities(self, activities, *, phase):
                for activity in activities:
                    self.upsert_activity(activity, phase=phase)

            def delete_activity(self, *, activity_id, **_kwargs):
                self.records.pop(activity_id, None)

            def list_activities(self):
                return list(self.records.values())

        activity_store = _ActivityStore()
        original = SessionActivityRegistry(activity_store)
        for activity_id, run_id in (
            ("activity-a", "run-a"),
            ("activity-b", "run-b"),
        ):
            original.start(
                backend="claude",
                runtime_key="runtime-incomplete-receipt",
                session_id="sess-incomplete-receipt",
                activity_id=activity_id,
                kind="local_agent",
                turn_id="turn-incomplete-receipt",
                run_id=run_id,
            )
            original.complete(
                backend="claude",
                runtime_key="runtime-incomplete-receipt",
                activity_id=activity_id,
                status="completed",
                metadata={"summary": f"summary for {activity_id}"},
                expects_output=True,
            )
        original.claim_completed_output_batch(
            "claude",
            "runtime-incomplete-receipt",
        )
        activity_store.records.pop("activity-b")

        recovered = SessionActivityRegistry(activity_store)
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=recovered,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        controller.emit_agent_message = dispatcher.emit_agent_message
        service = object.__new__(ScheduledTaskService)
        service.controller = controller
        service._activity_registry = lambda: recovered
        service._build_context = AsyncMock(
            return_value=MessageContext(
                user_id="scheduled",
                channel_id="C123",
                platform="slack",
            )
        )
        service._settle_pending_recovered_activity_terminals = lambda: None

        with (
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                return_value=None,
            ),
            patch.object(message_dispatcher_module, "persist_agent_message") as persist,
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
            ) as run_store,
            patch(
                "core.scheduled_tasks.resolve_session_id_target",
                return_value=SimpleNamespace(
                    session_key=SimpleNamespace(platform="slack"),
                    agent_name="claude",
                ),
            ),
        ):
            await service._drain_recovered_activity_outputs()

        self.assertEqual(controller.im_client.sent, [])
        persist.assert_not_called()
        run_store.assert_not_called()
        self.assertTrue(
            recovered.has_completed_output("claude", "runtime-incomplete-receipt")
        )

    async def test_batch_receipt_recovers_earlier_stale_member_without_redelivery(self):
        from core.scheduled_tasks import ScheduledTaskService

        class _ActivityStore:
            def __init__(self):
                self.records = {}
                self.fail_earlier_cleanup = False

            def upsert_activity(self, activity, *, phase):
                activity_id = activity["id"]
                if (
                    self.fail_earlier_cleanup
                    and activity_id == "activity-a"
                    and phase == "terminal"
                ):
                    raise RuntimeError("terminal write unavailable")
                self.records[activity_id] = {
                    "activity": dict(activity),
                    "phase": phase,
                }

            def upsert_activities(self, activities, *, phase):
                for activity in activities:
                    self.upsert_activity(activity, phase=phase)

            def delete_activity(self, *, activity_id, **_kwargs):
                if self.fail_earlier_cleanup and activity_id == "activity-a":
                    raise RuntimeError("delete unavailable")
                self.records.pop(activity_id, None)

            def list_activities(self):
                return list(self.records.values())

        activity_store = _ActivityStore()
        registry = SessionActivityRegistry(activity_store)
        settlement_events = []
        settled = asyncio.Event()

        def output_settled(activity):
            settlement_events.append(("claim", activity.id))
            if len(settlement_events) == 4:
                settled.set()

        registry.set_output_settled_callback(output_settled)
        for activity_id, run_id in (
            ("activity-a", "run-a"),
            ("activity-b", "run-b"),
        ):
            registry.start(
                backend="claude",
                runtime_key="runtime-batch-restart",
                session_id="sess-batch-restart",
                activity_id=activity_id,
                kind="local_agent",
                turn_id="turn-batch-restart",
                run_id=run_id,
            )
            registry.complete(
                backend="claude",
                runtime_key="runtime-batch-restart",
                activity_id=activity_id,
                status="completed",
                metadata={"summary": f"summary for {activity_id}"},
                expects_output=True,
            )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-batch-restart",
        )
        output = activity_completion_output(
            claimed[-1],
            activities=claimed,
            detached=True,
            completes_turn=False,
        )
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        native_message_id = output.native_message_id(context)
        accepted_messages = []

        def persist_message(_context, _message_type, text, **kwargs):
            accepted = {
                "id": "accepted-batch-message",
                "native_message_id": kwargs["native_message_id"],
                "text": text,
                "content": {"result_footer": kwargs["result_footer"]},
                "metadata": kwargs["metadata"],
            }
            accepted_messages.append(accepted)
            return accepted

        class _LiveRunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **_kwargs):
                settlement_events.append(("run", run_id))

            def close(self):
                pass

        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        activity_store.fail_earlier_cleanup = True
        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_LiveRunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                return_value=None,
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                side_effect=persist_message,
            ),
        ):
            delivered_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "Canonical batch result",
                output=output,
            )

        self.assertEqual(delivered_id, "bot-msg-1")
        self.assertEqual(controller.im_client.sent, [("C123", None, "Canonical batch result")])
        await asyncio.wait_for(settled.wait(), timeout=1)
        self.assertEqual(
            settlement_events,
            [
                ("run", "run-a"),
                ("run", "run-b"),
                ("claim", "activity-a"),
                ("claim", "activity-b"),
            ],
        )
        self.assertEqual(set(activity_store.records), {"activity-a"})
        stale = activity_store.records["activity-a"]["activity"]
        self.assertEqual(stale["metadata"]["output_batch_id"], output.activity_batch_id)
        accepted = accepted_messages[0]
        self.assertEqual(accepted["native_message_id"], native_message_id)
        self.assertEqual(
            accepted["metadata"]["activity_ids"],
            ["activity-a", "activity-b"],
        )

        activity_store.fail_earlier_cleanup = False
        recovered = SessionActivityRegistry(activity_store)
        restart_runs = []
        observed_native_ids = []

        class _RestartRunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                restart_runs.append((run_id, kwargs["text"]))

            def close(self):
                pass

        recovered_controller = _StubController()
        recovered_controller.agent_service = SimpleNamespace(
            activities=recovered,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        recovered_dispatcher = ConsolidatedMessageDispatcher(recovered_controller)
        recovered_controller.emit_agent_message = recovered_dispatcher.emit_agent_message
        service = object.__new__(ScheduledTaskService)
        service.controller = recovered_controller
        service._activity_registry = lambda: recovered
        service._build_context = AsyncMock(return_value=context)
        service._settle_pending_recovered_activity_terminals = lambda: None

        def accepted_message(_context, identity):
            observed_native_ids.append(identity)
            return accepted

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RestartRunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=accepted_message,
            ),
            patch.object(message_dispatcher_module, "persist_agent_message") as persist,
            patch(
                "core.scheduled_tasks.resolve_session_id_target",
                return_value=SimpleNamespace(
                    session_key=SimpleNamespace(platform="slack"),
                    agent_name="claude",
                ),
            ),
        ):
            await service._drain_recovered_activity_outputs()
            await service._drain_recovered_activity_outputs()

        self.assertEqual(recovered_controller.im_client.sent, [])
        persist.assert_not_called()
        self.assertEqual(
            restart_runs,
            [
                ("run-a", "Canonical batch result"),
                ("run-b", "Canonical batch result"),
            ],
        )
        self.assertEqual(set(observed_native_ids), {native_message_id})
        self.assertFalse(
            recovered.has_completed_output("claude", "runtime-batch-restart")
        )
        self.assertEqual(activity_store.list_activities(), [])

    async def test_separate_flushes_from_one_turn_get_distinct_receipts(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        accepted_by_native_id = {}
        persisted = []
        run_settlements = []

        def persist_message(_context, _message_type, text, **kwargs):
            accepted = {
                "id": f"message-{len(persisted) + 1}",
                "native_message_id": kwargs["native_message_id"],
                "text": text,
                "content": {"result_footer": kwargs["result_footer"]},
                "metadata": kwargs["metadata"],
            }
            accepted_by_native_id[kwargs["native_message_id"]] = accepted
            persisted.append(accepted)
            return accepted

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                run_settlements.append((run_id, kwargs["text"]))

            def close(self):
                pass

        async def flush(activity_id, run_id, summary):
            registry.start(
                backend="claude",
                runtime_key="runtime-two-flushes",
                session_id="sess-two-flushes",
                activity_id=activity_id,
                kind="local_agent",
                turn_id="turn-shared",
                run_id=run_id,
            )
            registry.complete(
                backend="claude",
                runtime_key="runtime-two-flushes",
                activity_id=activity_id,
                status="completed",
                metadata={"summary": summary},
                expects_output=True,
            )
            claimed = registry.claim_completed_output_batch(
                "claude",
                "runtime-two-flushes",
            )
            self.assertEqual([item.id for item in claimed], [activity_id])
            output = activity_completion_output(
                claimed[0],
                activities=claimed,
                detached=True,
                completes_turn=False,
            )
            await dispatcher.emit_agent_message(
                context,
                "result",
                summary,
                output=output,
            )
            return output

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "agent_message_exists",
                side_effect=lambda _context, native_id: accepted_by_native_id.get(native_id),
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                side_effect=persist_message,
            ),
        ):
            first = await flush("activity-first", "run-first", "First result")
            second = await flush("activity-second", "run-second", "Second result")

        self.assertNotEqual(first.activity_batch_id, second.activity_batch_id)
        self.assertNotEqual(
            first.native_message_id(context),
            second.native_message_id(context),
        )
        self.assertEqual(
            controller.im_client.sent,
            [
                ("C123", None, "First result"),
                ("C123", None, "Second result"),
            ],
        )
        self.assertEqual(
            [(item["text"], item["metadata"]["activity_ids"]) for item in persisted],
            [
                ("First result", ["activity-first"]),
                ("Second result", ["activity-second"]),
            ],
        )
        self.assertEqual(
            run_settlements,
            [
                ("run-first", "First result"),
                ("run-second", "Second result"),
            ],
        )

    async def test_invisible_activity_releases_claim_after_local_settlement(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-invisible",
            session_id="sess-invisible",
            activity_id="activity-invisible",
            kind="local_agent",
            run_id="run-invisible",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-invisible",
            activity_id="activity-invisible",
            status="completed",
            metadata={"summary": "<silent>internal only</silent>"},
            expects_output=True,
        )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-invisible",
        )
        settled = asyncio.Event()
        events = []

        def output_settled(activity):
            events.append(("waiter", activity.id, registry.has_completed_output(
                "claude", "runtime-invisible"
            )))
            settled.set()

        registry.set_output_settled_callback(output_settled)

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **_kwargs):
                events.append(("run", run_id))

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                registry,
                "_delete_activity",
                side_effect=RuntimeError("delete unavailable"),
            ),
            patch.object(
                registry,
                "_persist_activity",
                side_effect=RuntimeError("terminal write unavailable"),
            ),
        ):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                ),
                "result",
                "<silent>internal only</silent>",
                level="silent",
                output=activity_completion_output(
                    claimed[0],
                    activities=claimed,
                    detached=True,
                    completes_turn=False,
                ),
            )

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent, [])
        await asyncio.wait_for(settled.wait(), timeout=1)
        self.assertEqual(
            events,
            [
                ("run", "run-invisible"),
                ("waiter", "activity-invisible", False),
            ],
        )
        self.assertFalse(registry.has_completed_output("claude", "runtime-invisible"))
        later = registry.start(
            backend="claude",
            runtime_key="runtime-invisible",
            session_id="sess-invisible",
            activity_id="activity-later",
            kind="local_agent",
            turn_id="turn-later",
        )
        self.assertEqual(later.id, "activity-later")

    async def test_invisible_activity_propagates_incomplete_registry_settlement(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-invisible-incomplete",
            session_id="sess-invisible-incomplete",
            activity_id="activity-invisible-incomplete",
            kind="local_agent",
            run_id="run-invisible-incomplete",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-invisible-incomplete",
            activity_id="activity-invisible-incomplete",
            status="completed",
            expects_output=True,
        )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-invisible-incomplete",
        )

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                return None

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                registry,
                "settle_completed_output_batch",
                return_value=False,
            ),
        ):
            with self.assertRaises(ActivityOutputDeliveryError) as raised:
                await dispatcher.emit_agent_message(
                    MessageContext(
                        user_id="scheduled",
                        channel_id="C123",
                        platform="slack",
                    ),
                    "result",
                    "",
                    level="silent",
                    output=activity_completion_output(
                        claimed[0],
                        activities=claimed,
                        detached=True,
                        completes_turn=False,
                    ),
                )

        self.assertFalse(raised.exception.delivered)
        self.assertEqual(controller.im_client.sent, [])
        self.assertTrue(
            registry.has_completed_output("claude", "runtime-invisible-incomplete")
        )

    async def test_suppressed_activity_propagates_incomplete_registry_settlement(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-suppressed-incomplete",
            session_id="sess-suppressed-incomplete",
            activity_id="activity-suppressed-incomplete",
            kind="local_agent",
            run_id="run-suppressed-incomplete",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-suppressed-incomplete",
            activity_id="activity-suppressed-incomplete",
            status="completed",
            expects_output=True,
        )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-suppressed-incomplete",
        )

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                return None

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                return_value=None,
            ),
            patch.object(
                registry,
                "settle_completed_output_batch",
                return_value=False,
            ),
        ):
            with self.assertRaises(ActivityOutputDeliveryError) as raised:
                await dispatcher.emit_agent_message(
                    MessageContext(
                        user_id="scheduled",
                        channel_id="C123",
                        platform="slack",
                        platform_specific={
                            "suppress_delivery": True,
                            "task_trigger_kind": "agent_run",
                            "task_execution_id": "run-suppressed-incomplete",
                        },
                    ),
                    "result",
                    "private output",
                    output=activity_completion_output(
                        claimed[-1],
                        activities=claimed,
                        detached=True,
                        completes_turn=False,
                    ),
                )

        self.assertFalse(raised.exception.delivered)
        self.assertEqual(controller.im_client.sent, [])
        self.assertTrue(
            registry.has_completed_output(
                "claude",
                "runtime-suppressed-incomplete",
            )
        )

    async def test_suppressed_activity_releases_batch_when_snapshot_cleanup_fails(self):
        controller = _StubController()
        registry = SessionActivityRegistry()
        settled = []
        registry.set_output_settled_callback(
            lambda activity: settled.append(activity.id)
        )
        controller.agent_service = SimpleNamespace(
            activities=registry,
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-suppressed-cleanup",
            session_id="sess-suppressed-cleanup",
            activity_id="activity-suppressed-cleanup",
            kind="local_agent",
            run_id="run-suppressed-cleanup",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-suppressed-cleanup",
            activity_id="activity-suppressed-cleanup",
            status="completed",
            expects_output=True,
        )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-suppressed-cleanup",
        )

        class _RunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                return None

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_RunStore(),
            ),
            patch.object(
                message_dispatcher_module,
                "persist_agent_message",
                return_value=None,
            ),
            patch.object(
                registry,
                "_delete_activity",
                side_effect=RuntimeError("delete unavailable"),
            ),
            patch.object(
                registry,
                "_persist_activity",
                side_effect=RuntimeError("terminal write unavailable"),
            ),
        ):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                    platform_specific={
                        "suppress_delivery": True,
                        "task_trigger_kind": "agent_run",
                        "task_execution_id": "run-suppressed-cleanup",
                    },
                ),
                "result",
                "private output",
                output=activity_completion_output(
                    claimed[-1],
                    activities=claimed,
                    detached=True,
                    completes_turn=False,
                ),
            )

        self.assertEqual(message_id, "suppressed:run-suppressed-cleanup")
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(settled, ["activity-suppressed-cleanup"])
        self.assertFalse(
            registry.has_completed_output("claude", "runtime-suppressed-cleanup")
        )

    async def test_real_scheduler_settles_run_before_activity_claim_is_consumed(self):
        from core.scheduled_tasks import ScheduledTaskService
        from modules.agents.service import AgentService

        controller = _StubController()
        registry = SessionActivityRegistry()
        agent_service = AgentService(controller, activities=registry)
        controller.agent_service = agent_service
        events = []
        terminal_runs = set()

        class _RequestStore:
            def defer_run_terminal(self, run_id, *, terminal_status):
                events.append(("defer", run_id, terminal_status))

            def settle_deferred_run(self, run_id, *, error):
                events.append(("run", run_id, error))
                terminal_runs.add(run_id)
                return True

        scheduled = object.__new__(ScheduledTaskService)
        scheduled.controller = controller
        scheduled.request_store = _RequestStore()
        scheduled._drain_dirty = False
        real_settle = scheduled.settle_activity_runs

        def observed_settle(activity):
            events.append(
                ("terminal-owner", registry.has_pending_run_output("run-real-scheduler"))
            )
            return real_settle(activity)

        scheduled.settle_activity_runs = observed_settle
        controller.scheduled_task_service = scheduled
        agent_service.agents["claude"] = SimpleNamespace(
            on_activity_output_settled=lambda runtime_key: events.append(
                (
                    "waiter",
                    runtime_key,
                    "run-real-scheduler" in terminal_runs,
                    registry.has_completed_output("claude", runtime_key),
                )
            )
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        registry.start(
            backend="claude",
            runtime_key="runtime-real-scheduler",
            session_id="sess-real-scheduler",
            activity_id="activity-real-scheduler",
            kind="local_agent",
            run_id="run-real-scheduler",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-real-scheduler",
            activity_id="activity-real-scheduler",
            status="completed",
            metadata={"summary": "Visible result"},
            expects_output=True,
        )
        claimed = registry.claim_completed_output_batch(
            "claude",
            "runtime-real-scheduler",
        )

        class _InitialRunStore:
            def get_run(self, _run_id):
                return {"status": "running"}

            def record_run_output(self, _run_id, **_kwargs):
                raise RuntimeError("initial Run write unavailable")

            def close(self):
                pass

        with (
            patch.object(
                message_dispatcher_module,
                "SQLiteBackgroundTaskStore",
                return_value=_InitialRunStore(),
            ),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=None),
            patch.object(message_dispatcher_module, "persist_agent_message", return_value=None),
        ):
            message_id = await dispatcher.emit_agent_message(
                MessageContext(
                    user_id="scheduled",
                    channel_id="C123",
                    platform="slack",
                ),
                "result",
                "Visible result",
                output=activity_completion_output(
                    claimed[0],
                    activities=claimed,
                    detached=True,
                    completes_turn=False,
                ),
            )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            events,
            [
                ("terminal-owner", False),
                ("defer", "run-real-scheduler", "failed"),
                (
                    "run",
                    "run-real-scheduler",
                    "Background Activity activity-real-scheduler failed",
                ),
                ("waiter", "runtime-real-scheduler", True, False),
            ],
        )
        self.assertFalse(
            registry.has_completed_output("claude", "runtime-real-scheduler")
        )

    async def test_activity_run_settlement_follows_message_persistence(self):
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda _run_id: False),
            emit_matches_runtime_turn=lambda _context: False,
            release_runtime_turn=lambda _context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
        )
        events = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                events.append(("run", run_id, kwargs["terminal_status"]))

            def close(self):
                pass

        def persist(*_args, **_kwargs):
            events.append(("message", "persisted"))
            return {"id": "message-row"}

        with (
            patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
            patch.object(message_dispatcher_module, "persist_agent_message", side_effect=persist),
        ):
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "background work finished",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=True,
                    detached=True,
                    idempotency_key="activity-output",
                    run_id="run-origin",
                    requires_delivery_for_run_settlement=True,
                ),
            )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            events,
            [("message", "persisted"), ("run", "run-origin", "succeeded")],
        )

    async def test_owned_activity_defers_run_terminal_but_not_later_detached_output(self):
        controller = _StubController()
        blocking = [True]
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(
                has_blocking_run_activity=lambda run_id: blocking[0],
            ),
            emit_matches_runtime_turn=lambda context: True,
            release_runtime_turn=lambda context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-owned",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def defer_run_terminal(self, run_id, *, terminal_status, result_text):
                calls.append(("defer", run_id, terminal_status, result_text))

            def record_run_output(self, run_id, **kwargs):
                calls.append(("output", run_id, kwargs["output_id"], kwargs["terminal_status"]))

            def close(self):
                pass

        with (
            patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()),
            patch.object(message_dispatcher_module, "agent_message_exists", return_value=False),
        ):
            await dispatcher.emit_agent_message(
                context,
                "result",
                "started background work",
                output=MessageOutput(
                    completes_turn=True,
                    completes_run=True,
                    idempotency_key="output-1",
                    sequence=1,
                ),
            )
            blocking[0] = False
            await dispatcher.emit_agent_message(
                context,
                "result",
                "background work finished",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=True,
                    detached=True,
                    idempotency_key="output-2",
                    sequence=2,
                ),
            )

        self.assertEqual(
            calls,
            [
                ("defer", "run-owned", "succeeded", "started background work"),
                ("output", "run-owned", "output-1", None),
                ("output", "run-owned", "output-2", "succeeded"),
            ],
        )

    async def test_result_message_finalizes_scheduled_delivery(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "turn_source": "scheduled",
                "turn_base_session_id": "slack_scheduled-1",
                "scheduled_anchor_required": True,
            },
        )

        message_id = await dispatcher.emit_agent_message(context, "result", "hello")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(controller.im_client.sent, [("C123", None, "hello")])
        self.assertEqual(controller.session_handler.calls, [("C123", None, "bot-msg-1")])

    async def test_result_message_strips_silent_blocks(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C123", platform="slack")

        message_id = await dispatcher.emit_agent_message(
            context,
            "result",
            "<silent>internal decision</silent>\nVisible reply",
        )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(controller.im_client.sent, [("C123", None, "Visible reply")])

    async def test_silent_only_result_sends_nothing(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C123", platform="slack")

        message_id = await dispatcher.emit_agent_message(
            context,
            "result",
            "<silent>not relevant to the bot</silent>",
        )

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(controller.session_handler.calls, [])

    async def test_silent_only_log_message_sends_nothing(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C123", platform="slack")

        message_id = await dispatcher.emit_agent_message(
            context,
            "assistant",
            "<silent>only internal note</silent>",
        )

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent, [])

    async def test_suppressed_result_closes_transient_run_store(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "private output")

        self.assertEqual(message_id, "suppressed:run-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "private output", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_suppressed_agent_run_result_marks_run_terminal(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-agent",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "private agent output")

        self.assertEqual(message_id, "suppressed:run-agent")
        self.assertEqual(
            calls,
            [
                ("record", "run-agent", "private agent output", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_suppressed_agent_run_failure_records_diagnostic(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-agent",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "running"}

            def record_run_output(self, run_id, **kwargs):
                calls.append((run_id, kwargs))

            def close(self):
                pass

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(
                context,
                "result",
                "private failure",
                is_error=True,
                terminal_error="provider unavailable",
            )

        self.assertEqual(message_id, "suppressed:run-agent")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "run-agent")
        self.assertEqual(calls[0][1]["terminal_status"], "failed")
        self.assertEqual(calls[0][1]["error"], "provider unavailable")

    async def test_suppressed_coalesced_agent_run_result_marks_each_run_terminal(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
                "accepted_agent_run_ids": ["run-1", "run-2", "run-3"],
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "private agent output")

        self.assertEqual(message_id, "suppressed:run-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "private agent output", None, "succeeded"),
                ("record", "run-2", "private agent output", None, "succeeded"),
                ("record", "run-3", "private agent output", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_suppressed_coalesced_agent_run_result_preserves_cancelled_child(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
                "accepted_agent_run_ids": ["run-1", "run-2", "run-3"],
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                if run_id == "run-2":
                    return {"id": run_id, "status": "canceled", "cancel_requested": True}
                return {"id": run_id, "status": "queued", "cancel_requested": False}

            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "private agent output")

        self.assertEqual(message_id, "suppressed:run-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "private agent output", None, "succeeded"),
                ("record", "run-3", "private agent output", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_coalesced_agent_run_result_marks_each_run_terminal(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
                "accepted_agent_run_ids": ["run-1", "run-2", "run-3"],
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "shared visible result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "shared visible result", "bot-msg-1", "succeeded"),
                ("record", "run-2", "shared visible result", "bot-msg-1", "succeeded"),
                ("record", "run-3", "shared visible result", "bot-msg-1", "succeeded"),
                ("close",),
            ],
        )

    async def test_human_turn_result_settles_accepted_steer_agent_run(self):
        controller = _StubController()
        controller.session_turns = SimpleNamespace(
            accepted_agent_run_ids_for_turn=lambda turn_id: (
                ["run-steered"] if turn_id == "turn-active" else []
            ),
            on_terminal_result=lambda *_args, **_kwargs: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="human",
            channel_id="C123",
            platform="slack",
            platform_specific={"turn_token": "turn-active"},
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append((run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "steered result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("run-steered", "steered result", "bot-msg-1", "succeeded"),
                ("close",),
            ],
        )

    async def test_coalesced_agent_run_result_preserves_cancelled_child(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
                "accepted_agent_run_ids": ["run-1", "run-2", "run-3"],
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                if run_id == "run-2":
                    return {"id": run_id, "status": "canceled", "cancel_requested": True}
                return {"id": run_id, "status": "queued", "cancel_requested": False}

            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "shared visible result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "shared visible result", "bot-msg-1", "succeeded"),
                ("record", "run-3", "shared visible result", "bot-msg-1", "succeeded"),
                ("close",),
            ],
        )

    async def test_coalesced_agent_run_result_records_running_cancel_requested_run_terminal(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
                "accepted_agent_run_ids": ["run-1", "run-2"],
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                if run_id == "run-1":
                    return {"id": run_id, "status": "running", "cancel_requested": True}
                return {"id": run_id, "status": "queued", "cancel_requested": False}

            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "shared visible result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "shared visible result", "bot-msg-1", "succeeded"),
                ("record", "run-2", "shared visible result", "bot-msg-1", "succeeded"),
                ("close",),
            ],
        )

    async def test_suppressed_agent_run_ignores_non_result_process_messages(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-agent",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            system_id = await dispatcher.emit_agent_message(context, "system", "system prompt")
            tool_id = await dispatcher.emit_agent_message(context, "tool_call", "shell command")
            assistant_id = await dispatcher.emit_agent_message(context, "assistant", "working note")
            result_id = await dispatcher.emit_agent_message(context, "result", "final result")

        self.assertEqual(system_id, "suppressed:run-agent")
        self.assertEqual(tool_id, "suppressed:run-agent")
        self.assertEqual(assistant_id, "suppressed:run-agent")
        self.assertEqual(result_id, "suppressed:run-agent")
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(
            calls,
            [
                ("record", "run-agent", "final result", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_suppressed_notify_records_private_run_output(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-1",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "notify", "auth recovery required")

        self.assertEqual(message_id, "suppressed:run-1")
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(calls, [])

    async def test_visible_agent_run_result_marks_run_terminal(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-visible",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "visible result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-visible", "visible result", "bot-msg-1", "succeeded"),
                ("close",),
            ],
        )

    async def test_visible_agent_run_error_result_marks_run_failed(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-failed",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "backend failed", is_error=True)

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(
            calls,
            [
                ("record", "run-failed", "backend failed", "bot-msg-1", "failed"),
                ("close",),
            ],
        )

    async def test_empty_agent_run_error_result_marks_failed_and_releases_turn(self):
        controller = _StubController()
        released = []

        def _mark_turn_complete(context):
            released.append(context.channel_id)

        controller.mark_turn_complete = _mark_turn_complete
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-empty-failed",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "", is_error=True)

        self.assertIsNone(message_id)
        self.assertEqual(released, ["C123"])
        self.assertEqual(
            calls,
            [
                ("record", "run-empty-failed", "", None, "failed"),
                ("close",),
            ],
        )

    async def test_agent_run_result_delivery_failure_still_releases_turn(self):
        controller = _StubController()
        controller.im_client = _FailingIMClient()
        released = []

        def _mark_turn_complete(context):
            released.append(context.channel_id)

        controller.mark_turn_complete = _mark_turn_complete
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-delivery-failed",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                calls.append(("close",))

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "final but undelivered")

        self.assertIsNone(message_id)
        self.assertEqual(released, ["C123"])
        self.assertEqual(
            calls,
            [
                ("record", "run-delivery-failed", "final but undelivered", None, "succeeded"),
                ("close",),
            ],
        )

    async def test_delivery_override_sends_result_to_parent_channel(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            thread_id="171717.123",
            platform="slack",
            platform_specific={
                "turn_source": "scheduled",
                "turn_base_session_id": "slack_171717.123",
                "delivery_override": {
                    "user_id": "scheduled",
                    "channel_id": "C123",
                    "thread_id": None,
                    "platform": "slack",
                    "is_dm": False,
                },
                "scheduled_delivery_alias": {
                    "mode": "sent_message",
                    "session_key": "slack::C123",
                    "clear_source": False,
                },
            },
        )

        message_id = await dispatcher.emit_agent_message(context, "result", "hello")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(controller.im_client.sent, [("C123", None, "hello")])
        self.assertEqual(controller.session_handler.calls, [("C123", "171717.123", "bot-msg-1")])

    async def test_discord_long_result_uses_first_chunk_as_scheduled_anchor(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="discord",
            platform_specific={
                "turn_source": "scheduled",
                "turn_base_session_id": "discord_scheduled-1",
                "scheduled_anchor_required": True,
            },
        )
        long_text = "x" * 4200

        message_id = await dispatcher.emit_agent_message(context, "result", long_text)

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(len(controller.im_client.sent), 3)
        self.assertEqual("".join(text for _, _, text in controller.im_client.sent), long_text)
        self.assertEqual(controller.session_handler.calls, [("C123", None, "bot-msg-1")])


class HarnessRunResultTextTests(unittest.IsolatedAsyncioTestCase):
    """P2: every harness run must record ``result_text``, not just ``agent_run``.

    Scenario coverage: HFR-041 (result text is recorded), HFR-042 (the backfill
    is guarded), HFR-043 (recovered Activity attribution). Re-validates HFR-030
    (which recorder a terminal output lands in) and MESSAGE-DELIVERY-005 (a Run's
    output ledger vs. its one terminal result).
    """

    @staticmethod
    def _settled_scheduled_run(task_id: str = "task-daily-report"):
        """A ``scheduled`` run the scheduler already marked terminal at dispatch.

        That premature settlement is P1; it is the state every live
        ``scheduled``/``watch`` row is in by the time its result is delivered,
        so it is the state PR1 has to write ``result_text`` into.
        """
        from core.scheduled_tasks import TaskExecutionStore

        store = TaskExecutionStore()
        request = store.enqueue_task_run(task_id)
        assert store.claim(request.id) is not None
        store.complete(request, ok=True)
        return store, request

    async def test_scheduled_run_records_result_text_on_an_already_terminal_row(self):
        """HFR-041."""
        store, request = self._settled_scheduled_run()
        dispatched = store.get_run(request.id)
        self.assertEqual(dispatched["status"], "succeeded")
        self.assertFalse(dispatched["result_text"])

        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "scheduled",
                "task_execution_id": request.id,
            },
        )

        message_id = await dispatcher.emit_agent_message(context, "result", "daily report body")

        self.assertEqual(message_id, "bot-msg-1")
        settled = store.get_run(request.id)
        self.assertEqual(settled["result_text"], "daily report body")
        # The output ledger is the weaker assertion: it lands from the unguarded
        # payload UPDATE and would pass even with ``result_text`` still empty.
        outputs = (settled["result_payload"] or {}).get("outputs") or []
        self.assertEqual([entry["text"] for entry in outputs], ["daily report body"])
        # Settlement timing is unchanged: the backfill writes text, not status.
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["completed_at"], dispatched["completed_at"])

    async def test_watch_run_records_result_text_on_an_already_terminal_row(self):
        """HFR-041: the 67 live ``run_type='watch'`` rows are the same defect."""
        from core.scheduled_tasks import TaskExecutionStore

        store = TaskExecutionStore()
        request = store.enqueue_hook_send(
            session_key="slack::channel::C123",
            prompt="watch fired",
            run_type="watch",
        )
        self.assertIsNotNone(store.claim(request.id))
        store.complete(request, ok=True)
        self.assertFalse(store.get_run(request.id)["result_text"])

        dispatcher = ConsolidatedMessageDispatcher(_StubController())
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "watch",
                "task_execution_id": request.id,
            },
        )

        await dispatcher.emit_agent_message(context, "result", "watch follow-up body")

        self.assertEqual(store.get_run(request.id)["result_text"], "watch follow-up body")

    async def test_terminal_backfill_never_overwrites_a_real_terminal_result(self):
        """HFR-042. The backfill is guarded: only an empty ``result_text`` is filled."""
        from core.scheduled_tasks import TaskExecutionStore

        store = TaskExecutionStore()
        request = store.enqueue_task_run("task-guarded")
        self.assertIsNotNone(store.claim(request.id))
        sqlite_store = store._sqlite
        self.assertIsNotNone(sqlite_store)
        first = sqlite_store.record_run_output(
            request.id,
            output_id="terminal",
            text="the real terminal result",
            terminal_status="succeeded",
        )
        self.assertTrue(first["terminal_transition"])
        self.assertFalse(first["text_backfilled"])

        second = sqlite_store.record_run_output(
            request.id,
            output_id="late",
            text="a later output",
            terminal_status="succeeded",
        )

        self.assertFalse(second["terminal_transition"])
        self.assertFalse(second["text_backfilled"])
        self.assertEqual(store.get_run(request.id)["result_text"], "the real terminal result")

    async def _settled_run(self, task_id, *, terminal_status, error=None, text=""):
        """A harness run already settled by someone other than this delivery."""
        from core.scheduled_tasks import TaskExecutionStore

        store = TaskExecutionStore()
        request = store.enqueue_task_run(task_id)
        self.assertIsNotNone(store.claim(request.id))
        sqlite_store = store._sqlite
        self.assertIsNotNone(sqlite_store)
        seed = sqlite_store.record_run_output(
            request.id, output_id="seed", text=text, terminal_status=terminal_status, error=error
        )
        self.assertTrue(seed["terminal_transition"])
        return store, sqlite_store, request

    async def test_backfill_refuses_a_canceled_run(self):
        """HFR-045. A cancellation is a decision, not a missing field.

        Without this guard a late success body lands on a ``canceled`` row, and
        because ``_build_callback_message`` prefers ``result_text`` over the
        "canceled before producing a result" fallback, the user is told the run
        produced a result after they cancelled it.
        """
        store, sqlite_store, request = await self._settled_run(
            "task-canceled", terminal_status="canceled"
        )
        late = sqlite_store.record_run_output(
            request.id, output_id="late", text="daily report body", terminal_status="succeeded"
        )
        self.assertFalse(late["text_backfilled"])
        row = store.get_run(request.id)
        self.assertEqual(row["status"], "canceled")
        self.assertFalse((row["result_text"] or "").strip())

    async def test_backfill_refuses_an_error_on_a_succeeded_run(self):
        """HFR-046. ``error`` is a verdict, not description.

        A scheduled row settles ``succeeded`` at dispatch (P1). A later failing
        delivery must not produce a succeeded-with-an-error record; settling the
        real outcome is PR7's job.
        """
        store, sqlite_store, request = await self._settled_run(
            "task-succeeded", terminal_status="succeeded"
        )
        late = sqlite_store.record_run_output(
            request.id, output_id="late", text="", terminal_status="failed", error="backend blew up"
        )
        self.assertFalse(late["text_backfilled"])
        row = store.get_run(request.id)
        self.assertEqual(row["status"], "succeeded")
        self.assertFalse((row["error"] or "").strip())

    async def test_backfill_still_diagnoses_a_failed_run(self):
        """HFR-047. Agreement still repairs, so the rule does not cost PR1 its point.

        A ``failed`` row receiving a ``failed`` delivery agrees, so it still gets
        its diagnostic text and keeps the error it settled with.
        """
        store, sqlite_store, request = await self._settled_run(
            "task-failed", terminal_status="failed", error="original error"
        )
        late = sqlite_store.record_run_output(
            request.id, output_id="late", text="what the agent managed to say", terminal_status="failed"
        )
        self.assertTrue(late["text_backfilled"])
        row = store.get_run(request.id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["result_text"], "what the agent managed to say")
        self.assertEqual(row["error"], "original error")

    async def test_backfill_refuses_success_text_on_a_failed_run(self):
        """HFR-048. The inverse of HFR-046, and the pair enumeration missed.

        ``sweep_stale_runs`` terminalizes an orphaned run ``failed``; the agent
        is still alive and delivers a successful result afterwards. Without the
        agreement rule the row keeps ``failed`` while ``_build_callback_message``
        reports the success body, so the user is told the report is fine for a
        run recorded as failed with "owner vanished".
        """
        store, sqlite_store, request = await self._settled_run(
            "task-swept", terminal_status="failed", error="swept: owner vanished"
        )
        late = sqlite_store.record_run_output(
            request.id,
            output_id="late",
            text="the report, actually fine",
            terminal_status="succeeded",
        )
        self.assertFalse(late["text_backfilled"])
        row = store.get_run(request.id)
        self.assertEqual(row["status"], "failed")
        self.assertFalse((row["result_text"] or "").strip())
        self.assertEqual(row["error"], "swept: owner vanished")

    async def test_scheduled_result_takes_the_rich_recorder_not_the_legacy_one(self):
        """HFR-030: the widened gate moves harness results onto the output ledger.

        Delivered (visible) variant of ``test_visible_agent_run_result_marks_run_terminal``.
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "scheduled",
                "task_execution_id": "run-scheduled",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "succeeded"}

            def record_run_output(self, run_id, **kwargs):
                calls.append((run_id, kwargs))

            def close(self):
                pass

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            message_id = await dispatcher.emit_agent_message(context, "result", "visible scheduled result")

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "run-scheduled")
        self.assertEqual(calls[0][1]["text"], "visible scheduled result")
        self.assertEqual(calls[0][1]["terminal_status"], "succeeded")

    async def test_suppressed_scheduled_result_takes_the_rich_recorder_not_the_legacy_one(self):
        """Suppressed variant. The ``elif`` below the widened gate is its negation,
        so widening one without the other would silently reroute this result."""
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "scheduled",
                "task_execution_id": "run-scheduled",
            },
        )
        calls = []

        class _Store:
            def get_run(self, run_id):
                return {"status": "succeeded"}

            def record_run_output(self, run_id, **kwargs):
                calls.append(("output", run_id, kwargs))

            def record_run_message(self, run_id, **kwargs):
                calls.append(("legacy", run_id, kwargs))

            def close(self):
                pass

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=_Store()):
            await dispatcher.emit_agent_message(context, "result", "private scheduled result")

        self.assertEqual([call[0] for call in calls], ["output"])
        self.assertEqual(calls[0][2]["terminal_status"], "succeeded")

    async def test_recovered_activity_in_a_suppressed_session_records_result_on_its_run(self):
        """HFR-043. ``activity_recovery`` carries a synthetic ``activity:<backend>:<id>``
        execution id, not a run id. Excluded from the widened set it would fall to
        the legacy recorder, which addresses ``record_run_message`` to that synthetic
        id, finds no row, and returns — losing the result with no exception and no
        log line. Its real run ids ride on the Activity completion output instead.
        """
        from core.session_activities import SessionActivity, activity_completion_output
        from core.scheduled_tasks import TaskExecutionStore

        store = TaskExecutionStore()
        request = store.enqueue_task_run("task-with-activity")
        self.assertIsNotNone(store.claim(request.id))

        activity = SessionActivity(
            id="act-1",
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            kind="task",
            status="completed",
            run_id=request.id,
            metadata={"run_ids": [request.id]},
        )
        controller = _StubController()
        controller.agent_service = SimpleNamespace(
            activities=SimpleNamespace(has_blocking_run_activity=lambda run_id: False),
            emit_matches_runtime_turn=lambda context: False,
            release_runtime_turn=lambda context: None,
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "suppress_delivery": True,
                "task_trigger_kind": "activity_recovery",
                "task_execution_id": f"activity:claude:{activity.id}",
            },
        )

        await dispatcher.emit_agent_message(
            context,
            "result",
            "recovered background result",
            output=activity_completion_output(activity, detached=True, completes_turn=False),
        )

        settled = store.get_run(request.id)
        # Asserting only "the call did not raise" passes against the broken path.
        self.assertEqual(settled["result_text"], "recovered background result")
        self.assertEqual(settled["status"], "succeeded")
        self.assertIsNone(store.get_run(f"activity:claude:{activity.id}"))


class _AdapterShapedIMClient(_StubIMClient):
    """An IM client shaped like a real adapter: transport, bookkeeping, return id.

    ``swallow_bookkeeping_error`` selects the pre-fix vs post-fix adapter shape.
    """

    def __init__(self, *, swallow_bookkeeping_error: bool):
        super().__init__()
        self.swallow_bookkeeping_error = swallow_bookkeeping_error
        self.delivered = []

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        # Point of no return: the platform accepted the payload, the user HAS it.
        message_id = f"bot-msg-{self._next_id}"
        self._next_id += 1
        self.delivered.append(message_id)
        # Post-send local bookkeeping (sessions.mark_thread_active) blows up.
        try:
            raise RuntimeError("session store unavailable")
        except RuntimeError:
            if not self.swallow_bookkeeping_error:
                raise
        return message_id


class PostSendBookkeepingDeliveryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """The dispatcher cannot tell a bookkeeping raise from a failed send.

    ``emit_agent_message`` only sees the adapter's return value or its exception,
    so an adapter that lets post-send bookkeeping escape converts "delivered but
    unbookkept" into "never delivered": the run is recorded with ``message_id
    None`` even though the transport handed the message to the user, and whoever
    owes a durable notice for it re-sends a duplicate.

    That is why the fix has to live in the adapter (``modules/im/discord.py``
    already guards it; Slack and Feishu now match) and cannot be papered over in
    ``core/message_dispatcher.py``: the id is destroyed before it can escape.
    Adapter-level coverage lives in ``tests/test_im_post_send_bookkeeping.py``;
    these two cases pin the dispatcher-side consequence of each shape.

    Subordinate context: ack/delivery lane, HFR-079's family. No new scenario id.
    """

    def _run(self, client):
        controller = _StubController()
        controller.im_client = client
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_trigger_kind": "agent_run",
                "task_execution_id": "run-bookkeeping",
            },
        )
        calls = []

        class _Store:
            def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
                calls.append(("record", run_id, text, message_id, terminal_status))

            def close(self):
                pass

        return dispatcher, context, calls, _Store

    async def test_escaping_bookkeeping_error_loses_evidence_for_a_delivered_message(self):
        client = _AdapterShapedIMClient(swallow_bookkeeping_error=False)
        dispatcher, context, calls, store_cls = self._run(client)

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=store_cls()):
            message_id = await dispatcher.emit_agent_message(context, "result", "delivered body")

        # The transport delivered on the very first call, but because the id never
        # escaped the adapter the dispatcher read it as a failed send and walked its
        # fallback ladder, pushing the SAME logical message to the user again. One
        # bookkeeping failure, several user-visible copies, and no evidence at all.
        self.assertEqual(client.delivered[0], "bot-msg-1")
        self.assertGreater(len(client.delivered), 1)
        self.assertIsNone(message_id)
        self.assertEqual(calls, [("record", "run-bookkeeping", "delivered body", None, "succeeded")])

    async def test_swallowed_bookkeeping_error_keeps_evidence_for_a_delivered_message(self):
        client = _AdapterShapedIMClient(swallow_bookkeeping_error=True)
        dispatcher, context, calls, store_cls = self._run(client)

        with patch.object(message_dispatcher_module, "SQLiteBackgroundTaskStore", return_value=store_cls()):
            message_id = await dispatcher.emit_agent_message(context, "result", "delivered body")

        self.assertEqual(client.delivered, ["bot-msg-1"])
        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(calls, [("record", "run-bookkeeping", "delivered body", "bot-msg-1", "succeeded")])


class HarnessPromptEchoTests(unittest.IsolatedAsyncioTestCase):
    """MESSAGE-DELIVERY-018: a Harness turn announces its prompt in the IM channel.

    Without the echo an IM conversation only ever received the agent's reply, so a
    scheduled/watch/webhook/hook/``agent run`` result read as an answer to a question
    nobody in the channel could see.
    """

    def _context(self, **spec):
        payload = {
            "task_trigger_kind": "scheduled",
            "task_execution_id": "run-echo-1",
            "task_definition_id": "task-echo-1",
        }
        payload.update(spec)
        return MessageContext(
            user_id="scheduled",
            channel_id="C123",
            platform=payload.pop("platform", "slack"),
            thread_id=payload.pop("thread_id", None),
            message_id=payload.pop("message_id", "scheduled:run-echo-1"),
            platform_specific=payload,
        )

    async def test_scheduled_prompt_is_echoed_to_the_channel(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        message_id = await dispatcher.emit_harness_prompt(
            self._context(task_definition_name="Daily digest"),
            "summarize yesterday's deploys",
        )

        self.assertEqual(message_id, "bot-msg-1")
        self.assertEqual(len(controller.im_client.sent), 1)
        channel_id, thread_id, text = controller.im_client.sent[0]
        self.assertEqual(channel_id, "C123")
        self.assertIsNone(thread_id)
        self.assertIn("Daily digest", text)
        self.assertIn("> summarize yesterday's deploys", text)

    async def test_every_harness_trigger_kind_is_echoed(self):
        for kind in ("scheduled", "watch", "webhook", "hook", "agent_run"):
            with self.subTest(kind=kind):
                controller = _StubController()
                dispatcher = ConsolidatedMessageDispatcher(controller)
                await dispatcher.emit_harness_prompt(
                    # ``watch`` / ``webhook`` / ``hook`` echo their definition's stored
                    # instruction; the other two echo the dispatch text itself.
                    self._context(task_trigger_kind=kind, harness_display_prompt="do the thing"),
                    "do the thing",
                )
                self.assertEqual(len(controller.im_client.sent), 1)

    async def test_composed_prompt_echoes_the_instruction_not_the_generated_evidence(self):
        """Scenario: MESSAGE-DELIVERY-018

        A watch prompt is composed FOR THE AGENT: the stored instruction plus the
        waiter's raw stdout (``core/watches.py::_build_prompt``); an
        ``--on-failure agent`` escalation appends a generated failure report the same
        way. Echoing that verbatim would publish raw command output — tokens
        included — into a shared channel before the agent can redact it (Codex P1).
        """
        for kind in ("watch", "webhook", "hook"):
            with self.subTest(kind=kind):
                controller = _StubController()
                dispatcher = ConsolidatedMessageDispatcher(controller)
                composed = "check the deploy and report\n\ntoken=ghp_SECRET\nrows=42"

                await dispatcher.emit_harness_prompt(
                    self._context(
                        task_trigger_kind=kind,
                        harness_display_prompt="check the deploy and report",
                        display_text=composed,
                    ),
                    composed,
                )

                _channel_id, _thread_id, text = controller.im_client.sent[0]
                self.assertIn("> check the deploy and report", text)
                self.assertNotIn("ghp_SECRET", text)
                self.assertNotIn("rows=42", text)

    async def test_composed_prompt_without_a_stored_instruction_echoes_nothing(self):
        # A deleted / unresolvable definition means nothing here can tell the
        # user-authored instruction from the generated evidence, so the echo stays
        # silent rather than guessing (the Workbench row still has the full prompt).
        for kind in ("watch", "webhook", "hook"):
            with self.subTest(kind=kind):
                controller = _StubController()
                dispatcher = ConsolidatedMessageDispatcher(controller)

                result = await dispatcher.emit_harness_prompt(
                    self._context(task_trigger_kind=kind),
                    "waiter said: token=ghp_SECRET",
                )

                self.assertIsNone(result)
                self.assertEqual(controller.im_client.sent, [])

    async def test_echoed_mentions_cannot_ping_the_channel(self):
        """Quoting does not stop a renderer from resolving a mention (Codex P2).

        Discord sends without ``allowed_mentions``, so an echoed ``@everyone`` would
        really broadcast; Slack resolves ``<@U…>`` / ``<!channel>`` the same way.
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(task_definition_name="@here nightly"),
            "ping @everyone plus <@U123>, <@&456> and <!channel>",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        for mention in ("@everyone", "@here", "<@U123>", "<@&456>", "<!channel>"):
            self.assertNotIn(mention, text)
        # Only a zero-width break was inserted, so the prompt still reads the same.
        self.assertIn("ping @everyone plus <@U123>, <@&456> and <!channel>", text.replace("\u200b", ""))

    async def test_echoed_username_mention_cannot_notify_on_telegram(self):
        """A bare ``@username`` is a real mention on Telegram (Codex P2).

        ``TelegramFormatter.render`` HTML-escapes the body but leaves the sigil intact,
        so an echoed handle would notify that account. Neutralized for every adapter
        rather than in the Telegram formatter: one body is rendered by all of them, and
        a new adapter inherits the guard.
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(platform="telegram", task_definition_name="@release_bot nightly"),
            "ask @alice_dev to review, cc @team_lead",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        for mention in ("@alice_dev", "@team_lead", "@release_bot"):
            self.assertNotIn(mention, text)
        self.assertIn("ask @alice_dev to review, cc @team_lead", text.replace("\u200b", ""))

    async def test_echo_is_sent_as_markdown_so_the_quote_renders(self):
        # Slack builds a plain_text block for anything but markdown and would show
        # the ``> `` markers literally (Codex P3). Telegram resolves either value to
        # its own HTML default, so this changes nothing there.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        recorded: dict = {}

        async def _send(context, text, parse_mode=None, reply_to=None):
            recorded["parse_mode"] = parse_mode
            return "bot-msg-1"

        controller.im_client.send_message = _send

        await dispatcher.emit_harness_prompt(self._context(), "do the thing")

        self.assertEqual(recorded["parse_mode"], "markdown")

    async def test_activity_recovery_and_human_turns_are_not_echoed(self):
        # ``activity_recovery`` is a runtime re-injection, not a user-authored
        # instruction; a human turn's prompt IS the IM message already on screen.
        for kind in ("activity_recovery", "", "human"):
            with self.subTest(kind=kind):
                controller = _StubController()
                dispatcher = ConsolidatedMessageDispatcher(controller)
                result = await dispatcher.emit_harness_prompt(
                    self._context(task_trigger_kind=kind),
                    "do the thing",
                )
                self.assertIsNone(result)
                self.assertEqual(controller.im_client.sent, [])

    async def test_workbench_platform_is_not_echoed(self):
        # Workbench Chat renders the ``harness`` Message row itself.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(
            self._context(platform="avibe"),
            "do the thing",
        )

        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_background_session_is_not_echoed(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(
            self._context(suppress_delivery=True),
            "do the thing",
        )

        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_disabled_runtime_switch_keeps_result_only_behavior(self):
        controller = _StubController()
        controller.config.harness_prompt_echo = False
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(self._context(), "do the thing")

        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_display_snapshot_wins_over_internal_dispatch_text(self):
        """A replayed durable turn carries an internal recovery guard in its dispatch
        text; the channel must see the stored prompt instead (Codex P2)."""
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(display_text="summarize open PRs"),
            "[Avibe recovery: this request may have been delivered before restart.]"
            "\n\nsummarize open PRs",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertIn("> summarize open PRs", text)
        self.assertNotIn("Avibe recovery", text)

    async def test_merged_batch_echoes_every_distinct_prompt_it_dispatched(self):
        """Scenario: MESSAGE-DELIVERY-018

        Two ``vibe agent run`` deliveries queued for one busy session merge into a
        single Turn (``_collect_delivery_segment``) and BOTH prompts reach the backend
        (``_segment_dispatch_text``). Echoing the first snapshot alone would announce
        one instruction for a result that answers two (Codex P2).
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(
                task_trigger_kind="agent_run",
                display_text="summarize open PRs",
                display_texts=["summarize open PRs", "then close the stale ones"],
            ),
            "summarize open PRs\n\n---\n\nthen close the stale ones",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertIn("> summarize open PRs", text)
        self.assertIn("> then close the stale ones", text)

    async def test_merged_repeat_firings_of_one_task_echo_the_prompt_once(self):
        # Two firings of the same scheduled task carry the SAME stored prompt, so the
        # merged batch must not read as the instruction having been given twice.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(
                display_text="summarize open PRs",
                display_texts=["summarize open PRs", "summarize open PRs"],
            ),
            "summarize open PRs\n\n---\n\nsummarize open PRs",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertEqual(text.count("> summarize open PRs"), 1)

    async def test_empty_batch_snapshots_fall_back_to_the_single_snapshot(self):
        # The legacy mirror path stages no batch, and a batch of blank snapshots must
        # not silence an echo the singular key can still serve.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(display_text="summarize open PRs", display_texts=["", "  "]),
            "summarize open PRs",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertIn("> summarize open PRs", text)

    async def test_merged_composed_batch_echoes_each_stored_instruction(self):
        """Scenario: MESSAGE-DELIVERY-018

        The composed kinds echo the definition's stored instruction, and that
        instruction can be EDITED between two firings — so a merged batch dispatches
        two different ones and the singular key would announce only the first
        (Codex P2). Each Delivery's own stamped instruction, and still none of the
        generated evidence.
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(
                task_trigger_kind="watch",
                harness_display_prompt="check the deploy",
                harness_display_prompts=["check the deploy", "check the deploy and page me"],
            ),
            "check the deploy\n\ntoken=ghp_SECRET",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertIn("> check the deploy", text)
        self.assertIn("> check the deploy and page me", text)
        self.assertNotIn("ghp_SECRET", text)

    async def test_merged_composed_batch_of_one_instruction_echoes_it_once(self):
        # Repeat firings of an UNCHANGED watch carry the same stored instruction; the
        # merged batch must not read as it having been given twice.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(
                task_trigger_kind="watch",
                harness_display_prompt="check the deploy",
                harness_display_prompts=["check the deploy", "check the deploy"],
            ),
            "check the deploy\n\ntoken=ghp_SECRET",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertEqual(text.count("> check the deploy"), 1)

    async def test_merged_composed_batch_without_instructions_stays_silent(self):
        # An empty batch falls back to the singular key, and when that is absent too
        # the composed kinds still refuse to publish the generated evidence.
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(
            self._context(task_trigger_kind="watch", harness_display_prompts=["", "  "]),
            "waiter said: token=ghp_SECRET",
        )

        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_long_definition_name_is_bounded_before_sending(self):
        """A task/watch name is never length-validated at creation (Codex P2).

        The label is appended AFTER the prompt cap, so an unbounded name could push the
        body past Discord's 2,000-char limit — the adapter would reject the echo and the
        channel would see no prompt at all.
        """
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        limit = message_dispatcher_module.HARNESS_PROMPT_ECHO_MAX_NAME_CHARS

        await dispatcher.emit_harness_prompt(
            self._context(task_definition_name="n" * (limit * 40)),
            "do the thing",
        )

        _channel_id, _thread_id, text = controller.im_client.sent[0]
        self.assertNotIn("n" * (limit + 1), text)
        self.assertIn("n" * limit, text)
        self.assertIn("truncated", text)
        self.assertIn("> do the thing", text)

    async def test_runtime_switch_is_reloaded_before_the_gate(self):
        """A Harness turn reaches no IM inbound handler, so nothing else reloads
        ``controller.config``: the config-only toggle must be re-read here, or a
        true->false change would still send one more prompt (Codex P2)."""
        controller = _StubController()
        refreshed = []

        def _refresh_config_from_disk():
            refreshed.append(True)
            controller.config.harness_prompt_echo = False

        controller._refresh_config_from_disk = _refresh_config_from_disk
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(self._context(), "do the thing")

        self.assertTrue(refreshed)
        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_failed_config_reload_still_echoes(self):
        controller = _StubController()
        controller._refresh_config_from_disk = Mock(side_effect=RuntimeError("disk gone"))
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(self._context(), "do the thing")

        self.assertEqual(result, "bot-msg-1")
        self.assertEqual(len(controller.im_client.sent), 1)

    async def test_echo_follows_the_delivery_override_target(self):
        # The question must land where the answer lands (``post_to`` / deliver key).
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(
            self._context(
                delivery_override={
                    "user_id": "U9",
                    "channel_id": "C999",
                    "thread_id": "T9",
                    "platform": "slack",
                    "is_dm": False,
                }
            ),
            "do the thing",
        )

        self.assertEqual(controller.im_client.sent[0][0], "C999")
        self.assertEqual(controller.im_client.sent[0][1], "T9")

    async def test_repeat_dispatch_of_one_delivery_echoes_once(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = self._context()

        first = await dispatcher.emit_harness_prompt(context, "do the thing")
        second = await dispatcher.emit_harness_prompt(context, "do the thing")

        self.assertEqual(first, "bot-msg-1")
        self.assertIsNone(second)
        self.assertEqual(len(controller.im_client.sent), 1)

    async def test_distinct_runs_in_one_channel_each_echo(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        await dispatcher.emit_harness_prompt(self._context(message_id="scheduled:run-a"), "first")
        await dispatcher.emit_harness_prompt(self._context(message_id="scheduled:run-b"), "second")

        self.assertEqual(len(controller.im_client.sent), 2)

    async def test_echo_memory_stays_bounded(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        limit = message_dispatcher_module.HARNESS_PROMPT_ECHO_MEMORY

        for index in range(limit + 5):
            await dispatcher.emit_harness_prompt(
                self._context(message_id=f"scheduled:run-{index}"),
                "do the thing",
            )

        self.assertEqual(len(dispatcher._harness_prompt_echo_keys), limit)
        self.assertEqual(len(dispatcher._harness_prompt_echo_order), limit)

    async def test_long_prompt_is_truncated_and_every_line_quoted(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        limit = message_dispatcher_module.HARNESS_PROMPT_ECHO_MAX_CHARS

        await dispatcher.emit_harness_prompt(self._context(), "line one\nline two\n" + "x" * (limit * 2))

        text = controller.im_client.sent[0][2]
        body = text.split("\n", 1)[1]
        self.assertTrue(all(line.startswith("> ") for line in body.splitlines()))
        self.assertIn("truncated", text)
        self.assertLess(len(text), limit * 2)

    async def test_silent_only_prompt_sends_nothing(self):
        controller = _StubController()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(
            self._context(),
            "<silent>internal bookkeeping</silent>",
        )

        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])

    async def test_send_failure_never_blocks_the_turn(self):
        controller = _StubController()
        controller.im_client = _FailingIMClient()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        result = await dispatcher.emit_harness_prompt(self._context(), "do the thing")

        self.assertIsNone(result)
        # Not remembered, so a later healthy attempt for the same run can still echo.
        self.assertEqual(dispatcher._harness_prompt_echo_keys, set())
