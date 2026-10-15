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
