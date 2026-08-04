"""Version-coupled EverOS insight adapter."""

from .patches import RecorderHandle, install_error_scrubbers, prepare_call_recorder
from .reader import MemoryInsightPaths, MemoryInsightReader

__all__ = [
    "MemoryInsightPaths",
    "MemoryInsightReader",
    "RecorderHandle",
    "install_error_scrubbers",
    "prepare_call_recorder",
]
