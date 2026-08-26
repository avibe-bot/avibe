"""Memory-only filtering for materialized IM attachment leases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from core.inbound_attachment_lease import (
    InboundAttachmentLease,
    leased_attachment_records,
    open_leased_attachment_record,
)
from core.memory.attachments import (
    MAX_PINNED_ATTACHMENTS,
    MAX_PINNED_ATTACHMENT_BYTES,
    MAX_PINNED_BUNDLE_BYTES,
)
from core.memory.modality import classify_pinned_attachment
from core.memory.types import CaptureAttachment
from vibe.memory_contract import IM_ATTACHMENT_CAPTURE_PLATFORMS


AttachmentSkipReason = Literal[
    "count_limit",
    "download_failed",
    "file_too_large",
    "bundle_too_large",
    "unsupported_type",
]

@dataclass(frozen=True, slots=True)
class MemoryAttachmentSelection:
    attachments: tuple[CaptureAttachment, ...]
    skipped: tuple[AttachmentSkipReason, ...]


def select_memory_attachments(
    lease: InboundAttachmentLease,
) -> MemoryAttachmentSelection:
    """Keep valid siblings from one active shared-materializer lease."""

    _root, directory_fd, records = leased_attachment_records(lease)
    selected: list[CaptureAttachment] = []
    skipped: list[AttachmentSkipReason] = []
    total = 0
    try:
        for record in records:
            if (
                record.declared_size is not None
                and record.declared_size > MAX_PINNED_ATTACHMENT_BYTES
            ) or record.size > MAX_PINNED_ATTACHMENT_BYTES:
                skipped.append("file_too_large")
                continue
            file_fd: int | None = None
            try:
                file_fd, _source_info = open_leased_attachment_record(directory_fd, record)
                classification = classify_pinned_attachment(
                    record.name,
                    record.mimetype,
                    record.path,
                    file_fd=file_fd,
                )
            except (OSError, ValueError):
                classification = None
            finally:
                if file_fd is not None:
                    os.close(file_fd)
            if classification is None:
                skipped.append("unsupported_type")
                continue
            if len(selected) >= MAX_PINNED_ATTACHMENTS:
                skipped.append("count_limit")
                continue
            if total + record.size > MAX_PINNED_BUNDLE_BYTES:
                skipped.append("bundle_too_large")
                continue
            kind, extension = classification
            selected.append(
                CaptureAttachment(
                    kind=kind,
                    name=record.name,
                    uri=record.path.as_uri(),
                    ext=extension,
                )
            )
            total += record.size
    finally:
        os.close(directory_fd)
    return MemoryAttachmentSelection(tuple(selected), tuple(skipped))
