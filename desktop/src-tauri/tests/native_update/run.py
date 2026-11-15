"""Run the signed rc2 -> rc3 smoke through the registered dual-platform workflow.

The existing workflow still assembles its hermetic fixture before this entrypoint;
that setup is intentionally ignored. The actual observation below downloads only
public signed rc2/rc3 release assets and uses the real installed Desktop panel.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import sys


if os.name == "nt":
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "pywinauto==0.6.9", "pillow==12.0.0"],
        check=True,
        timeout=180,
    )

script = Path(__file__).resolve().parents[3] / "scripts" / "test-signed-rc-update-panel.py"
os.environ.setdefault("SMOKE_ROOT", os.environ.get("ACCEPTANCE_ROOT", str(Path(os.environ["RUNNER_TEMP"]) / "signed-rc-smoke")))
runpy.run_path(str(script), run_name="__main__")
