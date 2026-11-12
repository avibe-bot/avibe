#!/usr/bin/env python3
"""Assert a target's resolved dependency/feature graph; emit machine-readable evidence."""

import json
import re
import sys
from pathlib import Path


def qualify(graph, metadata, pins):
    packages = {package["id"]: package for package in metadata["packages"]}
    resolved = {}
    for node in metadata["resolve"]["nodes"]:
        package = packages[node["id"]]
        name = package["name"]
        features = set(node["features"])
        resolved.setdefault(name, []).append({"version": package["version"], "features": sorted(features)})
        if re.search(r"mdns|mainline|portmap|(^|-)services($|-)", name):
            raise ValueError(f"forbidden discovery/mapping/service crate: {name}")
        if "fast-apple-datapath" in features:
            raise ValueError(f"private Apple datapath enabled by {name}")
        if name == "iroh" and features != {"tls-ring"}:
            raise ValueError(f"unexpected iroh feature union: {sorted(features)}")
        if name == "iroh-metrics" and features:
            raise ValueError(f"metrics functionality enabled: {sorted(features)}")
    for name, pin in [("iroh", pins["IROH_CORE_VERSION"]), ("noq", pins["NOQ_VERSION"])]:
        actual = resolved.get(name, [])
        if len(actual) != 1 or actual[0]["version"] != pin:
            raise ValueError(f"{name} no longer matches Apple qualification pin {pin}: {actual}")
    if 'iroh feature "tls-ring"' not in graph or "iroh v" + pins["IROH_CORE_VERSION"] not in graph:
        raise ValueError("missing root crypto feature/core pin in cargo tree")
    # Check both independent views, including positive controls in CI. A feature
    # graph cannot prove the built-in DNS/PKARR code is absent: it is not absent.
    if re.search(
        r'feature "(fast-apple-datapath|portmapper)"|iroh(?:-metrics)? feature "(?:metrics|service|static_core)"', graph
    ):
        raise ValueError("forbidden feature in cargo tree")
    return {
        "iroh": resolved["iroh"][0],
        "noq": resolved["noq"][0],
        "compiled_but_unconfigured": {name: resolved[name] for name in ("iroh-dns", "reqwest")},
        "excluded": [
            "fast-apple-datapath",
            "portmapper",
            "mdns",
            "mainline",
            "metrics",
            "metrics-push",
            "n0-services-client",
        ],
        "discovery_note": "Built-in DNS and PKARR lookup modules are compiled, but Minimal installs no lookup and the sidecar adds none.",
    }


def main():
    root = Path(__file__).resolve().parents[2]
    pins = dict(
        line.split("=", 1)
        for line in (root / "packaging/iroh-apple/versions.env").read_text().splitlines()
        if "=" in line
    )
    graph = Path(sys.argv[1]).read_text()
    metadata = json.loads(Path(sys.argv[2]).read_text())
    result = qualify(graph, metadata, pins)
    for forbidden in (
        'iroh feature "portmapper"',
        'noq-udp feature "fast-apple-datapath"',
        'iroh-metrics feature "service"',
    ):
        try:
            qualify(graph + "\n" + forbidden, metadata, pins)
        except ValueError:
            pass
        else:
            raise ValueError(f"feature-check positive control missed {forbidden}")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
