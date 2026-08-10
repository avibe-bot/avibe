"""Child-only bootstrap for one scrubbed EverOS cascade rebuild."""

from __future__ import annotations

import runpy

from core.memory.everos_insight import install_error_scrubbers


_EVEROS_CLI_MODULE = "everos.entrypoints.cli.main"


def main() -> int:
    """Install persistence scrubbers before delegating to the EverOS CLI."""

    install_error_scrubbers()
    runpy.run_module(_EVEROS_CLI_MODULE, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry point
    raise SystemExit(main())
