"""Fixed, fail-closed loader for the optional in-process Memory runtime."""

from __future__ import annotations

import importlib
from typing import Any

from vibe.memory_contract import (
    MemoryPluginIncompatibleError,
    MemoryPluginUnavailableError,
)


MEMORY_RUNTIME_ENTRYPOINT = "core.memory.runtime"
MEMORY_RUNTIME_PROTOCOL_VERSION = 1
_PROTOCOL_ATTR = "MEMORY_RUNTIME_PROTOCOL_VERSION"


def load_memory_runtime(
    config: Any,
    *,
    allow_disabled: bool = False,
    **runtime_kwargs: Any,
) -> Any:
    """Load and construct the fixed Memory runtime for an enabled snapshot.

    This module intentionally imports no implementation module at import time.
    The entrypoint, protocol comparison, and constructor are deliberately fixed
    so a missing/broken optional implementation cannot affect core startup.
    """

    if not bool(getattr(config, "enabled", False)) and not allow_disabled:
        return None
    try:
        implementation = importlib.import_module(MEMORY_RUNTIME_ENTRYPOINT)
    except Exception as exc:
        raise MemoryPluginUnavailableError(
            "Memory implementation is unavailable"
        ) from exc
    if getattr(implementation, _PROTOCOL_ATTR, None) != MEMORY_RUNTIME_PROTOCOL_VERSION:
        raise MemoryPluginIncompatibleError(
            "Memory implementation protocol is incompatible"
        )
    factory = getattr(implementation, "create_memory_runtime", None)
    if not callable(factory):
        raise MemoryPluginUnavailableError(
            "Memory implementation constructor is unavailable"
        )
    try:
        runtime = factory(config, **runtime_kwargs)
    except Exception as exc:
        raise MemoryPluginUnavailableError(
            "Memory implementation could not be constructed"
        ) from exc
    if runtime is None:
        raise MemoryPluginUnavailableError(
            "Memory implementation returned no runtime"
        )
    try:
        module = getattr(runtime, "module", None)
        close = getattr(runtime, "close", None)
        has_available = hasattr(runtime, "available")
    except Exception as exc:
        raise MemoryPluginIncompatibleError(
            "Memory implementation runtime contract is incompatible"
        ) from exc
    if module is None or not callable(close) or not has_available:
        raise MemoryPluginIncompatibleError(
            "Memory implementation runtime contract is incompatible"
        )
    return runtime
