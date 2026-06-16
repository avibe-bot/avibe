import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.reply_enhancer import FileLink
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import media_objects


class _StubController:
    def __init__(self, *, platform="slack", language="en", public_url=""):
        self.config = SimpleNamespace(
            platform=platform,
            language=language,
            remote_access=SimpleNamespace(
                vibe_cloud=SimpleNamespace(
                    enabled=bool(public_url),
                    public_url=public_url,
                )
            ),
        )


class _StubIMClient:
    def __init__(self):
        self.file_uploads = []
        self.image_uploads = []
        self.video_uploads = []

    async def upload_file_from_path(self, context, file_path, title=None):
        self.file_uploads.append((context.channel_id, file_path, title))

    async def upload_image_from_path(self, context, file_path, title=None):
        self.image_uploads.append((context.channel_id, file_path, title))

    async def upload_video_from_path(self, context, file_path, title=None):
        self.video_uploads.append((context.channel_id, file_path, title))


class _FailingWechatIMClient(_StubIMClient):
    def __init__(self):
        super().__init__()
        self.sent_messages = []

    async def upload_file_from_path(self, context, file_path, title=None):
        self.file_uploads.append((context.channel_id, file_path, title))
        return ""

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append((context.channel_id, text, parse_mode))
        return "notice-1"


class _SuccessfulWechatIMClient(_FailingWechatIMClient):
    async def upload_file_from_path(self, context, file_path, title=None):
        self.file_uploads.append((context.channel_id, file_path, title))
        return "wc-file-1"


class MessageDispatcherFileUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_file_link_allows_path_outside_cwd(self):
        dispatcher = ConsolidatedMessageDispatcher(_StubController())
        im_client = _StubIMClient()
        context = MessageContext(user_id="U1", channel_id="C1")

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.txt"
            file_path.write_text("hello", encoding="utf-8")
            resolved_path = str(file_path.resolve())

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="report", path=str(file_path))],
            )

        self.assertEqual(im_client.file_uploads, [("C1", resolved_path, "report.txt")])
        self.assertEqual(im_client.image_uploads, [])

    async def test_upload_image_link_allows_path_outside_cwd(self):
        dispatcher = ConsolidatedMessageDispatcher(_StubController())
        im_client = _StubIMClient()
        context = MessageContext(user_id="U1", channel_id="C1")

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "screenshot.png"
            image_path.write_bytes(b"png")
            resolved_path = str(image_path.resolve())

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="preview", path=str(image_path), is_image=True)],
            )

        self.assertEqual(im_client.file_uploads, [])
        self.assertEqual(im_client.image_uploads, [("C1", resolved_path, "preview.png")])
        self.assertEqual(im_client.video_uploads, [])

    async def test_upload_video_link_uses_video_channel_even_for_image_syntax(self):
        dispatcher = ConsolidatedMessageDispatcher(_StubController())
        im_client = _StubIMClient()
        context = MessageContext(user_id="U1", channel_id="C1")

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "clip.mp4"
            video_path.write_bytes(b"mp4")
            resolved_path = str(video_path.resolve())

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="preview", path=str(video_path), is_image=True)],
            )

        self.assertEqual(im_client.file_uploads, [])
        self.assertEqual(im_client.image_uploads, [])
        self.assertEqual(im_client.video_uploads, [("C1", resolved_path, "preview.mp4")])

    async def test_wechat_failed_file_upload_sends_public_download_link(self):
        ensure_sqlite_state()
        dispatcher = ConsolidatedMessageDispatcher(
            _StubController(platform="wechat", language="en", public_url="https://alex.avibe.bot")
        )
        im_client = _FailingWechatIMClient()
        context = MessageContext(
            user_id="wx-user",
            channel_id="wx-user",
            platform="wechat",
            platform_specific={"agent_session_id": "ses_1"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.xlsx"
            file_path.write_bytes(b"xlsx")
            resolved_path = str(file_path.resolve())

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="report", path=str(file_path))],
            )

        self.assertEqual(im_client.file_uploads, [("wx-user", resolved_path, "report.xlsx")])
        self.assertEqual(len(im_client.sent_messages), 1)
        _, notice, parse_mode = im_client.sent_messages[0]
        self.assertEqual(parse_mode, "plain")
        self.assertIn("could not deliver report.xlsx as a WeChat file", notice)
        self.assertIn("after signing in if prompted", notice)
        self.assertIn("https://alex.avibe.bot/api/media/", notice)
        self.assertIn("?download=1", notice)

        engine = create_sqlite_engine()
        with engine.connect() as conn:
            rows = conn.execute(media_objects.select()).mappings().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["local_path"], resolved_path)
        self.assertEqual(rows[0]["file_name"], "report.xlsx")

    async def test_wechat_successful_file_upload_does_not_send_failure_notice(self):
        ensure_sqlite_state()
        dispatcher = ConsolidatedMessageDispatcher(
            _StubController(platform="wechat", language="en", public_url="https://alex.avibe.bot")
        )
        im_client = _SuccessfulWechatIMClient()
        context = MessageContext(user_id="wx-user", channel_id="wx-user", platform="wechat")

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.xlsx"
            file_path.write_bytes(b"xlsx")
            resolved_path = str(file_path.resolve())

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="report", path=str(file_path))],
            )

        self.assertEqual(im_client.file_uploads, [("wx-user", resolved_path, "report.xlsx")])
        self.assertEqual(im_client.sent_messages, [])

        engine = create_sqlite_engine()
        with engine.connect() as conn:
            rows = conn.execute(media_objects.select()).mappings().all()
        self.assertEqual(rows, [])

    async def test_wechat_failed_file_upload_without_public_url_sends_clear_notice(self):
        dispatcher = ConsolidatedMessageDispatcher(_StubController(platform="wechat", language="en"))
        im_client = _FailingWechatIMClient()
        context = MessageContext(user_id="wx-user", channel_id="wx-user", platform="wechat")

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "report.xlsx"
            file_path.write_bytes(b"xlsx")

            await dispatcher._upload_file_links(
                im_client,
                context,
                [FileLink(label="report", path=str(file_path))],
            )

        self.assertEqual(len(im_client.sent_messages), 1)
        notice = im_client.sent_messages[0][1]
        self.assertIn("could not deliver report.xlsx as a WeChat file", notice)
        self.assertNotIn("/api/media/", notice)


if __name__ == "__main__":
    unittest.main()
