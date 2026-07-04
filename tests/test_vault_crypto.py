"""Tiny contract tests for storage/vault_crypto.py.

All value cryptography now lives in the external avault custody core. Python only owns
secret-name validation, the persisted envelope shape, and a compatibility exception.
"""

from __future__ import annotations

import pytest

from storage import vault_crypto
from storage.vault_crypto import Sealed, VaultCryptoError


def test_sealed_is_text_envelope_shape():
    sealed = Sealed(ciphertext="ct", nonce="n", wrap_meta='{"scheme":"machine-aesgcm-v1"}')

    assert sealed.ciphertext == "ct"
    assert sealed.nonce == "n"
    assert sealed.wrap_meta == '{"scheme":"machine-aesgcm-v1"}'


def test_vault_crypto_error_kept_for_back_compat():
    assert issubclass(VaultCryptoError, Exception)


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("OPENAI_API_KEY", True),
        ("openAiKey", True),
        ("_localKey", True),
        ("A", True),
        ("A1_B2", True),
        ("lowercase", True),
        ("1LEADING_DIGIT", False),
        ("HAS-DASH", False),
        ("HAS SPACE", False),
        ("", False),
        (None, False),
    ],
)
def test_is_valid_secret_name(name, valid):
    assert vault_crypto.is_valid_secret_name(name) is valid
