import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_EVENT_HANDLER_PATH = Path(__file__).resolve().parents[1] / "modules/agents/codex/event_handler.py"
_SPEC = importlib.util.spec_from_file_location("test_codex_event_handler_module", _EVENT_HANDLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
CodexEventHandler = _MODULE.CodexEventHandler


class _TurnState:
    def __init__(self, turn_id: str, request):
        self.turn_id = turn_id
        self.request = request
        self.pending_assistant = None
        self.terminal_error = None
        self.terminal_error_notified = False
        self.visible_to_user = True


class _StubTurnRegistry:
    def __init__(self):
        self._turns = {}
        self._active_turns = {}

    def register_turn(self, turn_id: str, request):
        state = self._turns.get(turn_id)
        if state is None:
            state = _TurnState(turn_id, request)
            self._turns[turn_id] = state
        else:
            state.request = request
        self._active_turns[request.base_session_id] = turn_id
        return state

    def get_turn(self, turn_id: str):
        return self._turns.get(turn_id)

    def pop_turn(self, turn_id: str):
        state = self._turns.pop(turn_id, None)
        if state and self._active_turns.get(state.request.base_session_id) == turn_id:
            self._active_turns.pop(state.request.base_session_id, None)
        return state

    def hide_turn(self, turn_id: str):
        state = self._turns.get(turn_id)
        if not state:
            return None
        state.visible_to_user = False
        state.pending_assistant = None
        state.terminal_error = None
        state.terminal_error_notified = False
        if self._active_turns.get(state.request.base_session_id) == turn_id:
            self._active_turns.pop(state.request.base_session_id, None)
        return state

    def should_emit_progress(self, turn_id: str) -> bool:
        return self.should_emit_result(turn_id)

    def should_emit_terminal_error(self, turn_id: str) -> bool:
        return self.should_emit_result(turn_id)

    def should_emit_result(self, turn_id: str) -> bool:
        state = self._turns.get(turn_id)
        if not state:
            return False
        return state.visible_to_user and self._active_turns.get(state.request.base_session_id) == turn_id

    def is_active_turn(self, turn_id: str) -> bool:
        state = self._turns.get(turn_id)
        if not state:
            return False
        return self._active_turns.get(state.request.base_session_id) == turn_id


class _StubAgent:
    def __init__(self):
        self._turn_registry = _StubTurnRegistry()
        self._session_mgr = SimpleNamespace(set_thread_id=Mock(), get_thread_id=Mock(return_value=None))
        self.controller = SimpleNamespace(
            config=SimpleNamespace(reply_enhancements=True),
            emit_agent_message=AsyncMock(),
            agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
        )
        self.emit_result_message = AsyncMock()
        self._remove_ack_reaction = AsyncMock()
        self._maybe_backfill_session_title = Mock()

    def bind_agent_session_id(self, request, native_session_id):
        payload = dict(request.context.platform_specific or {})
        payload["agent_session_id"] = "sesk8m4q2p7x"
        request.context.platform_specific = payload
        return "sesk8m4q2p7x"

    def _get_formatter(self, context):
        return SimpleNamespace(format_system_message=lambda *args: "system message")


class CodexEventHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_started_binds_native_session_through_shared_lifecycle(self):
        agent = _StubAgent()
        agent.bind_agent_session_id = Mock(wraps=agent.bind_agent_session_id)
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(
            base_session_id="session-1",
            session_key="slack::channel::C1",
            working_path="/repo",
            context=SimpleNamespace(platform_specific={}),
        )

        await handler._on_thread_started({"thread": {"id": "thread-1"}}, request)

        agent._session_mgr.set_thread_id.assert_called_once_with("session-1", "thread-1")
        agent.bind_agent_session_id.assert_called_once_with(request, "thread-1")
        self.assertEqual(request.context.platform_specific["agent_session_id"], "sesk8m4q2p7x")
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_thread_started_does_not_bind_while_fork_correction_is_pending(self):
        agent = _StubAgent()
        agent.is_fork_correction_pending = Mock(return_value=True)
        agent.bind_agent_session_id = Mock(wraps=agent.bind_agent_session_id)
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(
            base_session_id="session-1",
            session_key="slack::channel::C1",
            working_path="/repo",
            context=SimpleNamespace(platform_specific={}),
        )

        await handler._on_thread_started({"thread": {"id": "thread-1"}}, request)

        agent.is_fork_correction_pending.assert_called_once_with("session-1")
        agent._session_mgr.set_thread_id.assert_not_called()
        agent.bind_agent_session_id.assert_not_called()
        self.assertNotIn("agent_session_id", request.context.platform_specific)

    async def test_retrying_error_is_suppressed(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_error(
            {
                "error": {"message": "Reconnecting... 5/5"},
                "willRetry": True,
                "turnId": "turn-1",
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_terminal_turn_error_is_emitted_immediately_and_not_duplicated_on_completion(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_error(
            {
                "error": {"message": "unexpected status 401 Unauthorized:"},
                "willRetry": False,
                "turnId": "turn-1",
            },
            request,
        )

        agent.controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once_with(
            request.context,
            "codex",
            "❌ Codex turn failed: unexpected status 401 Unauthorized:",
            output=ANY,
            terminal_error="unexpected status 401 Unauthorized:",
        )
        self.assertEqual(agent.controller.emit_agent_message.await_count, 2)
        notify_call, terminal_call = agent.controller.emit_agent_message.await_args_list
        self.assertEqual(
            notify_call.args[:3],
            (request.context, "notify", "❌ Codex turn failed: unexpected status 401 Unauthorized:"),
        )
        self.assertEqual(terminal_call.args[:3], (request.context, "result", ""))
        self.assertTrue(terminal_call.kwargs["is_error"])
        self.assertEqual(terminal_call.kwargs["level"], "silent")
        self.assertEqual(terminal_call.kwargs["terminal_error"], "unexpected status 401 Unauthorized:")

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "fallback message"},
                }
            },
            request,
        )

        assert agent.controller.emit_agent_message.await_count == 2
        agent._remove_ack_reaction.assert_awaited_once_with(request)

    async def test_turn_failure_falls_back_to_completion_error_when_no_error_notification_arrives(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "fallback message"},
                }
            },
            request,
        )

        agent.controller.agent_auth_service.maybe_emit_auth_recovery_message.assert_awaited_once_with(
            request.context,
            "codex",
            "❌ Codex turn failed: fallback message",
            output=ANY,
            terminal_error="fallback message",
        )
        self.assertEqual(agent.controller.emit_agent_message.await_count, 2)
        notify_call, terminal_call = agent.controller.emit_agent_message.await_args_list
        self.assertEqual(
            notify_call.args[:3],
            (request.context, "notify", "❌ Codex turn failed: fallback message"),
        )
        self.assertEqual(terminal_call.args[:3], (request.context, "result", ""))
        self.assertEqual(terminal_call.kwargs["terminal_error"], "fallback message")
        agent._remove_ack_reaction.assert_awaited_once_with(request)

    async def test_unknown_turn_error_is_logged_without_emitting(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)

        await handler._on_error(
            {
                "error": {"message": "old turn failed"},
                "willRetry": False,
                "turnId": "turn-old",
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()

    def test_clear_pending_hides_turn_and_returns_request(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        cleared_request = handler.clear_pending("turn-1")

        assert cleared_request is request
        turn_state = agent._turn_registry.get_turn("turn-1")
        assert turn_state is not None
        assert turn_state.visible_to_user is False

    async def test_hidden_turn_error_is_logged_without_emitting(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)
        handler.clear_pending("turn-1")

        await handler._on_error(
            {
                "error": {"message": "interrupted turn failed"},
                "willRetry": False,
                "turnId": "turn-1",
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_hidden_turn_failure_completion_does_not_clear_reaction_without_user_message(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)
        handler.clear_pending("turn-1")

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "hidden failure"},
                }
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()
        agent._remove_ack_reaction.assert_not_awaited()

    async def test_inactive_completion_does_not_clear_reaction_without_result_message(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        stale_request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        active_request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", stale_request)
        agent._turn_registry.register_turn("turn-2", active_request)

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                }
            },
            stale_request,
        )

        agent.emit_result_message.assert_not_awaited()
        agent._remove_ack_reaction.assert_not_awaited()

    async def test_interrupted_completion_releases_runtime_gate_without_result(self):
        agent = _StubAgent()
        release_runtime_turn = Mock()
        agent.controller.agent_service = SimpleNamespace(release_runtime_turn=release_runtime_turn)
        handler = CodexEventHandler(agent)
        context = SimpleNamespace(
            platform_specific={
                "agent_runtime_turn_key": "session-1:/repo",
                "agent_runtime_turn_token": "R1",
            }
        )
        request = SimpleNamespace(base_session_id="session-1", context=context, started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "interrupted",
                }
            },
            request,
        )

        agent.emit_result_message.assert_not_awaited()
        release_runtime_turn.assert_called_once_with(context)

    async def test_auth_recovery_message_suppresses_plain_notify(self):
        agent = _StubAgent()
        agent.controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_error(
            {
                "error": {"message": "unexpected status 401 Unauthorized:"},
                "willRetry": False,
                "turnId": "turn-1",
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_auth_recovery_completion_still_clears_reaction(self):
        agent = _StubAgent()
        agent.controller.agent_auth_service.maybe_emit_auth_recovery_message = AsyncMock(return_value=True)
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        await handler._on_turn_completed(
            {
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "unexpected status 401 Unauthorized:"},
                }
            },
            request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()
        agent._remove_ack_reaction.assert_awaited_once_with(request)

    async def test_inactive_turn_item_is_ignored(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        stale_request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", stale_request)
        agent._turn_registry.register_turn("turn-2", request)

        await handler._on_item_completed(
            {
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "old output"},
            },
            stale_request,
        )

        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_empty_success_result_falls_back_to_new_thread_generated_images(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            old_image = thread_dir / "old.png"
            old_image.write_bytes(b"old")
            handler.snapshot_generated_images("thread-1", "session-1")
            handler.bind_generated_image_snapshot("thread-1", "turn-1", "session-1")

            new_image = thread_dir / "new image.png"
            new_image.write_bytes(b"new")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
                request,
            )

        agent.emit_result_message.assert_awaited_once()
        result_text = agent.emit_result_message.await_args.args[1]
        assert "Generated image:" in result_text
        assert f"![generated image]({new_image.resolve().as_uri()})" in result_text
        assert str(old_image.resolve()) not in result_text
        agent._maybe_backfill_session_title.assert_called_once_with(request, "thread-1")

    async def test_empty_success_result_falls_back_when_generated_image_path_is_reused(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            image = thread_dir / "image.png"
            image.write_bytes(b"old")
            handler.snapshot_generated_images("thread-1", "session-1")
            handler.bind_generated_image_snapshot("thread-1", "turn-1", "session-1")

            image.write_bytes(b"new-content")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
                request,
            )

        agent.emit_result_message.assert_awaited_once()
        result_text = agent.emit_result_message.await_args.args[1]
        assert f"![generated image]({image.resolve().as_uri()})" in result_text

    async def test_stale_interrupted_turn_does_not_clear_new_turn_image_snapshot(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            before_second_turn = thread_dir / "before-second-turn.png"
            before_second_turn.write_bytes(b"old")

            handler.snapshot_generated_images("thread-1", "session-1")
            handler.bind_generated_image_snapshot("thread-1", "turn-1", "session-1")

            agent._turn_registry.register_turn("turn-2", request)
            handler.snapshot_generated_images("thread-1", "session-1")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
                request,
            )

            new_image = thread_dir / "new-for-turn-2.png"
            new_image.write_bytes(b"new")
            handler.bind_generated_image_snapshot("thread-1", "turn-2", "session-1")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-2", "status": "completed"},
                },
                request,
            )

        result_text = agent.emit_result_message.await_args.args[1]
        assert f"![generated image]({new_image.resolve().as_uri()})" in result_text
        assert str(before_second_turn.resolve()) not in result_text

    async def test_stale_turn_started_does_not_steal_pending_image_snapshot(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)
        handler.clear_pending("turn-1")
        agent._turn_registry.register_turn("turn-2", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            handler.snapshot_generated_images("thread-1", "session-1")

            await handler._on_turn_started(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1"},
                },
                request,
            )

            new_image = thread_dir / "new-for-turn-2.png"
            new_image.write_bytes(b"new")
            handler.bind_generated_image_snapshot("thread-1", "turn-2", "session-1")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-2", "status": "completed"},
                },
                request,
            )

        result_text = agent.emit_result_message.await_args.args[1]
        assert f"![generated image]({new_image.resolve().as_uri()})" in result_text

    async def test_empty_success_result_does_not_guess_without_image_snapshot(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            image = thread_dir / "new.png"
            image.write_bytes(b"new")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
                request,
            )

        agent.emit_result_message.assert_awaited_once()
        assert agent.emit_result_message.await_args.args[1] is None
        assert handler._image_snapshots_by_turn == {}

    async def test_generated_image_fallback_ignores_quick_reply_button_setting(self):
        agent = _StubAgent()
        agent.controller.config.reply_enhancements = False
        handler = CodexEventHandler(agent)
        request = SimpleNamespace(base_session_id="session-1", context=object(), started_at=0)
        agent._turn_registry.register_turn("turn-1", request)

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"CODEX_HOME": tmpdir}):
            thread_dir = Path(tmpdir) / "generated_images" / "thread-1"
            thread_dir.mkdir(parents=True)
            handler.snapshot_generated_images("thread-1", "session-1")
            handler.bind_generated_image_snapshot("thread-1", "turn-1", "session-1")
            image = thread_dir / "new.png"
            image.write_bytes(b"new")

            await handler._on_turn_completed(
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
                request,
            )

        agent.emit_result_message.assert_awaited_once()
        result = agent.emit_result_message.await_args.args[1]
        assert "![generated image](" in result

    def test_generated_images_dir_rejects_dot_segment_thread_ids(self):
        agent = _StubAgent()
        handler = CodexEventHandler(agent)

        assert handler._generated_images_dir(".") is None
        assert handler._generated_images_dir("..") is None
        assert handler._generated_images_dir("../other") is None


if __name__ == "__main__":
    unittest.main()
