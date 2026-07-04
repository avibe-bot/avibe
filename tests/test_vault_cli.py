"""CLI tests for ``vibe vault`` orchestration around the avault client.

These tests mock avault: Avibe stores and routes envelopes, but never decrypts values.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import threading
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
        tag=None,
        skill=None,
        description=None,
        allow_host=None,
        auth_header=None,
        auth_query=None,
        env=None,
        command_argv=None,
        reason=None,
        spec=None,
        spec_json=None,
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


PROTECTED_SIGNING_PUBLIC_META = {
    "signing_public_key": {
        "curve": "secp256k1",
        "public_key": "02" + "cd" * 32,
    }
}


def _create_standard_secret(name: str, *, sealed: Sealed | None = None, **kwargs):
    with cli._open_vault_engine().begin() as conn:
        return vault_service.create_secret(conn, name=name, sealed=sealed or _sealed(), **kwargs)


def _grant_from_request(conn, request: dict, *, session_id: str | None = None) -> dict:
    option = request["card"]["grant_options"][0]
    return vault_service.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        session_id=session_id,
        request_id=request["id"],
    )


def _set_protected_grant(name: str, *, session_id: str | None = None, purpose: str = "run") -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name=name, protection="protected", sealed=_sealed("protected"))
        req = vault_service.create_access_request(
            conn,
            name,
            purpose=purpose,
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": purpose} if session_id else {"mode": purpose},
        )
        return _grant_from_request(conn, req, session_id=session_id)


def _set_protected_always_ask_grant(name: str, *, session_id: str | None = None, purpose: str = "run") -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            protection="protected",
            sealed=_sealed("protected"),
            policy={"always_ask": True},
        )
        req = vault_service.create_access_request(
            conn,
            name,
            purpose=purpose,
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": purpose} if session_id else {"mode": purpose},
        )
        return _grant_from_request(conn, req, session_id=session_id)


def _set_tag_grant(names: list[str], *, tag: str = "crypto", session_id: str | None = None) -> dict:
    with cli._open_vault_engine().begin() as conn:
        for name in names:
            vault_service.create_secret(conn, name=name, protection="protected", tags=[tag], sealed=_sealed(name.lower()))
        req = vault_service.create_access_request(
            conn,
            source_selector={"tags": [tag]},
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": "run"} if session_id else {"mode": "run"},
        )
        return _grant_from_request(conn, req, session_id=session_id)


def _set_always_ask_standard_grant(name: str, *, session_id: str | None = None, purpose: str = "run") -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            protection="standard",
            sealed=_sealed(name.lower()),
            policy={"always_ask": True},
        )
        req = vault_service.create_access_request(
            conn,
            name,
            purpose=purpose,
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": purpose} if session_id else {"mode": purpose},
        )
        return _grant_from_request(conn, req, session_id=session_id)


def _set_always_ask_standard_tag_grant(
    names: list[str],
    *,
    tag: str = "batch",
    session_id: str | None = None,
    purpose: str = "run",
) -> dict:
    with cli._open_vault_engine().begin() as conn:
        for name in names:
            vault_service.create_secret(
                conn,
                name=name,
                protection="standard",
                sealed=_sealed(name.lower()),
                tags=[tag],
                policy={"always_ask": True},
            )
        req = vault_service.create_access_request(
            conn,
            source_selector={"tags": [tag]},
            purpose=purpose,
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": purpose} if session_id else {"mode": purpose},
        )
        return _grant_from_request(conn, req, session_id=session_id)


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


def test_parse_env_specs_rejects_duplicate_secret_alias():
    with pytest.raises(cli.TaskCliError) as exc:
        cli._parse_env_specs(["A=ASK_KEY", "B=ASK_KEY"])

    assert exc.value.code == "conflicting_env_alias"


def test_vault_parser_does_not_expose_plaintext_set_command():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["vault", "set", "OPENAI_API_KEY", "--stdin"])


def test_vault_access_command_flag_does_not_shadow_root_command():
    args = cli.build_parser().parse_args(
        ["vault", "access", "OPENAI_API_KEY", "--command", "deploy sync"],
    )

    assert args.command == "vault"
    assert args.vault_command == "access"
    assert args.operation_command == "deploy sync"
    assert cli._vault_cli_delivery(args)["command"] == "deploy sync"


def test_vault_sign_command_flag_does_not_shadow_root_command():
    args = cli.build_parser().parse_args(
        [
            "vault",
            "sign",
            "ETH_KEY",
            "--digest",
            "00" * 32,
            "--command",
            "wallet sign",
        ],
    )

    assert args.command == "vault"
    assert args.vault_command == "sign"
    assert args.operation_command == "wallet sign"
    assert cli._vault_cli_delivery(args)["command"] == "wallet sign"


def test_discover_reports_value_free_capabilities(tmp_path, capfd, monkeypatch):
    _create_standard_secret("STANDARD_KEY")
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("key"),
        )
        vault_service.create_secret(
            conn,
            name="PROTECTED_STATIC_KEY",
            protection="protected",
            sealed=_sealed("protected-static"),
        )
        vault_service.create_secret(
            conn,
            name="PROTECTED_ETH_KEY",
            protection="protected",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("protected-key"),
            public_meta=PROTECTED_SIGNING_PUBLIC_META,
        )
        vault_service.create_secret(
            conn,
            name="REMOTE_ETH_KEY",
            kind="keypair",
            signer_kind="remote",
            sealed=_sealed("remote-key"),
        )

    assert cli.cmd_vault_discover(_ns()) == 0
    payload = json.loads(capfd.readouterr().out)

    assert payload["kind"] == "vault_discovery"
    secrets = {item["name"]: item for item in payload["secrets"]}
    assert secrets["STANDARD_KEY"] == {
        "name": "STANDARD_KEY",
        "kind": "static",
        "protection": "standard",
        "access_grantable": True,
        "per_use_sign": False,
    }
    assert secrets["ETH_KEY"]["per_use_sign"] is True
    assert secrets["PROTECTED_STATIC_KEY"]["access_grantable"] is True
    assert secrets["PROTECTED_ETH_KEY"]["per_use_sign"] is True
    assert secrets["REMOTE_ETH_KEY"]["per_use_sign"] is False
    assert "sk-" not in json.dumps(payload)


def test_access_request_cli_uses_caller_session(tmp_path, capfd, monkeypatch):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_cli")

    assert cli.cmd_vault_access(_ns(name="PROTECTED_KEY", command="python sync.py", skill="deploy")) == 0
    payload = json.loads(capfd.readouterr().out)

    assert payload["kind"] == "vault_access_request"
    assert payload["request"]["requester"]["session_id"] == "ses_cli"
    assert payload["request"]["requester"]["skill"] == "deploy"
    assert payload["request"]["delivery"]["command"] == "python sync.py"
    assert payload["request"]["delivery"]["skill"] == "deploy"
    assert payload["request"]["card"]["skill"] == "deploy"
    assert payload["request"]["status"] == "pending"


def test_access_request_cli_accepts_protected_static_request(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    assert cli.cmd_vault_access(_ns(name="PROTECTED_KEY")) == 0
    payload = json.loads(capfd.readouterr().out)

    assert payload["kind"] == "vault_access_request"
    assert payload["request"]["secret_name"] == "PROTECTED_KEY"
    assert payload["request"]["card"]["protection"] == "protected"


def test_sign_request_and_await_cli_reads_approved_signature(capfd, monkeypatch):
    from vibe import api

    monkeypatch.setattr(api, "avault_sign", Mock(return_value={"signature": "ab" * 64, "recovery_id": 1}))
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("key"),
        )

    assert cli.cmd_vault_sign(_ns(name="ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")) == 0
    request_id = json.loads(capfd.readouterr().out)["request_id"]
    api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": request_id,
        }
    )

    assert cli.cmd_vault_await(_ns(request_id=request_id)) == 0
    payload = json.loads(capfd.readouterr().out)

    assert payload["kind"] == "vault_request_result"
    assert payload["status"] == "approved"
    assert payload["result"] == {"type": "signature", "signature": {"signature": "ab" * 64, "recovery_id": 1}}


def test_sign_request_cli_accepts_protected_keypair(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="PROTECTED_ETH_KEY",
            protection="protected",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("protected-key"),
            public_meta=PROTECTED_SIGNING_PUBLIC_META,
        )

    assert cli.cmd_vault_sign(_ns(name="PROTECTED_ETH_KEY", digest="00" * 32, scheme="ecdsa-secp256k1-recoverable")) == 0
    payload = json.loads(capfd.readouterr().out)

    assert payload["kind"] == "vault_sign_request"
    assert payload["request"]["secret_name"] == "PROTECTED_ETH_KEY"
    assert payload["request"]["card"]["protection"] == "protected"
    assert payload["request"]["card"]["grant_options"] == []


def test_vault_await_wait_treats_failed_request_as_terminal(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("key"),
        )
        req = vault_service.create_sign_request(
            conn,
            "ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
        )
        vault_service.claim_sign_request(
            conn,
            req["id"],
            name="ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
        )
        vault_service.fail_sign_request(conn, req["id"], reason="avault_failed")

    assert cli.cmd_vault_await(_ns(request_id=req["id"], wait=5)) == 1
    payload = json.loads(capfd.readouterr().err)

    assert payload["code"] == "request_failed"
    assert payload["details"] == {"request_id": req["id"]}


def test_vault_await_wait_keeps_waiting_from_signing_status(capfd, monkeypatch):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            kind="keypair",
            signer_kind="local",
            sealed=_sealed("key"),
        )
        req = vault_service.create_sign_request(
            conn,
            "ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
        )
        vault_service.claim_sign_request(
            conn,
            req["id"],
            name="ETH_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
        )

    wait_mock = Mock(
        return_value={
            "request": {"id": req["id"], "status": "approved"},
            "result": {"type": "signature", "signature": {"signature": "ab" * 64, "recovery_id": 1}},
        }
    )
    monkeypatch.setattr(cli, "_wait_for_vault_request", wait_mock)

    assert cli.cmd_vault_await(_ns(request_id=req["id"], wait=5)) == 0
    payload = json.loads(capfd.readouterr().out)

    wait_mock.assert_called_once_with(req["id"], timeout=5.0)
    assert payload["status"] == "approved"
    assert payload["result"] == {"type": "signature", "signature": {"signature": "ab" * 64, "recovery_id": 1}}


def test_run_calls_avault_with_env_mapping_and_records_delivery(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("SRC_KEY", sealed=_sealed("run"))
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


def test_run_expands_tags_and_skill_tags_as_union(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("A_KEY", sealed=_sealed("a"), tags=["deploy"])
    _create_standard_secret("B_KEY", sealed=_sealed("b"), tags=["skill:deploy"])
    _create_standard_secret("C_KEY", sealed=_sealed("c"), tags=["deploy", "skill:deploy"])
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(tag=["deploy"], skill=["deploy"], command_argv=["python3", "-c", "pass"]))

    assert code == 0
    delivered = {item["name"]: item["env"] for item in deliver.call_args.args[0]}
    assert delivered == {"A_KEY": "A_KEY", "B_KEY": "B_KEY", "C_KEY": "C_KEY"}


def test_run_skill_selects_skill_link_mirror_tag(monkeypatch):
    from vibe import api

    _create_standard_secret("LINKED_KEY", sealed=_sealed("linked"))
    with cli._open_vault_engine().begin() as conn:
        vault_service.link_secret_to_skills(conn, "LINKED_KEY", ["deploy"])
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(skill=["deploy"], command_argv=["python3", "-c", "pass"]))

    assert code == 0
    assert deliver.call_args.args[0] == [{"name": "LINKED_KEY", "env": "LINKED_KEY", "envelope": _sealed("linked")}]


@pytest.mark.parametrize("selector", [{"tag": ["missing"]}, {"skill": ["missing"]}])
def test_run_reports_empty_tag_or_skill_selector(capfd, selector):
    code = cli.cmd_vault_run(_ns(**selector, command_argv=["python3", "-c", "pass"]))
    payload = json.loads(capfd.readouterr().err)

    assert code == 1
    assert payload["code"] == "no_matching_secrets"
    assert payload["help_command"] == "vibe vault run --help"


def test_run_rejects_conflicting_alias_from_tag_selection(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("DEPLOY_KEY", tags=["deploy"])
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["LOCAL=DEPLOY_KEY"], tag=["deploy"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "conflicting_env_alias"
    deliver.assert_not_called()


def test_run_rejects_keypair_before_avault_delivery(capfd, monkeypatch):
    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ETH_KEY", sealed=_sealed("eth"), kind="keypair", signer_kind="local")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ETH_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "keypair_not_value_deliverable"
    deliver.assert_not_called()


def test_run_rejects_tag_selected_keypair_with_sign_hint(capfd, monkeypatch):
    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            sealed=_sealed("eth"),
            kind="keypair",
            signer_kind="local",
            tags=["deploy"],
        )
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()
    payload = json.loads(captured.err)

    assert code == 1
    assert payload["code"] == "keypair_not_value_deliverable"
    assert "vibe vault sign" in payload["hint"]
    deliver.assert_not_called()


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
        keep_env_path = command[8]
        env_script = Path(env_path).read_text()
        assert "export PYTHONPATH=caller-pythonpath\n" in env_script
        assert "export AWS_PROFILE=caller-profile\n" in env_script
        assert "export LOCAL_NAME=" not in env_script
        assert Path(keep_env_path).read_text() == "LOCAL_NAME\n"
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
    assert deliver.call_args.kwargs["grant_id"] == grant["id"]
    assert deliver.call_args.kwargs["context"] == {"session_id": grant.get("session_id"), "purpose": "run"}
    assert deliver.call_args.kwargs["secrets"] == [
        {"name": "PROTECTED_KEY", "env": "LOCAL_NAME", "envelope": _sealed("protected"), "tier": "protected"}
    ]
    command = deliver.call_args.kwargs["command"]
    shell = shutil.which("sh") or "/bin/sh"
    assert command[:2] == [shell, "-c"]
    assert command[3] == "avibe-vault-run"
    assert "env -i" not in command[2]
    assert 'grep' in command[2]
    assert 'unset "$name"' in command[2]
    assert '. "$env_file"; cd "$cwd" || exit 125; exec "$@"' in command[2]
    assert command[9:] == [
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


def test_run_allows_tty_stdio_for_protected_delivery(tmp_path, monkeypatch):
    from vibe import api

    monkeypatch.chdir(tmp_path)

    class TtyBytes(io.BytesIO):
        def isatty(self):
            return True

    fake_stdin = type("FakeStdin", (), {"buffer": TtyBytes(), "isatty": lambda self: True})()
    fake_stdout = type("FakeStdout", (), {"buffer": TtyBytes(), "isatty": lambda self: True})()
    fake_stderr = type("FakeStderr", (), {"buffer": TtyBytes(), "isatty": lambda self: True})()
    monkeypatch.setattr(cli.sys, "stdin", fake_stdin)
    monkeypatch.setattr(cli.sys, "stdout", fake_stdout)
    monkeypatch.setattr(cli.sys, "stderr", fake_stderr)
    _set_protected_grant("PROTECTED_KEY")
    deliver = Mock(return_value={"exit_code": 0})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_agent_release", Mock(return_value={"released": True}))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    assert cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"])) == 0
    deliver.assert_called_once()


def test_agent_run_trampoline_clears_agent_environment(tmp_path):
    env_file = tmp_path / "env.sh"
    env_file.write_text("export KEEP=value\n")
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdin = tmp_path / "stdin"
    keep_env = tmp_path / "keep-env"
    keep_env.write_text("SECRET\n")
    command = cli._agent_run_command(
        ["python3", "-c", "pass"],
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        stdin_path=str(stdin),
        env_path=str(env_file),
        keep_env_path=str(keep_env),
    )

    assert "exec " in command[2]
    assert "env -i " not in command[2]
    assert 'grep' in command[2]
    assert 'unset "$name"' in command[2]
    assert '. "$env_file"; cd "$cwd" || exit 125; exec "$@"' in command[2]


def test_agent_run_stdin_polling_uses_stdlib_select(monkeypatch):
    source = type("FdSource", (), {"fileno": lambda self: 10})()
    called = Mock(return_value=([10], [], []))
    monkeypatch.setattr(cli.select_module, "select", called)
    read = Mock(return_value=b"stdin chunk")
    monkeypatch.setattr(cli.os, "read", read)

    chunk = cli._AgentRunOutputBridge._read_stdin_chunk(source, threading.Event())

    assert chunk == b"stdin chunk"
    called.assert_called_once()
    read.assert_called_once_with(10, 8192)


def test_shell_env_exports_can_use_explicit_env():
    exports = cli._shell_env_exports({"KEEP": "ok", "BAD-NAME": "no", "SECRET": "old"}, exclude={"SECRET"})

    assert "export KEEP=ok\n" in exports
    assert "BAD-NAME" not in exports
    assert "SECRET" not in exports


def test_run_prefers_common_grant_for_protected_batch(tmp_path, capfd, monkeypatch):
    from vibe import api

    monkeypatch.chdir(tmp_path)
    tag_grant = _set_tag_grant(["A_KEY", "B_KEY"])
    with cli._open_vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"source": "cli"},
            delivery={"mode": "run"},
        )
        _grant_from_request(conn, req)
    deliver = Mock(return_value={"exit_code": 0, "stdout": b"", "stderr": b""})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["A_KEY", "B_KEY"], command_argv=["python3", "-c", "pass"]))

    assert code == 0
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["grant_id"] == tag_grant["id"]
    assert [secret["name"] for secret in deliver.call_args.kwargs["secrets"]] == ["A_KEY", "B_KEY"]


def test_run_uses_session_bound_protected_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    monkeypatch.chdir(tmp_path)
    _set_protected_grant("PROTECTED_KEY", session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 0, "stdout": b"", "stderr": b""})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["secrets"][0]["name"] == "PROTECTED_KEY"
    assert deliver.call_args.kwargs["grant_id"]


def test_fetch_uses_session_bound_protected_grant(capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli", purpose="fetch")
    with cli._open_vault_engine().begin() as conn:
        row = conn.execute(vault_service.vault_secrets.select().where(vault_service.vault_secrets.c.name == "PROTECTED_KEY")).mappings().one()
        conn.execute(
            vault_service.vault_secrets.update()
            .where(vault_service.vault_secrets.c.name == "PROTECTED_KEY")
            .values(policy=json.dumps({"allowed_hosts": ["example.com"], "auth": {"type": "bearer"}}))
        )
    assert row["name"] == "PROTECTED_KEY"
    deliver = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_agent_deliver_fetch", deliver)

    code = cli.cmd_vault_fetch(_ns(auth="PROTECTED_KEY", url="https://example.com/api", method="GET", output=None, session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 0
    assert captured.out == "ok"
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["grant_id"] == grant["id"]
    assert deliver.call_args.kwargs["sealed"] == _sealed("protected")


def test_inject_resolver_uses_session_bound_protected_grant(tmp_path):
    grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli", purpose="inject")
    engine = cli._open_vault_engine()

    resolved_grant, one_shot_grants, secrets = cli._resolve_vault_inject_delivery(
        engine,
        ["PROTECTED_KEY"],
        path=str(tmp_path / "out.env"),
        fmt="dotenv",
        args=_ns(session_id="ses_cli"),
    )

    assert resolved_grant["id"] == grant["id"]
    assert one_shot_grants == []
    assert secrets == [{"name": "PROTECTED_KEY", "key": "PROTECTED_KEY", "envelope": _sealed("protected")}]


def test_run_fast_path_reserves_protected_always_ask_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_always_ask_grant("PROTECTED_ASK", session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 0})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_ASK"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_fast_path_does_not_reuse_reserved_protected_always_ask_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    reserved_grant = _set_protected_always_ask_grant("PROTECTED_ASK", session_id="ses_cli")
    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        reserved = vault_service.find_active_grant_for_secret(
            conn,
            "PROTECTED_ASK",
            session_id="ses_cli",
            reserve_one_shot=True,
        )
        req = vault_service.create_access_request(
            conn,
            "PROTECTED_ASK",
            requester={"source": "cli", "session_id": "ses_cli"},
            delivery={"session_id": "ses_cli", "mode": "run"},
        )
        active_grant = _grant_from_request(conn, req, session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 0})
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_agent_release", Mock(return_value={"released": True}))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_ASK"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))

    assert reserved["id"] == reserved_grant["id"]
    assert code == 0
    deliver.assert_called_once()
    with engine.connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(vault_grants.select().where(vault_grants.c.id.in_([reserved_grant["id"], active_grant["id"]]))).mappings()
        }
    assert statuses == {reserved_grant["id"]: "reserved", active_grant["id"]: "expired"}


def test_inject_fast_path_reserves_protected_always_ask_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_always_ask_grant("PROTECTED_ASK", session_id="ses_cli", purpose="inject")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_inject", deliver)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock())
    out = tmp_path / "out.env"

    code = cli.cmd_vault_inject(_ns(keys="PROTECTED_ASK", out=str(out), format="dotenv", session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_mixed_grants_releases_reserved_always_ask_grants_before_delivery(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant_a = _set_protected_always_ask_grant("A_ASK", session_id="ses_cli")
    grant_b = _set_protected_always_ask_grant("B_ASK", session_id="ses_cli")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["A_ASK", "B_ASK"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "always_ask_selector_set"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                vault_grants.select().where(vault_grants.c.id.in_([grant_a["id"], grant_b["id"]]))
            ).mappings()
        }
    assert statuses == {grant_a["id"]: "active", grant_b["id"]: "active"}


def test_inject_mixed_grants_releases_reserved_always_ask_grants_before_delivery(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant_a = _set_protected_always_ask_grant("A_ASK", session_id="ses_cli", purpose="inject")
    grant_b = _set_protected_always_ask_grant("B_ASK", session_id="ses_cli", purpose="inject")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_inject", deliver)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock())

    code = cli.cmd_vault_inject(_ns(keys="A_ASK,B_ASK", out=str(tmp_path / "out.env"), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "mixed_grants"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                vault_grants.select().where(vault_grants.c.id.in_([grant_a["id"], grant_b["id"]]))
            ).mappings()
        }
    assert statuses == {grant_a["id"]: "active", grant_b["id"]: "active"}


def test_run_rejects_duplicate_env_alias_for_always_ask_secret(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(
        _ns(
            env=["A=ASK_KEY", "B=ASK_KEY"],
            command_argv=["python3", "-c", "pass"],
            session_id="ses_cli",
        )
    )
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "conflicting_env_alias"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_run_reuses_multi_secret_standard_one_shot_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_tag_grant(["ASK_A", "ASK_B"], session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 0, "delivered": True})
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(
        _ns(
            env=["A=ASK_A", "B=ASK_B"],
            command_argv=["python3", "-c", "pass"],
            session_id="ses_cli",
        )
    )

    assert code == 0
    deliver.assert_called_once_with(
        [
            {"name": "ASK_A", "env": "A", "envelope": _sealed("ask_a")},
            {"name": "ASK_B", "env": "B", "envelope": _sealed("ask_b")},
        ],
        ["python3", "-c", "pass"],
    )
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_tag_standard_always_ask_request_offers_batch_one_shot_grant(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret(
        "ASK_A",
        protection="standard",
        sealed=_sealed("ask_a"),
        tags=["deploy"],
        policy={"always_ask": True},
    )
    _create_standard_secret(
        "ASK_B",
        protection="standard",
        sealed=_sealed("ask_b"),
        tags=["deploy"],
        policy={"always_ask": True},
    )
    _create_standard_secret("NORMAL_DEPLOY", protection="standard", sealed=_sealed("normal"), tags=["deploy"])
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] is None
    assert requests[0]["delivery"]["source_selector"] == {"tags": ["deploy"]}
    assert requests[0]["card"]["one_shot"] is True
    option = requests[0]["card"]["grant_options"][0]
    assert option["source_selector"] == {"tags": ["deploy"]}
    assert option["one_shot"] is True
    assert option["member_snapshot"] == ["ASK_A", "ASK_B"]
    created = api.create_vault_grant({"request_id": requests[0]["id"], "session_id": "ses_cli"})
    assert created["grant"]["member_snapshot"] == ["ASK_A", "ASK_B"]
    assert created["grant"]["one_shot"] is True
    assert created["grant"]["delivery_status"] == "standard_ready"


def test_run_tag_reuses_standard_one_shot_subset_with_normal_secrets(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_tag_grant(["ASK_A", "ASK_B"], tag="deploy", session_id="ses_cli")
    _create_standard_secret("NORMAL_DEPLOY", protection="standard", sealed=_sealed("normal"), tags=["deploy"])
    deliver = Mock(return_value={"exit_code": 0, "delivered": True})
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once_with(
        [
            {"name": "ASK_A", "env": "ASK_A", "envelope": _sealed("ask_a")},
            {"name": "ASK_B", "env": "ASK_B", "envelope": _sealed("ask_b")},
            {"name": "NORMAL_DEPLOY", "env": "NORMAL_DEPLOY", "envelope": _sealed("normal")},
        ],
        ["python3", "-c", "pass"],
    )
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_inject_reuses_multi_secret_standard_one_shot_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_tag_grant(["ASK_A", "ASK_B"], session_id="ses_cli", purpose="inject")
    deliver = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)
    out = tmp_path / "out.env"

    code = cli.cmd_vault_inject(_ns(keys="ASK_A,ASK_B", out=str(out), format="dotenv", session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once_with(
        str(out),
        "dotenv",
        [
            {"name": "ASK_A", "key": "ASK_A", "envelope": _sealed("ask_a")},
            {"name": "ASK_B", "key": "ASK_B", "envelope": _sealed("ask_b")},
        ],
    )
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_does_not_consume_always_ask_grant_when_later_secret_needs_approval(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="OTHER_ASK", protection="standard", sealed=_sealed("other"), policy={"always_ask": True})
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(
        _ns(
            env=["ASK_KEY", "OTHER_ASK"],
            command_argv=["python3", "-c", "pass"],
            session_id="ses_cli",
        )
    )
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_run_consumes_standard_always_ask_grant_after_delivery(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    deliver = Mock(return_value=0)
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ASK_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))

    assert code == 0
    deliver.assert_called_once_with(
        [{"name": "ASK_KEY", "env": "ASK_KEY", "envelope": _sealed("ask_key")}],
        ["python3", "-c", "pass"],
    )
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_consumes_standard_always_ask_grant_when_child_exits_70(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 70, "delivered": True})
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ASK_KEY"], command_argv=["python3", "-c", "raise SystemExit(70)"], session_id="ses_cli"))

    assert code == 70
    deliver.assert_called_once_with(
        [{"name": "ASK_KEY", "env": "ASK_KEY", "envelope": _sealed("ask_key")}],
        ["python3", "-c", "raise SystemExit(70)"],
    )
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_consumes_standard_always_ask_grant_when_avault_errors_after_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    deliver = Mock(side_effect=api.AvaultError("timed out after possible handoff"))
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ASK_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_run_releases_standard_always_ask_grant_when_avault_fails_before_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    deliver = Mock(side_effect=api.AvaultPreHandoffError("avault is required for vault run"))
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ASK_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_run_releases_one_shot_grant_when_later_resolution_aborts_before_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ETH_KEY", sealed=_sealed("eth"), kind="keypair", signer_kind="local")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_run", deliver)

    code = cli.cmd_vault_run(_ns(env=["ASK_KEY", "ETH_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "keypair_not_value_deliverable"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_inject_consumes_standard_always_ask_grant_when_avault_errors_after_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    deliver = Mock(side_effect=api.AvaultError("write failed after possible handoff"))
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)

    code = cli.cmd_vault_inject(_ns(keys="ASK_KEY", out=str(tmp_path / "out.env"), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_inject_releases_standard_always_ask_grant_when_avault_fails_before_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    deliver = Mock(side_effect=api.AvaultPreHandoffError("avault is required for vault inject"))
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)

    code = cli.cmd_vault_inject(_ns(keys="ASK_KEY", out=str(tmp_path / "out.env"), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    deliver.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_inject_uses_resolved_output_path_for_standard_always_ask_delivery(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    deliver = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = str((tmp_path / "out.env").resolve(strict=False))

    code = cli.cmd_vault_inject(_ns(keys="ASK_KEY", out="~/out.env", format="dotenv", session_id="ses_cli"))
    payload = json.loads(capfd.readouterr().out)

    assert code == 0
    assert payload["path"] == expected
    deliver.assert_called_once_with(expected, "dotenv", [{"name": "ASK_KEY", "key": "ASK_KEY", "envelope": _sealed("ask_key")}])
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "expired"


def test_inject_rejects_unwritable_output_before_reserving_one_shot_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)

    code = cli.cmd_vault_inject(
        _ns(keys="ASK_KEY", out=str(tmp_path / "missing" / "out.env"), format="dotenv", session_id="ses_cli")
    )
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "output_unwritable"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_inject_rejects_existing_directory_output_before_reserving_one_shot_grant(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)
    out = tmp_path / "out.env"
    out.mkdir()

    code = cli.cmd_vault_inject(_ns(keys="ASK_KEY", out=str(out), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "output_unwritable"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_inject_releases_one_shot_grant_when_later_resolution_aborts_before_handoff(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli", purpose="inject")
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ETH_KEY", sealed=_sealed("eth"), kind="keypair", signer_kind="local")
    deliver = Mock()
    monkeypatch.setattr(api, "avault_deliver_inject", deliver)

    code = cli.cmd_vault_inject(_ns(keys="ASK_KEY,ETH_KEY", out=str(tmp_path / "out.env"), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "keypair_not_value_deliverable"
    deliver.assert_not_called()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_expire_agent_grant_after_missing_does_not_reserve_replacement_grant(tmp_path, capfd, monkeypatch):
    engine = cli._open_vault_engine()
    first = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    with engine.begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"source": "cli", "session_id": "ses_cli"},
            delivery={"mode": "run", "session_id": "ses_cli"},
        )
        replacement = _grant_from_request(conn, req, session_id="ses_cli")

    cli._expire_agent_grant_after_missing(
        engine,
        first["id"],
        ["ASK_KEY"],
        requester={"source": "cli", "session_id": "ses_cli"},
        delivery={"mode": "run", "session_id": "ses_cli"},
    )

    with engine.connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(vault_grants.select().where(vault_grants.c.id.in_([first["id"], replacement["id"]]))).mappings()
        }
    assert statuses == {first["id"]: "expired", replacement["id"]: "active"}


def test_run_mixed_tiers_without_grant_creates_one_protected_approval(tmp_path, capfd, monkeypatch):
    _create_standard_secret("STANDARD_KEY")
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    code = cli.cmd_vault_run(_ns(env=["STANDARD_KEY", "PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["card"]["protected_secret_names"] == ["PROTECTED_KEY"]
    assert requests[0]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_KEY"]


def test_run_mixed_tiers_delivers_once_through_resident_agent(tmp_path, capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("STANDARD_KEY", sealed=_sealed("standard"))
    grant = _set_protected_grant("PROTECTED_KEY")
    deliver = Mock(return_value={"exit_code": 0})
    legacy_deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", legacy_deliver)

    code = cli.cmd_vault_run(_ns(env=["STANDARD_KEY", "PROTECTED_KEY"], command_argv=["python3", "-c", "pass"]))

    assert code == 0
    legacy_deliver.assert_not_called()
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["grant_id"] == grant["id"]
    assert deliver.call_args.kwargs["context"] == {"session_id": grant.get("session_id"), "purpose": "run"}
    assert deliver.call_args.kwargs["secrets"] == [
        {"name": "STANDARD_KEY", "env": "STANDARD_KEY", "envelope": _sealed("standard"), "tier": "standard"},
        {"name": "PROTECTED_KEY", "env": "PROTECTED_KEY", "envelope": _sealed("protected"), "tier": "protected"},
    ]


def test_run_mixed_tiers_reserves_standard_one_shot_batch(tmp_path, capfd, monkeypatch):
    from vibe import api

    standard_grant = _set_always_ask_standard_tag_grant(["ASK_A", "ASK_B"], session_id="ses_cli")
    protected_grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli")
    deliver = Mock(return_value={"exit_code": 0})
    legacy_deliver = Mock()
    monkeypatch.setattr(api, "avault_agent_deliver_run", deliver)
    monkeypatch.setattr(api, "avault_deliver_run", legacy_deliver)

    code = cli.cmd_vault_run(
        _ns(
            env=["A=ASK_A", "B=ASK_B", "PROTECTED_KEY"],
            command_argv=["python3", "-c", "pass"],
            session_id="ses_cli",
        )
    )

    assert code == 0
    legacy_deliver.assert_not_called()
    deliver.assert_called_once()
    assert deliver.call_args.kwargs["grant_id"] == protected_grant["id"]
    assert deliver.call_args.kwargs["secrets"] == [
        {"name": "ASK_A", "env": "A", "envelope": _sealed("ask_a"), "tier": "standard"},
        {"name": "ASK_B", "env": "B", "envelope": _sealed("ask_b"), "tier": "standard"},
        {"name": "PROTECTED_KEY", "env": "PROTECTED_KEY", "envelope": _sealed("protected"), "tier": "protected"},
    ]
    with cli._open_vault_engine().connect() as conn:
        standard_status = conn.execute(vault_grants.select().where(vault_grants.c.id == standard_grant["id"])).mappings().one()["status"]
        protected_status = conn.execute(vault_grants.select().where(vault_grants.c.id == protected_grant["id"])).mappings().one()["status"]
    assert standard_status == "expired"
    assert protected_status == "active"


def test_run_mixed_tiers_releases_standard_one_shot_when_protected_cache_missing(capfd, monkeypatch):
    from vibe import api

    standard_grant = _set_always_ask_standard_tag_grant(["ASK_A", "ASK_B"], session_id="ses_cli")
    protected_grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(side_effect=api.AvaultError("grant is missing or expired")))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(
        _ns(
            env=["A=ASK_A", "B=ASK_B", "PROTECTED_KEY"],
            command_argv=["python3", "-c", "pass"],
            session_id="ses_cli",
        )
    )
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        standard_status = conn.execute(vault_grants.select().where(vault_grants.c.id == standard_grant["id"])).mappings().one()["status"]
        protected_status = conn.execute(vault_grants.select().where(vault_grants.c.id == protected_grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert standard_status == "active"
    assert protected_status == "expired"
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_KEY"]


def test_run_tag_request_offers_fixed_protected_set_grant(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("A_DEPLOY", protection="protected", tags=["deploy"], sealed=_sealed("a"))
    _create_standard_secret("B_DEPLOY", protection="protected", tags=["deploy"], sealed=_sealed("b"))
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock())
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] is None
    assert requests[0]["delivery"]["source_selector"] == {"tags": ["deploy"]}
    assert requests[0]["card"]["protected_secret_names"] == ["A_DEPLOY", "B_DEPLOY"]
    option = requests[0]["card"]["grant_options"][0]
    assert option["source_selector"] == {"tags": ["deploy"]}
    assert option["member_snapshot"] == ["A_DEPLOY", "B_DEPLOY"]


def test_run_comma_env_request_offers_normalized_protected_set_grant(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("A_DEPLOY", protection="protected", sealed=_sealed("a"))
    _create_standard_secret("B_DEPLOY", protection="protected", sealed=_sealed("b"))
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock())
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(env=["A_DEPLOY,api_key=B_DEPLOY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["delivery"]["source_selector"] == {"env": ["A_DEPLOY", "api_key=B_DEPLOY"]}
    option = requests[0]["card"]["grant_options"][0]
    assert option["source_selector"] == {"env": ["A_DEPLOY", "api_key=B_DEPLOY"]}
    assert option["member_snapshot"] == ["A_DEPLOY", "B_DEPLOY"]


def test_run_tag_request_rejects_standard_always_ask_in_protected_set(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("PROTECTED_DEPLOY", protection="protected", tags=["deploy"], sealed=_sealed("protected"))
    _create_standard_secret(
        "ASK_DEPLOY",
        protection="standard",
        tags=["deploy"],
        sealed=_sealed("ask"),
        policy={"always_ask": True},
    )
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock())
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    payload = json.loads(captured.err)
    assert payload["code"] == "always_ask_selector_set"
    assert payload["details"]["protected_secret_names"] == ["PROTECTED_DEPLOY"]
    assert payload["details"]["standard_always_ask_secret_names"] == ["ASK_DEPLOY"]
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_requests(conn, status="pending") == []


def test_run_tag_protected_request_allows_preapproved_standard_always_ask(capfd, monkeypatch):
    from vibe import api

    _create_standard_secret("PROTECTED_DEPLOY", protection="protected", tags=["deploy"], sealed=_sealed("protected"))
    _create_standard_secret("NORMAL_DEPLOY", protection="standard", tags=["deploy"], sealed=_sealed("normal"))
    _create_standard_secret(
        "ASK_DEPLOY",
        protection="standard",
        tags=["deploy"],
        sealed=_sealed("ask"),
        policy={"always_ask": True},
    )
    with cli._open_vault_engine().begin() as conn:
        standard_req = vault_service.create_access_request(
            conn,
            "ASK_DEPLOY",
            requester={"source": "cli", "session_id": "ses_cli"},
            delivery={"session_id": "ses_cli", "mode": "run"},
        )
        standard_grant = _grant_from_request(conn, standard_req, session_id="ses_cli")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock())
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == standard_grant["id"])).mappings().one()["status"]
    assert status == "active"
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_DEPLOY"
    assert requests[0]["delivery"]["source_selector"] == {"tags": ["deploy"]}
    assert requests[0]["card"]["protected_secret_names"] == ["PROTECTED_DEPLOY"]
    assert requests[0]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_DEPLOY"]


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

    grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(side_effect=api.AvaultError("grant is missing or expired")))

    code = cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert status == "expired"
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["requester"]["session_id"] == "ses_cli"
    assert requests[0]["delivery"]["session_id"] == "ses_cli"
    assert requests[0]["card"]["grant_options"][0]["purpose"] == "run"


def test_run_reopens_one_selector_set_when_agent_cache_is_missing(capfd, monkeypatch):
    from vibe import api

    grant = _set_tag_grant(["A_KEY", "B_KEY"])
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(side_effect=api.AvaultError("grant is missing or expired")))

    code = cli.cmd_vault_run(_ns(env=["A_KEY", "B_KEY"], command_argv=["python3", "-c", "pass"]))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert status == "expired"
    assert len(requests) == 1
    assert requests[0]["secret_name"] is None
    assert requests[0]["delivery"]["protected_secret_names"] == ["A_KEY", "B_KEY"]
    assert requests[0]["delivery"]["source_selector"] == {"env": ["A_KEY", "B_KEY"]}
    option = requests[0]["card"]["grant_options"][0]
    assert option["source_selector"] == {"env": ["A_KEY", "B_KEY"]}
    assert option["member_snapshot"] == ["A_KEY", "B_KEY"]


def test_run_releases_standard_one_shot_when_protected_cache_is_missing(capfd, monkeypatch):
    from vibe import api

    standard_grant = _set_always_ask_standard_tag_grant(["ASK_DEPLOY"], tag="deploy", session_id="ses_cli")
    protected_grant = _set_tag_grant(["PROTECTED_DEPLOY"], tag="deploy", session_id="ses_cli")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(side_effect=api.AvaultError("grant is missing or expired")))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    code = cli.cmd_vault_run(_ns(tag=["deploy"], command_argv=["python3", "-c", "pass"], session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                vault_grants.select().where(vault_grants.c.id.in_([standard_grant["id"], protected_grant["id"]]))
            ).mappings()
        }
        requests = vault_service.list_requests(conn, status="pending")
    assert statuses == {standard_grant["id"]: "active", protected_grant["id"]: "expired"}
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_DEPLOY"
    assert requests[0]["delivery"]["source_selector"] == {"tags": ["deploy"]}
    assert requests[0]["delivery"]["protected_secret_names"] == ["PROTECTED_DEPLOY"]
    assert requests[0]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_DEPLOY"]


def test_fetch_reopens_session_bound_approval_when_agent_cache_is_missing(capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli", purpose="fetch")
    with cli._open_vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_secrets.update()
            .where(vault_service.vault_secrets.c.name == "PROTECTED_KEY")
            .values(policy=json.dumps({"allowed_hosts": ["example.com"], "auth": {"type": "bearer"}}))
        )
    monkeypatch.setattr(api, "avault_agent_deliver_fetch", Mock(side_effect=api.AvaultError("grant is missing or expired")))

    code = cli.cmd_vault_fetch(_ns(auth="PROTECTED_KEY", url="https://example.com/api", method="GET", output=None, session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert status == "expired"
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["requester"]["session_id"] == "ses_cli"
    assert requests[0]["delivery"]["session_id"] == "ses_cli"
    assert requests[0]["card"]["grant_options"][0]["purpose"] == "fetch"


def test_inject_reopens_session_bound_approval_when_agent_cache_is_missing(tmp_path, capfd, monkeypatch):
    from vibe import api

    grant = _set_protected_grant("PROTECTED_KEY", session_id="ses_cli", purpose="inject")
    monkeypatch.setattr(api, "avault_agent_deliver_inject", Mock(side_effect=api.AvaultError("grant is missing or expired")))
    out = tmp_path / "out.env"

    code = cli.cmd_vault_inject(_ns(keys="PROTECTED_KEY", out=str(out), format="dotenv", session_id="ses_cli"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
        requests = vault_service.list_requests(conn, status="pending")
    assert status == "expired"
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["requester"]["session_id"] == "ses_cli"
    assert requests[0]["delivery"]["session_id"] == "ses_cli"
    assert requests[0]["card"]["grant_options"][0]["purpose"] == "inject"


def test_fetch_resolver_publishes_pending_request_when_approval_required(monkeypatch):
    published = []
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", lambda **kwargs: published.append(kwargs))
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    with pytest.raises(cli.TaskCliError) as exc_info:
        cli._resolve_single_vault_delivery(
            cli._open_vault_engine(),
            "PROTECTED_KEY",
            requester={"source": "cli", "session_id": "ses_cli"},
            delivery={"session_id": "ses_cli", "mode": "fetch"},
            purpose="fetch",
        )

    assert exc_info.value.code == "approval_required"
    assert len(published) == 1
    request = published[0]["request"]
    assert published[0]["scope"] == "request"
    assert request["status"] == "pending"
    assert request["secret_name"] == "PROTECTED_KEY"
    assert request["delivery"]["session_id"] == "ses_cli"
    assert request["card"]["grant_options"][0]["purpose"] == "fetch"


def test_inject_resolver_publishes_pending_request_when_approval_required(tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", lambda **kwargs: published.append(kwargs))
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected"))

    with pytest.raises(cli.TaskCliError) as exc_info:
        cli._resolve_vault_inject_delivery(
            cli._open_vault_engine(),
            ["PROTECTED_KEY"],
            path=str(tmp_path / "out.env"),
            fmt="dotenv",
            args=_ns(session_id="ses_cli"),
        )

    assert exc_info.value.code == "approval_required"
    assert len(published) == 1
    request = published[0]["request"]
    assert published[0]["scope"] == "request"
    assert request["status"] == "pending"
    assert request["secret_name"] == "PROTECTED_KEY"
    assert request["delivery"]["session_id"] == "ses_cli"
    assert request["card"]["grant_options"][0]["purpose"] == "inject"


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


def test_run_records_protected_delivery_when_child_exits_70(capfd, monkeypatch):
    from vibe import api

    _set_protected_grant("PROTECTED_KEY")
    monkeypatch.setattr(api, "avault_agent_deliver_run", Mock(return_value={"exit_code": 70}))
    monkeypatch.setattr(api, "avault_deliver_run", Mock())

    assert cli.cmd_vault_run(_ns(env=["PROTECTED_KEY"], command_argv=["python3", "-c", "pass"])) == 70
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 1


def test_run_records_delivery_when_legacy_avault_returns_70(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("NODELIVER_KEY")
    monkeypatch.setattr(api, "avault_deliver_run", Mock(return_value=70))

    assert cli.cmd_vault_run(_ns(env=["NODELIVER_KEY"], command_argv=["python3", "-c", "pass"])) == 70
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 1


def test_run_releases_standard_always_ask_grant_on_explicit_pre_handoff_failure(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    grant = _set_always_ask_standard_grant("ASK_KEY", session_id="ses_cli")
    monkeypatch.setattr(api, "avault_deliver_run", Mock(return_value={"exit_code": 70, "delivered": False}))

    assert cli.cmd_vault_run(_ns(env=["ASK_KEY"], command_argv=["python3", "-c", "pass"], session_id="ses_cli")) == 70
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 0
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(vault_grants.select().where(vault_grants.c.id == grant["id"])).mappings().one()["status"]
    assert status == "active"


def test_run_bad_command_does_not_call_avault_or_deliver(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("NODELIVER_KEY")
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


def test_request_accepts_spec_path(tmp_path, capfd):
    spec_path = tmp_path / "request-spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "tags": ["github"],
                "protection": "protected",
                "policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "bearer"}},
                "links": {"skills": ["github-pr-review"]},
            }
        ),
        encoding="utf-8",
    )

    code = cli.cmd_vault_request(_ns(name="GITHUB_TOKEN", reason="need PR status", spec=str(spec_path)))
    payload = json.loads(capfd.readouterr().out)

    assert code == 0
    assert payload["request"]["card"]["spec"]["tags"] == ["github", "skill:github-pr-review"]
    assert payload["request"]["card"]["spec"]["policy"]["allowed_hosts"] == ["api.github.com"]
    assert payload["request"]["card"]["spec"]["links"] == {"skills": ["github-pr-review"]}
    assert payload["request_id"] in payload["message"]
    assert "Provide secret" in payload["message"]
    assert "Add secret" not in payload["message"]


def test_request_rejects_spec_with_plaintext_value(capfd):
    code = cli.cmd_vault_request(_ns(name="BAD_KEY", spec_json='{"policy":{"value":"secret"}}'))
    payload = json.loads(capfd.readouterr().err)

    assert code == 1
    assert payload["code"] == "invalid_spec"


def test_request_case_conflict_uses_name_conflict_code(capfd):
    _create_standard_secret("openAiKey")

    code = cli.cmd_vault_request(_ns(name="OpenAIKey", reason="need it"))
    payload = json.loads(capfd.readouterr().err)

    assert code == 1
    assert payload["code"] == "secret_name_case_conflict"
    assert "openAiKey" in payload["error"]


def test_request_for_existing_secret_returns_fulfilled(tmp_path, capfd, monkeypatch):
    _create_standard_secret("HAVE_KEY")
    assert cli.cmd_vault_request(_ns(name="HAVE_KEY", wait=30)) == 0
    assert json.loads(capfd.readouterr().out)["status"] == "fulfilled"


def test_request_wait_outputs_fulfilled_request(capfd, monkeypatch):
    def wait_mock(request_id, *, timeout, poll_interval=2.0):
        return {"id": request_id, "status": "fulfilled", "request_type": "provision", "secret_name": "WAIT_KEY"}

    monkeypatch.setattr(cli, "_wait_for_provision", wait_mock)

    assert cli.cmd_vault_request(_ns(name="WAIT_KEY", wait=30)) == 0
    payload = json.loads(capfd.readouterr().out)
    assert payload["status"] == "fulfilled"
    assert payload["request"]["status"] == "fulfilled"


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
