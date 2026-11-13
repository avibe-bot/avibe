"""Run the real native/UI/backend/Runtime chain with explicit immutable inputs.

Use the Runtime candidate's Python environment. Every child receives test-owned
HOME/XDG/TMP/Avibe state; the tests retain logs and stop only their own processes.
The backend checkout must already contain the built, combined B1/B2 candidate.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    for name in ("runtime-checkout", "runtime-ui", "backend-checkout", "native-probe", "log"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--runtime-head", required=True)
    parser.add_argument("--backend-head", required=True)
    parser.add_argument("--updater", action="store_true")
    args, pytest_args = parser.parse_known_args()
    # Even a failing pre-install assertion must hit the actual installer's
    # non-bundle rejection before it can stage, unpack or replace an app.
    assert not any(part.endswith(".app") for part in args.native_probe.resolve().parts), "native fixture must stay outside an application bundle"
    for checkout, head in (
        (args.runtime_checkout, args.runtime_head),
        (args.backend_checkout, args.backend_head),
    ):
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == head
        assert not subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=checkout)
    root = Path(tempfile.mkdtemp(prefix="desktop-instance-chain-"))
    env = {key: os.environ[key] for key in ("PATH", "LANG", "__CF_USER_TEXT_ENCODING") if key in os.environ}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR", "AVIBE_HOME"):
        path = root / key.lower()
        path.mkdir()
        env[key] = str(path)
    fixture = Path(__file__).resolve().parent
    env.update(
        DESKTOP_COMBINED_CHECKOUT=str(args.backend_checkout.resolve()),
        DESKTOP_COMBINED_HEAD=args.backend_head,
        DESKTOP_R1_HEAD=args.runtime_head,
        DESKTOP_RUNTIME_UI=str(args.runtime_ui.resolve()),
        DESKTOP_NATIVE_PROBE=str(args.native_probe.resolve()),
        PYTHONPATH=str(args.runtime_checkout.resolve()) + os.pathsep + str(fixture),
    )
    if args.updater:
        env["DESKTOP_FIXTURE_UPDATER"] = "1"
    command = [
        sys.executable, "-m", "pytest", "-p", "tests.conftest", "--rootdir", str(args.runtime_checkout.resolve()),
        str(fixture / "test_native_chain.py"), "-q", "-s", *pytest_args,
    ]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w") as log:
        result = subprocess.run(command, cwd=args.runtime_checkout, env=env, stdout=log, stderr=subprocess.STDOUT)
    record = {
        "runtime_head": args.runtime_head, "backend_head": args.backend_head,
        "native_binary_sha256": hashlib.sha256(args.native_probe.read_bytes()).hexdigest(),
        "command": command, "environment": env, "state_root": str(root), "exit_code": result.returncode,
    }
    args.log.with_suffix(".record.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"exit_code": result.returncode, "state_root": str(root), "log": str(args.log)}))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
