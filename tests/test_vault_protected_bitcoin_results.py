"""B12 normal HTTP completion, response-loss GET, CLI and callback extraction.

Direct storage success cannot catch a dropped point/context or a second result
source in these consumers. Public never-fund signatures only; no browser custody,
physical UV, service or chain claim follows from these source-path checks.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from storage import vault_service
from storage.models import vault_requests, vault_secrets
from tests.test_vault_bitcoin_ecdsa_context import PUBLIC, _signature
from tests.test_vault_protected_bitcoin import _create, _request
from tests.test_vault_protected_bitcoin import capable_cohort as capable_cohort
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, cli


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("later_change", ["replace", "delete"])
def test_B12_http_post_get_cli_await_and_callback_read_only_frozen_result(capable_cohort, capfd, address_type, later_change):
    from vibe.ui_server import app

    _create()
    initial, context = _request(address_type)
    signature = {"signature": _signature(context)}
    client = app.test_client()
    response = client.post("/api/vault/sign", json={
        "name": "PUBLIC_TEST_BTC", "request_id": initial["id"], "digest": context["digest"],
        "scheme": "ecdsa-secp256k1-der", "signature": signature,
    }, headers=csrf_headers(client))
    assert response.status_code == 200
    completed = response.get_json()
    assert set(completed) == {"ok", "signature", "request"} and completed["signature"] == signature
    with api._vault_engine().begin() as conn:
        row = dict(conn.execute(select(vault_requests).where(vault_requests.c.id == initial["id"])).mappings().one())
        callback = vault_service.resolve_request_callback(row)
        assert callback is not None and callback.session_id == "original-test-session"
        assert f"vibe vault await {initial['id']}" in callback.message
        assert "Do not rerun `vibe vault sign`" in callback.message
        assert signature["signature"] not in callback.message and "parentTransaction" not in callback.message
        assert row["callback_status"] == "pending"
        if later_change == "delete":
            conn.execute(vault_secrets.delete().where(vault_secrets.c.name == "PUBLIC_TEST_BTC"))
        else:
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == "PUBLIC_TEST_BTC").values(
                id="vlt_public_replacement_after_success", nonce="replacement-nonce",
            ))
    # A caller that lost the POST response reads the original request ID once;
    # neither new signing nor current-name identity participates in result reads.
    readback = client.get(f"/api/vault/requests/{initial['id']}")
    assert readback.status_code == 200
    saved = readback.get_json()
    assert saved["result"] == {"type": "signature", "signature": signature}
    assert cli.cmd_vault_await(SimpleNamespace(request_id=initial["id"], wait=0)) == 0
    awaited = json.loads(capfd.readouterr().out)
    assert awaited["kind"] == "vault_request_result" and awaited["request_id"] == initial["id"]
    assert awaited["result"] == saved["result"]
    for request in (completed["request"], saved["request"], awaited["request"]):
        delivery = request["delivery"]
        frozen = delivery["operation_context"]["release"]
        assert request["id"] == initial["id"] and request["status"] == "approved"
        assert frozen["publicKey"] == frozen["signingKey"]["public_key"] == delivery["signing_key"]["public_key"] == PUBLIC.hex()
        assert frozen["signingKey"] == initial["delivery"]["signing_key"]
        assert frozen["signingContext"] == delivery["signing_context"] == context
        assert frozen["signingContext"]["addressType"] == address_type
        assert delivery["signature"] == signature
