import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from config.v2_config import SlackConfig
from modules.im.slack import SlackBot


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
            received["is_ordinary_text"] = context.is_original_human_text

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
                "is_ordinary_text": True,
            },
        )
        slack.sessions.mark_thread_active.assert_called_once_with("U123", "C123", "1710000000.000700")

    async def test_app_mention_normalized_user_text_retains_shared_content(self):
        slack = SlackBot(SlackConfig(bot_token="xoxb-test"))
        received = {}

        async def _on_message(context, text):
            received["text"] = text
            received["normalized_user_text"] = (context.platform_specific or {}).get(
                "normalized_user_text"
            )

        slack.register_callbacks(on_message=_on_message)
        slack.settings_manager = object()
        slack.sessions = SimpleNamespace(mark_thread_active=Mock())
        slack._get_bot_user_id = AsyncMock(return_value="U_BOT")
        slack._extract_shared_message_content = AsyncMock(return_value="Forwarded details")
        payload = {
            "event_id": "evt-app-mention-shared",
            "team_id": "T1",
            "authorizations": [{"user_id": "U_BOT"}],
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@U_BOT>",
                "ts": "1710000000.000701",
            },
        }

        await slack._handle_event(payload)

        self.assertEqual(
            received,
            {
                "text": "<@U_BOT>\n\nForwarded details",
                "normalized_user_text": "Forwarded details",
            },
        )


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


if __name__ == "__main__":
    unittest.main()
