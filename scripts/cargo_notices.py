#!/usr/bin/env python3
"""Collect reviewed redistribution notices for a locked, target-resolved Cargo graph.

This is a provenance/packaging check, not a legal review. No build is performed.
The caller selects its root package and all targets it redistributes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 repository tooling
    import tomli as tomllib

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def notice_file(path: Path) -> bool:
    # copying.rs in objc2-foundation is code, not a license text.
    if path.suffix.lower() in {".rs", ".c", ".h", ".py", ".js"}:
        return False
    return bool(re.match(r"(?i)^(licen[cs]e|copying|copyright|notice|unlicense)([._-]|$)", path.name))


def resolved_packages(lockfile: Path, targets: list[str], toolchain: str, package: str) -> list[dict]:
    require(bool(targets), "at least one target is required")
    require(lockfile.name == "Cargo.lock", "lockfile must be the workspace Cargo.lock")
    before = lockfile.read_bytes()
    selected = {}
    for target in targets:
        metadata = json.loads(subprocess.check_output([
            "cargo", f"+{toolchain}", "metadata", "--locked", "--format-version", "1",
            "--manifest-path", str(lockfile.with_name("Cargo.toml")), "--filter-platform", target,
        ], cwd=lockfile.parent, text=True))
        packages = {p["id"]: p for p in metadata["packages"]}
        roots = [p for p in metadata["workspace_members"] if packages[p]["name"] == package]
        require(len(roots) == 1, f"expected one workspace root package: {package}")
        nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
        pending, seen = list(roots), set()
        while pending:
            identity = pending.pop()
            if identity in seen:
                continue
            seen.add(identity)
            selected[identity] = packages[identity]
            # Cargo filters the target edges; retain every dependency kind,
            # including build/proc-macro and dev dependencies, not all packages.
            pending.extend(dep["pkg"] for dep in nodes[identity]["deps"])
    require(lockfile.read_bytes() == before, "Cargo.lock changed during notice collection")
    return sorted(selected.values(), key=lambda p: (p["name"], p["version"], p["id"]))


def supplemental_files(entry: dict, cache: dict) -> list[tuple[dict, bytes]]:
    result = []
    for file in entry["files"]:
        name = file["name"]
        require(name == Path(name).name and name not in {".", ".."}, "unsafe supplemental filename")
        url = file["url"]
        require(bool(re.fullmatch(r"https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/.+", url)),
                "supplemental URL must pin an immutable source commit")
        if url not in cache:
            with urllib.request.urlopen(url, timeout=60) as response:
                cache[url] = response.read()
        data = cache[url]
        require(bool(data) and sha256(data) == file["sha256"], f"supplemental notice checksum mismatch: {name}")
        result.append((file, data))
    return result


def mpl_source(package: dict, root: Path, checksum: dict) -> dict:
    vcs = json.loads((root / ".cargo_vcs_info.json").read_text())
    commit = vcs["git"]["sha1"]
    repository = package["repository"].removesuffix(".git")
    require(bool(re.fullmatch(r"https://github\.com/[^/]+/[^/]+", repository))
            and bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "MPL source requires an exact upstream commit")
    require(bool(checksum["files"]), "MPL source requires Cargo vendor file checksums")
    for name, expected in checksum["files"].items():
        path = root / name
        require(path.resolve().is_relative_to(root.resolve()) and sha256(path.read_bytes()) == expected,
                f"MPL source was modified: {package['name']}/{name}")
    return {
        "name": package["name"], "version": package["version"],
        "registry_url": f"https://static.crates.io/crates/{package['name']}/{package['name']}-{package['version']}.crate",
        "repository": repository, "upstream_commit": commit,
        "upstream_url": f"{repository}/tree/{commit}", "crate_sha256": checksum["package"],
        "modification_status": "unmodified",
    }


def collect_notices(lockfile: Path, targets: list[str], toolchain: str, package: str,
                    policy_path: Path, output: Path) -> dict:
    lockfile = lockfile.resolve()
    policy = json.loads(policy_path.read_text())
    locked = {(p["name"], p["version"], p.get("source")): p for p in tomllib.loads(lockfile.read_text())["package"]}
    packages = resolved_packages(lockfile, targets, toolchain, package)
    output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "notice output directory must be empty")
    inventory, mpl, cache, missing = [], [], {}, []
    for p in packages:
        key = f"{p['name']}-{p['version']}"
        expression = p.get("license")
        require(expression in policy["expressions"], f"unreviewed license expression: {key}: {expression}")
        selected = policy["expressions"][expression]
        require(not re.search(r"(?:^|[^A-Za-z])(?:A?GPL|LGPL)-", selected), "copyleft option requires a new policy review")
        root = Path(p["manifest_path"]).parent
        source = p.get("source")
        require(source in {None, REGISTRY}, f"unreviewed crate source: {key}: {source}")
        lock = locked[(p["name"], p["version"], source)]
        checksum = json.loads((root / ".cargo-checksum.json").read_text()) if source else None
        if checksum is not None:
            require(checksum["package"] == lock["checksum"], f"vendor/lock checksum mismatch: {key}")
        override = policy["packages"].get(key)
        if override:
            require(source == REGISTRY and override["crate_sha256"] == lock["checksum"]
                    and override["license"] == expression, f"notice exception does not match locked crate: {key}")
        paths = root.rglob("*") if source else [*root.iterdir(), *lockfile.parent.iterdir()]
        notices = {path for path in paths if path.is_file() and notice_file(path)}
        if p.get("license_file"):
            notices.add(root / p["license_file"])
        extra = supplemental_files(override, cache) if override else []
        if not notices and not extra:
            missing.append(key)
        directory = output / key
        directory.mkdir()
        files = []
        for path in sorted(notices):
            base = root if path.resolve().is_relative_to(root.resolve()) else lockfile.parent
            require(path.resolve().is_relative_to(base.resolve()) and (base == root or source is None),
                    f"unsafe notice file: {key}/{path.name}")
            relative = path.relative_to(base)
            dest = directory / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes()
            require(bool(data), f"empty bundled notice: {key}/{relative}")
            dest.write_bytes(data)
            files.append(f"LICENSES/{key}/{relative.as_posix()}")
        for file, data in extra:
            dest = directory / "supplemental" / file["name"]
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(data)
            files.append(f"LICENSES/{key}/supplemental/{file['name']}")
        classification = override["class"] if override else "bundled"
        require(classification in {"bundled", "upstream", "standard"}, "unknown notice source class")
        if classification == "standard":
            # Preserve declarations without inventing copyright holders or
            # substituting metadata authors into canonical license templates.
            (directory / "Cargo.toml").write_bytes((root / "Cargo.toml").read_bytes())
            files.append(f"LICENSES/{key}/Cargo.toml")
        inventory.append({"name": p["name"], "version": p["version"], "license": expression,
                          "selected_license": selected, "authors": p.get("authors", []),
                          "repository": p.get("repository"), "crate_sha256": lock.get("checksum"),
                          "class": classification, "files": sorted(set(files)),
                          "supplemental_sources": override["files"] if override else []})
        if selected == "MPL-2.0":
            require(checksum is not None, "MPL workspace source requires an explicit source distribution policy")
            mpl.append(mpl_source(p, root, checksum))
    require(not missing, "no reviewed redistribution notice source: " + ", ".join(missing))
    result = {"schema_version": 1, "targets": targets, "root_package": package,
              "lockfile_sha256": sha256(lockfile.read_bytes()), "policy_sha256": sha256(policy_path.read_bytes()),
              "counts": dict(Counter(p["class"] for p in inventory)), "packages": inventory, "mpl_sources": mpl}
    (output / "packages.json").write_text(json.dumps(inventory, indent=2) + "\n")
    (output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    text = ["# Redistribution notices", "", "Standard-text entries are canonical license references, not upstream copyright notices.",
            "Authors and declared expressions are preserved as metadata; no copyright holder is inferred.",
            "This notice collection is not a legal review.", "", "## MPL-2.0 covered source", "",
            "The following MPL-2.0 crates are distributed unmodified. Recipients may obtain their covered source",
            "from the checksum-pinned registry archives or exact upstream commits listed below."]
    for item in mpl:
        text += ["", f"- {item['name']} {item['version']} (unmodified)",
                 f"  Registry source: {item['registry_url']}", f"  SHA-256: {item['crate_sha256']}",
                 f"  Upstream source: {item['upstream_url']}"]
    (output / "NOTICE.md").write_text("\n".join(text) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lockfile", type=Path, required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--toolchain", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_notices(args.lockfile, args.target, args.toolchain, args.package, args.policy, args.output)
    print(json.dumps(result["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
