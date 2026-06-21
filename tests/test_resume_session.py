import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.controller import Controller
from core.handlers.command_handlers import CommandHandlers
from core.handlers.session_handler import SessionHandler
from modules.agents.native_sessions.types import NativeResumeSession
from modules.im import MessageContext
from config.v2_config import SlackConfig

try:
    from modules.im.slack import SlackBot
except ModuleNotFoundError:
    SlackBot = None


class _StubSettingsManager:
    def __init__(self):
        self.set_calls = []
        self.mark_calls = []
        self.routing_calls = []
        self.remove_calls = []

    def remove_agent_session(self, settings_key, agent_name, thread_id):
        # Resume clears any prior binding at the anchor before re-binding so the
        # bind creates a fresh record (never mutates an existing native).
        self.remove_calls.append((settings_key, agent_name, thread_id))
        return False

    def set_agent_session_mapping(self, settings_key, agent_name, thread_id, session_id):
        self.set_calls.append((settings_key, agent_name, thread_id, session_id))

    def mark_thread_active(self, user_id, channel_id, thread_ts):
        self.mark_calls.append((user_id, channel_id, thread_ts))

    def list_all_agent_sessions(self, user_id):
        return {}

    def get_channel_routing(self, settings_key):
        return None

    def set_channel_routing(self, settings_key, routing):
        self.routing_calls.append((settings_key, routing))


class _StubIMClient:
    def __init__(self):
        self.messages = []
        self.resume_calls = []
        self.prepared_context = None

    async def send_message(self, context, text, parse_mode=None):
        ts = f"T{len(self.messages) + 1}"
        self.messages.append((context.channel_id, context.thread_id, text, ts))
        return ts

    async def open_resume_session_modal(self, trigger_id, sessions, channel_id, thread_id, host_message_ts):
        self.resume_calls.append((trigger_id, sessions, channel_id, thread_id, host_message_ts))

    async def run_on_client_loop(self, coro):
        return await coro

    async def prepare_resume_context(self, context, host_message_ts=None, is_dm=False):
        return self.prepared_context or context


class _StubNativeSessionService:
    def __init__(self, sessions=None):
        self.sessions = sessions or []
        self.calls = []

    def list_recent_sessions(self, working_path: str, limit: int = 100):
        self.calls.append((working_path, limit))
        return list(self.sessions)

    def get_session(self, working_path: str, agent: str, native_session_id: str):
        for item in self.sessions:
            if item.agent == agent and item.native_session_id == native_session_id:
                return item
        return None


class _StubConfig:
    def __init__(self, platform="slack"):
        self.platform = platform
        self.language = "en"
        self.claude = type("ClaudeCfg", (), {"cwd": "/tmp"})()


class _StubController(Controller):
    def __init__(self):
        # Bypass base __init__ to avoid wiring everything
        pass

    def init_minimal(self, im_client, settings_manager, config, session_manager=None):
        self.im_client = im_client
        self.settings_manager = settings_manager
        self.sessions = settings_manager
        self.config = config
        self.session_manager = session_manager
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}
        self.agent_service = type("A", (), {"agents": {"claude": object(), "codex": object()}})()
        self.native_session_service = _StubNativeSessionService()
        self.command_handler = CommandHandlers(self)
        self.session_handler = SessionHandler(self)

    def _get_settings_key(self, context: MessageContext) -> str:
        return context.user_id if (context.platform_specific or {}).get("is_dm") else context.channel_id

    def _get_session_key(self, context: MessageContext) -> str:
        return f"{getattr(context, 'platform', None) or 'test'}::{self._get_settings_key(context)}"

    def get_cwd(self, context: MessageContext) -> str:
        return "/Users/cyh/avibe"


class ResumeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_resume_session_submission_threads(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="claude",
                    agent_prefix="cc",
                    native_session_id="sess_abc",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=10.0,
                    last_agent_message="The latest Claude answer ends with a concise handoff.",
                    last_agent_tail="...concise handoff",
                )
            ]
        )

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="claude",
            session_id="sess_abc",
        )

        self.assertEqual(
            settings.set_calls,
            [("slack::C111", "claude", "slack_169999.123", "sess_abc")],
        )
        self.assertEqual(settings.mark_calls, [("U123", "C111", "169999.123")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertIn("sess_abc", im_client.messages[0][2])
        self.assertIn("The latest Claude answer ends with a concise handoff", im_client.messages[0][2])
        self.assertIn("Reply in this thread", im_client.messages[1][2])

    async def test_handle_resume_session_submission_clears_prior_backend_at_anchor(self):
        # A resume landing on an anchor already pinned to a DIFFERENT backend must
        # clear that row first, or the bind collides with the (scope_id,
        # session_anchor) unique invariant (e.g. a Feishu resume button fired
        # inside an existing thread, bypassing the scope-only command guard). (Codex P2.)
        settings = _StubSettingsManager()
        settings.find_session_for_anchor = lambda settings_key, anchor: {
            "agent_variant": "claude",
            "agent_backend": "claude",
        }
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        ctrl.agent_service = type("A", (), {"agents": {"opencode": object(), "claude": object()}})()

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="opencode",
            session_id="oc_sess",
        )

        anchor = "slack_169999.123"
        # The prior claude row at the anchor was cleared before binding opencode.
        self.assertIn(("slack::C111", "claude", anchor), settings.remove_calls)
        self.assertEqual(settings.set_calls, [("slack::C111", "opencode", anchor, "oc_sess")])

    async def test_handle_resume_session_submission_replaces_record_not_mutates(self):
        # Resume must create a FRESH record at the anchor (clear + re-bind), never
        # mutate the existing native in place — otherwise the native_session_id
        # write-once guard would silently drop the rebind (Codex P2).
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="claude",
            session_id="sess_new",
        )

        # The anchor is cleared first, then re-bound to the user-selected native.
        self.assertEqual(settings.remove_calls, [("slack::C111", "claude", "slack_169999.123")])
        self.assertEqual(settings.set_calls, [("slack::C111", "claude", "slack_169999.123", "sess_new")])

    async def test_handle_resume_session_submission_preserves_same_backend_scope_overrides(self):
        from modules.settings_manager import ChannelRouting

        settings = _StubSettingsManager()
        settings.get_channel_routing = lambda _settings_key: ChannelRouting(
            agent_name="reviewer",
            model="gpt-5.5",
            reasoning_effort="xhigh",
            codex_agent="reviewer-sub",
        )
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        ctrl.vibe_agent_store = SimpleNamespace(
            get=lambda name: SimpleNamespace(backend="codex") if name == "reviewer" else None
        )

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="codex",
            session_id="codex_native",
        )

        self.assertEqual(len(settings.routing_calls), 1)
        settings_key, routing = settings.routing_calls[0]
        self.assertEqual(settings_key, "C111")
        self.assertEqual(routing.agent_name, "codex")
        self.assertEqual(routing.model, "gpt-5.5")
        self.assertEqual(routing.reasoning_effort, "xhigh")
        self.assertEqual(routing.codex_agent, "reviewer-sub")

    async def test_handle_resume_session_submission_dm_falls_back_to_channel(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U999",
            channel_id="DXYZ",
            thread_id=None,
            agent="codex",
            session_id="sess_dm",
        )

        # No thread provided -> new confirmation message anchor used
        self.assertEqual(settings.set_calls, [("slack::DXYZ", "codex", "slack_T1", "sess_dm")])
        self.assertEqual(settings.mark_calls, [("U999", "DXYZ", "T1")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertIn("sess_dm", im_client.messages[0][2])
        self.assertIn("Send your next message directly", im_client.messages[1][2])

    async def test_handle_resume_session_submission_prepares_resume_binding(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        codex_agent = type("CodexAgent", (), {"prepare_resume_binding": AsyncMock()})()
        ctrl.agent_service = type("A", (), {"agents": {"claude": object(), "codex": codex_agent}})()

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U999",
            channel_id="C111",
            thread_id="169999.123",
            agent="codex",
            session_id="sess_abc",
        )

        codex_agent.prepare_resume_binding.assert_awaited_once_with(
            base_session_id="slack_169999.123",
            session_key="slack::C111",
            working_path="/Users/cyh/avibe",
        )

    async def test_handle_resume_session_submission_prepares_claude_binding(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        claude_agent = type("ClaudeAgent", (), {"prepare_resume_binding": AsyncMock()})()
        ctrl.agent_service = type("A", (), {"agents": {"claude": claude_agent, "codex": object()}})()

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="claude",
            session_id="sess_abc",
        )

        claude_agent.prepare_resume_binding.assert_awaited_once_with(
            base_session_id="slack_169999.123",
            session_key="slack::C111",
            working_path="/Users/cyh/avibe",
        )

    async def test_handle_resume_session_submission_skips_resume_prepare_when_backend_has_no_hook(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig())
        ctrl.agent_service = type("A", (), {"agents": {"claude": object(), "codex": object()}})()

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U123",
            channel_id="C111",
            thread_id="169999.123",
            agent="claude",
            session_id="sess_abc",
        )

        self.assertEqual(len(settings.set_calls), 1)
        self.assertEqual(settings.set_calls[0][0], "slack::C111")
        self.assertEqual(settings.set_calls[0][1], "claude")
        self.assertEqual(settings.set_calls[0][3], "sess_abc")

    async def test_handle_resume_session_submission_discord_dm_uses_channel_session_key(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="discord"))
        ctrl.im_client.should_use_thread_for_dm_session = lambda: False

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U999",
            channel_id="DMCHAN",
            thread_id=None,
            agent="codex",
            session_id="sess_dm",
            is_dm=True,
        )

        self.assertEqual(settings.set_calls, [("discord::U999", "codex", "discord_DMCHAN", "sess_dm")])
        self.assertEqual(settings.mark_calls, [("U999", "DMCHAN", "T1")])

    async def test_handle_resume_session_submission_lark_dm_uses_thread_session_key(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="lark"))
        ctrl.im_client.should_use_thread_for_dm_session = lambda: True

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U888",
            channel_id="DMCHAT",
            thread_id="root_123",
            agent="claude",
            session_id="sess_lark_dm",
            is_dm=True,
        )

        self.assertEqual(settings.set_calls, [("lark::U888", "claude", "lark_root_123", "sess_lark_dm")])
        self.assertEqual(settings.mark_calls, [("U888", "DMCHAT", "root_123")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertIn("Reply to this message", im_client.messages[1][2])

    async def test_handle_resume_session_submission_uses_prepared_thread_context(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="discord"))
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        im_client.prepared_context = MessageContext(
            user_id="U777",
            channel_id="C777",
            platform="discord",
            thread_id="SUB123",
            message_id="HOST1",
            platform_specific={"is_dm": False},
        )

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U777",
            channel_id="C777",
            thread_id=None,
            agent="codex",
            session_id="sess_sub",
            host_message_ts="HOST1",
            is_dm=False,
            platform="discord",
        )

        self.assertEqual(settings.set_calls, [("discord::C777", "codex", "discord_SUB123", "sess_sub")])
        self.assertEqual(settings.mark_calls, [("U777", "C777", "SUB123")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertEqual(im_client.messages[0][1], None)
        self.assertEqual(im_client.messages[1][1], "SUB123")
        self.assertIn("Reply in the subchannel", im_client.messages[1][2])

    async def test_handle_resume_session_submission_telegram_group_keeps_channel_mapping(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="telegram"))
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        ctrl.im_client.should_use_message_id_for_channel_session = lambda context=None: False

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U777",
            channel_id="-100123",
            thread_id=None,
            agent="codex",
            session_id="sess_telegram_group",
            host_message_ts="HOST1",
            is_dm=False,
            platform="telegram",
        )

        self.assertEqual(
            settings.set_calls,
            [("telegram::-100123", "codex", "telegram_-100123", "sess_telegram_group")],
        )
        self.assertEqual(settings.mark_calls, [("U777", "-100123", "T1")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertEqual(im_client.messages[0][1], None)
        self.assertEqual(im_client.messages[1][1], None)
        self.assertIn("Send your next message directly", im_client.messages[1][2])

    async def test_handle_resume_session_submission_telegram_forum_keeps_topic_mapping(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="telegram"))
        ctrl.im_client.should_use_thread_for_reply = lambda: True
        ctrl.im_client.should_use_message_id_for_channel_session = lambda context=None: False

        await ctrl.session_handler.handle_resume_session_submission(
            user_id="U777",
            channel_id="-100123",
            thread_id="99",
            agent="codex",
            session_id="sess_telegram_topic",
            host_message_ts="HOST1",
            is_dm=False,
            platform="telegram",
        )

        self.assertEqual(
            settings.set_calls,
            [("telegram::-100123", "codex", "telegram_99", "sess_telegram_topic")],
        )
        self.assertEqual(settings.mark_calls, [("U777", "-100123", "99")])
        self.assertEqual(len(im_client.messages), 2)
        self.assertEqual(im_client.messages[0][1], "99")
        self.assertEqual(im_client.messages[1][1], "99")
        self.assertIn("Reply in this thread", im_client.messages[1][2])

    async def test_command_handlers_handle_resume_opens_modal(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="slack"))
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="codex",
                    agent_prefix="cx",
                    native_session_id="thread_123",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=100.0,
                    last_agent_message="done",
                    last_agent_tail="...done",
                )
            ]
        )

        # Scope-level invocation (no thread): /resume is only allowed at the
        # channel level, so the modal anchors on the host message.
        ctx = MessageContext(
            user_id="U1",
            channel_id="CCHAN",
            thread_id=None,
            message_id="TS1",
            platform_specific={"trigger_id": "TRIG"},
        )

        await ctrl.command_handler.handle_resume(ctx)

        self.assertEqual(im_client.messages, [])
        self.assertEqual(len(im_client.resume_calls), 1)
        trigger_id, sessions, channel_id, thread_id, host_ts = im_client.resume_calls[0]
        self.assertEqual((trigger_id, channel_id, thread_id, host_ts), ("TRIG", "CCHAN", "TS1", "TS1"))
        self.assertEqual([item.native_session_id for item in sessions], ["thread_123"])
        self.assertEqual(ctrl.native_session_service.calls, [("/Users/cyh/avibe", 100)])

    async def test_handle_resume_in_existing_thread_is_rejected(self):
        # /resume is scope-level only: invoking it inside an existing thread must be
        # rejected (no modal/menu) so it can never rebind that thread's session.
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="slack"))

        ctx = MessageContext(
            user_id="U1",
            channel_id="CCHAN",
            thread_id="TH1",
            message_id="TS1",
            platform="slack",
            platform_specific={"trigger_id": "TRIG"},
        )

        await ctrl.command_handler.handle_resume(ctx)

        # No modal opened; a scope-only explanation is sent instead.
        self.assertEqual(im_client.resume_calls, [])
        self.assertEqual(len(im_client.messages), 1)
        self.assertIn("channel level", im_client.messages[0][2])

    async def test_command_handlers_handle_resume_filters_disabled_backends_before_modal(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="slack"))
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="opencode",
                    agent_prefix="oc",
                    native_session_id="oc_disabled",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=200.0,
                    last_agent_message="done",
                    last_agent_tail="...done",
                ),
                NativeResumeSession(
                    agent="codex",
                    agent_prefix="cx",
                    native_session_id="cx_enabled",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=100.0,
                    last_agent_message="done",
                    last_agent_tail="...done",
                ),
            ]
        )

        ctx = MessageContext(
            user_id="U1",
            channel_id="CCHAN",
            thread_id=None,
            message_id="TS1",
            platform="slack",
            platform_specific={"trigger_id": "TRIG"},
        )

        await ctrl.command_handler.handle_resume(ctx)

        self.assertEqual(len(im_client.resume_calls), 1)
        _, sessions, _, _, _ = im_client.resume_calls[0]
        self.assertEqual([item.native_session_id for item in sessions], ["cx_enabled"])

    async def test_command_handlers_handle_resume_without_trigger_sends_menu_prompt(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="slack"))
        ctrl.command_handler.handle_start = AsyncMock()

        ctx = MessageContext(
            user_id="U1",
            channel_id="CCHAN",
            thread_id=None,
            message_id="TS1",
            platform="slack",
            platform_specific={},
        )

        await ctrl.command_handler.handle_resume(ctx)

        self.assertEqual(len(im_client.messages), 1)
        self.assertIn("menu message", im_client.messages[0][2])
        self.assertEqual(ctrl.native_session_service.calls, [])
        ctrl.command_handler.handle_start.assert_awaited_once()

    async def test_command_handlers_handle_resume_telegram_uses_native_sessions(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="telegram"))
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="codex",
                    agent_prefix="cx",
                    native_session_id="session_telegram_123",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=100.0,
                    last_agent_message="done",
                    last_agent_tail="...done",
                )
            ]
        )

        # Scope-level invocation (no topic/thread): resume is only allowed at the
        # group/channel level, so the modal anchors on the host message.
        ctx = MessageContext(
            user_id="U1",
            channel_id="TGCHAN",
            thread_id=None,
            message_id="MSG1",
            platform="telegram",
            platform_specific={"is_dm": False},
        )

        await ctrl.command_handler.handle_resume(ctx)

        self.assertEqual(im_client.messages, [])
        self.assertEqual(len(im_client.resume_calls), 1)
        trigger_id, sessions, channel_id, thread_id, host_ts = im_client.resume_calls[0]
        self.assertEqual(trigger_id, ctx)
        self.assertEqual((channel_id, thread_id, host_ts), ("TGCHAN", "MSG1", "MSG1"))
        self.assertEqual([item.native_session_id for item in sessions], ["session_telegram_123"])
        self.assertEqual(ctrl.native_session_service.calls, [("/Users/cyh/avibe", 25)])

    async def test_command_handlers_handle_resume_wechat_lists_recent_sessions(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="wechat"))
        ctrl.config.language = "zh"
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="claude",
                    agent_prefix="cc",
                    native_session_id="claude-1",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=datetime(2026, 3, 27, 14, 32),
                    sort_ts=10.0,
                    last_agent_message="",
                    last_agent_tail="...修好了 Claude fallback 列表",
                ),
                NativeResumeSession(
                    agent="codex",
                    agent_prefix="cx",
                    native_session_id="codex-1",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=datetime(2026, 3, 27, 14, 10),
                    sort_ts=9.0,
                    last_agent_message="",
                    last_agent_tail="...继续在子区里回复这条消息",
                ),
            ]
        )

        ctx = MessageContext(
            user_id="wx-user",
            channel_id="wx-chat",
            platform="wechat",
            platform_specific={"is_dm": True, "platform": "wechat"},
        )

        await ctrl.command_handler.handle_resume(ctx)

        self.assertEqual(len(im_client.messages), 1)
        text = im_client.messages[0][2]
        self.assertIn("当前工作目录下最近的 Agent 会话", text)
        self.assertIn("1. cc ...修好了 Claude fallback 列表", text)
        self.assertIn("2. cx ...继续在子区里回复这条消息", text)
        self.assertIn("/resume 1 - 恢复当前列表中的第 1 条", text)
        self.assertIn("/resume more - 查看下一页", text)

    async def test_command_handlers_handle_resume_wechat_numeric_selection_uses_snapshot(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="wechat"))
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="opencode",
                    agent_prefix="oc",
                    native_session_id="oc-1",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=10.0,
                    last_agent_message="",
                    last_agent_tail="...第一条",
                ),
                NativeResumeSession(
                    agent="claude",
                    agent_prefix="cc",
                    native_session_id="cc-2",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=9.0,
                    last_agent_message="",
                    last_agent_tail="...第二条",
                ),
            ]
        )
        ctrl.session_handler.handle_resume_session_submission = AsyncMock()
        ctx = MessageContext(
            user_id="wx-user",
            channel_id="wx-chat",
            platform="wechat",
            message_id="MSG1",
            platform_specific={"is_dm": True, "platform": "wechat"},
        )

        await ctrl.command_handler.handle_resume(ctx)
        await ctrl.command_handler.handle_resume(ctx, "1")

        ctrl.session_handler.handle_resume_session_submission.assert_awaited_once_with(
            user_id="wx-user",
            channel_id="wx-chat",
            thread_id=None,
            agent="claude",
            session_id="cc-2",
            host_message_ts="MSG1",
            is_dm=True,
            platform="wechat",
        )

    async def test_command_handlers_handle_resume_wechat_manual_backend_session_id(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="wechat"))
        ctrl.session_handler.handle_resume_session_submission = AsyncMock()
        ctx = MessageContext(
            user_id="wx-user",
            channel_id="wx-chat",
            platform="wechat",
            message_id="MSG1",
            platform_specific={"is_dm": True, "platform": "wechat"},
        )

        await ctrl.command_handler.handle_resume(ctx, "cc 59adbb74-ce14-418f-b176-28210e21b6ae")

        ctrl.session_handler.handle_resume_session_submission.assert_awaited_once_with(
            user_id="wx-user",
            channel_id="wx-chat",
            thread_id=None,
            agent="claude",
            session_id="59adbb74-ce14-418f-b176-28210e21b6ae",
            host_message_ts="MSG1",
            is_dm=True,
            platform="wechat",
        )

    async def test_command_handlers_handle_resume_wechat_latest_skips_disabled_backends(self):
        settings = _StubSettingsManager()
        im_client = _StubIMClient()
        ctrl = _StubController()
        ctrl.init_minimal(im_client, settings, _StubConfig(platform="wechat"))
        ctrl.native_session_service = _StubNativeSessionService(
            [
                NativeResumeSession(
                    agent="opencode",
                    agent_prefix="oc",
                    native_session_id="oc_disabled",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=200.0,
                    last_agent_message="",
                    last_agent_tail="...latest disabled",
                ),
                NativeResumeSession(
                    agent="codex",
                    agent_prefix="cx",
                    native_session_id="cx_enabled",
                    working_path="/Users/cyh/avibe",
                    created_at=None,
                    updated_at=None,
                    sort_ts=100.0,
                    last_agent_message="",
                    last_agent_tail="...latest enabled",
                ),
            ]
        )
        ctrl.session_handler.handle_resume_session_submission = AsyncMock()
        ctx = MessageContext(
            user_id="wx-user",
            channel_id="wx-chat",
            platform="wechat",
            message_id="MSG1",
            platform_specific={"is_dm": True, "platform": "wechat"},
        )

        await ctrl.command_handler.handle_resume(ctx, "latest")

        ctrl.session_handler.handle_resume_session_submission.assert_awaited_once_with(
            user_id="wx-user",
            channel_id="wx-chat",
            thread_id=None,
            agent="codex",
            session_id="cx_enabled",
            host_message_ts="MSG1",
            is_dm=True,
            platform="wechat",
        )

    async def test_resume_modal_manual_session_uses_manual_agent(self):
        if SlackBot is None:
            self.skipTest("Slack dependencies not installed in this environment")
        cfg = SlackConfig(bot_token="xoxb-test")
        slack = SlackBot(cfg)
        received = {}

        async def _on_resume(user_id, channel_id, thread_id, agent, session, host_ts):
            received["args"] = (user_id, channel_id, thread_id, agent, session, host_ts)

        slack._on_resume_session = _on_resume

        payload = {
            "type": "view_submission",
            "user": {"id": "U1"},
            "view": {
                "callback_id": "resume_session_modal",
                "state": {
                    "values": {
                        "agent_block": {"agent_select": {"selected_option": {"value": "codex"}}},
                        "manual_block": {"manual_input": {"value": "manual_sess"}},
                        "session_block": {"session_select": {"selected_option": {"value": "claude|sess_drop"}}},
                    }
                },
                "private_metadata": ('{"channel_id":"C1","thread_id":"TH1","host_message_ts":"TS1"}'),
            },
        }

        await slack._handle_view_submission(payload)

        self.assertEqual(
            received["args"],
            ("U1", "C1", "TH1", "codex", "manual_sess", "TS1"),
        )


if __name__ == "__main__":
    unittest.main()
