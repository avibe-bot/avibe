"""CLI tests for ``vibe vault export`` / ``inject`` with avault-backed delivery."""

from __future__ import annotations

import argparse
import json

import pytest

from storage import vault_service
from storage.models import vault_grants
from storage.vault_crypto import Sealed
from sqlalchemy import select
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


def _use_count(name: str) -> int:
    with cli._open_vault_engine().connect() as conn:
        return vault_service.get_secret_meta(conn, name)["use_count"]


def _create_standard_secret(
    name: str,
    *,
    sealed: Sealed | None = None,
    default_field: str | None = None,
    fields: list[dict] | None = None,
    tags: list[str] | None = None,
    policy: dict | None = None,
):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name=name,
            sealed=sealed or _sealed(name.lower()),
            default_field=default_field,
            fields=fields,
            tags=tags,
            policy=policy,
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


def _set_protected_grant(name: str, *, session_id: str | None = None) -> dict:
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name=name, protection="protected", sealed=_sealed(name.lower()))
        req = vault_service.create_access_request(
            conn,
            name,
            purpose="inject",
            requester={"source": "cli", "session_id": session_id} if session_id else {"source": "cli"},
            delivery={"session_id": session_id, "mode": "inject"} if session_id else {"mode": "inject"},
        )
        return _grant_from_request(conn, req, session_id=session_id)


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

    _create_standard_secret("A_KEY", sealed=_sealed("a"))
    _create_standard_secret("B_KEY", sealed=_sealed("b"))
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
            {"name": "A_KEY", "key": "A_KEY", "envelope": _sealed("a"), "tier": "standard"},
            {"name": "B_KEY", "key": "B_KEY", "envelope": _sealed("b"), "tier": "standard"},
        ],
    )
    assert _use_count("A_KEY") == 1
    assert _use_count("B_KEY") == 1


def test_inject_canonicalizes_default_alias_and_maps_extra_field_key(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret(
        "LOGIN",
        sealed=_sealed("password"),
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)
    out = tmp_path / "login.env"

    assert cli.cmd_vault_inject(
        _ns(keys="LOGIN.password,LOGIN.username", out=str(out), format="dotenv")
    ) == 0
    capfd.readouterr()

    inject.assert_called_once_with(
        str(out),
        "dotenv",
        [
            {
                "name": "LOGIN",
                "key": "LOGIN_PASSWORD",
                "envelope": _sealed("password"),
                "tier": "standard",
            },
            {
                "name": "LOGIN.username",
                "key": "LOGIN_USERNAME",
                "envelope": _sealed("username"),
                "tier": "standard",
            },
        ],
    )


@pytest.mark.parametrize("selector", [{"tag": ["deploy"]}, {"skill": ["release"]}])
def test_inject_accepts_tag_and_skill_selectors_for_all_fields(tmp_path, capfd, monkeypatch, selector):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret(
        "DEPLOY_LOGIN",
        sealed=_sealed("password"),
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
        tags=["deploy", "skill:release"],
    )
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)
    out = tmp_path / "deploy.env"

    assert cli.cmd_vault_inject(
        _ns(**selector, out=str(out), format="dotenv")
    ) == 0
    capfd.readouterr()

    assert inject.call_args.args[2] == [
        {
            "name": "DEPLOY_LOGIN",
            "key": "DEPLOY_LOGIN",
            "envelope": _sealed("password"),
            "tier": "standard",
        },
        {
            "name": "DEPLOY_LOGIN.username",
            "key": "DEPLOY_LOGIN_USERNAME",
            "envelope": _sealed("username"),
            "tier": "standard",
        },
    ]


def test_inject_rejects_selector_record_conflict_before_avault(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret(
        "LOGIN",
        default_field="password",
        fields=[{"name": "username", "sealed": _sealed("username")}],
    )
    inject = Mock()
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    code = cli.cmd_vault_inject(
        _ns(
            env=["A=LOGIN", "B=LOGIN.password"],
            out=str(tmp_path / "conflict.env"),
            format="dotenv",
        )
    )
    payload = json.loads(capfd.readouterr().err)

    assert code == 1
    assert payload["code"] == "conflicting_env_alias"
    inject.assert_not_called()


@pytest.mark.parametrize("protection", ["standard", "protected"])
@pytest.mark.parametrize(
    "selector",
    [
        {"keys": "ASK_LOGIN.password,ASK_LOGIN.username"},
        {"env": ["ASK_LOGIN.password", "ASK_LOGIN.username"]},
    ],
)
def test_inject_one_shot_field_pair_records_one_secret_delivery(
    tmp_path, capfd, monkeypatch, protection, selector
):
    from unittest.mock import Mock

    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="ASK_LOGIN",
            protection=protection,
            sealed=_sealed("password"),
            default_field="password",
            fields=[{"name": "username", "sealed": _sealed("username")}],
            policy={"always_ask": True},
        )
        request = vault_service.create_access_request(
            conn,
            "ASK_LOGIN",
            purpose="inject",
            requester={"source": "cli"},
            delivery={"mode": "inject"},
        )
        grant = _grant_from_request(conn, request)

    inject = Mock(return_value=None)
    monkeypatch.setattr(
        api,
        "avault_agent_deliver_inject" if protection == "protected" else "avault_deliver_inject",
        inject,
    )
    out = tmp_path / "ask-login.env"

    assert cli.cmd_vault_inject(
        _ns(**selector, out=str(out), format="dotenv")
    ) == 0
    capfd.readouterr()

    inject.assert_called_once()
    records = inject.call_args.kwargs["secrets"] if protection == "protected" else inject.call_args.args[2]
    assert records == [
        {
            "name": "ASK_LOGIN",
            "key": "ASK_LOGIN_PASSWORD",
            "envelope": _sealed("password"),
            "tier": protection,
        },
        {
            "name": "ASK_LOGIN.username",
            "key": "ASK_LOGIN_USERNAME",
            "envelope": _sealed("username"),
            "tier": protection,
        },
    ]
    with cli._open_vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])
        ).scalar_one()
        meta = vault_service.get_secret_meta(conn, "ASK_LOGIN")
        audits = vault_service.list_audit(conn, secret_name="ASK_LOGIN")
    assert status == "expired"
    assert meta["use_count"] == 1
    delivered = [row for row in audits if row["event"] == "delivered"]
    assert json.loads(delivered[-1]["delivery"])["fields"] == ["password", "username"]


@pytest.mark.parametrize("protection", ["standard", "protected"])
def test_inject_tag_approval_covers_secret_members_and_delivers_all_fields(
    tmp_path, capfd, monkeypatch, protection
):
    from unittest.mock import Mock

    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="LOGIN",
            protection=protection,
            sealed=_sealed("password"),
            default_field="password",
            fields=[{"name": "username", "sealed": _sealed("username")}],
            tags=["deploy"],
            policy={"always_ask": True} if protection == "standard" else {},
        )
        vault_service.create_secret(
            conn,
            name="OTHER",
            protection=protection,
            sealed=_sealed("other"),
            tags=["deploy"],
        )
    inject = Mock(return_value=None)
    monkeypatch.setattr(
        api,
        "avault_agent_deliver_inject" if protection == "protected" else "avault_deliver_inject",
        inject,
    )
    args = _ns(tag=["deploy"], out=str(tmp_path / "deploy.env"), format="dotenv")

    assert cli.cmd_vault_inject(args) == 1
    assert json.loads(capfd.readouterr().err)["code"] == "approval_required"
    inject.assert_not_called()
    with cli._open_vault_engine().begin() as conn:
        requests = vault_service.list_requests(conn, status="pending")
        assert len(requests) == 1
        request = requests[0]
        assert request["card"]["grant_options"][0]["member_snapshot"] == (
            ["LOGIN", "OTHER"] if protection == "protected" else ["LOGIN"]
        )
        grant = _grant_from_request(conn, request)

    assert cli.cmd_vault_inject(args) == 0
    capfd.readouterr()
    records = inject.call_args.kwargs["secrets"] if protection == "protected" else inject.call_args.args[2]
    assert [(item["name"], item["key"], item["tier"]) for item in records] == [
        ("LOGIN", "LOGIN", protection),
        ("LOGIN.username", "LOGIN_USERNAME", protection),
        ("OTHER", "OTHER", protection),
    ]
    if protection == "protected":
        inject.side_effect = api.AvaultError("grant is missing or expired")
        assert cli.cmd_vault_inject(args) == 1
        assert json.loads(capfd.readouterr().err)["code"] == "approval_required"
        with cli._open_vault_engine().connect() as conn:
            requests = vault_service.list_requests(conn, status="pending")
            status = conn.execute(
                select(vault_grants.c.status).where(vault_grants.c.id == grant["id"])
            ).scalar_one()
        assert status == "expired"
        assert len(requests) == 1
        assert requests[0]["card"]["grant_options"][0]["member_snapshot"] == ["LOGIN", "OTHER"]
        assert requests[0]["delivery"]["protected_secret_names"] == ["LOGIN", "OTHER"]


def test_inject_rejects_keypair_before_avault_delivery(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ETH_KEY", sealed=_sealed("eth"), kind="keypair", signer_kind="local")
    inject = Mock()
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    code = cli.cmd_vault_inject(_ns(keys="ETH_KEY", out=str(tmp_path / "secrets.env"), format="dotenv"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "keypair_not_value_deliverable"
    inject.assert_not_called()


def test_inject_uses_agent_delivery_for_protected_grant(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    grant = _set_protected_grant("PROTECTED_KEY")
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_agent_deliver_inject", inject)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock())
    out = tmp_path / "secrets.env"

    assert cli.cmd_vault_inject(_ns(keys="PROTECTED_KEY", out=str(out), format="dotenv")) == 0

    inject.assert_called_once_with(
        grant_id=grant["id"],
        path=str(out),
        fmt="dotenv",
        secrets=[
            {
                "name": "PROTECTED_KEY",
                "key": "PROTECTED_KEY",
                "envelope": _sealed("protected_key"),
                "tier": "protected",
            }
        ],
    )
    assert "value" not in repr(inject.call_args.kwargs)


def test_inject_resolves_protected_relative_output_in_caller_cwd(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    monkeypatch.chdir(tmp_path)
    grant = _set_protected_grant("PROTECTED_KEY")
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_agent_deliver_inject", inject)
    monkeypatch.setattr(api, "avault_deliver_inject", Mock())
    expected = str(tmp_path / ".env")

    assert cli.cmd_vault_inject(_ns(keys="PROTECTED_KEY", out=".env", format="dotenv")) == 0
    payload = json.loads(capfd.readouterr().out)

    inject.assert_called_once_with(
        grant_id=grant["id"],
        path=expected,
        fmt="dotenv",
        secrets=[
            {
                "name": "PROTECTED_KEY",
                "key": "PROTECTED_KEY",
                "envelope": _sealed("protected_key"),
                "tier": "protected",
            }
        ],
    )
    assert payload["path"] == expected


def test_inject_persists_protected_approval_request_without_grant(tmp_path, capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected_key"))
    out = tmp_path / "secrets.env"

    code = cli.cmd_vault_inject(_ns(keys="PROTECTED_KEY", out=str(out), format="dotenv"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "approval_required"
    with cli._open_vault_engine().connect() as conn:
        requests = vault_service.list_requests(conn, status="pending")
    assert len(requests) == 1
    assert requests[0]["secret_name"] == "PROTECTED_KEY"
    assert requests[0]["delivery"]["mode"] == "inject"
    assert requests[0]["delivery"]["path"] == str(out)


def test_inject_rejects_missing_later_secret_before_creating_approval(tmp_path, capfd):
    with cli._open_vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PROTECTED_KEY", protection="protected", sealed=_sealed("protected_key"))
    out = tmp_path / "secrets.env"

    code = cli.cmd_vault_inject(_ns(keys="PROTECTED_KEY,MISSING_KEY", out=str(out), format="dotenv"))
    captured = capfd.readouterr()

    assert code == 1
    assert json.loads(captured.err)["code"] == "secret_not_found"
    with cli._open_vault_engine().connect() as conn:
        assert vault_service.list_requests(conn, status="pending") == []


@pytest.mark.parametrize("fmt", ["yaml", "toml"])
def test_inject_rejects_unavailable_formats_before_avault(tmp_path, capfd, monkeypatch, fmt):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("A_KEY")
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

    _create_standard_secret("K")
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    code = cli.cmd_vault_inject(_ns(keys="K", out=str(tmp_path / "o"), format="xml"))
    assert code == 1
    assert json.loads(capfd.readouterr().err)["code"] == "invalid_format"
    inject.assert_not_called()


def test_inject_dedupes_repeated_keys(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("A_KEY", sealed=_sealed("a"))
    inject = Mock(return_value=None)
    monkeypatch.setattr(api, "avault_deliver_inject", inject)

    out = tmp_path / "dup.env"
    assert cli.cmd_vault_inject(_ns(keys="A_KEY,A_KEY", out=str(out), format="dotenv")) == 0
    assert json.loads(capfd.readouterr().out)["keys"] == ["A_KEY"]
    inject.assert_called_once_with(
        str(out),
        "dotenv",
        [{"name": "A_KEY", "key": "A_KEY", "envelope": _sealed("a"), "tier": "standard"}],
    )
    assert _use_count("A_KEY") == 1


def test_inject_payload_does_not_leak_value(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("SECRET_KEY")
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(return_value=None))

    cli.cmd_vault_inject(_ns(keys="SECRET_KEY", out=str(tmp_path / "s.env"), format="dotenv"))
    payload_out = capfd.readouterr().out
    assert "topsecret-INJECT" not in payload_out


def test_inject_does_not_record_delivery_when_avault_fails(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("A_KEY")
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(side_effect=api.AvaultError("boom")))

    code = cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(tmp_path / "fail.env"), format="dotenv"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "avault_failed"
    assert _use_count("A_KEY") == 0


def test_inject_succeeds_even_if_audit_fails(tmp_path, capfd, monkeypatch):
    from unittest.mock import Mock

    from vibe import api

    _create_standard_secret("A_KEY")
    monkeypatch.setattr(api, "avault_deliver_inject", Mock(return_value=None))

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(vault_service, "record_deliveries", _boom)
    assert cli.cmd_vault_inject(_ns(keys="A_KEY", out=str(tmp_path / "ok.env"), format="dotenv")) == 0
