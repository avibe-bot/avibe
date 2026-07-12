from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.message_dispatcher as message_dispatcher_module
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput
from modules.im import MessageContext


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
                {"terminal_status": "failed", "error": "provider unavailable"},
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
            with self.assertRaisesRegex(RuntimeError, "not durably persisted"):
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

        self.assertEqual(recorded, [])
        persist.assert_not_called()

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
            with self.assertRaisesRegex(RuntimeError, "run store unavailable"):
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

        self.assertEqual(events, [("message", "persisted"), ("run", "run-origin")])

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

            def defer_run_terminal(self, run_id, *, terminal_status):
                calls.append(("defer", run_id, terminal_status))

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
                ("defer", "run-owned", "succeeded"),
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
                ("record", "run-1", "private output", "suppressed:run-1", None),
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
                ("record", "run-agent", "private agent output", "suppressed:run-agent", "succeeded"),
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
                "coalesced_queue": {"execution_ids": ["run-1", "run-2", "run-3"]},
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
                ("record", "run-1", "private agent output", "suppressed:run-1", "succeeded"),
                ("record", "run-2", "private agent output", "suppressed:run-1", "succeeded"),
                ("record", "run-3", "private agent output", "suppressed:run-1", "succeeded"),
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
                "coalesced_queue": {"execution_ids": ["run-1", "run-2", "run-3"]},
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
                ("record", "run-1", "private agent output", "suppressed:run-1", "succeeded"),
                ("record", "run-3", "private agent output", "suppressed:run-1", "succeeded"),
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
                "coalesced_queue": {"execution_ids": ["run-1", "run-2", "run-3"]},
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
                "coalesced_queue": {"execution_ids": ["run-1", "run-2", "run-3"]},
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
                "coalesced_queue": {"execution_ids": ["run-1", "run-2"]},
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
                ("record", "run-agent", "final result", "suppressed:run-agent", "succeeded"),
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
        self.assertEqual(
            calls,
            [
                ("record", "run-1", "auth recovery required", "suppressed:run-1", None),
                ("close",),
            ],
        )

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
