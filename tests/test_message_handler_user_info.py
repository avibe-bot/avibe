import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.im import MessageContext
from core.agent_input import AgentInputMetadata


def _load_message_handler_class():
    with patch.dict(sys.modules, {}, clear=False):
        agents_module = types.ModuleType("modules.agents")
        agents_module.__path__ = []
        agent_request = type("AgentRequest", (), {})
        setattr(agents_module, "AgentRequest", agent_request)
        sys.modules["modules.agents"] = agents_module
        agents_base_module = types.ModuleType("modules.agents.base")
        setattr(agents_base_module, "AgentRequest", agent_request)
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


class _StubIMClient:
    def __init__(self, user_info):
        self.user_info = user_info
        self.formatter = None

    async def get_user_info(self, user_id):
        return self.user_info


class _StubController:
    def __init__(self, user_info):
        self.config = type("Config", (), {"platform": "slack"})()
        self.im_client = _StubIMClient(user_info)
        self.settings_manager = type("Settings", (), {})()
        self.session_manager = object()
        self.receiver_tasks = {}


class MessageHandlerUserInfoTests(unittest.IsolatedAsyncioTestCase):
    def test_build_current_time_line_uses_readable_utc_offset(self):
        line = AgentInputMetadata().render(
            "hello", types.SimpleNamespace(include_time_info=True),
            now=datetime(2026, 5, 13, 11, 42, 8, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(line, "[Now: 2026-05-13 11:42:08 UTC+08:00]\nhello")

    async def test_prepend_user_info_prefers_real_name_over_name(self):
        handler = MessageHandler(_StubController({"display_name": "", "real_name": "Alex", "name": "cyh"}))
        context = MessageContext(user_id="U0E0FM3QT", channel_id="C1")

        metadata = await handler.prepare_input_metadata(context, human=True)
        result = metadata.render("hello", types.SimpleNamespace(include_time_info=False))

        self.assertEqual(result, "[Alex<U0E0FM3QT>]\nhello")

    async def test_prepend_user_info_uses_display_name_when_present(self):
        handler = MessageHandler(_StubController({"display_name": "Alex Chen", "real_name": "Alex", "name": "cyh"}))
        context = MessageContext(user_id="U0E0FM3QT", channel_id="C1")

        metadata = await handler.prepare_input_metadata(context, human=True)
        result = metadata.render("hello", types.SimpleNamespace(include_time_info=False))

        self.assertEqual(result, "[Alex Chen<U0E0FM3QT>]\nhello")

    async def test_prepend_message_metadata_includes_time_above_user_info(self):
        handler = MessageHandler(_StubController({"display_name": "", "real_name": "Alex", "name": "cyh"}))
        handler.config.include_time_info = True
        handler.config.include_user_info = True
        context = MessageContext(user_id="U0E0FM3QT", channel_id="C1")

        metadata = await handler.prepare_input_metadata(context, human=True)
        result = metadata.render("hello", handler.config, now=datetime(2026, 5, 13, 11, 42, 8, tzinfo=timezone(timedelta(hours=8))))

        self.assertEqual(result, "[Now: 2026-05-13 11:42:08 UTC+08:00]\n[Alex<U0E0FM3QT>]\nhello")

    async def test_prepend_message_metadata_can_include_time_without_user_info(self):
        handler = MessageHandler(_StubController({"display_name": "", "real_name": "Alex", "name": "cyh"}))
        handler.config.include_time_info = True
        handler.config.include_user_info = True
        context = MessageContext(user_id="U0E0FM3QT", channel_id="C1")

        metadata = await handler.prepare_input_metadata(context, human=False)
        result = metadata.render("scheduled work", handler.config, now=datetime(2026, 5, 13, 11, 42, 8, tzinfo=timezone(timedelta(hours=8))))

        self.assertEqual(result, "[Now: 2026-05-13 11:42:08 UTC+08:00]\nscheduled work")

    async def test_prepend_message_metadata_places_source_session_after_time(self):
        handler = MessageHandler(_StubController({"display_name": "", "real_name": "Alex", "name": "cyh"}))
        handler.config.include_time_info = True
        context = MessageContext(
            user_id="caller",
            channel_id="C1",
            platform_specific={
                "source_kind": "callback",
                "source_session_id": "ses_sender123",
            },
        )

        metadata = await handler.prepare_input_metadata(context, human=False)
        result = metadata.render("callback result", handler.config, now=datetime(2026, 5, 13, 11, 42, 8, tzinfo=timezone(timedelta(hours=8))))

        self.assertEqual(
            result,
            "[Now: 2026-05-13 11:42:08 UTC+08:00]\n"
            "From: #ses_sender123\ncallback result",
        )

    async def test_prepend_message_metadata_uses_agent_source_actor_as_session(self):
        handler = MessageHandler(_StubController({"display_name": "", "real_name": "Alex", "name": "cyh"}))
        handler.config.include_time_info = False
        context = MessageContext(
            user_id="caller",
            channel_id="C1",
            platform_specific={
                "source_kind": "agent",
                "source_actor": "ses_sender456",
            },
        )

        metadata = await handler.prepare_input_metadata(context, human=False)
        result = metadata.render("delegated work", handler.config)

        self.assertEqual(result, "From: #ses_sender456\ndelegated work")


if __name__ == "__main__":
    unittest.main()
