"""Verify exact source inputs and run only the inactive candidate's local checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from isolation import isolated_environment, sandbox_prefix


HERE = Path(__file__).resolve().parent


def verify_inputs(source: Path) -> dict:
    receipt = json.loads((HERE / "inputs.json").read_text())
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if head != receipt["source_sha"]:
        raise RuntimeError(f"Wrong engine base: {head}")
    for name, expected in receipt["sha256"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Frozen input changed: {name}")
    return receipt


def verify_candidate(source: Path) -> str:
    receipt = json.loads((HERE / "patched-files.json").read_text())
    changed = set()
    for arguments in (["diff", "HEAD", "--name-only", "-z"], ["ls-files", "--others", "-z"]):
        raw = subprocess.check_output(["git", "-C", str(source), *arguments])
        changed.update(name.decode() for name in raw.split(b"\0") if name)
    if changed != set(receipt):
        raise RuntimeError("Candidate has missing/extra edits or untracked files; preserve and inspect it.")
    for name, expected in receipt.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Candidate file differs from the maintained patch: {name}")
    return hashlib.sha256((HERE / "native-intent.patch").read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("apply", "test", "build", "wire"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    state = args.state.resolve()
    if source == state or state.is_relative_to(source):
        raise ValueError("Evidence/cache state must be outside the source checkout.")
    receipt = verify_inputs(source)
    if args.phase == "apply":
        status = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain"], text=True,
        )
        if status:
            raise RuntimeError("Checkout is dirty; preserve it and use a fresh exact-base checkout.")
        patch = HERE / "native-intent.patch"
        for options in (["--check"], []):
            subprocess.run(["git", "-C", str(source), "apply", *options, str(patch)], check=True)
        verify_candidate(source)
        return
    # Confirm that the maintained candidate, including its Go tests, is applied.
    subprocess.run(
        ["git", "-C", str(source), "apply", "--reverse", "--check", str(HERE / "native-intent.patch")],
        check=True,
    )
    patch_sha = verify_candidate(source)
    env = isolated_environment(state)
    prefix = sandbox_prefix(state)
    go = shutil.which("go")
    if not go:
        raise RuntimeError("Go is required; see README.md for isolated prerequisite download.")
    version = subprocess.check_output([*prefix, go, "version"], cwd=source, env=env, text=True)
    if version.split()[2] != receipt["go_version"]:
        raise RuntimeError(f"Wrong Go version: {version.strip()}")
    build_identity = {"source_sha": receipt["source_sha"], "patch_sha256": patch_sha,
                      "frozen_inputs": receipt["sha256"], "go_version": version.strip()}
    if args.phase == "test":
        commands = [
            [go, "test", "-mod=readonly", "-p=1", "-count=1",
             "./internal/thinking/...", "./internal/modelconfig",
             "./internal/runtime/executor/helps", "./sdk/cliproxy/auth"],
            [go, "test", "-mod=readonly", "-p=1", "-count=1",
             "-run", "^TestThinking|^TestSummaryIntent", "./test"],
            [go, "test", "-mod=readonly", "-p=1", "-count=1",
             "./internal/runtime/executor"],
        ]
    elif args.phase == "build":
        (state / "bin").mkdir(exist_ok=True)
        commands = [[go, "build", "-mod=readonly", "-p=1", "-trimpath", "-buildvcs=false",
                     "-o", str(state / "bin/cli-proxy-api"), "./cmd/server"]]
    else:
        previous_build = json.loads((state / "build.json").read_text())
        for name, value in build_identity.items():
            if previous_build[name] != value:
                raise RuntimeError("Binary is not from the current frozen candidate; rebuild before wire tests.")
        if previous_build["binary_sha256"] != hashlib.sha256((state / "bin/cli-proxy-api").read_bytes()).hexdigest():
            raise RuntimeError("Candidate binary changed after build.")
        wire_state = Path(tempfile.mkdtemp(prefix="wire-", dir=state)) / "run"
        commands = [[sys.executable, str(HERE / "wire_matrix.py"),
                     "--binary", str(state / "bin/cli-proxy-api"), "--state", str(wire_state)]]
    for index, command in enumerate(commands):
        log = state / f"{args.phase}-{index}.log"
        with log.open("wb") as output:
            result = subprocess.run(
                [*prefix, *command], cwd=source, env=env,
                stdout=output, stderr=subprocess.STDOUT, timeout=600,
            )
        print(f"{args.phase}-{index}: exit {result.returncode}; evidence: {log}", flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
    verify_inputs(source)
    verify_candidate(source)
    if args.phase == "build":
        build_identity["binary_sha256"] = hashlib.sha256((state / "bin/cli-proxy-api").read_bytes()).hexdigest()
        build_identity["command"] = commands[0][1:]
        (state / "build.json").write_text(json.dumps(build_identity, indent=2) + "\n")


if __name__ == "__main__":
    main()
