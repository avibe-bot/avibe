"""CLI tests for ``vibe vault export`` / ``inject`` with avault-backed delivery."""

from __future__ import annotations

import argparse
import json

import pytest

from storage import vault_service
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
        keys=None,
        out=None,
        format="dotenv",
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _set(name, value, tmp_path, monkeypatch, capfd, *, sealed: Sealed | None = None):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / f"{name}.txt"
    vf.write_text(value)
    monkeypatch.setattr(api, "avault_seal", Mock(return_value=sealed or _sealed(name.lower())))
    assert cli.cmd_vault_set(_ns(name=name, from_file=str(vf))) == 0
    capfd.readouterr()


def test_export_is_deprecated_and_does_not_touch_db(capfd):
    code = cli.cmd_vault_export(_ns(env=["OPENAI_API_KEY"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "export_deprecated"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_secrets(conn) == []


@pytest.mark.parametrize("fmt", ["dotenv", "json"])
def test_inject_calls_avault_and_records_delivery(tmp_path, capfd, monkeypatch, fmt):
    from unittest.mock import Mock

    from vibe import api

    _set("A_KEY", "alpha-1", tmp_path, monkeypatch, capfd, sealed=_sealed("a"))
    _set("B_KEY", "beta-2", tmp_path, monkeypatch, capfd, sealed=_sealed("b"))
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)
    out = tmp_path / f"secrets.{fmt}"

    assert cli.cmd_vault_inject(_ns(keys="A_KEY,B_KEY", out=str(out), format=fmt)) == 0
    payload = json.loads(capfd.readouterr().out)
    assert payload["written"] is True
    assert payload["format"] == fmt
    inject.assert_called_once_with(
        str(out),
        fmt,
        [
            {"name": "A_KEY", "key": "A_KEY", "envelope": _sealed("a")},
            {"name": "B_KEY", "key": "B_KEY", "envelope": _sealed("b")},
        ],
    )
    cli.cmd_vault_list(_ns())
    secrets = {s["name"]: s for s in json.loads(capfd.readouterr().out)["secrets"]}
    assert secrets["A_KEY"]["use_count"] == 1
    assert secrets["B_KEY"]["use_count"] == 1


@pytest.mark.parametrize("fmt", ["yaml", "toml"])
def test_inject_rejects_unavailable_formats_before_avault(tmp_path, capfd, monkeypatch, fmt):
    from unittest.mock import Mock

    from vibe import api

    _set("A_KEY", "alpha-1", tmp_path, monkeypatch, capfd)
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    code = cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(tmp_path / f"s.{fmt}"), format=fmt))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "format_unavailable"
    inject.assert_not_called()


def test_inject_unknown_format_rejected_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("K", "v", tmp_path, monkeypatch, capfd)
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    code = cli.cmd_vault_inject(_ns(keys="K", out=str(tmp_path / "o"), format="xml"))
    assert code == 1
    assert json.loads(capfd.readouterr().err)["code"] == "invalid_format"
    inject.assert_not_called()


def test_inject_dedupes_repeated_keys(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("A_KEY", "alpha-1", tmp_path, monkeypatch, capfd, sealed=_sealed("a"))
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    out = tmp_path / "dup.env"
    assert cli.cmd_vault_inject(_ns(keys="A_KEY,A_KEY", out=str(out), format="dotenv")) == 0
    assert json.loads(capfd.readouterr().out)["keys"] == ["A_KEY"]
    inject.assert_called_once_with(str(out), "dotenv", [{"name": "A_KEY", "key": "A_KEY", "envelope": _sealed("a")}])
    cli.cmd_vault_list(_ns())
    assert json.loads(capfd.readouterr().out)["secrets"][0]["use_count"] == 1


def test_inject_payload_does_not_leak_value(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("SECRET_KEY", "topsecret-INJECT", tmp_path, monkeypatch, capfd)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(return_value=None))

    cli.cmd_vault_inject(_ns(keys="SECRET_KEY", out=str(tmp_path / "s.env"), format="dotenv"))
    payload_out = capfd.readouterr().out
    assert "topsecret-INJECT" not in payload_out


def test_inject_does_not_record_delivery_when_avault_fails(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("A_KEY", "alpha-1", tmp_path, monkeypatch, capfd)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(side_effect=api.AvaultError("boom")))

    code = cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(tmp_path / "fail.env"), format="dotenv"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    cli.cmd_vault_list(_ns())
    assert json.loads(capfd.readouterr().out)["secrets"][0]["use_count"] == 0


def test_inject_succeeds_even_if_audit_fails(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("A_KEY", "alpha-1", tmp_path, monkeypatch, capfd)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(return_value=None))

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(vault_service, "record_deliveries", _boom)
    assert cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(tmp_path / "ok.env"), format="dotenv")) == 0
