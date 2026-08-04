"""Version-coupled EverOS insight adapter."""

from .patches import install_error_scrubbers, prepare_call_recorder
from .recorder import RecorderHandle
from .reader import MemoryInsightPaths, MemoryInsightReader

__all__ = [
    "MemoryInsightPaths",
    "MemoryInsightReader",
    "RecorderHandle",
    "install_error_scrubbers",
    "prepare_call_recorder",
]
