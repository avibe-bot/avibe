"""Service-start evidence for the watchdog's one-shot warm GC freeze."""

import gc
import sys


# An embedded controller with no observation keeps the empty-generation guard.
_startup_count: int | None = None


def observe_service_startup() -> None:
    """Sample once, before the controller loop, never from a later watchdog."""
    global _startup_count
    if _startup_count is not None:
        return
    _startup_count = 0
    count = gc.get_freeze_count()
    if not count or sys.implementation.name != "cpython":
        return
    # Free-threaded collectors do not all exclude frozen objects from
    # get_objects(). Don't use that observation to authorize a nonempty heap.
    from sysconfig import get_config_var

    if get_config_var("Py_GIL_DISABLED"):
        return
    # In CPython's generational collector a foreign freeze takes sys, a
    # process-lifetime tracked module, OUT of the collectible generations.
    # Interpreter initialization can populate the permanent generation while
    # leaving sys collectible. Don't learn a site hook's foreign freeze.
    try:
        collectible = gc.get_objects()
    except Exception:
        return  # e.g. an audit hook denied inspection; keep the safe default
    if any(obj is sys for obj in collectible):
        _startup_count = count


def can_freeze() -> bool:
    # No heap snapshot in the collector, on the loop or at watchdog creation.
    count = gc.get_freeze_count()
    return not count or count == _startup_count
