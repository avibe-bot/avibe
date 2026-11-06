"""Closed restart-state vocabulary shared by producers, retention and display."""

from __future__ import annotations

from enum import Enum


class RestartState(str, Enum):
    """Closed restart-state vocabulary and its retention policy."""

    def __new__(cls, value: str, retention: str | None):
        member = str.__new__(cls, value)
        member._value_ = value
        member.retention = retention
        return member

    SCHEDULED = ("scheduled", "seed")
    RUNNING = ("running", "seed")
    SUCCEEDED = ("succeeded", "result")
    FAILED = ("failed", "result")
    ERROR = ("error", "result")
    CANCELLED = ("cancelled", "result")
    SKIPPED = ("skipped", "result")
    UNKNOWN = ("unknown", None)
