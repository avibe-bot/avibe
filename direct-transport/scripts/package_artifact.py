#!/usr/bin/env python3
"""Package a qualified binary as a workflow artifact, never a release."""

import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    target = sys.argv[1]
    temp = Path(os.environ["RUNNER_TEMP"])
    out = temp / "direct-transport-artifact"
    out.mkdir()
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    binary = Path("target") / target / "release" / ("avibe-direct-transport" + suffix)
    features = json.loads((temp / "feature-evidence.json").read_text())
    manifest = {
        "name": "avibe-direct-transport",
        "version": "0.1.0",
        "target": target,
        "source_sha": os.environ["GITHUB_SHA"],
        "binary": binary.name,
        "binary_sha256": sha(binary),
        "binary_bytes": binary.stat().st_size,
        "cargo_lock_sha256": sha(Path("Cargo.lock")),
        "features": features,
        "rustc": subprocess.check_output(["rustc", "--version", "--verbose"], text=True).strip(),
        "cargo": subprocess.check_output(["cargo", "--version"], text=True).strip(),
        "runner": platform.platform(),
        "release_build_seconds": int(os.environ["BUILD_SECONDS"]),
        "distribution": "workflow-artifact-only",
        "patched_dependencies": False,
        "qualification": {
            "native_contract_tests": "passed",
            "resolved_feature_graph": "passed",
            "linux_static": "passed" if "linux" in target else "not-applicable",
            "private_apple_datapath": "passed-with-positive-control" if "apple" in target else "not-applicable",
        },
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    archive = out / f"avibe-direct-transport-{target}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname=binary.name)
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(temp / "feature-graph.txt", arcname="feature-graph.txt")
        tar.add(temp / "feature-evidence.json", arcname="feature-evidence.json")
        tar.add("Cargo.lock", arcname="Cargo.lock")
    (out / (archive.name + ".sha256")).write_text(f"{sha(archive)}  {archive.name}\n")
    summary = f"{target}: binary {binary.stat().st_size} bytes; archive {archive.stat().st_size} bytes; release build {os.environ['BUILD_SECONDS']} s\n"
    print(summary, end="")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
        stream.write(summary + "\n")


if __name__ == "__main__":
    main()
