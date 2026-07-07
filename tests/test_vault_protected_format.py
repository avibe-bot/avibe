"""Protected-tier envelope reference. Pure crypto, no I/O."""

from __future__ import annotations

import pytest

from storage import vault_protected as vp
from storage.vault_protected import ProtectedFormatError


def test_vmk_opens_protected_secret_round_trip():
    vmk = vp.new_vmk()
    sealed = vp.seal_protected(b"the protected value", vmk)

    assert vp.open_protected(sealed, vmk) == b"the protected value"


def test_open_with_wrong_vmk_fails():
    vmk = vp.new_vmk()
    sealed = vp.seal_protected(b"v", vmk)

    with pytest.raises(ProtectedFormatError):
        vp.open_protected(sealed, vp.new_vmk())


def test_each_seal_is_unique():
    vmk = vp.new_vmk()
    a = vp.seal_protected(b"same", vmk)
    b = vp.seal_protected(b"same", vmk)
    assert a.ciphertext != b.ciphertext and a.nonce != b.nonce
