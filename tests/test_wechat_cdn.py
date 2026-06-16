import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.im import wechat_cdn


class WeChatCdnTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_cdn_upload_url_accepts_legacy_upload_param(self):
        url = wechat_cdn._resolve_cdn_upload_url(
            "https://novac2c.cdn.weixin.qq.com/c2c",
            {"upload_param": "abc+/="},
            "file-key",
            "upload_image_to_cdn",
        )

        self.assertEqual(
            url,
            "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=abc%2B/%3D&filekey=file-key",
        )

    def test_resolve_cdn_upload_url_accepts_upload_full_url(self):
        url = wechat_cdn._resolve_cdn_upload_url(
            "https://novac2c.cdn.weixin.qq.com/c2c",
            {
                "upload_full_url": (
                    "https://novac2c.cdn.weixin.qq.com/c2c/upload?"
                    "encrypted_query_param=abc&amp;filekey=file-key&amp;taskid=task-1"
                )
            },
            "ignored-file-key",
            "upload_image_to_cdn",
        )

        self.assertEqual(
            url,
            "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=abc&filekey=file-key&taskid=task-1",
        )

    def test_resolve_cdn_upload_url_accepts_camel_case_full_url(self):
        url = wechat_cdn._resolve_cdn_upload_url(
            "https://novac2c.cdn.weixin.qq.com/c2c",
            {
                "uploadFullUrl": (
                    "https://dynamic-cdn.weixin.qq.com/c2c/upload?"
                    "encrypted_query_param=abc&amp;filekey=file-key"
                )
            },
            "ignored-file-key",
            "upload_file_to_cdn",
        )

        self.assertEqual(
            url,
            "https://dynamic-cdn.weixin.qq.com/c2c/upload?encrypted_query_param=abc&filekey=file-key",
        )

    async def test_upload_image_to_cdn_uses_upload_full_url_response(self):
        with patch(
            "modules.im.wechat_cdn.get_upload_url",
            new=AsyncMock(
                return_value={
                    "upload_full_url": (
                        "https://novac2c.cdn.weixin.qq.com/c2c/upload?"
                        "encrypted_query_param=abc&filekey=file-key&taskid=task-1"
                    )
                }
            ),
        ):
            with patch(
                "modules.im.wechat_cdn.upload_buffer_to_cdn", new=AsyncMock(return_value="download-param")
            ) as mock_upload:
                with patch("os.urandom", side_effect=[bytes.fromhex("11" * 16), bytes.fromhex("22" * 16)]):
                    with patch.object(Path, "read_bytes", return_value=b"png"):
                        result = await wechat_cdn.upload_image_to_cdn(
                            base_url="https://ilinkai.weixin.qq.com",
                            token="token",
                            cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
                            to_user_id="user-1",
                            file_path="/tmp/photo.png",
                        )

        self.assertEqual(result["encrypt_query_param"], "download-param")
        self.assertEqual(result["filekey"], "11111111111111111111111111111111")
        self.assertEqual(
            mock_upload.await_args.kwargs["upload_url"],
            "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=abc&filekey=file-key&taskid=task-1",
        )

    async def test_upload_file_to_cdn_logs_legacy_fallback_host(self):
        with patch(
            "modules.im.wechat_cdn.get_upload_url",
            new=AsyncMock(return_value={"upload_param": "legacy-param"}),
        ) as mock_get:
            with patch(
                "modules.im.wechat_cdn.upload_buffer_to_cdn", new=AsyncMock(return_value="download-param")
            ) as mock_upload:
                with patch("os.urandom", side_effect=[bytes.fromhex("11" * 16), bytes.fromhex("22" * 16)]):
                    with patch.object(Path, "read_bytes", return_value=b"doc"):
                        with self.assertLogs("modules.im.wechat_cdn", level="INFO") as captured:
                            result = await wechat_cdn.upload_file_to_cdn(
                                base_url="https://ilinkai.weixin.qq.com",
                                token="token",
                                cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
                                to_user_id="user-1",
                                file_path="/tmp/report.xlsx",
                            )

        self.assertEqual(result["encrypt_query_param"], "download-param")
        params = mock_get.await_args.kwargs["params"]
        self.assertEqual(params["rawsize"], 3)
        self.assertEqual(params["filesize"], 16)
        self.assertEqual(
            mock_upload.await_args.kwargs["upload_url"],
            "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=legacy-param&filekey=11111111111111111111111111111111",
        )
        self.assertIn("falling back to configured CDN host=novac2c.cdn.weixin.qq.com", "\n".join(captured.output))

    async def test_upload_buffer_to_cdn_logs_5xx_body_and_host(self):
        class _Response:
            status = 500
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return "gateway rejected large body"

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                return _Response()

        with patch("modules.im.wechat_cdn.aiohttp.ClientSession", side_effect=lambda timeout: _Session()):
            with self.assertLogs("modules.im.wechat_cdn", level="WARNING") as captured:
                with self.assertRaises(RuntimeError):
                    await wechat_cdn.upload_buffer_to_cdn(
                        upload_url="https://dynamic-cdn.weixin.qq.com/c2c/upload?x=1",
                        data=b"xlsx-bytes",
                        aes_key=bytes.fromhex("22" * 16),
                    )

        logs = "\n".join(captured.output)
        self.assertIn("gateway rejected large body", logs)
        self.assertIn("host=dynamic-cdn.weixin.qq.com", logs)
        self.assertIn("raw_size=10", logs)
        self.assertIn("x=%3Credacted%3E", logs)
        self.assertNotIn("x=1", logs)

    async def test_upload_buffer_to_cdn_retries_5xx_body_containing_client_error(self):
        class _Response:
            status = 500
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self):
                return "upstream client error while reading large body"

        class _Session:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                self.calls += 1
                return _Response()

        session = _Session()
        with patch("modules.im.wechat_cdn.aiohttp.ClientSession", side_effect=lambda timeout: session):
            with self.assertRaises(RuntimeError):
                await wechat_cdn.upload_buffer_to_cdn(
                    upload_url=(
                        "https://dynamic-cdn.weixin.qq.com/c2c/upload?"
                        "encrypted_query_param=secret&filekey=file-key"
                    ),
                    data=b"xlsx-bytes",
                    aes_key=bytes.fromhex("22" * 16),
                )

        self.assertEqual(session.calls, 3)


if __name__ == "__main__":
    unittest.main()
