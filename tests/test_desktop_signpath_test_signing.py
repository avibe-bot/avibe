"""Byte-level half of the SignPath TEST-signing verifier; the Authenticode half runs on Windows CI."""

from __future__ import annotations

from pathlib import Path
import struct
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop" / "scripts"))
import signpath_test_signing as verifier  # noqa: E402


PE_HEADER = 0x80
# Optional-header size, NumberOfRvaAndSizes offset, and data-directory offset per the PE/COFF spec.
LAYOUTS = {0x10B: (224, 92, 96), 0x20B: (240, 108, 112)}


def installer(magic=0x10B, overlay=b"NSIS overlay " * 101):
    size, count, _ = LAYOUTS[magic]
    data = bytearray(PE_HEADER + 24 + size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, PE_HEADER)
    data[PE_HEADER:PE_HEADER + 4] = b"PE\0\0"
    struct.pack_into("<H", data, PE_HEADER + 20, size)
    struct.pack_into("<H", data, PE_HEADER + 24, magic)
    struct.pack_into("<I", data, PE_HEADER + 24 + count, 16)
    return bytes(data) + overlay


def fields(magic):
    optional = PE_HEADER + 24
    return optional + 64, optional + LAYOUTS[magic][2] + 4 * 8


def sign(unsigned, magic=0x10B, trailing=b""):
    """What signtool does: pad to 8 bytes, append WIN_CERTIFICATE, set CheckSum and the security entry."""
    table = struct.pack("<IHH", 40, 0x0200, 0x0002) + b"pkcs7 der" * 4
    data = bytearray(unsigned + b"\0" * ((-len(unsigned)) % 8))
    checksum, entry = fields(magic)
    struct.pack_into("<I", data, checksum, 0x5A5A)
    struct.pack_into("<II", data, entry, len(data), len(table))
    return bytes(data + table + trailing)


def flip(data, offset):
    changed = bytearray(data)
    changed[offset] ^= 0xFF
    return bytes(changed)


@pytest.mark.parametrize("magic", [0x10B, 0x20B])
def test_payload_accepts_only_an_added_authenticode_signature(magic):
    unsigned = installer(magic)
    assert len(unsigned) % 8  # the signer must pad
    signed = sign(unsigned, magic)
    assert verifier.verify_payload(unsigned, signed) == {
        "certificate_table_offset": len(unsigned) + (-len(unsigned)) % 8,
        "certificate_table_size": len(signed) - len(unsigned) - (-len(unsigned)) % 8,
    }


UNSIGNED = installer()
SIGNED = sign(UNSIGNED)


@pytest.mark.parametrize(("unsigned", "signed", "message"), [
    (UNSIGNED, flip(SIGNED, len(UNSIGNED) - 7), "differ outside the Authenticode fields"),
    (UNSIGNED, flip(SIGNED, PE_HEADER + 8), "differ outside the Authenticode fields"),
    (UNSIGNED, flip(SIGNED, len(UNSIGNED) + 1), "padding before the certificate table is not zero"),
    (UNSIGNED, sign(UNSIGNED, trailing=b"appended"), "not one certificate table appended"),
    (UNSIGNED, sign(UNSIGNED[:-16]), "not one certificate table appended"),
    (UNSIGNED, UNSIGNED, "not one certificate table appended"),
    (SIGNED, sign(SIGNED), "already has a certificate table"),
    (b"not a PE" * 16, SIGNED, "not a PE file"),
])
def test_payload_rejects_any_other_change(unsigned, signed, message):
    with pytest.raises(verifier.VerificationError, match=message) as error:
        verifier.verify_payload(unsigned, signed)
    assert error.value.reason == "payload"


def test_negative_controls_pass_only_on_their_own_rejection_reason():
    def rejects(reason):
        def check():
            raise verifier.VerificationError(reason, "fixture")
        return check

    assert verifier.expect_rejection("payload", rejects("payload")) == "payload: fixture"
    with pytest.raises(RuntimeError, match="rejected for certificate, expected payload"):
        verifier.expect_rejection("payload", rejects("certificate"))
    with pytest.raises(RuntimeError, match="was accepted"):
        verifier.expect_rejection("payload", lambda: None)

    def setup_failure():
        raise RuntimeError("PowerShell failed")

    with pytest.raises(RuntimeError, match="PowerShell failed"):
        verifier.expect_rejection("payload", setup_failure)
