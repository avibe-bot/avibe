import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.v2_config import AudioAsrConfig, RemoteAccessConfig, VibeCloudRemoteAccessConfig
from core.audio_asr import (
    AudioAsrEmptyTranscriptError,
    AudioAsrProtocolError,
    AudioAsrService,
    AudioAsrTimeoutError,
    AudioAsrUnavailableError,
    AudioTranscript,
    append_audio_transcripts_to_message,
    format_audio_transcript_echo,
)
from modules.im import MessageContext
from modules.im.base import FileAttachment, FileDownloadResult

from tests.test_message_handler_typing import MessageHandler, _StubController


class AudioAsrServiceTests(unittest.TestCase):
    def test_audio_detection_skips_wechat_silk(self):
        service = AudioAsrService(SimpleNamespace(audio_asr=AudioAsrConfig()))

        self.assertTrue(service.is_audio_attachment(FileAttachment(name="voice.m4a", mimetype="audio/mp4")))
        self.assertTrue(service.is_audio_attachment(FileAttachment(name="voice.ogg", mimetype="application/octet-stream")))
        self.assertFalse(service.is_audio_attachment(FileAttachment(name="wechat_voice.silk", mimetype="audio/silk")))
        self.assertFalse(service.is_audio_attachment(FileAttachment(name="report.pdf", mimetype="application/pdf")))

    def test_audio_detection_uses_local_m4a_signature_when_metadata_is_generic(self):
        service = AudioAsrService(SimpleNamespace(audio_asr=AudioAsrConfig()))
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "F0B50NK1CS2"
            audio_path.write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom")

            self.assertTrue(
                service.is_audio_attachment(
                    FileAttachment(
                        name="F0B50NK1CS2",
                        mimetype="application/octet-stream",
                        local_path=str(audio_path),
                    )
                )
            )

    def test_requires_enabled_vibe_cloud_pairing(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=False,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )

        self.assertFalse(service.is_available())

    def test_transcript_blocks(self):
        transcripts = [
            AudioTranscript(
                attachment_name="voice.m4a",
                local_path="/tmp/voice.m4a",
                text="hello world",
            )
        ]

        self.assertEqual(
            append_audio_transcripts_to_message("please handle", transcripts),
            "please handle\n\n[Audio Transcripts]\n- voice.m4a: hello world",
        )
        self.assertEqual(
            format_audio_transcript_echo(
                transcripts,
                single_label="Voice transcript:",
                multiple_label="Voice transcripts:",
            ),
            "Voice transcript:\nhello world",
        )

    def test_http_callers_can_preserve_timeout_classification(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.webm"
            audio_path.write_bytes(b"audio")
            attachment = FileAttachment(
                name="voice.webm",
                mimetype="audio/webm",
                local_path=str(audio_path),
                size=5,
            )
            timeout = AsyncMock(side_effect=AudioAsrTimeoutError("timed out"))
            with patch.object(service, "_transcribe_one", timeout):
                with self.assertRaises(AudioAsrTimeoutError):
                    asyncio.run(
                        service.transcribe_attachments(
                            [attachment],
                            raise_on_timeout=True,
                        )
                    )
                self.assertEqual(
                    asyncio.run(service.transcribe_attachments([attachment])),
                    [],
                )

    def test_empty_upstream_transcript_has_a_distinct_classification(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )

        class EmptyResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"text": "  "}

        class EmptySession:
            def post(self, *_args, **_kwargs):
                return EmptyResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            attachment = FileAttachment(
                name="voice.wav",
                mimetype="audio/wav",
                local_path=str(audio_path),
                size=12,
            )

            with self.assertRaises(AudioAsrEmptyTranscriptError):
                asyncio.run(
                    service._transcribe_one(
                        EmptySession(),
                        service._runtime_config(),
                        attachment,
                        10**12,
                    )
                )

            empty = AsyncMock(side_effect=AudioAsrEmptyTranscriptError("empty"))
            with patch.object(service, "_transcribe_one", empty):
                with self.assertRaises(AudioAsrEmptyTranscriptError):
                    asyncio.run(
                        service.transcribe_attachments(
                            [attachment],
                            raise_on_empty=True,
                        )
                    )
                self.assertEqual(
                    asyncio.run(service.transcribe_attachments([attachment])),
                    [],
                )

    def test_success_payload_requires_string_and_preserves_whitespace(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )

        class MalformedResponse:
            status = 200

            def __init__(self, payload=None, *, invalid_json=False):
                self.payload = payload
                self.invalid_json = invalid_json

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                if self.invalid_json:
                    raise ValueError("invalid JSON")
                return self.payload

            async def text(self):
                return "{"

        class MalformedSession:
            def __init__(self, response):
                self.response = response

            def post(self, *_args, **_kwargs):
                return self.response

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            attachment = FileAttachment(
                name="voice.wav",
                mimetype="audio/wav",
                local_path=str(audio_path),
                size=12,
            )

            malformed_responses = [
                MalformedResponse(invalid_json=True),
                MalformedResponse({}),
                MalformedResponse({"text": 123}),
            ]
            for response in malformed_responses:
                with self.subTest(response=response):
                    with self.assertRaises(AudioAsrProtocolError):
                        asyncio.run(
                            service._transcribe_one(
                                MalformedSession(response),
                                service._runtime_config(),
                                attachment,
                                10**12,
                            )
                        )

            transcript = asyncio.run(
                service._transcribe_one(
                    MalformedSession(MalformedResponse({"text": "first paragraph\n\n"})),
                    service._runtime_config(),
                    attachment,
                    10**12,
                )
            )
            self.assertEqual(transcript.text, "first paragraph\n\n")

    def test_provider_outage_has_an_opt_in_availability_classification(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )

        class UnavailableResponse:
            status = 503

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"error": "asr_unavailable"}

        class UnavailableSession:
            def post(self, *_args, **_kwargs):
                return UnavailableResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            attachment = FileAttachment(
                name="voice.wav",
                mimetype="audio/wav",
                local_path=str(audio_path),
                size=12,
            )

            with self.assertRaises(AudioAsrUnavailableError):
                asyncio.run(
                    service._transcribe_one(
                        UnavailableSession(),
                        service._runtime_config(),
                        attachment,
                        10**12,
                    )
                )

            unavailable = AsyncMock(
                side_effect=AudioAsrUnavailableError("unavailable"),
            )
            with patch.object(service, "_transcribe_one", unavailable):
                with self.assertRaises(AudioAsrUnavailableError):
                    asyncio.run(
                        service.transcribe_attachments(
                            [attachment],
                            raise_on_unavailable=True,
                        )
                    )
                self.assertEqual(
                    asyncio.run(service.transcribe_attachments([attachment])),
                    [],
                )

    def test_dictation_classifies_non_json_proxy_errors_by_status(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )

        class ErrorResponse:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                raise ValueError("proxy returned HTML")

        class ErrorSession:
            def __init__(self, status):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, *_args, **_kwargs):
                return ErrorResponse(self.status)

        cases = [
            (503, AudioAsrUnavailableError),
            (504, AudioAsrTimeoutError),
        ]
        for status, expected_error in cases:
            with self.subTest(status=status):
                with patch(
                    "core.audio_asr.aiohttp.ClientSession",
                    return_value=ErrorSession(status),
                ):
                    with self.assertRaises(expected_error):
                        asyncio.run(
                            service.transcribe_voice_segment(
                                None,
                                dictation_id="dictation-1",
                                sequence=1,
                                overlap_ms=0,
                                final=True,
                                finalize_only=True,
                                receipts=["receipt-0"],
                                before="",
                                after="",
                            )
                        )

    def test_http_callers_can_override_the_request_deadline(self):
        service = AudioAsrService(
            SimpleNamespace(
                audio_asr=AudioAsrConfig(enabled=True, timeout_seconds=60.0),
                remote_access=RemoteAccessConfig(
                    vibe_cloud=VibeCloudRemoteAccessConfig(
                        enabled=True,
                        backend_url="https://avibe.bot",
                        instance_id="instance",
                        instance_secret="secret",
                    )
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.wav"
            audio_path.write_bytes(b"audio")
            attachment = FileAttachment(
                name="voice.wav",
                mimetype="audio/wav",
                local_path=str(audio_path),
                size=5,
            )
            transcribe = AsyncMock(return_value=None)
            with (
                patch("core.audio_asr.time.monotonic", return_value=100.0),
                patch.object(service, "_transcribe_one", transcribe),
            ):
                asyncio.run(
                    service.transcribe_attachments(
                        [attachment],
                        timeout_seconds=120.0,
                    )
                )

            self.assertEqual(transcribe.await_args.args[3], 220.0)


class _AttachmentIMClient:
    def __init__(self, payload: bytes = b"audio"):
        self.payload = payload
        self.sent_messages = []
        self.formatter = SimpleNamespace(format_error=lambda text: text)

    def should_use_thread_for_reply(self):
        return False

    async def prepare_turn_context(self, context, source):
        return context

    async def get_user_info(self, user_id):
        return {"display_name": user_id}

    async def download_file_to_path(self, file_info, target_path):
        Path(target_path).write_bytes(self.payload)
        return FileDownloadResult(True)

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent_messages.append(text)
        return "echo-1"


class _FakeAudioAsrService:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error
        self.calls = []

    async def transcribe_attachments(self, files):
        self.calls.append(files)
        if self.error:
            raise self.error
        return self.result


class MessageHandlerAudioAsrTests(unittest.IsolatedAsyncioTestCase):
    async def _run_turn(self, *, asr_service, echo_transcript=True, language="en"):
        controller = _StubController(platform="slack", ack_mode="message", typing_result=True)
        controller.config.audio_asr = AudioAsrConfig(enabled=True, echo_transcript=echo_transcript)
        controller.config.language = language
        controller._get_lang = lambda: controller.config.language
        controller.config_refresh_calls = 0

        def _refresh_config_from_disk():
            controller.config_refresh_calls += 1

        controller._refresh_config_from_disk = _refresh_config_from_disk
        controller.im_client = _AttachmentIMClient()
        controller.audio_asr_service = asr_service
        handler = MessageHandler(controller)
        handler.set_session_handler(controller.session_handler or SimpleNamespace())

        class _SessionHandler:
            @staticmethod
            def get_session_info(context, source="human"):
                return ("base", "/tmp", "base:/tmp")

            @staticmethod
            def should_allocate_scheduled_anchor(context, source="human"):
                return False

        handler.set_session_handler(_SessionHandler())
        attachment = FileAttachment(name="voice.m4a", mimetype="audio/mp4", url="file-id", size=5)
        context = MessageContext(user_id="U1", channel_id="C1", message_id="M1", files=[attachment])

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.paths.get_attachments_dir", return_value=Path(tmpdir)):
                await handler.handle_user_message(context, "please transcribe")

        return controller

    async def test_audio_asr_success_appends_transcript_and_echoes(self):
        transcript = AudioTranscript("voice.m4a", "/tmp/voice.m4a", "hello from audio")
        asr_service = _FakeAudioAsrService(result=[transcript])

        controller = await self._run_turn(asr_service=asr_service)

        self.assertEqual(len(asr_service.calls), 1)
        self.assertEqual(controller.config_refresh_calls, 1)
        request = controller.agent_service.requests[0][1]
        self.assertIn("[Audio Transcripts]", request.message)
        self.assertIn("hello from audio", request.message)
        self.assertEqual(len(request.files), 1)
        self.assertIn("Voice transcript:\nhello from audio", controller.im_client.sent_messages)

    async def test_audio_asr_error_falls_back_to_original_message_and_files(self):
        asr_service = _FakeAudioAsrService(error=RuntimeError("asr down"))

        controller = await self._run_turn(asr_service=asr_service)

        request = controller.agent_service.requests[0][1]
        self.assertNotIn("[Audio Transcripts]", request.message)
        self.assertIn("please transcribe", request.message)
        self.assertEqual(len(request.files), 1)
        self.assertFalse(any("Voice transcript" in message for message in controller.im_client.sent_messages))

    async def test_audio_asr_echo_can_be_disabled(self):
        transcript = AudioTranscript("voice.m4a", "/tmp/voice.m4a", "hello from audio")
        asr_service = _FakeAudioAsrService(result=[transcript])

        controller = await self._run_turn(asr_service=asr_service, echo_transcript=False)

        request = controller.agent_service.requests[0][1]
        self.assertIn("hello from audio", request.message)
        self.assertFalse(any("Voice transcript" in message for message in controller.im_client.sent_messages))

    async def test_audio_asr_echo_uses_configured_language(self):
        transcript = AudioTranscript("voice.m4a", "/tmp/voice.m4a", "你好")
        asr_service = _FakeAudioAsrService(result=[transcript])

        controller = await self._run_turn(asr_service=asr_service, language="zh")

        self.assertIn("语音转写：\n你好", controller.im_client.sent_messages)


if __name__ == "__main__":
    unittest.main()
