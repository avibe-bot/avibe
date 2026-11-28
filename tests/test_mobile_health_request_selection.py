"""Public CLI request selection against isolated SQLite and a synthetic phone."""
import json
import sys

import pytest

from core.mobile_health_contract import LEGACY_METRICS, METRICS
from tests.test_mobile_health import (IDENTITY, clock, device_for, heart_manifest,  # noqa: F401
                                     pair_device, store, upload_payload)
from vibe import cli, cli_mobile


@pytest.fixture
def invoke(monkeypatch, capsys):
    monkeypatch.setattr(cli_mobile, "current_identity", lambda _: IDENTITY)
    monkeypatch.setattr(cli_mobile, "notify_phone", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mobile, "publish_request_card", lambda *a, **kw: None)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["vibe", "mobile", "health", *args, "--no-callback"])
        with pytest.raises(SystemExit) as exit:
            cli.main()
        return exit.value.code, json.loads(capsys.readouterr().out)
    return run


def acknowledge(store, device, request, *, seq=1):
    claim = store.claim(device["id"], maximum=1, request_id=request["id"])["requests"][0]
    store.upload(device, upload_payload(seq=seq, names=tuple(claim["metrics"]), empty=True))
    store.result(device, request["id"], {
        "claim_token": claim["claim_token"], "outcome": "succeeded",
        "result": {"metrics": {name: "empty" for name in claim["metrics"]}},
    })


@pytest.mark.parametrize("names", [LEGACY_METRICS, METRICS])
def test_cli_connect_decides_from_device_at_write_time(store, invoke, monkeypatch, names):
    initial, _ = store.create_request("health.connect", IDENTITY)
    code = store.issue_pairing(initial["id"], IDENTITY)["pairing_code"]
    original = cli_mobile._request
    paired = []

    def interleave(*args, **kwargs):
        # Caller B is paused after its read/preparation, before its write.
        assert not store.status()["connected"]
        # Caller A finishes the entire connection while B is descheduled.
        phone = store.pair({"pairing_code": code, "device_name": "Synthetic", "platform": "ios",
                            "app_version": "2", "metrics": list(names),
                            "installation_id": "same-phone"}, IDENTITY)
        paired.append(phone)
        acknowledge(store, device_for(store, phone), initial)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_mobile, "_request", interleave)
    status, reply = invoke("connect", "--wait", "0")
    assert status == 0
    if names == METRICS:
        assert reply["kind"] == "mobile_health" and reply["connected"]
    else:
        request = reply["request"]
        assert request["minimum_health_version"] == 2
        assert request["metrics"] == list(METRICS[len(LEGACY_METRICS):])
        assert store.issue_pairing(request["id"], IDENTITY)["expansion"]
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == len(names)
        assert conn.execute("SELECT revoked_at FROM devices WHERE id=?", (paired[0]["device_id"],)).fetchone()[0] is None
        assert not conn.execute("SELECT 1 FROM requests WHERE state='queued' AND device_id IS NULL AND consent_device_id IS NULL").fetchone()


@pytest.mark.parametrize("expansion", [False, True])
def test_cli_connect_waits_for_original_request_until_ack_and_result(store, invoke, expansion):
    paired, pending = pair_device(store, names=list(LEGACY_METRICS if expansion else METRICS))
    device = device_for(store, paired)
    if expansion:
        acknowledge(store, device, pending)
        _, reply = invoke("connect", "--wait", "0")
        pending = reply["request"]
        code = store.issue_pairing(pending["id"], IDENTITY)["pairing_code"]
        store.manifest(device, heart_manifest(list(METRICS), pairing_code=code, request_id=pending["id"]))
        device = device_for(store, paired)
    claim = store.claim(device["id"], maximum=1, request_id=pending["id"])["requests"][0]
    for uploaded in [False, True]:
        if uploaded:
            store.upload(device, upload_payload(seq=2 if expansion else 1,
                         names=tuple(claim["metrics"]), empty=True))
        status, reply = invoke("connect", "--wait", "0.01")
        assert status == 1 and reply["code"] == "device_wait_timeout"
        assert reply["details"]["request_id"] == pending["id"]
        status, reply = invoke("connect", "--wait", "0")
        assert status == 0 and reply["request"]["id"] == pending["id"]
        assert reply["request"]["state"] == "claimed"
    store.result(device, pending["id"], {"claim_token": claim["claim_token"], "outcome": "succeeded",
                 "result": {"metrics": {name: "empty" for name in claim["metrics"]}}})
    status, reply = invoke("connect", "--wait", "0.01")
    assert status == 0 and reply["kind"] == "mobile_health" and reply["connected"]


def test_cli_full_confirmation_without_pending_or_ack_requests_sync(store, invoke):
    paired, initial = pair_device(store, names=list(METRICS))
    store.cancel_request(initial["id"])
    status, reply = invoke("connect", "--wait", "0.01")
    assert status == 1 and reply["code"] == "device_wait_timeout"
    recovery = store.show_request(reply["details"]["request_id"])
    assert recovery["operation"] == "health.refresh" and recovery["metrics"] == list(METRICS)
    acknowledge(store, device_for(store, paired), recovery)
    status, reply = invoke("connect", "--wait", "0")
    assert status == 0 and reply["kind"] == "mobile_health"


def test_cli_hrv_refresh_cannot_complete_from_frozen_four_type_receipt(store, invoke, monkeypatch):
    paired, initial = pair_device(store, names=list(LEGACY_METRICS))
    device = device_for(store, paired)
    acknowledge(store, device, initial)
    status, reply = invoke("read", "--metric", "steps", "--period", "today", "--refresh", "--wait", "0")
    assert status == 1
    old_id = reply["details"]["request_id"]
    old = store.claim(device["id"], maximum=1, request_id=old_id)["requests"][0]
    frozen = upload_payload(seq=2, names=tuple(old["metrics"]), empty=True)
    _, reply = invoke("connect", "--wait", "0")
    expansion = reply["request"]
    code = store.issue_pairing(expansion["id"], IDENTITY)["pairing_code"]
    store.manifest(device, heart_manifest(list(METRICS), pairing_code=code, request_id=expansion["id"]))
    device = device_for(store, paired)
    real_wait = cli_mobile.wait_for_request

    def deliver_old_then_wait(*args, **kwargs):
        store.upload(device, frozen)
        store.result(device, old_id, {"claim_token": old["claim_token"], "outcome": "succeeded",
                     "result": {"metrics": {name: "empty" for name in old["metrics"]}}})
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(cli_mobile, "wait_for_request", deliver_old_then_wait)
    status, reply = invoke("read", "--metric", "hrv", "--period", "today", "--refresh", "--wait", "0.01")
    assert status == 1 and reply["code"] == "device_wait_timeout"
    new_id = reply["details"]["request_id"]
    assert new_id != old_id
    assert reply["details"]["data"]["metrics"]["hrv"]["items"][0]["availability"] == "not_synced"
    assert store.show_request(old_id)["metrics"] == list(LEGACY_METRICS)
    assert store.show_request(old_id)["state"] == "succeeded"
    def deliver_covering_then_wait(*args, **kwargs):
        assert args[1] == new_id
        acknowledge(store, device, store.show_request(new_id), seq=3)
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(cli_mobile, "wait_for_request", deliver_covering_then_wait)
    status, reply = invoke("read", "--metric", "hrv", "--period", "today", "--refresh", "--wait", "0.01")
    assert status == 0 and reply["metrics"]["hrv"]["items"][0]["availability"] == "empty"
