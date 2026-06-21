"""machine-key export/import (P1, §7.2). All crypto uses explicit tmp key paths."""

from __future__ import annotations

import argparse
import io
import json

import pytest

from storage import vault_crypto
from storage.vault_crypto import VaultCryptoError
from vibe import cli


def test_export_import_preserves_key_so_secrets_still_open(tmp_path):
    src = tmp_path / "machine.key"
    sealed = vault_crypto.seal_standard(b"the-secret", key_path=src)
    blob = vault_crypto.export_machine_key("pass-phrase-1", key_path=src)
    dst = tmp_path / "restored" / "machine.key"
    vault_crypto.import_machine_key(blob, "pass-phrase-1", key_path=dst)
    # The restored key opens a secret sealed under the original — the whole point.
    assert vault_crypto.open_standard(sealed, key_path=dst) == b"the-secret"
    assert src.read_bytes() == dst.read_bytes()


def test_wrong_passphrase_fails(tmp_path):
    blob = vault_crypto.export_machine_key("right", key_path=tmp_path / "machine.key")
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "wrong", key_path=tmp_path / "d" / "machine.key")


def test_refuses_overwrite_without_force(tmp_path):
    src = tmp_path / "machine.key"
    blob = vault_crypto.export_machine_key("p", key_path=src)
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "p", key_path=src)  # src already exists
    vault_crypto.import_machine_key(blob, "p", key_path=src, force=True)  # force is allowed


def test_tampered_blob_fails(tmp_path):
    blob = vault_crypto.export_machine_key("p", key_path=tmp_path / "machine.key")
    blob["ciphertext"] = vault_crypto._b64(b"x" * 48)
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "p", key_path=tmp_path / "d" / "machine.key")


def test_unrecognized_blob_rejected(tmp_path):
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key({"scheme": "nope"}, "p", key_path=tmp_path / "machine.key")


def test_empty_passphrase_rejected(tmp_path):
    with pytest.raises(VaultCryptoError):
        vault_crypto.export_machine_key("", key_path=tmp_path / "machine.key")


def test_cli_export_then_import_roundtrip(tmp_path, monkeypatch, capfd):
    # Export the (auto-created) machine key from the isolated home to a file.
    out = tmp_path / "vault-key.json"
    monkeypatch.setattr("sys.stdin", io.StringIO("my-passphrase\n"))
    assert cli.cmd_vault_key_export(argparse.Namespace(out=str(out))) == 0
    payload = json.loads(capfd.readouterr().out)
    assert payload["written"] is True
    # The export blob holds the wrapped key — it must be 0600 from creation.
    import os
    import stat

    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    blob = json.loads(out.read_text())
    assert blob["scheme"] == vault_crypto.EXPORT_SCHEME

    # Import it back (force, since the home already has the key) — must succeed.
    monkeypatch.setattr("sys.stdin", io.StringIO("my-passphrase\n"))
    assert cli.cmd_vault_key_import(argparse.Namespace(file=str(out), force=True)) == 0
    assert json.loads(capfd.readouterr().out)["imported"] is True
