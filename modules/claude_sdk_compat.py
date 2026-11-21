"""Compatibility helpers for environments without claude_agent_sdk installed.

The SDK itself loads on first use of one of its names (PEP 562 ``__getattr__``),
not when this module is imported: a controller that never runs the Claude Code
backend never pays for it. Read SDK names as attributes of this module at the
point of use (``claude_sdk.ClaudeSDKClient``), not with ``from ... import`` at
module level, which would load the SDK at import time again.
"""

from __future__ import annotations

from typing import Any

CLAUDE_SDK_MAX_BUFFER_SIZE = 16 * 1024 * 1024
CLAUDE_SDK_BUFFER_ERROR_TEXT = "JSON message exceeded maximum buffer size"


def is_claude_sdk_buffer_error(error: object) -> bool:
    """Return True when Claude SDK failed on a single oversized JSON message."""
    return CLAUDE_SDK_BUFFER_ERROR_TEXT in str(error)


def __getattr__(name: str) -> Any:
    # The import system probes modules for dunder names (``__path__`` on every
    # ``from ... import``); those must never load the SDK.
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from modules import claude_sdk_runtime

    try:
        value = getattr(claude_sdk_runtime, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    globals()[name] = value  # resolved once; later reads are plain attribute reads
    return value
