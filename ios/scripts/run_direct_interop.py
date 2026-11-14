#!/usr/bin/env python3
"""Hosted-only consumer of merged Apple/sidecar pins and the v2 App harness.

No installation, product state, publication, candidate override, or shell
transport. Python owns the test process, fixture sockets and sidecar children.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import tarfile

import direct_interop_host as host
import prepare_iroh_app as apple

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_PIN = "vibe/direct_transport_runtime_manifest.json"
DEPENDENCIES = (
    "packaging/iroh-apple/release.json", SIDECAR_PIN,
    "ios/Avibe/DirectIrohBinding.swift",
    "ios/AvibeTests/DirectIrohInteropTests.swift",
    "ios/Avibe.xcodeproj/project.pbxproj",
    "ios/Avibe.xcodeproj/xcshareddata/xcschemes/Avibe.xcscheme",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], timeout=20)


def merged_dependencies() -> dict:
    """Compare all consuming inputs to the freshly fetched default branch."""
    hashes = {}
    for name in DEPENDENCIES:
        path = ROOT / name
        host.require(path.is_file() and not path.is_symlink(), f"missing dependency: {name}")
        merged = git("show", f"origin/master:{name}")
        host.require(path.read_bytes() == merged, f"dependency is not the merged default-branch input: {name}")
        hashes[name] = digest(merged)
    apple.load_pin()
    pin = json.loads((ROOT / SIDECAR_PIN).read_text())
    host.require(pin.get("schema_version") == 1 and pin.get("release_state") == "published",
                 "sidecar consumer pin is not published/merged")
    provenance = pin.get("provenance", {})
    source = provenance.get("source_sha", "")
    host.require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source) is not None,
                 "sidecar pin has no full source SHA")
    git("merge-base", "--is-ancestor", source, "origin/master")
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "merged_base": git("rev-parse", "origin/master").decode().strip(),
            "dependencies": hashes}


def hosted_root() -> Path:
    host.require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_OS") == "macOS",
                 "native interoperability is authorized only on the hosted macOS runner")
    root = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
    host.require(root.is_dir() and str(root) != str(Path.cwd()) and root != Path("/"), "missing RUNNER_TEMP")
    return root / "avibe-direct-interop"


def sidecar_asset(pin: dict, machine: str) -> tuple[str, dict]:
    target = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}.get(machine)
    host.require(target is not None, "unsupported hosted sidecar architecture")
    key = "darwin-arm64" if machine == "arm64" else "darwin-x64"
    item = pin.get("archives", {}).get(key, {})
    tag = pin.get("release_tag", "")
    host.require(isinstance(tag, str) and re.fullmatch(r"direct-transport-v[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*", tag) is not None,
                 "invalid published sidecar tag")
    name = f"avibe-direct-transport-{target}.tar.gz"
    host.require(pin.get("release_state") == "published" and pin.get("source") == "avibe-bot/avibe"
                 and item.get("name") == name and item.get("format") == "tar.gz"
                 and item.get("bin_path") == "avibe-direct-transport"
                 and item.get("url") == f"https://github.com/avibe-bot/avibe/releases/download/{tag}/{name}",
                 "sidecar archive is not the exact published release asset")
    for field in ("sha256", "binary_sha256"):
        host.require(isinstance(item.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", item[field]) is not None,
                     f"invalid sidecar {field}")
    host.require(type(item.get("size")) is int and 0 < item["size"] <= 128 * 1024 * 1024, "invalid sidecar size")
    return target, item


def unpack_sidecar(data: bytes, pin: dict, machine: str, output: Path) -> dict:
    target, item = sidecar_asset(pin, machine)
    host.require(len(data) == item["size"] and digest(data) == item["sha256"], "sidecar archive pin mismatch")
    selected = {}
    names = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            host.require(not path.is_absolute() and ".." not in path.parts and "\\" not in member.name
                         and member.name not in names and (member.isdir() or member.isfile()), "unsafe sidecar archive member")
            names.add(member.name)
            total += member.size
            host.require(len(names) <= 4096 and 0 <= member.size <= 128 * 1024 * 1024
                         and total <= 256 * 1024 * 1024, "sidecar archive expansion bound exceeded")
            if member.name in {"avibe-direct-transport", "manifest.json"}:
                host.require(member.isfile(), "sidecar binary/manifest is not a regular file")
                stream = archive.extractfile(member)
                host.require(stream is not None, "sidecar archive member missing")
                selected[member.name] = stream.read(member.size + 1)
                host.require(len(selected[member.name]) == member.size, "sidecar archive member truncated")
    host.require(set(selected) == {"avibe-direct-transport", "manifest.json"}, "sidecar archive lacks binary/manifest")
    binary = selected["avibe-direct-transport"]
    manifest = json.loads(selected["manifest.json"])
    provenance = pin.get("provenance", {})
    host.require(digest(binary) == item["binary_sha256"] == manifest.get("binary_sha256")
                 and manifest.get("binary_bytes") == len(binary) and manifest.get("target") == target
                 and manifest.get("source_sha") == provenance.get("source_sha")
                 and manifest.get("workflow_run_id") == str(provenance.get("qualification_run_id"))
                 and manifest.get("workflow_run_attempt") == provenance.get("qualification_attempt"),
                 "sidecar binary/source/qualification identity mismatch")
    output.mkdir(parents=True, exist_ok=False)
    executable = output / "avibe-direct-transport"
    executable.write_bytes(binary)
    executable.chmod(0o700)
    (output / "manifest.json").write_bytes(selected["manifest.json"])
    receipt = {"release_tag": pin["release_tag"], "archive_sha256": item["sha256"],
               "binary_sha256": digest(binary), "source_sha": provenance["source_sha"], "target": target}
    host.write_json(output / "verified.json", receipt)
    return receipt


def prepare() -> None:
    evidence = merged_dependencies()
    root = hosted_root()
    root.mkdir(exist_ok=False)
    pin = json.loads((ROOT / SIDECAR_PIN).read_text())
    _, asset = sidecar_asset(pin, platform.machine())
    binary_data = apple.download(asset, limit=128 * 1024 * 1024)
    evidence["sidecar"] = unpack_sidecar(binary_data, pin, platform.machine(), root / "sidecar")
    apple.stage(root / "apple")
    evidence["apple"] = apple.verify_local(root / "apple")
    # App build paths are fixed by the merged graph. Copy only the verified
    # staged tree; never make a local installed runtime or mutate a pin.
    destination = ROOT / "ios/Avibe/IrohLib"
    host.require(not destination.exists(), "App iroh staging must be fresh")
    shutil.copytree(root / "apple", destination)
    host.write_json(root / "dependencies.json", evidence)


def reconcile(directory: Path, xcode_exit: int) -> dict:
    host.require(xcode_exit == 0, f"xcodebuild exited {xcode_exit}")
    document = json.loads((directory / "host.json").read_text())
    host.require(not document.get("errors"), "host supervisor recorded failures")
    for tier in ("engine", "transport"):
        app = json.loads((directory / f"{tier}.json").read_text())
        evidence = document if tier == "engine" else document.get("transport", {})
        host.require(app.get("schema") == evidence.get("schema") == 2
                     and app.get("tier") == evidence.get("tier") == tier, "result schema/tier mismatch")
        if tier == "transport" and evidence.get("status") == "not_covered":
            host.require(app.get("status") == "not_covered" and app.get("cases") == evidence.get("cases") == [],
                         "transport skip not independently confirmed")
            continue
        host.require(app.get("status") == evidence.get("status") == "passed", f"{tier} did not pass")
        host.require(isinstance(app.get("cases"), list) and isinstance(evidence.get("cases"), list), "missing case arrays")
        host.require([c.get("name") for c in app["cases"]] == list(host.CASES)
                     and [c.get("name") for c in evidence["cases"]] == list(host.CASES), "case inventory/order mismatch")
        for client, server in zip(app["cases"], evidence["cases"], strict=True):
            host.require(client.get("outcome") == "passed" and server.get("host") == "confirmed", "case verdict mismatch")
            fingerprint = digest(json.dumps(client, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
            host.require(fingerprint == server.get("app_result_sha256"), "App result artifact differs from its control evidence")
    return {"status": "passed", "engine": "passed", "transport": document["transport"]["status"]}


async def run(simulator: str) -> None:
    root = hosted_root()
    current = merged_dependencies()
    prepared = json.loads((root / "dependencies.json").read_text())
    host.require(current["dependencies"] == prepared["dependencies"] and current["head"] == prepared["head"],
                 "dependencies changed after preparation")
    _, asset = sidecar_asset(json.loads((ROOT / SIDECAR_PIN).read_text()), platform.machine())
    binary = root / "sidecar/avibe-direct-transport"
    host.require(binary.is_file() and not binary.is_symlink() and digest(binary.read_bytes()) == asset["binary_sha256"],
                 "staged sidecar no longer matches the merged pin")
    apple.verify_local(root / "apple")
    apple.verify_local(ROOT / "ios/Avibe/IrohLib")
    fixture_host = host.Host(binary, root / "results")
    process = None
    code = -1
    failure = None
    try:
        fixture = await fixture_host.start()
        env = dict(os.environ, TEST_RUNNER_AVIBE_DIRECT_INTEROP_FIXTURE=str(fixture))
        command = ["xcodebuild", "-project", "ios/Avibe.xcodeproj", "-scheme", "Avibe",
                   "-configuration", "Debug", "-destination", f"platform=iOS Simulator,id={simulator}",
                   "-parallel-testing-enabled", "NO", "-maximum-concurrent-test-simulator-destinations", "1",
                   "-only-testing:AvibeTests/DirectIrohInteropTests", "-derivedDataPath", str(root / "DerivedData"),
                   "-resultBundlePath", str(root / "Interop.xcresult"), "CODE_SIGNING_ALLOWED=NO", "test"]
        with (root / "xcodebuild.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env, stdout=log, stderr=log,
                                                           start_new_session=True)
            host.write_json(root / "xcodebuild-pid.json", {"pid": process.pid, "process_group": process.pid, "command": command})
            async with asyncio.timeout(1200):
                code = await process.wait()
            if code == 0:
                async with asyncio.timeout(5):
                    await fixture_host.complete.wait()
    except (Exception, asyncio.CancelledError) as error:
        failure = f"{type(error).__name__}: {error}"
    finally:
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await fixture_host.close()
    host.require(failure is None, failure or "host failed")
    host.write_json(root / "verdict.json", reconcile(root / "results", code))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-dependencies")
    commands.add_parser("prepare")
    commands.add_parser("simulator")
    execute = commands.add_parser("run")
    execute.add_argument("--simulator", required=True)
    arguments = parser.parse_args()
    if arguments.command == "check-dependencies":
        print(json.dumps(merged_dependencies(), indent=2))
    elif arguments.command == "prepare":
        prepare()
    elif arguments.command == "simulator":
        hosted_root()
        listing = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "devices", "available", "--json"], timeout=30))
        devices = listing["devices"].get("com.apple.CoreSimulator.SimRuntime.iOS-26-5", [])
        matches = sorted(d["udid"] for d in devices if d.get("isAvailable") and d.get("name") == "iPhone 17 Pro")
        host.require(bool(matches), "pinned iPhone 17 Pro/iOS 26.5 simulator is missing")
        print(matches[0])
    else:
        asyncio.run(run_with_signals(arguments.simulator))


async def run_with_signals(simulator: str) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    cancelled = False

    def cancel_once() -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, cancel_once)
    try:
        await run(simulator)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    main()
