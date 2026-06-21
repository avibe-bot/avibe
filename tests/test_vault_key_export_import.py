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
    src = tmp_path / "machine.key"
    vault_crypto.get_or_create_machine_key(src)
    blob = vault_crypto.export_machine_key("right", key_path=src)
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "wrong", key_path=tmp_path / "d" / "machine.key")


def test_refuses_overwrite_without_force(tmp_path):
    src = tmp_path / "machine.key"
    vault_crypto.get_or_create_machine_key(src)
    blob = vault_crypto.export_machine_key("p", key_path=src)
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "p", key_path=src)  # src already exists
    vault_crypto.import_machine_key(blob, "p", key_path=src, force=True)  # force is allowed


def test_tampered_blob_fails(tmp_path):
    src = tmp_path / "machine.key"
    vault_crypto.get_or_create_machine_key(src)
    blob = vault_crypto.export_machine_key("p", key_path=src)
    blob["ciphertext"] = vault_crypto._b64(b"x" * 48)
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "p", key_path=tmp_path / "d" / "machine.key")


def test_failed_force_import_preserves_existing_key(tmp_path):
    # A failed --force import (corrupt/undecryptable blob) must NOT destroy the live key —
    # decryption is verified before the file is touched, and the write is an atomic replace.
    src = tmp_path / "machine.key"
    original = vault_crypto.get_or_create_machine_key(src)
    blob = vault_crypto.export_machine_key("right", key_path=src)
    blob["ciphertext"] = vault_crypto._b64(b"x" * 48)  # corrupt → decrypt fails
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "right", key_path=src, force=True)
    assert src.read_bytes() == original  # untouched; secrets sealed under it still open


def test_export_refuses_when_no_machine_key(tmp_path):
    # Export must back up an EXISTING key, never mint one — minting on "export" would write a
    # fresh random key and silently orphan any secrets sealed under the key the user expected.
    missing = tmp_path / "machine.key"
    with pytest.raises(VaultCryptoError):
        vault_crypto.export_machine_key("p", key_path=missing)
    assert not missing.exists()  # and it must not have created one as a side effect


def test_import_rejects_out_of_bounds_kdf_params(tmp_path):
    src = tmp_path / "machine.key"
    vault_crypto.get_or_create_machine_key(src)
    blob = vault_crypto.export_machine_key("p", key_path=src)
    blob["n"] = 2**30  # absurd scrypt cost — must be refused before any derivation runs
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key(blob, "p", key_path=tmp_path / "d" / "machine.key")


def test_unrecognized_blob_rejected(tmp_path):
    with pytest.raises(VaultCryptoError):
        vault_crypto.import_machine_key({"scheme": "nope"}, "p", key_path=tmp_path / "machine.key")


def test_empty_passphrase_rejected(tmp_path):
    with pytest.raises(VaultCryptoError):
        vault_crypto.export_machine_key("", key_path=tmp_path / "machine.key")


def test_cli_export_then_import_roundtrip(tmp_path, monkeypatch, capfd):
    # The machine key must already exist (export backs up an existing key, never mints one);
    # the isolated home (conftest VIBE_REMOTE_HOME) has none until we seed it.
    vault_crypto.get_or_create_machine_key()
    # Export the machine key from the isolated home to a file.
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


def test_cli_export_writes_audit_event(tmp_path, monkeypatch, capfd):
    # Exporting the machine key is the most sensitive vault op, so it must leave a value-free
    # audit trail for the activity panel.
    from storage.models import vault_audit

    vault_crypto.get_or_create_machine_key()
    out = tmp_path / "vault-key.json"
    secret_phrase = "super-secret-passphrase-ZZZ"
    monkeypatch.setattr("sys.stdin", io.StringIO(secret_phrase + "\n"))
    assert cli.cmd_vault_key_export(argparse.Namespace(out=str(out))) == 0
    capfd.readouterr()
    engine = cli._open_vault_engine()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    assert "key_exported" in {r["event"] for r in rows}
    # The audit row never carries the passphrase.
    assert all(secret_phrase not in json.dumps(r) for r in rows)
