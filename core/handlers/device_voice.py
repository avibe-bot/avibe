"""Narrow local-device host boundary. No device or codec dependency in Avibe."""

from __future__ import annotations

import asyncio
import base64
import binascii
from html.parser import HTMLParser
import io
import re
from urllib.parse import urlsplit
import wave

import aiohttp
from markdown_it import MarkdownIt

from core.control_requests import ControlRequest, failure, result

MAX_AUDIO_BYTES = 2_000_000
MAX_SPEECH_CHARS = 120


def _failure(controller, code: str, key: str) -> dict:
    from vibe.i18n import t

    language = getattr(getattr(controller, "config", None), "language", "en")
    return failure(code, t("device." + key, language))


def speech_projection(text: str) -> str:
    """Speak up to two complete plain sentences; never synthesize code/URLs."""
    class HTMLScope(HTMLParser):
        """Track HTML across inline tokens/paragraphs without rendering it."""

        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.tags: list[str] = []
            self.invalid = False

        def handle_starttag(self, tag, attrs):
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                           "link", "meta", "param", "source", "track", "wbr"}:
                self.tags.append(tag)

        def handle_endtag(self, tag):
            if not self.tags or self.tags[-1] != tag:
                self.invalid = True
            else:
                self.tags.pop()

        def handle_startendtag(self, tag, attrs):
            pass

    def without_addresses(value: str) -> str:
        # CommonMark recognizes angle-bracket autolinks, not bare addresses.
        # Filter only text leaves here; Markdown structure is already parsed.
        for word in value.split():
            candidate = word.strip(".,;:!?。！？，、()[]{}<>\"'")
            address = "://" in candidate or "@" in candidate or candidate.casefold().startswith(("//", "www.", "mailto:"))
            if not address:
                try:
                    host = urlsplit("//" + candidate).hostname or ""
                except ValueError:
                    host = ""
                labels = host.split(".")
                address = (len(labels) > 1 and len(labels[-1]) >= 2 and labels[-1].isalpha()
                           and all(label and all(c.isalnum() or c == "-" for c in label) for label in labels))
            if address:
                value = value.replace(word, "")
        return value

    text = re.sub(r"<silent>.*?</silent>", "", text, flags=re.S)
    html = HTMLScope()
    parents: list[str] = []
    prose: list[str] = []
    allowed = {"paragraph_open", "heading_open", "bullet_list_open", "ordered_list_open",
               "list_item_open", "blockquote_open"}
    for token in MarkdownIt("commonmark").enable("table").parse(text):
        if token.type == "hr":
            break
        if token.nesting == 1:
            parents.append(token.type)
        elif token.nesting == -1:
            parents.pop()
        elif token.type == "html_block":
            html.feed(token.content)
        elif token.type == "inline" and parents and all(parent in allowed for parent in parents):
            # CommonMark treats an indented continuation without a blank line as
            # prose. Withhold that ambiguous paragraph instead of speaking code.
            ambiguous_indent = any(line.startswith("    ") for line in token.content.expandtabs(4).splitlines())
            parts: list[str] = []
            autolink = False
            for child in token.children or []:
                if child.type == "html_inline":
                    html.feed(child.content)
                elif child.type == "link_open":
                    autolink = child.markup == "autolink"
                elif child.type == "link_close":
                    autolink = False
                elif not html.tags and not html.invalid and not autolink:
                    if child.type == "text":
                        # An unmatched backtick is a CommonMark text token. The
                        # remainder is withheld, including subsequent emphasis.
                        prefix, marker, _ = child.content.partition("`")
                        parts.append(without_addresses(prefix))
                        if marker:
                            break
                    elif child.type in {"softbreak", "hardbreak", "code_inline", "image"}:
                        parts.append(" ")
            if not html.tags and not html.invalid and not ambiguous_indent:
                prose.append("".join(parts))
    text = " ".join(" ".join(prose).split())
    sentences = re.findall(r"[^。！？.!?]+[。！？.!?]", text)
    chosen: list[str] = []
    for sentence in sentences[:2]:
        if len("".join(chosen) + sentence) > MAX_SPEECH_CHARS:
            break
        chosen.append(sentence)
    return "".join(chosen).strip()


def _wav(data: bytes, *, input_audio: bool) -> None:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or audio.getframerate() != (16000 if input_audio else 24000)
                or not 0 < audio.getnframes() <= audio.getframerate() * (25 if input_audio else 40)
                or len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2
            ):
                raise ValueError("invalid_audio")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid_audio") from exc


async def cloud_audio(config, action: str, value: str) -> dict:
    """Host-only paired-instance proxy. No credentials or upstream bodies escape."""
    from core.audio_asr import AudioAsrService

    asr = AudioAsrService(config)
    runtime = asr._runtime_config()
    if runtime is None or (action == "transcribe" and not asr.is_available()):
        raise ValueError("voice_unavailable")
    if action == "transcribe":
        if len(value) > (MAX_AUDIO_BYTES * 4 // 3 + 4):
            raise ValueError("audio_too_large")
        try:
            audio = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid_audio") from exc
        _wav(audio, input_audio=True)
        body = aiohttp.FormData()
        body.add_field("model", config.audio_asr.model)
        body.add_field("response_format", "json")
        body.add_field("file", audio, filename="utterance.wav", content_type="audio/wav")
        url = asr._endpoint_url(runtime)
        kwargs = {"data": body}
    else:
        if not value.strip() or len(value) > MAX_SPEECH_CHARS:
            raise ValueError("invalid_speech")
        url = runtime.base_url.rstrip("/") + "/v1/audio/speech"
        kwargs = {"json": {"text": value}}
    headers = {
        "X-Vibe-Instance-Id": runtime.instance_id,
        "X-Vibe-Device-Secret": runtime.device_secret,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
            async with client.post(url, headers=headers, allow_redirects=False, **kwargs) as response:
                if response.status != 200:
                    raise ValueError("voice_upstream_unavailable")
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > MAX_AUDIO_BYTES:
                        raise ValueError("voice_response_too_large")
                if action == "speech":
                    if response.content_type != "audio/wav":
                        raise ValueError("invalid_audio")
                    _wav(bytes(data), input_audio=False)
                    return {"audio": base64.b64encode(data).decode("ascii")}
                import json

                payload = json.loads(data)
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise ValueError("invalid_transcript")
                return {"text": text.strip()}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ValueError("voice_upstream_unavailable") from None


async def handle_device_voice(controller, request: ControlRequest, *, run_sync=asyncio.to_thread) -> dict:
    """Trusted private IPC; model execution is always the isolated chat-only path."""
    from core.handlers.device_chat import DeviceChat, ToolsForbidden
    from vibe import cli

    if request.expired:
        return _failure(controller, "deadline_expired", "expired")
    authority = await run_sync(lambda: cli._cli_invocation_authority(request.caller_env))
    if authority is not None:
        return _failure(controller, "local_device_only", "localOnly")
    args = request.arguments
    action = args.get("action")
    allowed = {
        "status": {"action"},
        "transcribe": {"action", "audio"},
        "speech": {"action", "text"},
        "open": {"action", "device_id"},
        "close": {"action", "conversation_id"},
        "chat": {"action", "conversation_id", "request_id", "text"},
        "history": {"action", "conversation_id"},
    }
    if not isinstance(action, str) or action not in allowed or set(args) != allowed[action]:
        return _failure(controller, "invalid_device_request", "invalidRequest")
    try:
        if action == "status":
            from core.audio_asr import AudioAsrService

            chat = getattr(controller, "device_chat", None)
            payload = {"asr_available": AudioAsrService(controller.config).is_available(),
                       "tts_available": "requires_cloud_capability", "bot": "littlevibey",
                       "execution_policy": "device_chat_only_v1",
                       "conversation_id": chat.active.id if chat and chat.active else None}
        elif action in {"transcribe", "speech"}:
            value = args["audio" if action == "transcribe" else "text"]
            if not isinstance(value, str):
                raise ValueError("invalid_device_request")
            payload = await cloud_audio(controller.config, action, value)
        else:
            chat = getattr(controller, "device_chat", None)
            if chat is None:
                chat = DeviceChat(controller)
                controller.device_chat = chat
            if action == "open":
                device_id = args["device_id"]
                if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", device_id):
                    raise ValueError("invalid_device_id")
                payload = {"conversation_id": await chat.open(device_id)}
            elif action == "close":
                await chat.close(args["conversation_id"])
                payload = {}
            elif action == "history":
                payload = {"transcript": await run_sync(lambda: chat.history(args["conversation_id"]))}
            else:
                try:
                    answer = await chat.answer(args["conversation_id"], args["request_id"], args["text"])
                except ToolsForbidden:
                    from vibe.i18n import t

                    answer = t("device.chatOnly", getattr(controller.config, "language", "en"))
                speech = speech_projection(answer)
                if not speech:
                    raise ValueError("speech_withheld")
                payload = {"request_id": args["request_id"], "text": speech}
        return result({"ok": True, "kind": "device_voice", **payload})
    except (ValueError, TypeError, OSError):
        return _failure(controller, "device_voice_unavailable", "unavailable")
