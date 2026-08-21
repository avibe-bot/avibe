from __future__ import annotations

from enum import Enum


class ShowRuntimeFailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CONFIGURED = "configured"


_TRANSIENT_FAILURES = frozenset(
    {
        "runtime_archive_download_failed",
        "runtime_install_already_running",
        "runtime_install_failed",
        "runtime_manifest_download_failed",
    }
)


def classify_show_runtime_failure(reason: str | None) -> ShowRuntimeFailureClass:
    """Classify install failures for retry and recovery behavior."""
    if reason and reason.endswith("_unavailable_offline"):
        return ShowRuntimeFailureClass.CONFIGURED
    if reason in _TRANSIENT_FAILURES:
        return ShowRuntimeFailureClass.TRANSIENT
    return ShowRuntimeFailureClass.PERMANENT
