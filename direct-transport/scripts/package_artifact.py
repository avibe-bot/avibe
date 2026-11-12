#!/usr/bin/env python3
"""Package a qualified binary and notices; publication is a separate guarded workflow."""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cargo_notices import collect_notices  # noqa: E402
from scripts.direct_transport_notices import policy_bytes  # noqa: E402


def collect_licenses(target, temp):
    licenses = temp / "direct-transport-licenses" / "LICENSES"
    policy = temp / "sidecar-license-policy.json"
    policy.write_bytes(policy_bytes())
    result = collect_notices(
        Path("Cargo.lock"), [target], os.environ["RUST_TOOLCHAIN"],
        "avibe-direct-transport", policy, licenses,
    )
    shutil.copyfile(policy, licenses / "policy.json")
    graph = (temp / "feature-graph.txt").read_text()
    coordinates = set(re.findall(r"(?:^|[\s─├└│])([A-Za-z0-9_-]+) v([^\s]+)", graph, re.M))
    if {(p["name"], p["version"]) for p in result["packages"]} != coordinates:
        raise ValueError("notice inventory differs from qualified feature graph")
    sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip())
    rust = sysroot / "share/doc/rust"
    for name in ("COPYRIGHT.html", "licenses/Apache-2.0.txt", "licenses/MIT.txt"):
        if not (rust / name).is_file():
            raise ValueError(f"missing Rust distribution notice: {name}")
    dest = licenses / ("rust-" + os.environ["RUST_TOOLCHAIN"])
    dest.mkdir()
    shutil.copytree(rust / "licenses", dest / "licenses")
    for name in ("COPYRIGHT.html", "COPYRIGHT-library.html"):
        if (rust / name).is_file():
            shutil.copyfile(rust / name, dest / name)
    return licenses


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
    licenses = collect_licenses(target, temp)
    manifest = {
        "name": "avibe-direct-transport",
        "version": tomllib.loads(Path("Cargo.toml").read_text())["package"]["version"],
        "target": target,
        "source_sha": os.environ["GITHUB_SHA"],
        "pull_request_head_sha": os.environ["SOURCE_HEAD_SHA"],
        "direct_dependencies": tomllib.loads(Path("Cargo.toml").read_text())["dependencies"],
        "binary": binary.name,
        "binary_sha256": sha(binary),
        "binary_bytes": binary.stat().st_size,
        "cargo_lock_sha256": sha(Path("Cargo.lock")),
        "features": features,
        "rustc": subprocess.check_output(["rustc", "--version", "--verbose"], text=True).strip(),
        "cargo": subprocess.check_output(["cargo", "--version"], text=True).strip(),
        "runner": platform.platform(),
        "release_build_seconds": int(os.environ["BUILD_SECONDS"]),
        "distribution": "qualified-workflow-artifact",
        "licenses_provenance_sha256": sha(licenses / "provenance.json"),
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
        "workflow_run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "patched_dependencies": False,
        "transport_liveness": {"idle_timeout_ms": 6000, "keep_alive_ms": 1500},
        "qualification": {
            "native_contract_tests": "passed",
            "resolved_feature_graph": "passed",
            "linux_static": "passed" if "linux" in target else "not-applicable",
            "linux_idle_and_outage_tolerance": "passed" if "linux" in target else "not-applicable",
            "linux_traffic_and_address_changes": "passed" if "linux" in target else "not-applicable",
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
        tar.add(licenses, arcname="LICENSES")
    (out / (archive.name + ".sha256")).write_text(f"{sha(archive)}  {archive.name}\n")
    summary = f"{target}: binary {binary.stat().st_size} bytes; archive {archive.stat().st_size} bytes; release build {os.environ['BUILD_SECONDS']} s\n"
    print(summary, end="")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
        stream.write(summary + "\n")


if __name__ == "__main__":
    main()
