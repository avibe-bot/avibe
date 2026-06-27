"""CLI tests for ``vibe vault`` orchestration around the avault client.

These tests mock avault: Avibe stores and routes envelopes, but never decrypts values.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from storage import vault_service
from storage.models import vault_audit, vault_grants
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


def _set_protected_grant(name: str, *, session_id: str | None = None) -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name=name, protection="protected", sealed=_sealed("protected"))
        req = vault_service.create_access_request(
            conn,
            name,
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": "run"} if session_id else {"mode": "run"},
        )
        return vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref=name,
            session_id=session_id,
            created_by_request_id=req["id"],
        )


def _set_group_grant(names: list[str], *, group: str = "crypto", session_id: str | None = None) -> dict:
    with cli._open_vault_engine().begin() as conn:
        for name in names:
            vault_service.create_secret(conn, name=name, protection="protected", group=group, sealed=_sealed(name.lower()))
        req = vault_service.create_access_request(
            conn,
            names[0],
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": "run"} if session_id else {"mode": "run"},
        )
        return vault_service.create_grant(
            conn,
            scope_type="group",
            scope_ref=group,
            session_id=session_id,
            created_by_request_id=req["id"],
        )


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


def test_set_seals_with_avault_without_value_derived_metadata(tmp_path, capfd, monkeypatch):
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
    assert "preview" not in secret
    assert "sk-ant-abcd1234" not in json.dumps(payload)
    assert "1234" not in json.dumps(payload)
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


def test_run_delivers_protected_secret_under_agent_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "caller-pythonpath")
    monkeypatch.setenv("AWS_PROFILE", "caller-profile")
    monkeypatch.setenv("LOCAL_NAME", "stale-caller-secret")
    monkeypatch.setattr(cli.sys, "stdin", type("FakeStdin", (), {"buffer": io.BytesIO(b"caller stdin\n")})())
    grant = _set_protected_grant("PROTECTED_KEY")

    def _deliver(**kwargs):
        command = kwargs["command"]
        stdout_path = command[4]
        stderr_path = command[5]
        stdin_path = command[6]
        env_path = command[7]
        env_script = Path(env_path).read_text()
        assert "export PYTHONPATH=caller-pythonpath\n" in env_script
        assert "export AWS_PROFILE=caller-profile\n" in env_script
        assert "export LOCAL_NAME=" not in env_script
        assert Path(stdin_path).read_bytes() == b"caller stdin\n"
        with open(stdout_path, "wb", buffering=0) as stdout:
            stdout.write(b"protected out\n")
        with open(stderr_path, "wb", buffering=0) as stderr:
            stderr.write(b"protected err\n")
        return {"exit_code": 0}

    deliver = Mock(side_effect=_deliver)
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["LOCAL_NAME=PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 0
    assert "protected out\n" in captured.out
    assert "protected err\n" in captured.err
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["scope_type"] == grant["scope_type"]
    assert deliver.call_args.kwargs["scope_ref"] == grant["scope_ref"]
    assert deliver.call_args.kwargs["secrets"] == [{"name": "PROTECTED_KEY", "env": "LOCAL_NAME", "envelope": _sealed("protected")}]
    command = deliver.call_args.kwargs["command"]
    assert command[:4] == [
        shutil.which("sh") or "/bin/sh",
        "-c",
        (
            'stdout_fifo=$1; stderr_fifo=$2; stdin_fifo=$3; env_file=$4; cwd=$5; shift 5; '
            'exec <"$stdin_fifo" >"$stdout_fifo" 2>"$stderr_fifo"; '
            '. "$env_file"; cd "$cwd" || exit 125; exec "$@"'
        ),
        "avibe-vault-run",
    ]
    assert command[8:] == [
        str(tmp_path),
        shutil.which("python3") or "python3",
        "-c",
        "pass",
    ]
    assert "value" not in repr(deliver.call_args.kwargs)
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.get_secret_meta(conn, "PROTECTED_KEY")["use_count"] == 1


def test_run_reports_fifo_bridge_errors(capfd, monkeypatch):
    from vibe import api

    _set_protected_grant("PROTECTED_KEY")
    monkeypatch.setattr(
        cli,
        "_AgentRunOutputBridge",
        Mock(side_effect=cli.TaskCliError("protected run FIFOs are unsupported", code="unsupported_platform")),
    )
    deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "unsupported_platform"
    deliver.assert_not_called()


def test_run_rejects_protected_tty_without_delivering(capfd, monkeypatch):
    from vibe import api

    _set_protected_grant("PROTECTED_KEY")
    monkeypatch.setattr(cli, "_stdio_has_tty", lambda: True)
    deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "protected_tty_unsupported"
    deliver.assert_not_called()


def test_run_prefers_common_grant_for_protected_batch(tmp_path, capfd, monkeypatch):
    from vibe import api

    monkeypatch.chdir(tmp_path)
    group_grant = _set_group_grant(["A_KEY", "B_KEY"])
    with cli._open_vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"source": "cli"},
            delivery={"mode": "run"},
        )
        vault_service.create_grant(conn, scope_type="secret", scope_ref="A_KEY", created_by_request_id=req["id"])
    deliver = Mock(return_value={"exit_code": 0, "stdout": b"", "stderr": b""})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["A_KEY", "B_KEY"], command_argv=["python3", "-c", "pass"]))

    assert code == 0
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["scope_type"] == group_grant["scope_type"]
    assert deliver.call_args.kwargs["scope_ref"] == group_grant["scope_ref"]
    assert [secret["name"] for secret in deliver.call_args.kwargs["secrets"]] == ["A_KEY", "B_KEY"]


def test_run_rejects_mixed_tiers_before_creating_approval(tmp_path, capfd, monkeypatch):
    _set_secret("STANDARD_KEY", "standard", tmp_path, monkeypatch, capfd)
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    code = cli.cmd_vault_run(_ns(env=["STANDARD_KEY", "PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "mixed_protection_tiers"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_requests(conn, status="pending") == []


def test_run_rejects_missing_later_secret_before_creating_approval(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY", "MISSING_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "secret_not_found"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_requests(conn, status="pending") == []


def test_run_expires_grant_when_agent_cache_is_missing(capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_grant("PROTECTED_KEY")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(side_effect=api.AvaultError("grant is missing or expired")))

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert status == "expired"
    assert requests[0]["secret_name"] == "PROTECTED_KEY"


def test_run_persists_protected_approval_request_without_grant(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["delivery"]["mode"] == "run"


def test_run_skips_protected_delivery_audit_when_agent_returns_pre_spawn_70(capfd, monkeypatch):
    from vibe import api

    _set_protected_grant("PROTECTED_KEY")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(return_value={"exit_code": 70}))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    assert cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"])) == 70
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 0


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
