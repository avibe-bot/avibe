"""Fixed, fail-closed loader for the optional in-process Memory runtime."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from vibe.memory_contract import (
    MemoryImplementationIncompatibleError,
    MemoryImplementationUnavailableError,
    MemoryRuntimeBusyError,
)


MEMORY_RUNTIME_ENTRYPOINT = "avibe_memory.runtime"
# Host-owned transport bound used by the list route. Keeping this here lets
# disabled/failing package paths validate cursors without importing the optional
# implementation module.
MEMORY_LIST_CURSOR_MAX_BYTES = 8192


def _resolve_memory_runtime_factory() -> Callable[..., Any]:
    """Import the fixed entrypoint and return its runtime factory."""

    try:
        implementation = importlib.import_module(MEMORY_RUNTIME_ENTRYPOINT)
    except Exception as exc:
        raise MemoryImplementationUnavailableError(
            "Memory implementation is unavailable"
        ) from exc
    try:
        factory = getattr(implementation, "create_memory_runtime", None)
    except Exception as exc:
        raise MemoryImplementationUnavailableError(
            "Memory implementation is unavailable"
        ) from exc
    if not callable(factory):
        raise MemoryImplementationUnavailableError(
            "Memory implementation constructor is unavailable"
        )
    return factory


def probe_memory_runtime_entrypoint() -> None:
    """Validate the runtime entrypoint contract without constructing it."""

    _resolve_memory_runtime_factory()


def load_memory_runtime(
    config: Any,
    *,
    allow_disabled: bool = False,
    **runtime_kwargs: Any,
) -> Any:
    """Load and construct the fixed Memory runtime for an enabled snapshot.

    This module intentionally imports no implementation module at import time.
    The entrypoint and constructor are deliberately fixed. Publishable companion
    packages carry exact reciprocal version pins, while this boundary validates the
    factory and constructed runtime surface so a missing or broken implementation
    cannot affect core startup.
    """

    if not bool(getattr(config, "enabled", False)) and not allow_disabled:
        return None
    factory = _resolve_memory_runtime_factory()
    try:
        runtime = factory(config, **runtime_kwargs)
    except MemoryRuntimeBusyError:
        raise
    except Exception as exc:
        raise MemoryImplementationUnavailableError(
            "Memory implementation could not be constructed"
        ) from exc
    if runtime is None:
        raise MemoryImplementationUnavailableError(
            "Memory implementation returned no runtime"
        )
    try:
        module = getattr(runtime, "module", None)
        close = getattr(runtime, "close", None)
        has_available = hasattr(runtime, "available")
    except Exception as exc:
        raise MemoryImplementationIncompatibleError(
            "Memory implementation runtime contract is incompatible"
        ) from exc
    if module is None or not callable(close) or not has_available:
        raise MemoryImplementationIncompatibleError(
            "Memory implementation runtime contract is incompatible"
        )
    return runtime
