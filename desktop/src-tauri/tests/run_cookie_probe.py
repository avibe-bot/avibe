"""Run a native WebView probe with disposable HOME/XDG/WebView state."""

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
    parser.add_argument("--root-env", default="AVIBE_COOKIE_PROBE_ROOT", help="variable that names the test root")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="extra probe variable")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="avibe-webview-probe-"))
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
    environment[args.root_env] = str(root)
    environment.update(item.split("=", 1) for item in args.set)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w") as log:
        log.write(f"probe root: {root}\n")
        log.flush()
        # subprocess.run terminates only this owned process on timeout.
        try:
            returncode = subprocess.run(
                [str(args.executable.resolve())],
                env=environment,
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            # A stuck probe still says how far it got.
            log.write(f"probe timed out after {args.timeout} seconds\n")
            returncode = 124
    print(args.log.read_text(), end="")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
