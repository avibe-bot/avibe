"""Provider-independent public exports for Avibe's local Memory capability."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any


_TYPE_EXPORTS = frozenset(
    {
        "CaptureAccepted",
        "CaptureAttachment",
        "CaptureDuplicate",
        "CaptureReceipt",
        "CaptureRequest",
        "CaptureSkipped",
        "MemoryErrorCode",
        "MemoryItem",
        "MemoryItems",
        "MemoryKind",
        "MemoryListItem",
        "MemoryListPage",
        "MemoryListResult",
        "MemoryProfile",
        "MemoryProfileExplicitInfo",
        "MemoryProfileTrait",
        "MemoryResult",
        "OperationFailed",
        "ProviderSessionRef",
        "RecallItems",
        "RecallMode",
        "RecallPolicy",
        "RecallResult",
    }
)

__all__ = [
    "CaptureAccepted",
    "CaptureAttachment",
    "CaptureDuplicate",
    "CaptureReceipt",
    "CaptureRequest",
    "CaptureSkipped",
    "MemoryErrorCode",
    "MemoryItem",
    "MemoryItems",
    "MemoryKind",
    "MemoryListItem",
    "MemoryListPage",
    "MemoryListResult",
    "MemoryModule",
    "MemoryProfile",
    "MemoryProfileExplicitInfo",
    "MemoryProfileTrait",
    "MemoryResult",
    "OperationFailed",
    "ProviderSessionRef",
    "RecallItems",
    "RecallMode",
    "RecallPolicy",
    "RecallResult",
]

if TYPE_CHECKING:
    from core.memory.module import MemoryModule as MemoryModule
    from core.memory.types import (
        CaptureAccepted as CaptureAccepted,
        CaptureAttachment as CaptureAttachment,
        CaptureDuplicate as CaptureDuplicate,
        CaptureReceipt as CaptureReceipt,
        CaptureRequest as CaptureRequest,
        CaptureSkipped as CaptureSkipped,
        MemoryErrorCode as MemoryErrorCode,
        MemoryItem as MemoryItem,
        MemoryItems as MemoryItems,
        MemoryKind as MemoryKind,
        MemoryListItem as MemoryListItem,
        MemoryListPage as MemoryListPage,
        MemoryListResult as MemoryListResult,
        MemoryProfile as MemoryProfile,
        MemoryProfileExplicitInfo as MemoryProfileExplicitInfo,
        MemoryProfileTrait as MemoryProfileTrait,
        MemoryResult as MemoryResult,
        OperationFailed as OperationFailed,
        ProviderSessionRef as ProviderSessionRef,
        RecallItems as RecallItems,
        RecallMode as RecallMode,
        RecallPolicy as RecallPolicy,
        RecallResult as RecallResult,
    )


def __getattr__(name: str) -> Any:
    """Load public compatibility exports without penalizing leaf imports."""

    if name == "MemoryModule":
        value = getattr(import_module("core.memory.module"), name)
    elif name in _TYPE_EXPORTS:
        value = getattr(import_module("core.memory.types"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
