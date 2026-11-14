"""Bounded Windows build of the exact Avibe-pinned source plus existing patch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Hosted Windows only")
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("No signing/build credentials admitted")
    root = Path(os.environ["RUNNER_TEMP"]) / "computer-use-spike"
    evidence = root / "evidence"
    manifest = json.loads(Path("desktop/cua-driver/sources.json").read_text())
    source = manifest["driver"]["source"]
    archive = root / source["archive"]
    with urllib.request.urlopen(source["url"], timeout=90) as response:
        archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != source["sha256"]:
        raise RuntimeError("Pinned source hash mismatch")
    patch = Path("desktop/cua-driver") / manifest["driver"]["patch"]["file"]
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["driver"]["patch"]["sha256"]:
        raise RuntimeError("Pinned patch hash mismatch")
    with tarfile.open(archive) as bundle:
        bundle.extractall(root, filter="data")
    tree = root / source["root"]
    subprocess.run(["git", "apply", "--check", str(patch.resolve())], cwd=tree, check=True, timeout=30)
    subprocess.run(["git", "apply", str(patch.resolve())], cwd=tree, check=True, timeout=30)
    workspace = tree / "libs/cua-driver/rust"
    command = ["cargo", "build", "--locked", "-j", "2", "-p", "cua-driver", "--bin", "cua-driver", "--target", "x86_64-pc-windows-msvc"]
    started = time.monotonic()
    result = {"source_commit": manifest["driver"]["source_commit"], "patch_sha256": manifest["driver"]["patch"]["sha256"],
              "full_patch_applied": True, "profile": "debug", "command": command}
    try:
        with (evidence / "patched-build.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=workspace, stdout=log, stderr=subprocess.STDOUT)
            try:
                returncode = process.wait(timeout=720)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], timeout=30, check=False, stdout=log, stderr=subprocess.STDOUT)
                process.wait(timeout=30)
                raise
        result["returncode"] = returncode
        executable = workspace / "target/x86_64-pc-windows-msvc/debug/cua-driver.exe"
        if returncode == 0:
            result["binary_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
            (root / "patched-executable.txt").write_text(str(executable), encoding="utf-8")
    except subprocess.TimeoutExpired:
        result["timeout"] = True
    finally:
        result["seconds"] = round(time.monotonic()-started, 1)
        (evidence / "patched-build.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
