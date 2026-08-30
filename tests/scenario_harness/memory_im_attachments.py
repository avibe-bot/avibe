"""Hermetic Slack-to-Memory attachment capture scenario harness."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from avibe_memory.capture_adapter import EnabledMemoryAdapter
from core.handlers.message_handler import memory_turn_event
from core.handlers.inbound_attachments import InboundAttachmentMaterializer
from avibe_memory.everos import FakeMemoryProvider, ProviderCapture
from avibe_memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from avibe_memory.store import MemoryStore
from avibe_memory.types import MemoryItem
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext
from modules.im.message_facts import is_original_human_slack_attachment, is_original_human_slack_text


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


class _Bindings:
    def __init__(self, *, bound: bool) -> None:
        self.bound = bound

    def is_enabled_user(self, platform: str, user_id: str) -> bool:
        return platform == "slack" and user_id == "U1" and self.bound


class _LifecycleAdmission:
    def release(self) -> None:
        return None


@dataclass
class _InspectableProvider(FakeMemoryProvider):
    observed_payloads: list[bytes] = field(default_factory=list)
    provider_invocations: list[str] = field(default_factory=list)

    async def add(self, capture: ProviderCapture):
        if capture.attachments:
            attachment = capture.attachments[0]
            source = Path(attachment.uri.removeprefix("file://"))
            payload = source.read_bytes()
            self.observed_payloads.append(payload)
            self.provider_invocations.append("multimodal_llm")
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
        self._attachment_generation = (
            None if attachment_status == "not_configured" else 1
        )

    def principal_for_user_key(self, user_key: str) -> str:
        assert user_key == "slack:U1"
        return PRINCIPAL

    async def attachment_capture_status(self) -> str:
        return self._attachment_status

    def attachment_capture_config_generation(self) -> int | None:
        return self._attachment_generation


class MemoryIMAttachmentScenarioHarness:
    """Drive the real shared materializer, admission, pin, and bounded writer."""

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
        self.bound = bound
        self.adapter = self._new_adapter()
        self.is_dm = is_dm
        self.downloader: _SlackDownloader | None = None
        self.last_context: MessageContext | None = None

    def _new_adapter(self) -> EnabledMemoryAdapter:
        return EnabledMemoryAdapter(
            module=self.module,
            principals=self.runtime,
            bindings=_Bindings(bound=self.bound),
            lifecycle_snapshot_matches=lambda _session_id, snapshot: snapshot == 1,
            acquire_lifecycle_admission=lambda _session_id: _completed(
                _LifecycleAdmission()
            ),
            attachment_capture_status=self.runtime.attachment_capture_status,
            attachment_config_generation=(
                self.runtime.attachment_capture_config_generation
            ),
        )

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
            is_original_human_text=is_original_human_slack_text(event, files),
            is_original_human_attachment=is_original_human_slack_attachment(event, files),
        )
        self.last_context = context
        self.downloader = _SlackDownloader(
            {name: payload for name, (_mimetype, payload) in payloads.items()}
        )
        batch = await InboundAttachmentMaterializer(
            effective_home=self.home,
            attachments_root=self.home / "attachments",
        ).materialize(context, self.downloader)
        try:
            self.adapter = self._new_adapter()
            assert self.adapter.start()
            self.adapter.offer(
                memory_turn_event(
                    context,
                    text,
                    "stable-session",
                    1,
                    batch.lease,
                )
            )
            batch.lease.adopt()
        finally:
            batch.lease.release()
        try:
            await self.adapter.wait_idle_for_tests()
        finally:
            await self.adapter.cancel_memory_capture_tasks()

    @property
    def memory_bundle_entries(self) -> tuple[Path, ...]:
        bundles = self.home / "memory" / "attachments" / "bundles"
        return tuple(bundles.iterdir()) if bundles.exists() else ()


async def _completed(value: object) -> object:
    return value
