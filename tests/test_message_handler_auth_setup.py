import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.im import MessageContext


def _load_message_handler_class():
    originals = {}

    def _set_module(name, module):
        originals.setdefault(name, sys.modules.get(name))
        sys.modules[name] = module

    agents_module = types.ModuleType("modules.agents")
    agents_module.__path__ = [str(ROOT / "modules" / "agents")]
    setattr(agents_module, "AgentRequest", type("AgentRequest", (), {}))
    _set_module("modules.agents", agents_module)

    agents_base_module = types.ModuleType("modules.agents.base")
    setattr(agents_base_module, "AgentRequest", type("AgentRequest", (), {}))
    _set_module("modules.agents.base", agents_base_module)

    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str(ROOT / "core")]
    _set_module("core", core_pkg)

    handlers_pkg = types.ModuleType("core.handlers")
    handlers_pkg.__path__ = [str(ROOT / "core" / "handlers")]
    _set_module("core.handlers", handlers_pkg)

    try:
        for module_name, relative_path in (
            ("core.handlers.base", ROOT / "core" / "handlers" / "base.py"),
            ("core.handlers.message_handler", ROOT / "core" / "handlers" / "message_handler.py"),
        ):
            originals.setdefault(module_name, sys.modules.get(module_name))
            spec = importlib.util.spec_from_file_location(module_name, relative_path)
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

        return sys.modules["core.handlers.message_handler"].MessageHandler
    finally:
        for module_name, original in reversed(list(originals.items())):
            if original is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = original


MessageHandler = _load_message_handler_class()


class _StubSessions:
    def __init__(self):
        self.recorded = []
        self._claimed = set()

    def is_message_already_processed(self, channel_id, thread_ts, message_ts):
        return False

    def record_processed_message(self, channel_id, thread_ts, message_ts):
        self.recorded.append((channel_id, thread_ts, message_ts))
        return None

    def try_record_processed_message(self, channel_id, thread_ts, message_ts):
        key = (channel_id, thread_ts, message_ts)
        if key in self._claimed:
            return False
        self._claimed.add(key)
        self.recorded.append(key)
        return True


class _StubSettingsManager:
    def __init__(self):
        self.sessions = _StubSessions()

    def get_channel_routing(self, settings_key):
        return None

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class _StubIMClient:
    def __init__(self):
        self.sent_messages = []
        self.formatter = types.SimpleNamespace(format_error=lambda text: text, format_warning=lambda text: text)

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append((context.channel_id, text))
        return "msg-1"


class _StubController:
    def __init__(self):
        self.config = types.SimpleNamespace(
            platform="slack",
            ack_mode="reaction",
            include_time_info=False,
            include_user_info=False,
            language="en",
        )
        self.im_client = _StubIMClient()
        self.settings_manager = _StubSettingsManager()
        self.session_manager = object()
        self.session_handler = None
        self.receiver_tasks = {}
        self.agent_service = types.SimpleNamespace(default_agent="codex")
        self.settings_handler = types.SimpleNamespace()
        self.command_handler = types.SimpleNamespace()
        self.agent_auth_service = types.SimpleNamespace(handle_setup_callback=AsyncMock())

    def get_im_client_for_context(self, context):
        return self.im_client

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"slack::{context.channel_id}"

    def _get_lang(self):
        return "en"


class MessageHandlerAuthSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_setup_callback_routes_to_agent_auth_service(self):
        controller = _StubController()
        controller.settings_handler.handle_info_message_types = AsyncMock()
        controller.settings_handler.handle_info_how_it_works = AsyncMock()
        controller.command_handler.handle_cwd = AsyncMock()
        controller.command_handler.handle_change_cwd_modal = AsyncMock()
        controller.command_handler.handle_new = AsyncMock()
        controller.command_handler.handle_resume = AsyncMock()
        controller.settings_handler.handle_settings = AsyncMock()
        controller.settings_handler.handle_routing = AsyncMock()

        handler = MessageHandler(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack")

        await handler.handle_callback_query(context, "auth_setup:auto")

        controller.agent_auth_service.handle_setup_callback.assert_awaited_once_with(context, "auth_setup:auto")

    async def test_plain_claude_callback_value_is_consumed_before_agent_routing(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_consume_setup_reply = AsyncMock(return_value=True)

        handler = MessageHandler(controller)
        context = MessageContext(user_id="U1", channel_id="C1", platform="slack", message_id="m1")

        result = await handler.handle_user_message(context, "auth-code#oauth-state")

        self.assertIsNone(result)
        controller.agent_auth_service.maybe_consume_setup_reply.assert_awaited_once_with(
            context,
            "auth-code#oauth-state",
        )
        self.assertEqual(controller.settings_manager.sessions.recorded, [("im:slack:C1", "im:slack:m1", "im:slack:m1")])

    async def test_platform_qualified_dedup_keeps_colliding_native_ids_distinct(self):
        controller = _StubController()
        controller.agent_auth_service.maybe_consume_setup_reply = AsyncMock(return_value=True)
        handler = MessageHandler(controller)

        slack = MessageContext(user_id="U1", channel_id="C1", platform="slack", message_id="m1")
        duplicate_slack = MessageContext(user_id="U1", channel_id="C1", platform="slack", message_id="m1")
        discord = MessageContext(user_id="U1", channel_id="C1", platform="discord", message_id="m1")

        await handler.handle_user_message(slack, "first")
        await handler.handle_user_message(duplicate_slack, "duplicate")
        await handler.handle_user_message(discord, "second platform")

        self.assertEqual(
            controller.settings_manager.sessions.recorded,
            [
                ("im:slack:C1", "im:slack:m1", "im:slack:m1"),
                ("im:discord:C1", "im:discord:m1", "im:discord:m1"),
            ],
        )
        self.assertEqual(controller.agent_auth_service.maybe_consume_setup_reply.await_count, 2)


if __name__ == "__main__":
    unittest.main()
