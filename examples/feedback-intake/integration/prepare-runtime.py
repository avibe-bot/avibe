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
    assert hashlib.sha256(manifest_raw).hexdigest() == MANIFEST_SHA
    manifest = json.loads(manifest_raw)
    assert manifest["runtime_version"] == VERSION
    assert manifest["archives"]["linux-x64"]["sha256"] == LINUX_SHA
    candidates = [entry for entry in manifest["archives"].values() if entry["name"] == archive_path.name]
    assert len(candidates) == 1
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == candidates[0]["sha256"]
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path) as archive:
        archive.extractall(destination, filter="data")
    assert (destination / "packages/runtime/dist/cli.js").is_file()
    return destination


if __name__ == "__main__":
    print(prepare(*(Path(arg).resolve() for arg in sys.argv[1:])))
