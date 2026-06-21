"""Protected-tier envelope format reference (P1, vaults.md §7.1). Pure crypto, no I/O."""

from __future__ import annotations

import pytest

from storage import vault_protected as vp
from storage.vault_protected import ProtectedFormatError


def test_password_unwraps_vmk_and_secret_round_trips():
    vmk = vp.new_vmk()
    wrap_meta = vp.build_wrap_meta(vmk, ["correct horse battery staple"])
    sealed = vp.seal_protected(b"the protected value", vmk)

    # Recover the VMK from the password, then open the secret.
    recovered = vp.unwrap_vmk(wrap_meta, "correct horse battery staple")
    assert recovered == vmk
    assert vp.open_protected(sealed, recovered) == b"the protected value"


def test_wrong_password_cannot_unwrap():
    vmk = vp.new_vmk()
    wrap_meta = vp.build_wrap_meta(vmk, ["right"])
    with pytest.raises(ProtectedFormatError):
        vp.unwrap_vmk(wrap_meta, "wrong")


def test_multi_factor_either_password_unwraps_same_vmk():
    vmk = vp.new_vmk()
    wrap_meta = vp.build_wrap_meta(vmk, ["laptop-pass", "phone-pass"])
    assert vp.unwrap_vmk(wrap_meta, "laptop-pass") == vmk
    assert vp.unwrap_vmk(wrap_meta, "phone-pass") == vmk


def test_add_copy_is_a_rotation_not_a_reencrypt():
    # Adding a password copy wraps the SAME vmk — existing secrets stay valid (only the
    # tiny wrap_meta changes, never the ciphertext). This is the cheap-rekey property.
    vmk = vp.new_vmk()
    sealed = vp.seal_protected(b"v", vmk)
    wrap_meta = vp.build_wrap_meta(vmk, ["old-pass"])
    wrap_meta = vp.add_password_copy(wrap_meta, vmk, "new-pass")
    assert vp.unwrap_vmk(wrap_meta, "old-pass") == vmk
    assert vp.unwrap_vmk(wrap_meta, "new-pass") == vmk
    assert vp.open_protected(sealed, vp.unwrap_vmk(wrap_meta, "new-pass")) == b"v"


def test_open_with_wrong_vmk_fails():
    vmk = vp.new_vmk()
    sealed = vp.seal_protected(b"v", vmk)
    with pytest.raises(ProtectedFormatError):
        vp.open_protected(sealed, vp.new_vmk())


def test_build_requires_a_factor():
    with pytest.raises(ProtectedFormatError):
        vp.build_wrap_meta(vp.new_vmk(), [])


def test_each_seal_is_unique():
    vmk = vp.new_vmk()
    a = vp.seal_protected(b"same", vmk)
    b = vp.seal_protected(b"same", vmk)
    assert a.ciphertext != b.ciphertext and a.nonce != b.nonce
