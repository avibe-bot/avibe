"""Verify/extract the commissioned Runtime archive before running HTTP tests.

Usage: python prepare-runtime.py MANIFEST ARCHIVE DESTINATION
Prints the verified root; no downloading, installation or running services.
"""
import hashlib
import json
from pathlib import Path
import sys
import tarfile

VERSION = "5e31eda3536db3ea4de018fb253d0ed7d5c69a09"
MANIFEST_SHA = "98725b13df13c206ba689609f5df198bd5cd9b83d3484b58142944a2775e7698"
LINUX_SHA = "ea1079eca7bf532192c72950e1c9cd6f183192fb3c30af6a87f7fa0a63305b6e"


def prepare(manifest_path, archive_path, destination):
    manifest_raw = manifest_path.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != MANIFEST_SHA:
        raise ValueError("Runtime manifest hash mismatch")
    manifest = json.loads(manifest_raw)
    if manifest["runtime_version"] != VERSION or manifest["archives"]["linux-x64"]["sha256"] != LINUX_SHA:
        raise ValueError("Runtime deployment binding mismatch")
    candidates = [entry for entry in manifest["archives"].values() if entry["name"] == archive_path.name]
    if len(candidates) != 1:
        raise ValueError("Runtime archive not in pinned manifest")
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != candidates[0]["sha256"]:
        raise ValueError("Runtime archive hash mismatch")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path) as archive:
        archive.extractall(destination, filter="data")
    if not (destination / "packages/runtime/dist/cli.js").is_file():
        raise ValueError("Runtime archive missing CLI")
    return destination


if __name__ == "__main__":
    print(prepare(*(Path(arg).resolve() for arg in sys.argv[1:])))
