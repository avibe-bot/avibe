"""CLI tests for ``vibe vault fetch`` around avault delivery.

Avibe validates policy and passes an envelope/request to avault. It never receives the
secret value.
"""

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
        auth=None,
        url=None,
        method="GET",
        header=None,
        data=None,
        data_file=None,
        output=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _set(name, value, tmp_path, monkeypatch, capfd, **kw):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / f"{name}.txt"
    vf.write_text(value)
    monkeypatch.setattr(api, "avault_seal", Mock(return_value=_sealed(name.lower())))
    assert cli.cmd_vault_set(_ns(name=name, from_file=str(vf), **kw)) == 0
    capfd.readouterr()


def test_fetch_passes_bearer_request_to_avault_and_writes_stdout(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("GH_PAT", "ghp-never-returned", tmp_path, monkeypatch, capfd, allow_host=["api.github.com"])
    fetch = Mock(return_value={"status": 200, "headers": {"content-type": "application/json"}, "body": '{"ok":true}'})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.github.com/repos/o/r"))
    captured = capfd.readouterr()

    assert code == 0
    assert captured.out == '{"ok":true}'
    assert "ghp-never-returned" not in captured.out
    assert "ghp-never-returned" not in captured.err
    fetch.assert_called_once()
    name, sealed, request = fetch.call_args.args
    assert name == "GH_PAT"
    assert sealed == _sealed("gh_pat")
    assert request["allowed_hosts"] == ["api.github.com"]
    assert request["inject"] == {"type": "bearer"}
    assert request["method"] == "GET"
    assert request["url"] == "https://api.github.com/repos/o/r"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.get_secret_meta(conn, "GH_PAT")["use_count"] == 1


def test_fetch_header_auth_request_shape(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("SVC_KEY", "apikey-never-returned", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"], auth_header="X-Api-Key")
    fetch = Mock(return_value={"status": 204, "headers": {}, "body": ""})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    assert cli.cmd_vault_fetch(_ns(auth="SVC_KEY", url="https://api.example.com/v1/thing")) == 0
    request = fetch.call_args.args[2]
    assert request["inject"] == {"type": "header", "name": "X-Api-Key"}


def test_fetch_query_auth_request_shape(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("QUERY_KEY", "query-never-returned", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"], auth_query="api_key")
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    assert cli.cmd_vault_fetch(_ns(auth="QUERY_KEY", url="https://api.example.com/v1/thing")) == 0
    request = fetch.call_args.args[2]
    assert request["inject"] == {"type": "query", "name": "api_key"}


def test_fetch_post_body_and_headers_pass_to_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("POST_KEY", "k", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 201, "headers": {}, "body": "created"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    assert cli.cmd_vault_fetch(
        _ns(
            auth="POST_KEY",
            url="https://api.example.com/create",
            method="POST",
            header=["X-Trace: 123"],
            data='{"x":1}',
        )
    ) == 0
    request = fetch.call_args.args[2]
    assert request["method"] == "POST"
    assert request["headers"] == {"X-Trace": "123"}
    assert request["body"] == '{"x":1}'


def test_fetch_writes_mocked_response_to_output_file(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("OUT_KEY", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "file body"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)
    out = tmp_path / "resp.txt"

    assert cli.cmd_vault_fetch(_ns(auth="OUT_KEY", url="https://api.example.com/x", output=str(out))) == 0
    assert capfd.readouterr().out == ""
    assert out.read_text() == "file body"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://evil.example.com/x", "host_not_allowed"),
        ("http://api.example.com/x", "insecure_transport"),
    ],
)
def test_fetch_preflights_reject_before_avault(tmp_path, capfd, monkeypatch, url, code):
    from unittest.mock import Mock

    from vibe import api

    _set("BOUND_KEY", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    result = cli.cmd_vault_fetch(_ns(auth="BOUND_KEY", url=url))
    captured = capfd.readouterr()

    assert result == 1
    assert json.loads(captured.err)["code"] == code
    fetch.assert_not_called()


def test_fetch_refuses_unbound_secret_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("UNBOUND_KEY", "secret", tmp_path, monkeypatch, capfd)
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="UNBOUND_KEY", url="https://api.example.com/x"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "proxy_unbound"
    fetch.assert_not_called()


def test_fetch_rejects_trace_method_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("GH_PAT", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.example.com/x", method="TRACE"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "method_not_allowed"
    fetch.assert_not_called()


def test_fetch_rejects_host_header_override_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("GH_PAT", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.example.com/x", header=["Host: evil.example.com"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "forbidden_header"
    fetch.assert_not_called()


def test_set_rejects_host_auth_header_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    vf = tmp_path / "v.txt"
    vf.write_text("tok")
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal", seal)

    code = cli.cmd_vault_set(_ns(name="HOST_AUTH_KEY", from_file=str(vf), allow_host=["api.example.com"], auth_header="Host"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "forbidden_header"
    seal.assert_not_called()


def test_fetch_output_unwritable_is_preflighted_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("GH_PAT", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    bad_out = tmp_path / "no_such_dir" / "resp.json"
    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.example.com/x", method="POST", output=str(bad_out)))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "output_unwritable"
    fetch.assert_not_called()


def test_fetch_returns_response_even_if_audit_fails(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set("GH_PAT", "secret", tmp_path, monkeypatch, capfd, allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(vault_service, "record_proxy_use", _boom)
    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.example.com/x"))
    captured = capfd.readouterr()
    assert code == 0
    assert captured.out == "ok"
    fetch.assert_called_once()


def test_host_allowed_is_case_insensitive():
    assert cli._host_allowed("api.github.com", ["API.GITHUB.COM"]) is True
    assert cli._host_allowed("api.github.com", [".GitHub.com"]) is True
    assert cli._host_allowed("API.GITHUB.COM", ["api.github.com"]) is True
