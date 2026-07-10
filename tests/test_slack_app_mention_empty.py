import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from config.v2_config import SlackConfig


def _install_slack_stubs() -> None:
    if "aiohttp" not in sys.modules:
        aiohttp_mod = types.ModuleType("aiohttp")

        class _ClientWebSocketResponse:
            closed = False

        class _ClientSession:
            async def close(self):
                return None

        class _ClientTimeout:
            def __init__(self, *args, **kwargs):
                pass

        aiohttp_mod.ClientWebSocketResponse = _ClientWebSocketResponse
        aiohttp_mod.ClientSession = _ClientSession
        aiohttp_mod.ClientTimeout = _ClientTimeout
        sys.modules["aiohttp"] = aiohttp_mod

    if "markdown_to_mrkdwn" not in sys.modules:
        markdown_mod = types.ModuleType("markdown_to_mrkdwn")

        class _SlackMarkdownConverter:
            def convert(self, text):
                return text

        markdown_mod.SlackMarkdownConverter = _SlackMarkdownConverter
        sys.modules["markdown_to_mrkdwn"] = markdown_mod

    if "slack_sdk" not in sys.modules:
        slack_sdk = types.ModuleType("slack_sdk")
        web_mod = types.ModuleType("slack_sdk.web")
        web_async_mod = types.ModuleType("slack_sdk.web.async_client")
        socket_mode_mod = types.ModuleType("slack_sdk.socket_mode")
        socket_mode_aiohttp_mod = types.ModuleType("slack_sdk.socket_mode.aiohttp")
        socket_mode_request_mod = types.ModuleType("slack_sdk.socket_mode.request")
        socket_mode_response_mod = types.ModuleType("slack_sdk.socket_mode.response")
        errors_mod = types.ModuleType("slack_sdk.errors")

        class _AsyncWebClient:
            def __init__(self, *args, **kwargs):
                pass

            async def auth_test(self):
                return {"user_id": "U_BOT"}

        class _SocketModeClient:
            def __init__(self, *args, **kwargs):
                pass

        class _SocketModeRequest:
            pass

        class _SocketModeResponse:
            def __init__(self, *args, **kwargs):
                pass

        class _SlackApiError(Exception):
            def __init__(self, message="", response=None):
                super().__init__(message)
                self.response = response

        web_async_mod.AsyncWebClient = _AsyncWebClient
        socket_mode_aiohttp_mod.SocketModeClient = _SocketModeClient
        socket_mode_request_mod.SocketModeRequest = _SocketModeRequest
        socket_mode_response_mod.SocketModeResponse = _SocketModeResponse
        errors_mod.SlackApiError = _SlackApiError

        sys.modules["slack_sdk"] = slack_sdk
        sys.modules["slack_sdk.web"] = web_mod
        sys.modules["slack_sdk.web.async_client"] = web_async_mod
        sys.modules["slack_sdk.socket_mode"] = socket_mode_mod
        sys.modules["slack_sdk.socket_mode.aiohttp"] = socket_mode_aiohttp_mod
        sys.modules["slack_sdk.socket_mode.request"] = socket_mode_request_mod
        sys.modules["slack_sdk.socket_mode.response"] = socket_mode_response_mod
        sys.modules["slack_sdk.errors"] = errors_mod


def _load_local_slack_bot():
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    sys.modules.pop("modules.im.slack", None)
    spec = importlib.util.spec_from_file_location("modules.im.slack", repo_root / "modules" / "im" / "slack.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["modules.im.slack"] = module
    spec.loader.exec_module(module)
    return module.SlackBot


_install_slack_stubs()
SlackBot = _load_local_slack_bot()


class _ResponseLike:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class SlackAppMentionEmptyTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_app_mention_dispatches_empty_message_for_start_menu(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(context, text):
            received["channel_id"] = context.channel_id
            received["thread_id"] = context.thread_id
            received["text"] = text
            received["control_text"] = (context.platform_specific or {}).get("control_text")

        slack.register_callbacks(on_message=_on_message)
        slack.settings_manager = object()
        slack.sessions = SimpleNamespace(mark_thread_active=Mock())
        slack._get_bot_user_id = AsyncMock(return_value="U_BOT")
        slack._extract_shared_message_content = AsyncMock(return_value=None)

        payload = {
            "event_id": "evt-app-mention-empty",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@U_BOT> \n \t",
                "ts": "1710000000.000700",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(
            received,
            {
                "channel_id": "C123",
                "thread_id": "1710000000.000700",
                "text": "",
                "control_text": "",
            },
        )
        slack.sessions.mark_thread_active.assert_called_once_with("U123", "C123", "1710000000.000700")


class SlackFileAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_file_attachments_preserves_file_id_when_url_missing(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))

        attachments = slack._extract_file_attachments([{"id": "F123", "mimetype": "application/pdf"}])

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].name, "F123")
        self.assertIsNone(attachments[0].url)
        self.assertEqual(attachments[0].__dict__["slack_file_id"], "F123")

    async def test_resolve_downloadable_file_info_hydrates_thin_file_event(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        slack.web_client = SimpleNamespace(
            files_info=AsyncMock(
                return_value=_ResponseLike({
                    "file": {
                        "id": "F123",
                        "name": "report.pdf",
                        "url_private_download": "https://files.slack.test/report.pdf",
                    }
                })
            )
        )

        resolved = await slack._resolve_downloadable_file_info({"slack_file_id": "F123", "name": "F123"})

        self.assertEqual(resolved["name"], "report.pdf")
        self.assertEqual(resolved["url_private_download"], "https://files.slack.test/report.pdf")
        slack.web_client.files_info.assert_awaited_once_with(file="F123")


class SlackRoutingModalTests(unittest.TestCase):
    @staticmethod
    def _find_select(view, block_id):
        for block in view["blocks"]:
            if block.get("block_id") == block_id:
                return block["element"]
        raise AssertionError(f"block {block_id} not found")

    def test_backend_switch_does_not_reuse_other_backend_canonical_override(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        current_routing = SimpleNamespace(
            agent_backend="codex",
            model="gpt-5.4",
            reasoning_effort="high",
            opencode_agent=None,
            opencode_model=None,
            opencode_reasoning_effort=None,
            claude_agent=None,
            claude_model=None,
            claude_reasoning_effort=None,
            codex_agent=None,
            codex_model=None,
            codex_reasoning_effort=None,
        )

        view = slack._build_routing_modal_view(
            channel_id="C123",
            registered_backends=["opencode", "codex"],
            current_backend="codex",
            current_routing=current_routing,
            opencode_agents=[],
            opencode_models={"openai": {"models": [{"id": "gpt-5.4", "name": "gpt-5.4"}]}},
            opencode_default_config={},
            codex_models=["gpt-5.4"],
            selected_backend="opencode",
        )

        model_select = self._find_select(view, "opencode_model_block")
        reasoning_select = self._find_select(view, "opencode_reasoning_block")
        self.assertEqual(model_select["initial_option"]["value"], "__default__")
        self.assertEqual(reasoning_select["initial_option"]["value"], "__default__")

    def test_codex_reasoning_uses_shared_catalog_options(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        current_routing = SimpleNamespace(
            agent_backend="codex",
            model="gpt-5.6-terra",
            reasoning_effort=None,
            codex_agent=None,
            codex_model=None,
            codex_reasoning_effort=None,
        )

        view = slack._build_routing_modal_view(
            channel_id="C123",
            registered_backends=["codex"],
            current_backend="codex",
            current_routing=current_routing,
            opencode_agents=[],
            opencode_models={},
            opencode_default_config={},
            codex_models=["gpt-5.6-terra"],
            backend_reasoning_options={
                "codex": {
                    "gpt-5.6-terra": [
                        {"value": "__default__", "label": "(Default)"},
                        {"value": "ultra", "label": "Ultra"},
                    ]
                }
            },
        )

        reasoning_select = self._find_select(view, "codex_reasoning_block")
        self.assertEqual(
            [option["value"] for option in reasoning_select["options"]],
            ["__default__", "ultra"],
        )


if __name__ == "__main__":
    unittest.main()
