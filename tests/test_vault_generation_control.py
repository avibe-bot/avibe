"""Thin-CLI/service boundary and generation guards; no RNG duplicated in Python."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select

from core import control_ipc
from core.control_requests import ControlRequest, new_request, result
from core.handlers.cli_requests import handle_control_request
from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, metadata, vault_audit, vault_secrets
from storage.vault_crypto import Sealed
from vibe import api, cli, internal_client


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from storage import db

    value = create_sqlite_engine(tmp_path / "service.sqlite")
    metadata.create_all(value)
    monkeypatch.setattr(db, "get_cached_sqlite_engine", lambda: value)
    monkeypatch.setattr(api, "release_vault_agent_scopes", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", lambda **kw: None)
    yield value
    value.dispose()


def _transport(monkeypatch, receive):
    endpoint = control_ipc.ControlIpcClientEndpoint(transport="unix", socket_path=Path("/unused-test.sock"))
    monkeypatch.setattr(internal_client, "_resolve_endpoint", lambda _: endpoint)
    monkeypatch.setattr(internal_client, "_sync_transport", lambda _: httpx.MockTransport(receive))


def _run(arguments, *, caller=None):
    request = new_request("vault.generate", {"name": "LOGIN", "recipe": {"type": "password"}, **arguments})
    payload = request.to_payload()
    if caller is not None:
        payload["caller_env"] = caller
    return asyncio.run(handle_control_request(SimpleNamespace(), payload))


def _counts(engine):
    with engine.connect() as conn:
        return (len(conn.execute(select(vault_secrets)).all()), len(conn.execute(select(vault_audit)).all()))


def test_generate_main_is_db_free_and_never_accepts_provenance_inputs(monkeypatch, capfd):
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_real_caller")
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Generation CLI opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", forbidden)
    sent = []

    def receive(request):
        sent.append(ControlRequest.from_payload(json.loads(request.content)))
        return httpx.Response(200, json=result({"schema_version": 1, "ok": True, "kind": "vault_secret",
                  "secret": {"name": "LOGIN", "generated": []}, "generated": {"by": "agent"}}))

    _transport(monkeypatch, receive)
    monkeypatch.setattr(sys, "argv", ["vibe", "vault", "generate", "LOGIN", "--field", "password", "--no-lookalikes"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert attempts == []
    assert sent[0].operation == "vault.generate"
    assert sent[0].caller_env["AVIBE_SESSION_ID"] == "ses_real_caller"
    assert "session_id" not in sent[0].arguments
    assert sent[0].arguments["recipe"] == {"type": "password", "no_lookalikes": True}
    assert json.loads(capfd.readouterr().out)["secret"]["name"] == "LOGIN"


def test_missing_service_has_no_offline_generation_or_process_start(monkeypatch, tmp_path, capfd):
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(tmp_path / "missing.sock"))
    start = Mock(side_effect=AssertionError("must not start process"))
    connect = Mock(side_effect=AssertionError("must not open SQLite"))
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(sqlite3, "connect", connect)
    args = cli.build_parser().parse_args(["vault", "generate", "LOGIN"])
    assert cli.cmd_vault_generate(args) == 1
    payload = json.loads(capfd.readouterr().err)
    assert (payload["code"], payload["execution"], payload["help_command"]) == (
        "service_unavailable", "not_executed", "vibe vault generate --help",
    )
    start.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("tail", [["--unknown"], ["--type", "invalid"], ["--length", "false"], ["--no-symbols=false"]])
def test_generate_parser_rejects_unknown_or_wrong_type_flags_with_help(tail, capfd):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["vault", "generate", "LOGIN", *tail])
    assert exited.value.code != 0
    assert json.loads(capfd.readouterr().err)["help_command"] == "vibe vault generate --help"


@pytest.mark.parametrize("recipe", [
    {"type": "unknown"}, {"type": "password", "length": 7}, {"type": "password", "length": 101},
    {"type": "password", "length": 128},
    {"type": "password", "bytes": 32}, {"type": "hex", "length": 24},
    {"type": "base64", "digits": False}, {"type": "hex", "symbols": False},
    {"type": "hex", "no_lookalikes": False}, {"type": "base64", "bytes": 15},
    {"type": "hex", "bytes": 65}, {"type": "password", "digits": "false"},
    {"type": "password", "unknown": "do-not-echo"}, {"type": "password", "field": "password"},
])
def test_invalid_recipe_never_reaches_custody_or_storage(engine, monkeypatch, recipe):
    custody = Mock(side_effect=AssertionError("custody must not run"))
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run({"recipe": recipe})
    assert response["execution"] == "not_executed"
    assert response["payload"]["code"] == "invalid_generate_recipe"
    assert response["payload"]["help_command"] == "vibe vault generate --help"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


@pytest.mark.parametrize("length,generated", [(8, False), (11, False), (12, True), (100, True)])
def test_direct_generation_lengths_are_what_avault_makes(engine, monkeypatch, length, generated):
    # The user picks 8..100; an Agent generates 12..100 (custody's floor). Shorter is refused here,
    # before custody, with the request preset as the way forward, rather than failing inside avault.
    custody = Mock(return_value=Sealed("generated-envelope", "nonce", "wrap"))
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run({"recipe": {"type": "password", "length": length}})
    if generated:
        assert response["exit_code"] == 0
        assert custody.call_args.args[1]["length"] == length
        return
    assert response["execution"] == "not_executed"
    assert response["payload"]["code"] == "invalid_generate_recipe"
    assert "at least 12 characters" in response["payload"]["error"]
    assert "vibe vault request" in response["payload"]["hint"]
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


def test_generate_length_help_states_the_accepted_range(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["vault", "generate", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert f"Password characters, {api.AVAULT_GENERATE_PASSWORD_MIN_LENGTH}..{vs.GENERATE_PASSWORD_LENGTH[1]}" in help_text


@pytest.mark.parametrize("field,kind,protection", [
    (None, "static", "standard"), ("password", "static", "standard"),
    ("new", "static", "protected"), ("new", "keypair", "standard"),
])
def test_existing_target_guards_precede_custody(engine, monkeypatch, field, kind, protection):
    with engine.begin() as conn:
        vs.create_secret(conn, name="LOGIN", sealed=Sealed("stored-test-plaintext", "nonce", "wrap"),
                         kind=kind, protection=protection, default_field="password" if kind == "static" else None)
    before = _counts(engine)
    custody = Mock(side_effect=AssertionError("custody must not run"))
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run({"field": field})
    assert response["payload"]["code"] == "generate_target_not_allowed"
    assert "vibe vault request" in response["payload"]["hint"]
    assert "stored-test-plaintext" not in json.dumps(response)
    assert _counts(engine) == before
    custody.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"name": "bad.name"}, {"field": "Uppercase"}, {"description": 7},
    {"auth_name": "X-Token"}, {"fetch_auth": "header"},
    {"allow_host": ["bad host"]},
])
def test_invalid_target_or_metadata_is_rejected_before_custody(engine, monkeypatch, arguments):
    custody = Mock()
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run(arguments)
    assert response["execution"] == "not_executed"
    assert response["payload"]["ok"] is False
    assert response["payload"]["help_command"] == "vibe vault generate --help"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


def test_case_conflicting_name_and_full_field_set_refuse_without_custody(engine, monkeypatch):
    with engine.begin() as conn:
        vs.create_secret(conn, name="LOGIN", sealed=Sealed("opaque", "nonce", "wrap"),
                         fields=[{"name": f"field{i}", "sealed": Sealed("opaque", "nonce", "wrap")} for i in range(31)])
    custody = Mock()
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    before = _counts(engine)
    case = _run({"name": "login"})
    assert case["payload"]["code"] == "generate_target_not_allowed"
    assert "vibe vault request" in case["payload"]["hint"]
    full = _run({"field": "new"})
    assert full["payload"]["ok"] is False
    assert _counts(engine) == before
    custody.assert_not_called()


def test_protected_flag_and_spoofed_provenance_fail_without_generation(engine, monkeypatch):
    custody = Mock(side_effect=AssertionError("custody must not run"))
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    assert _run({"protection": "protected"})["payload"]["code"] == "generate_target_not_allowed"
    assert _run({"session_id": "spoof"})["payload"]["code"] == "invalid_generate_arguments"
    assert _run({"recipe": {"type": "password", "session_id": "spoof"}})["payload"]["code"] == "invalid_generate_recipe"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


def test_authenticated_service_generates_and_preserves_unrelated_metadata(engine, monkeypatch, capfd, caplog):
    known_plaintext = "known-stored-secret-never-in-output"
    with engine.begin() as conn:
        now = vs._now()
        conn.execute(agent_sessions.insert().values(id="ses_origin", session_anchor="ses_origin", title=None,
                     agent_name="vibey", agent_backend="vibey", agent_variant="vibey", workdir="/test",
                     native_session_id="", status="active", visibility="background", pinned=0,
                     metadata_json="{}", created_at=now, updated_at=now, last_active_at=now))
        vs.create_secret(conn, name="LOGIN", sealed=Sealed(known_plaintext, "nonce", "wrap"),
                         description="Existing description", tags=["old", "skill:keep"],
                         policy={"allowed_hosts": ["api.example.com"], "auth": {"type": "header", "name": "X-Token"}, "internal": "preserve"})
    custody = Mock(return_value=Sealed("generated-envelope", "nonce", "wrap"))
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run({"field": "token", "recipe": {"type": "hex"}, "tag": ["new"]}, caller={"AVIBE_SESSION_ID": "ses_origin"})
    assert response["exit_code"] == 0
    secret = response["payload"]["secret"]
    assert secret["description"] == "Existing description"
    assert secret["tags"] == ["new", "skill:keep"]
    assert secret["policy"] == {"allowed_hosts": ["api.example.com"], "auth": {"type": "header", "name": "X-Token"}, "internal": "preserve"}
    assert secret["generated"][0]["session_id"] == "ses_origin"
    custody.assert_called_once_with("LOGIN.token", {"type": "hex", "bytes": 32})
    output = capfd.readouterr()
    with engine.connect() as conn:
        audits = [dict(row) for row in conn.execute(select(vault_audit)).mappings()]
    assert known_plaintext not in json.dumps(response) + json.dumps(audits) + output.out + output.err + caplog.text


def test_service_requires_editor_before_custody(engine, monkeypatch):
    from vibe.authorization import AuthorizationContext

    monkeypatch.setattr(cli, "_cli_invocation_authority", lambda env: AuthorizationContext(is_remote=True))
    custody = Mock()
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    response = _run({})
    assert response["payload"]["code"] == "instance_access_forbidden"
    assert cli._CLI_COMMAND_FLOORS[("vault", "generate")] == "editor"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


@pytest.mark.parametrize("version", ["0.1.12", None])
def test_unsupported_avault_version_fails_before_subprocess(monkeypatch, version):
    monkeypatch.setattr(api, "_probe_avault_version", lambda *args: version)
    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: "/test/avault")
    custody = Mock()
    monkeypatch.setattr(api, "_run_avault", custody)
    with pytest.raises(api.AvaultPreHandoffError, match=r"requires avault >= 0.1.13") as error:
        api.avault_generate_secret("LOGIN", {"type": "hex", "bytes": 32})
    assert error.value.minimum_version == "0.1.13"
    custody.assert_not_called()


@pytest.mark.parametrize("tail", [["--type", "hex", "--no-digits"], ["--type", "base64", "--length", "24"], ["--bytes", "32"]])
def test_cli_recipe_mismatches_fail_whole_call(engine, monkeypatch, capfd, tail):
    custody = Mock()
    monkeypatch.setattr(api, "avault_generate_secret", custody)
    monkeypatch.setattr(cli, "_vault_control_request", lambda operation, arguments, **kw: _run(arguments))
    args = cli.build_parser().parse_args(["vault", "generate", "LOGIN", *tail])
    assert cli.cmd_vault_generate(args) == 1
    assert json.loads(capfd.readouterr().err)["code"] == "invalid_generate_recipe"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


def test_old_avault_version_keeps_safe_actionable_error_through_service_and_cli(engine, monkeypatch, capfd):
    monkeypatch.setattr(api, "_probe_avault_version", lambda *args: "0.1.12")
    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: "/test/avault")
    custody = Mock()
    monkeypatch.setattr(api, "_run_avault", custody)
    monkeypatch.setattr(cli, "_vault_control_request", lambda operation, arguments, **kw: _run(arguments))
    args = cli.build_parser().parse_args(["vault", "generate", "LOGIN"])
    assert cli.cmd_vault_generate(args) == 1
    payload = json.loads(capfd.readouterr().err)
    assert payload["code"] == "avault_upgrade_required"
    assert payload["details"] == {"minimum_version": "0.1.13"}
    assert "requires avault >= 0.1.13" in payload["error"]
    assert payload["help_command"] == "vibe vault generate --help"
    custody.assert_not_called()
    assert _counts(engine) == (0, 0)


@pytest.mark.parametrize("recipe,flags", [
    ({"type": "password", "length": 24, "digits": False, "symbols": False, "no_lookalikes": True},
     ["--type", "password", "--length", "24", "--no-digits", "--no-symbols", "--no-lookalikes"]),
    ({"type": "hex", "bytes": 16}, ["--type", "hex", "--bytes", "16"]),
    ({"type": "base64", "bytes": 64}, ["--type", "base64", "--bytes", "64"]),
])
def test_custody_wrapper_has_only_recipe_flags_and_sealed_output(monkeypatch, recipe, flags):
    monkeypatch.setattr(api, "_require_avault_min_version", lambda *args, **kw: None)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"ciphertext": "opaque", "nonce": "nonce", "wrap_meta": "wrap"})))
    monkeypatch.setattr(api, "_run_avault", run)
    assert api.avault_generate_secret("LOGIN.token", recipe) == Sealed("opaque", "nonce", "wrap")
    run.assert_called_once_with(["generate-secret", "--name", "LOGIN.token", *flags])


@pytest.mark.parametrize("returncode,stdout", [(1, "known-plaintext"), (0, '{"value":"known-plaintext"}'), (0, 'known-plaintext')])
def test_custody_errors_do_not_reflect_process_output(monkeypatch, returncode, stdout):
    monkeypatch.setattr(api, "_require_avault_min_version", lambda *args, **kw: None)
    monkeypatch.setattr(api, "_run_avault", lambda *args: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="known-plaintext"))
    with pytest.raises(api.AvaultError) as error:
        api.avault_generate_secret("LOGIN", {"type": "hex", "bytes": 32})
    assert "known-plaintext" not in str(error.value)
