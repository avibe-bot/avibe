#!/usr/bin/env python3
"""Build the pinned and explicitly patched macOS Cua Driver helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "cua-driver" / "sources.json"
TARGET_ARCH = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
}
SHA256 = re.compile(r"[0-9a-f]{64}")
POLICY = ROOT / "cua-driver" / "policy.yaml"
TOOL_SNAPSHOT = ROOT / "cua-driver" / "tools-v0.31.0.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_manifest(path: Path = SOURCES) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    driver = payload.get("driver")
    macos = driver.get("macos") if isinstance(driver, dict) else None
    if (
        payload.get("schema_version") != 1
        or not isinstance(driver, dict)
        or driver.get("version") != "0.31.0"
        or driver.get("tag") != "cua-driver-rs-v0.31.0"
        or driver.get("source_commit") != "5272e492d61b96caf08e3bf434d91126c1f3dccc"
        or SHA256.fullmatch(str(driver.get("tool_snapshot_sha256"))) is None
        or driver.get("license") != "MIT"
        or not isinstance(driver.get("release_base_url"), str)
        or driver.get("release_checksums_asset") != "checksums.txt"
        or SHA256.fullmatch(str(driver.get("release_checksums_sha256"))) is None
        or not isinstance(macos, dict)
        or macos.get("asset") != "cua-driver-rs-0.31.0-darwin-universal-binary.tar.gz"
        or SHA256.fullmatch(str(macos.get("sha256"))) is None
        or not isinstance(driver.get("source"), dict)
        or driver["source"].get("archive")
        != "cua-5272e492d61b96caf08e3bf434d91126c1f3dccc.tar.gz"
        or driver["source"].get("url")
        != "https://codeload.github.com/trycua/cua/tar.gz/5272e492d61b96caf08e3bf434d91126c1f3dccc"
        or SHA256.fullmatch(str(driver["source"].get("sha256"))) is None
        or driver["source"].get("root")
        != "cua-5272e492d61b96caf08e3bf434d91126c1f3dccc"
        or not isinstance(driver.get("patch"), dict)
        or driver["patch"].get("variant") != "avibe-raw-single-click-v1"
        or driver["patch"].get("file")
        != "patches/0001-macos-raw-single-click.patch"
        or SHA256.fullmatch(str(driver["patch"].get("sha256"))) is None
        or driver["patch"].get("contract")
        != "click.click_mode=raw; embedded parent-liveness cleanup"
        or not isinstance(driver["patch"].get("upstream_refs"), list)
        or not driver["patch"]["upstream_refs"]
        or not isinstance(driver["patch"].get("upgrade_plan"), str)
        or not driver["patch"]["upgrade_plan"].strip()
        or payload.get("managed_policy") != "policy.yaml"
        or payload.get("tool_snapshot") != "tools-v0.31.0.json"
    ):
        raise ValueError("invalid pinned Cua Driver source manifest")
    patch = path.parent / driver["patch"]["file"]
    if not patch.is_file() or digest(patch) != driver["patch"]["sha256"]:
        raise ValueError("pinned Cua Driver patch hash mismatch")
    try:
        snapshot = json.loads(TOOL_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("pinned Cua Driver tool snapshot is unreadable") from exc
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("patch_sha256") != driver["patch"]["sha256"]
    ):
        raise ValueError("pinned Cua Driver tool snapshot patch hash mismatch")
    if digest(TOOL_SNAPSHOT) != driver["tool_snapshot_sha256"]:
        raise ValueError("pinned Cua Driver tool snapshot hash mismatch")
    if "perception" in json.dumps(payload).lower():
        raise ValueError("Cua perception assets must not enter the Phase 1 package")
    verify_managed_policy_surface()
    return payload


def verify_managed_policy_surface(
    policy_path: Path = POLICY,
    snapshot_path: Path = TOOL_SNAPSHOT,
) -> None:
    """Keep the shipped allow-list equal to the reviewed tool snapshot."""

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError("pinned Computer Use policy or snapshot is unreadable") from exc
    snapshot_tools = snapshot.get("tools") if isinstance(snapshot, dict) else None
    policy_tools = (
        policy.get("allow", {}).get("tools")
        if isinstance(policy, dict)
        else None
    )
    if not isinstance(snapshot_tools, list) or not isinstance(policy_tools, list):
        raise ValueError("pinned Computer Use policy and snapshot must contain tool lists")
    snapshot_names = [
        tool.get("name") for tool in snapshot_tools if isinstance(tool, dict)
    ]
    if (
        len(snapshot_names) != len(snapshot_tools)
        or any(not isinstance(name, str) or not name for name in snapshot_names)
        or len(set(snapshot_names)) != len(snapshot_names)
        or snapshot_names != policy_tools
    ):
        raise ValueError("managed Computer Use policy does not match the pinned tool snapshot")


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "avibe-desktop-cua-driver-builder/1"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise ValueError(f"download failed with HTTP {response.status}: {url}")
        with target.open("wb") as output:
            shutil.copyfileobj(response, output)


def extract_driver_source(manifest: dict, archive_path: Path, destination: Path) -> Path:
    """Extract only the pinned driver subtree from the verified source archive."""

    source = manifest["driver"]["source"]
    if digest(archive_path) != source["sha256"]:
        raise ValueError("Cua Driver source archive hash mismatch")
    archive_root = PurePosixPath(source["root"])
    driver_root = archive_root / "libs" / "cua-driver"
    extracted_files = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe Cua Driver source archive path")
            if path.parts[: len(driver_root.parts)] != driver_root.parts:
                continue
            relative = PurePosixPath(*path.parts[1:])
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("Cua Driver source subtree contains a non-regular file")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("Cua Driver source archive member could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(extracted, output)
            target.chmod(member.mode & 0o777)
            extracted_files += 1
    source_root = destination
    cargo_manifest = source_root / "libs" / "cua-driver" / "rust" / "Cargo.toml"
    if extracted_files == 0 or not cargo_manifest.is_file():
        raise ValueError("Cua Driver source archive is missing the pinned Rust workspace")
    return source_root


def run(command: list[str], *, merge_stderr: bool = True) -> str:
    return subprocess.check_output(
        command,
        text=True,
        stderr=subprocess.STDOUT if merge_stderr else None,
    )


def apply_driver_patch(manifest: dict, source_root: Path) -> Path:
    patch = ROOT / "cua-driver" / manifest["driver"]["patch"]["file"]
    subprocess.run(
        ["git", "-C", str(source_root), "apply", "--check", str(patch)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "apply", str(patch)],
        check=True,
    )
    return patch


def build_driver(manifest: dict, source_root: Path, target: str) -> Path:
    workspace = source_root / "libs" / "cua-driver" / "rust"
    target_dir = ROOT / "target" / "cua-driver-patched"
    environment = {
        **os.environ,
        "CARGO_INCREMENTAL": "0",
        "CARGO_TARGET_DIR": str(target_dir),
    }
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "-p",
            "cua-driver",
            "--bin",
            "cua-driver",
            "--target",
            target,
        ],
        cwd=workspace,
        env=environment,
        check=True,
    )
    built = target_dir / target / "release" / "cua-driver"
    if not built.is_file():
        raise ValueError("patched Cua Driver build did not produce the expected binary")
    verify_architecture(built, target)
    return built


def verify_architecture(binary: Path, target: str) -> None:
    info = run(["lipo", "-info", str(binary)])
    arch = TARGET_ARCH[target]
    if arch not in info or "Non-fat file" not in info:
        raise ValueError(f"patched Cua Driver has the wrong architecture: {info}")


def verify_built_contract(binary: Path) -> None:
    if run([str(binary), "--version"]).strip() != "cua-driver 0.31.0":
        raise ValueError("patched Cua Driver reports an unexpected version")
    try:
        docs = json.loads(
            run([str(binary), "dump-docs", "--type", "mcp"], merge_stderr=False)
        )
        click = next(tool for tool in docs["tools"] if tool.get("name") == "click")
        click_mode = click["input_schema"]["properties"]["click_mode"]
    except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
        raise ValueError("patched Cua Driver does not expose click.click_mode") from exc
    if click_mode.get("enum") != ["auto", "raw"]:
        raise ValueError("patched Cua Driver click.click_mode contract is unexpected")


def sign_adhoc(binary: Path) -> None:
    """Sign the built helper ad hoc, as Apple's linker already does for arm64 output.

    The linker leaves x86_64 output unsigned, and a gh-v TEST build has Tauri sign
    the app ad hoc while bundling, which codesign refuses while a nested helper is
    unsigned. The packaging workflow still re-signs the helper from the inside out.
    """
    subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True)
    verify_code_signature(binary)


def verify_code_signature(binary: Path) -> None:
    subprocess.run(["codesign", "--verify", "--strict", str(binary)], check=True)


def provenance_record(
    manifest: dict, target: str, *, patch_sha256: str, binary_sha256: str
) -> dict:
    driver = manifest["driver"]
    return {
        "schema_version": 2,
        "version": driver["version"],
        "tag": driver["tag"],
        "source_commit": driver["source_commit"],
        "target": target,
        "arch": TARGET_ARCH[target],
        "source_archive": driver["source"]["archive"],
        "source_archive_sha256": driver["source"]["sha256"],
        "patch_variant": driver["patch"]["variant"],
        "patch_file": driver["patch"]["file"],
        "patch_sha256": patch_sha256,
        "tool_snapshot_sha256": digest(TOOL_SNAPSHOT),
        "prepared_binary_sha256": binary_sha256,
        "build_origin": "repository_patch_built",
    }


def reuse_verified(manifest: dict, target: str, output: Path) -> bool:
    """Accept an already prepared helper only when it passes a fresh build's checks.

    The helper and its provenance must describe exactly the pinned source, patch,
    snapshot, and target, and the binary must hash to the recorded value. It then
    passes the same architecture, code-signature, and tool-contract checks as a
    helper built here. Anything else is rebuilt from source.
    """
    provenance_path = output.with_name(f"{output.name}.provenance.json")
    if any(path.is_symlink() or not path.is_file() for path in (output, provenance_path)):
        return False
    try:
        recorded = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    patch = ROOT / "cua-driver" / manifest["driver"]["patch"]["file"]
    expected = provenance_record(
        manifest,
        target,
        patch_sha256=digest(patch),
        binary_sha256=digest(output),
    )
    if recorded != expected:
        return False
    try:
        verify_architecture(output, target)
        verify_code_signature(output)
        verify_built_contract(output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"::warning::Rebuilding the Cua Driver; the prepared helper failed verification: {exc}")
        return False
    print(f"Reusing the verified prepared Cua Driver {expected['prepared_binary_sha256']}")
    return True


def prepare(
    target: str,
    *,
    source_archive: Path | None = None,
    output_dir: Path | None = None,
    reuse: bool = False,
) -> Path:
    manifest = load_manifest()
    driver = manifest["driver"]
    destination = (output_dir or ROOT / "src-tauri" / "binaries").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"cua-driver-{target}"
    if reuse and reuse_verified(manifest, target, output):
        return output

    with tempfile.TemporaryDirectory(prefix="avibe-cua-driver-") as temporary:
        work = Path(temporary)
        resolved_archive = source_archive or work / driver["source"]["archive"]
        if source_archive is None:
            download(driver["source"]["url"], resolved_archive)
        source_root = extract_driver_source(manifest, resolved_archive, work / "source")
        patch = apply_driver_patch(manifest, source_root)
        prepared = build_driver(manifest, source_root, target)
        sign_adhoc(prepared)
        verify_built_contract(prepared)
        temporary_output = destination / f".{output.name}.{os.getpid()}.tmp"
        shutil.copyfile(prepared, temporary_output)
        temporary_output.chmod(0o755)
        os.replace(temporary_output, output)
        provenance = provenance_record(
            manifest,
            target,
            patch_sha256=digest(patch),
            binary_sha256=digest(output),
        )
        provenance_output = output.with_name(f"{output.name}.provenance.json")
        temporary_provenance = destination / f".{provenance_output.name}.{os.getpid()}.tmp"
        temporary_provenance.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_provenance, provenance_output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-manifest", action="store_true")
    parser.add_argument("--target", choices=TARGET_ARCH)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--reuse-verified",
        action="store_true",
        help="keep an existing helper whose provenance and checks match the pinned inputs",
    )
    args = parser.parse_args()
    if args.check_manifest:
        load_manifest()
        if args.target is None:
            return
    if args.target is None:
        parser.error("--target is required unless only --check-manifest is used")
    output = prepare(
        args.target,
        source_archive=args.source_archive,
        output_dir=args.output_dir,
        reuse=args.reuse_verified,
    )
    print(output)


if __name__ == "__main__":
    main()
