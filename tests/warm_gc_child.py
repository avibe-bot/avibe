"""Exercise the warm-freeze state machine with a real GC in a disposable interpreter."""

import gc
import sys
from pathlib import Path


def main(case):
    # Run with -I -S so site hooks and the pytest process are not participants.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if case == "before_startup":
        gc.freeze()

    from core.warm_gc_baseline import observe_service_startup

    observe_service_startup()
    if case == "before_watchdog":
        gc.freeze()
    from core.loop_watchdog import EventLoopWatchdog

    now = [0.0]
    watchdog = EventLoopWatchdog(None, monotonic=lambda: now[0])
    prior = gc.get_freeze_count()
    armed = watchdog.arm_warm_freeze()
    if case in {"before_startup", "before_watchdog"}:
        assert armed == "off" and watchdog.rollback_warm_freeze() == "off"
        assert gc.get_freeze_count() > 0
        return
    assert armed == "armed", (prior, armed)
    if case == "after_arm":
        gc.freeze()
    now[0] = 7201.0
    gc.callbacks.append(watchdog._gc_callback)
    try:
        gc.collect(2)  # real full collection with real callback metadata
    finally:
        gc.callbacks.remove(watchdog._gc_callback)
    watchdog._answer()
    if case == "after_arm":
        assert watchdog._freeze_state == "armed"
        assert watchdog.rollback_warm_freeze() == "retired" and gc.get_freeze_count() > 0
    else:
        assert watchdog._freeze_state == "frozen", (prior, watchdog._freeze_state)
        assert gc.get_freeze_count() > prior
        assert watchdog.rollback_warm_freeze() == "retired" and gc.get_freeze_count() == 0
        assert watchdog.arm_warm_freeze() == "retired"


if __name__ == "__main__":
    main(sys.argv[1])
