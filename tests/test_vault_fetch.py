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


def _create_standard_secret(
    name: str,
    *,
    allow_host: list[str] | None = None,
    auth_header: str | None = None,
    auth_query: str | None = None,
) -> None:
    policy: dict = {}
    if allow_host:
        policy["allowed_hosts"] = allow_host
    if auth_header:
        policy["auth"] = {"type": "header", "name": auth_header}
    elif auth_query:
        policy["auth"] = {"type": "query", "name": auth_query}
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            sealed=_sealed(name.lower()),
            policy=policy or None,
        )


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


def _set_protected_grant(name: str, *, allow_host: list[str], session_id: str | None = None) -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            protection="protected",
            sealed=_sealed(name.lower()),
            policy={"allowed_hosts": allow_host},
        )
        req = vault_service.create_access_request(
            conn,
            name,
            purpose="fetch",
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": "fetch"} if session_id else {"mode": "fetch"},
        )
        return _grant_from_request(conn, req, session_id=session_id)


def _set_always_ask_grant(name: str, *, allow_host: list[str]) -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            sealed=_sealed(name.lower()),
            policy={"always_ask": True, "allowed_hosts": allow_host},
        )
        req = vault_service.create_access_request(
            conn,
            name,
            purpose="fetch",
            requester={"source": "cli"},
            delivery={"mode": "fetch"},
        )
        return _grant_from_request(conn, req)


def test_fetch_passes_bearer_request_to_avault_and_writes_stdout(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("GH_PAT", allow_host=["api.github.com"])
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


def test_fetch_rejects_keypair_before_avault_delivery(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ETH_KEY",
            sealed=_sealed("eth"),
            kind="keypair",
            signer_kind="local",
        )
    fetch = Mock()
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="ETH_KEY", url="https://api.example.com/v1/thing"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "keypair_not_value_deliverable"
    fetch.assert_not_called()


def test_fetch_uses_agent_delivery_for_protected_grant(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    grant = _set_protected_grant("GH_PAT", allow_host=["api.github.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": '{"ok":true}'})
    monkeypatch.setattr(api, "avault_agent_deliver_fetch", fetch)
    monkeypatch.setattr(api, "avault_deliver_fetch", Mock())

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.github.com/repos/o/r"))
    captured = capfd.readouterr()

    assert code == 0
    assert captured.out == '{"ok":true}'
    fetch.assert_called_once()
    assert fetch.call_args.kwargs["grant_id"] == grant["id"]
    assert fetch.call_args.kwargs["sealed"] == _sealed("gh_pat")
    assert "value" not in repr(fetch.call_args.kwargs)


def test_fetch_persists_protected_approval_request_without_grant(capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="GH_PAT",
            protection="protected",
            sealed=_sealed("gh_pat"),
            policy={"allowed_hosts": ["api.github.com"]},
        )

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.github.com/repos/o/r"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "GH_PAT"
    assert requests[0]["delivery"]["mode"] == "fetch"
    assert requests[0]["delivery"]["host"] == "api.github.com"


def test_fetch_header_auth_request_shape(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("SVC_KEY", allow_host=["api.example.com"], auth_header="X-Api-Key")
    fetch = Mock(return_value={"status": 204, "headers": {}, "body": ""})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    assert cli.cmd_vault_fetch(_ns(auth="SVC_KEY", url="https://api.example.com/v1/thing")) == 0
    request = fetch.call_args.args[2]
    assert request["inject"] == {"type": "header", "name": "X-Api-Key"}


def test_fetch_query_auth_request_shape(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("QUERY_KEY", allow_host=["api.example.com"], auth_query="api_key")
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    assert cli.cmd_vault_fetch(_ns(auth="QUERY_KEY", url="https://api.example.com/v1/thing")) == 0
    request = fetch.call_args.args[2]
    assert request["inject"] == {"type": "query", "name": "api_key"}


def test_fetch_post_body_and_headers_pass_to_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("POST_KEY", allow_host=["api.example.com"])
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

    _create_standard_secret("OUT_KEY", allow_host=["api.example.com"])
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

    _create_standard_secret("BOUND_KEY", allow_host=["api.example.com"])
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

    _create_standard_secret("UNBOUND_KEY")
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

    _create_standard_secret("GH_PAT", allow_host=["api.example.com"])
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

    _create_standard_secret("GH_PAT", allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url="https://api.example.com/x", header=["Host: evil.example.com"]))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "forbidden_header"
    fetch.assert_not_called()


def test_fetch_rejects_stored_host_auth_policy_before_avault(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("HOST_AUTH_KEY", allow_host=["api.example.com"], auth_header="Host")
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="HOST_AUTH_KEY", url="https://api.example.com/x"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "forbidden_header"
    fetch.assert_not_called()


def test_fetch_output_unwritable_is_preflighted_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("GH_PAT", allow_host=["api.example.com"])
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

    _create_standard_secret("GH_PAT", allow_host=["api.example.com"])
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


def test_fetch_returns_response_even_if_one_shot_cleanup_fails(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _set_always_ask_grant("ASK_KEY", allow_host=["api.example.com"])
    fetch = Mock(return_value={"status": 200, "headers": {}, "body": "ok"})
    cleanup = Mock(side_effect=RuntimeError("db locked"))
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)
    monkeypatch.setattr(api, "consume_one_shot_grants", cleanup)

    code = cli.cmd_vault_fetch(_ns(auth="ASK_KEY", url="https://api.example.com/x", method="POST"))
    captured = capfd.readouterr()

    assert code == 0
    assert captured.out == "ok"
    fetch.assert_called_once()
    cleanup.assert_called_once()


def test_fetch_consumes_one_shot_grant_when_avault_errors_after_possible_egress(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    grant = _set_always_ask_grant("ASK_KEY", allow_host=["api.example.com"])
    fetch = Mock(side_effect=api.AvaultError("timed out after sending request"))
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="ASK_KEY", url="https://api.example.com/x", method="POST"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "request_failed"
    fetch.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(
            vault_service.vault_grants.select().where(vault_service.vault_grants.c.id == grant["id"])
        ).mappings().one()["status"]
    assert status == "expired"


def test_fetch_releases_one_shot_grant_when_avault_fails_before_handoff(capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    grant = _set_always_ask_grant("ASK_KEY", allow_host=["api.example.com"])
    fetch = Mock(side_effect=api.AvaultPreHandoffError("avault is required for vault fetch"))
    monkeypatch.setattr(api, "avault_deliver_fetch", fetch)

    code = cli.cmd_vault_fetch(_ns(auth="ASK_KEY", url="https://api.example.com/x", method="POST"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "request_failed"
    fetch.assert_called_once()
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(
            vault_service.vault_grants.select().where(vault_service.vault_grants.c.id == grant["id"])
        ).mappings().one()["status"]
    assert status == "active"


def test_host_allowed_is_case_insensitive():
    assert cli._host_allowed("api.github.com", ["API.GITHUB.COM"]) is True
    assert cli._host_allowed("api.github.com", [".GitHub.com"]) is True
    assert cli._host_allowed("API.GITHUB.COM", ["api.github.com"]) is True
