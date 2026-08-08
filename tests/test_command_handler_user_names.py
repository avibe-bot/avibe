import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.im import MessageContext

# Imported before ``_load_command_handlers_class`` runs: that loader swaps
# ``sys.modules`` under a ``patch.dict``, so a module first imported inside it is
# dropped on exit and re-imported later as a SECOND object with its own
# ContextVar. Pre-importing keeps one identity for the teardown ledger below.
import storage.session_reclaim  # noqa: F401


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

    async def test_new_flushes_before_clear_and_continues_after_failed_flush(self):
        controller = _StubController({"display_name": "小王"})
        calls = []

        async def _final_flush(context, raw_session_id, *, deadline_seconds):
            calls.append(("flush", context, raw_session_id, deadline_seconds))
            raise RuntimeError("provider unavailable")

        async def _clear_sessions(session_key):
            calls.append(("clear", session_key))
            return {}

        controller.final_flush_memory_session = _final_flush
        controller.agent_service.clear_sessions = _clear_sessions  # type: ignore[attr-defined]
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat", platform="wechat")

        await handler.handle_new(context)

        self.assertEqual(
            calls,
            [
                ("flush", context, "wechat_wx-chat", 5.0),
                ("clear", "wechat::wx-chat"),
            ],
        )
        self.assertEqual(len(controller.im_client.sent_messages), 1)

    async def test_new_does_not_flush_a_fallback_session_anchor(self):
        controller = _StubController({"display_name": "Alex"})
        flush_calls = []
        clear_base_calls = []

        async def _final_flush(*args, **kwargs):
            flush_calls.append((args, kwargs))

        def _raise_for_missing_canonical_anchor(_context):
            raise RuntimeError("session identity unavailable")

        controller.final_flush_memory_session = _final_flush
        controller.session_handler.get_base_session_id = _raise_for_missing_canonical_anchor
        controller.sessions = type(
            "Sessions",
            (),
            {"clear_session_base": lambda _self, key, anchor: clear_base_calls.append((key, anchor)) or 1},
        )()
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="U1", channel_id="C1", message_id="M1", platform="slack")

        await handler.handle_new(context)

        self.assertEqual(flush_calls, [])
        self.assertEqual(clear_base_calls, [("slack::C1", "slack_M1")])

    async def test_new_command_reports_paused_bound_definitions(self):
        """D2 — `/new` pauses definitions pinned to the session it clears, and says so.

        Without the notice the user's daily report silently stops with no
        indication that an everyday command switched it off.
        """
        from storage.session_reclaim import current_reclaim_ledger

        controller = _StubController({"display_name": "小王"})

        async def _clear_and_reclaim(session_key):
            ledger = current_reclaim_ledger()
            assert ledger is not None, "/new did not open a teardown context"
            ledger.append(
                {
                    "definition_id": "task1",
                    "definition_type": "scheduled",
                    "mode": "pause",
                    "session_id": "ses1",
                }
            )
            return {"claude": 1}

        controller.agent_service.clear_sessions = _clear_and_reclaim  # type: ignore[attr-defined]
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat")

        await handler.handle_new(context)

        self.assertEqual(len(controller.im_client.sent_messages), 1)
        text = controller.im_client.sent_messages[0][1]
        self.assertIn("已开启新的会话", text)
        self.assertIn("已暂停 1 个", text)
        self.assertIn("vibe task resume", text)

    async def test_new_command_gives_watches_their_own_recovery_commands(self):
        """HFR-058 — a paused watch must be told the commands that can reach it.

        Tasks and watches are reclaimed by the same teardown but are managed by
        two different command groups. ``vibe task resume <watch-id>`` fails with
        ``task_not_found``, so a combined notice hands the watch half of the
        audience directions that cannot work -- and the watch stays paused.
        """
        from storage.session_reclaim import current_reclaim_ledger

        controller = _StubController({"display_name": "小王"})

        async def _clear_and_reclaim(session_key):
            ledger = current_reclaim_ledger()
            assert ledger is not None, "/new did not open a teardown context"
            ledger.append(
                {
                    "definition_id": "task1",
                    "definition_type": "scheduled",
                    "mode": "pause",
                    "session_id": "ses1",
                }
            )
            ledger.append(
                {
                    "definition_id": "watch1",
                    "definition_type": "watch",
                    "mode": "pause",
                    "session_id": "ses1",
                }
            )
            return {"claude": 1}

        controller.agent_service.clear_sessions = _clear_and_reclaim  # type: ignore[attr-defined]
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="wx-user", channel_id="wx-chat")

        await handler.handle_new(context)

        text = controller.im_client.sent_messages[0][1]
        self.assertIn("vibe task resume", text)
        self.assertIn("vibe watch resume", text)
        self.assertIn("vibe watch list", text)

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
        flush_calls = []
        call_order = []

        async def _final_flush(context, raw_session_id, *, deadline_seconds):
            call_order.append("flush")
            flush_calls.append((context, raw_session_id, deadline_seconds))

        controller.final_flush_memory_session = _final_flush
        controller.session_handler = type(
            "SessionHandler",
            (),
            {"get_base_session_id": staticmethod(lambda _context: "telegram_-100123_1")},
        )()
        handler = CommandHandlers(controller)
        controller.im_client.started_topic_context = MessageContext(
            user_id="42",
            channel_id="-100123",
            thread_id="99",
            platform="telegram",
        )
        original_start = controller.im_client.start_new_topic_session

        async def _start_new_topic(context):
            call_order.append("topic")
            return await original_start(context)

        controller.im_client.start_new_topic_session = _start_new_topic
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
        self.assertEqual(flush_calls, [(context, "telegram_-100123_1", 5.0)])
        self.assertEqual(call_order, ["flush", "topic"])

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


class _RecordingBackendAgent:
    """One registered backend adapter, over REAL storage.

    ``clear_sessions`` reproduces the adapter's storage-facing line verbatim --
    ``modules/agents/codex/agent.py`` and its siblings all run
    ``self.sessions.clear_agent_sessions(session_key, self.name)`` -- and drops
    only the in-memory runtime bookkeeping (turn registry, session locks) that
    has no bearing on which rows survive. The store underneath is the real
    ``config.v2_sessions.SessionsStore``, so the SQL that decides survival is
    production's, not the test's.
    """

    def __init__(self, name: str, sessions, calls: list):
        self.name = name
        self.sessions = sessions
        self._calls = calls

    async def clear_sessions(self, session_key: str) -> int:
        self._calls.append(("clear_agent_sessions", session_key, self.name))
        return self.sessions.clear_agent_sessions(session_key, self.name)


class _RealStorageAgentService:
    """Mirrors ``AgentService.clear_sessions``: fan out to EVERY registered backend.

    The fan-out is the point. ``/new`` does not clear only the backend that owns
    the live session; it clears all of them, which is how a clear issued for
    ``claude`` reaches a row bound to ``codex``.
    """

    def __init__(self, sessions, names, calls: list):
        self.default_agent = "codex"
        self.agents = {name: _RecordingBackendAgent(name, sessions, calls) for name in names}

    async def clear_sessions(self, session_key: str) -> dict:
        cleared: dict = {}
        for name, agent in self.agents.items():
            count = await agent.clear_sessions(session_key)
            if count:
                cleared[name] = count
        return cleared


class NewCommandStorageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """HFR-242 — `/new` end to end, over real storage rather than a service call.

    The service-method regressions (HFR-240/241) call ``delete_agent_sessions``
    directly, which is the path that was FIXED rather than the path a user
    takes. That is how the previous fix passed its own test while ``/new`` still
    destroyed the row: the guard sat in the ``session_anchor_prefix`` branch, and
    ``handle_new`` reaches storage through ``agent_service.clear_sessions()``
    FIRST -- backend-wide, no prefix -- so the row was already gone before the
    guarded clear ran. Only a test that drives ``handle_new`` itself, through
    both calls in their real order, can catch that class of miss.
    """

    async def test_new_keeps_a_superseded_session_and_its_definitions_but_clears_the_live_one(self):
        from sqlalchemy import select as sa_select

        from config.v2_sessions import SessionsStore
        from storage.db import create_sqlite_engine
        from storage.models import agent_sessions, run_definitions

        session_key = "slack::C9"
        anchor = "slack_C9"
        now = "2026-06-08T00:00:00Z"

        with tempfile.TemporaryDirectory() as tmp_home, patch.dict(
            os.environ, {"AVIBE_HOME": tmp_home}, clear=False
        ):
            state_dir = Path(tmp_home) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            store = SessionsStore(sessions_path=state_dir / "sessions.json")
            engine = None
            try:
                # The row `/new` must NOT destroy: bound to codex with a native id
                # that is write-once, then superseded when claude claims the same
                # anchor. Superseding renames the anchor and keeps the row on
                # purpose -- its transcript is not recoverable.
                superseded_id = store.bind_agent_session(session_key, "codex", anchor, "codex-native-1")
                self.assertIsNotNone(superseded_id)
                live_id = store.ensure_agent_session_id(session_key, "claude", anchor)
                self.assertIsNotNone(live_id)
                self.assertNotEqual(live_id, superseded_id)

                engine = create_sqlite_engine(store.db_path)
                with engine.connect() as conn:
                    superseded_anchor = conn.execute(
                        sa_select(agent_sessions.c.session_anchor).where(
                            agent_sessions.c.id == superseded_id
                        )
                    ).scalar_one()
                self.assertIn(":superseded:", superseded_anchor, "fixture did not actually supersede the row")

                # A scheduled task pinned to each row. The pinned/live pair is what
                # separates "the guard works" from "the guard preserves everything".
                with engine.begin() as conn:
                    for def_id, sid in (("task_pinned", superseded_id), ("task_live", live_id)):
                        conn.execute(
                            run_definitions.insert().values(
                                id=def_id,
                                definition_type="scheduled",
                                enabled=1,
                                deleted_at=None,
                                session_id=sid,
                                created_at=now,
                                updated_at=now,
                                metadata_json="{}",
                            )
                        )

                calls: list = []
                real_clear_base = store.clear_session_base

                def _recording_clear_base(user_id, base_session_id):
                    calls.append(("clear_session_base", user_id, base_session_id))
                    return real_clear_base(user_id, base_session_id)

                store.clear_session_base = _recording_clear_base  # type: ignore[method-assign]

                controller = _StubController({"display_name": "Alex"})
                controller.sessions = store
                controller.agent_service = _RealStorageAgentService(store, ["claude", "codex"], calls)
                handler = CommandHandlers(controller)
                context = MessageContext(
                    user_id="U1",
                    channel_id="C9",
                    platform="slack",
                    platform_specific={"platform": "slack"},
                )

                await handler.handle_new(context)

                # `/new`'s real call order: every backend cleared scope-wide FIRST,
                # the anchor-prefixed clear only after. A guard that lives in the
                # second call is dead code by the time it runs.
                self.assertEqual(
                    calls,
                    [
                        ("clear_agent_sessions", session_key, "claude"),
                        ("clear_agent_sessions", session_key, "codex"),
                        ("clear_session_base", session_key, anchor),
                    ],
                )

                with engine.connect() as conn:
                    surviving = {
                        row["id"]: dict(row)
                        for row in conn.execute(sa_select(agent_sessions)).mappings()
                    }
                    definitions = {
                        row["id"]: dict(row)
                        for row in conn.execute(sa_select(run_definitions)).mappings()
                    }
                # The LIVE half, captured while the store is still the one `/new` ran
                # against. Everything above reads the durable rows through an engine
                # this test opened; the process that has to keep serving them is this
                # ``SessionsStore``.
                live_mappings = {
                    str(agent_name): dict(thread_map)
                    for agent_name, thread_map in store.state.session_mappings.get(
                        session_key, {}
                    ).items()
                }
            finally:
                if engine is not None:
                    engine.dispose()
                store.close()

        # 1. The superseded session survived `/new`, transcript pointer intact.
        self.assertIn(
            superseded_id,
            surviving,
            "`/new` hard-deleted the superseded session row; superseding promises the row is kept, "
            "and its native session id is write-once",
        )
        self.assertEqual(
            surviving[superseded_id]["native_session_id"],
            "codex-native-1",
            "the superseded row survived but lost the native id that makes it resumable",
        )

        # 2. Its bound definition was never reclaimed: the session it targets is
        #    still there, so pausing it would silently stop a user's task for no
        #    reason -- the D2 regression in the opposite direction.
        pinned = definitions["task_pinned"]
        self.assertIsNone(pinned["deleted_at"], "the definition pinned to a SURVIVING session was soft-deleted")
        self.assertEqual(
            pinned["enabled"],
            1,
            "the definition pinned to a SURVIVING session was paused; nothing was torn down under it",
        )

        # 3. The live row in the same scope WAS cleared -- the guard is not simply
        #    preserving everything -- and its definition is PAUSED, not deleted
        #    (D2: `/new` is an everyday command, archive is the terminal one).
        self.assertNotIn(live_id, surviving, "`/new` failed to clear the live session it is supposed to clear")
        live_def = definitions["task_live"]
        self.assertIsNone(live_def["deleted_at"], "a `/new` teardown soft-deleted a definition; D2 requires a pause")
        self.assertEqual(live_def["enabled"], 0, "the definition still fires into a deleted session")
        self.assertTrue(live_def["last_error"], "the definition was paused with no explanation")

        # 4. HFR-274 -- AND THE LIVE HALF AGREES WITH ALL OF THAT. Everything above
        #    reads the durable rows through an engine this test opened. The running
        #    process keeps its own map of anchor -> session id, and
        #    ``clear_session_base`` used to prune it by the same prefix rule the SQL
        #    once used: ``<anchor>:superseded:<id>`` starts with the anchor, so the
        #    local pointer to the row the guard deliberately KEPT was deleted anyway
        #    and ``save_state`` made it permanent. A row nothing can reach is not a
        #    row that was kept.
        mapped_anchors = {
            thread_id for thread_map in live_mappings.values() for thread_id in thread_map
        }
        self.assertIn(
            superseded_anchor,
            mapped_anchors,
            "the live session map lost its entry for the superseded session the database "
            f"kept ({superseded_id} at {superseded_anchor}); the map holds {live_mappings!r}",
        )
        self.assertNotIn(
            anchor,
            mapped_anchors,
            "the live session map still points at the anchor `/new` cleared: "
            f"{live_mappings!r}",
        )
        self.assertIn("/new", live_def["last_error"])

        # 4. The user is told about exactly the one definition that was paused.
        #    Two would mean the superseded row was reclaimed as well.
        self.assertEqual(len(controller.im_client.sent_messages), 1)
        text = controller.im_client.sent_messages[0][1]
        self.assertIn("已开启新的会话", text)
        self.assertIn("已暂停 1 个", text)


async def _clear_sessions(_settings_key):
    return {}


if __name__ == "__main__":
    unittest.main()
