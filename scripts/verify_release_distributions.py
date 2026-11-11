#!/usr/bin/env python3
"""Run shipped-byte assertions and three isolated install checks on one runner."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile

INSTALL_TESTS = (
    "test_installed_wheel_imports_and_cli_without_memory",
    "test_installed_wheel_mirrors_complete_builtin_skills_snapshot",
    "test_installed_sdist_mirrors_complete_builtin_skills_snapshot",
)


def run_contracts(contract: Path) -> int:
    contract = contract.resolve()
    # Run every other assertion, including any future archive/metadata tests.
    checked = subprocess.run([
        sys.executable, "-m", "pytest", str(contract), "-v",
        "-k", "not (" + " or ".join(INSTALL_TESTS) + ")",
    ], check=False)
    if checked.returncode:
        return checked.returncode

    with tempfile.TemporaryDirectory(prefix="release-install-checks-") as temporary:
        root = Path(temporary)

        def install_check(name: str) -> int:
            lane = root / name
            home = lane / "home"
            temp = lane / "tmp"
            home.mkdir(parents=True)
            temp.mkdir()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AVIBE_CALLER_") and key not in {
                       "AVIBE_SESSION_ID", "AVIBE_RUN_ID", "VIBE_INTERNAL_DISPATCH_SOCKET",
                   }}
            env.update({key: str(home / suffix) for key, suffix in {
                "HOME": "", "AVIBE_HOME": ".avibe", "XDG_CONFIG_HOME": ".config",
                "XDG_DATA_HOME": ".local/share", "XDG_CACHE_HOME": ".cache",
                "XDG_STATE_HOME": ".local/state", "CODEX_HOME": ".codex",
                "CLAUDE_CONFIG_DIR": ".claude",
            }.items()})
            env.update({key: str(temp) for key in ("TMPDIR", "TMP", "TEMP")})
            with (lane / "pytest.log").open("w+") as log:
                try:
                    result = subprocess.run([
                        sys.executable, "-m", "pytest", f"{contract}::{name}", "-v",
                        "--basetemp", str(lane / "pytest"),
                    ], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600, check=False)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    log.write("\nInstall verification exceeded 600 seconds.\n")
                    code = 1
                log.seek(0)
                print(f"::group::{name}\n{log.read()}::endgroup::", flush=True)
            return code

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(install_check, INSTALL_TESTS))
        return int(any(results))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    return run_contracts(parser.parse_args().contract)


if __name__ == "__main__":
    raise SystemExit(main())
