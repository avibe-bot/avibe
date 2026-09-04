import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def _wait_capture_tasks(handler) -> None:
    adapter = handler.controller.memory_adapter
    while adapter.capture_tasks:
        tasks = tuple(adapter.capture_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

from modules.im import MessageContext
from modules.sessions_facade import SessionsFacade
from core.processing_indicator import ProcessingIndicatorService


def _load_message_handler_class():
    with patch.dict(sys.modules, {}, clear=False):
        agents_module = types.ModuleType("modules.agents")
        agents_module.__path__ = [str(ROOT / "modules" / "agents")]

        @dataclass
        class _AgentRequest:
            context: MessageContext
            message: str
            user_message: str
            working_path: str
            base_session_id: str
            composite_session_id: str
            session_key: str
            ack_message_id: str | None = None
            subagent_name: str | None = None
            subagent_key: str | None = None
            subagent_model: str | None = None
            subagent_reasoning_effort: str | None = None
            processing_indicator: object | None = None
            ack_reaction_message_id: str | None = None
            ack_reaction_emoji: str | None = None
            typing_indicator_active: bool = False
            typing_indicator_task: asyncio.Task | None = None
            vibe_agent_id: str | None = None
            vibe_agent_name: str | None = None
            vibe_agent_backend: str | None = None
            vibe_agent_model: str | None = None
            vibe_agent_reasoning_effort: str | None = None
            vibe_agent_model_explicit: bool = False
            vibe_agent_reasoning_effort_explicit: bool = False
            vibe_agent_system_prompt: str | None = None
            files: list | None = None

        setattr(agents_module, "AgentRequest", _AgentRequest)
        sys.modules["modules.agents"] = agents_module
        agents_base_module = types.ModuleType("modules.agents.base")
        setattr(agents_base_module, "AgentRequest", _AgentRequest)
        sys.modules["modules.agents.base"] = agents_base_module

        core_pkg = types.ModuleType("core")
        core_pkg.__path__ = [str(ROOT / "core")]
        sys.modules["core"] = core_pkg

        handlers_pkg = types.ModuleType("core.handlers")
        handlers_pkg.__path__ = [str(ROOT / "core" / "handlers")]
        sys.modules["core.handlers"] = handlers_pkg

        base_name = "core.handlers.base"
        base_spec = importlib.util.spec_from_file_location(base_name, ROOT / "core" / "handlers" / "base.py")
        assert base_spec is not None
        assert base_spec.loader is not None
        base_module = importlib.util.module_from_spec(base_spec)
        sys.modules[base_name] = base_module
        base_spec.loader.exec_module(base_module)

        module_name = "core.handlers.message_handler"
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / "handlers" / "message_handler.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module.MessageHandler


MessageHandler = _load_message_handler_class()


class _StubSessions:
    def is_message_already_processed(self, channel_id, thread_ts, message_ts):
        return False

    def record_processed_message(self, channel_id, thread_ts, message_ts):
        return None


class _StubSettingsManager:
    def __init__(self):
        self.sessions = _StubSessions()
        self.routing = None

    def get_channel_routing(self, settings_key):
        return self.routing


class _StubIMClient:
    def __init__(self, *, typing_result: bool):
        self.typing_result = typing_result
        self.typing_calls = []
        self.clear_calls = []
        self.reactions = []
        self.sent_messages = []
        self.removed_keyboards = []
        self.formatter = type("Formatter", (), {"format_error": staticmethod(lambda text: text)})()

    def should_use_thread_for_reply(self):
        return False

    async def prepare_turn_context(self, context, source):
        return context

    async def get_user_info(self, user_id):
        return {"display_name": f"user:{user_id}"}

    async def download_file_to_path(self, file_info, target_path):
        self.sent_messages.append(("download", file_info["name"], target_path))
        from modules.im.base import FileDownloadResult

        return FileDownloadResult(False, "not implemented")

    async def send_typing_indicator(self, context):
        self.typing_calls.append((context.channel_id, context.user_id))
        return self.typing_result

    async def clear_typing_indicator(self, context):
        self.clear_calls.append((context.channel_id, context.user_id))
        return True

    async def add_reaction(self, context, message_id, emoji):
        self.reactions.append((context.channel_id, message_id, emoji))
        return True

    async def remove_reaction(self, context, message_id, emoji):
        self.reactions.append((context.channel_id, message_id, f"remove:{emoji}"))
        return True

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append((context.channel_id, text))
        return "msg-1"

    async def remove_inline_keyboard(self, context, message_id, text=None, parse_mode=None):
        self.removed_keyboards.append((context.channel_id, context.platform, message_id))
        return True


class _StubAgentService:
    def __init__(self):
        self.default_agent = "codex"
        self.requests = []
        self.stop_requests = []
        self.error = None
        self.stop_result = True

    async def handle_message(self, agent_name, request):
        if self.error is not None:
            raise self.error
        self.requests.append((agent_name, request))

    async def handle_stop(self, agent_name, request):
        self.stop_requests.append((agent_name, request))
        return self.stop_result


class _RecordingMemoryAdapter:
    def __init__(self) -> None:
        self.events = []

    @property
    def capture_tasks(self) -> set:
        return set()

    def offer(self, event) -> None:
        self.events.append(event)

    def quiesce_memory_capture_tasks(self) -> None:
        return None

    async def cancel_memory_capture_tasks(self) -> None:
        return None


class _StubController:
    def __init__(self, *, platform: str, ack_mode: str, typing_result: bool):
        self.config = type(
            "Config",
            (),
            {
                "platform": platform,
                "ack_mode": ack_mode,
                "include_time_info": False,
                "include_user_info": False,
                "language": "en",
            },
        )()
        self.im_client = _StubIMClient(typing_result=typing_result)
        self.settings_manager = _StubSettingsManager()
        self.session_manager = object()
        self.session_handler = None
        self.receiver_tasks = {}
        self.agent_service = _StubAgentService()
        self.settings_handler = type("Settings", (), {})()
        self.command_handler = type("Cmd", (), {"handle_start": staticmethod(lambda context, args: None)})()
        self.agent_auth_service = type("Auth", (), {})()
        self.processing_indicator = ProcessingIndicatorService(self)
        self.memory_adapter = _RecordingMemoryAdapter()

    def update_thread_message_id(self, context):
        return None

    async def emit_agent_message(
        self,
        context,
        message_type,
        text,
        parse_mode="markdown",
        *,
        is_error=False,
        level="normal",
        output=None,
        terminal_error=None,
        delivery=None,
    ):
        # Terminal error results settle the dot + release the SSE waiter via the
        # outbound chokepoint; a no-op here (these are IM turns, no workbench dot).
        if message_type == "notify":
            delivered_id = await self.get_im_client_for_context(context).send_message(
                context,
                text,
            )
            if delivery is not None:
                delivery.send_returned = True
                delivery.delivered_id = delivered_id
            return delivered_id
        return None

    def get_im_client_for_context(self, context):
        return self.im_client

    def resolve_agent_for_context(self, context):
        return "codex"

    def resolve_vibe_agent_for_context(
        self,
        context,
        override_agent_id=None,
        override_agent_name=None,
        required=False,
    ):
        if override_agent_name:
            return type(
                "VibeAgent",
                (),
                {
                    "id": f"agent-{override_agent_name}",
                    "name": override_agent_name,
                    "backend": "claude",
                    "model": "claude-opus",
                    "reasoning_effort": None,
                    "system_prompt": "Explicit prompt",
                },
            )()
        return type(
            "VibeAgent",
            (),
            {
                "id": "agent-default",
                "name": "default",
                "backend": "codex",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "system_prompt": "Default prompt",
            },
        )()

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{getattr(context, 'platform', None) or 'test'}::{self._get_settings_key(context)}"

    def _get_lang(self):
        return "en"


def _capture_reservation(generation: int | None = 1):
    return types.SimpleNamespace(config_generation=generation, release=Mock())


class _StubSessionHandler:
    def __init__(self):
        self.alias_calls = []

    @staticmethod
    def get_session_info(context, source="human"):
        return ("base-session", "/tmp", "base-session:/tmp")

    @staticmethod
    def should_allocate_scheduled_anchor(context, source="human"):
        return False

    def alias_session_base(self, context, *, source_base_session_id, alias_base_session_id, clear_source=False):
        self.alias_calls.append(
            {
                "source_base_session_id": source_base_session_id,
                "alias_base_session_id": alias_base_session_id,
                "clear_source": clear_source,
            }
        )
        return False


class MessageHandlerTypingTests(unittest.IsolatedAsyncioTestCase):
    async def test_im_human_input_enters_delivery_owner_before_backend(self):
        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
        controller.settings_manager.sessions.try_record_processed_message = Mock()
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._admit_human_delivery = AsyncMock(return_value=True)
        handler._is_duplicate_human_delivery = Mock(return_value=False)
        handler._prepend_message_metadata = AsyncMock(
            return_value="[metadata]\nhello"
        )
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
        )

        await handler.handle_user_message(context, "hello")

        handler._admit_human_delivery.assert_awaited_once()
        assert (
            handler._admit_human_delivery.await_args.kwargs["dispatch_text"]
            == "[metadata]\nhello"
        )
        controller.settings_manager.sessions.try_record_processed_message.assert_not_called()
        self.assertEqual(controller.agent_service.requests, [])

    async def test_explicit_opencode_subagent_uses_new_turn_delivery_intent(self):
        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
        server = types.SimpleNamespace(
            ensure_running=AsyncMock(),
            get_available_agents=AsyncMock(return_value=[{"name": "reviewer"}]),
        )
        controller.agent_service.agents = {
            "opencode": types.SimpleNamespace(
                _get_server=AsyncMock(return_value=server)
            )
        }
        controller.get_cwd = Mock(return_value="/tmp")
        controller.resolve_vibe_agent_for_context = Mock(
            return_value=types.SimpleNamespace(
                id="agent-opencode",
                name="opencode",
                backend="opencode",
                model=None,
                reasoning_effort=None,
                system_prompt=None,
            )
        )
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._admit_human_delivery = AsyncMock(return_value=True)
        handler._is_duplicate_human_delivery = Mock(return_value=False)
        handler._prepend_message_metadata = AsyncMock(return_value="check this")
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m-subagent",
            platform="slack",
        )

        await handler.handle_user_message(context, "reviewer: check this")

        call = handler._admit_human_delivery.await_args.kwargs
        self.assertEqual(call["delivery_intent"], "queue")
        self.assertEqual(
            call["admission_context"]["message_handler_route"]["subagent_name"],
            "reviewer",
        )
        self.assertEqual(
            call["admission_context"]["message_handler_route"]["base_session_id"],
            "base-session",
        )
        self.assertEqual(controller.agent_service.requests, [])

    async def test_duplicate_im_attachment_skips_download_before_admission(self):
        from modules.im.base import FileAttachment

        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._is_duplicate_human_delivery = Mock(return_value=True)
        handler._materialize_file_attachments = AsyncMock()
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m-attachment-retry",
            platform="slack",
            files=[
                FileAttachment(
                    name="report.pdf",
                    mimetype="application/pdf",
                    url="https://files.example/report.pdf",
                )
            ],
        )

        await handler.handle_user_message(context, "review this")

        handler._materialize_file_attachments.assert_not_awaited()
        controller.session_turns.deliver.assert_not_awaited()
        self.assertEqual(controller.agent_service.requests, [])

    async def test_durable_attachment_lease_stays_active_until_admission_owns_it(self):
        from modules.im.base import FileAttachment

        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._is_duplicate_human_delivery = Mock(return_value=False)
        handler._prepend_message_metadata = AsyncMock(return_value="review this")
        lease = Mock()
        attachment = FileAttachment(
            name="report.pdf",
            mimetype="application/pdf",
            local_path="/tmp/leased-report.pdf",
            size=10,
        )
        handler._materialize_file_attachments = AsyncMock(
            return_value=types.SimpleNamespace(
                attachments=(attachment,),
                display_errors=(),
                lease=lease,
            )
        )

        async def admit(**kwargs):
            self.assertIs(kwargs["attachment_lease"], lease)
            lease.adopt.assert_not_called()
            lease.release.assert_not_called()
            lease.adopt()
            lease.release()
            return True

        handler._admit_human_delivery = AsyncMock(side_effect=admit)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m-attachment",
            platform="slack",
            files=[FileAttachment("report.pdf", "application/pdf", url="private")],
        )

        await handler.handle_user_message(context, "review this")

        lease.adopt.assert_called_once_with()
        lease.release.assert_called_once_with()

    async def test_failed_durable_admission_releases_unowned_attachment_lease(self):
        from modules.im.base import FileAttachment

        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._is_duplicate_human_delivery = Mock(return_value=False)
        handler._prepend_message_metadata = AsyncMock(return_value="review this")
        handler._emit_agent_dispatch_failure = AsyncMock()
        lease = Mock()
        attachment = FileAttachment(
            name="report.pdf",
            mimetype="application/pdf",
            local_path="/tmp/leased-report.pdf",
            size=10,
        )
        handler._materialize_file_attachments = AsyncMock(
            return_value=types.SimpleNamespace(
                attachments=(attachment,),
                display_errors=(),
                lease=lease,
            )
        )
        handler._admit_human_delivery = AsyncMock(
            side_effect=RuntimeError("admission rejected")
        )
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m-rejected-attachment",
            platform="slack",
            files=[FileAttachment("report.pdf", "application/pdf", url="private")],
        )

        await handler.handle_user_message(context, "review this")

        lease.adopt.assert_not_called()
        lease.release.assert_called_once_with()

    async def test_inline_stop_uses_durable_empty_p0_owner(self):
        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        cancel = AsyncMock(return_value={"ok": True, "status": "cancel_requested"})
        controller.session_turns = types.SimpleNamespace(
            deliver=AsyncMock(),
            cancel=cancel,
        )
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            thread_id="T1",
            message_id="m-stop",
            platform="slack",
            platform_specific={
                "agent_session_id": "ses-durable",
                "control_text": "stop",
            },
        )

        await handler.handle_user_message(context, "stop")

        cancel.assert_awaited_once_with("ses-durable")
        self.assertEqual(controller.agent_service.stop_requests, [])
        self.assertEqual(controller.agent_service.requests, [])

    async def test_retried_inline_stop_cannot_cancel_a_newer_turn(self):
        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        cancel = AsyncMock(return_value={"ok": True, "status": "cancel_requested"})
        controller.session_turns = types.SimpleNamespace(
            deliver=AsyncMock(),
            cancel=cancel,
        )
        controller.settings_manager.sessions.try_record_processed_message = Mock(
            side_effect=[True, False]
        )
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            thread_id="T1",
            message_id="m-stop-retry",
            platform="slack",
            platform_specific={
                "agent_session_id": "ses-durable",
                "control_text": "stop",
            },
        )

        await handler.handle_user_message(context, "stop")
        await handler.handle_user_message(context, "stop")

        cancel.assert_awaited_once_with("ses-durable")
        self.assertEqual(controller.agent_service.stop_requests, [])
        self.assertEqual(controller.agent_service.requests, [])

    async def test_pre_request_failure_uses_plain_terminal_output(self):
        controller = _StubController(
            platform="slack",
            ack_mode="reaction",
            typing_result=True,
        )
        controller.emit_agent_message = AsyncMock(return_value=None)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._prepare_turn_context = AsyncMock(
            side_effect=RuntimeError("context preparation failed")
        )
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
        )

        error = await handler._handle_turn(
            context,
            "hello",
            source=handler.TURN_SOURCE_HUMAN,
        )

        self.assertEqual(error, "context preparation failed")
        self.assertEqual(controller.emit_agent_message.await_count, 2)
        notify_call, call = controller.emit_agent_message.await_args_list
        self.assertEqual(
            notify_call.args[:3],
            (context, "notify", "Error: context preparation failed"),
        )
        self.assertEqual(call.args[:3], (context, "result", ""))
        output = call.kwargs["output"]
        self.assertTrue(output.completes_turn)
        self.assertTrue(output.settles_run)
        self.assertIsNone(output.activity_id)

    async def test_wechat_forces_typing_even_when_ack_mode_is_message(self):
        controller = _StubController(platform="wechat", ack_mode="message", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="U1", channel_id="C1", message_id="m1")

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertIsNone(request.ack_message_id)
        self.assertTrue(request.typing_indicator_active)
        self.assertEqual(controller.im_client.sent_messages, [])
        self.assertEqual(controller.im_client.reactions, [])
        self.assertGreaterEqual(len(controller.im_client.typing_calls), 1)

        await handler._remove_ack_reaction(context, request)
        self.assertEqual(controller.im_client.clear_calls, [("C1", "U1")])

    async def test_typing_mode_falls_back_to_reaction_when_platform_lacks_typing(self):
        controller = _StubController(platform="slack", ack_mode="typing", typing_result=False)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="U1", channel_id="C1", message_id="m1")

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertFalse(request.typing_indicator_active)
        # Reaction is selected but deferred to the runtime gate (not added eagerly).
        self.assertTrue(request.processing_indicator.reaction_indicator_selected)
        self.assertIsNone(request.ack_reaction_message_id)
        self.assertEqual(controller.im_client.reactions, [])
        # Turn start (gate acquired) promotes to the running 👀 on the message.
        await controller.processing_indicator.promote_reaction_to_running(request)
        self.assertEqual(request.ack_reaction_message_id, "m1")
        self.assertEqual(request.ack_reaction_emoji, "👀")
        self.assertEqual(controller.im_client.reactions, [("C1", "m1", "👀")])

    async def test_regular_message_passes_default_vibe_agent_metadata(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="U1", channel_id="C1", message_id="m1", platform="slack")

        await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "codex")
        self.assertEqual(request.vibe_agent_id, "agent-default")
        self.assertEqual(request.vibe_agent_name, "default")
        self.assertEqual(request.vibe_agent_backend, "codex")
        self.assertEqual(request.vibe_agent_model, "gpt-5.4")
        self.assertEqual(request.vibe_agent_reasoning_effort, "high")
        self.assertEqual(request.vibe_agent_system_prompt, "Default prompt")
        self.assertEqual(
            request.context.platform_specific["resolved_vibe_agent"],
            {"id": "agent-default", "name": "default", "backend": "codex"},
        )

    async def test_scope_model_and_reasoning_override_vibe_agent_defaults(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.settings_manager.routing = type(
            "Routing",
            (),
            {
                "agent_name": None,
                "agent_backend": None,
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
            },
        )()
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="U1", channel_id="C1", message_id="m1", platform="slack")

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertEqual(request.vibe_agent_model, "gpt-5.5")
        self.assertEqual(request.vibe_agent_reasoning_effort, "xhigh")
        self.assertEqual(request.vibe_agent_system_prompt, "Default prompt")

    async def test_claude_specific_scope_reasoning_overrides_vibe_agent_default(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)

        def _resolve_claude_agent(context, override_agent_name=None, required=False):
            return type(
                "VibeAgent",
                (),
                {
                    "id": "agent-claude-opus",
                    "name": "claude-opus",
                    "backend": "claude",
                    "model": "claude-opus-4-8",
                    "reasoning_effort": "high",
                    "system_prompt": "Claude prompt",
                },
            )()

        controller.resolve_vibe_agent_for_context = _resolve_claude_agent
        controller.settings_manager.routing = type(
            "Routing",
            (),
            {
                "agent_name": "claude-opus",
                "model": "claude-opus-4-8",
                "reasoning_effort": "max",
                "opencode_agent": None,
                "claude_agent": None,
                "claude_model": None,
                "claude_reasoning_effort": None,
                "codex_agent": None,
            },
        )()
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="U1", channel_id="C1", message_id="m1", platform="slack")

        await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertEqual(request.vibe_agent_model, "claude-opus-4-8")
        self.assertEqual(request.vibe_agent_reasoning_effort, "max")
        self.assertEqual(request.vibe_agent_system_prompt, "Claude prompt")

    async def test_avibe_target_variant_applies_to_claude_request(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)

        def _resolve_claude_agent(context, override_agent_name=None, required=False):
            return type(
                "VibeAgent",
                (),
                {
                    "id": "agent-claude",
                    "name": "claude",
                    "backend": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "system_prompt": None,
                },
            )()

        controller.resolve_vibe_agent_for_context = _resolve_claude_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="workbench",
            channel_id="ses-1",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_run_target": {
                    "agent_backend": "claude",
                    "agent_variant": "reviewer",
                    "workdir": "/repo/work",
                    "model": "claude-opus-4-8",
                    "reasoning_effort": "max",
                }
            },
        )

        with patch("modules.agents.subagent_router.load_claude_subagent", return_value=None):
            await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertEqual(request.subagent_name, "reviewer")
        self.assertEqual(request.base_session_id, "base-session:reviewer")
        self.assertEqual(request.vibe_agent_model, "claude-opus-4-8")
        self.assertEqual(request.vibe_agent_reasoning_effort, "max")

    async def test_avibe_backend_variant_does_not_namespace_normal_claude_session(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)

        def _resolve_claude_agent(context, override_agent_name=None, required=False):
            return type(
                "VibeAgent",
                (),
                {
                    "id": "agent-claude",
                    "name": "claude",
                    "backend": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "system_prompt": None,
                },
            )()

        controller.resolve_vibe_agent_for_context = _resolve_claude_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="workbench",
            channel_id="ses-1",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_run_target": {
                    "agent_backend": "claude",
                    "agent_variant": "claude",
                    "workdir": "/repo/work",
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertIsNone(request.subagent_name)
        self.assertEqual(request.base_session_id, "base-session")

    async def test_avibe_agent_name_variant_does_not_namespace_normal_claude_session(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)

        def _resolve_claude_agent(context, override_agent_name=None, required=False):
            return type(
                "VibeAgent",
                (),
                {
                    "id": "agent-claude-contract",
                    "name": "contract-bot",
                    "backend": "claude",
                    "model": None,
                    "reasoning_effort": None,
                    "system_prompt": None,
                },
            )()

        controller.resolve_vibe_agent_for_context = _resolve_claude_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="workbench",
            channel_id="ses-1",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_run_target": {
                    "agent_name": "contract-bot",
                    "agent_backend": "claude",
                    "agent_variant": "contract-bot",
                    "workdir": "/repo/work",
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertIsNone(request.subagent_name)
        self.assertEqual(request.base_session_id, "base-session")

    async def test_reply_anchor_alias_keeps_original_anchor_mapping(self):
        controller = _StubController(platform="discord", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        session_handler = _StubSessionHandler()
        handler.set_session_handler(session_handler)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            thread_id="thread-1",
            message_id="m1",
            platform="discord",
            platform_specific={"reply_anchor_base_session_id": "discord_anchor-1"},
        )

        await handler.handle_user_message(context, "hello")

        self.assertEqual(
            session_handler.alias_calls,
            [
                {
                    "source_base_session_id": "discord_anchor-1",
                    "alias_base_session_id": "base-session",
                    "clear_source": False,
                }
            ],
        )

    async def test_wechat_context_forces_typing_even_when_primary_platform_is_slack(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="wx-user",
            channel_id="wx-chat",
            message_id="m1",
            platform="wechat",
            platform_specific={"platform": "wechat"},
        )

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertTrue(request.typing_indicator_active)
        self.assertEqual(controller.im_client.reactions, [])
        self.assertGreaterEqual(len(controller.im_client.typing_calls), 1)

    async def test_wechat_typing_is_cleared_when_agent_processing_raises(self):
        controller = _StubController(platform="wechat", ack_mode="message", typing_result=True)
        captured_requests = []

        async def _raise_after_request(agent_name, request):
            captured_requests.append(request)
            raise RuntimeError("agent failed")

        controller.agent_service.handle_message = _raise_after_request
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="wx-user", channel_id="wx-chat", message_id="m1")

        await handler.handle_user_message(context, "hello")

        self.assertEqual(controller.im_client.clear_calls, [("wx-chat", "wx-user")])
        self.assertFalse(captured_requests[0].typing_indicator_active)
        self.assertIsNone(captured_requests[0].typing_indicator_task)
        self.assertEqual(controller.im_client.sent_messages, [("wx-chat", "Error: agent failed")])

    async def test_telegram_reaction_mode_matches_global_ack_strategy(self):
        controller = _StubController(platform="telegram", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="tg-user", channel_id="tg-chat", message_id="m1", platform="telegram")

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertFalse(request.typing_indicator_active)
        self.assertTrue(request.processing_indicator.reaction_indicator_selected)
        self.assertIsNone(request.ack_reaction_message_id)
        self.assertEqual(controller.im_client.reactions, [])
        await controller.processing_indicator.promote_reaction_to_running(request)
        self.assertEqual(request.ack_reaction_message_id, "m1")
        self.assertEqual(request.ack_reaction_emoji, "👀")
        self.assertEqual(controller.im_client.reactions, [("tg-chat", "m1", "👀")])

    async def test_existing_session_backend_wins_when_no_vibe_agent_attached(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "agent_session_target": {
                    "session_id": "ses_legacy",
                    "agent_name": None,
                    "agent_backend": "codex",
                }
            },
        )

        await handler.handle_user_message(context, "continue")

        agent_name, _ = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "codex")

    async def test_existing_im_thread_resolves_agent_by_stable_id(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.settings_manager.sessions.find_session_for_anchor = lambda *_args: {
            "id": "ses_preserved",
            "agent_id": "agent-original",
            "agent_name": "pm",
            "agent_backend": "claude",
            "visibility": "foreground",
        }
        observed = {}

        def _resolve_agent(
            _context,
            *,
            override_agent_id=None,
            override_agent_name=None,
            required=False,
        ):
            observed.update(
                agent_id=override_agent_id,
                agent_name=override_agent_name,
                required=required,
            )
            return type(
                "VibeAgent",
                (),
                {
                    "id": "agent-original",
                    "name": "_pm-ab12",
                    "backend": "claude",
                    "model": "claude-opus",
                    "reasoning_effort": None,
                    "system_prompt": None,
                },
            )()

        controller.resolve_vibe_agent_for_context = _resolve_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
        )

        await handler.handle_user_message(context, "continue")

        self.assertEqual(
            observed,
            {
                "agent_id": "agent-original",
                "agent_name": "pm",
                "required": True,
            },
        )
        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertEqual(request.vibe_agent_id, "agent-original")
        self.assertEqual(request.vibe_agent_name, "_pm-ab12")

    async def test_unusable_durable_session_agent_does_not_fallback_to_backend(self):
        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        observed = {}

        def _reject_archived_agent(
            _context,
            *,
            override_agent_id=None,
            override_agent_name=None,
            required=False,
        ):
            observed.update(
                agent_id=override_agent_id,
                agent_name=override_agent_name,
                required=required,
            )
            raise ValueError("archived Agent is disabled")

        controller.resolve_vibe_agent_for_context = _reject_archived_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="workbench",
            channel_id="ses_archived",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_archived",
                    "agent_id": "agent-archived",
                    "agent_name": "_worker-ab12",
                    "agent_backend": "codex",
                }
            },
        )

        await handler.handle_user_message(context, "continue")

        self.assertEqual(
            observed,
            {
                "agent_id": "agent-archived",
                "agent_name": "_worker-ab12",
                "required": True,
            },
        )
        self.assertEqual(controller.agent_service.requests, [])
        self.assertTrue(
            any("archived Agent is disabled" in text for _, text in controller.im_client.sent_messages)
        )

    async def test_unusable_persisted_scope_agent_does_not_fallback_to_backend(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.settings_manager.routing = type(
            "Routing",
            (),
            {"agent_name": "_worker-ab12"},
        )()
        observed = {}

        def _reject_archived_agent(
            _context,
            *,
            override_agent_id=None,
            override_agent_name=None,
            required=False,
        ):
            observed.update(
                agent_id=override_agent_id,
                agent_name=override_agent_name,
                required=required,
            )
            raise ValueError("archived scope Agent is disabled")

        controller.resolve_vibe_agent_for_context = _reject_archived_agent
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={"agent_run_target": {"agent_backend": "codex"}},
        )

        await handler.handle_user_message(context, "start a new thread")

        self.assertEqual(
            observed,
            {
                "agent_id": None,
                "agent_name": None,
                "required": True,
            },
        )
        self.assertEqual(controller.agent_service.requests, [])
        self.assertTrue(
            any("archived scope Agent is disabled" in text for _, text in controller.im_client.sent_messages)
        )

    async def test_existing_session_backend_does_not_attach_default_vibe_agent_metadata(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "agent_session_target": {
                    "session_id": "ses_legacy",
                    "agent_name": None,
                    "agent_backend": "codex",
                }
            },
        )

        await handler.handle_user_message(context, "continue")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "codex")
        self.assertIsNone(request.vibe_agent_name)
        self.assertIsNone(request.vibe_agent_model)
        self.assertIsNone(request.vibe_agent_system_prompt)

    async def test_existing_session_backend_ignores_scope_model_override(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.settings_manager.routing = type(
            "Routing",
            (),
            {
                "agent_name": None,
                "model": "claude-opus-4-8",
                "reasoning_effort": "max",
            },
        )()
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "agent_session_target": {
                    "session_id": "ses_codex",
                    "agent_name": None,
                    "agent_backend": "codex",
                }
            },
        )

        await handler.handle_user_message(context, "continue")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "codex")
        self.assertIsNone(request.vibe_agent_model)
        self.assertIsNone(request.vibe_agent_reasoning_effort)

    async def test_workbench_inherited_route_materializes_at_turn_start(self):
        """HFR-462: a workbench session created on an inherited default (empty model /
        effort) gets the resolved Agent defaults pinned onto its row when the
        turn STARTS, so the chat-header picker keeps showing the full route."""
        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        sessions_store = Mock()
        sessions_store.is_message_in_processed_set.return_value = False
        sessions_store.try_add_to_processed_set.return_value = True
        sessions_store.materialize_agent_session_route = Mock(return_value=True)
        controller.settings_manager.sessions = SessionsFacade(sessions_store)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="ses_wb",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_wb",
                    "agent_id": None,
                    "agent_name": None,
                    "agent_backend": None,
                    "agent_variant": None,
                    "model": None,
                    "reasoning_effort": None,
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        sessions_store.materialize_agent_session_route.assert_called_once_with(
            "ses_wb",
            agent_id="agent-default",
            agent_name="default",
            model="gpt-5.4",
            reasoning_effort="high",
            expected_route={
                "agent_id": None,
                "agent_name": None,
                "agent_backend": None,
                "agent_variant": None,
                "model": None,
                "reasoning_effort": None,
                "explicit_overrides": [],
            },
        )

    async def test_workbench_explicit_route_skips_materialization(self):
        """A session whose row already carries an explicit model / effort must
        not be touched at turn start — materialization is fill-if-empty only."""
        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        controller.settings_manager.sessions.materialize_agent_session_route = Mock(return_value=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="ses_wb",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_wb",
                    "agent_name": "codex",
                    "agent_backend": "codex",
                    "model": "gpt-5.5",
                    "reasoning_effort": "xhigh",
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        controller.settings_manager.sessions.materialize_agent_session_route.assert_not_called()

    async def test_workbench_scalar_explicit_route_marker_uses_shared_parser(self):
        """A tolerated legacy scalar marker must mean the same thing to the
        dispatch snapshot and the storage CAS: model stays explicitly null while
        the unpinned effort can still materialize."""
        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        sessions_store = Mock()
        sessions_store.is_message_in_processed_set.return_value = False
        sessions_store.try_add_to_processed_set.return_value = True
        sessions_store.materialize_agent_session_route = Mock(return_value=True)
        controller.settings_manager.sessions = SessionsFacade(sessions_store)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="ses_wb",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_wb",
                    "agent_id": None,
                    "agent_name": None,
                    "agent_backend": None,
                    "agent_variant": None,
                    "model": None,
                    "reasoning_effort": None,
                    "metadata": {"explicit_setting_overrides": "model"},
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        _agent_name, request = controller.agent_service.requests[0]
        self.assertTrue(request.vibe_agent_model_explicit)
        self.assertFalse(request.vibe_agent_reasoning_effort_explicit)
        sessions_store.materialize_agent_session_route.assert_called_once_with(
            "ses_wb",
            agent_id="agent-default",
            agent_name="default",
            model=None,
            reasoning_effort="high",
            expected_route={
                "agent_id": None,
                "agent_name": None,
                "agent_backend": None,
                "agent_variant": None,
                "model": None,
                "reasoning_effort": None,
                "explicit_overrides": ["model"],
            },
        )

    async def test_im_turn_never_materializes_session_route(self):
        """Scheduled IM turns can carry ``agent_session_target``; their model
        semantics still belong to channel routing and must not be pinned."""
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.settings_manager.sessions.materialize_agent_session_route = Mock(return_value=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_im",
                    "agent_name": "codex",
                    "agent_backend": "codex",
                    "model": None,
                    "reasoning_effort": None,
                }
            },
        )

        await handler.handle_user_message(context, "hello")

        self.assertEqual(len(controller.agent_service.requests), 1)
        controller.settings_manager.sessions.materialize_agent_session_route.assert_not_called()

    async def test_background_im_session_suppresses_outward_delivery(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "agent_run_target": {
                    "agent_session_id": "ses_background",
                    "agent_backend": "codex",
                    "visibility": "background",
                }
            },
        )

        await handler.handle_user_message(context, "continue")

        self.assertEqual(len(controller.agent_service.requests), 1)
        agent_name, request = controller.agent_service.requests[0]
        self.assertTrue(request.context.platform_specific["suppress_delivery"])
        self.assertEqual(agent_name, "codex")

    async def test_lark_typing_preference_uses_registry_reaction_capability(self):
        controller = _StubController(platform="lark", ack_mode="typing", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="lark-user", channel_id="lark-chat", message_id="om_1", platform="lark")

        await handler.handle_user_message(context, "hello")

        _, request = controller.agent_service.requests[0]
        self.assertFalse(request.typing_indicator_active)
        self.assertEqual(controller.im_client.typing_calls, [])
        self.assertTrue(request.processing_indicator.reaction_indicator_selected)
        self.assertIsNone(request.ack_reaction_message_id)
        self.assertEqual(controller.im_client.reactions, [])
        await controller.processing_indicator.promote_reaction_to_running(request)
        self.assertEqual(request.ack_reaction_message_id, "om_1")
        self.assertEqual(controller.im_client.reactions, [("lark-chat", "om_1", "👀")])

    async def test_platform_specific_client_is_used_for_user_info(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat", platform="wechat")

        class _WechatClient(_StubIMClient):
            async def get_user_info(self, user_id):
                return {"display_name": "WeChat User"}

        wechat_client = _WechatClient(typing_result=True)
        controller.get_im_client_for_context = lambda _context: wechat_client  # type: ignore[method-assign]

        result = await handler._prepend_user_info(context, "hello")

        self.assertEqual(result, "[WeChat User<wx-user>]\nhello")

    async def test_control_text_handles_mentioned_inline_stop(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            thread_id="T1",
            message_id="m1",
            platform="slack",
            platform_specific={"control_text": "stop"},
        )

        await handler.handle_user_message(context, "<@U_BOT> stop")

        self.assertEqual(len(controller.agent_service.stop_requests), 1)
        self.assertEqual(controller.agent_service.requests, [])

    async def test_control_text_handles_inline_stop_in_telegram_general_topic(self):
        controller = _StubController(platform="telegram", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="-1001",
            thread_id=None,
            message_id="m1",
            platform="telegram",
            platform_specific={
                "control_text": "stop",
                "is_dm": False,
                "is_forum": True,
                "is_topic_message": True,
            },
        )

        await handler.handle_user_message(context, "stop")

        self.assertEqual(len(controller.agent_service.stop_requests), 1)
        self.assertEqual(controller.agent_service.requests, [])

    async def test_target_routing_subagent_preserves_backend_runtime_key_for_stop(self):
        from core.handlers.command_handlers import CommandHandlers

        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        controller.command_handler = CommandHandlers(controller)
        handler = MessageHandler(controller)
        session_handler = _StubSessionHandler()
        controller.session_handler = session_handler
        handler.set_session_handler(session_handler)
        context = MessageContext(
            user_id="workbench",
            channel_id="ses_main",
            message_id="m1",
            platform="avibe",
            platform_specific={
                "agent_run_target": {
                    "platform": "avibe",
                    "settings_key": "ses_main",
                    "session_key": "avibe::ses_main",
                    "session_anchor": "ses_main",
                    "workdir": "/tmp",
                    "source": "human",
                    "scope_id": "avibe::project::proj",
                    "scope_type": "project",
                    "agent_session_id": "ses_main",
                    "agent_name": "reviewer-agent",
                    "agent_backend": "claude",
                    "agent_variant": "reviewer",
                },
                "agent_session_target": {
                    "id": "ses_main",
                    "session_anchor": "ses_main",
                    "agent_name": "reviewer-agent",
                    "agent_backend": "claude",
                    "agent_variant": "reviewer",
                },
            },
        )

        await handler.handle_user_message(context, "hello")

        agent_name, request = controller.agent_service.requests[0]
        self.assertEqual(agent_name, "claude")
        self.assertEqual(request.base_session_id, "base-session:reviewer")
        self.assertEqual(context.platform_specific["backend_base_session_id"], "base-session:reviewer")

        await controller.command_handler.handle_stop(context)

        stop_agent, stop_request = controller.agent_service.stop_requests[0]
        self.assertEqual(stop_agent, "claude")
        self.assertEqual(stop_request.base_session_id, "base-session:reviewer")
        self.assertEqual(stop_request.composite_session_id, "base-session:reviewer:/tmp")

    async def test_workbench_stop_suppresses_no_active_notice(self):
        from core.handlers.command_handlers import CommandHandlers

        controller = _StubController(platform="avibe", ack_mode="reaction", typing_result=True)
        controller.agent_service.stop_result = False
        controller.command_handler = CommandHandlers(controller)
        controller.session_handler = _StubSessionHandler()
        context = MessageContext(
            user_id="workbench",
            channel_id="ses_main",
            platform="avibe",
            platform_specific={"suppress_stop_no_active_notice": True},
        )

        handled = await controller.command_handler.handle_stop(context)

        self.assertFalse(handled)
        self.assertEqual(controller.im_client.sent_messages, [])

    async def test_empty_fallback_uses_agent_message_not_control_text(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.command_handler.handle_start = AsyncMock()
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "bot_mention": "<@U_BOT>",
                "control_text": "",
                "normalized_user_text": "shared content",
            },
        )

        await handler.handle_user_message(context, "<@U_BOT>\n\nshared content")

        controller.command_handler.handle_start.assert_not_awaited()
        _, request = controller.agent_service.requests[0]
        self.assertIn("shared content", request.message)
        self.assertEqual(request.user_message, "shared content")

    async def test_request_separates_user_message_from_prompt_decorations(self):
        from core.audio_asr import AudioTranscript

        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.config.include_time_info = True
        controller.config.include_user_info = True
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        handler._build_current_time_line = lambda: "[Current Time: 2026-07-26 16:00:00 UTC+08:00]"
        attachment_lease = Mock()
        handler._materialize_file_attachments = AsyncMock(
            return_value=types.SimpleNamespace(
                attachments=(object(),),
                display_errors=("report.pdf could not be downloaded",),
                lease=attachment_lease,
            )
        )
        handler._transcribe_audio_attachments = AsyncMock(
            return_value=[
                AudioTranscript(
                    attachment_name="note.m4a",
                    local_path="/tmp/note.m4a",
                    text="Keep the user's words",
                )
            ]
        )
        handler._echo_audio_transcripts_if_enabled = AsyncMock()
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={
                "bot_mention": "<@U_BOT>",
                "control_text": "Investigate session title fallback",
                "normalized_user_text": "Investigate session title fallback",
            },
            files=[object()],
        )

        await handler.handle_user_message(context, "<@U_BOT> Investigate session title fallback")

        _, request = controller.agent_service.requests[0]
        prepended_lines = request.message.splitlines()[:2]
        self.assertIn("<@U_BOT>", request.message)
        self.assertNotIn("<@U_BOT>", request.user_message)
        self.assertIn("[Audio Transcripts]", request.user_message)
        self.assertIn("Keep the user's words", request.user_message)
        self.assertFalse(set(prepended_lines) & set(request.user_message.splitlines()))
        self.assertNotIn("[Attachment Download Errors]", request.user_message)
        self.assertIn("[Attachment Download Errors]", request.message)

    async def test_control_text_routes_mentioned_subagent_prefix(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="m1",
            platform="slack",
            platform_specific={"bot_mention": "<@U_BOT>", "control_text": "reviewer: check this"},
        )

        from modules.agents.subagent_router import SubagentDefinition

        with patch(
            "modules.agents.subagent_router.load_codex_subagent",
            return_value=SubagentDefinition(name="reviewer"),
        ):
            await handler.handle_user_message(context, "<@U_BOT> reviewer: check this")

        _, request = controller.agent_service.requests[0]
        self.assertEqual(request.subagent_name, "reviewer")
        self.assertEqual(request.subagent_key, "reviewer")
        self.assertEqual(request.message, "check this")
        # 🤖 is added eagerly in the handler; the ack reaction is deferred to the
        # gate, so only 🤖 is present until the turn starts and promotes to 👀.
        self.assertEqual(controller.im_client.reactions, [("C1", "m1", "🤖")])
        await controller.processing_indicator.promote_reaction_to_running(request)
        self.assertEqual(controller.im_client.reactions, [("C1", "m1", "🤖"), ("C1", "m1", "👀")])

    async def test_scheduled_turn_returns_error_string_after_notifying_im(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        controller.agent_service.error = RuntimeError("boom")

        async def emit(context, message_type, text, **kwargs):
            if message_type == "notify":
                delivered_id = await controller.im_client.send_message(context, text)
                delivery = kwargs["delivery"]
                delivery.send_returned = True
                delivery.delivered_id = delivered_id
                return delivered_id
            return None

        controller.emit_agent_message = AsyncMock(side_effect=emit)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="scheduled",
            channel_id="C1",
            message_id="scheduled:task-1:abc",
            platform="slack",
            platform_specific={
                "turn_token": "turn-scheduled-error",
                "task_execution_id": "run-scheduled-error",
            },
        )

        result = await handler.handle_scheduled_message(context, "hello")

        self.assertEqual(result, "boom")
        self.assertEqual(controller.im_client.sent_messages, [("C1", "Error: boom")])
        self.assertEqual(controller.emit_agent_message.await_count, 2)
        notify_call, _terminal_call = controller.emit_agent_message.await_args_list
        self.assertEqual(notify_call.args[:3], (context, "notify", "Error: boom"))
        terminal_output = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(
            terminal_output.metadata["turn_failure_notification"],
            {
                "failure_id": "turn:turn-scheduled-error",
                "ack_evidence": "delivery_only",
                "delivered": True,
            },
        )

    async def test_handled_backend_key_error_is_not_reclassified_as_missing_agent(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)

        async def fail_after_dispatch(_agent_name, request):
            error = KeyError("backend config")
            request.failure_handled = True
            await request.failure_handler(error)
            raise error

        controller.agent_service.handle_message = fail_after_dispatch

        async def emit(context, message_type, text, **kwargs):
            if message_type == "notify":
                delivered_id = await controller.im_client.send_message(context, text)
                delivery = kwargs["delivery"]
                delivery.send_returned = True
                delivery.delivered_id = delivered_id
                return delivered_id
            return None

        controller.emit_agent_message = AsyncMock(side_effect=emit)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="scheduled",
            channel_id="C1",
            message_id="scheduled:task-1:key-error",
            platform="slack",
            platform_specific={
                "turn_token": "turn-scheduled-key-error",
                "task_execution_id": "run-scheduled-key-error",
            },
        )

        result = await handler.handle_scheduled_message(context, "hello")

        self.assertEqual(result, "'backend config'")
        self.assertEqual(controller.im_client.sent_messages, [("C1", "Error: 'backend config'")])
        self.assertEqual(controller.emit_agent_message.await_count, 2)
        self.assertNotIn("not available", controller.im_client.sent_messages[0][1])

    async def test_durable_scheduled_turn_does_not_mirror_before_acceptance(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="scheduled",
            channel_id="C1",
            message_id="watch:native-1",
            platform="slack",
            platform_specific={
                "delivery_id": "msg_delivery_1",
                "delivery_ids": ["msg_delivery_1"],
            },
        )

        with patch("core.message_mirror.mirror_harness_inbound") as mirror:
            await handler.handle_scheduled_message(context, "hello")

        mirror.assert_not_called()
        self.assertEqual(len(controller.agent_service.requests), 1)

    async def test_resume_session_callback_preserves_platform(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        setattr(
            controller,
            "session_handler",
            type("SessionHandler", (), {"handle_resume_session_submission": AsyncMock()})(),
        )
        handler = MessageHandler(controller)
        context = MessageContext(
            user_id="u1",
            channel_id="c1",
            thread_id="t1",
            platform="lark",
            platform_specific={"platform": "lark", "is_dm": False},
        )

        await handler.handle_callback_query(context, "resume_session:opencode:session-1")

        getattr(controller, "session_handler").handle_resume_session_submission.assert_awaited_once_with(
            user_id="u1",
            channel_id="c1",
            thread_id="t1",
            agent="opencode",
            session_id="session-1",
            is_dm=False,
            platform="lark",
        )

    async def test_legacy_opencode_question_callback_is_ignored(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(user_id="u1", channel_id="c1", platform="slack")

        await handler.handle_callback_query(context, "opencode_question:choose:1")

        self.assertEqual(controller.agent_service.requests, [])

    async def test_quick_reply_callback_preserves_platform(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.handle_user_message = AsyncMock()  # type: ignore[method-assign]
        context = MessageContext(
            user_id="u1",
            channel_id="chat1",
            message_id="om_123",
            platform="lark",
            platform_specific={"platform": "lark", "is_dm": False},
        )

        await handler.handle_callback_query(context, "quick_reply:继续")

        self.assertEqual(controller.im_client.removed_keyboards, [("chat1", "lark", "om_123")])
        self.assertEqual(controller.im_client.sent_messages, [("chat1", "Reply: 继续")])
        self.assertEqual(controller.im_client.reactions, [])
        handler.handle_user_message.assert_awaited_once()
        forwarded_context, forwarded_text = handler.handle_user_message.await_args.args
        self.assertEqual(forwarded_text, "继续")
        self.assertEqual(forwarded_context.platform, "lark")
        self.assertIsNone(forwarded_context.message_id)
        self.assertEqual((forwarded_context.platform_specific or {}).get("processing_indicator_message_id"), "msg-1")

    async def test_quick_reply_callback_reaction_uses_echo_as_indicator_target(self):
        controller = _StubController(platform="slack", ack_mode="reaction", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="u1",
            channel_id="chat1",
            message_id="prompt-msg",
            platform="slack",
            platform_specific={"platform": "slack", "is_dm": False},
        )

        await handler.handle_callback_query(context, "quick_reply:按钮 1")

        self.assertEqual(controller.im_client.removed_keyboards, [("chat1", "slack", "prompt-msg")])
        self.assertEqual(controller.im_client.sent_messages, [("chat1", "Reply: 按钮 1")])
        # Reaction deferred to the gate; the echo message id is still the target.
        self.assertEqual(controller.im_client.reactions, [])
        _, request = controller.agent_service.requests[0]
        self.assertIsNone(request.context.message_id)
        self.assertTrue(request.processing_indicator.reaction_indicator_selected)
        self.assertIsNone(request.ack_reaction_message_id)
        self.assertIsNone(request.ack_message_id)
        self.assertFalse(request.typing_indicator_active)
        await controller.processing_indicator.promote_reaction_to_running(request)
        self.assertEqual(controller.im_client.reactions, [("chat1", "msg-1", "👀")])
        self.assertEqual(request.ack_reaction_message_id, "msg-1")

    async def test_quick_reply_callback_typing_uses_global_indicator_strategy(self):
        controller = _StubController(platform="telegram", ack_mode="typing", typing_result=True)
        handler = MessageHandler(controller)
        handler.set_session_handler(_StubSessionHandler())
        context = MessageContext(
            user_id="u1",
            channel_id="chat1",
            message_id="prompt-msg",
            platform="telegram",
            platform_specific={"platform": "telegram", "is_dm": False},
        )

        await handler.handle_callback_query(context, "quick_reply:按钮 2")

        self.assertEqual(controller.im_client.sent_messages, [("chat1", "Reply: 按钮 2")])
        self.assertEqual(controller.im_client.reactions, [])
        self.assertEqual(controller.im_client.typing_calls, [("chat1", "u1")])
        _, request = controller.agent_service.requests[0]
        self.assertTrue(request.typing_indicator_active)
        self.assertIsNone(request.ack_message_id)
        self.assertIsNone(request.ack_reaction_message_id)
        await handler._remove_ack_reaction(context, request)


if __name__ == "__main__":
    unittest.main()
