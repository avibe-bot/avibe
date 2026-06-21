"""CLI tests for ``vibe vault`` (P0 commit 3).

In-process: call the cmd_* handlers with constructed argparse namespaces. The autouse
``VIBE_REMOTE_HOME`` isolation in conftest points the state DB + machine key at tmp, so
nothing touches the real ``~/.avibe``. ``capfd`` captures fd-level output (including the
child process spawned by ``run``) so the no-stdout-leak property is checked for real.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

import pytest

from vibe import cli


def _ns(**kw):
    base = dict(
        name=None, stdin=False, from_file=None, group=None, tag=None, description=None,
        env=None, command_argv=None, reason=None, skill=None, wait=None, no_wait=False, json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


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


def test_set_then_list_is_masked(tmp_path, capfd):
    vf = tmp_path / "value.txt"
    vf.write_text("sk-ant-abcd1234")
    assert cli.cmd_vault_set(_ns(name="OPENAI_API_KEY", from_file=str(vf), description="key")) == 0
    capfd.readouterr()  # drain the 'saved' payload
    assert cli.cmd_vault_list(_ns()) == 0
    out = json.loads(capfd.readouterr().out)
    secret = out["secrets"][0]
    assert secret["name"] == "OPENAI_API_KEY"
    assert secret["preview"] == "…1234"
    assert "sk-ant-abcd1234" not in json.dumps(out)


def test_run_injects_to_child_without_stdout_leak(tmp_path, capfd):
    vf = tmp_path / "value.txt"
    vf.write_text("topsecret-RUNVAL-42")
    assert cli.cmd_vault_set(_ns(name="RUN_KEY", from_file=str(vf))) == 0
    capfd.readouterr()

    child_out = tmp_path / "child.txt"
    code = cli.cmd_vault_run(
        _ns(
            env=["RUN_KEY"],
            command_argv=[
                sys.executable,
                "-c",
                "import os, sys; open(sys.argv[1], 'w').write(os.environ['RUN_KEY'])",
                str(child_out),
            ],
        )
    )
    captured = capfd.readouterr()
    assert code == 0
    # The value reached the child's environment...
    assert child_out.read_text() == "topsecret-RUNVAL-42"
    # ...but never the CLI's own stdout/stderr.
    assert "topsecret-RUNVAL-42" not in captured.out
    assert "topsecret-RUNVAL-42" not in captured.err


def test_run_supports_env_rename(tmp_path, capfd):
    vf = tmp_path / "value.txt"
    vf.write_text("renamed-value")
    cli.cmd_vault_set(_ns(name="SRC_KEY", from_file=str(vf)))
    capfd.readouterr()
    child_out = tmp_path / "child.txt"
    code = cli.cmd_vault_run(
        _ns(
            env=["LOCAL_NAME=SRC_KEY"],
            command_argv=[
                sys.executable,
                "-c",
                "import os, sys; open(sys.argv[1], 'w').write(os.environ['LOCAL_NAME'])",
                str(child_out),
            ],
        )
    )
    assert code == 0
    assert child_out.read_text() == "renamed-value"


def test_run_missing_secret_is_clean_error(tmp_path, capfd):
    code = cli.cmd_vault_run(_ns(env=["NOPE"], command_argv=["echo", "hi"]))
    captured = capfd.readouterr()
    assert code == 1
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["code"] == "secret_not_found"


def test_set_rejects_invalid_name(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("x")
    code = cli.cmd_vault_set(_ns(name="lower_bad", from_file=str(vf)))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "invalid_name"


def test_set_requires_one_value_source(tmp_path, capfd):
    # Neither --stdin nor --from-file.
    code = cli.cmd_vault_set(_ns(name="NO_SOURCE"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "missing_value_source"


def test_rm_then_list_empty(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("v")
    cli.cmd_vault_set(_ns(name="GONE_KEY", from_file=str(vf)))
    capfd.readouterr()
    assert cli.cmd_vault_rm(_ns(name="GONE_KEY")) == 0
    capfd.readouterr()
    assert cli.cmd_vault_list(_ns()) == 0
    assert json.loads(capfd.readouterr().out)["secrets"] == []


def test_run_rejects_bad_env_name(tmp_path, capfd):
    code = cli.cmd_vault_run(_ns(env=["BAD-NAME=KEY"], command_argv=["echo", "hi"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "invalid_env_name"


def test_request_creates_pending(tmp_path, capfd):
    code = cli.cmd_vault_request(_ns(name="WANTED_KEY", reason="need it"))
    captured = capfd.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["secret_name"] == "WANTED_KEY"
    assert payload["status"] == "pending"
    assert payload["request_id"].startswith("vrq_")


def test_request_for_existing_secret_returns_fulfilled(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("v")
    cli.cmd_vault_set(_ns(name="HAVE_KEY", from_file=str(vf)))
    capfd.readouterr()
    assert cli.cmd_vault_request(_ns(name="HAVE_KEY", wait=30)) == 0  # must not block
    assert json.loads(capfd.readouterr().out)["status"] == "fulfilled"


def test_run_bad_command_does_not_deliver(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("v")
    cli.cmd_vault_set(_ns(name="NODELIVER_KEY", from_file=str(vf)))
    capfd.readouterr()
    code = cli.cmd_vault_run(_ns(env=["NODELIVER_KEY"], command_argv=["definitely-not-a-real-binary-xyz123"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "command_not_found"
    # The secret was never resolved → no usage recorded.
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 0


def test_from_file_preserves_trailing_newline(tmp_path):
    # PEM/SSH material and many tokens end in a significant newline — --from-file is byte-exact.
    vf = tmp_path / "key.pem"
    vf.write_text("-----BEGIN-----\nabc\n-----END-----\n")
    value = cli._read_secret_value(_ns(from_file=str(vf)), help_command="x")
    assert value == "-----BEGIN-----\nabc\n-----END-----\n"


def test_stdin_strips_only_one_trailing_newline(monkeypatch):
    # Interactive stdin drops the single Enter/heredoc newline, but not internal/extra ones.
    monkeypatch.setattr("sys.stdin", io.StringIO("tok\n"))
    assert cli._read_secret_value(_ns(stdin=True), help_command="x") == "tok"
    monkeypatch.setattr("sys.stdin", io.StringIO("tok\n\n"))
    assert cli._read_secret_value(_ns(stdin=True), help_command="x") == "tok\n"


def test_run_non_executable_command_is_clean_error(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("v")
    cli.cmd_vault_set(_ns(name="EXEC_KEY", from_file=str(vf)))
    capfd.readouterr()
    # A file that passes which() (+x, absolute path) but fails execve (no shebang / bad format)
    # raises OSError, not FileNotFoundError → must be a structured error, not a traceback, and
    # must not record a delivery (the child never started).
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\x00\x01 not a valid executable")
    os.chmod(bad, 0o755)
    code = cli.cmd_vault_run(_ns(env=["EXEC_KEY"], command_argv=[str(bad)]))
    captured = capfd.readouterr()
    assert code == 126
    assert json.loads(captured.err)["code"] == "command_not_executable"
    cli.cmd_vault_list(_ns())
    assert json.loads(capfd.readouterr().out)["secrets"][0]["use_count"] == 0


def test_run_records_delivery_after_spawn(tmp_path, capfd):
    vf = tmp_path / "v.txt"
    vf.write_text("delivered-value")
    cli.cmd_vault_set(_ns(name="DELIVER_KEY", from_file=str(vf)))
    capfd.readouterr()
    # A successful run records exactly one delivery (recorded right after spawn, not after the
    # child exits — so an interrupted long-running child is still audited).
    code = cli.cmd_vault_run(_ns(env=["DELIVER_KEY"], command_argv=[sys.executable, "-c", "pass"]))
    assert code == 0
    capfd.readouterr()
    cli.cmd_vault_list(_ns())
    secret = json.loads(capfd.readouterr().out)["secrets"][0]
    assert secret["use_count"] == 1
    assert secret["last_used_at"] is not None
