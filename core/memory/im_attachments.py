"""Memory-only filtering for materialized IM attachment leases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.handlers.inbound_attachments import (
    InboundAttachmentLease,
    leased_attachment_records,
)
from core.memory.attachments import (
    MAX_PINNED_ATTACHMENTS,
    MAX_PINNED_ATTACHMENT_BYTES,
    MAX_PINNED_BUNDLE_BYTES,
)
from core.memory.modality import classify_pinned_attachment
from core.memory.types import CaptureAttachment


AttachmentSkipReason = Literal[
    "count_limit",
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

    _root, records = leased_attachment_records(lease)
    selected: list[CaptureAttachment] = []
    skipped: list[AttachmentSkipReason] = []
    total = 0
    for index, record in enumerate(records):
        if index >= MAX_PINNED_ATTACHMENTS:
            skipped.append("count_limit")
            continue
        if (
            record.declared_size is not None
            and record.declared_size > MAX_PINNED_ATTACHMENT_BYTES
        ) or record.size > MAX_PINNED_ATTACHMENT_BYTES:
            skipped.append("file_too_large")
            continue
        classification = classify_pinned_attachment(
            record.name,
            record.mimetype,
            record.path,
        )
        if classification is None:
            skipped.append("unsupported_type")
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
    return MemoryAttachmentSelection(tuple(selected), tuple(skipped))

