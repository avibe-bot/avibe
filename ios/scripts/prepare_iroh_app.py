#!/usr/bin/env python3
"""Stage and verify the published iOS iroh binding for the App build.

The script is deliberately consumer-only: it accepts only a published
packaging/iroh-apple/release.json pin, downloads the exact named assets, and
never builds, republishes, or overrides a candidate. The staged directory is
build input, not a checked-in source tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PIN_PATH = ROOT / "packaging/iroh-apple/release.json"
CHECKER = ROOT / "packaging/iroh-apple/check-private-apple-datapath.sh"
BINARY_NAME = "IrohLib.xcframework.zip"
SUPPORT_NAME = "IrohLib-support.zip"
BINDING_NAME = "IrohLib.swift"
MANIFEST_NAME = "manifest.json"
GRAPH_NAMES = (
    "feature-graph-aarch64-apple-ios.txt",
    "feature-graph-aarch64-apple-ios-sim.txt",
    "feature-graph-x86_64-apple-ios.txt",
)
MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
RELEASE_URL = re.compile(
    r"https://github\.com/avibe-bot/avibe/releases/download/[^/]+/[A-Za-z0-9._-]+"
)
FORBIDDEN_FEATURES = (
    'iroh feature "fast-apple-datapath"',
    'noq feature "fast-apple-datapath"',
    'noq-udp feature "fast-apple-datapath"',
    'iroh feature "portmapper"',
)
LOCAL_DISCOVERY = re.compile(
    r"(^|[^A-Za-z0-9])(address[-_ ]?lookup|dns[-_ ]?sd|dnssd|local[-_ ]?net(work)?|mdns|m[-_ ]?dns|multicast|service[-_ ]?discovery|zeroconf)([^A-Za-z0-9]|$)",
    re.IGNORECASE,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_pin(path: Path = PIN_PATH) -> dict:
    pin = load_json(path)
    require(pin.get("schema_version") == 1, "unknown release pin schema")
    require(pin.get("release_state") == "published", "the iroh release pin is not published")
    require(isinstance(pin.get("release_tag"), str) and pin["release_tag"], "published pin has no release tag")
    require(re.fullmatch(r"[0-9a-f]{64}", pin.get("sha256", "")) is not None, "published pin has no binary checksum")
    require(pin.get("deployment_target") == "17.0", "iroh App deployment target is not iOS 17.0")
    return pin


def asset(pin: dict, name: str) -> dict:
    item = pin.get("assets", {}).get(name)
    require(isinstance(item, dict), f"published pin does not name {name}")
    url = item.get("url", "")
    require(RELEASE_URL.fullmatch(url) is not None, f"{name} URL is not a release URL")
    require(f"/{pin['release_tag']}/{name}" in url, f"{name} URL is not from the pinned release")
    require(re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) is not None, f"{name} has no checksum")
    require(isinstance(item.get("size"), int) and item["size"] > 0, f"{name} has no positive size")
    return item


def download(item: dict, *, limit: int) -> bytes:
    require(item["size"] <= limit, "pinned asset exceeds the consumer size bound")
    with urllib.request.urlopen(item["url"], timeout=90) as response:  # noqa: S310 - exact reviewed https URL
        data = response.read(limit + 1)
    require(len(data) == item["size"], "downloaded asset size differs from the pin")
    require(sha256(data) == item["sha256"], "downloaded asset checksum differs from the pin")
    return data


def archive_members(data: bytes, *, label: str) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(archive.testzip() is None, f"corrupt {label}")
        for info in archive.infolist():
            name = info.filename
            if info.is_dir():
                continue
            path = PurePosixPath(name)
            require(
                not path.is_absolute()
                and "\\" not in name
                and ".." not in path.parts
                and all(part not in {"", "."} for part in path.parts),
                f"unsafe {label} member: {name!r}",
            )
            require(not stat.S_ISLNK(info.external_attr >> 16), f"symlink in {label}: {name}")
            require(name not in members, f"duplicate {label} member: {name}")
            require(info.file_size <= MAX_MEMBER_BYTES, f"oversized {label} member: {name}")
            total += info.file_size
            require(total <= MAX_TOTAL_BYTES, f"{label} expands beyond the consumer bound")
            members[name] = archive.read(info)
    return members


def validate_binary(data: bytes) -> dict[str, bytes]:
    members = archive_members(data, label="xcframework")
    required = {
        "Iroh.xcframework/Info.plist",
        "Iroh.xcframework/ios-arm64/libiroh_ffi.a",
        "Iroh.xcframework/ios-arm64/Headers/Export.h",
        "Iroh.xcframework/ios-arm64/Headers/iroh_ffiFFI.h",
        "Iroh.xcframework/ios-arm64/Headers/module.modulemap",
        "Iroh.xcframework/ios-arm64_x86_64-simulator/libiroh_ffi-ios-simulator.a",
        "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/Export.h",
        "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/iroh_ffiFFI.h",
        "Iroh.xcframework/ios-arm64_x86_64-simulator/Headers/module.modulemap",
    }
    require(required <= members.keys(), "published xcframework has an incomplete iOS layout")
    require(all(name.startswith("Iroh.xcframework/") for name in members), "xcframework contains an unexpected top-level member")
    require(not any("macos" in name.lower() for name in members), "published xcframework contains a macOS slice")
    return members


def validate_graphs(pin: dict, downloaded: dict[str, bytes]) -> None:
    manifest = json.loads(downloaded[MANIFEST_NAME])
    require(manifest.get("iroh_ffi_commit") == pin.get("upstream_commit"), "artifact commit differs from the published pin")
    require(manifest.get("iroh_core_version") == pin.get("iroh_core_version"), "artifact iroh version differs from the published pin")
    require(manifest.get("ios_deployment_target") == pin.get("deployment_target"), "artifact deployment target differs from the published pin")
    require(manifest.get("features") == ["metrics", "tls-ring"], "artifact feature policy differs from the approved policy")
    require("fast-apple-datapath" in manifest.get("excluded_iroh_features", []), "artifact does not record the private datapath exclusion")
    for name in GRAPH_NAMES:
        text = downloaded[name].decode("utf-8")
        for forbidden in FORBIDDEN_FEATURES:
            require(forbidden not in text, f"forbidden feature reached {name}: {forbidden}")
        require(not LOCAL_DISCOVERY.search(text), f"local discovery reached {name}")
        require('iroh feature "metrics"' in text, f"iroh metrics is missing from {name}")
        require('iroh feature "tls-ring"' in text, f"iroh tls-ring is missing from {name}")


def verify_local(staging: Path, pin: dict | None = None) -> dict:
    pin = pin or load_pin()
    require(pin.get("release_state") == "published", "the iroh release pin is not published")
    binary = staging / BINARY_NAME
    binding = staging / BINDING_NAME
    support = staging / SUPPORT_NAME
    manifest = staging / MANIFEST_NAME
    require(binary.is_file() and binding.is_file() and support.is_file() and manifest.is_file(), "iroh staging is incomplete")
    binary_data = binary.read_bytes()
    binary_item = asset(pin, BINARY_NAME)
    require(len(binary_data) == binary_item["size"] and sha256(binary_data) == binary_item["sha256"], "staged binary does not match the published pin")
    validate_binary(binary_data)
    binding_item = asset(pin, BINDING_NAME)
    binding_data = binding.read_bytes()
    require(len(binding_data) == binding_item["size"] and sha256(binding_data) == binding_item["sha256"], "staged binding does not match the published pin")
    support_item = asset(pin, SUPPORT_NAME)
    support_data = support.read_bytes()
    require(len(support_data) == support_item["size"] and sha256(support_data) == support_item["sha256"], "staged support archive does not match the published pin")
    support_members = archive_members(support_data, label="support archive")
    require(support_members.get(BINDING_NAME) == binding_data, "staged wrapper differs from the support archive")
    downloaded = {MANIFEST_NAME: manifest.read_bytes()}
    for name in GRAPH_NAMES:
        path = staging / name
        require(path.is_file(), f"iroh staging is missing {name}")
        item = asset(pin, name)
        content = path.read_bytes()
        require(len(content) == item["size"] and sha256(content) == item["sha256"], f"staged {name} differs from the published pin")
        downloaded[name] = content
    validate_graphs(pin, downloaded)
    verified = load_json(staging / "verified.json") if (staging / "verified.json").is_file() else {}
    require(verified.get("release_tag") == pin["release_tag"], "iroh staging was made from another release")
    require(verified.get("binary_sha256") == pin["sha256"], "iroh staging provenance is stale")
    return {"release_tag": pin["release_tag"], "binary_sha256": pin["sha256"], "binding_sha256": sha256(binding_data), "support_sha256": sha256(support_data)}


def stage(staging: Path, pin_path: Path = PIN_PATH) -> None:
    pin = load_pin(pin_path)
    names = (BINARY_NAME, SUPPORT_NAME, BINDING_NAME, MANIFEST_NAME, *GRAPH_NAMES)
    downloaded = {name: download(asset(pin, name), limit=MAX_BINARY_BYTES if name == BINARY_NAME else MAX_TOTAL_BYTES) for name in names}
    validate_binary(downloaded[BINARY_NAME])
    support_members = archive_members(downloaded[SUPPORT_NAME], label="support archive")
    require(support_members.get(BINDING_NAME) == downloaded[BINDING_NAME], "published wrapper differs from the support archive")
    validate_graphs(pin, {MANIFEST_NAME: downloaded[MANIFEST_NAME], **{name: downloaded[name] for name in GRAPH_NAMES}})
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / BINARY_NAME).write_bytes(downloaded[BINARY_NAME])
    (staging / BINDING_NAME).write_bytes(downloaded[BINDING_NAME])
    (staging / SUPPORT_NAME).write_bytes(downloaded[SUPPORT_NAME])
    (staging / MANIFEST_NAME).write_bytes(downloaded[MANIFEST_NAME])
    for name in GRAPH_NAMES:
        (staging / name).write_bytes(downloaded[name])
    binary_members = validate_binary(downloaded[BINARY_NAME])
    for name, content in binary_members.items():
        path = staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (staging / "verified.json").write_text(json.dumps({
        "schema_version": 1,
        "release_tag": pin["release_tag"],
        "binary_sha256": pin["sha256"],
        "binding_sha256": sha256(downloaded[BINDING_NAME]),
        "support_sha256": sha256(downloaded[SUPPORT_NAME]),
        "manifest_sha256": sha256(downloaded[MANIFEST_NAME]),
        "graph_sha256": {name: sha256(downloaded[name]) for name in GRAPH_NAMES},
    }, indent=2, sort_keys=True) + "\n")
    verify_local(staging, pin)
    print(f"staged published iroh Apple artifact {pin['release_tag']} in {staging}")


def verify_product(staging: Path, product: Path) -> None:
    verify_local(staging)
    require(product.is_file(), f"linked App product does not exist: {product}")
    subprocess.run(["bash", str(CHECKER), str(product)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("stage", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--staging", type=Path, required=True)
    product = subparsers.add_parser("verify-product")
    product.add_argument("--staging", type=Path, required=True)
    product.add_argument("--product", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.staging)
    elif args.command == "verify":
        print(json.dumps(verify_local(args.staging), sort_keys=True))
    else:
        verify_product(args.staging, args.product)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError, zipfile.BadZipFile) as error:
        raise SystemExit(f"iroh App artifact rejected: {error}") from error
