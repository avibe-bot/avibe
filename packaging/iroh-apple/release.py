#!/usr/bin/env python3
"""Read-only packaging and validation for the owner-triggered iroh release job.

This program never creates a tag or release. Only the workflow's guarded action
can publish. Run with --help for the build, manifest and pre-publication checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import zipfile

HERE = Path(__file__).resolve().parent
REPOSITORY = "avibe-bot/avibe"
ZIP_NAME = "IrohLib.xcframework.zip"
SUPPORT_NAME = "IrohLib-support.zip"
RELEASE_NAME = "iroh-apple-release.json"
IOS_TARGETS = ("aarch64-apple-ios", "aarch64-apple-ios-sim", "x86_64-apple-ios")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pins() -> dict[str, str]:
    result = {}
    for line in (HERE / "versions.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = shlex.split(value)[0]
    return result


def expected_tag(config: dict[str, str]) -> str:
    return f"iroh-apple-{config['IROH_FFI_TAG']}-{config['IROH_APPLE_REVISION']}"


def validate_tag(tag: str, config: dict[str, str]) -> None:
    require(bool(re.fullmatch(r"iroh-apple-v\d+\.\d+\.\d+-[1-9]\d*", tag)), "invalid tag family")
    require(tag == expected_tag(config), "tag does not match pinned upstream version and revision")


def support_bundle(source: Path, output: Path) -> None:
    """Carry license/NOTICE files from the locked vendor tree alongside bindings."""
    metadata = json.loads(subprocess.check_output(
        ["cargo", f"+{pins()['RUST_VERSION']}", "metadata", "--locked", "--format-version", "1"],
        cwd=source, text=True,
    ))
    sysroot = Path(subprocess.check_output(
        ["rustc", f"+{pins()['RUST_VERSION']}", "--print", "sysroot"], text=True,
    ).strip())
    rust_notices = sysroot / "share/doc/rust"
    inventory = []
    with zipfile.ZipFile(output / SUPPORT_NAME, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(output / "IrohLib.swift", "IrohLib.swift")
        for name in ("LICENSE-APACHE", "LICENSE-MIT"):
            archive.write(source / name, f"LICENSES/iroh-ffi/{name}")
        for name in ("LICENSE-APACHE", "LICENSE-MIT", "COPYRIGHT.html"):
            require((rust_notices / name).is_file(), f"missing Rust distribution notice: {name}")
            archive.write(rust_notices / name, f"LICENSES/rust-{pins()['RUST_VERSION']}/{name}")
        for package in sorted(metadata["packages"], key=lambda item: (item["name"], item["version"])):
            root = Path(package["manifest_path"]).parent
            names = set()
            for path in root.rglob("*"):
                if path.is_file() and re.match(r"(?i)^(licen[cs]e|copying|copyright|notice)([._-]|$)", path.name):
                    names.add(path)
            if package.get("license_file"):
                names.add(root / package["license_file"])
            require(bool(names), f"no redistribution license text found: {package['name']} {package['version']}")
            prefix = f"LICENSES/{package['name']}-{package['version']}"
            files = []
            for path in sorted(names):
                require(path.is_file() and path.resolve().is_relative_to(root.resolve()), f"unsafe license file: {path}")
                target = f"{prefix}/{path.relative_to(root).as_posix()}"
                archive.write(path, target)
                files.append(target)
            inventory.append({"name": package["name"], "version": package["version"],
                              "license": package.get("license"), "files": files})
        archive.writestr("LICENSES/packages.json", json.dumps(inventory, indent=2) + "\n")
    print(f"Collected notices for {len(inventory)} locked packages (including build/host dependencies).")


def qualification_files(directory: Path) -> list[Path]:
    names = [ZIP_NAME, f"{ZIP_NAME}.sha256", "IrohLib.swift", "manifest.json", SUPPORT_NAME,
             "ios-runtime-test.json", "ios-runtime-test.log", "patched-check.log", "positive-control-check.log",
             "xcodebuild-iphoneos.log", "xcodebuild-iphonesimulator.log"]
    names += [f"feature-graph-{target}.txt" for target in IOS_TARGETS]
    result = [directory / name for name in names]
    for path in result:
        require(path.is_file() and path.stat().st_size > 0, f"missing artifact: {path.name}")
    return result


def prepare(directory: Path, tag: str, source_sha: str, run_id: str) -> dict:
    config = pins()
    validate_tag(tag, config)
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source_sha)), "invalid source commit")
    require(run_id.isdecimal() and int(run_id) > 0, "invalid qualification run id")
    files = qualification_files(directory)
    manifest = read_json(directory / "manifest.json")
    runtime = read_json(directory / "ios-runtime-test.json")
    for field, pin in (("iroh_ffi_tag", "IROH_FFI_TAG"), ("iroh_ffi_commit", "IROH_FFI_COMMIT"),
                       ("iroh_core_version", "IROH_CORE_VERSION"),
                       ("ios_deployment_target", "IOS_DEPLOYMENT_TARGET")):
        require(manifest.get(field) == config[pin], f"qualification pin mismatch: {field}")
    target = config["IOS_DEPLOYMENT_TARGET"]
    tested = runtime.get("runtime_version", "")
    require(manifest.get("ios_runtime_tested") == tested and bool(re.fullmatch(r"17\.[0-4](?:\.\d+)?", tested)),
            "inconsistent or out-of-range runtime evidence")
    require(runtime.get("runtime_identifier", "").startswith("com.apple.CoreSimulator.SimRuntime.iOS-17-"),
            "runtime identifier mismatch")
    require(type(runtime.get("xcodebuild_exit_code")) is int and runtime["xcodebuild_exit_code"] == 0,
            "runtime XCTest failed")
    test_log = (directory / "ios-runtime-test.log").read_text()
    require("** TEST SUCCEEDED **" in test_log and bool(re.search(r"Executed [1-9]\d* tests, with 0 failures", test_log)),
            "missing executed passing XCTest evidence")
    for name in ("xcodebuild-iphoneos.log", "xcodebuild-iphonesimulator.log", "ios-runtime-test.log"):
        text = (directory / name).read_text()
        require(not re.search(r"was built for (?:a )?newer .*version .*than .*being linked", text, re.I),
                f"deployment mismatch in {name}")
        if name.startswith("xcodebuild-"):
            require("** BUILD SUCCEEDED **" in text, f"build did not succeed: {name}")
    require("private Apple datapath check passed:" in (directory / "patched-check.log").read_text(), "missing patched check")
    control = (directory / "positive-control-check.log").read_text()
    require("ios-arm64/libiroh_ffi.a" in control and "in 1 binary/archive candidate(s)" in control
            and "sendmsg_x" in control and "recvmsg_x" in control, "missing iOS positive control")
    require((directory / f"{ZIP_NAME}.sha256").read_text().split()[0] == digest(directory / ZIP_NAME), "zip checksum mismatch")
    with zipfile.ZipFile(directory / ZIP_NAME) as archive:
        required = ["Iroh.xcframework/Info.plist", "Iroh.xcframework/ios-arm64/libiroh_ffi.a",
                    "Iroh.xcframework/ios-arm64_x86_64-simulator/libiroh_ffi-ios-simulator.a"]
        require(all(name in archive.namelist() for name in required), "wrong xcframework zip layout")
        require(archive.testzip() is None, "corrupt xcframework zip")
    with zipfile.ZipFile(directory / SUPPORT_NAME) as archive:
        require(archive.read("IrohLib.swift") == (directory / "IrohLib.swift").read_bytes(), "bindings mismatch")
        for name in ("LICENSES/iroh-ffi/LICENSE-APACHE", "LICENSES/iroh-ffi/LICENSE-MIT",
                     f"LICENSES/rust-{config['RUST_VERSION']}/LICENSE-APACHE",
                     f"LICENSES/rust-{config['RUST_VERSION']}/LICENSE-MIT",
                     f"LICENSES/rust-{config['RUST_VERSION']}/COPYRIGHT.html"):
            require(bool(archive.read(name)), f"missing upstream license: {name}")
        inventory = json.loads(archive.read("LICENSES/packages.json"))
        require(bool(inventory), "empty dependency license inventory")
        for package in inventory:
            require(bool(package["files"]), "empty package license inventory")
            for name in package["files"]:
                require(bool(archive.read(name)), f"empty license: {name}")
    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}"
    result = {
        "schema_version": 1, "release_state": "pending", "release_tag": tag,
        "asset_url": f"{base}/{ZIP_NAME}", "sha256": digest(directory / ZIP_NAME),
        "upstream_commit": config["IROH_FFI_COMMIT"], "iroh_core_version": config["IROH_CORE_VERSION"],
        "patch_sha256": digest(HERE / "iroh-ffi-no-private-apple-datapath.patch"),
        "build_recipe_sha256": digest(HERE / "build-iroh-apple.sh"),
        "deployment_target": target, "tested_runtime": runtime["runtime_version"],
        "runtime_identifier": runtime["runtime_identifier"], "source_commit": source_sha,
        "qualification_run_id": run_id,
        "assets": {path.name: {"url": f"{base}/{path.name}", "sha256": digest(path), "size": path.stat().st_size}
                   for path in files},
    }
    (directory / RELEASE_NAME).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def gh_json(endpoint: str):
    return json.loads(subprocess.check_output(["gh", "api", "--paginate", "--slurp", endpoint], text=True))


def refuse_existing_tag(tag: str) -> None:
    # Paginated listings cover draft releases as well as bare/annotated tags.
    # Any API/auth/pagination failure propagates; it is never treated as absence.
    for endpoint, field in (("tags", "name"), ("releases", "tag_name")):
        pages = gh_json(f"repos/{REPOSITORY}/{endpoint}?per_page=100")
        require(isinstance(pages, list) and all(isinstance(page, list) for page in pages), "invalid collision response")
        require(not any(item[field] == tag for page in pages for item in page), f"tag/release already exists: {tag}")


def publication_context(env: dict[str, str], tag: str) -> None:
    validate_tag(tag, pins())
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "publication repository is not avibe-bot/avibe")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "publication requires manual dispatch")
    require(env.get("GITHUB_REF") == "refs/heads/master", "publication requires master")


def publication_evidence(manifest: dict) -> None:
    require(manifest["tested_runtime"] == manifest["deployment_target"],
            "publication requires tested runtime equal to deployment target")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("support", "prepare", "preflight"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    if args.command == "support":
        require(args.source is not None, "support requires --source")
        support_bundle(args.source, args.directory)
        return
    tag = args.tag or expected_tag(pins())
    if args.command == "preflight":
        publication_context(dict(os.environ), tag)
        existing = read_json(args.directory / RELEASE_NAME)
        fresh = prepare(args.directory, tag, os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"])
        require(existing == fresh, "release candidate differs from the same-run manifest")
        publication_evidence(fresh)
        refuse_existing_tag(tag)
        # Visible only if the following release action succeeds. Repository pin
        # stays pending until an independently verified promotion PR is merged.
        fresh["release_state"] = "published"
        (args.directory / RELEASE_NAME).write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
    else:
        if args.tag:
            publication_context(dict(os.environ), tag)
            refuse_existing_tag(tag)
        candidate = prepare(args.directory, tag, os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"])
        if args.tag:
            publication_evidence(candidate)
    print(f"Validated {tag}; no release or tag written by this command.")


if __name__ == "__main__":
    main()
