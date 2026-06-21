import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from config.v2_config import LarkConfig
from core.auth import AuthResult

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_opencode_utils_module() -> None:
    if "aiohttp" not in sys.modules:
        try:
            __import__("aiohttp")
        except ImportError:
            sys.modules["aiohttp"] = types.ModuleType("aiohttp")

    if "modules.agents.opencode.utils" in sys.modules:
        return

    if "modules.agents" not in sys.modules:
        agents_mod = types.ModuleType("modules.agents")
        agents_mod.__path__ = [str(ROOT / "modules" / "agents")]
        sys.modules["modules.agents"] = agents_mod
    if "modules.agents.opencode" not in sys.modules:
        opencode_mod = types.ModuleType("modules.agents.opencode")
        opencode_mod.__path__ = [str(ROOT / "modules" / "agents" / "opencode")]
        sys.modules["modules.agents.opencode"] = opencode_mod

    spec = importlib.util.spec_from_file_location(
        "modules.agents.opencode.utils",
        ROOT / "modules" / "agents" / "opencode" / "utils.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["modules.agents.opencode.utils"] = module
    spec.loader.exec_module(module)


_install_opencode_utils_module()

from modules.im.feishu import FeishuBot


class FeishuPostMessageTests(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self) -> FeishuBot:
        return FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret"))

    def test_extract_post_text_handles_language_wrapped_content(self):
        bot = self._make_bot()
        text = bot._extract_post_text(
            {
                "zh_cn": {
                    "title": "日报",
                    "content": [
                        [{"tag": "text", "text": "hello"}],
                        [{"tag": "img", "image_key": "img_123"}],
                    ],
                }
            }
        )

        self.assertEqual(text, "日报\nhello\n[image]")

    def test_extract_post_images_handles_language_wrapped_content(self):
        bot = self._make_bot()
        attachments = bot._extract_post_images(
            "om_123",
            {
                "zh_cn": {
                    "content": [
                        [{"tag": "img", "image_key": "img_123"}],
                        [{"tag": "text", "text": "hello"}],
                    ]
                }
            },
        )

        self.assertIsNotNone(attachments)
        assert attachments is not None
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].name, "img_123.image")
        self.assertEqual(
            attachments[0].url,
            "https://open.feishu.cn/open-apis/im/v1/messages/om_123/resources/img_123?type=image",
        )

    async def test_async_handle_message_keeps_text_and_images_for_wrapped_post(self):
        bot = self._make_bot()
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=True)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot.on_message_callback = AsyncMock()

        event_data = {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_user"},
            },
            "message": {
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_id": "om_123",
                "message_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "title": "日报",
                            "content": [
                                [{"tag": "text", "text": "hello"}],
                                [{"tag": "img", "image_key": "img_123"}],
                            ],
                        }
                    }
                ),
            },
        }

        await bot._async_handle_message(event_data)

        bot.on_message_callback.assert_awaited_once()
        args = bot.on_message_callback.await_args.args
        context, text = args
        self.assertEqual(text, "日报\nhello\n[image]")
        self.assertIsNotNone(context.files)
        assert context.files is not None
        self.assertEqual(len(context.files), 1)
        self.assertEqual(context.files[0].name, "img_123.image")

    async def test_active_thread_requires_fresh_mention_when_require_mention_enabled(self):
        bot = FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret", require_mention=True))
        bot._bot_open_id = "ou_bot"
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=False)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot.on_message_callback = AsyncMock()

        class _Sessions:
            def is_thread_active(self, _user_id, _chat_id, _root_id):
                return True

            def is_thread_active_for_user(self, _user_id, _chat_id, _root_id):
                return False

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _chat_id, global_default=False):
                return global_default

        bot.set_settings_manager(_SettingsManager())

        await bot._async_handle_message(
            {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_user"},
                },
                "message": {
                    "chat_id": "oc_chat",
                    "chat_type": "group",
                    "message_id": "om_child",
                    "root_id": "om_root",
                    "message_type": "text",
                    "content": json.dumps({"text": "@colleague take a look"}),
                    "mentions": [{"key": "@colleague", "id": {"open_id": "ou_colleague"}}],
                },
            }
        )

        bot.on_message_callback.assert_not_awaited()

    async def test_scheduled_active_thread_still_bypasses_mention_requirement(self):
        bot = FeishuBot(LarkConfig(app_id="app-id", app_secret="app-secret", require_mention=True))
        bot._bot_open_id = "ou_bot"
        bot.check_authorization = lambda **kwargs: AuthResult(allowed=True, is_dm=False)
        bot.dispatch_text_command = AsyncMock(return_value=False)
        bot.on_message_callback = AsyncMock()

        class _Sessions:
            def is_thread_active(self, _user_id, _chat_id, _root_id):
                return True

            def is_thread_active_for_user(self, user_id, chat_id, root_id):
                return user_id == "scheduled" and chat_id == "oc_chat" and root_id == "om_root"

        class _SettingsManager:
            sessions = _Sessions()

            def get_require_mention(self, _chat_id, global_default=False):
                return global_default

        bot.set_settings_manager(_SettingsManager())

        await bot._async_handle_message(
            {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_user"},
                },
                "message": {
                    "chat_id": "oc_chat",
                    "chat_type": "group",
                    "message_id": "om_child",
                    "root_id": "om_root",
                    "message_type": "text",
                    "content": json.dumps({"text": "scheduled follow-up context"}),
                    "mentions": [],
                },
            }
        )

        bot.on_message_callback.assert_awaited_once()
        self.assertEqual(bot.on_message_callback.await_args.args[1], "scheduled follow-up context")


if __name__ == "__main__":
    unittest.main()
