"""Scope the hermetic controller's native-CLI inventory to the processes its harness started.

Native credential migration refuses while a native CLI of the same user runs
(``core.backend_restart.native_cli_processes``). A developer's machine runs
real Claude, Codex and OpenCode processes, so the unscoped scan would refuse
every migration scenario there. Only that scan is scoped, to the descendants
of the pytest process that launched this controller: Avibe-owned runtimes and
anything a scenario starts itself stay visible, so "native busy" stays
testable. Nothing else's view of the process table changes.

This module runs before the controller's own directory is on ``sys.path``, so
it must not import Avibe code.
"""

import os
import sys

_SCANNER = "core.backend_restart"


def _scope_native_inventory_to_harness() -> None:
    import psutil

    harness = os.getppid()
    process_iter = psutil.process_iter

    def scoped(*args, **kwargs):
        processes = process_iter(*args, **kwargs)
        if sys._getframe(1).f_globals.get("__name__") != _SCANNER:
            return processes
        owned = {child.pid for child in psutil.Process(harness).children(recursive=True)}
        return (process for process in processes if process.pid in owned)

    scoped.cache_clear = process_iter.cache_clear
    psutil.process_iter = scoped

_scope_native_inventory_to_harness()


# Temporary CI-only location probe. No exception text, locals, source lines,
# request data or absolute paths are collected. Only B8 opts in.
if os.environ.get("B8_TRACE_SITES") == "1" and hasattr(sys, "monitoring"):
    from pathlib import Path

    _paths = ("core/handlers/model_hub/service.py", "vibe/model_hub_runtime/adapter.py",
              "vibe/model_hub_runtime/supervisor.py", "vibe/model_hub_runtime/client.py",
              "vibe/model_hub_runtime/state.py", "vibe/model_hub_runtime/config.py")
    _seen = set()

    def _record_site(code, offset, exc):
        if isinstance(exc, (StopIteration, StopAsyncIteration, GeneratorExit)):
            return
        path = next((p for p in _paths if code.co_filename.endswith('/' + p)), None)
        if path is None or len(_seen) >= 256:
            return
        line = next((n for start, end, n in code.co_lines() if start <= offset < end), None)
        if line is None or (path, line) in _seen:
            return
        _seen.add((path, line))
        with (Path(os.environ['AVIBE_HOME']) / 'b8-sites.txt').open('a') as report:
            print(f'{path}:{line}', file=report)

    _mon = sys.monitoring
    _mon.use_tool_id(3, 'b8-ci-location-only')
    for _event in (_mon.events.RAISE, _mon.events.PY_UNWIND):
        _mon.register_callback(3, _event, _record_site)
    _mon.set_events(3, _mon.events.RAISE | _mon.events.PY_UNWIND)
