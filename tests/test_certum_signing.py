from __future__ import annotations

import struct
from pathlib import Path
import subprocess

import pytest

from scripts import certum_signing


def unsigned_pe(size: int = 0x240) -> bytes:
    data = bytearray(size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 20, 224)
    struct.pack_into("<H", data, 0x80 + 24, 0x10B)
    return bytes(data)


def signed_pe(unsigned: bytes) -> bytes:
    data = bytearray(unsigned + b"\0" * ((-len(unsigned)) % 8))
    optional = 0x80 + 24
    checksum = optional + 64
    security = optional + 96 + 32
    payload = b"pkcs7 fixture"
    table = struct.pack("<IHH", 8 + len(payload), 0x0200, 0x0002) + payload
    struct.pack_into("<I", data, checksum, 0x5A5A)
    struct.pack_into("<II", data, security, len(data), len(table))
    return bytes(data) + table


def test_payload_verification_accepts_only_authenticode_append(tmp_path: Path):
    original = tmp_path / "unsigned.exe"
    signed = tmp_path / "signed.exe"
    original.write_bytes(unsigned_pe())
    signed.write_bytes(signed_pe(original.read_bytes()))
    certum_signing.verify_payload_unchanged(original, signed)


def test_payload_verification_rejects_payload_mutation(tmp_path: Path):
    original = tmp_path / "unsigned.exe"
    signed = tmp_path / "signed.exe"
    original.write_bytes(unsigned_pe())
    data = bytearray(signed_pe(original.read_bytes()))
    data[0x100] ^= 1
    signed.write_bytes(data)
    with pytest.raises(certum_signing.SigningError, match="payload bytes"):
        certum_signing.verify_payload_unchanged(original, signed)


def test_signer_failure_is_classified_without_returning_raw_output(tmp_path: Path, monkeypatch):
    installer = tmp_path / "installer.exe"
    signer = tmp_path / "ssign.exe"
    installer.write_bytes(unsigned_pe())
    signer.write_bytes(b"fixture")
    monkeypatch.setenv("CERTUM_EMAIL", "person@example.invalid")
    monkeypatch.setenv("CERTUM_OTP", "otpauth://totp/example?secret=not-for-output")

    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout=b"access_token=do-not-leak", stderr=b"oauth failure"
        )

    monkeypatch.setattr(certum_signing.subprocess, "run", failed_run)
    with pytest.raises(certum_signing.SigningError, match="rejected the signing request") as error:
        certum_signing.sign(installer, signer, tmp_path / "output")
    assert "access_token" not in str(error.value)
    assert "oauth" not in str(error.value)


def test_signer_environment_uses_file_cache_block(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CERTUM_EMAIL", "person@example.invalid")
    monkeypatch.setenv("CERTUM_OTP", "otpauth://totp/example?secret=not-for-output")
    sentinel = tmp_path / "cache-sentinel"
    sentinel.write_bytes(b"not-a-directory")
    environment = certum_signing._signer_environment(sentinel)
    assert environment["XDG_RUNTIME_DIR"] == str(sentinel)
    assert environment["CERTUM_EMAIL"] == "person@example.invalid"
    assert environment["CERTUM_OTP"].startswith("otpauth://")
    assert "HOME" not in environment
    assert "USERPROFILE" not in environment
