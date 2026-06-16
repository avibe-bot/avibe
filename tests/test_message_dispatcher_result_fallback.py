from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext


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


class _StubIMClient:
    def __init__(self, *, fail_first_send: bool = False, upload_id: str = "file-1"):
        self.sent_messages = []
        self.uploaded_markdowns = []
        self._next_id = 1
        self._fail_first_send = fail_first_send
        self._send_attempts = 0
        self._upload_id = upload_id

    def should_use_thread_for_reply(self):
        return False

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self._send_attempts += 1
        if self._fail_first_send and self._send_attempts == 1:
            raise RuntimeError("inline send failed")
        self.sent_messages.append((context.channel_id, text, parse_mode))
        message_id = f"msg-{self._next_id}"
        self._next_id += 1
        return message_id

    async def upload_markdown(self, context, title, content, filetype="markdown"):
        self.uploaded_markdowns.append((context.channel_id, title, content, filetype))
        return self._upload_id


class _DropClient(_StubIMClient):
    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append((context.channel_id, text, parse_mode))
        return None

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        self.sent_messages.append((context.channel_id, text, parse_mode))
        return None

    async def upload_markdown(self, context, title, content, filetype="markdown"):
        self.uploaded_markdowns.append((context.channel_id, title, content, filetype))
        return None


class _NativeMarkdownIMClient(_StubIMClient):
    def __init__(self):
        super().__init__()
        self.native_markdown_messages = []

    async def send_markdown_message(self, context, text, keyboard=None, reply_to=None):
        self.native_markdown_messages.append((context.channel_id, text, keyboard, reply_to))
        message_id = f"native-{self._next_id}"
        self._next_id += 1
        return message_id


class _StubController:
    def __init__(
        self,
        *,
        platform: str = "lark",
        language: str = "en",
        fail_first_send: bool = False,
        upload_id: str = "file-1",
        im_client=None,
        reply_enhancements: bool = False,
    ):
        self.config = type(
            "Config",
            (),
            {"platform": platform, "language": language, "reply_enhancements": reply_enhancements},
        )()
        self.session_handler = _StubSessionHandler()
        self.im_client = im_client or _StubIMClient(fail_first_send=fail_first_send, upload_id=upload_id)
        self.agent_service = None

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{context.platform}::{context.channel_id}"

    def get_settings_manager_for_context(self, context):
        return _StubSettingsManager()

    def get_im_client_for_context(self, context):
        return self.im_client


class MessageDispatcherResultFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_slack_result_uses_native_markdown_sender_when_available(self):
        im_client = _NativeMarkdownIMClient()
        controller = _StubController(platform="slack", im_client=im_client)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        text = "| A | B |\n| - | - |\n| 1 | 2 |"

        message_id = await dispatcher.emit_agent_message(context, "result", text)

        self.assertEqual(message_id, "native-1")
        self.assertEqual(im_client.sent_messages, [])
        self.assertEqual(im_client.native_markdown_messages, [("C1", text, None, None)])

    async def test_slack_result_passes_quick_replies_to_native_markdown_sender(self):
        im_client = _NativeMarkdownIMClient()
        controller = _StubController(platform="slack", im_client=im_client, reply_enhancements=True)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

        message_id = await dispatcher.emit_agent_message(
            context,
            "result",
            "Body\n\n---\n[Continue] | [Stop]",
        )

        self.assertEqual(message_id, "native-1")
        self.assertEqual(im_client.sent_messages, [])
        channel_id, text, keyboard, reply_to = im_client.native_markdown_messages[0]
        self.assertEqual(channel_id, "C1")
        self.assertEqual(text, "Body")
        self.assertIsNone(reply_to)
        self.assertEqual([button.text for button in keyboard.buttons[0]], ["Continue", "Stop"])

    async def test_result_persists_cleaned_display_text_not_raw(self):
        """The persisted result must match what the user was shown, not the raw
        text with reply-enhancer artifacts. The inbox preview + chat transcript
        reload the persisted row, so the trailing quick-reply button block (and
        file:// links) must already be stripped at persist time."""
        im_client = _NativeMarkdownIMClient()
        controller = _StubController(platform="slack", im_client=im_client, reply_enhancements=True)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        raw = "Body\n\n---\n[Continue] | [Stop]"

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(context, "result", raw)

        # Delivered text had the quick-reply block stripped to "Body".
        _, delivered_text, _, _ = im_client.native_markdown_messages[0]
        self.assertEqual(delivered_text, "Body")
        # The persisted row must equal the displayed text, not the raw input.
        persist.assert_called_once()
        _, persisted_type, persisted_text = persist.call_args.args
        self.assertEqual(persisted_type, "result")
        self.assertEqual(persisted_text, "Body")
        self.assertNotIn("[Continue]", persisted_text)

    async def test_avibe_result_persists_quick_replies_for_workbench(self):
        """avibe carries the parsed quick-reply labels to ``persist_agent_message``
        (as the ``quick_replies`` kwarg) so the workbench can render the button
        group; the persisted text still has the trailing block stripped."""
        controller = _StubController(platform="avibe", reply_enhancements=True)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="avibe")
        raw = "Pick one:\n\n---\n[✅ Yes] | [🙅 No]"

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(context, "result", raw)

        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1], "result")
        self.assertEqual(persist.call_args.kwargs.get("quick_replies"), ["✅ Yes", "🙅 No"])
        # The block is still stripped from the persisted text itself.
        self.assertNotIn("[✅ Yes]", persist.call_args.args[2])
        self.assertIn("Pick one", persist.call_args.args[2])

    async def test_suppressed_delivery_is_not_persisted(self):
        """Suppressed scheduled output is intentionally private — it must NOT
        leak into the cross-platform messages history."""
        controller = _StubController(platform="slack")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="U1", channel_id="C1", platform="slack",
            platform_specific={"suppress_delivery": True},
        )
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(context, "result", "private output")
        persist.assert_not_called()
        self.assertTrue(message_id.startswith("suppressed:"))

    async def test_notify_persisted_only_on_successful_send(self):
        controller = _StubController(platform="slack")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(context, "notify", "heads up")
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1], "notify")

    async def test_notify_not_persisted_when_send_fails(self):
        class _FailClient(_StubIMClient):
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("platform API down")

        controller = _StubController(platform="slack", im_client=_FailClient())
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            result = await dispatcher.emit_agent_message(context, "notify", "heads up")
        self.assertIsNone(result)
        persist.assert_not_called()

    async def test_removed_platform_notify_drop_is_not_persisted(self):
        controller = _StubController(platform="discord", im_client=_DropClient())
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="discord")

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            result = await dispatcher.emit_agent_message(context, "notify", "late notify")

        self.assertIsNone(result)
        persist.assert_not_called()

    async def test_removed_platform_result_drop_is_not_persisted(self):
        controller = _StubController(platform="discord", im_client=_DropClient())
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="discord")

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            result = await dispatcher.emit_agent_message(context, "result", "late result")

        self.assertIsNone(result)
        persist.assert_not_called()

    async def test_stale_runtime_result_is_dropped_before_delivery_and_persistence(self):
        controller = _StubController(platform="slack")
        controller.agent_service = type(
            "AgentService",
            (),
            {
                "emit_matches_runtime_turn": staticmethod(lambda _context: False),
                "release_runtime_turn": staticmethod(lambda _context: None),
            },
        )()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_runtime_turn_key": "s:/repo", "agent_runtime_turn_token": "old"},
        )

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(context, "result", "late result")

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent_messages, [])
        persist.assert_not_called()

    async def test_result_releases_runtime_gate_after_result_cleanup(self):
        controller = _StubController(platform="slack")
        events = []

        class _AgentService:
            @staticmethod
            def emit_matches_runtime_turn(_context):
                return True

            @staticmethod
            def release_runtime_turn(_context):
                events.append("release")

        controller.agent_service = _AgentService()
        dispatcher = ConsolidatedMessageDispatcher(controller)

        async def _clear_consolidated_state(_context):
            events.append("clear")

        dispatcher._clear_consolidated_state = _clear_consolidated_state
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={"agent_runtime_turn_key": "s:/repo", "agent_runtime_turn_token": "tok"},
        )

        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(context, "result", "done")

        self.assertEqual(events, ["clear", "release"])

    async def test_muted_log_message_still_persists(self):
        """assistant / tool_call rows persist BEFORE the mute filter, so a muted
        process log still lands in the store (product requirement)."""
        class _HiddenSettings(_StubSettingsManager):
            def is_message_type_hidden(self, settings_key, canonical_type):
                return True

        controller = _StubController(platform="slack")
        controller.get_settings_manager_for_context = lambda ctx: _HiddenSettings()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            result = await dispatcher.emit_agent_message(context, "assistant", "thinking…")
        # Hidden → not delivered, but still persisted.
        self.assertIsNone(result)
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1], "assistant")

    async def test_summary_upload_becomes_primary_anchor_without_duplicate_upload(self):
        controller = _StubController(platform="lark", language="en", fail_first_send=True)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="lark")
        long_text = "x" * 35000

        message_id = await dispatcher.emit_agent_message(context, "result", long_text)

        self.assertEqual(message_id, "file-1")
        self.assertEqual(
            controller.im_client.uploaded_markdowns,
            [("C1", "result.md", long_text, "markdown")],
        )
        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "⚠️ The message could not be sent inline, so I sent it as `result.md` above.", "markdown")],
        )
        self.assertEqual(controller.session_handler.calls, [("C1", None, "file-1")])

    async def test_attachment_only_notice_uses_configured_language(self):
        controller = _StubController(platform="lark", language="zh", fail_first_send=True)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="lark")
        text = "| A | B |\n| - | - |\n| 1 | 2 |"

        message_id = await dispatcher.emit_agent_message(context, "result", text)

        self.assertEqual(message_id, "file-1")
        self.assertEqual(
            controller.im_client.uploaded_markdowns,
            [("C1", "result.md", text, "markdown")],
        )
        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "⚠️ 这条消息无法以内联形式发送，所以我已将完整内容作为 `result.md` 发在上方。", "markdown")],
        )
        self.assertEqual(controller.session_handler.calls, [("C1", None, "file-1")])

    async def test_slack_attachment_only_fallback_does_not_finalize_with_file_id(self):
        controller = _StubController(platform="slack", language="en", fail_first_send=True, upload_id="F123")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="scheduled",
            channel_id="C1",
            thread_id="171717.123",
            platform="slack",
            platform_specific={
                "turn_source": "scheduled",
                "turn_base_session_id": "slack_171717.123",
                "scheduled_delivery_alias": {
                    "mode": "sent_message",
                    "session_key": "slack::C1",
                    "clear_source": False,
                },
            },
        )
        text = "| A | B |\n| - | - |\n| 1 | 2 |"

        message_id = await dispatcher.emit_agent_message(context, "result", text)

        self.assertEqual(message_id, "F123")
        self.assertEqual(
            controller.im_client.uploaded_markdowns,
            [("C1", "result.md", text, "markdown")],
        )
        self.assertEqual(
            controller.im_client.sent_messages,
            [("C1", "⚠️ The message could not be sent inline, so I sent it as `result.md` above.", "markdown")],
        )
        self.assertEqual(controller.session_handler.calls, [])


class _AvibeStatusController(_StubController):
    """avibe controller stub that records the sidebar-dot writes."""

    def __init__(self):
        super().__init__(platform="avibe")
        self.status_calls = []
        self.active_sink = None  # set to {"turn_token": ...} to simulate a live turn
        from core.session_turns import SessionTurnManager

        # The dot now settles via the turn owner (FSM); wire a real one so its
        # on_terminal_result reaches this stub's set_agent_status recorder.
        self.session_turns = SessionTurnManager(self)

    @staticmethod
    def _session_id_from_context(context):
        return ((context.platform_specific or {}).get("agent_session_id")) or None

    def get_turn_sink(self, session_key):
        return self.active_sink

    def set_agent_status(self, session_id, status):
        self.status_calls.append((session_id, status))


def _avibe_ctx():
    return MessageContext(
        user_id="U1",
        channel_id="ses-1",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-1"},
    )


class MessageDispatcherStatusChokepointTests(unittest.IsolatedAsyncioTestCase):
    """The OUTBOUND status chokepoint: a terminal ``result`` settles the avibe dot
    (idle, or failed when ``is_error``); a ``notify`` is not terminal and leaves it."""

    async def test_terminal_result_settles_dot_idle(self):
        controller = _AvibeStatusController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(_avibe_ctx(), "result", "")
        self.assertEqual(controller.status_calls, [("ses-1", "idle")])

    async def test_terminal_error_result_settles_dot_failed(self):
        controller = _AvibeStatusController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(_avibe_ctx(), "result", "", is_error=True)
        self.assertEqual(controller.status_calls, [("ses-1", "failed")])

    async def test_notify_does_not_settle_dot(self):
        controller = _AvibeStatusController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(_avibe_ctx(), "notify", "fyi")
        self.assertEqual(controller.status_calls, [])

    async def test_superseded_turn_result_does_not_settle_dot(self):
        # A late result whose turn_token != the active sink's token (a stopped or
        # superseded turn) must NOT settle the dot for the new active turn.
        controller = _AvibeStatusController()
        controller.active_sink = {"turn_token": "new-turn"}
        dispatcher = ConsolidatedMessageDispatcher(controller)
        ctx = MessageContext(
            user_id="U1",
            channel_id="ses-1",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-1", "turn_token": "old-turn"},
        )
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(ctx, "result", "", is_error=True)
        self.assertEqual(controller.status_calls, [])

    async def test_active_turn_token_match_settles_dot(self):
        # Same token (the live turn) → the dot settles normally.
        controller = _AvibeStatusController()
        controller.active_sink = {"turn_token": "turn-1"}
        dispatcher = ConsolidatedMessageDispatcher(controller)
        ctx = MessageContext(
            user_id="U1",
            channel_id="ses-1",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-1", "turn_token": "turn-1"},
        )
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(ctx, "result", "")
        self.assertEqual(controller.status_calls, [("ses-1", "idle")])

    async def test_tokenless_result_does_not_settle_dot_when_live_turn_exists(self):
        # A live interactive turn registered a sink WITH a token; an older
        # scheduled/watch result arrives tokenless (scheduled runs register no sink).
        # It must NOT settle the live turn's dot — previously the guard fail-opened on
        # the absent token, so the stale result flipped the live turn to idle (Codex P2).
        controller = _AvibeStatusController()
        controller.active_sink = {"turn_token": "live-turn"}
        dispatcher = ConsolidatedMessageDispatcher(controller)
        ctx = MessageContext(
            user_id="U1",
            channel_id="ses-1",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-1"},  # no turn_token (scheduled)
        )
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await dispatcher.emit_agent_message(ctx, "result", "")
        self.assertEqual(controller.status_calls, [])

    async def test_silent_result_settles_dot_but_suppresses_delivery(self):
        # ``level="silent"`` is the explicit visibility grade (orthogonal to type):
        # a terminal result still settles the dot + releases the stream, but is NOT
        # delivered or persisted — even with NON-EMPTY text. This is what an
        # intentional stop emits: the turn ends cleanly with no user-facing bubble,
        # replacing the old "fake invisibility via empty text" trick.
        controller = _AvibeStatusController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(
                _avibe_ctx(), "result", "🛑 stopped", level="silent"
            )
        self.assertIsNone(message_id)
        self.assertEqual(controller.status_calls, [("ses-1", "idle")])  # dot still settles
        persist.assert_not_called()  # never recorded in history
        self.assertEqual(controller.im_client.sent_messages, [])  # no user-facing bubble

    async def test_silent_notify_is_not_terminal_and_suppressed(self):
        # A silent NON-result (notify) is not terminal: it neither settles the dot
        # nor is delivered/persisted.
        controller = _AvibeStatusController()
        dispatcher = ConsolidatedMessageDispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(
                _avibe_ctx(), "notify", "fyi", level="silent"
            )
        self.assertIsNone(message_id)
        self.assertEqual(controller.status_calls, [])
        persist.assert_not_called()
        self.assertEqual(controller.im_client.sent_messages, [])


if __name__ == "__main__":
    unittest.main()
