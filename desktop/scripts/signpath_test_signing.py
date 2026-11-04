#!/usr/bin/env python3
"""Independently verify a SignPath TEST-signed Avibe Windows installer.

Only the outer NSIS installer PE is signed; the executables it embeds are not.
The byte checks are portable; the Authenticode checks call Windows PowerShell.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

INSTALLER = "Avibe-test-setup.exe"
# DER SHA-256 of SignPath's self-signed "Test certificate for 'Avibe [OSS]'".
CERTIFICATE_SHA256 = "38f8d097ed29ecd73394e347a401c0d099379e89e1c47a3843f442041829bc0a"
SCOPE = "Outer NSIS installer PE only; the executables it embeds are not signed."
POWERSHELL = "powershell.exe"


class VerificationError(Exception):
    """A verification verdict; `reason` names the check that rejected the file."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def signature_fields(data: bytes) -> tuple[int, int, int, int]:
    """Return the CheckSum offset, security entry offset, and certificate table offset and size."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise VerificationError("payload", "not a PE file")
    header = struct.unpack_from("<I", data, 0x3C)[0]
    optional = header + 24
    if optional + 2 > len(data) or data[header:header + 4] != b"PE\0\0":
        raise VerificationError("payload", "missing PE header")
    optional_size = struct.unpack_from("<H", data, header + 20)[0]
    magic = struct.unpack_from("<H", data, optional)[0]
    layouts = {0x10B: (92, 96), 0x20B: (108, 112)}  # PE32, PE32+
    if magic not in layouts:
        raise VerificationError("payload", "unknown optional header")
    count, directories = layouts[magic]
    entry = directories + 4 * 8  # IMAGE_DIRECTORY_ENTRY_SECURITY
    if entry + 8 > optional_size or optional + optional_size > len(data):
        raise VerificationError("payload", "no security directory")
    if struct.unpack_from("<I", data, optional + count)[0] <= 4:
        raise VerificationError("payload", "no security directory")
    offset, size = struct.unpack_from("<II", data, optional + entry)
    return optional + 64, optional + entry, offset, size


def verify_payload(unsigned: bytes, signed: bytes) -> dict:
    """Require the signed installer to be the unsigned one with only an Authenticode signature added."""
    checksum, entry, offset, size = signature_fields(unsigned)
    if offset or size:
        raise VerificationError("payload", "the unsigned installer already has a certificate table")
    signed_checksum, signed_entry, offset, size = signature_fields(signed)
    end = len(unsigned)
    if (signed_checksum, signed_entry) != (checksum, entry):
        raise VerificationError("payload", "PE headers differ")
    if not size or not end <= offset <= end + 7 or offset + size != len(signed):
        raise VerificationError("payload", "the signature is not one certificate table appended to the installer")
    if any(signed[end:offset]):
        raise VerificationError("payload", "padding before the certificate table is not zero")
    before, after = memoryview(unsigned), memoryview(signed)
    for start, stop in ((0, checksum), (checksum + 4, entry), (entry + 8, end)):
        if before[start:stop] != after[start:stop]:
            raise VerificationError("payload", "installer bytes differ outside the Authenticode fields")
    return {"certificate_table_offset": offset, "certificate_table_size": size}


def powershell(script: str, **env: str) -> str:
    """Run a Windows PowerShell script; a failure is a setup error, never a verdict."""
    prelude = "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n"
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode()
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env={**os.environ, **env}, capture_output=True, text=True, timeout=600,
    )
    if result.returncode:
        raise RuntimeError(f"PowerShell failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


SIGNATURE = r"""
$signature = Get-AuthenticodeSignature -LiteralPath $env:AVIBE_SIGNPATH_FILE
$certificate = $signature.SignerCertificate
$result = [ordered]@{
  status = [string]$signature.Status
  status_message = [string]$signature.StatusMessage
  certificate = ''
  subject = ''
  issuer = ''
  not_after = ''
}
if ($certificate) {
  $result.certificate = [Convert]::ToBase64String($certificate.RawData)
  $result.subject = $certificate.Subject
  $result.issuer = $certificate.Issuer
  $result.not_after = $certificate.NotAfter.ToUniversalTime().ToString('o')
}
$result | ConvertTo-Json -Compress
"""

TRUST = r"""
$raw = [Convert]::FromBase64String($env:AVIBE_SIGNPATH_CERTIFICATE)
$certificate = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 (, $raw)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store ('Root', 'LocalMachine')
$store.Open('ReadWrite')
try {
  $present = $store.Certificates.Find('FindByThumbprint', $certificate.Thumbprint, $false).Count -gt 0
  if ($env:AVIBE_SIGNPATH_TRUST -eq 'add') {
    if ($present) { throw 'the test certificate is already trusted on this runner' }
    $store.Add($certificate)
  } elseif ($present) {
    $store.Remove($certificate)
  }
} finally {
  $store.Close()
}
"""

FOREIGN_SIGNATURE = r"""
$certificate = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Avibe SignPath negative control' `
  -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddDays(1)
try {
  Set-AuthenticodeSignature -LiteralPath $env:AVIBE_SIGNPATH_FILE -Certificate $certificate `
    -HashAlgorithm SHA256 | Out-Null
} finally {
  Remove-Item -LiteralPath "Cert:\CurrentUser\My\$($certificate.Thumbprint)" -DeleteKey
}
"""


def authenticode(path: Path) -> dict:
    result = json.loads(powershell(SIGNATURE, AVIBE_SIGNPATH_FILE=str(path)))
    result["certificate"] = base64.b64decode(result["certificate"])
    return result


@contextmanager
def trusted(certificate: bytes):
    """Trust exactly this certificate on the runner, only for the duration of the block."""
    encoded = base64.b64encode(certificate).decode()
    powershell(TRUST, AVIBE_SIGNPATH_CERTIFICATE=encoded, AVIBE_SIGNPATH_TRUST="add")
    try:
        yield
    finally:
        powershell(TRUST, AVIBE_SIGNPATH_CERTIFICATE=encoded, AVIBE_SIGNPATH_TRUST="remove")


def require_identity(signature: dict, expected: str) -> str:
    if not signature["certificate"]:
        raise VerificationError("signature", f"no signer certificate ({signature['status']})")
    fingerprint = hashlib.sha256(signature["certificate"]).hexdigest()
    if fingerprint != expected:
        raise VerificationError("certificate", f"signer certificate {fingerprint} is not the pinned test certificate")
    return fingerprint


def verify_signed(unsigned: Path, signed: Path, expected: str = CERTIFICATE_SHA256) -> dict:
    """Payload, pinned identity, then a Valid Authenticode verdict under ephemeral trust."""
    payload = verify_payload(unsigned.read_bytes(), signed.read_bytes())
    untrusted = authenticode(signed)
    fingerprint = require_identity(untrusted, expected)
    with trusted(untrusted["certificate"]):
        signature = authenticode(signed)
    require_identity(signature, expected)
    if signature["status"] != "Valid":
        raise VerificationError("signature", f"{signature['status']}: {signature['status_message']}")
    return {
        **payload,
        "certificate_sha256": fingerprint,
        "subject": signature["subject"],
        "issuer": signature["issuer"],
        "not_after": signature["not_after"],
        "status_without_trust": untrusted["status"],
        "status_with_pinned_trust": signature["status"],
    }


def expect_rejection(reason: str, check) -> str:
    """Pass only when the check rejects for exactly this reason; anything else fails the run."""
    try:
        check()
    except VerificationError as error:
        if error.reason != reason:
            raise RuntimeError(f"negative control rejected for {error.reason}, expected {reason}") from error
        return str(error)
    raise RuntimeError(f"negative control was accepted; expected a {reason} rejection")


def negative_controls(unsigned: Path, signed: Path, work: Path) -> dict:
    data = bytearray(signed.read_bytes())
    _, _, offset, _ = signature_fields(bytes(data))
    data[offset // 2] ^= 0xFF  # inside the signed content, past the PE headers
    tampered = work / "tampered" / INSTALLER
    tampered.parent.mkdir()
    tampered.write_bytes(data)
    payload = expect_rejection("payload", lambda: verify_payload(unsigned.read_bytes(), bytes(data)))
    certificate = authenticode(signed)["certificate"]
    with trusted(certificate):
        status = authenticode(tampered)["status"]
    if status != "HashMismatch":
        raise RuntimeError(f"tampered installer reported {status}, expected HashMismatch")

    foreign = work / "foreign" / INSTALLER
    foreign.parent.mkdir()
    shutil.copyfile(unsigned, foreign)
    powershell(FOREIGN_SIGNATURE, AVIBE_SIGNPATH_FILE=str(foreign))
    identity = expect_rejection("certificate", lambda: verify_signed(unsigned, foreign))
    return {
        "tampered_payload": payload,
        "tampered_authenticode": status,
        "foreign_certificate": identity,
    }


def only_installer(directory: Path) -> Path:
    entries = sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*"))
    if entries != [INSTALLER]:
        raise VerificationError("artifact", f"{directory} must contain only {INSTALLER}, found {entries}")
    return directory / INSTALLER


def digest(path: Path) -> dict:
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--unsigned-dir", type=Path, required=True)
    verify.add_argument("--signed-dir", type=Path, required=True)
    verify.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    unsigned = only_installer(args.unsigned_dir)
    signed = only_installer(args.signed_dir)
    signature = verify_signed(unsigned, signed)
    with tempfile.TemporaryDirectory() as work:
        controls = negative_controls(unsigned, signed, Path(work))
    report = {
        "kind": "avibe-signpath-test-signing",
        "production": False,
        "scope": SCOPE,
        "result": "verified",
        "version": os.environ.get("AVIBE_DESKTOP_VERSION", ""),
        "source_sha": os.environ.get("GITHUB_SHA", ""),
        "workflow_run": os.environ.get("GITHUB_RUN_ID", ""),
        "signing_request_id": os.environ.get("SIGNPATH_SIGNING_REQUEST_ID", ""),
        "signing_request_url": os.environ.get("SIGNPATH_SIGNING_REQUEST_URL", ""),
        "unsigned": digest(unsigned),
        "signed": digest(signed),
        "signature": signature,
        "negative_controls": controls,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
