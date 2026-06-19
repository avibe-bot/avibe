import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_settings import SettingsStore
from modules.im.wechat_auth import WeChatAuthManager
from vibe import api as vibe_api


class WeChatAuthManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_login_persists_base_url_on_session(self):
        manager = WeChatAuthManager()

        with patch(
            "modules.im.wechat_auth.get_bot_qrcode",
            new=AsyncMock(return_value={"qrcode": "qr-token", "qrcode_img_content": "https://example.com/qr.png"}),
        ):
            result = await manager.start_login(
                base_url="https://wechat.example.com",
                local_token_list=[" saved-token ", "", "second-token"],
            )

        session = manager.get_session(result["session_key"])
        self.assertIsNotNone(session)
        self.assertEqual(session.base_url, "https://wechat.example.com")
        self.assertEqual(session.local_token_list, ["saved-token", "second-token"])

    async def test_poll_status_returns_expired_when_session_missing(self):
        manager = WeChatAuthManager()

        result = await manager.poll_status("missing-session")

        self.assertEqual(result["status"], "expired")
        self.assertIn("start a new login", result["message"].lower())

    async def test_start_login_returns_error_payload_when_qr_fetch_fails(self):
        manager = WeChatAuthManager()

        with patch(
            "modules.im.wechat_auth.get_bot_qrcode",
            new=AsyncMock(side_effect=RuntimeError("upstream unavailable")),
        ):
            result = await manager.start_login(base_url="https://wechat.example.com")

        self.assertFalse(result["ok"])
        self.assertIn("Failed to start login", result["error"])
        self.assertIsNone(manager.get_session(result["session_key"]))

    async def test_wait_for_login_returns_immediately_when_session_missing(self):
        manager = WeChatAuthManager()

        result = await manager.wait_for_login("missing-session", timeout_s=5)

        self.assertEqual(result["status"], "expired")
        self.assertIn("start a new login", result["message"].lower())

    async def test_poll_status_follows_scan_redirect_and_preserves_session(self):
        manager = WeChatAuthManager()

        with patch(
            "modules.im.wechat_auth.get_bot_qrcode",
            new=AsyncMock(return_value={"qrcode": "qr-token", "qrcode_img_content": "https://example.com/qr.png"}),
        ):
            started = await manager.start_login(session_key="qr-session", base_url="https://ilinkai.weixin.qq.com")

        with patch(
            "modules.im.wechat_auth.get_qrcode_status",
            new=AsyncMock(return_value={"status": "scaned_but_redirect", "redirect_host": "redirect.weixin.qq.com"}),
        ) as poll:
            result = await manager.poll_status(started["session_key"])

        self.assertEqual(result["status"], "scaned")
        self.assertEqual(result["base_url"], "https://redirect.weixin.qq.com")
        session = manager.get_session("qr-session")
        self.assertIsNotNone(session)
        self.assertEqual(session.base_url, "https://ilinkai.weixin.qq.com")
        self.assertEqual(session.current_base_url, "https://redirect.weixin.qq.com")
        poll.assert_awaited_once_with("https://ilinkai.weixin.qq.com", "qr-token", verify_code=None)

        with patch(
            "modules.im.wechat_auth.get_qrcode_status",
            new=AsyncMock(
                return_value={
                    "status": "confirmed",
                    "bot_token": "token-1",
                    "ilink_bot_id": "bot-1",
                    "ilink_user_id": "wx-user",
                }
            ),
        ) as redirected_poll:
            confirmed = await manager.poll_status("qr-session")

        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["base_url"], "https://redirect.weixin.qq.com")
        redirected_poll.assert_awaited_once_with("https://redirect.weixin.qq.com", "qr-token", verify_code=None)

    async def test_poll_status_returns_already_connected_for_binded_redirect(self):
        manager = WeChatAuthManager()

        with patch(
            "modules.im.wechat_auth.get_bot_qrcode",
            new=AsyncMock(return_value={"qrcode": "qr-token", "qrcode_img_content": "https://example.com/qr.png"}),
        ):
            started = await manager.start_login(session_key="qr-session", base_url="https://ilinkai.weixin.qq.com")

        with patch(
            "modules.im.wechat_auth.get_qrcode_status",
            new=AsyncMock(return_value={"status": "binded_redirect"}),
        ):
            result = await manager.poll_status(started["session_key"])

        self.assertEqual(result["status"], "already_connected")
        self.assertEqual(result["base_url"], "https://ilinkai.weixin.qq.com")
        self.assertIn("already connected", result["message"].lower())
        self.assertIsNone(manager.get_session("qr-session"))

    def test_wechat_config_handles_already_connected_status(self):
        source = Path("ui/src/components/steps/WeChatConfig.tsx").read_text(encoding="utf-8")

        self.assertIn("status === 'already_connected'", source)
        self.assertIn("setLoginState('connected')", source)
        self.assertIn("preserveExistingConnectionFields(result)", source)
        self.assertIn("base_url: baseUrl || data.wechat?.base_url || ''", source)
        self.assertIn("result.status === 'refreshed'", source)
        self.assertIn("setQrCodeUrl(result.qrcode_url || '')", source)

    def test_wechat_config_does_not_render_idle_start_for_saved_token(self):
        source = Path("ui/src/components/steps/WeChatConfig.tsx").read_text(encoding="utf-8")

        self.assertIn("const hasSavedBotToken = hasUsableSecret(data.wechat, 'bot_token', botToken);", source)
        self.assertIn("const isAlreadyBound = loginState === 'idle' && !botToken && hasSavedBotToken;", source)
        self.assertIn("loginState === 'idle' && !botToken && !isAlreadyBound", source)
        self.assertIn("if (!autoStartLogin) return;", source)

        settings_source = Path("ui/src/components/settings/SettingsPlatformsPage.tsx").read_text(encoding="utf-8")
        self.assertIn("autoStartLogin={false}", settings_source)

    async def test_poll_status_refresh_preserves_local_token_list(self):
        manager = WeChatAuthManager()
        qr_fetch = AsyncMock(
            side_effect=[
                {"qrcode": "qr-token-1", "qrcode_img_content": "https://example.com/qr-1.png"},
                {"qrcode": "qr-token-2", "qrcode_img_content": "https://example.com/qr-2.png"},
            ]
        )

        with patch("modules.im.wechat_auth.get_bot_qrcode", new=qr_fetch):
            started = await manager.start_login(
                session_key="qr-session",
                base_url="https://ilinkai.weixin.qq.com",
                local_token_list=["saved-token"],
            )

            with patch(
                "modules.im.wechat_auth.get_qrcode_status",
                new=AsyncMock(return_value={"status": "expired"}),
            ):
                refreshed = await manager.poll_status(started["session_key"])

        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(refreshed["qrcode_url"], "https://example.com/qr-2.png")
        self.assertEqual(qr_fetch.await_count, 2)
        self.assertEqual(qr_fetch.await_args_list[0].kwargs["local_token_list"], ["saved-token"])
        self.assertEqual(qr_fetch.await_args_list[1].kwargs["local_token_list"], ["saved-token"])
        session = manager.get_session("qr-session")
        self.assertIsNotNone(session)
        self.assertEqual(session.qrcode, "qr-token-2")

    async def test_poll_status_refresh_uses_login_host_after_status_redirect(self):
        manager = WeChatAuthManager()
        qr_fetch = AsyncMock(
            side_effect=[
                {"qrcode": "qr-token-1", "qrcode_img_content": "https://example.com/qr-1.png"},
                {"qrcode": "qr-token-2", "qrcode_img_content": "https://example.com/qr-2.png"},
            ]
        )

        with patch("modules.im.wechat_auth.get_bot_qrcode", new=qr_fetch):
            started = await manager.start_login(
                session_key="qr-session",
                base_url="https://ilinkai.weixin.qq.com",
                local_token_list=["saved-token"],
            )
            with patch(
                "modules.im.wechat_auth.get_qrcode_status",
                new=AsyncMock(return_value={"status": "scaned_but_redirect", "redirect_host": "redirect.weixin.qq.com"}),
            ):
                await manager.poll_status(started["session_key"])

            with patch(
                "modules.im.wechat_auth.get_qrcode_status",
                new=AsyncMock(return_value={"status": "expired"}),
            ):
                refreshed = await manager.poll_status(started["session_key"])

        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(qr_fetch.await_args_list[1].args[0], "https://ilinkai.weixin.qq.com")
        session = manager.get_session("qr-session")
        self.assertIsNotNone(session)
        self.assertEqual(session.current_base_url, "https://ilinkai.weixin.qq.com")

    async def test_poll_status_refreshes_after_blocked_verify_code(self):
        manager = WeChatAuthManager()
        qr_fetch = AsyncMock(
            side_effect=[
                {"qrcode": "qr-token-1", "qrcode_img_content": "https://example.com/qr-1.png"},
                {"qrcode": "qr-token-2", "qrcode_img_content": "https://example.com/qr-2.png"},
            ]
        )

        with patch("modules.im.wechat_auth.get_bot_qrcode", new=qr_fetch):
            started = await manager.start_login(session_key="qr-session", base_url="https://ilinkai.weixin.qq.com")
            with patch(
                "modules.im.wechat_auth.get_qrcode_status",
                new=AsyncMock(return_value={"status": "verify_code_blocked"}),
            ):
                refreshed = await manager.poll_status(started["session_key"], verify_code="bad-code")

        self.assertEqual(refreshed["status"], "refreshed")
        self.assertEqual(refreshed["qrcode_url"], "https://example.com/qr-2.png")
        self.assertIsNotNone(manager.get_session("qr-session"))

    async def test_poll_status_submits_verify_code_and_confirms(self):
        manager = WeChatAuthManager()

        with patch(
            "modules.im.wechat_auth.get_bot_qrcode",
            new=AsyncMock(return_value={"qrcode": "qr-token", "qrcode_img_content": "https://example.com/qr.png"}),
        ):
            started = await manager.start_login(session_key="qr-session", base_url="https://ilinkai.weixin.qq.com")

        with patch(
            "modules.im.wechat_auth.get_qrcode_status",
            new=AsyncMock(return_value={"status": "need_verifycode"}),
        ) as first_poll:
            result = await manager.poll_status(started["session_key"])

        self.assertEqual(result["status"], "need_verifycode")
        first_poll.assert_awaited_once_with("https://ilinkai.weixin.qq.com", "qr-token", verify_code=None)

        with patch(
            "modules.im.wechat_auth.get_qrcode_status",
            new=AsyncMock(
                return_value={
                    "status": "confirmed",
                    "bot_token": "token-1",
                    "ilink_bot_id": "bot-1",
                    "baseurl": "https://wechat.example.com",
                    "ilink_user_id": "wx-user",
                }
            ),
        ) as verify_poll:
            confirmed = await manager.poll_status("qr-session", verify_code="1234")

        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["bot_token"], "token-1")
        verify_poll.assert_awaited_once_with("https://ilinkai.weixin.qq.com", "qr-token", verify_code="1234")

    async def test_auto_bind_wechat_user_marks_one_time_menu_hint_pending(self):
        SettingsStore.reset_instance()

        with patch("vibe.api.load_config") as load_config:
            load_config.return_value.runtime.default_cwd = "/tmp/vibe"
            load_config.return_value.agents.default_backend = "opencode"
            result = vibe_api.auto_bind_wechat_user("wx-user")

        self.assertTrue(result["ok"])
        self.assertFalse(result["already_bound"])
        self.assertTrue(result["pending_bind_menu_hint"])

        user = SettingsStore.get_instance().get_user("wx-user", platform="wechat")
        self.assertIsNotNone(user)
        self.assertTrue(user.pending_bind_menu_hint)  # type: ignore[union-attr]

    async def test_auto_bind_wechat_user_rearms_existing_user_hint(self):
        SettingsStore.reset_instance()
        store = SettingsStore.get_instance()
        store.add_user("wx-user", "WeChat User", platform="wechat")

        result = vibe_api.auto_bind_wechat_user("wx-user")

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_bound"])
        self.assertTrue(result["pending_bind_menu_hint"])

        user = SettingsStore.get_instance().get_user("wx-user", platform="wechat")
        self.assertIsNotNone(user)
        self.assertTrue(user.pending_bind_menu_hint)  # type: ignore[union-attr]
