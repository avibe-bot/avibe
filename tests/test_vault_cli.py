"""CLI tests for ``vibe vault`` orchestration around the avault client.

These tests mock avault: Avibe stores and routes envelopes, but never decrypts values.
"""

from __future__ import annotations

import argparse
import io
import json
from unittest.mock import Mock

import pytest

from storage import vault_service
from storage.models import vault_audit
from storage.vault_crypto import Sealed
from vibe import cli


def _ns(**kw):
    base = dict(
        name=None,
        stdin=False,
        from_file=None,
        group=None,
        tag=None,
        description=None,
        allow_host=None,
        auth_header=None,
        auth_query=None,
        env=None,
        command_argv=None,
        reason=None,
        skill=None,
        wait=None,
        no_wait=False,
        json=False,
        out=None,
        file=None,
        force=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _set_secret(name: str, value: str, tmp_path, monkeypatch, capfd, *, sealed: Sealed | None = None):
    from vibe import api

    vf = tmp_path / f"{name}.txt"
    vf.write_text(value)
    seal_mock = Mock(return_value=sealed or _sealed())
    monkeypatch.setattr(api, "avault_seal", seal_mock)
    assert cli.cmd_vault_set(_ns(name=name, from_file=str(vf))) == 0
    capfd.readouterr()
    return seal_mock


@pytest.mark.parametrize(
    "specs,expected",
    [
        (["OPENAI_API_KEY"], {"OPENAI_API_KEY": "OPENAI_API_KEY"}),
        (["LOCAL=VAULT_NAME"], {"LOCAL": "VAULT_NAME"}),
        (["A,B"], {"A": "A", "B": "B"}),
        (["A", "B=C"], {"A": "A", "B": "C"}),
    ],
)
def test_parse_env_specs(specs, expected):
    assert cli._parse_env_specs(specs) == expected


def test_set_seals_with_avault_and_stores_preview(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / "value.txt"
    vf.write_text("sk-ant-abcd1234")
    seal = Mock(return_value=_sealed("saved"))
    monkeypatch.setattr(api, "avault_seal", seal)

    assert cli.cmd_vault_set(_ns(name="OPENAI_API_KEY", from_file=str(vf), description="key")) == 0
    payload = json.loads(capfd.readouterr().out)
    secret = payload["secret"]
    assert secret["name"] == "OPENAI_API_KEY"
    assert secret["preview"] == "…1234"
    assert "sk-ant-abcd1234" not in json.dumps(payload)
    seal.assert_called_once_with("OPENAI_API_KEY", b"sk-ant-abcd1234")
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.get_envelope(conn, "OPENAI_API_KEY") == _sealed("saved")


def test_set_rejects_invalid_name_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / "v.txt"
    vf.write_text("x")
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal", seal)

    code = cli.cmd_vault_set(_ns(name="lower_bad", from_file=str(vf)))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "invalid_name"
    seal.assert_not_called()


def test_set_requires_one_value_source(capfd):
    code = cli.cmd_vault_set(_ns(name="NO_SOURCE"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "missing_value_source"


def test_set_maps_avault_failure(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / "v.txt"
    vf.write_text("x")
    monkeypatch.setattr(api, "avault_seal", Mock(side_effect=api.AvaultError("boom")))

    code = cli.cmd_vault_set(_ns(name="FAIL_KEY", from_file=str(vf)))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"


def test_run_calls_avault_with_env_mapping_and_records_delivery(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set_secret("SRC_KEY", "secret-RUNVAL-42", tmp_path, monkeypatch, capfd, sealed=_sealed("run"))
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["LOCAL_NAME=SRC_KEY"], command_argv=["python3", "-c", "pass"]))
    assert code == 0
    deliver.assert_called_once_with(
        [{"name": "SRC_KEY", "env": "LOCAL_NAME", "envelope": _sealed("run")}],
        ["python3", "-c", "pass"],
    )
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 1
    assert secret["last_used_at"] is not None


def test_run_skips_delivery_audit_when_avault_returns_70(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set_secret("NODELIVER_KEY", "secret", tmp_path, monkeypatch, capfd)
    monkeypatch.setattr(api, "avault_deliver_run", Mock(return_value=70))

    assert cli.cmd_vault_run(_ns(env=["NODELIVER_KEY"], command_argv=["python3", "-c", "pass"])) == 70
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 0


def test_run_bad_command_does_not_call_avault_or_deliver(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set_secret("NODELIVER_KEY", "v", tmp_path, monkeypatch, capfd)
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["NODELIVER_KEY"], command_argv=["definitely-not-a-real-binary-xyz123"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "command_not_found"
    deliver.assert_not_called()
    cli.cmd_vault_list(_ns())
    assert json.loads(capfd.readouterr().out)["secrets"][0]["use_count"] == 0


def test_run_missing_secret_is_clean_error(capfd):
    code = cli.cmd_vault_run(_ns(env=["NOPE"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()
    assert code == 1
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["code"] == "secret_not_found"


def test_run_rejects_bad_env_name(capfd):
    code = cli.cmd_vault_run(_ns(env=["BAD-NAME=KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "invalid_env_name"


def test_export_is_deprecated_and_does_not_touch_db(capfd):
    code = cli.cmd_vault_export(_ns(env=["OPENAI_API_KEY"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "export_deprecated"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_secrets(conn) == []


def test_request_creates_pending(capfd):
    code = cli.cmd_vault_request(_ns(name="WANTED_KEY", reason="need it"))
    captured = capfd.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["secret_name"] == "WANTED_KEY"
    assert payload["status"] == "pending"
    assert payload["request_id"].startswith("vrq_")


def test_request_for_existing_secret_returns_fulfilled(tmp_path, capfd, monkeypatch):
    _set_secret("HAVE_KEY", "v", tmp_path, monkeypatch, capfd)
    assert cli.cmd_vault_request(_ns(name="HAVE_KEY", wait=30)) == 0
    assert json.loads(capfd.readouterr().out)["status"] == "fulfilled"


def test_from_file_preserves_trailing_newline(tmp_path):
    vf = tmp_path / "key.pem"
    vf.write_text("-----BEGIN-----\nabc\n-----END-----\n")
    value = cli._read_secret_value(_ns(from_file=str(vf)), help_command="x")
    assert value == "-----BEGIN-----\nabc\n-----END-----\n"


def test_from_file_preserves_crlf(tmp_path):
    vf = tmp_path / "win.pem"
    vf.write_bytes(b"line1\r\nline2\r\n")
    assert cli._read_secret_value(_ns(from_file=str(vf)), help_command="x") == "line1\r\nline2\r\n"


def test_stdin_strips_only_one_trailing_newline(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("tok\n"))
    assert cli._read_secret_value(_ns(stdin=True), help_command="x") == "tok"
    monkeypatch.setattr("sys.stdin", io.StringIO("tok\n\n"))
    assert cli._read_secret_value(_ns(stdin=True), help_command="x") == "tok\n"


def test_key_export_calls_avault_and_audits(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    blob = {"scheme": "avault-backup-v1", "ciphertext": "wrapped"}
    export = Mock(return_value=blob)
    monkeypatch.setattr(api, "avault_key_export", export)
    monkeypatch.setattr("sys.stdin", io.StringIO("my-passphrase\n"))
    out = tmp_path / "vault-key.json"

    assert cli.cmd_vault_key_export(_ns(out=str(out))) == 0
    capfd.readouterr()
    export.assert_called_once_with("my-passphrase")
    assert json.loads(out.read_text()) == blob
    with cli._open_vault_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    assert "key_exported" in {r["event"] for r in rows}
    assert all("my-passphrase" not in json.dumps(r) for r in rows)


def test_key_import_calls_avault_and_audits(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    blob = {"scheme": "avault-backup-v1", "ciphertext": "wrapped"}
    path = tmp_path / "vault-key.json"
    path.write_text(json.dumps(blob))
    import_ = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_key_import", import_)
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))

    assert cli.cmd_vault_key_import(_ns(file=str(path), force=True)) == 0
    assert json.loads(capfd.readouterr().out)["imported"] is True
    import_.assert_called_once_with(blob, "pw", force=True)
    with cli._open_vault_engine().connect() as conn:
        rows = [dict(r) for r in conn.execute(vault_audit.select()).mappings()]
    assert "key_imported" in {r["event"] for r in rows}
