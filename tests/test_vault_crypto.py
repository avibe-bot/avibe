"""Unit tests for storage/vault_crypto.py (P0 standard-tier envelope).

These pass an explicit ``key_path`` under ``tmp_path`` so the machine key is written
to the test's temp dir, never the real ``~/.avibe`` state dir.
"""

from __future__ import annotations

import os
import stat

import pytest

from storage import vault_crypto
from storage.vault_crypto import Sealed, VaultCryptoError


def test_seal_open_round_trip(tmp_path):
    key_path = tmp_path / "machine.key"
    secret = b"sk-ant-super-secret-value"
    sealed = vault_crypto.seal_standard(secret, key_path=key_path)
    assert isinstance(sealed, Sealed)
    # Nothing in the envelope leaks the plaintext.
    assert b"super-secret" not in sealed.ciphertext.encode()
    assert vault_crypto.open_standard(sealed, key_path=key_path) == secret


def test_each_seal_uses_fresh_dek_and_nonce(tmp_path):
    key_path = tmp_path / "machine.key"
    a = vault_crypto.seal_standard(b"same value", key_path=key_path)
    b = vault_crypto.seal_standard(b"same value", key_path=key_path)
    assert a.ciphertext != b.ciphertext  # different nonce/DEK each time
    assert a.nonce != b.nonce


def test_open_with_wrong_machine_key_fails(tmp_path):
    sealed = vault_crypto.seal_standard(b"value", machine_key=os.urandom(32))
    with pytest.raises(VaultCryptoError):
        vault_crypto.open_standard(sealed, machine_key=os.urandom(32))


def test_open_tampered_ciphertext_fails(tmp_path):
    key = os.urandom(32)
    sealed = vault_crypto.seal_standard(b"value", machine_key=key)
    tampered = Sealed(ciphertext="YWJjZA==", nonce=sealed.nonce, wrap_meta=sealed.wrap_meta)
    with pytest.raises(VaultCryptoError):
        vault_crypto.open_standard(tampered, machine_key=key)


def test_open_unknown_scheme_fails(tmp_path):
    bad = Sealed(ciphertext="AA==", nonce="AA==", wrap_meta='{"scheme": "nope"}')
    with pytest.raises(VaultCryptoError):
        vault_crypto.open_standard(bad, machine_key=os.urandom(32))


def test_get_or_create_machine_key_is_idempotent_and_0600(tmp_path):
    key_path = tmp_path / "vault" / "machine.key"
    first = vault_crypto.get_or_create_machine_key(key_path)
    second = vault_crypto.get_or_create_machine_key(key_path)
    assert first == second
    assert len(first) == 32
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600


def test_existing_loose_machine_key_is_tightened_on_read(tmp_path):
    # A key restored from backup / copied with 0644 must be repaired to 0600 before use — it
    # decrypts every standard-tier secret and the feature relies on that mode.
    key_path = tmp_path / "machine.key"
    created = vault_crypto.get_or_create_machine_key(key_path)
    os.chmod(key_path, 0o644)
    read_back = vault_crypto.get_machine_key(key_path)  # read repairs the mode
    assert read_back == created
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    # get_or_create takes the same repair path for an already-present key.
    os.chmod(key_path, 0o640)
    vault_crypto.get_or_create_machine_key(key_path)
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600


def test_corrupt_machine_key_length_rejected(tmp_path):
    key_path = tmp_path / "machine.key"
    key_path.write_bytes(b"too-short")
    with pytest.raises(VaultCryptoError):
        vault_crypto.get_or_create_machine_key(key_path)


def test_open_does_not_create_a_key(tmp_path):
    # Decrypting with a missing key must raise — never silently create a wrong key.
    sealed = vault_crypto.seal_standard(b"v", machine_key=os.urandom(32))
    missing = tmp_path / "absent" / "machine.key"
    with pytest.raises(VaultCryptoError):
        vault_crypto.open_standard(sealed, key_path=missing)
    assert not missing.exists()


@pytest.mark.parametrize(
    "name,valid",
    [
        ("OPENAI_API_KEY", True),
        ("A", True),
        ("A1_B2", True),
        ("lowercase", False),
        ("1LEADING_DIGIT", False),
        ("HAS-DASH", False),
        ("HAS SPACE", False),
        ("", False),
        (None, False),
    ],
)
def test_is_valid_secret_name(name, valid):
    assert vault_crypto.is_valid_secret_name(name) is valid
