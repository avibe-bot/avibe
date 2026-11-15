"""HD custody consumer contracts, at real storage/API/CLI boundaries.

A fixed public-only avault master fixture supplies keys/signatures independently
of this implementation. Existing single-key tests pin the legacy wire behavior;
these cases protect the new account routing, per-use binding and atomic import.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import sqlite3
import subprocess
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import resource_access_service, vault_hd, vault_service
from storage.models import resource_access_policies, vault_audit, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, cli

FIXTURE = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-seed-wallet.json").read_text())
BLIND_BOX = {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "test-enc", "ct": "test-ct"}
CREATE = {
    "name": "HD_TEST_WALLET", "kind": "keypair", "signer_kind": "hd-seed", "protection": "standard",
    "keypair": {"type": "hd-seed", "source": "import", "format": "raw-seed"}, "blind_box": BLIND_BOX,
}
DIGEST = "01" * 32
MESSAGE = "deadbeef"


@pytest.fixture
def custody(monkeypatch):
    fake = SimpleNamespace(version="0.1.11", calls=[], reply=copy.deepcopy(FIXTURE["import_response"]), hook=None)

    def run(args, *, stdin=None, **_kwargs):
        fake.calls.append((list(args), json.loads(stdin) if stdin else None))
        if fake.hook:
            fake.hook()
        return SimpleNamespace(returncode=0, stdout=json.dumps(fake.reply), stderr="")

    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": fake.version})
    monkeypatch.setattr(api, "_run_avault", run)
    monkeypatch.setattr(api, "_notify_vault_request_created", Mock())
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", Mock())
    return fake


def create(custody):
    result = api.create_vault_secret(copy.deepcopy(CREATE))["secret"]
    custody.calls.clear()
    return result


def rows(table):
    with api._vault_engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def meta():
    with api._vault_engine().connect() as conn:
        return vault_service.get_secret_meta(conn, "HD_TEST_WALLET")


def sign_input(chain="evm", account=0):
    return {"name": "HD_TEST_WALLET", "chain": chain, "account": account, **(
        {"message_hex": MESSAGE} if chain == "solana" else {"digest": DIGEST}
    )}


def signature(chain, account):
    return next(item["signature"] for item in FIXTURE["signatures"] if item["chain"] == chain and item["account"] == account)


def test_create_stores_one_typed_row_with_avault_public_keys_only(custody):
    result = api.create_vault_secret(copy.deepcopy(CREATE))["secret"]
    assert custody.calls == [(
        ["keypair", "import", "--name", "HD_TEST_WALLET", "--type", "hd-seed", "--format", "raw-seed", "--blind-box"],
        BLIND_BOX,
    )]
    row, = rows(vault_secrets)
    assert (row["kind"], row["signer_kind"], row["protection"]) == ("keypair", "hd-seed", "standard")
    assert {key: row[key] for key in ("ciphertext", "nonce", "wrap_meta")} == FIXTURE["import_response"]["envelope"]
    accounts = json.loads(row["public_meta"])["hd_accounts"]
    assert len(accounts) == 20
    assert [item["account"] for item in accounts if item["enabled"]] == [0]
    assert result["account_limit"] == 20
    assert result["per_use_sign"] and not result["access_grantable"]
    assert result["accounts"][0]["name"] is None
    assert result["accounts"][0]["last_signer"] is None
    assert {key: result["accounts"][0][key] for key in ("account", "eth", "btc_taproot", "solana")} == FIXTURE["expected_addresses"][0]
    assert "public_key" not in json.dumps(result)
    assert len(rows(vault_audit)) == 1


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
def test_create_policy_is_protected_before_audit_fulfilment_and_publication(custody, monkeypatch, chain):
    observed = []
    original_audit = vault_service.audit
    original_fulfil = vault_service.fulfill_pending_provision_requests_for_secret

    def assert_stored(conn, phase):
        row = conn.execute(select(vault_secrets)).mappings().one()
        accounts = json.loads(row["public_meta"])["hd_accounts"]
        assert accounts[0]["always_ask"] is True
        assert all(item["always_ask"] is False for item in accounts[1:])
        observed.append(phase)

    def audit(conn, event, **kwargs):
        if event == "created":
            assert_stored(conn, "audit")
        return original_audit(conn, event, **kwargs)

    def fulfil(conn, name, **kwargs):
        assert_stored(conn, "fulfil")
        return original_fulfil(conn, name, **kwargs)

    def publish(**_kwargs):
        if "publish" in observed:
            return
        observed.append("publish")
        assert meta()["accounts"][0]["always_ask"] is True
        # Reproduce the earliest post-commit consumer, before create returns.
        custody.reply = signature(chain, 0)
        pending = api.vault_sign(sign_input(chain))
        assert pending["code"] == "approval_required"
        assert pending["request"]["delivery"]["derivation"] == {"chain": chain, "account": 0}

    monkeypatch.setattr(vault_service, "audit", audit)
    monkeypatch.setattr(vault_service, "fulfill_pending_provision_requests_for_secret", fulfil)
    monkeypatch.setattr(api, "_publish_vaults_updated", publish)
    result = api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": True,
                                      "expected_account0": expected_account0()})
    assert result["secret"]["accounts"][0]["always_ask"] is True
    assert observed == ["audit", "fulfil", "publish"]
    assert len(custody.calls) == 1 and custody.calls[0][0][0] == "keypair"
    assert len(rows(vault_requests)) == 1
    assert not any(row["event"] == "signed" for row in rows(vault_audit))
    assert meta()["use_count"] == 0


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_create_policy_rejects_non_boolean_before_custody(custody, value):
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": value})
    assert error.value.code == "invalid_hd_wallet"
    assert custody.calls == []
    assert rows(vault_secrets) == rows(vault_audit) == []


@pytest.mark.parametrize("kind,protection", [("static", "standard"), ("static", "protected"),
                                             ("keypair", "standard"), ("keypair", "protected")])
@pytest.mark.parametrize("value", [False, True])
def test_create_policy_non_hd_rejects_even_false(custody, kind, protection, value):
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({"name": "ORDINARY", "kind": kind, "protection": protection,
                                 "blind_box": BLIND_BOX, "account0_always_ask": value})
    assert error.value.code == "invalid_hd_wallet"
    assert custody.calls == []
    assert rows(vault_secrets) == rows(vault_audit) == []


@pytest.mark.parametrize("extra", [{}, {"account0_always_ask": False}])
def test_create_policy_omitted_or_false_preserves_direct_signing(custody, extra):
    result = api.create_vault_secret({**copy.deepcopy(CREATE), **extra})
    assert result["secret"]["accounts"][0]["always_ask"] is False
    custody.calls.clear()
    custody.reply = signature("evm", 0)
    assert api.vault_sign(sign_input())["signature"] == custody.reply
    assert len(custody.calls) == 1
    assert rows(vault_requests) == []


def test_create_policy_creator_may_raise_initial_policy_but_not_edit_existing(custody):
    editor = resource_access_service.ResourceUserContext(instance_role="editor", subject="creator", is_remote=True)
    assert not editor.can_manage_instance
    result = api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": True}, user_context=editor)
    assert result["secret"]["accounts"][0]["always_ask"] is True
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_hd_account(CREATE["name"], {"name": "Changed"}, account=0, user_context=editor)
    assert error.value.status == 403
    assert meta()["accounts"][0]["always_ask"] is True


def test_create_policy_viewer_is_refused_before_custody(custody, monkeypatch):
    publish = Mock()
    monkeypatch.setattr(api, "_publish_vaults_updated", publish)
    viewer = resource_access_service.ResourceUserContext(instance_role="viewer")
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": True}, user_context=viewer)
    assert error.value.status == 403
    assert custody.calls == []
    assert rows(vault_secrets) == rows(vault_audit) == []
    publish.assert_not_called()


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_create_policy_storage_rejects_non_boolean_without_writes(custody, value):
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError, match="boolean"):
            vault_service.create_secret(conn, name=CREATE["name"], kind="keypair", signer_kind="hd-seed",
                                        sealed=Sealed(**FIXTURE["import_response"]["envelope"]),
                                        account0_always_ask=value)
        assert conn.execute(select(vault_secrets)).first() is None
        assert conn.execute(select(vault_audit)).first() is None


@pytest.mark.parametrize("value", [False, True])
def test_create_policy_storage_rejects_non_hd_without_writes(custody, value):
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError, match="HD seed"):
            vault_service.create_secret(conn, name="ORDINARY", sealed=Sealed("opaque", "opaque", "opaque"),
                                        account0_always_ask=value)
        assert conn.execute(select(vault_secrets)).first() is None


def test_create_policy_fulfilment_failure_rolls_back_and_never_publishes(custody, monkeypatch):
    before_policies = rows(resource_access_policies)
    publish = Mock()
    monkeypatch.setattr(api, "_publish_vaults_updated", publish)
    monkeypatch.setattr(vault_service, "fulfill_pending_provision_requests_for_secret",
                        Mock(side_effect=RuntimeError("synthetic fulfilment failure")))
    with pytest.raises(RuntimeError, match="synthetic fulfilment failure"):
        api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": True})
    assert rows(vault_secrets) == rows(vault_audit) == []
    assert rows(resource_access_policies) == before_policies
    publish.assert_not_called()


def test_create_policy_lost_response_retains_protection(custody, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(api, "_publish_vaults_updated", Mock(side_effect=RuntimeError("synthetic lost response")))
        with pytest.raises(RuntimeError, match="synthetic lost response"):
            api.create_vault_secret({**copy.deepcopy(CREATE), "account0_always_ask": True})
    custody.calls.clear()
    assert meta()["accounts"][0]["always_ask"] is True
    assert api.vault_sign(sign_input())["code"] == "approval_required"
    assert custody.calls == []
    # A retry cannot clear the committed policy by omitting the initial setting.
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert error.value.code == "secret_exists"
    assert meta()["accounts"][0]["always_ask"] is True


def test_create_policy_http_response_is_protected_without_followup_patch(custody):
    from vibe.ui_server import app

    client = app.test_client()
    response = client.post("/api/vault/secrets", json={**copy.deepcopy(CREATE), "account0_always_ask": True},
                           headers=csrf_headers(client))
    assert response.status_code == 200
    assert response.get_json()["secret"]["accounts"][0]["always_ask"] is True
    custody.calls.clear()
    assert api.vault_sign(sign_input())["code"] == "approval_required"
    assert custody.calls == []


@pytest.mark.parametrize("change", [
    {"protection": "protected"}, {"kind": "static"}, {"signer_kind": "local"},
    {"keypair": {"type": "hd-seed", "source": "generate", "format": "raw-seed"}},
    {"keypair": None}, {"blind_box": None}, {"seed": "not-accepted"},
    {"public_meta": {"hd_accounts": []}}, {"public_meta": {"signing_public_key": {}}},
    {"policy": {"always_ask": True}}, {"sealed": {}},
])
def test_import_rejects_fallbacks_and_client_keys_before_custody(custody, change):
    with pytest.raises(api.VaultApiError):
        api.create_vault_secret({**copy.deepcopy(CREATE), **change})
    assert custody.calls == []
    assert rows(vault_secrets) == []
    assert rows(vault_audit) == []


@pytest.mark.parametrize("case", ["short", "duplicate", "wrong-key", "wrong-type", "wrong-envelope", "wrong-ciphertext"])
def test_malformed_custody_response_leaves_no_partial_row(custody, case):
    if case == "short":
        custody.reply["accounts"].pop()
    elif case == "duplicate":
        custody.reply["accounts"][1]["account"] = 0
    elif case == "wrong-key":
        custody.reply["accounts"][19]["bitcoin"] = "00" * 33
    elif case == "wrong-type":
        custody.reply["type"] = "secp256k1"
    elif case == "wrong-envelope":
        wrap = json.loads(custody.reply["envelope"]["wrap_meta"])
        wrap["scheme"] = "machine-aesgcm-v1"
        custody.reply["envelope"]["wrap_meta"] = json.dumps(wrap)
    else:
        custody.reply["envelope"]["ciphertext"] = "AA=="
    with pytest.raises(api.VaultApiError, match="malformed"):
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert rows(vault_secrets) == []
    assert rows(vault_audit) == []


def expected_account0():
    return {field: FIXTURE["expected_addresses"][0][field] for field in ("eth", "btc_taproot", "solana")}


def test_expected_account0_matches_without_replacing_custody_metadata(custody):
    result = api.create_vault_secret({**copy.deepcopy(CREATE), "expected_account0": expected_account0()})["secret"]
    assert {field: result["accounts"][0][field] for field in expected_account0()} == expected_account0()
    row, = rows(vault_secrets)
    assert "expected_account0" not in json.loads(row["public_meta"])
    assert json.loads(row["public_meta"])["hd_accounts"][0]["evm"] == FIXTURE["import_response"]["accounts"][0]["evm"]
    assert len(rows(vault_audit)) == 1


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
def test_wrong_custody_account0_fails_before_persistence_and_retry_can_succeed(custody, chain):
    before_policies = rows(resource_access_policies)
    custody.reply["accounts"][0][chain] = custody.reply["accounts"][1][chain]
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "expected_account0": expected_account0()})
    assert error.value.code == "hd_address_check_failed" and error.value.status == 409
    assert len(custody.calls) == 1
    assert rows(vault_secrets) == [] and rows(vault_audit) == []
    assert rows(resource_access_policies) == before_policies
    custody.reply = copy.deepcopy(FIXTURE["import_response"])
    result = api.create_vault_secret({**copy.deepcopy(CREATE), "expected_account0": expected_account0()})["secret"]
    assert {field: result["accounts"][0][field] for field in expected_account0()} == expected_account0()
    assert len(rows(vault_secrets)) == len(rows(vault_audit)) == 1


@pytest.mark.parametrize("expected", [
    None, {}, [], {**expected_account0(), "account": 0},
    {field: value for field, value in expected_account0().items() if field != "solana"},
    {**expected_account0(), "eth": 1}, {**expected_account0(), "eth": "0x" + "a" * 100},
    {**expected_account0(), "btc_taproot": "tb" + expected_account0()["btc_taproot"][2:]},
    {**expected_account0(), "solana": "0" * 44},
])
def test_malformed_expected_account0_is_rejected_before_custody(custody, expected):
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "expected_account0": expected})
    assert error.value.code == "invalid_hd_wallet"
    assert custody.calls == [] and rows(vault_secrets) == [] and rows(vault_audit) == []


@pytest.mark.parametrize("kind,protection", [("keypair", "standard"), ("keypair", "protected"), ("static", "standard")])
def test_non_hd_create_rejects_expected_account0_before_custody(custody, kind, protection):
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({"name": "ORDINARY", "kind": kind, "protection": protection,
                                 "blind_box": BLIND_BOX, "expected_account0": expected_account0()})
    assert error.value.code == "invalid_hd_wallet"
    assert custody.calls == [] and rows(vault_secrets) == [] and rows(vault_audit) == []


def test_http_hd_address_mismatch_never_leaves_a_wallet_for_another_client(custody):
    from vibe.ui_server import app

    client = app.test_client()
    headers = csrf_headers(client)
    expected = expected_account0()
    expected["eth"] = FIXTURE["expected_addresses"][1]["eth"]
    response = client.post("/api/vault/secrets", json={**copy.deepcopy(CREATE), "expected_account0": expected}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["code"] == "hd_address_check_failed"
    assert client.get("/api/vault/secrets").get_json()["secrets"] == []
    assert rows(vault_audit) == []


def test_duplicate_create_never_calls_custody_again(custody):
    create(custody)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert error.value.code == "secret_exists"
    assert custody.calls == []
    assert len(rows(vault_secrets)) == 1


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_interrupted_hd_import_before_persistence_leaves_no_wallet(custody, failure):
    """A custody interruption must not persist a stub that a retry cannot replace."""
    def interrupt():
        raise failure("test-only custody interruption")

    custody.hook = interrupt
    with pytest.raises(failure, match="test-only custody interruption"):
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert rows(vault_secrets) == []
    assert rows(vault_audit) == []
    custody.hook = None
    assert api.create_vault_secret(copy.deepcopy(CREATE))["secret"]["account_limit"] == 20
    assert len(rows(vault_secrets)) == 1


def test_hd_create_storage_failure_rolls_back_row_policy_and_audit(custody, monkeypatch):
    """Unlike malformed custody output, this fails after INSERT in the real transaction."""
    original_audit = vault_service.audit
    before_policies = rows(resource_access_policies)

    def fail_after_audit(conn, *args, **kwargs):
        original_audit(conn, *args, **kwargs)
        assert conn.execute(select(vault_secrets.c.id)).first() is not None
        raise RuntimeError("test-only persistence failure")

    monkeypatch.setattr(vault_service, "audit", fail_after_audit)
    with pytest.raises(RuntimeError, match="test-only persistence failure"):
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert len(custody.calls) == 1
    assert rows(vault_secrets) == []
    assert rows(vault_audit) == []
    assert rows(resource_access_policies) == before_policies
    monkeypatch.setattr(vault_service, "audit", original_audit)
    api.create_vault_secret(copy.deepcopy(CREATE))
    assert len(rows(vault_secrets)) == len(rows(vault_audit)) == 1


def test_hd_create_lost_response_retry_preserves_committed_wallet(custody, monkeypatch):
    """Failure after commit is an existing wallet, not permission to overwrite its seed."""
    with monkeypatch.context() as patch:
        patch.setattr(api, "_publish_vaults_updated", Mock(side_effect=RuntimeError("test-only lost response")))
        with pytest.raises(RuntimeError, match="test-only lost response"):
            api.create_vault_secret(copy.deepcopy(CREATE))
    committed, = rows(vault_secrets)
    custody.calls.clear()
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(copy.deepcopy(CREATE))
    assert error.value.code == "secret_exists"
    assert custody.calls == []
    assert rows(vault_secrets) == [committed]
    assert len(rows(vault_audit)) == 1


def test_hd_simultaneous_imports_have_one_complete_winner(custody, monkeypatch):
    """Two different custody responses race; stored envelope/keys must belong to one winner."""
    barrier = Barrier(2)
    lock = Lock()
    replies = [copy.deepcopy(FIXTURE["import_response"]) for _ in range(2)]
    # The storage owner treats custody envelopes as opaque. Distinct valid-shape
    # ciphertext and account keys let this test detect a mixed or overwritten row.
    replies[1]["envelope"]["ciphertext"] = base64.b64encode(b"\x01" * 80).decode()
    for index, account in enumerate(replies[1]["accounts"]):
        for chain in vault_hd.SCHEMES:
            account[chain] = replies[0]["accounts"][(index + 1) % 20][chain]

    def racing_custody(args, *, stdin=None, **_kwargs):
        with lock:
            index = len(custody.calls)
            custody.calls.append((list(args), json.loads(stdin)))
        barrier.wait(timeout=10)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies[index]), stderr="")

    monkeypatch.setattr(api, "_run_avault", racing_custody)

    def attempt(_):
        try:
            return api.create_vault_secret(copy.deepcopy(CREATE))
        except api.VaultApiError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(isinstance(result, dict) and result["ok"] for result in results) == 1
    assert "secret_exists" in results
    assert len(custody.calls) == 2
    row, = rows(vault_secrets)
    stored_envelope = {key: row[key] for key in ("ciphertext", "nonce", "wrap_meta")}
    stored_accounts = json.loads(row["public_meta"])["hd_accounts"]
    assert len(stored_accounts) == 20
    stored_keys = [{key: account[key] for key in ("account", *vault_hd.SCHEMES)} for account in stored_accounts]
    assert sum(stored_envelope == reply["envelope"] and stored_keys == reply["accounts"] for reply in replies) == 1
    winner, = [result for result in results if isinstance(result, dict)]
    assert winner["secret"]["accounts"] == meta()["accounts"]
    assert len(rows(vault_audit)) == 1


def test_hd_sqlite_backup_restore_preserves_full_row_and_account_state(custody, monkeypatch, tmp_path):
    """SQLite's backup API includes committed WAL state; raw database copying is not assumed safe.

    This protects storage bytes, not machine-key portability. Real custody
    restoration/signing is a separate opt-in acceptance check.
    """
    from storage.db import create_sqlite_engine

    create(custody)
    for _ in range(19):
        api.update_vault_hd_account("HD_TEST_WALLET", {})
    api.update_vault_hd_account("HD_TEST_WALLET", {"name": "Last account", "always_ask": True}, account=19)
    expected_rows = rows(vault_secrets)
    expected_meta = meta()
    engine = api._vault_engine()
    restored_path = tmp_path / "restored.sqlite"
    with sqlite3.connect(str(engine.url.database)) as source, sqlite3.connect(restored_path) as target:
        source.backup(target)
    restored = create_sqlite_engine(restored_path)
    try:
        monkeypatch.setattr(api, "_vault_engine", lambda: restored)
        assert rows(vault_secrets) == expected_rows
        assert meta() == expected_meta
        assert len(meta()["accounts"]) == 20
        assert meta()["accounts"][19]["always_ask"] is True
    finally:
        restored.dispose()


@pytest.mark.parametrize("entrypoint", ["create", "inline", "request", "cli"])
@pytest.mark.parametrize("version", ["0.1.10", None])
def test_hd_version_floor_fails_before_custody_or_pending_request(custody, capfd, entrypoint, version):
    if entrypoint != "create":
        create(custody)
        api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True}, account=0)
    custody.version = version
    if entrypoint == "cli":
        args = cli.build_parser().parse_args(["vault", "sign", "HD_TEST_WALLET", "--chain", "evm", "--digest", DIGEST])
        assert cli.cmd_vault_sign(args) == 1
        assert json.loads(capfd.readouterr().err)["code"] == "avault_upgrade_required"
    else:
        fn = {"create": api.create_vault_secret, "inline": api.vault_sign, "request": api.request_vault_sign}[entrypoint]
        with pytest.raises(api.VaultApiError) as error:
            fn(copy.deepcopy(CREATE) if entrypoint == "create" else sign_input())
        assert error.value.code == "avault_upgrade_required"
        assert "0.1.11" in str(error.value)
    assert custody.calls == []
    assert rows(vault_requests) == []
    if entrypoint == "create":
        assert rows(vault_secrets) == []


def test_account_enable_edit_cap_and_search_are_public_metadata_only(custody):
    create(custody)
    enabled = api.update_vault_hd_account("HD_TEST_WALLET", {})["secret"]
    assert [item["account"] for item in enabled["accounts"]] == [0, 1]
    expected = FIXTURE["expected_addresses"][1]
    assert {key: enabled["accounts"][1][key] for key in expected} == expected
    api.update_vault_hd_account("HD_TEST_WALLET", {"name": "  Spending  ", "always_ask": True}, account=1)
    assert meta()["accounts"][1]["name"] == "Spending"
    assert meta()["accounts"][1]["always_ask"] is True
    assert meta()["accounts"][0]["always_ask"] is False
    api.update_vault_secret("HD_TEST_WALLET", {"description": "Test wallet"})
    api.update_vault_hd_account("HD_TEST_WALLET", {"name": None}, account=1)
    assert meta()["description"] == "Test wallet"
    assert meta()["accounts"][1]["name"] is None
    with api._vault_engine().connect() as conn:
        assert len(vault_service.list_secrets(conn, query=expected["btc_taproot"])) == 1
    for _ in range(18):
        api.update_vault_hd_account("HD_TEST_WALLET", {})
    assert meta()["accounts"][-1]["account"] == 19
    with pytest.raises(api.VaultApiError, match="20"):
        api.update_vault_hd_account("HD_TEST_WALLET", {})
    assert custody.calls == []


@pytest.mark.parametrize("account,changes", [
    (1, {"name": "Disabled"}), (True, {"always_ask": True}), (20, {"name": "Bad"}),
    (0, {"always_ask": "false"}), (0, {"name": " "}), (0, {"name": "x" * 81}),
    (0, {"evm": "forged"}), (None, {"always_ask": True}), (0, {}),
])
def test_invalid_account_edits_do_not_mutate(custody, account, changes):
    before = create(custody)
    with pytest.raises(api.VaultApiError):
        api.update_vault_hd_account("HD_TEST_WALLET", changes, account=account)
    assert meta() == before
    assert custody.calls == []


def test_account_mutation_requires_management_and_serializes_enables(custody):
    create(custody)
    viewer = resource_access_service.ResourceUserContext(instance_role="viewer")
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_hd_account("HD_TEST_WALLET", {}, user_context=viewer)
    assert error.value.status == 403
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: api.update_vault_hd_account("HD_TEST_WALLET", {}), range(2)))
    assert sorted(len(result["secret"]["accounts"]) for result in results) == [2, 3]
    assert [item["account"] for item in meta()["accounts"]] == [0, 1, 2]
    assert custody.calls == []


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
@pytest.mark.parametrize("account", [0, 1])
@pytest.mark.parametrize("approval", [False, True])
def test_signs_only_selected_account_and_records_original_session(custody, chain, account, approval):
    create(custody)
    api.update_vault_hd_account("HD_TEST_WALLET", {})
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": approval}, account=account)
    custody.reply = signature(chain, account)
    payload = {**sign_input(chain, account), "requester": {"session_id": "session-original"}}
    if approval:
        pending = api.vault_sign(payload)
        assert pending["code"] == "approval_required"
        request = pending["request"]
        assert ("solana_lifetime_hint" in request) == (chain == "solana")
        assert request["delivery"]["derivation"] == {"chain": chain, "account": account}
        assert request["card"]["account"]["account"] == account
        assert request["delivery"]["operation_context"]["release"]["derivation"] == {"chain": chain, "account": account}
        assert custody.calls == []
        payload = {**sign_input(chain, account), "request_id": request["id"], "requester": {"session_id": "approving-browser"}}
    result = api.vault_sign(payload)
    assert result["ok"] and result["signature"] == custody.reply
    assert "signing_public_key" not in result  # Omitted BTC selector and EVM/Solana keep their shipped result wire.
    command, wire = custody.calls[0]
    assert command == ["sign"]
    assert wire == {
        "name": "HD_TEST_WALLET", "key_envelope": FIXTURE["import_response"]["envelope"],
        "scheme": vault_hd.SCHEMES[chain], "derivation": {"chain": chain, "account": account},
        **({"message_hex": MESSAGE} if chain == "solana" else {"digest": DIGEST}),
    }
    account_meta = meta()["accounts"][account]
    assert account_meta["last_signer"]["session_id"] == "session-original"
    assert meta()["accounts"][1 - account]["last_signer"] is None
    signed, = [row for row in rows(vault_audit) if row["event"] == "signed"]
    assert json.loads(signed["delivery"])["derivation"] == {"chain": chain, "account": account}
    assert json.loads(signed["requester"])["session_id"] == "session-original"
    if approval:
        with pytest.raises(api.VaultApiError):
            api.vault_sign(payload)
        assert len(custody.calls) == 1


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
def test_wrong_account_signature_never_delivered_or_audited(custody, chain):
    create(custody)
    custody.reply = signature(chain, 1)
    with pytest.raises(api.VaultApiError):
        api.vault_sign(sign_input(chain, 0))
    assert meta()["use_count"] == 0
    assert meta()["accounts"][0]["last_signer"] is None
    assert not any(row["event"] == "signed" for row in rows(vault_audit))


@pytest.mark.parametrize("change", [
    {"chain": None}, {"chain": "ethereum"}, {"account": 1}, {"account": 20},
    {"account": -1}, {"account": True}, {"account": "0"},
    {"scheme": "schnorr-secp256k1-bip340"}, {"message_hex": MESSAGE},
])
def test_invalid_sign_selection_fails_before_custody_or_request(custody, change):
    create(custody)
    with pytest.raises(api.VaultApiError):
        api.vault_sign({**sign_input(), **change})
    assert custody.calls == []
    assert rows(vault_requests) == []


@pytest.mark.parametrize("mutation", ["account", "chain", "context", "key", "envelope", "identity"])
def test_approval_cannot_switch_its_persisted_target(custody, mutation):
    create(custody)
    api.update_vault_hd_account("HD_TEST_WALLET", {})
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True}, account=0)
    pending = api.vault_sign(sign_input())["request"]
    payload = {**sign_input(), "request_id": pending["id"]}
    if mutation in {"account", "chain", "context"}:
        payload.update({"account": 1} if mutation == "account" else {"chain": "bitcoin"} if mutation == "chain" else {
            "signing_context": {"kind": "evm-transaction", "digestAlgorithm": "keccak256", "chainId": "1", "unsignedTransaction": "0x01", "digest": DIGEST},
        })
    else:
        with api._vault_engine().begin() as conn:
            row = vault_service.require_secret_access(conn, "HD_TEST_WALLET")
            if mutation == "key":
                public = json.loads(row["public_meta"])
                public["hd_accounts"][0]["evm"] = public["hd_accounts"][1]["evm"]
                changes = {"public_meta": json.dumps(public)}
            else:
                changes = {"nonce": "other-envelope"} if mutation == "envelope" else {"id": "replacement-record"}
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == "HD_TEST_WALLET").values(**changes))
    with pytest.raises(api.VaultApiError, match="does not match"):
        api.vault_sign(payload)
    assert custody.calls == []
    assert rows(vault_requests)[0]["status"] == "pending"


def test_failed_or_keychain_interrupted_approval_does_not_change_accounts(custody, monkeypatch):
    create(custody)
    api.update_vault_hd_account("HD_TEST_WALLET", {})
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True}, account=1)
    payload = sign_input("solana", 1)
    request = api.vault_sign(payload)["request"]
    payload["request_id"] = request["id"]
    original = api._run_avault

    def no_authorization(*args, **kwargs):
        raise api.AvaultAuthorizationRequiredBeforeHandoffError("authorization required")

    monkeypatch.setattr(api, "_run_avault", no_authorization)
    with pytest.raises(api.VaultApiError):
        api.vault_sign(payload)
    pending = rows(vault_requests)[0]
    assert pending["status"] == "pending"
    assert json.loads(pending["delivery"])["derivation"] == {"chain": "solana", "account": 1}
    monkeypatch.setattr(api, "_run_avault", original)
    custody.reply = signature("solana", 0)
    with pytest.raises(api.VaultApiError):
        api.vault_sign(payload)
    assert rows(vault_requests)[0]["status"] == "failed"
    assert meta()["accounts"][1]["last_signer"] is None


@pytest.mark.parametrize("approval", [False, True])
def test_record_replacement_during_signing_never_gets_old_signature_or_usage(custody, approval):
    create(custody)
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": approval}, account=0)
    payload = sign_input("solana")
    if approval:
        payload["request_id"] = api.vault_sign(payload)["request"]["id"]
    custody.reply = signature("solana", 0)

    def replace_record():
        with api._vault_engine().begin() as conn:
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == "HD_TEST_WALLET").values(id="replacement-record"))

    custody.hook = replace_record
    with pytest.raises(api.VaultApiError):
        api.vault_sign(payload)
    assert meta()["use_count"] == 0
    assert not any(row["event"] == "signed" for row in rows(vault_audit))


def test_hd_cannot_be_reclassified_rotated_revealed_or_value_delivered(custody):
    create(custody)
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.VaultServiceError):
            vault_service.get_envelope(conn, "HD_TEST_WALLET")
        with pytest.raises(vault_service.InvalidRequestError):
            vault_service.update_secret_classification(conn, "HD_TEST_WALLET", kind="static")
        with pytest.raises(vault_service.InvalidRequestError):
            vault_service.rotate_secret(conn, "HD_TEST_WALLET", Sealed(**FIXTURE["import_response"]["envelope"]))
    with pytest.raises(api.VaultApiError):
        api.create_vault_reveal_context("HD_TEST_WALLET")
    assert custody.calls == []


def test_cli_default_account_list_and_plain_keypair_option_refusal(custody, capfd):
    create(custody)
    custody.reply = signature("solana", 0)
    parser = cli.build_parser()
    assert cli.cmd_vault_sign(parser.parse_args(["vault", "sign", "HD_TEST_WALLET", "--chain", "solana", "--message-hex", MESSAGE])) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["derivation"] == {"chain": "solana", "account": 0}
    assert cli.cmd_vault_list(parser.parse_args(["vault", "list"])) == 0
    listed = json.loads(capfd.readouterr().out)["secrets"][0]
    assert listed["signer_kind"] == "hd-seed"
    assert set(listed["accounts"][0]) == {"account", "name", "eth", "btc_taproot", "solana", "paths", "bitcoin_address_types", "always_ask"}
    assert listed["accounts"][0]["always_ask"] is False
    assert "durable nonce" in listed["solana_signing_note"]
    assert listed["accounts"][0]["bitcoin_address_types"] == ["p2tr"]
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="PLAIN", kind="keypair", sealed=Sealed("ct", "nonce", "meta"))
    before = len(custody.calls)
    for options in (["--chain", "evm"], ["--account", "0"]):
        assert cli.cmd_vault_sign(parser.parse_args(["vault", "sign", "PLAIN", "--digest", DIGEST, *options])) == 1
        assert json.loads(capfd.readouterr().err)["code"] in {"invalid_request", "invalid_signing_key", "invalid_hd_wallet"}
    assert len(custody.calls) == before


@pytest.mark.parametrize("chain", ["evm", "solana"])
def test_cli_reports_the_approval_when_account_policy_changes_after_its_preflight(custody, monkeypatch, capfd, chain):
    """The API owns the final policy check; a racing toggle cannot become null-signature success."""
    create(custody)
    original = api.vault_sign

    def toggle_then_sign(payload):
        api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True}, account=0)
        return original(payload)

    monkeypatch.setattr(api, "vault_sign", toggle_then_sign)
    args = cli.build_parser().parse_args([
        "vault", "sign", "HD_TEST_WALLET", "--chain", chain,
        *(["--message-hex", MESSAGE] if chain == "solana" else ["--digest", DIGEST]),
        "--session-id", "original-session", "--command", "verify and sign", "--no-callback",
    ])
    assert cli.cmd_vault_sign(args) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["kind"] == "vault_sign_request"
    assert result["request"]["status"] == "pending"
    assert ("solana_lifetime_hint" in result["request"]) == (chain == "solana")
    assert "signature" not in result
    request, = rows(vault_requests)
    requester = json.loads(request["requester"])
    assert requester["session_id"] == "original-session"
    assert requester["callback_disabled"] is True
    assert json.loads(request["delivery"])["command"] == "verify and sign"
    assert custody.calls == []


def test_hd_policy_can_only_be_raised_and_rejected_edit_is_atomic(custody):
    create(custody)
    api.update_vault_hd_account("HD_TEST_WALLET", {})
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": False, "name": "Before"}, account=0)
    api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True}, account=0)
    before_rows, before_audit = rows(vault_secrets), rows(vault_audit)
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": False, "name": "Must not change"}, account=0)
    assert error.value.code == "vault_policy_authz_required"
    assert error.value.status == 409
    with pytest.raises(vault_service.SigningPolicyDowngradeUnavailableError):
        with api._vault_engine().begin() as conn:
            vault_service.update_hd_account(conn, "HD_TEST_WALLET", account=0, changes={"always_ask": False})
    assert rows(vault_secrets) == before_rows and rows(vault_audit) == before_audit
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_secret("HD_TEST_WALLET", {"always_ask": True})
    assert error.value.code == "invalid_metadata"  # HD settings are per shared account index.
    assert rows(vault_secrets) == before_rows and rows(vault_audit) == before_audit
    updated = api.update_vault_hd_account("HD_TEST_WALLET", {"always_ask": True, "name": "Still editable"}, account=0)
    assert updated["secret"]["accounts"][0]["name"] == "Still editable"
    assert updated["secret"]["accounts"][0]["always_ask"] is True
    assert updated["secret"]["accounts"][1]["always_ask"] is False
    assert custody.calls == []
    for chain in ("evm", "bitcoin", "solana"):
        response = api.vault_sign(sign_input(chain, 0))
        assert response["code"] == "approval_required"
    assert custody.calls == []


def test_http_account_routes_use_real_storage_and_reject_nonobjects(custody):
    from vibe.ui_server import app

    create(custody)
    client = app.test_client()
    headers = csrf_headers(client)
    response = client.post("/api/vault/secrets/HD_TEST_WALLET/accounts", json={}, headers=headers)
    assert response.status_code == 200
    assert len(response.get_json()["secret"]["accounts"]) == 2
    response = client.patch("/api/vault/secrets/HD_TEST_WALLET/accounts/1", json={"name": "Daily", "always_ask": True}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["secret"]["accounts"][1]["name"] == "Daily"
    response = client.patch("/api/vault/secrets/HD_TEST_WALLET/accounts/1", json={"always_ask": False}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["code"] == "vault_policy_authz_required"
    assert meta()["accounts"][1]["always_ask"] is True
    response = client.post("/api/vault/secrets/HD_TEST_WALLET/accounts", json=[], headers=headers)
    assert response.status_code == 400
    assert len(meta()["accounts"]) == 2
    assert custody.calls == []


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("AVIBE_HD_MASTER_TEST_DIR"), reason="requires an explicit isolated master-build fixture/store")
def test_real_master_blindbox_import_and_three_chain_signing(monkeypatch):
    """Opt-in consumer check before the release: only the version probe is overridden.

    The supplied directory must be a test-owned master build with avault-test.sh
    (env -i, HOME/XDG/AVAULT_HOME isolation, --store file) and the matching public
    HD fixture. The wrapper, not Python, owns access to its disposable machine key.
    No production path discovery, keychain access or plaintext seed is involved.
    """
    root = Path(os.environ["AVIBE_HD_MASTER_TEST_DIR"]).resolve()
    fixture = json.loads((root / "hd-public-fixture.json").read_text())
    assert fixture["name"] == "HD_TEST_WALLET"
    assert fixture["source"]["commit"] == FIXTURE["source"]["commit"]
    wrapper = root / "avault-test.sh"
    wrapper_text = wrapper.read_text()
    assert "env -i" in wrapper_text and "AVAULT_STORE=file" in wrapper_text and 'AVAULT_HOME="$ROOT/avault-home"' in wrapper_text

    def run(args, *, stdin=None, **_kwargs):
        return subprocess.run([str(wrapper), *args], input=stdin, capture_output=True, timeout=30, check=False)

    actual_version = run(["--version"]).stdout.decode().strip()
    assert actual_version == fixture["source"]["version"] == "avault 0.1.10"
    monkeypatch.setattr(api, "_run_avault", run)
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": "0.1.10"})
    payload = {**CREATE, "blind_box": fixture["blindbox_import"]["blind_box"]}
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(payload)
    assert error.value.code == "avault_upgrade_required"
    assert rows(vault_secrets) == []
    # avault#49 deliberately did not bump/release 0.1.11; test the real contract
    # without adding a development bypass to production code.
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": "0.1.11"})
    api.create_vault_secret(payload)
    api.update_vault_hd_account("HD_TEST_WALLET", {})
    for account in (0, 1):
        for chain in ("evm", "bitcoin", "solana"):
            result = api.vault_sign(sign_input(chain, account))
            assert result["ok"]
            assert len(result["signature"]["signature"]) == 128
    assert meta()["use_count"] == 6
    for index, account in enumerate(meta()["accounts"]):
        assert {key: account[key] for key in ("account", "eth", "btc_taproot", "solana")} == fixture["expected_addresses"][index]
