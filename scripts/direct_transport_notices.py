"""Compose sidecar notice policy without changing the Apple policy's identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def policy_bytes() -> bytes:
    paths = (ROOT / "packaging/iroh-apple/license-sources.json", ROOT / "direct-transport/license-policy-overlay.json")
    documents = [path.read_bytes().replace(b"\r\n", b"\n") for path in paths]
    base, overlay = [json.loads(data) for data in documents]
    if base["schema_version"] != 1 or overlay["schema_version"] != 1:
        raise ValueError("unsupported notice policy schema")
    for group in ("expressions", "packages"):
        for key, value in overlay[group].items():
            if key in base[group] and base[group][key] != value:
                raise ValueError(f"sidecar overlay must not override shared policy: {key}")
            base[group][key] = value
    base["composition"] = {
        "base_sha256": hashlib.sha256(documents[0]).hexdigest(),
        "overlay_sha256": hashlib.sha256(documents[1]).hexdigest(),
    }
    return (json.dumps(base, indent=2, sort_keys=True) + "\n").encode()
