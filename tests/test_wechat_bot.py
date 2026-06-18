import asyncio
import json
import logging
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_settings import SettingsStore, UserSettings
from core.auth import AuthResult
from modules.im import MessageContext
from modules.im.wechat import WeChatBot, WeChatConfig, _get_updates_error_code
from modules.im import wechat_api as wechat_api_module
from modules.settings_manager import SettingsManager


class WeChatBotTests(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self) -> WeChatBot:
        bot = WeChatBot(WeChatConfig(bot_token="token", base_url="https://ilinkai.weixin.qq.com"))
        setattr(bot.config, "cdn_base_url", "https://novac2c.cdn.weixin.qq.com/c2c")
        return bot

    def test_extract_text_reads_text_item_and_voice_text(self):
        msg = {
            "item_list": [
                {"type": 1, "text_item": {"text": "hello"}},
                {"type": 3, "voice_item": {"text": "world", "playtime": 2300}},
            ]
        }

        self.assertEqual(WeChatBot._extract_text(msg), "hello [Voice 2.3s] world")

    def test_extract_text_appends_quoted_message_title(self):
        msg = {
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": "look at this"},
                    "ref_msg": {"title": "quoted image summary"},
                }
            ]
        }

        self.assertEqual(WeChatBot._extract_text(msg), "look at this\n[Quoted message: quoted image summary]")

    def test_normalize_poll_timeout_clamps_values(self):
        self.assertEqual(WeChatBot._normalize_poll_timeout("70000"), 60000)
        self.assertEqual(WeChatBot._normalize_poll_timeout("1000"), 5000)
        self.assertEqual(WeChatBot._normalize_poll_timeout("abc"), 35000)

    def test_get_updates_error_code_prefers_errcode(self):
        self.assertEqual(_get_updates_error_code({"errcode": -14, "errmsg": "session timeout"}), -14)
        self.assertIsNone(_get_updates_error_code({"ret": 0}))

    def test_wechat_api_metadata_matches_current_openclaw_weixin_interface(self):
        self.assertEqual(wechat_api_module.CHANNEL_VERSION, "2.4.3")
        self.assertEqual(wechat_api_module.ILINK_APP_ID, "bot")
        self.assertEqual(wechat_api_module.ILINK_APP_CLIENT_VERSION, 132099)
        with patch("vibe.__version__", "3.0.3.dev102+g5a817be5f"):
            self.assertEqual(
                wechat_api_module._build_base_info(),
                {"channel_version": "2.4.3", "bot_agent": "Avibe/3.0.3.dev102+g5a817be5f OpenClaw/2.4.3"},
            )
        self.assertEqual(
            wechat_api_module._build_common_headers(),
            {"iLink-App-Id": "bot", "iLink-App-ClientVersion": "132099"},
        )

    def test_wechat_api_bot_agent_sanitizes_version(self):
        self.assertEqual(wechat_api_module._safe_product_version("3.0.3 dev/102"), "3.0.3_dev_102")

    async def test_wechat_api_get_updates_adds_long_poll_timeout_grace(self):
        captured = {}

        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return '{"ret": 0, "msgs": [], "get_updates_buf": "buf-2"}'

        class _Session:
            def __init__(self, timeout):
                captured["timeout"] = timeout.total

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["data"] = data
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session(timeout)):
            result = await wechat_api_module.get_updates(
                "https://wechat.example.com",
                "token",
                "buf-1",
                timeout_ms=35_000,
            )

        self.assertEqual(result["get_updates_buf"], "buf-2")
        self.assertEqual(captured["timeout"], 40.0)
        self.assertTrue(captured["url"].endswith("/ilink/bot/getupdates"))
        self.assertEqual(captured["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(captured["headers"]["iLink-App-ClientVersion"], "132099")
        self.assertNotIn("Content-Length", captured["headers"])
        self.assertEqual(json.loads(captured["data"])["base_info"]["channel_version"], "2.4.3")

    async def test_wechat_api_get_updates_poll_logs_are_debug(self):
        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return '{"ret": 0, "msgs": [], "get_updates_buf": "buf-2"}'

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session()):
            with self.assertLogs("modules.im.wechat_api", level="DEBUG") as captured:
                await wechat_api_module.get_updates(
                    "https://wechat.example.com",
                    "token",
                    "buf-1",
                    timeout_ms=35_000,
                )

        self.assertTrue(captured.records)
        self.assertTrue(
            all(record.levelno == logging.DEBUG for record in captured.records),
            [record.getMessage() for record in captured.records],
        )

    async def test_wechat_api_get_updates_returns_empty_response_on_timeout(self):
        class _TimeoutingRequest:
            async def __aenter__(self):
                raise TimeoutError()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                return _TimeoutingRequest()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session()):
            result = await wechat_api_module.get_updates(
                "https://wechat.example.com",
                "token",
                "buf-1",
                timeout_ms=35_000,
            )

        self.assertEqual(result, {"ret": 0, "msgs": [], "get_updates_buf": "buf-1"})

    async def test_wechat_api_fetch_keeps_non_long_poll_timeout_unchanged(self):
        captured = {}

        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return '{"ret": 0}'

        class _Session:
            def __init__(self, timeout):
                captured["timeout"] = timeout.total

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                captured["headers"] = headers
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session(timeout)):
            result = await wechat_api_module._api_fetch(
                "https://wechat.example.com",
                "ilink/bot/test",
                {"hello": "world"},
                token="token",
                timeout_ms=15_000,
            )

        self.assertEqual(result, {"ret": 0})
        self.assertEqual(captured["timeout"], 15.0)
        self.assertEqual(captured["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(captured["headers"]["iLink-App-ClientVersion"], "132099")

    async def test_wechat_api_notify_start_uses_current_metadata(self):
        captured = {}

        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return '{"ret": 0}'

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                captured["url"] = url
                captured["data"] = data
                captured["headers"] = headers
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session()):
            result = await wechat_api_module.notify_start("https://wechat.example.com", "token")

        self.assertEqual(result, {"ret": 0})
        self.assertTrue(captured["url"].endswith("/ilink/bot/msg/notifystart"))
        self.assertEqual(json.loads(captured["data"])["base_info"]["channel_version"], "2.4.3")
        self.assertEqual(captured["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(captured["headers"]["iLink-App-ClientVersion"], "132099")

    async def test_wechat_api_qr_fetch_posts_current_interface_metadata(self):
        captured = {}

        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self, content_type=None):
                captured["content_type"] = content_type
                return {"qrcode": "qr-token", "qrcode_img_content": "https://wechat.example.com/qr"}

        class _Session:
            def __init__(self, timeout):
                captured["timeout_total"] = timeout.total

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                captured["url"] = url
                captured["data"] = data
                captured["headers"] = headers
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session(timeout)):
            result = await wechat_api_module.get_bot_qrcode(
                "https://wechat.example.com",
                "3",
                local_token_list=["token-1"],
            )

        self.assertEqual(result["qrcode"], "qr-token")
        self.assertTrue(captured["url"].endswith("/ilink/bot/get_bot_qrcode?bot_type=3"))
        self.assertEqual(json.loads(captured["data"]), {"local_token_list": ["token-1"]})
        self.assertEqual(captured["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(captured["headers"]["iLink-App-ClientVersion"], "132099")
        self.assertEqual(captured["content_type"], None)
        self.assertEqual(captured["timeout_total"], 30.0)

    async def test_wechat_api_qr_status_uses_current_interface_headers_and_timeout(self):
        captured = {}

        class _Response:
            ok = True
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return '{"status": "wait"}'

        class _Session:
            def __init__(self, timeout):
                captured["timeout"] = timeout.total

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return _Response()

        with patch("modules.im.wechat_api.aiohttp.ClientSession", side_effect=lambda timeout: _Session(timeout)):
            result = await wechat_api_module.get_qrcode_status(
                "https://wechat.example.com",
                "qr token",
                verify_code="1234",
                timeout_ms=3000,
            )

        self.assertEqual(result, {"status": "wait"})
        self.assertEqual(captured["timeout"], 3.0)
        self.assertIn("qrcode=qr%20token", captured["url"])
        self.assertIn("verify_code=1234", captured["url"])
        self.assertEqual(captured["headers"], {"iLink-App-Id": "bot", "iLink-App-ClientVersion": "132099"})

    async def test_get_user_info_uses_short_fixed_display_name(self):
        bot = self._make_bot()

        result = await bot.get_user_info("o9cq80wPgvrwjnEYWkG_55au4v8Q@im.wechat")

        self.assertEqual(result["display_name"], "WeChat User")
        self.assertEqual(result["real_name"], "WeChat User")
        self.assertEqual(result["name"], "WeChat User")

    async def test_send_typing_indicator_fetches_typing_ticket(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with patch(
            "modules.im.wechat.wechat_api.get_config",
            new=AsyncMock(return_value={"ret": 0, "typing_ticket": "ticket-1"}),
        ) as mock_cfg:
            with patch("modules.im.wechat.wechat_api.send_typing", new=AsyncMock(return_value=True)) as mock_typing:
                result = await bot.send_typing_indicator(context)

        self.assertTrue(result)
        mock_cfg.assert_awaited_once()
        mock_typing.assert_awaited_once()

    async def test_clear_typing_indicator_uses_cached_ticket(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})
        bot._typing_tickets[("user-1", "ctx-1")] = "ticket-1"

        with patch("modules.im.wechat.wechat_api.send_typing", new=AsyncMock(return_value=True)) as mock_typing:
            result = await bot.clear_typing_indicator(context)

        self.assertTrue(result)
        args = mock_typing.await_args.args  # type: ignore[union-attr]
        self.assertEqual(args[3], "ticket-1")

    async def test_notify_connection_state_is_best_effort(self):
        bot = self._make_bot()

        with patch("modules.im.wechat_api.notify_start", new=AsyncMock(return_value={"ret": 0})) as start:
            await bot._notify_connection_state("start")

        start.assert_awaited_once_with("https://ilinkai.weixin.qq.com", "token")

        with patch("modules.im.wechat_api.notify_stop", new=AsyncMock(side_effect=RuntimeError("offline"))):
            await bot._notify_connection_state("stop")

    async def test_notify_connection_state_sends_stop_once(self):
        bot = self._make_bot()

        with patch("modules.im.wechat_api.notify_start", new=AsyncMock(return_value={"ret": 0})):
            await bot._notify_connection_state("start")

        with patch("modules.im.wechat_api.notify_stop", new=AsyncMock(return_value={"ret": 0})) as stop:
            await bot._notify_connection_state("stop")
            await bot._notify_connection_state("stop")

        stop.assert_awaited_once_with("https://ilinkai.weixin.qq.com", "token")

    async def test_process_inbound_message_dispatches_allowed_message(self):
        bot = self._make_bot()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot._process_media_items = AsyncMock(return_value=None)
        bot.on_message_callback = AsyncMock()

        msg = {
            "message_id": "mid-1",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }

        await bot._process_inbound_message(msg)
        await asyncio.gather(*tuple(bot._message_callback_tasks))

        bot.on_message_callback.assert_awaited_once()
        args = bot.on_message_callback.await_args.args  # type: ignore[union-attr]
        self.assertEqual(args[0].user_id, "user-1")
        self.assertEqual(args[1], "hi")

    async def test_process_inbound_message_sends_pending_bind_menu_hint_once(self):
        SettingsStore.reset_instance()
        store = SettingsStore.get_instance()
        store.set_users_for_platform(
            "wechat",
            {
                "user-1": UserSettings(
                    display_name="WeChat User",
                    enabled=True,
                    pending_bind_menu_hint=True,
                )
            },
        )
        store.save()

        bot = self._make_bot()
        bot.set_settings_manager(SettingsManager(platform="wechat"))
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot._process_media_items = AsyncMock(return_value=None)
        bot.on_message_callback = AsyncMock()
        bot.send_message = AsyncMock(return_value="wc-hint")

        msg = {
            "message_id": "mid-hint-1",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }

        await bot._process_inbound_message(msg)
        await asyncio.gather(*tuple(bot._message_callback_tasks))

        bot.send_message.assert_awaited_once()
        self.assertIn("/start", bot.send_message.await_args.args[1])  # type: ignore[union-attr]
        bot.on_message_callback.assert_awaited_once()
        self.assertEqual(bot.on_message_callback.await_args.args[1], "hi")  # type: ignore[union-attr]
        user = SettingsStore.get_instance().get_user("user-1", platform="wechat")
        self.assertIsNotNone(user)
        self.assertFalse(user.pending_bind_menu_hint)  # type: ignore[union-attr]

        bot.send_message.reset_mock()
        bot.on_message_callback.reset_mock()
        second_msg = {
            "message_id": "mid-hint-2",
            "from_user_id": "user-1",
            "context_token": "ctx-2",
            "item_list": [{"type": 1, "text_item": {"text": "again"}}],
        }

        await bot._process_inbound_message(second_msg)
        await asyncio.gather(*tuple(bot._message_callback_tasks))

        bot.send_message.assert_not_awaited()
        bot.on_message_callback.assert_awaited_once()
        self.assertEqual(bot.on_message_callback.await_args.args[1], "again")  # type: ignore[union-attr]

    async def test_process_inbound_message_sends_pending_bind_menu_hint_before_command(self):
        SettingsStore.reset_instance()
        store = SettingsStore.get_instance()
        store.set_users_for_platform(
            "wechat",
            {
                "user-1": UserSettings(
                    display_name="WeChat User",
                    enabled=True,
                    pending_bind_menu_hint=True,
                )
            },
        )
        store.save()

        bot = self._make_bot()
        bot.set_settings_manager(SettingsManager(platform="wechat"))
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot._process_media_items = AsyncMock(return_value=None)
        bot.send_message = AsyncMock(return_value="wc-hint")
        bot.dispatch_text_command = AsyncMock(return_value=True)
        bot.on_message_callback = AsyncMock()

        msg = {
            "message_id": "mid-hint-command",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "/start"}}],
        }

        await bot._process_inbound_message(msg)

        bot.send_message.assert_awaited_once()
        self.assertIn("/start", bot.send_message.await_args.args[1])  # type: ignore[union-attr]
        bot.dispatch_text_command.assert_awaited_once()
        bot.on_message_callback.assert_not_called()

    async def test_process_inbound_message_keeps_pending_bind_menu_hint_after_send_failure(self):
        SettingsStore.reset_instance()
        store = SettingsStore.get_instance()
        store.set_users_for_platform(
            "wechat",
            {
                "user-1": UserSettings(
                    display_name="WeChat User",
                    enabled=True,
                    pending_bind_menu_hint=True,
                )
            },
        )
        store.save()

        bot = self._make_bot()
        bot.set_settings_manager(SettingsManager(platform="wechat"))
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot._process_media_items = AsyncMock(return_value=None)
        bot.on_message_callback = AsyncMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("temporary send failure"))

        msg = {
            "message_id": "mid-hint-fail",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }

        await bot._process_inbound_message(msg)
        await asyncio.gather(*tuple(bot._message_callback_tasks))

        bot.send_message.assert_awaited_once()
        bot.on_message_callback.assert_awaited_once()
        self.assertEqual(bot.on_message_callback.await_args.args[1], "hi")  # type: ignore[union-attr]
        user = SettingsStore.get_instance().get_user("user-1", platform="wechat")
        self.assertIsNotNone(user)
        self.assertTrue(user.pending_bind_menu_hint)  # type: ignore[union-attr]

    async def test_process_inbound_message_persists_context_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"VIBE_REMOTE_HOME": tmpdir}):
                bot = self._make_bot()
                bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
                bot.dispatch_text_command = AsyncMock(return_value=False)
                bot._process_media_items = AsyncMock(return_value=None)

                msg = {
                    "message_id": "mid-token-1",
                    "from_user_id": "user-1",
                    "context_token": "ctx-persisted",
                    "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
                }

                await bot._process_inbound_message(msg)

                cache_path = Path(tmpdir) / "state" / "wechat_context_tokens.json"
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(data["tokens"]["user-1"]["context_token"], "ctx-persisted")

                restored = self._make_bot()
                restored._load_context_tokens()
                self.assertEqual(restored._get_context_token_for_user("user-1"), "ctx-persisted")

    async def test_send_dm_reuses_persisted_context_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"VIBE_REMOTE_HOME": tmpdir}):
                bot = self._make_bot()
                bot._remember_context_token("user-1", "ctx-persisted")

                restored = self._make_bot()
                with patch("modules.im.wechat.wechat_api.send_message", new=AsyncMock(return_value={})) as mock_send:
                    result = await restored.send_dm("user-1", "hello")

        self.assertIsNotNone(result)
        mock_send.assert_awaited_once()
        self.assertEqual(mock_send.await_args.args[3], "ctx-persisted")  # type: ignore[union-attr]

    async def test_process_inbound_message_does_not_block_commands_behind_running_callback(self):
        bot = self._make_bot()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot._process_media_items = AsyncMock(return_value=None)

        callback_started = asyncio.Event()
        release_callback = asyncio.Event()

        async def running_callback(_context, _text: str):
            callback_started.set()
            await release_callback.wait()

        async def dispatch_command(_context, text: str, allow_plain_bind: bool = False) -> bool:
            return text == "/new"

        bot.on_message_callback = running_callback
        bot.dispatch_text_command = AsyncMock(side_effect=dispatch_command)

        normal_msg = {
            "message_id": "mid-1",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
        command_msg = {
            "message_id": "mid-2",
            "from_user_id": "user-1",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "/new"}}],
        }

        first_task = asyncio.create_task(bot._process_inbound_message(normal_msg))
        await asyncio.sleep(0)
        self.assertTrue(first_task.done())
        await callback_started.wait()

        second_task = asyncio.create_task(bot._process_inbound_message(command_msg))
        await asyncio.sleep(0)
        self.assertTrue(second_task.done())

        dispatched_texts = [call.args[1] for call in bot.dispatch_text_command.await_args_list]
        self.assertEqual(dispatched_texts, ["hi", "/new"])

        release_callback.set()
        await asyncio.gather(*tuple(bot._message_callback_tasks))

    async def test_process_media_items_falls_back_to_referenced_media(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", files=[])
        msg = {
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": "please review"},
                    "ref_msg": {
                        "title": "screenshot",
                        "message_item": {
                            "type": 2,
                            "image_item": {
                                "media": {
                                    "encrypt_query_param": "ref-param",
                                    "aes_key": "ref-key",
                                }
                            },
                        },
                    },
                }
            ]
        }

        await bot._process_media_items(msg, context)

        self.assertIsNotNone(context.files)
        self.assertEqual(len(context.files or []), 1)
        attachment = (context.files or [])[0]
        self.assertEqual(attachment.url, "ref-param")

    async def test_process_inbound_message_sends_denial_for_unauthorized_user(self):
        bot = self._make_bot()
        bot.check_authorization = lambda **kwargs: AuthResult(
            allowed=False,
            denial="unbound_dm",
            is_dm=True,
        )
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot._process_media_items = AsyncMock(return_value=None)
        bot.send_message = AsyncMock(return_value="wc-1")
        bot.on_message_callback = AsyncMock()

        msg = {
            "message_id": "mid-2",
            "from_user_id": "user-2",
            "context_token": "ctx-2",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }

        await bot._process_inbound_message(msg)

        bot.send_message.assert_awaited_once()
        bot.on_message_callback.assert_not_called()

    async def test_download_file_to_path_uses_encrypt_query_param_and_image_aeskey(self):
        bot = self._make_bot()

        file_info = {
            "name": "wechat_image.jpg",
            "url": "encrypted-param",
            "cdn_info": {"encrypt_query_param": "encrypted-param"},
            "wechat_item": {"aeskey": "00112233445566778899aabbccddeeff"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = str(Path(tmpdir) / "wechat-test-image.bin")
            with patch(
                "modules.im.wechat._wechat_cdn_mod.download_and_decrypt", new=AsyncMock(return_value=b"img")
            ) as mock_dl:
                result = await bot.download_file_to_path(file_info, target_path)

        self.assertTrue(result.success)
        mock_dl.assert_awaited_once()
        args = mock_dl.await_args.args  # type: ignore[union-attr]
        self.assertEqual(args[0], "https://novac2c.cdn.weixin.qq.com/c2c")
        self.assertEqual(args[1], "encrypted-param")
        self.assertTrue(isinstance(args[2], str) and len(args[2]) > 0)

    async def test_download_file_to_path_uses_voice_media_aes_key(self):
        bot = self._make_bot()

        file_info = {
            "name": "wechat_voice.silk",
            "url": "voice-param",
            "cdn_info": {"encrypt_query_param": "voice-param", "aes_key": "dm9pY2Uta2V5"},
            "wechat_item": {},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = str(Path(tmpdir) / "wechat-test-voice.bin")
            with patch(
                "modules.im.wechat._wechat_cdn_mod.download_and_decrypt", new=AsyncMock(return_value=b"voice")
            ) as mock_dl:
                result = await bot.download_file_to_path(file_info, target_path)

        self.assertTrue(result.success)
        mock_dl.assert_awaited_once_with(
            "https://novac2c.cdn.weixin.qq.com/c2c",
            "voice-param",
            "dm9pY2Uta2V5",
        )

    async def test_upload_image_from_path_uses_cdn_workflow(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "photo.png"
            image_path.write_bytes(b"png")
            cdn_meta = {"encrypt_query_param": "p", "aes_key": "k", "file_size": 3, "file_size_ciphertext": 16}
            with patch(
                "modules.im.wechat.wechat_cdn.upload_image_to_cdn", new=AsyncMock(return_value=cdn_meta)
            ) as mock_upload:
                with patch("modules.im.wechat.wechat_api.send_message", new=AsyncMock(return_value={})) as mock_send:
                    result = await bot.upload_image_from_path(context, str(image_path))

        self.assertTrue(result.startswith("wc-img-"))
        mock_upload.assert_awaited_once()
        mock_send.assert_awaited_once()
        sent_items = mock_send.await_args.args[4]  # type: ignore[union-attr]
        self.assertEqual(sent_items[0]["image_item"]["media"]["encrypt_query_param"], "p")
        self.assertEqual(sent_items[0]["image_item"]["media"]["aes_key"], "k")
        self.assertEqual(sent_items[0]["image_item"]["media"]["encrypt_type"], 1)
        self.assertEqual(sent_items[0]["image_item"]["mid_size"], 16)

    async def test_send_message_accepts_empty_success_response(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with patch("modules.im.wechat.wechat_api.send_message", new=AsyncMock(return_value={})):
            result = await bot.send_message(context, "hello")

        self.assertTrue(result.startswith("wc-"))

    async def test_send_message_marks_session_expired_on_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"VIBE_REMOTE_HOME": tmpdir}):
                bot = self._make_bot()
                context = MessageContext(
                    user_id="user-1",
                    channel_id="user-1",
                    platform_specific={"context_token": "ctx-1"},
                )
                bot._auth_manager.is_logged_in = True
                bot._remember_context_token("user-1", "ctx-1")

                with patch(
                    "modules.im.wechat.wechat_api.send_message",
                    new=AsyncMock(return_value={"errcode": -14, "errmsg": "session timeout"}),
                ):
                    with self.assertRaises(RuntimeError):
                        await bot.send_message(context, "hello")

                cache_path = Path(tmpdir) / "state" / "wechat_context_tokens.json"
                data = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertFalse(bot._auth_manager.is_logged_in)
        self.assertEqual(data["tokens"], {})

    async def test_upload_file_from_path_uses_cdn_workflow(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.pdf"
            file_path.write_bytes(b"pdf")
            cdn_meta = {"encrypt_query_param": "p", "aes_key": "k", "file_size": 3}
            with patch(
                "modules.im.wechat.wechat_cdn.upload_file_to_cdn", new=AsyncMock(return_value=cdn_meta)
            ) as mock_upload:
                with patch("modules.im.wechat.wechat_api.send_message", new=AsyncMock(return_value={})) as mock_send:
                    result = await bot.upload_file_from_path(context, str(file_path), title="report.pdf")

        self.assertTrue(result.startswith("wc-file-"))
        mock_upload.assert_awaited_once()
        mock_send.assert_awaited_once()
        sent_items = mock_send.await_args.args[4]  # type: ignore[union-attr]
        self.assertEqual(sent_items[0]["file_item"]["media"]["encrypt_query_param"], "p")
        self.assertEqual(sent_items[0]["file_item"]["media"]["aes_key"], "k")
        self.assertEqual(sent_items[0]["file_item"]["media"]["encrypt_type"], 1)
        self.assertEqual(sent_items[0]["file_item"]["file_name"], "report.pdf")
        self.assertEqual(sent_items[0]["file_item"]["len"], "3")

    async def test_upload_file_from_path_routes_video_to_native_video_message(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "clip.mp4"
            file_path.write_bytes(b"mp4")
            cdn_meta = {"encrypt_query_param": "p", "aes_key": "k", "file_size_ciphertext": 12}
            with patch(
                "modules.im.wechat.wechat_cdn.upload_file_to_cdn", new=AsyncMock(return_value=cdn_meta)
            ) as mock_upload:
                with patch("modules.im.wechat.wechat_api.send_message", new=AsyncMock(return_value={})) as mock_send:
                    result = await bot.upload_file_from_path(context, str(file_path), title="clip.mp4")

        self.assertTrue(result.startswith("wc-video-"))
        sent_items = mock_send.await_args.args[4]  # type: ignore[union-attr]
        self.assertEqual(sent_items[0]["type"], 5)
        self.assertEqual(sent_items[0]["video_item"]["media"]["encrypt_query_param"], "p")
        self.assertEqual(sent_items[0]["video_item"]["video_size"], 12)
        self.assertEqual(mock_upload.await_args.kwargs["media_type"], 2)  # type: ignore[union-attr]

    async def test_upload_image_from_path_returns_empty_on_send_error_response(self):
        bot = self._make_bot()
        context = MessageContext(user_id="user-1", channel_id="user-1", platform_specific={"context_token": "ctx-1"})

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "photo.png"
            image_path.write_bytes(b"png")
            cdn_meta = {"encrypt_query_param": "p", "aes_key": "k", "file_size": 3, "file_size_ciphertext": 16}
            with patch("modules.im.wechat.wechat_cdn.upload_image_to_cdn", new=AsyncMock(return_value=cdn_meta)):
                with patch(
                    "modules.im.wechat.wechat_api.send_message",
                    new=AsyncMock(return_value={"ret": 500, "errmsg": "bad media"}),
                ):
                    result = await bot.upload_image_from_path(context, str(image_path))

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
