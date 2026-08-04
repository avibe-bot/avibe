"""Version-coupled EverOS insight adapter."""

from .patches import RecorderHandle, prepare_call_recorder
from .reader import MemoryInsightPaths, MemoryInsightReader

__all__ = [
    "MemoryInsightPaths",
    "MemoryInsightReader",
    "RecorderHandle",
    "prepare_call_recorder",
]
