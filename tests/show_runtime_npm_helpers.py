"""Standalone locked npm consumer fixture; no npm resolution or network needed."""

import base64
import hashlib
import json
from pathlib import Path


def write_npm_runtime_project(root: Path, *, tarball: bytes = b"fixture", lock_version: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dependencies = {"@avibe/show-runtime": "1.0.0"}
    package = {"private": True, "type": "module", "dependencies": dependencies}
    lock = {
        "name": "show-runtime-consumer",
        "lockfileVersion": lock_version,
        "requires": True,
        "packages": {
            "": {"dependencies": dependencies},
            "node_modules/@avibe/show-runtime": {
                "version": "1.0.0",
                "resolved": "https://registry.example.invalid/show-runtime-1.0.0.tgz",
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode(),
                "bin": {"avibe-show-runtime": "cli.js"},
            },
        },
    }
    if lock_version == 2:
        lock["dependencies"] = {"@avibe/show-runtime": lock["packages"]["node_modules/@avibe/show-runtime"].copy()}
    (root / "package.json").write_text(json.dumps(package) + "\n", encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
    return root
