"""CLI tests for the help-only delivery modes ``vibe vault export`` / ``inject``."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from vibe import cli


def _ns(**kw):
    base = dict(
        name=None, stdin=False, from_file=None, group=None, tag=None, description=None,
        allow_host=None, auth_header=None, auth_query=None,
        env=None, keys=None, out=None, format="dotenv", json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _set(name, value, tmp_path):
    vf = tmp_path / f"{name}.txt"
    vf.write_text(value)
    assert cli.cmd_vault_set(_ns(name=name, from_file=str(vf))) == 0


def test_export_emits_eval_lines(tmp_path, capfd):
    _set("OPENAI_API_KEY", "sk-with space&special", tmp_path)
    capfd.readouterr()
    assert cli.cmd_vault_export(_ns(env=["OPENAI_API_KEY", "ALIAS=OPENAI_API_KEY"])) == 0
    out = capfd.readouterr().out
    # Both the same-name and the renamed export are emitted, shell-quoted so eval is safe.
    assert "export OPENAI_API_KEY=" in out
    assert "export ALIAS=" in out
    # Round-trip: a shell parsing the quoted value recovers the original.
    import shlex

    line = next(line_ for line_ in out.splitlines() if line_.startswith("export OPENAI_API_KEY="))
    rhs = line.split("=", 1)[1]
    assert shlex.split(rhs)[0] == "sk-with space&special"


@pytest.mark.parametrize("fmt", ["dotenv", "json", "yaml", "toml"])
def test_inject_renders_each_format_to_0600_file(tmp_path, capfd, fmt):
    _set("A_KEY", "alpha-1", tmp_path)
    _set("B_KEY", "beta-2", tmp_path)
    capfd.readouterr()
    out = tmp_path / f"secrets.{fmt}"
    assert cli.cmd_vault_inject(_ns(keys="A_KEY,B_KEY", out=str(out), format=fmt)) == 0
    payload = json.loads(capfd.readouterr().out)
    assert payload["written"] is True and payload["format"] == fmt

    # File is 0600.
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600

    text = out.read_text()
    assert "alpha-1" in text and "beta-2" in text
    if fmt == "json":
        assert json.loads(text) == {"A_KEY": "alpha-1", "B_KEY": "beta-2"}
    elif fmt == "toml":
        assert 'A_KEY = "alpha-1"' in text
    elif fmt == "yaml":
        import yaml

        assert yaml.safe_load(text) == {"A_KEY": "alpha-1", "B_KEY": "beta-2"}
    else:  # dotenv
        assert "A_KEY=alpha-1" in text


def test_inject_overwrites_preexisting_loose_file_as_0600(tmp_path, capfd):
    # If the target already exists with permissive perms, the write must NOT inherit them —
    # the atomic 0600 temp+replace ensures the secret is never momentarily world-readable.
    _set("A_KEY", "alpha-1", tmp_path)
    capfd.readouterr()
    out = tmp_path / "preexisting.env"
    out.write_text("OLD=stale\n")
    os.chmod(out, 0o644)
    assert cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(out), format="dotenv")) == 0
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert out.read_text() == "A_KEY=alpha-1\n"  # fully replaced, not appended


def test_inject_dedupes_repeated_keys(tmp_path, capfd):
    # --keys A,A renders one entry; usage/audit must reflect one delivery, not two.
    _set("A_KEY", "alpha-1", tmp_path)
    capfd.readouterr()
    out = tmp_path / "dup.env"
    assert cli.cmd_vault_inject(_ns(keys="A_KEY,A_KEY", out=str(out), format="dotenv")) == 0
    assert json.loads(capfd.readouterr().out)["keys"] == ["A_KEY"]
    cli.cmd_vault_list(_ns())
    assert json.loads(capfd.readouterr().out)["secrets"][0]["use_count"] == 1


def test_inject_payload_does_not_leak_value(tmp_path, capfd):
    _set("SECRET_KEY", "topsecret-INJECT", tmp_path)
    capfd.readouterr()
    out = tmp_path / "s.env"
    cli.cmd_vault_inject(_ns(keys="SECRET_KEY", out=str(out), format="dotenv"))
    payload_out = capfd.readouterr().out
    # The CLI's JSON payload reports path/keys, never the value (the value is only in the file).
    assert "topsecret-INJECT" not in payload_out
    assert "topsecret-INJECT" in out.read_text()


def test_export_missing_secret_clean_error(tmp_path, capfd):
    code = cli.cmd_vault_export(_ns(env=["NOPE"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "secret_not_found"


def test_inject_unknown_format_rejected(tmp_path, capfd):
    _set("K", "v", tmp_path)
    capfd.readouterr()
    code = cli.cmd_vault_inject(_ns(keys="K", out=str(tmp_path / "o"), format="xml"))
    assert code == 1
    assert json.loads(capfd.readouterr().err)["code"] == "invalid_format"
