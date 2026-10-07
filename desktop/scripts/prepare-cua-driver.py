#!/usr/bin/env python3
"""Verify and prepare the pinned macOS Cua Driver nested helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "cua-driver" / "sources.json"
TARGET_ARCH = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


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
        or driver.get("license") != "MIT"
        or not isinstance(driver.get("release_base_url"), str)
        or driver.get("release_checksums_asset") != "checksums.txt"
        or SHA256.fullmatch(str(driver.get("release_checksums_sha256"))) is None
        or not isinstance(macos, dict)
        or macos.get("asset") != "cua-driver-rs-0.31.0-darwin-universal-binary.tar.gz"
        or SHA256.fullmatch(str(macos.get("sha256"))) is None
        or payload.get("managed_policy") != "policy.yaml"
        or payload.get("tool_snapshot") != "tools-v0.31.0.json"
    ):
        raise ValueError("invalid pinned Cua Driver source manifest")
    if "perception" in json.dumps(payload).lower():
        raise ValueError("Cua perception assets must not enter the Phase 1 package")
    return payload


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


def checksums(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._+-]+)", line)
        if match:
            parsed[match.group(2)] = match.group(1)
    return parsed


def verify_release_inputs(manifest: dict, archive: Path, checksum_file: Path) -> None:
    driver = manifest["driver"]
    asset = driver["macos"]["asset"]
    if digest(checksum_file) != driver["release_checksums_sha256"]:
        raise ValueError("Cua Driver checksums.txt hash mismatch")
    release_hashes = checksums(checksum_file.read_text(encoding="utf-8"))
    expected = driver["macos"]["sha256"]
    if release_hashes.get(asset) != expected:
        raise ValueError("Cua Driver release checksums do not contain the pinned asset hash")
    if digest(archive) != expected:
        raise ValueError("Cua Driver archive hash mismatch")


def safe_member(archive: tarfile.TarFile, name: str) -> tarfile.TarInfo:
    matches = [
        member
        for member in archive.getmembers()
        if member.isfile() and Path(member.name).name == name
    ]
    if len(matches) != 1:
        raise ValueError(f"Cua Driver archive must contain exactly one {name}")
    member = matches[0]
    if member.name.startswith("/") or ".." in Path(member.name).parts:
        raise ValueError("unsafe Cua Driver archive path")
    return member


def run(command: list[str]) -> str:
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)


def prepare(
    target: str,
    *,
    archive: Path | None = None,
    checksum_file: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    manifest = load_manifest()
    arch = TARGET_ARCH[target]
    driver = manifest["driver"]
    base_url = driver["release_base_url"].rstrip("/")
    destination = (output_dir or ROOT / "src-tauri" / "binaries").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"cua-driver-{target}"

    with tempfile.TemporaryDirectory(prefix="avibe-cua-driver-") as temporary:
        work = Path(temporary)
        resolved_checksums = checksum_file or work / "checksums.txt"
        resolved_archive = archive or work / driver["macos"]["asset"]
        if checksum_file is None:
            download(
                f"{base_url}/{driver['release_checksums_asset']}",
                resolved_checksums,
            )
        if archive is None:
            download(f"{base_url}/{driver['macos']['asset']}", resolved_archive)
        verify_release_inputs(manifest, resolved_archive, resolved_checksums)

        universal = work / "cua-driver"
        with tarfile.open(resolved_archive, "r:gz") as source:
            member = safe_member(source, "cua-driver")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ValueError("Cua Driver archive member could not be read")
            with universal.open("wb") as stream:
                shutil.copyfileobj(extracted, stream)
        universal.chmod(0o755)
        universal_sha256 = digest(universal)

        prepared = work / f"cua-driver-{arch}"
        subprocess.run(
            ["lipo", str(universal), "-thin", arch, "-output", str(prepared)],
            check=True,
        )
        info = run(["lipo", "-info", str(prepared)])
        if arch not in info or "Non-fat file" not in info:
            raise ValueError(f"prepared Cua Driver has the wrong architecture: {info}")
        subprocess.run(
            ["codesign", "--verify", "--strict", "--verbose=2", str(prepared)],
            check=True,
        )
        prepared.chmod(0o755)
        temporary_output = destination / f".{output.name}.{os.getpid()}.tmp"
        shutil.copyfile(prepared, temporary_output)
        temporary_output.chmod(0o755)
        os.replace(temporary_output, output)
        provenance = {
            "schema_version": 1,
            "version": driver["version"],
            "tag": driver["tag"],
            "source_commit": driver["source_commit"],
            "target": target,
            "arch": arch,
            "release_checksums_sha256": driver["release_checksums_sha256"],
            "archive": driver["macos"]["asset"],
            "archive_sha256": driver["macos"]["sha256"],
            "extracted_universal_sha256": universal_sha256,
            "thinned_upstream_sha256": digest(output),
            "thinned_signature": "upstream_preserved",
        }
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
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--checksums", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.check_manifest:
        load_manifest()
        if args.target is None:
            return
    if args.target is None:
        parser.error("--target is required unless only --check-manifest is used")
    output = prepare(
        args.target,
        archive=args.archive,
        checksum_file=args.checksums,
        output_dir=args.output_dir,
    )
    print(output)


if __name__ == "__main__":
    main()
