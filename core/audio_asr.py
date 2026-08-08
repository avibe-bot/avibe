"""Audio transcription helpers for inbound file attachments."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import aiohttp

from config.v2_config import AudioAsrConfig, V2Config
from modules.im.base import FileAttachment

logger = logging.getLogger(__name__)

_SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}

AUDIO_SIGNATURE_SAMPLE_BYTES = 64


def detect_audio_mime_from_sample(data: bytes) -> tuple[str, str] | None:
    """Detect high-confidence audio MIME metadata from file signature bytes."""
    if len(data) < 4:
        return None

    if len(data) >= 12 and data[4:8] == b"ftyp":
        brands = data[8:AUDIO_SIGNATURE_SAMPLE_BYTES]
        if data[8:12] in {b"M4A ", b"M4B "} or b"M4A " in brands or b"M4B " in brands:
            return ("audio/mp4", ".m4a")
        return None

    if data[:4] == b"OggS":
        return ("audio/ogg", ".ogg")

    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ("audio/wav", ".wav")

    if data[:4] == b"fLaC":
        return ("audio/flac", ".flac")

    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return ("audio/aac", ".aac")

    if data[:3] == b"ID3" or (
        len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0
    ):
        return ("audio/mpeg", ".mp3")

    return None


def detect_audio_mime_from_file(path: Path) -> tuple[str, str] | None:
    try:
        with path.open("rb") as file_obj:
            return detect_audio_mime_from_sample(file_obj.read(AUDIO_SIGNATURE_SAMPLE_BYTES))
    except OSError:
        return None


@dataclass
class AudioTranscript:
    attachment_name: str
    local_path: str
    text: str
    duration_ms: int | None = None


@dataclass
class AudioAsrRuntimeConfig:
    base_url: str
    instance_id: str
    device_secret: str


class AudioAsrTimeoutError(TimeoutError):
    """Raised when the upstream ASR request exhausts its total deadline."""


class AudioAsrEmptyTranscriptError(ValueError):
    """Raised when upstream accepts the audio but returns no transcript text."""


class AudioAsrProtocolError(ValueError):
    """Raised when upstream returns a success response with an invalid schema."""


class AudioAsrInvalidDictationError(ValueError):
    """Raised when upstream rejects composer dictation metadata or receipts."""


class AudioAsrUnavailableError(ConnectionError):
    """Raised when the configured upstream ASR service is unavailable."""


class AudioAsrService:
    """Transcribe downloaded audio attachments through AVIBE ASR."""

    def __init__(self, config: V2Config):
        self.config = config

    def _get_audio_asr_config(self) -> AudioAsrConfig:
        return getattr(self.config, "audio_asr", None) or AudioAsrConfig()

    def _runtime_config(self) -> AudioAsrRuntimeConfig | None:
        cloud = getattr(getattr(self.config, "remote_access", None), "vibe_cloud", None)
        if not cloud:
            return None
        if not getattr(cloud, "enabled", False):
            return None
        base_url = (getattr(cloud, "backend_url", "") or "").strip().rstrip("/")
        instance_id = (getattr(cloud, "instance_id", "") or "").strip()
        device_secret = (getattr(cloud, "instance_secret", "") or "").strip()
        if not base_url or not instance_id or not device_secret:
            return None
        return AudioAsrRuntimeConfig(base_url=base_url, instance_id=instance_id, device_secret=device_secret)

    def _endpoint_url(self, runtime: AudioAsrRuntimeConfig) -> str:
        asr_config = self._get_audio_asr_config()
        endpoint_path = (asr_config.endpoint_path or "/v1/audio/transcriptions").strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"
        return urljoin(f"{runtime.base_url}/", endpoint_path.lstrip("/"))

    @staticmethod
    def _dictation_endpoint_url(runtime: AudioAsrRuntimeConfig) -> str:
        return urljoin(f"{runtime.base_url}/", "v1/voice/dictations")

    def is_available(self) -> bool:
        asr_config = self._get_audio_asr_config()
        return bool(asr_config.enabled and self._runtime_config())

    def is_audio_attachment(self, attachment: FileAttachment) -> bool:
        mimetype = (attachment.mimetype or "").lower()
        if mimetype.startswith("audio/"):
            if mimetype == "audio/silk":
                return False
            return True
        name = attachment.name or attachment.local_path or ""
        suffix = Path(name).suffix.lower()
        if suffix in _SUPPORTED_AUDIO_EXTENSIONS:
            return True

        if attachment.local_path:
            return detect_audio_mime_from_file(Path(attachment.local_path)) is not None
        return False

    def eligible_attachments(self, attachments: Iterable[FileAttachment]) -> list[FileAttachment]:
        asr_config = self._get_audio_asr_config()
        eligible: list[FileAttachment] = []
        for attachment in attachments:
            if not attachment.local_path or not self.is_audio_attachment(attachment):
                continue
            if asr_config.max_file_bytes is not None and attachment.size and attachment.size > asr_config.max_file_bytes:
                logger.info(
                    "Skipping audio ASR for %s: file size %s exceeds configured max %s",
                    attachment.name,
                    attachment.size,
                    asr_config.max_file_bytes,
                )
                continue
            eligible.append(attachment)
        return eligible

    async def transcribe_attachments(
        self,
        attachments: list[FileAttachment],
        *,
        raise_on_empty: bool = False,
        raise_on_timeout: bool = False,
        raise_on_unavailable: bool = False,
        timeout_seconds: float | None = None,
    ) -> list[AudioTranscript]:
        asr_config = self._get_audio_asr_config()
        if not asr_config.enabled:
            return []
        runtime = self._runtime_config()
        if not runtime:
            logger.info("Skipping audio ASR: Avibe Cloud pairing credentials are unavailable")
            return []
        eligible = self.eligible_attachments(attachments)
        if not eligible:
            return []

        request_timeout_seconds = max(
            0.1,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else asr_config.timeout_seconds or 60.0
            ),
        )
        deadline = time.monotonic() + request_timeout_seconds
        timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [self._transcribe_one(session, runtime, attachment, deadline) for attachment in eligible]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        transcripts: list[AudioTranscript] = []
        for result in results:
            if isinstance(result, AudioTranscript):
                transcripts.append(result)
            elif isinstance(result, AudioAsrEmptyTranscriptError):
                if raise_on_empty:
                    raise result
            elif isinstance(result, AudioAsrTimeoutError) and raise_on_timeout:
                raise result
            elif isinstance(result, AudioAsrUnavailableError) and raise_on_unavailable:
                raise result
            elif isinstance(result, Exception):
                logger.warning("Audio ASR skipped after error: %s", result)
        return transcripts

    async def transcribe_voice_segment(
        self,
        attachment: FileAttachment | None,
        *,
        dictation_id: str,
        sequence: int,
        overlap_ms: int,
        final: bool,
        finalize_only: bool,
        receipts: list[str],
        before: str,
        after: str,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        """Forward one composer segment using the server-owned dictation contract."""
        runtime = self._runtime_config()
        if not self._get_audio_asr_config().enabled or not runtime:
            raise AudioAsrUnavailableError("audio ASR upstream unavailable")

        form = aiohttp.FormData()
        form.add_field("model", self._get_audio_asr_config().model)
        form.add_field("response_format", "json")
        form.add_field("dictation_id", dictation_id)
        form.add_field("sequence", str(sequence))
        form.add_field("overlap_ms", str(overlap_ms))
        form.add_field("final", "true" if final else "false")
        if finalize_only:
            form.add_field("finalize_only", "true")
        for receipt in receipts:
            form.add_field("receipt", receipt)
        if before:
            form.add_field("before", before)
        if after:
            form.add_field("after", after)

        path: Path | None = None
        handle = None
        if attachment is not None:
            path = Path(attachment.local_path or "")
            if not path.is_file():
                raise AudioAsrProtocolError("composer audio file is unavailable")
            mimetype = attachment.mimetype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            detected_audio = detect_audio_mime_from_file(path)
            if detected_audio and not mimetype.lower().startswith(("audio/", "video/")):
                mimetype = detected_audio[0]
            filename = attachment.name or path.name
            if detected_audio and not Path(filename).suffix:
                filename = f"{filename}{detected_audio[1]}"
            handle = path.open("rb")
            form.add_field("file", handle, filename=filename, content_type=mimetype)

        timeout = aiohttp.ClientTimeout(total=max(0.1, timeout_seconds))
        started_at = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._dictation_endpoint_url(runtime),
                    data=form,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "avibe/dev",
                        "X-Vibe-Instance-Id": runtime.instance_id,
                        "X-Vibe-Device-Secret": runtime.device_secret,
                    },
                ) as response:
                    payload = None
                    try:
                        payload = await response.json(content_type=None)
                    except Exception as exc:
                        if 200 <= response.status < 300:
                            raise AudioAsrProtocolError("audio ASR returned invalid JSON") from exc
                    if response.status < 200 or response.status >= 300:
                        upstream_error = payload.get("error") if isinstance(payload, dict) else None
                        if response.status == 504 or upstream_error == "transcription_timeout":
                            raise AudioAsrTimeoutError("audio ASR upstream timed out")
                        if response.status == 422 and upstream_error == "transcription_empty":
                            raise AudioAsrEmptyTranscriptError("audio ASR returned an empty transcript")
                        if response.status == 422 and upstream_error == "invalid_dictation":
                            raise AudioAsrInvalidDictationError("audio ASR rejected dictation metadata")
                        if response.status == 503 or upstream_error in {
                            "asr_not_configured",
                            "asr_unavailable",
                        }:
                            raise AudioAsrUnavailableError("audio ASR upstream unavailable")
                        raise AudioAsrProtocolError("audio ASR rejected the composer segment")
        except (
            AudioAsrEmptyTranscriptError,
            AudioAsrInvalidDictationError,
            AudioAsrProtocolError,
            AudioAsrTimeoutError,
            AudioAsrUnavailableError,
        ):
            raise
        except asyncio.TimeoutError:
            raise AudioAsrTimeoutError("audio ASR request timed out") from None
        except aiohttp.ClientError as exc:
            raise AudioAsrUnavailableError("audio ASR request unavailable") from exc
        finally:
            if handle is not None:
                handle.close()

        if not isinstance(payload, dict):
            raise AudioAsrProtocolError("audio ASR returned a malformed success response")
        if isinstance(payload.get("text"), str):
            cleanup = payload.get("cleanup")
            if cleanup not in {"success", "fallback"}:
                raise AudioAsrProtocolError("audio ASR omitted cleanup outcome")
        elif not (
            isinstance(payload.get("receipt"), str)
            and isinstance(payload.get("sequence"), int)
            and not isinstance(payload.get("sequence"), bool)
        ):
            raise AudioAsrProtocolError("audio ASR returned a malformed success response")
        logger.info(
            "Composer voice segment completed: sequence=%s final=%s duration_ms=%s",
            sequence,
            final,
            int((time.monotonic() - started_at) * 1000),
        )
        return payload

    async def _transcribe_one(
        self,
        session: aiohttp.ClientSession,
        runtime: AudioAsrRuntimeConfig,
        attachment: FileAttachment,
        deadline: float,
    ) -> AudioTranscript | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioAsrTimeoutError("audio ASR deadline exhausted")

        path = Path(attachment.local_path or "")
        if not path.is_file():
            logger.warning("Skipping audio ASR for %s: local file is unavailable", attachment.name)
            return None

        asr_config = self._get_audio_asr_config()
        detected_audio = detect_audio_mime_from_file(path)
        mimetype = attachment.mimetype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        mimetype_lower = mimetype.lower()
        if detected_audio and (
            not mimetype
            or mimetype_lower == "application/octet-stream"
            or not mimetype_lower.startswith(("audio/", "video/"))
        ):
            mimetype = detected_audio[0]
        filename = attachment.name or path.name
        if detected_audio and not Path(filename).suffix:
            filename = f"{filename}{detected_audio[1]}"
        start = time.monotonic()
        form = aiohttp.FormData()
        form.add_field("model", asr_config.model)
        form.add_field("response_format", "json")

        with path.open("rb") as handle:
            form.add_field(
                "file",
                handle,
                filename=filename,
                content_type=mimetype,
            )
            try:
                async with session.post(
                    self._endpoint_url(runtime),
                    data=form,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "avibe/dev",
                        "X-Vibe-Instance-Id": runtime.instance_id,
                        "X-Vibe-Device-Secret": runtime.device_secret,
                    },
                    timeout=aiohttp.ClientTimeout(total=max(0.1, remaining)),
                ) as response:
                    payload: Any = None
                    try:
                        payload = await response.json(content_type=None)
                    except Exception:
                        text = await response.text()
                        if response.status < 200 or response.status >= 300:
                            payload = {"error": text[:200]}
                    duration_ms = int((time.monotonic() - start) * 1000)
                    if response.status < 200 or response.status >= 300:
                        logger.warning(
                            "Audio ASR failed for %s: status=%s mimetype=%s duration_ms=%s",
                            attachment.name,
                            response.status,
                            mimetype,
                            duration_ms,
                        )
                        upstream_error = (
                            payload.get("error")
                            if isinstance(payload, dict)
                            else None
                        )
                        if response.status == 504 or upstream_error == "transcription_timeout":
                            raise AudioAsrTimeoutError("audio ASR upstream timed out")
                        if response.status == 503 or upstream_error in {
                            "asr_not_configured",
                            "asr_unavailable",
                        }:
                            raise AudioAsrUnavailableError("audio ASR upstream unavailable")
                        return None
            except (AudioAsrTimeoutError, AudioAsrUnavailableError):
                raise
            except asyncio.TimeoutError:
                logger.warning("Audio ASR timed out for %s", attachment.name)
                raise AudioAsrTimeoutError("audio ASR request timed out") from None
            except aiohttp.ClientError as exc:
                logger.warning("Audio ASR request unavailable for %s: %s", attachment.name, exc)
                raise AudioAsrUnavailableError("audio ASR request unavailable") from exc
            except Exception as exc:
                logger.warning("Audio ASR request failed for %s: %s", attachment.name, exc)
                return None

        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            logger.warning("Audio ASR returned a malformed success response for %s", attachment.name)
            raise AudioAsrProtocolError("audio ASR returned a malformed success response")
        text = payload["text"]
        if not text.strip():
            logger.warning("Audio ASR returned empty transcript for %s", attachment.name)
            raise AudioAsrEmptyTranscriptError("audio ASR returned an empty transcript")
        return AudioTranscript(
            attachment_name=attachment.name or path.name,
            local_path=str(path),
            text=text,
            duration_ms=duration_ms,
        )


def format_audio_transcripts_block(transcripts: list[AudioTranscript]) -> str:
    if not transcripts:
        return ""
    lines = ["[Audio Transcripts]"]
    for transcript in transcripts:
        name = transcript.attachment_name or Path(transcript.local_path).name
        text = transcript.text.replace("\r", " ").strip()
        lines.append(f"- {name}: {text}")
    return "\n".join(lines)


def append_audio_transcripts_to_message(message: str, transcripts: list[AudioTranscript]) -> str:
    block = format_audio_transcripts_block(transcripts)
    if not block:
        return message
    if not message or not message.strip():
        return block
    return f"{message}\n\n{block}"


def format_audio_transcript_echo(
    transcripts: list[AudioTranscript],
    *,
    single_label: str,
    multiple_label: str,
) -> str:
    if not transcripts:
        return ""
    if len(transcripts) == 1:
        return f"{single_label}\n{transcripts[0].text.strip()}"
    lines = [multiple_label]
    for transcript in transcripts:
        name = transcript.attachment_name or Path(transcript.local_path).name
        lines.append(f"- {name}: {transcript.text.strip()}")
    return "\n".join(lines)
