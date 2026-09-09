"""Verify exact source inputs and run only the inactive candidate's local checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from budgets import PHASES
from execution_inputs import file_sha256, python_identity, records_sha256, regular_file, tree_records, verify_go
from isolation import isolated_environment, namespace_receipt, safe_git, validate_state_root
from fixture import fixture_identity, source_digest


HERE = Path(__file__).resolve().parent


def verify_inputs(source: Path) -> dict:
    receipt = json.loads((HERE / "inputs.json").read_text())
    head = safe_git(source, "rev-parse", "HEAD").decode().strip()
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
        raw = safe_git(source, *arguments)
        changed.update(name.decode() for name in raw.split(b"\0") if name)
    if changed != set(receipt):
        raise RuntimeError("Candidate has missing/extra edits or untracked files; preserve and inspect it.")
    for name, expected in receipt.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Candidate file differs from the maintained patch: {name}")
    return hashlib.sha256((HERE / "native-intent.patch").read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    """Candidate evidence is exclusive; the parent terminal receipt stays separate."""
    with path.open("x") as output:
        output.write(json.dumps(value, indent=2) + "\n")


def input_identity(source: Path, fixture: Path, proof: dict, go: Path) -> dict:
    receipt = verify_inputs(source)
    patch_sha = verify_candidate(source)
    prerequisites = json.loads((HERE / "prerequisites.json").read_text())
    toolchain = verify_go(
        Path(proof["toolchain"]), Path(proof["go_archive"]), go, prerequisites["linux_arm64_go"],
    )
    return {
        "source_sha": receipt["source_sha"], "patch_sha256": patch_sha,
        "frozen_inputs": receipt["sha256"], "source_full_sha256": source_digest(source),
        "go": toolchain, "avibe_fixture": fixture_identity(fixture, receipt),
        "recipe_tree_sha256": source_digest(HERE),
        "python_trusted_setup": python_identity(Path(proof["python_env"]), fixture),
        "setup_only": {"uv": prerequisites["linux_arm64_uv"],
                       "scope": "declared setup prerequisite, not executed or archive-attested by this phase"},
    }


def build_artifacts(root: Path) -> dict:
    records = tree_records(root)
    records.pop("build.json", None)
    return records


def select_build(root: Path, proof: dict, identity: dict) -> dict:
    selection = proof["selected_build"]
    if selection is None or root != Path(selection["output"]):
        raise RuntimeError("Wire must select the envelope's explicit read-only prior build.")
    if (root / "bin").is_symlink() or not (root / "bin").is_dir():
        raise RuntimeError("Build binary directory must be a real directory.")
    with regular_file(root / "build.json") as source:
        raw = source.read()
    previous = json.loads(raw)
    if previous["invocation"] != selection["receipt"] or previous["inputs"] != identity:
        raise RuntimeError("Selected build is stale or belongs to different verified inputs.")
    if previous["artifacts_sha256"] != records_sha256(build_artifacts(root)):
        raise RuntimeError("Selected build output was substituted or changed.")
    binary_sha = file_sha256(root / "bin/cli-proxy-api")
    if previous["binary_sha256"] != binary_sha:
        raise RuntimeError("Selected diagnostic binary changed.")
    return {
        **selection, "build_receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "binary_sha256": binary_sha, "artifacts_sha256": previous["artifacts_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("apply", "test", "build", "wire"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--fixture", type=Path, help="Complete verified Avibe commit export; required for execution.")
    parser.add_argument("--build", type=Path, help="Explicit previous build output, required only for wire.")
    args = parser.parse_args()
    # Public consumers refuse protected aliases BEFORE any setup/output write.
    state = validate_state_root(args.state)
    source = validate_state_root(args.source).resolve(strict=True)
    if source == state or state.is_relative_to(source):
        raise ValueError("Evidence/cache state must be outside the source checkout.")
    proof = namespace_receipt() if args.phase != "apply" else None
    verify_inputs(source)
    if args.phase == "apply":
        status = safe_git(source, "status", "--porcelain")
        if status:
            raise RuntimeError("Checkout is dirty; preserve it and use a fresh exact-base checkout.")
        patch = HERE / "native-intent.patch"
        for options in (["--check"], []):
            safe_git(source, "apply", *options, str(patch))
        verify_candidate(source)
        return
    # Confirm that the maintained candidate, including its Go tests, is applied.
    safe_git(source, "apply", "--reverse", "--check", str(HERE / "native-intent.patch"))
    verify_candidate(source)
    budget = PHASES[args.phase]
    expected_network = "none" if args.phase == "build" else "loopback"
    if (proof["phase"] != args.phase or proof["budget"] != budget.receipt()
            or Path(proof["source"]) != source or Path(proof["state"]) != state
            or Path(proof["recipe"]) != HERE
            or proof["network"] != expected_network):
        raise RuntimeError("Phase/source/state/network/budget does not match the parent envelope.")
    output = validate_state_root(Path(proof["output"])).resolve(strict=True)
    if output == state or output.is_relative_to(state) or state.is_relative_to(output):
        raise RuntimeError("Per-invocation output and shared cache must be disjoint.")
    if args.fixture is None or args.fixture.resolve(strict=True) != Path(proof["fixture"]):
        raise ValueError("Select the complete frozen read-only Avibe fixture.")
    fixture = args.fixture.resolve(strict=True)
    if (args.phase == "wire") != (args.build is not None):
        raise ValueError("Only wire must select an explicit prior build.")
    go_name = shutil.which("go")
    if not go_name:
        raise RuntimeError("The exact pinned Go extraction is required.")
    go = Path(go_name).resolve(strict=True)
    write_json(output / "invocation.json", {"parent_receipt": proof["receipt"], "phase": args.phase,
                                           "budget": budget.receipt()})
    identity = input_identity(source, fixture, proof, go)
    write_json(output / "inputs-before.json", identity)
    env = isolated_environment(output, cache=state, go=go)
    version = subprocess.check_output(
        [str(go), "version"], cwd=source, env=env, text=True, timeout=budget.input_seconds,
    )
    if version.strip() != f"go version {identity['go']['version']} linux/arm64":
        raise RuntimeError(f"Wrong Go version: {version.strip()}")
    with (output / "go-version.txt").open("x") as stream:
        stream.write(version)
    selected = None
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
        (output / "bin").mkdir()
        commands = [[go, "build", "-mod=readonly", "-p=1", "-trimpath", "-buildvcs=false",
                     "-o", str(output / "bin/cli-proxy-api"), "./cmd/server"]]
    else:
        build_root = args.build.resolve(strict=True)
        selected = select_build(build_root, proof, identity)
        wire_state = output / "wire"
        commands = [[sys.executable, str(HERE / "wire_matrix.py"),
                     "--binary", str(build_root / "bin/cli-proxy-api"), "--state", str(wire_state),
                     "--fixture", str(fixture), "--fixture-sha256", identity["avibe_fixture"]["source_sha256"]]]
    if len(commands) != budget.commands:
        raise RuntimeError("Command plan does not match its complete phase allowance.")
    try:
        for index, command in enumerate(commands):
            log = output / f"{args.phase}-{index}.log"
            with log.open("xb") as stream:
                result = subprocess.run(
                    command, cwd=source, env=env, stdin=subprocess.DEVNULL, close_fds=True,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=budget.command_seconds,
                )
            print(f"{args.phase}-{index}: exit {result.returncode}; evidence: {log}", flush=True)
            if result.returncode:
                raise SystemExit(result.returncode)
    finally:
        # Failure evidence and all prior runs remain in place. The parent owns
        # the terminal verdict; these files do not impersonate that receipt.
        after = input_identity(source, fixture, proof, go)
        write_json(output / "inputs-after.json", after)
        if after != identity:
            raise RuntimeError("Execution inputs changed during the phase.")
        if selected is not None and select_build(build_root, proof, identity) != selected:
            raise RuntimeError("Selected build changed during wire execution.")
    if args.phase == "build":
        write_json(output / "build.json", {
            "invocation": proof["receipt"], "inputs": identity,
            "binary_sha256": file_sha256(output / "bin/cli-proxy-api"),
            "artifacts_sha256": records_sha256(build_artifacts(output)),
            "command": [str(part) for part in commands[0][1:]],
        })
    elif args.phase == "wire":
        wire_identity = {
            "invocation": proof["receipt"], "inputs": identity, "selected_build": selected,
            "artifacts": {
                name: file_sha256(wire_state / name)
                for name in ("matrix.json", "integration.json", "lifecycle.json")
            },
        }
        write_json(output / "wire.json", wire_identity)


if __name__ == "__main__":
    main()
