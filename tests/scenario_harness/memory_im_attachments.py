"""Hermetic Slack-to-Memory attachment capture scenario harness."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from core.controller import Controller
from core.handlers.inbound_attachments import InboundAttachmentMaterializer
from core.memory.everos import FakeMemoryProvider, ProviderCapture
from core.memory.everos_insight.recorder import (
    ProviderCallInput,
    ProviderCallRow,
    normalize_provider_call,
)
from core.memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from core.memory.store import MemoryStore
from core.memory.types import MemoryItem
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext
from modules.im.message_facts import is_ordinary_slack_attachment, is_ordinary_slack_text


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "default"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAYElEQVR42u3QAQ0AAAwC"
    "IPuX1hzfIQLpcxEgQIAAAQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAA"
    "AQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAAAQLuG0bQw7Ko2TvAAAAAAElFTkSuQmCC"
)


class _SlackDownloader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    async def download_file_to_path(
        self,
        file_info,
        _target_path,
        *,
        max_bytes=None,
        target_fd=None,
        **_kwargs,
    ) -> FileDownloadResult:
        assert target_fd is not None
        payload = self.payloads[str(file_info["name"])]
        self.calls.append(
            {
                "name": file_info["name"],
                "max_bytes": max_bytes,
            }
        )
        os.write(target_fd, payload)
        return FileDownloadResult(True)


class _UserStore:
    def __init__(self, *, bound: bool) -> None:
        self.bound = bound

    def maybe_reload(self) -> None:
        return None

    def get_user(self, _user_id: str, *, platform: str):
        assert platform == "slack"
        return SimpleNamespace(enabled=True) if self.bound else None


class _SettingsManager:
    def __init__(self, *, bound: bool) -> None:
        self._store = _UserStore(bound=bound)

    def get_store(self) -> _UserStore:
        return self._store


@dataclass
class _InspectableProvider(FakeMemoryProvider):
    observed_payloads: list[bytes] = field(default_factory=list)
    call_log: list[ProviderCallRow] = field(default_factory=list)

    async def add(self, capture: ProviderCapture):
        if capture.attachments:
            attachment = capture.attachments[0]
            source = Path(attachment.uri.removeprefix("file://"))
            payload = source.read_bytes()
            self.observed_payloads.append(payload)
            data_uri = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
            self.call_log.append(
                normalize_provider_call(
                    ProviderCallInput(
                        id=f"multimodal-{len(self.call_log) + 1}",
                        started_at_ms=1_700_000_000_000,
                        duration_ms=1,
                        kind="multimodal_llm",
                        stage="boundary",
                        status="ok",
                        request={
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": capture.text},
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": data_uri},
                                        },
                                    ],
                                }
                            ]
                        },
                        response={"content": "attachment parsed"},
                    ),
                    exact_redaction_values=(attachment.uri,),
                )
            )
            self.search_items = (
                MemoryItem(
                    kind="fact",
                    text=f"Captured Slack attachment {attachment.name}",
                ),
            )
        return await super().add(capture)


class _Runtime:
    def __init__(self, module: MemoryModule, *, attachment_status: str) -> None:
        self.module = module
        self.available = True
        self.retired = False
        self._attachment_status = attachment_status

    def principal_for_user_key(self, user_key: str) -> str:
        assert user_key == "slack:U1"
        return PRINCIPAL

    async def attachment_capture_status(self) -> str:
        return self._attachment_status


class MemoryIMAttachmentScenarioHarness:
    """Drive the real shared materializer, admission, pin, outbox, and worker."""

    def __init__(
        self,
        root: Path,
        *,
        attachment_status: str = "ready",
        bound: bool = True,
        is_dm: bool = True,
    ) -> None:
        self.home = root / "avibe-home"
        self.home.mkdir(mode=0o700, parents=True)
        self.provider = _InspectableProvider()
        self.module = MemoryModule(
            MemoryStore(self.home / "state" / "memory.sqlite"),
            self.provider,
            enabled=True,
            disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
            effective_home=self.home,
        )
        self.runtime = _Runtime(self.module, attachment_status=attachment_status)
        self.controller = Controller.__new__(Controller)
        self.controller.config = SimpleNamespace(
            memory=SimpleNamespace(enabled=True, recovery_intent=None)
        )
        self.controller.platform_settings_managers = {
            "slack": _SettingsManager(bound=bound)
        }
        self.controller.memory_runtime = self.runtime
        self.controller.memory_module = self.module
        self.controller.get_cwd = lambda _context: str(root / "project")
        self.is_dm = is_dm
        self.downloader: _SlackDownloader | None = None
        self.last_context: MessageContext | None = None

    async def capture(
        self,
        *,
        text: str,
        payloads: dict[str, tuple[str, bytes]],
    ) -> None:
        files = [
            FileAttachment(
                name=name,
                mimetype=mimetype,
                url=f"https://files.slack.test/{index}",
                size=len(payload),
            )
            for index, (name, (mimetype, payload)) in enumerate(payloads.items())
        ]
        event = {
            "type": "message",
            "subtype": "file_share",
            "user": "U1",
            "text": text,
            "files": [
                {"name": item.name, "mimetype": item.mimetype, "size": item.size}
                for item in files
            ],
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": ([{"type": "text", "text": text}] if text else []),
                        }
                    ],
                }
            ],
        }
        context = MessageContext(
            user_id="U1",
            channel_id="D1" if self.is_dm else "C1",
            platform="slack",
            thread_id="native-1",
            message_id="native-1",
            platform_specific={"platform": "slack", "is_dm": self.is_dm},
            files=files,
            is_ordinary_text=is_ordinary_slack_text(event, files),
            is_ordinary_attachment=is_ordinary_slack_attachment(event, files),
        )
        self.last_context = context
        self.downloader = _SlackDownloader(
            {name: payload for name, (_mimetype, payload) in payloads.items()}
        )
        batch = await InboundAttachmentMaterializer(
            effective_home=self.home,
            attachments_root=self.home / "attachments",
        ).materialize(context, self.downloader)
        memory_lease = None
        try:
            if await self.controller.memory_attachment_capture_admitted(
                context,
                "stable-session",
            ):
                memory_lease = batch.lease.retain()
            await self.controller.capture_user_memory(
                context,
                text,
                "stable-session",
                attachment_lease=memory_lease,
            )
            batch.lease.adopt()
        finally:
            if memory_lease is not None:
                memory_lease.release()
            batch.lease.release()
        await self.module.drain()
        await self.module.final_flush(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            raw_session_id="stable-session",
        )

    @property
    def memory_bundle_entries(self) -> tuple[Path, ...]:
        bundles = self.home / "memory" / "attachments" / "bundles"
        return tuple(bundles.iterdir()) if bundles.exists() else ()
