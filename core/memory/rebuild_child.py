"""Child-only bootstrap for one scrubbed EverOS cascade rebuild."""

from __future__ import annotations

import os
import runpy
import signal


_EVEROS_CLI_MODULE = "everos.entrypoints.cli.main"


def _set_private_umask() -> None:
    os.umask(0o077)


def _await_parent_ownership() -> None:
    os.kill(os.getpid(), signal.SIGSTOP)


def install_error_scrubbers() -> None:
    """Import scrubbers only after private child process setup is complete."""

    from core.memory.everos_insight import install_error_scrubbers as install

    install()


def main() -> int:
    """Install persistence scrubbers before delegating to the EverOS CLI."""

    _set_private_umask()
    _await_parent_ownership()
    install_error_scrubbers()
    runpy.run_module(_EVEROS_CLI_MODULE, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry point
    raise SystemExit(main())
