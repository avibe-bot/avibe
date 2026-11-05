#!/usr/bin/env python3
"""Run the pinned Certum SimplySign Authenticode signer without leaking credentials.

The workflow supplies CERTUM_EMAIL and CERTUM_OTP only to this short-lived
process.  Signer output is captured in memory because the upstream CLI may
include OAuth details in an error.  On Windows XDG_RUNTIME_DIR points at a
regular file, so ssign-core's Windows temp-directory fallback cannot create
its bearer-token cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import urllib.request
import zipfile

SIGNER_VERSION = "0.1.9"
SIGNER_ARCHIVE_URL = (
    "https://github.com/Le-Syl21/ssign/releases/download/"
    f"v{SIGNER_VERSION}/ssign-windows-x86_64.zip"
)
SIGNER_ARCHIVE_SHA256 = "c5406a993d4a952aa76bea494c173708103d1ac33817151b1d5dfd0faa16252a"
SIGNER_ARCHIVE_NAME = "ssign-windows-x86_64.zip"
SIGNER_BINARY_NAME = "ssign.exe"


class SigningError(RuntimeError):
    """A fixed, non-secret signing failure classification."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_signer(destination: Path) -> Path:
    """Download and extract exactly the pinned upstream Windows binary."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / SIGNER_ARCHIVE_NAME
    with urllib.request.urlopen(SIGNER_ARCHIVE_URL, timeout=120) as response, archive.open("wb") as stream:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            if not block:
                break
            stream.write(block)
    if sha256(archive) != SIGNER_ARCHIVE_SHA256:
        raise SigningError("pinned Certum signer archive digest mismatch")

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if names != [SIGNER_BINARY_NAME]:
            raise SigningError("pinned Certum signer archive contents changed")
        info = bundle.getinfo(SIGNER_BINARY_NAME)
        if info.is_dir() or info.filename != SIGNER_BINARY_NAME:
            raise SigningError("invalid pinned Certum signer archive entry")
        signer = destination / SIGNER_BINARY_NAME
        signer.write_bytes(bundle.read(info))
    if not signer.is_file() or signer.is_symlink() or signer.stat().st_size == 0:
        raise SigningError("pinned Certum signer was not extracted")
    return signer


def _signer_environment(cache_sentinel: Path) -> dict[str, str]:
    # Keep only runtime variables needed by a Windows executable and the two
    # explicitly mapped secrets.  In particular, do not inherit HOME or any
    # unrelated CI credentials.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if os.environ.get(key)
    }
    email = os.environ.get("CERTUM_EMAIL", "").strip()
    otp = os.environ.get("CERTUM_OTP", "").strip()
    if not email or not otp.startswith("otpauth://"):
        raise SigningError("Certum signing credentials are missing or malformed")
    environment.update(
        {
            "CERTUM_EMAIL": email,
            "CERTUM_OTP": otp,
            # ssign-core resolves Windows cache_path from XDG_RUNTIME_DIR
            # before falling back to %TEMP%. A regular file makes the cache
            # path uncreatable; verify this invariant after the child exits.
            "XDG_RUNTIME_DIR": str(cache_sentinel),
        }
    )
    return environment


def sign(installer: Path, signer: Path, output_dir: Path) -> Path:
    """Sign one installer once and return the signed output path."""
    if installer.is_symlink() or not installer.is_file() or installer.stat().st_size == 0:
        raise SigningError("Certum signing input is missing or invalid")
    if signer.is_symlink() or not signer.is_file() or signer.stat().st_size == 0:
        raise SigningError("Certum signer is missing or invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SigningError("Certum signing output directory must be empty")

    with tempfile.TemporaryDirectory(prefix="certum-cache-", dir=output_dir.parent) as temporary:
        cache_sentinel = Path(temporary) / "runtime-file"
        cache_sentinel.write_bytes(b"cache-disabled")
        environment = _signer_environment(cache_sentinel)
        try:
            result = subprocess.run(
                [str(signer), "-o", str(output_dir), "-n", "Avibe Windows installer", str(installer)],
                cwd=temporary,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SigningError("Certum signer timed out; remote outcome is unknown") from exc
        if result.returncode != 0:
            # Never include stdout/stderr: ssign errors may contain bearer or
            # OAuth material.  A failed invocation must not be retried here.
            raise SigningError("Certum signer rejected the signing request")
        cache_path = cache_sentinel / "ssign" / "session.json"
        if cache_path.exists():
            raise SigningError("Certum signer created a bearer-token cache")

    signed = output_dir / installer.name
    if signed.is_symlink() or not signed.is_file() or signed.stat().st_size <= installer.stat().st_size:
        raise SigningError("Certum signer did not produce a signed installer")
    return signed


def _pe_security_layout(data: bytes) -> tuple[int, int, int, int]:
    if data[:2] != b"MZ":
        raise SigningError("installer is not a PE image")
    nt = struct.unpack_from("<I", data, 0x3C)[0]
    if data[nt : nt + 4] != b"PE\0\0":
        raise SigningError("installer has an invalid PE header")
    optional = nt + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise SigningError("installer has an unsupported PE format")
    security_directory = optional + (96 if magic == 0x10B else 112) + 32
    certificate_offset, certificate_size = struct.unpack_from("<II", data, security_directory)
    return optional + 64, security_directory, certificate_offset, certificate_size


def verify_payload_unchanged(original: Path, signed: Path) -> None:
    """Verify Authenticode changed only the checksum/security-directory area."""
    before = original.read_bytes()
    after = signed.read_bytes()
    checksum, security, offset, size = _pe_security_layout(after)
    _, _, before_offset, before_size = _pe_security_layout(before)
    if before_offset or before_size:
        raise SigningError("Certum signing input already carries an Authenticode signature")
    if not (len(before) <= offset <= len(before) + 7):
        raise SigningError("Certum signer moved the PE certificate table unexpectedly")
    if offset + size != len(after) or size <= 8:
        raise SigningError("Certum signer produced an invalid PE certificate table")
    for start, end in ((0, checksum), (checksum + 4, security), (security + 8, len(before))):
        if before[start:end] != after[start:end]:
            raise SigningError("Certum signer changed installer payload bytes")
    length, revision, kind = struct.unpack_from("<IHH", after, offset)
    if revision != 0x200 or kind != 2 or not (8 < length <= size):
        raise SigningError("Certum signer produced an invalid Authenticode record")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download")
    download.add_argument("--directory", type=Path, required=True)
    signing = sub.add_parser("sign")
    signing.add_argument("--installer", type=Path, required=True)
    signing.add_argument("--signer", type=Path, required=True)
    signing.add_argument("--output-directory", type=Path, required=True)
    payload = sub.add_parser("check-payload")
    payload.add_argument("--original", type=Path, required=True)
    payload.add_argument("--signed", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "download":
            signer = download_signer(args.directory)
            print(json.dumps({"status": "downloaded", "signer": signer.name, "version": SIGNER_VERSION}))
        elif args.command == "sign":
            signed = sign(args.installer, args.signer, args.output_directory)
            verify_payload_unchanged(args.installer, signed)
            print(json.dumps({"status": "signed", "sha256": sha256(signed), "signer": SIGNER_VERSION}))
        else:
            verify_payload_unchanged(args.original, args.signed)
            print(json.dumps({"status": "payload-unchanged"}))
        return 0
    except SigningError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
