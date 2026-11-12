"""Run the native cookie probe with disposable HOME/XDG/WebView state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="avibe-cookie-probe-"))
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "__CF_USER_TEXT_ENCODING")
        if key in os.environ
    }
    for key, directory in {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
        "TMPDIR": "tmp",
    }.items():
        path = root / directory
        path.mkdir()
        environment[key] = str(path)
    environment["AVIBE_COOKIE_PROBE_ROOT"] = str(root)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w") as log:
        log.write(f"probe root: {root}\n")
        log.flush()
        # subprocess.run terminates only this owned process on timeout.
        completed = subprocess.run(
            [str(args.executable.resolve())],
            env=environment,
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=45,
            check=False,
        )
    print(args.log.read_text(), end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
