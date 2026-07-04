import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.im import MessageContext


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_command_handlers_class():
    with patch.dict(sys.modules, {}, clear=False):
        agents_module = types.ModuleType("modules.agents")
        agents_module.__path__ = [str(ROOT / "modules" / "agents")]
        setattr(agents_module, "AgentRequest", type("AgentRequest", (), {}))
        setattr(
            agents_module,
            "get_agent_display_name",
            lambda agent_name, fallback=None: agent_name or fallback or "Unknown",
        )
        sys.modules["modules.agents"] = agents_module
        agents_base_module = types.ModuleType("modules.agents.base")
        setattr(agents_base_module, "AgentRequest", type("AgentRequest", (), {}))
        sys.modules["modules.agents.base"] = agents_base_module

        core_pkg = types.ModuleType("core")
        core_pkg.__path__ = [str(ROOT / "core")]
        sys.modules["core"] = core_pkg

        handlers_pkg = types.ModuleType("core.handlers")
        handlers_pkg.__path__ = [str(ROOT / "core" / "handlers")]
        sys.modules["core.handlers"] = handlers_pkg

        command_module = None
        for module_name, relative_path in (
            ("core.handlers.base", ROOT / "core" / "handlers" / "base.py"),
            ("core.handlers.command_handlers", ROOT / "core" / "handlers" / "command_handlers.py"),
        ):
            spec = importlib.util.spec_from_file_location(module_name, relative_path)
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            if module_name == "core.handlers.command_handlers":
                command_module = module

        assert command_module is not None
        return command_module.CommandHandlers


CommandHandlers = _load_command_handlers_class()


class _StubFormatter:
    @staticmethod
    def format_code_inline(text):
        return f"`{text}`"


class _StubIMClient:
    def __init__(self, user_info):
        self.user_info = user_info
        self.sent_messages = []
        self.sent_contexts = []
        self.sent_button_messages = []
        self.channel_info_calls = []
        self.formatter = _StubFormatter()
        self.started_topic_context = None

    async def get_user_info(self, user_id):
        return self.user_info

    async def get_channel_info(self, channel_id):
        self.channel_info_calls.append(channel_id)
        return {"id": channel_id, "name": channel_id}

    async def send_message(self, context, text, parse_mode=None):
        self.sent_contexts.append(context)
        self.sent_messages.append((context.channel_id, text))
        return "T1"

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        self.sent_button_messages.append((context.channel_id, text, keyboard))
        return "T2"

    async def start_new_topic_session(self, context):
        return self.started_topic_context


class _StubSettingsManager:
    def __init__(self):
        self.bind_calls = []
        self.bind_result = (True, False)
        self.custom_cwd_calls = []
        self.session_row = None
        self.session_lookup_calls = []

    def is_bound_user(self, user_id, platform=None):
        return False

    def bind_user_with_code(self, user_id, display_name, code, dm_chat_id="", platform=None):
        self.bind_calls.append((user_id, display_name, code, dm_chat_id, platform))
        return self.bind_result

    def set_custom_cwd(self, settings_key, cwd):
        self.custom_cwd_calls.append((settings_key, cwd))

    def find_session_for_anchor(self, session_key, session_anchor):
        self.session_lookup_calls.append((session_key, session_anchor))
        return self.session_row


class _StubController:
    def __init__(self, user_info):
        self.config = type("Config", (), {"platform": "slack", "language": "zh"})()
        self.im_client = _StubIMClient(user_info)
        self.settings_manager = _StubSettingsManager()
        self.sessions = self.settings_manager
        self.session_handler = type(
            "SessionHandler",
            (),
            {"get_base_session_id": staticmethod(lambda context: f"{context.platform}_{context.channel_id}")},
        )()
        self.session_manager = object()
        self.receiver_tasks = {}
        self.cleared_sessions = []

        async def _clear_sessions(session_key):
            self.cleared_sessions.append(session_key)
            return {"claude": 1}

        self.agent_service = type(
            "AgentService",
            (),
            {"default_agent": "codex", "clear_sessions": staticmethod(_clear_sessions)},
        )()

    def _get_settings_key(self, context: MessageContext) -> str:
        return context.user_id if context.channel_id.startswith("D") else context.channel_id

    def _get_session_key(self, context: MessageContext) -> str:
        platform = getattr(context, "platform", None) or "test"
        is_dm = bool((context.platform_specific or {}).get("is_dm", False))
        if is_dm and context.channel_id == context.user_id:
            return f"{platform}::user::{self._get_settings_key(context)}"
        return f"{platform}::{self._get_settings_key(context)}"

    def resolve_agent_for_context(self, context: MessageContext) -> str:
        return "codex"


class CommandHandlerUserNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_bind_success_prefers_real_name_when_display_name_blank(self):
        controller = _StubController(
            {
                "display_name": "",
                "display_name_normalized": "",
                "real_name": "Alex",
                "real_name_normalized": "Alex",
                "name": "cyh",
            }
        )
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="U0E0FM3QT", channel_id="D123")

        await handler.handle_bind(context, "bind-code")

        self.assertEqual(
            controller.settings_manager.bind_calls,
            [("U0E0FM3QT", "Alex", "bind-code", "D123", "slack")],
        )
        self.assertEqual(
            controller.im_client.sent_messages,
            [
                (
                    "D123",
                    "✅ 绑定成功！欢迎，Alex。你现在可以通过私信使用 Avibe。\n\n"
                    "要打开操作菜单，直接 @bot 即可，不需要加任何内容。",
                )
            ],
        )

    async def test_bind_rate_limit_blocks_before_code_validation(self):
        controller = _StubController({"display_name": "Alex"})
        handler = CommandHandlers(controller)
        handler._bind_attempt_limiter = type(
            "Limiter",
            (),
            {
                "check": lambda _self, **_kwargs: types.SimpleNamespace(
                    allowed=False,
                    retry_after_seconds=42,
                ),
                "record_failure": lambda _self, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
                "reset": lambda _self, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
            },
        )()
        context = MessageContext(user_id="U0E0FM3QT", channel_id="D123")

        await handler.handle_bind(context, "bad-code")

        self.assertEqual(controller.settings_manager.bind_calls, [])
        self.assertEqual(controller.im_client.sent_messages, [("D123", "❌ 绑定码错误次数过多，请 42 秒后再试。")])

    async def test_bind_invalid_code_records_failed_attempt(self):
        controller = _StubController({"display_name": "Alex"})
        controller.settings_manager.bind_result = (False, False)
        handler = CommandHandlers(controller)
        calls = []

        class Limiter:
            def check(self, **kwargs):
                return types.SimpleNamespace(allowed=True)

            def record_failure(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(allowed=False, retry_after_seconds=30)

            def reset(self, **kwargs):
                raise AssertionError("unexpected")

        handler._bind_attempt_limiter = Limiter()
        context = MessageContext(user_id="U0E0FM3QT", channel_id="D123")

        await handler.handle_bind(context, "bad-code")

        self.assertEqual(calls, [{"platform": "slack", "user_id": "U0E0FM3QT", "channel_id": "D123"}])
        self.assertEqual(controller.im_client.sent_messages, [("D123", "❌ 绑定码错误次数过多，请 30 秒后再试。")])

    async def test_wechat_bind_success_points_to_start_menu(self):
        controller = _StubController({"display_name": "小王"})
        setattr(controller.config, "platform", "wechat")
        handler = CommandHandlers(controller)
        context = MessageContext(
            user_id="wx-user",
            channel_id="wx-user",
            platform="wechat",
            platform_specific={"platform": "wechat", "is_dm": True},
        )

        await handler.handle_bind(context, "bind-code")

        self.assertEqual(
            controller.im_client.sent_messages,
            [
                (
                    "wx-user",
                    "✅ 绑定成功！欢迎，小王。你现在可以通过私信使用 Avibe。\n\n"
                    "发送 `/start` 即可唤起更多操作菜单。",
                )
            ],
        )

    async def test_wechat_start_message_uses_localized_compact_commands(self):
        controller = _StubController({"display_name": "小王"})
        setattr(controller.config, "platform", "wechat")
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat")

        await handler.handle_start(context)

        self.assertEqual(len(controller.im_client.sent_messages), 1)
        _, message = controller.im_client.sent_messages[0]
        self.assertIn("欢迎使用 Avibe！", message)
        self.assertIn("你好 小王！", message)
        self.assertIn("/start - 显示欢迎消息", message)
        self.assertIn("/setcwd <路径> - 设置工作目录", message)
        self.assertIn("/resume - 恢复当前目录下最近的会话", message)
        self.assertIn("/setup [claude|codex|opencode] - 修复后端登录/认证", message)
        self.assertIn("/new - 开启一个全新的会话", message)
        self.assertNotIn("User ID", message)
        self.assertNotIn("How it works", message)
        self.assertNotIn("频道：", message)

    async def test_new_command_sends_fresh_session_confirmation(self):
        controller = _StubController({"display_name": "小王"})
        controller.agent_service.clear_sessions = _clear_sessions  # type: ignore[attr-defined]
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat")

        await handler.handle_new(context)

        self.assertEqual(
            controller.im_client.sent_messages,
            [("wx-chat", "🆕 已开启新的会话。你下一条消息会从全新对话开始。")],
        )

    async def test_setcwd_keeps_existing_scope_session_and_shows_new_hint(self):
        controller = _StubController({"display_name": "小王"})
        controller.settings_manager.session_row = {"agent_backend": "claude"}
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat", platform="wechat")

        await handler.handle_set_cwd(context, ".")

        self.assertEqual(controller.settings_manager.custom_cwd_calls, [("wx-chat", str(ROOT))])
        self.assertEqual(controller.cleared_sessions, [])
        self.assertEqual(controller.settings_manager.session_lookup_calls, [("wechat::wx-chat", "wechat_wx-chat")])
        self.assertEqual(len(controller.im_client.sent_messages), 1)
        text = controller.im_client.sent_messages[0][1]
        self.assertIn("✅", text)
        self.assertIn(str(ROOT), text)
        self.assertIn("请使用 /new 命令创建新会话，以使设置变更生效。新会话创建后将覆盖当前会话。", text)

    async def test_setcwd_does_not_show_new_hint_without_existing_scope_session(self):
        controller = _StubController({"display_name": "小王"})
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat", platform="wechat")

        await handler.handle_set_cwd(context, ".")

        self.assertEqual(controller.cleared_sessions, [])
        self.assertEqual(controller.settings_manager.session_lookup_calls, [("wechat::wx-chat", "wechat_wx-chat")])
        text = controller.im_client.sent_messages[0][1]
        self.assertIn(str(ROOT), text)
        self.assertNotIn("请使用 /new 命令创建新会话", text)

    async def test_telegram_dm_new_command_clears_user_and_legacy_channel_scopes(self):
        controller = _StubController({"display_name": "Alex"})
        setattr(controller.config, "platform", "telegram")
        clear_calls = []
        clear_base_calls = []

        async def _record_clear(session_key):
            clear_calls.append(session_key)
            return {}

        controller.agent_service.clear_sessions = _record_clear  # type: ignore[attr-defined]
        controller.sessions = type(
            "Sessions",
            (),
            {"clear_session_base": lambda _self, key, anchor: clear_base_calls.append((key, anchor)) or 1},
        )()
        handler = CommandHandlers(controller)
        context = MessageContext(
            user_id="58181121",
            channel_id="58181121",
            message_id="77",
            platform="telegram",
            platform_specific={"platform": "telegram", "is_dm": True},
        )

        await handler.handle_new(context)

        self.assertEqual(
            clear_calls,
            ["telegram::user::58181121", "telegram::channel::58181121", "telegram::58181121"],
        )
        self.assertEqual(
            clear_base_calls,
            [
                ("telegram::user::58181121", "telegram_58181121"),
                ("telegram::channel::58181121", "telegram_58181121"),
                ("telegram::58181121", "telegram_58181121"),
            ],
        )

    async def test_wechat_dm_new_command_clears_user_and_legacy_channel_scopes(self):
        controller = _StubController({"display_name": "Alex"})
        setattr(controller.config, "platform", "wechat")
        clear_calls = []
        clear_base_calls = []

        async def _record_clear(session_key):
            clear_calls.append(session_key)
            return {}

        controller.agent_service.clear_sessions = _record_clear  # type: ignore[attr-defined]
        controller.sessions = type(
            "Sessions",
            (),
            {"clear_session_base": lambda _self, key, anchor: clear_base_calls.append((key, anchor)) or 1},
        )()
        handler = CommandHandlers(controller)
        context = MessageContext(
            user_id="wxid_alice",
            channel_id="wxid_alice",
            message_id="77",
            platform="wechat",
            platform_specific={"platform": "wechat", "is_dm": True},
        )

        await handler.handle_new(context)

        self.assertEqual(
            clear_calls,
            ["wechat::user::wxid_alice", "wechat::channel::wxid_alice", "wechat::wxid_alice"],
        )
        self.assertEqual(
            clear_base_calls,
            [
                ("wechat::user::wxid_alice", "wechat_wxid_alice"),
                ("wechat::channel::wxid_alice", "wechat_wxid_alice"),
                ("wechat::wxid_alice", "wechat_wxid_alice"),
            ],
        )

    async def test_telegram_new_command_creates_topic_session_when_supported(self):
        controller = _StubController({"display_name": "Alex"})
        setattr(controller.config, "platform", "telegram")
        controller.agent_service.clear_sessions = _clear_sessions  # type: ignore[attr-defined]
        handler = CommandHandlers(controller)
        controller.im_client.started_topic_context = MessageContext(
            user_id="42",
            channel_id="-100123",
            thread_id="99",
            platform="telegram",
        )
        context = MessageContext(
            user_id="42",
            channel_id="-100123",
            thread_id="1",
            platform="telegram",
            platform_specific={"platform": "telegram"},
        )

        await handler.handle_new(context)

        self.assertEqual(
            controller.im_client.sent_messages,
            [("-100123", "🆕 已开启新的会话。你下一条消息会从全新对话开始。")],
        )
        self.assertEqual(controller.im_client.sent_contexts[0].thread_id, "99")

    async def test_slack_dm_start_skips_channel_info_lookup(self):
        controller = _StubController({"display_name": "Alex"})
        handler = CommandHandlers(controller)
        context = MessageContext(
            user_id="U0E0FM3QT",
            channel_id="D123",
            platform="slack",
            platform_specific={"is_dm": True, "platform": "slack"},
        )

        await handler.handle_start(context)

        self.assertEqual(controller.im_client.channel_info_calls, [])
        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        _, text, _ = controller.im_client.sent_button_messages[0]
        self.assertIn("私信", text)


async def _clear_sessions(_settings_key):
    return {}


if __name__ == "__main__":
    unittest.main()
