#!/usr/bin/env python3
"""Fetch the published avault that checks engine channel signatures in CI.

The channel's signature check needs avault's keyless ``verify`` verb, first
released in 0.1.10. Jobs that verify an index, and the contract test that runs
avault's shared vectors through Avibe's caller, use that exact published
release: the oldest verifier a client may use, not whichever avault Avibe
currently installs for users. Each archive is pinned by the SHA-256 the avault
release manifest published, and nothing else is accepted.

Prints the path of the extracted binary. It installs nothing outside
``--output-dir`` and touches no avault store.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import sys
import tarfile
from pathlib import Path
from runpy import run_path

download_to_path = run_path(str(Path(__file__).with_name("release_asset_download.py")))["download_to_path"]

VERSION = "0.1.10"
RELEASE_ROOT = f"https://github.com/avibe-bot/avault/releases/download/v{VERSION}"
#: SHA-256 of each v0.1.10 archive, from that release's ``manifest.json``.
ARCHIVES = {
    "linux-arm64": "ffed26317b9557c76913c07cb2dd640b088c1abe21364cb54ad43603c601d17a",
    "linux-x64": "3812bfc870210a65b6b1712a913f6db305366223e94b96f99491da756a760f51",
    "macos-arm64": "a3424c5cbe3abcad3198305603ac8dfa94d96e8a68378fcd2621376e88186e3a",
    "macos-x64": "1db282933bfa2ed61476d21bdbc6ec2503a4aed1f65c9ee9c803d19ef5694700",
    "windows-arm64": "4991460026ea9e18bbf0c9f0117706cdb1fb9f5a2dbf31e3889bb00514da4622",
    "windows-x64": "e4e44f48bdfe3aa6c760b64d4b0fe351e47cf30687ea0823e98f36809dd6e331",
}
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024


def host_target(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    family = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(system)
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    if family is None or arch is None:
        raise SystemExit(f"avault publishes no archive for {system}/{machine}")
    return f"{family}-{arch}"


def extract_verified(archive: Path, target: str, output_dir: Path) -> Path:
    """Check the archive against its pin, then extract the avault binary."""

    if archive.stat().st_size > MAX_ARCHIVE_BYTES or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVES[target]:
        raise SystemExit(f"the avault {VERSION} {target} archive does not match its published digest")
    name = "avault.exe" if target.startswith("windows-") else "avault"
    with tarfile.open(archive, "r:gz") as bundle:
        member = next((item for item in bundle.getmembers() if item.name == name), None)
        if member is None or not member.isfile() or member.size > MAX_BINARY_BYTES:
            raise SystemExit(f"the avault {VERSION} {target} archive holds no {name}")
        stream = bundle.extractfile(member)
        assert stream is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        binary = output_dir / name
        binary.write_bytes(stream.read())
    binary.chmod(0o755)
    return binary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(ARCHIVES), help="default: this host")
    args = parser.parse_args(argv)
    target = args.target or host_target()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"avault-{VERSION}-{target}.tar.gz"
    download_to_path(f"{RELEASE_ROOT}/{archive.name}", archive, max_bytes=MAX_ARCHIVE_BYTES)
    binary = extract_verified(archive, target, args.output_dir)
    # Bytes, not text: a shell that captures this must not receive a carriage return on Windows.
    sys.stdout.buffer.write(f"{binary.as_posix()}\n".encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
