"""Private mobile contracts: fake device only; no ambient runtime or network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from core.handlers import mobile_health
from core.mobile_health_contract import MobileError, validate_result, validate_upload
from storage import mobile_store
from storage.mobile_store import MobileStore
from tests.ui_server_test_helpers import _save_config, csrf_headers
from vibe import cli, cli_mobile, permissions, ui_server

IDENTITY = {"id": "inst_123", "name": "Synthetic instance", "owner": "owner@example.test"}


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 11, 18, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(mobile_store, "_now", lambda: current[0].isoformat())
    return current


@pytest.fixture
def store(clock):
    return MobileStore()


def pair_device(store, *, installation="synthetic-install", names=None):
    names = names or ["steps", "sleep"]
    request, _ = store.create_request("health.connect", IDENTITY, names=names)
    code = store.issue_pairing(request["id"], IDENTITY)
    result = store.pair({
        "pairing_code": code["pairing_code"], "device_name": "假设备 iPhone",
        "platform": "ios", "app_version": "1.2.3", "metrics": names,
        "installation_id": installation,
    }, IDENTITY)
    return result, request


def upload_payload(*, seq=1, day="2026-11-18", names=("steps", "sleep"), empty=False):
    items = []
    for name in names:
        value = {"count": 987654321} if name == "steps" else {
            "asleep_minutes": 400.5, "in_bed_minutes": None,
            "start_at": "2026-11-17T23:00:00+08:00", "end_at": "2026-11-18T07:00:00+08:00",
            "stages": {"core": 300.5, "deep": 100},
        }
        items.append({
            "metric": name, "day": day, "timezone": "Asia/Shanghai",
            "availability": "empty" if empty else "ok", "value": None if empty else value,
            "latest_sample_end_at": None if empty else "2026-11-18T07:00:00+08:00",
            "queried_at": "2026-11-18T08:30:00+08:00", "closed": False,
        })
    return {
        "device_seq": seq, "computed_at": "2026-11-18T08:31:00+08:00",
        "window": {"metrics": list(names), "from": day, "to": day},
        "items": items, "failures": [],
    }


def device_for(store, paired):
    return store.authenticate(paired["credential"], IDENTITY)


def test_pairing_is_one_use_request_bound_and_stores_only_hashes(store, clock):
    """A replay or competing code must not replace a request's paired device."""
    request, _ = store.create_request("health.connect", IDENTITY, names=["steps"])
    first = store.issue_pairing(request["id"], IDENTITY)
    second = store.issue_pairing(request["id"], IDENTITY)
    payload = {"pairing_code": first["pairing_code"], "device_name": "假 iPhone", "platform": "ios",
               "app_version": "1", "metrics": ["steps"]}
    paired = store.pair(payload, IDENTITY)
    for code in (first["pairing_code"], second["pairing_code"]):
        with pytest.raises(MobileError, match="device_unauthorized"):
            store.pair({**payload, "pairing_code": code}, IDENTITY)
    with store.transaction() as conn:
        row = dict(conn.execute("SELECT * FROM devices").fetchone())
        assert row["credential_hash"] == hashlib.sha256(paired["credential"].encode()).hexdigest()
        assert paired["credential"] not in json.dumps(row)
        assert conn.execute("SELECT count(*) FROM pairings").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.show_request(request["id"])["state"] == "queued"
    other_request, _ = store.create_request("health.connect", IDENTITY, names=["steps"])
    code = store.issue_pairing(other_request["id"], IDENTITY)
    clock[0] += timedelta(minutes=5)
    with pytest.raises(MobileError, match="device_unauthorized"):
        store.pair({**payload, "pairing_code": code["pairing_code"]}, IDENTITY)


def test_targeted_disconnect_preserves_other_phone_data_and_pending_pairing(store, clock):
    first, req_a = pair_device(store, installation="phone-a")
    store.upload(device_for(store, first), upload_payload())
    store.cancel_request(req_a["id"])
    clock[0] += timedelta(seconds=1)
    second, req_b = pair_device(store, installation="phone-b")
    store.upload(device_for(store, second), upload_payload())
    store.cancel_request(req_b["id"])
    pending, _ = store.create_request("health.connect", IDENTITY)
    code = store.issue_pairing(pending["id"], IDENTITY)

    store.revoke(device_id=first["device_id"])

    with pytest.raises(MobileError, match="device_revoked"):
        device_for(store, first)
    assert device_for(store, second)["id"] == second["device_id"]
    with store.transaction() as conn:
        assert [(r[0], r[1]) for r in conn.execute(
            "SELECT device_id,count(*) FROM health_days GROUP BY device_id"
        )] == [(second["device_id"], 2)]
    store.precheck_pairing({"pairing_code": code["pairing_code"]})


def test_day_window_is_atomic_monotonic_and_preserves_empty_and_failure(store):
    """Incomplete uploads cannot partially replace data; failures keep prior days."""
    paired, _ = pair_device(store)
    device = device_for(store, paired)
    first = upload_payload()
    incomplete = {**first, "items": first["items"][:1]}
    with pytest.raises(MobileError, match="incomplete_window"):
        store.upload(device, incomplete)
    store.upload(device, first)
    with pytest.raises(MobileError, match="stale_seq"):
        store.upload(device, first)
    empty = upload_payload(seq=2, empty=True)
    store.upload(device, empty)
    read = store.read(["steps", "sleep"], "2026-11-17", "2026-11-18", caller="codex", max_age=21600)
    for name in ("steps", "sleep"):
        items = read["metrics"][name]["items"]
        assert items[0]["availability"] == "not_synced"
        assert items[1]["availability"] == "empty" and items[1]["value"] is None
        assert read["metrics"][name]["freshness"]["stale"] is True
        assert items[1]["received_at"]
    failed = upload_payload(seq=3)
    failed["items"] = [item for item in failed["items"] if item["metric"] == "steps"]
    failed["failures"] = [{
        "metric": "sleep", "at": "2026-11-18T08:35:00+08:00", "code": "protected_data_unavailable",
    }]
    store.upload(device, failed)
    assert store.status()["metrics"]["sleep"]["last_failure"]["code"] == "protected_data_unavailable"
    assert store.read(["sleep"], "2026-11-18", "2026-11-18", caller="codex")[
        "metrics"]["sleep"]["items"][0]["value"] is None
    validate_upload(upload_payload(), allowed_metrics=["steps", "sleep"])


def test_claims_are_bound_leased_bounded_and_results_are_value_free(store, clock):
    """Reclaims rotate tokens; the same phone ledger can report without reexecution."""
    paired, connect = pair_device(store)
    device = device_for(store, paired)
    assert store.claim(device["id"], maximum=1, request_id="mreq_" + "0" * 32) == {"requests": []}
    first = store.claim(device["id"], maximum=1, request_id=connect["id"])["requests"][0]
    assert first["operation"] == "health.connect"
    assert store.claim(device["id"], maximum=1)["requests"] == []
    # The fake phone's execution ledger survives reclaim and stores status only.
    executions = {}
    store.upload(device, upload_payload())
    executions[first["id"]] = {"steps": "ok", "sleep": "ok"}
    clock[0] += timedelta(seconds=61)
    reclaimed = store.claim(device["id"], maximum=1)["requests"][0]
    assert reclaimed["id"] == first["id"] and reclaimed["claim_token"] != first["claim_token"]
    with pytest.raises(MobileError, match="claim_mismatch"):
        store.result(device, first["id"], {"claim_token": first["claim_token"], "outcome": "succeeded",
                     "result": {"metrics": executions[first["id"]]}})
    result = {"claim_token": reclaimed["claim_token"], "outcome": "succeeded",
              "result": {"metrics": executions[reclaimed["id"]]}}
    assert store.result(device, connect["id"], result) == {"state": "succeeded"}
    assert store.result(device, connect["id"], result) == {"state": "succeeded"}
    assert len(executions) == 1
    bad = {**result, "result": {"metrics": {"steps": {"count": 987654321}}}}
    with pytest.raises(MobileError, match="invalid_payload"):
        store.result(device, connect["id"], bad)
    refresh, new = store.create_request("health.refresh", IDENTITY)
    again, fresh = store.create_request("health.refresh", IDENTITY)
    assert new and not fresh and refresh["id"] == again["id"]
    claim = store.claim(device["id"], maximum=1)["requests"][0]
    store.cancel_request(claim["id"])
    assert store.result(device, claim["id"], {
        "claim_token": claim["claim_token"], "outcome": "failed", "error": {"code": "internal"},
    }) == {"state": "canceled"}
    with store.transaction() as conn:
        assert conn.execute("SELECT late_result FROM requests WHERE id=?", (claim["id"],)).fetchone()[0] == 1
    next_request, _ = store.create_request("health.refresh", IDENTITY)
    for _ in range(3):
        assert store.claim(device["id"], maximum=1)["requests"]
        clock[0] += timedelta(seconds=61)
    assert store.claim(device["id"], maximum=1)["requests"] == []
    assert store.show_request(next_request["id"])["state"] == "failed"


def test_retention_credential_expiry_and_revoke_physically_remove_health_values(store, clock):
    """Retention is enforced by local calendar and revoke scrubs database pages."""
    paired, request = pair_device(store)
    device = device_for(store, paired)
    store.upload(device, upload_payload(day="2026-10-19"))
    assert not store.read(["steps"], "2026-11-18", "2026-11-18", caller="codex")[
        "metrics"]["steps"]["items"][0]["value"]
    store.upload(device, upload_payload(seq=2, day="2026-10-20"))
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 2
    clock[0] += timedelta(days=1)
    store.cleanup()
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0
    store.upload(device, upload_payload(seq=3, day="2026-11-19"))
    store.cancel_request(request["id"])
    clock[0] += timedelta(days=8)
    store.cleanup()
    with pytest.raises(MobileError, match="request_not_found"):
        store.show_request(request["id"])
    clock[0] += timedelta(days=90)
    store.cleanup()
    with pytest.raises(MobileError, match="device_revoked"):
        device_for(store, paired)
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM accesses").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0
    assert b"987654321" not in store.path.read_bytes()


def test_waiter_and_callback_compete_transactionally_and_dead_process_hands_over(store, clock, monkeypatch):
    """A terminal request produces either a synchronous continuation or one stable Run."""
    from storage.lock import MigrationFileLock
    waiter = "a" * 32
    lock = MigrationFileLock(store.waiter_path(waiter), timeout_seconds=0)
    lock.acquire()
    request, _ = store.create_request(
        "health.connect", IDENTITY, callback_session="ses_synthetic", waiter_id=waiter, wait_seconds=20,
    )
    store.cancel_request(request["id"])
    assert store.pending_callbacks() == []
    assert store.finish_wait(request["id"], waiter, consumed=True)
    lock.release()
    assert store.pending_callbacks() == []
    dead_waiter = "b" * 32
    dead_lock = MigrationFileLock(store.waiter_path(dead_waiter), timeout_seconds=0)
    dead_lock.acquire()
    second, _ = store.create_request(
        "health.connect", IDENTITY, callback_session="ses_synthetic", waiter_id=dead_waiter, wait_seconds=20,
    )
    store.cancel_request(second["id"])
    dead_lock.release()
    callbacks = store.pending_callbacks()
    assert [row["id"] for row in callbacks] == [second["id"]]
    assert not store.finish_wait(second["id"], dead_waiter, consumed=True)
    assert store.pending_callbacks()[0]["callback_run"] == callbacks[0]["callback_run"]
    sent = {}
    def enqueue(_request_store, **kwargs):
        sent[kwargs["run_id"]] = kwargs
        return SimpleNamespace(id=kwargs["run_id"])
    monkeypatch.setattr("core.scheduled_tasks.enqueue_session_callback", enqueue)
    mobile_health.deliver_callbacks(store, object())
    mobile_health.deliver_callbacks(store, object())
    assert len(sent) == 1 and "987654321" not in json.dumps(sent)


@pytest.mark.parametrize("killed,elapsed,reclaimed", [(True, 21, True), (False, 21, False), (True, 19, False)])
def test_abandoned_waiter_requires_both_expiry_and_a_free_process_lock(
    store, clock, tmp_path, killed, elapsed, reclaimed,
):
    import subprocess
    import time
    import psutil
    from storage.lock import MigrationFileLock, probe_lock_holder

    ready = tmp_path / "waiter-ready.json"
    waiter = "a" * 32
    driver = """
import json, os, sys
from pathlib import Path
from storage import mobile_store
from storage.lock import MigrationFileLock
mobile_store._now = lambda: sys.argv[4]
store = mobile_store.MobileStore(Path(sys.argv[1]))
with MigrationFileLock(store.waiter_path(sys.argv[3]), timeout_seconds=0):
    request, _ = store.create_request("health.connect",
        {"id":"inst_123", "owner":"owner@example.test"}, waiter_id=sys.argv[3], wait_seconds=20)
    ready = Path(sys.argv[2])
    stage = ready.with_suffix(".tmp")
    stage.write_text(json.dumps({"id":request["id"], "pid":os.getpid()}))
    stage.replace(ready)
    sys.stdin.buffer.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", driver, str(store.path), str(ready), waiter, clock[0].isoformat()],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    actual = None
    try:
        until = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < until:
            time.sleep(0.01)
        assert ready.exists(), "Synthetic waiter did not publish its ready marker"
        record = json.loads(ready.read_text())
        actual = psutil.Process(record["pid"])
        launcher = psutil.Process(process.pid)
        assert actual == launcher or actual in launcher.children(recursive=True)
        assert probe_lock_holder(store.waiter_path(waiter)) == "held"
        if killed:
            actual.kill()
            process.wait(timeout=10)
            assert probe_lock_holder(store.waiter_path(waiter)) == "free"
        clock[0] += timedelta(seconds=elapsed)
        before = store.show_request(record["id"])
        new_waiter = "b" * 32
        with MigrationFileLock(store.waiter_path(new_waiter), timeout_seconds=0):
            assert store.arm_wait(record["id"], new_waiter, 20) == reclaimed
        assert store.show_request(record["id"]) == before
        with store.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (record["id"],)).fetchone()
            assert row["callback_session"] is None
            assert row["waiter_id"] == (new_waiter if reclaimed else waiter)
    finally:
        if actual is not None and actual.is_running():
            try:
                actual.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if process.stdin:
            process.stdin.close()
        if process.stderr:
            process.stderr.close()


@pytest.mark.parametrize("callback_session", [None, "ses_synthetic"])
def test_reclaimed_cli_waiter_keeps_results_and_rejects_the_old_continuation(
    store, clock, monkeypatch, callback_session,
):
    from storage.lock import MigrationFileLock

    request, _ = store.create_request(
        "health.connect", IDENTITY, names=["steps", "sleep"], callback_session=callback_session,
    )
    paired, _ = pair_device(store)
    device = device_for(store, paired)
    old_waiter = "a" * 32
    with MigrationFileLock(store.waiter_path(old_waiter), timeout_seconds=0):
        assert store.arm_wait(request["id"], old_waiter, 20)
    clock[0] += timedelta(seconds=21)
    claim = store.claim(device["id"], maximum=1, request_id=request["id"])["requests"][0]
    store.upload(device, upload_payload())
    store.result(device, request["id"], {
        "claim_token": claim["claim_token"], "outcome": "succeeded",
        "result": {"metrics": {"steps": "ok", "sleep": "ok"}},
    })
    before = store.show_request(request["id"])
    wait = cli_mobile.wait_for_request

    def old_waiter_arrives_late(*args, **kwargs):
        assert not store.finish_wait(request["id"], old_waiter, consumed=True)
        with pytest.raises(MobileError, match="device_wait_timeout"):
            mobile_health.wait_for_request(store, request["id"], 0, waiter_id=old_waiter)
        assert store.pending_callbacks() == []
        return wait(*args, **kwargs)

    monkeypatch.setattr(cli_mobile, "wait_for_request", old_waiter_arrives_late)
    args = cli.build_parser().parse_args(["mobile", "requests", "await", request["id"], "--timeout", "20"])
    assert cli_mobile.execute(args)["request"] == before
    assert store.show_request(request["id"]) == before
    assert store.pending_callbacks() == []
    with pytest.raises(MobileError, match="continuation_owned"):
        cli_mobile.execute(args)


def test_concurrent_reclaimers_elect_one_waiter(store, clock):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from storage.lock import MigrationFileLock

    old = "a" * 32
    with MigrationFileLock(store.waiter_path(old), timeout_seconds=0):
        request, _ = store.create_request("health.connect", IDENTITY, waiter_id=old, wait_seconds=20)
    clock[0] += timedelta(seconds=21)
    barrier = threading.Barrier(2)

    def compete(waiter):
        with MigrationFileLock(store.waiter_path(waiter), timeout_seconds=0):
            barrier.wait(timeout=5)
            return waiter, store.arm_wait(request["id"], waiter, 20)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, ["b" * 32, "c" * 32]))
    assert sum(armed for _, armed in results) == 1
    for waiter, armed in results:
        assert store.finish_wait(request["id"], waiter, consumed=True) == armed
    assert not store.finish_wait(request["id"], old, consumed=True)


def test_refresh_timeout_retains_existing_data_and_never_reports_success(store, monkeypatch, capsys):
    """A phone that does not answer must produce the documented error envelope."""
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    monkeypatch.setattr(cli_mobile, "current_identity", lambda _store: IDENTITY)
    monkeypatch.setattr(cli_mobile, "notify_phone", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_mobile, "publish_request_card", lambda *_args, **_kwargs: None)
    args = cli.build_parser().parse_args([
        "mobile", "health", "read", "--metric", "steps", "--from", "2026-11-18", "--to", "2026-11-18",
        "--refresh", "--wait", "0", "--no-callback",
    ])
    assert cli_mobile.cmd_mobile(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and not result["ok"]
    assert result["code"] == "device_wait_timeout"
    assert result["details"]["request_id"]
    assert result["details"]["data"]["metrics"]["steps"]["items"][0]["value"]["count"] == 987654321


def test_skill_examples_parse_with_real_cli_parser_and_invalid_windows_fail():
    """Agent-visible examples remain actual callers of the shipped parser."""
    skill = Path(__file__).resolve().parents[1] / "skills/use-mobile-health/SKILL.md"
    examples = [line for line in skill.read_text().splitlines() if line.startswith("vibe mobile ")]
    parser = cli.build_parser()
    for example in examples:
        args = parser.parse_args(shlex.split(example)[1:])
        assert args.command == "mobile"
    for suffix in (
        ["--period", "today", "--from", "2026-11-01"],
        ["--callback-session-id", "ses_test", "--no-callback"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["mobile", "health", "read", *suffix])


@pytest.fixture
def mobile_http(monkeypatch, tmp_path, store):
    config = _save_config(tmp_path)
    projection = {
        "instance": {"id": "inst_123", "name": "Synthetic instance", "access_mode": "allowlist"},
        "access": {"owner": {"email": "owner@example.test"}, "entries": []},
    }
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "fake-test-secret"
    config.save()
    monkeypatch.setattr(permissions, "get_current_permissions", lambda *_args: SimpleNamespace(
        source="live", projection=projection,
    ))
    ui_server._auth_ratelimit.clear()
    return ui_server.app.test_client(), projection


def test_device_http_auth_scope_owner_pairing_and_no_cookie_health_read(store, mobile_http, caplog):
    """Real request hooks cannot turn a browser cookie or route alias into device authority."""
    client, projection = mobile_http
    request, _ = store.create_request("health.connect", IDENTITY, names=["steps"])
    assert client.post("/api/mobile/pairings", json={"request_id": request["id"]}).status_code == 403
    headers = csrf_headers(client)
    code = client.post("/api/mobile/pairings", json={"request_id": request["id"]}, headers=headers)
    assert code.status_code == 200
    pair = client.post("/api/mobile/device/v1/pair", json={
        "pairing_code": code.get_json()["pairing_code"], "device_name": "假设备",
        "platform": "ios", "app_version": "1", "metrics": ["steps"],
    })
    assert pair.status_code == 200
    credential = pair.get_json()["credential"]
    auth = {"Authorization": f"Bearer {credential}"}
    upload = upload_payload(names=("steps",))
    assert client.put("/api/mobile/device/v1/health/days", json=upload, headers=headers).status_code == 401
    uploaded = client.put("/api/mobile/device/v1/health/days", json=upload, headers=auth)
    assert uploaded.status_code == 200, uploaded.text
    assert "set-cookie" not in uploaded.headers
    status = client.get(f"/api/mobile/requests/{request['id']}")
    assert status.status_code == 200
    assert set(status.get_json()) == {
        "id", "operation", "state", "created_at", "expires_at", "requires_user", "metrics", "error",
    }
    for path in ("/api/mobile/health", "/api/mobile/device/v1/health/days"):
        assert client.get(path, headers=headers).status_code in {404, 405}
    for path in (
        "/api/mobile/device/v1/health%2Fdays", "/api/mobile/device/v1/health/days/",
        "/api/mobile/device/v1/%68ealth/days",
    ):
        response = client.put(path, json=upload, headers=auth)
        assert response.status_code != 200
    assert client.post("/api/mobile/pairings", json={"request_id": request["id"]}, headers=auth).status_code == 403
    assert "987654321" not in caplog.text
    assert client.put("/api/mobile/device/v1/health/days", content=" " * (256 * 1024 + 1),
                      headers={**auth, "Content-Type": "application/json"}).status_code == 413
    projection["access"]["owner"]["email"] = "next-owner@example.test"
    denied = client.put("/api/mobile/device/v1/health/days", json=upload, headers=auth)
    # A live owner change revokes the previous owner's device before it authenticates.
    assert denied.status_code == 403 and denied.get_json()["code"] == "device_revoked"
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0


def _health_days(store) -> int:
    with store.transaction() as conn:
        return conn.execute("SELECT count(*) FROM health_days").fetchone()[0]


def _set_instance_kind(kind: str) -> None:
    from config.v2_config import V2Config

    config = V2Config.load()
    config.remote_access.vibe_cloud.instance_kind = kind
    config.save()


ORG = {"id": "org_synthetic", "name": "Synthetic organization"}
SHARED = [
    {"kind": "email", "value": "colleague@example.test", "role": "member"},
    {"kind": "organization_group", "value": "grp_synthetic", "role": "viewer"},
]


@pytest.mark.parametrize("kind, organization, access_mode, entries", [
    ("personal", None, "allowlist", []),
    ("personal", None, "allowlist", SHARED),
    ("personal", None, "public", []),
    ("organization", ORG, "allowlist", []),
    ("organization", ORG, "allowlist", SHARED),
    ("organization", ORG, "public", SHARED),
], ids=["personal", "personal-shared", "personal-public", "org", "org-shared", "org-public"])
def test_owner_syncs_on_any_instance_kind_or_sharing(store, mobile_http, kind, organization, access_mode, entries):
    """Max 2026-11-20: who may reach the instance (and its Agents) is the owner's own access setting."""
    client, projection = mobile_http
    _set_instance_kind(kind)
    projection["instance"].update({"organization": organization, "access_mode": access_mode})
    projection["access"]["entries"] = entries
    request, _ = store.create_request("health.connect", IDENTITY, names=["steps", "sleep"])
    code = client.post("/api/mobile/pairings", json={"request_id": request["id"]}, headers=csrf_headers(client))
    assert code.status_code == 200, code.text
    pair = client.post("/api/mobile/device/v1/pair", json={
        "pairing_code": code.get_json()["pairing_code"], "device_name": "假设备",
        "platform": "ios", "app_version": "1", "metrics": ["steps", "sleep"],
    })
    assert pair.status_code == 200, pair.text
    auth = {"Authorization": f"Bearer {pair.get_json()['credential']}"}
    uploaded = client.put("/api/mobile/device/v1/health/days", json=upload_payload(), headers=auth)
    assert uploaded.status_code == 200, uploaded.text
    assert mobile_health.current_identity(store)["owner"] == "owner@example.test"
    assert _health_days(store) > 0 and store.status()["connected"]


@pytest.mark.parametrize("failure", [
    "cached-projection", "permissions-unavailable", "missing-credentials", "no-owner", "other-instance",
])
def test_unconfirmed_eligibility_pauses_without_revoking_or_purging(store, mobile_http, monkeypatch, failure):
    """Only a live, confirmed owner change (or an explicit revoke/unpair) may purge."""
    client, projection = mobile_http
    paired, _ = pair_device(store)
    auth = {"Authorization": f"Bearer {paired['credential']}"}
    assert client.put("/api/mobile/device/v1/health/days", json=upload_payload(), headers=auth).status_code == 200
    before = _health_days(store)
    if failure == "cached-projection":
        monkeypatch.setattr(permissions, "get_current_permissions", lambda *_a: SimpleNamespace(source="cache"))
    elif failure == "permissions-unavailable":
        def unavailable(*_a):
            raise permissions.PermissionsError("synthetic 403 without an owner answer")
        monkeypatch.setattr(permissions, "get_current_permissions", unavailable)
    elif failure == "missing-credentials":
        from config.v2_config import V2Config
        config = V2Config.load()
        config.remote_access.vibe_cloud.instance_secret = ""
        config.save()
    elif failure == "no-owner":
        projection["access"]["owner"]["email"] = None
    else:
        projection["instance"]["id"] = "inst_other"
    response = client.put("/api/mobile/device/v1/health/days", json=upload_payload(seq=2), headers=auth)
    assert response.status_code == 503, response.text
    assert response.get_json()["code"] == "instance_eligibility_unavailable"
    with pytest.raises(MobileError, match="instance_eligibility_unavailable"):
        mobile_health.current_identity(store)
    assert mobile_health.maintenance_pass(SimpleNamespace(), check_eligibility=True) is False
    assert store.status()["connected"] and _health_days(store) == before


def test_live_permissions_unavailability_pauses_and_device_signout_revokes(store, mobile_http, monkeypatch):
    client, _ = mobile_http
    paired, _ = pair_device(store)
    auth = {"Authorization": f"Bearer {paired['credential']}"}
    monkeypatch.setattr(permissions, "get_current_permissions", lambda *_args: SimpleNamespace(source="cache"))
    response = client.put("/api/mobile/device/v1/health/days", json=upload_payload(), headers=auth)
    assert response.status_code == 503 and response.headers["Retry-After"] == "30"
    monkeypatch.setattr("vibe.ui_mobile_routes.current_identity", lambda _store: IDENTITY)
    assert client.delete("/api/mobile/device/v1/", headers=auth).status_code == 200
    response = client.post("/api/mobile/device/v1/requests/claim",
                           json={"wait_seconds": 0, "max": 1}, headers=auth)
    assert response.status_code == 403 and response.get_json()["code"] == "device_revoked"


@pytest.mark.parametrize("endpoint", ["/health", "/"])
@pytest.mark.parametrize("content_type", [None, "application/json"])
def test_empty_device_delete_revokes_with_explicit_zero_length(
    store, mobile_http, endpoint, content_type,
):
    client, _ = mobile_http
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    auth = {"Authorization": f"Bearer {paired['credential']}", "Content-Length": "0"}
    if content_type:
        auth["Content-Type"] = content_type
    # Nonempty bodies still require an empty JSON object, without changing state.
    path = "/api/mobile/device/v1" + endpoint
    assert client.delete(path, json={"unexpected": True}, headers=auth).status_code == 422
    assert store.status()["connected"]
    response = client.delete(path, content=b"", headers=auth)
    assert response.status_code == 200
    assert not store.status()["connected"]
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0
    rejected = client.post("/api/mobile/device/v1/requests/claim",
                           json={"wait_seconds": 0, "max": 1}, headers=auth)
    assert rejected.status_code == 403 and rejected.get_json()["code"] == "device_revoked"


def test_mobile_health_cannot_enter_general_data_query(store, tmp_path):
    """Separate file ownership, not a table filter, protects generic exports."""
    from storage.pagination import PageRequest
    from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    general = tmp_path / "general.sqlite3"
    with sqlite3.connect(general) as conn:
        conn.execute("CREATE TABLE ordinary(id TEXT)")
        conn.execute("INSERT INTO ordinary VALUES('safe')")
    result = run_read_only_query("SELECT * FROM ordinary", db_path=general, page_request=PageRequest())
    assert "987654321" not in json.dumps(result.rows)
    with pytest.raises(ReadOnlyQueryError):
        run_read_only_query("SELECT * FROM health_days", db_path=general, page_request=PageRequest())
    with pytest.raises(ReadOnlyQueryError):
        run_read_only_query(f"ATTACH DATABASE '{store.path}' AS mobile", db_path=general, page_request=PageRequest())
    with store.transaction() as conn:
        assert "987654321" not in json.dumps([dict(row) for row in conn.execute("SELECT * FROM accesses")])


@pytest.mark.parametrize("invalid", [None, True, 12, [], {}, ["internal"]])
def test_untrusted_status_shapes_fail_with_fixed_codes(invalid):
    """All malformed JSON statuses fail at validation, without formatting values."""
    upload = upload_payload()
    upload["items"] = []
    upload["failures"] = [{"metric": "steps", "at": upload["computed_at"], "code": invalid}]
    with pytest.raises(MobileError, match="invalid_payload"):
        validate_upload(upload, allowed_metrics=["steps", "sleep"])
    for payload in (
        {"claim_token": "token", "outcome": invalid},
        {"claim_token": "token", "outcome": "failed", "error": {"code": invalid}},
    ):
        with pytest.raises(MobileError, match="invalid_payload"):
            validate_result(payload, ["steps"])


def test_cancellation_does_not_fabricate_native_user_decline(store):
    request, _ = store.create_request("health.connect", IDENTITY)
    store.cancel_request(request["id"])
    with pytest.raises(MobileError, match="request_terminal"):
        mobile_health.wait_for_request(store, request["id"], 0, waiter_id=None)


def test_metadata_exposes_only_allowlisted_failure_status(store, mobile_http):
    client, _ = mobile_http
    paired, request = pair_device(store)
    device = device_for(store, paired)
    claim = store.claim(device["id"], maximum=1)["requests"][0]
    store.result(device, request["id"], {
        "claim_token": claim["claim_token"], "outcome": "failed", "error": {"code": "user_declined"},
    })
    response = client.get(f"/api/mobile/requests/{request['id']}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["state"] == "failed" and payload["error"] == {"code": "user_declined"}
    assert not {"result", "claim_token", "credential"} & payload.keys()


def test_remote_device_bearer_cannot_acquire_browser_owner_authority(store, mobile_http):
    client, _ = mobile_http
    paired, _ = pair_device(store)
    auth = {"Authorization": "Bearer " + paired["credential"]}
    remote = {"base_url": "https://alex.avibe.bot", "environ_base": {"REMOTE_ADDR": "203.0.113.10"}}
    response = client.put("/api/mobile/device/v1/health/days", json=upload_payload(), headers=auth, **remote)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert client.get("/api/permissions", headers=auth, **remote).status_code == 401
    assert client.post("/api/mobile/pairings", json={"request_id": "mreq_" + "0" * 32},
                       headers=auth, **remote).status_code == 401
    response = client.post("/api/mobile/device/v1/requests/claim", json={"max": 1, "wait_seconds": 0},
                           headers={**auth, "Host": "unrelated.example.test"}, **remote)
    assert response.status_code == 503


def test_private_storage_rejects_linked_files_and_directories(tmp_path):
    target = tmp_path / "ordinary.sqlite3"
    target.write_bytes(b"ordinary")
    private = tmp_path / "private"
    private.mkdir()
    (private / "mobile.sqlite3").symlink_to(target)
    with pytest.raises(MobileError, match="private_storage_unavailable"):
        MobileStore(private / "mobile.sqlite3")
    assert target.read_bytes() == b"ordinary"
    alias = tmp_path / "private-alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(MobileError, match="private_storage_unavailable"):
        MobileStore(alias / "other.sqlite3")


def test_private_storage_without_descriptor_chmod_still_rejects_links(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    private = tmp_path / "mobile"
    store = MobileStore(private / "mobile.sqlite3")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert private.stat().st_mode & 0o777 == 0o700
    ordinary = tmp_path / "ordinary.db"
    ordinary.write_bytes(b"synthetic")
    link = private / "linked.sqlite3"
    link.symlink_to(ordinary)
    with pytest.raises(MobileError, match="private_storage_unavailable"):
        MobileStore(link)
    assert ordinary.read_bytes() == b"synthetic"


def test_relative_cli_windows_use_phone_calendar_and_freshness_uses_instants(store):
    paired, _ = pair_device(store)
    device = device_for(store, paired)
    store.manifest(device, {
        "contract": 1, "app_version": "1", "os_version": "26", "timezone": "America/Los_Angeles",
        "metrics": ["steps", "sleep"], "background_delivery": True, "push_wake": True,
    })
    args = cli.build_parser().parse_args(["mobile", "health", "read", "--period", "today"])
    assert cli_mobile._window(args, today=store.calendar_today()) == ("2026-11-17", "2026-11-17")
    payload = upload_payload(names=("steps",), day="2026-11-17")
    payload["window"]["to"] = "2026-11-18"
    earlier = {**payload["items"][0], "queried_at": "2026-11-18T01:00:00+08:00"}
    later = {**earlier, "day": "2026-11-18", "queried_at": "2026-11-17T23:00:00+00:00"}
    payload["items"] = [earlier, later]
    store.upload(device, payload)
    data = store.read(["steps"], "2026-11-17", "2026-11-18", caller="test")
    assert data["metrics"]["steps"]["freshness"]["queried_at"] == earlier["queried_at"]


def test_browser_file_media_and_show_surfaces_cannot_export_mobile_storage(store, mobile_http, tmp_path):
    """The private-tree invariant holds across file aliases and parent copies."""
    from urllib.parse import quote
    from core import file_browser_service as files
    from core.mobile_storage_boundary import is_mobile_private_path
    from storage import media_service
    from storage.db import get_cached_sqlite_engine

    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    client, _ = mobile_http
    alias = tmp_path / "alias.db"
    alias.symlink_to(store.path)
    folder_alias = tmp_path / "alias-dir"
    folder_alias.symlink_to(store.path.parent, target_is_directory=True)
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary file")
    variants = (
        str(store.path), str(alias), str(folder_alias / store.path.name),
        str(store.path.parent / ".." / "mobile" / store.path.name),
        str(store.path).upper(), str(store.path) + "/",
        str(store.path).replace("mobile", "%6dobile"),
    )
    for path in variants:
        assert is_mobile_private_path(path)
        response = client.get("/api/files/content?path=" + quote(path, safe=""))
        assert response.status_code == 403
        assert "987654321" not in response.text
        with pytest.raises(files.FileBrowserError, match="Permission denied"):
            files.copy_path(path, str(tmp_path / "exported.db"))
    assert client.get("/api/files/content?path=" + quote(str(ordinary), safe="")).text == "ordinary file"
    for operation in (
        lambda: files.copy_path(str(store.path.parent.parent), str(tmp_path / "exported-state")),
        lambda: files.move_path(str(store.path.parent.parent), str(tmp_path / "moved-state")),
        lambda: files.rename_path(str(store.path.parent.parent), "renamed-state"),
        lambda: files.delete_path(str(store.path.parent.parent), recursive=True),
    ):
        with pytest.raises(files.FileBrowserError, match="Permission denied"):
            operation()
    assert not (tmp_path / "exported-state").exists()
    assert files.search(str(store.path.parent.parent), "987654321")["results"] == []
    with get_cached_sqlite_engine().begin() as conn:
        with pytest.raises(ValueError, match="Private mobile storage"):
            media_service.register(conn, scope_id=None, session_id=None, kind="file",
                                   source="test", local_path=str(store.path))
    page = tmp_path / "page"
    page.mkdir()
    (page / "private.db").symlink_to(store.path)
    from config import paths
    show_page = paths.get_show_page_dir("ses_fake")
    show_page.mkdir(parents=True, exist_ok=True)
    (show_page / "private.db").symlink_to(store.path)
    for path in (f"@fs{store.path}", "private.db"):
        assert ui_server._is_show_page_runtime_denied_path(path, session_id="ses_fake")
    response = ui_server._show_page_file_response(page, "private.db")
    assert response.status_code == 404


def test_private_identity_covers_hard_links_and_literal_percent_roots(store, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from config import paths
    from core import file_browser_service as files
    from core.mobile_storage_boundary import MobileStorageBoundary
    private = tmp_path / "home%41" / "state" / "mobile"
    private.mkdir(parents=True)
    secret = private / "synthetic.txt"
    secret.write_text("synthetic health value")
    alias = tmp_path / "ordinary-name.txt"
    os.link(secret, alias)
    monkeypatch.setattr(paths, "get_state_dir", lambda: private.parent)
    original_scandir = os.scandir
    @contextmanager
    def zero_identity_entries(path):
        with original_scandir(path) as entries:
            # Windows DirEntry identities are zero, unlike os.stat(path).
            yield (SimpleNamespace(path=entry.path, stat=lambda **_kwargs: os.stat_result((0,) * 10))
                   for entry in entries)
    def windows_directory_entries(path):
        if not isinstance(path, int) and os.path.realpath(path) == os.path.realpath(private):
            return zero_identity_entries(path)
        return original_scandir(path)
    monkeypatch.setattr(os, "scandir", windows_directory_entries)
    boundary = MobileStorageBoundary()
    assert boundary.is_private_path(secret)
    assert boundary.is_private_path(alias)
    assert boundary.is_private_path(private / "new-directory" / "new-file")
    assert boundary.is_private_path(private.parent, include_ancestors=True)
    with pytest.raises(files.FileBrowserError, match="Permission denied"):
        files.file_content(str(alias))
    listing = files.list_directory(str(tmp_path), show_hidden=True)
    assert "ordinary-name.txt" not in {entry["name"] for entry in listing["entries"]}
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    assert not boundary.is_private_path(ordinary)
    assert files.file_content(str(ordinary)).data == b"ordinary"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor aliases")
@pytest.mark.parametrize("spelling", ["dev", "DEV", "Dev"])
@pytest.mark.parametrize("surface", ["file", "register", "media", "show", "runtime", "metadata"])
@pytest.mark.parametrize("alias_kind", ["fd", "symlink"])
def test_fd_aliases_cannot_publish_private_bytes(
    store, mobile_http, tmp_path, surface, alias_kind, spelling,
):
    """All path consumers refuse descriptor aliases, including previously minted media."""
    from urllib.parse import quote
    from core import file_browser_service as files
    from storage import media_service
    from storage.db import get_cached_sqlite_engine
    from storage.models import media_objects

    secret = store.path.parent / "synthetic.txt"
    secret.write_text("synthetic-private-marker")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    client, _ = mobile_http
    if surface in {"register", "media"}:
        from storage.importer import ensure_sqlite_state

        ensure_sqlite_state()
    with secret.open("rb") as handle:
        alias = Path(f"/{spelling}/fd/{handle.fileno()}")
        if not alias.parent.exists():
            pytest.skip("This filesystem has no case-insensitive devfs alias")
        if alias_kind == "symlink":
            link = tmp_path / "descriptor-alias.txt"
            link.symlink_to(alias)
            alias = link
        if surface == "file":
            response = client.get("/api/files/content?path=" + quote(str(alias), safe=""))
            assert response.status_code == 403
            assert "synthetic-private-marker" not in response.text
        elif surface == "register":
            with get_cached_sqlite_engine().begin() as conn:
                with pytest.raises(ValueError, match="Private mobile storage"):
                    media_service.register(conn, scope_id=None, session_id=None, kind="file",
                                           source="test", local_path=str(alias))
        elif surface == "media":
            with get_cached_sqlite_engine().begin() as conn:
                token = media_service.register(conn, scope_id=None, session_id=None, kind="file",
                                               source="test", local_path=str(ordinary))
                # A token minted before the new guard must not retain the old capability.
                conn.execute(media_objects.update().where(media_objects.c.token == token).values(
                    local_path=str(alias),
                ))
            response = client.get(f"/api/media/{token}")
            assert response.status_code == 404
            assert "synthetic-private-marker" not in response.text
        elif surface == "show":
            with ui_server.app.test_request_context("/"):
                response = ui_server._show_page_file_response(Path("/"), str(alias).lstrip("/"))
                assert response.status_code == 404
        elif surface == "metadata":
            with pytest.raises(files.FileBrowserError) as failure:
                files.metadata(str(alias))
            assert failure.value.status_code == 403
        else:
            assert ui_server._is_show_page_runtime_denied_path(f"@fs{alias}", session_id="ses_fake")
    assert files.file_content(str(ordinary)).data == b"ordinary"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor aliases")
@pytest.mark.parametrize("spelling", ["dev", "DEV", "Dev"])
@pytest.mark.parametrize("operation", ["search", "replace"])
@pytest.mark.parametrize("via_symlink", [False, True])
def test_descriptor_text_operations_refuse_the_namespace(
    store, tmp_path, spelling, operation, via_symlink,
):
    from core import file_browser_service as files

    root = Path(f"/{spelling}/fd")
    if not root.exists():
        pytest.skip("This filesystem has no case-insensitive devfs alias")
    if via_symlink:
        alias = tmp_path / "descriptor-root"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    secret = store.path.parent / "synthetic.txt"
    secret.write_text("synthetic-private-marker")
    with secret.open("rb"):
        with pytest.raises(files.FileBrowserError) as failure:
            if operation == "search":
                files.search(str(root), "synthetic-private-marker")
            else:
                # No match even on the old implementation: this probe must not
                # attempt a write into devfs before establishing the boundary.
                files.replace(str(root), "absent-test-query", "replacement")
        assert failure.value.status_code == 403
    assert secret.read_text() == "synthetic-private-marker"


@pytest.mark.parametrize("lossy", [False, True])
@pytest.mark.parametrize("replacement_time", ["before_open", "after_open"])
def test_search_reader_checks_and_consumes_the_same_open_object(
    store, tmp_path, monkeypatch, lossy, replacement_time,
):
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_text("synthetic-private-marker")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    original_open = open
    opened = []

    def substitute_at_open(path, *args, **kwargs):
        if Path(path) != ordinary:
            return original_open(path, *args, **kwargs)
        opened.append(path)
        if replacement_time == "before_open":
            return original_open(secret, *args, **kwargs)
        handle = original_open(path, *args, **kwargs)
        ordinary.unlink()
        ordinary.symlink_to(secret)
        return handle

    monkeypatch.setattr(files, "open", substitute_at_open, raising=False)
    text, mtime = files._read_file_text(ordinary, lossy=lossy)
    assert text == (None if replacement_time == "before_open" else "ordinary")
    assert (mtime is None) == (replacement_time == "before_open")
    assert len(opened) == 1


@pytest.mark.parametrize("operation", ["search", "replace"])
def test_text_consumers_skip_private_objects_substituted_at_open(
    store, tmp_path, monkeypatch, operation,
):
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_text("synthetic-private-marker")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    original_open = open

    def substitute_at_open(path, *args, **kwargs):
        return original_open(secret if Path(path) == ordinary else path, *args, **kwargs)

    monkeypatch.setattr(files, "open", substitute_at_open, raising=False)
    if operation == "search":
        result = files.search(str(tmp_path), "synthetic-private-marker")
        assert result["total_matches"] == 0
    else:
        result = files.replace(str(tmp_path), "synthetic-private-marker", "replacement")
        assert result["files_changed"] == 0 and result["undo_token"] is None
    assert ordinary.read_text() == "ordinary"
    assert secret.read_text() == "synthetic-private-marker"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
def test_search_reader_keeps_the_early_nonregular_file_screen(store, tmp_path, monkeypatch):
    from core import file_browser_service as files

    fifo = tmp_path / "ordinary.fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(files, "open", lambda *_a, **_kw: pytest.fail("A FIFO must not be opened"), raising=False)
    assert files._read_file_text(fifo, lossy=True) == (None, None)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_cross_device_move_preserves_source_timestamps(store, tmp_path, monkeypatch, kind):
    from core import file_browser_service as files

    source = tmp_path / "source"
    if kind == "directory":
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "file.txt").write_bytes(b"ordinary")
    elif kind == "symlink":
        if os.utime not in os.supports_follow_symlinks:
            pytest.skip("Symlink timestamps are not supported")
        ordinary = tmp_path / "ordinary.txt"
        ordinary.write_bytes(b"ordinary")
        source.symlink_to(ordinary)
    else:
        source.write_bytes(b"ordinary")
    entries = [source, *source.rglob("*")] if kind == "directory" else [source]
    expected = {}
    for entry in entries:
        os.utime(entry, ns=(1_900_000_000_123456789, 1_600_000_000_987654321), follow_symlinks=False)
        info = entry.lstat()
        expected[entry.relative_to(source)] = (info.st_atime_ns, info.st_mtime_ns)
    destination = tmp_path / "destination"
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
    assert files.move_path(str(source), str(destination))["ok"]
    assert not source.exists() and not source.is_symlink()
    for relative, timestamps in expected.items():
        info = (destination / relative).lstat()
        assert (info.st_atime_ns, info.st_mtime_ns) == timestamps


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Extended attributes are not supported")
def test_cross_device_move_preserves_extended_attributes(store, tmp_path, monkeypatch):
    from core import file_browser_service as files

    source = tmp_path / "source"
    source.write_bytes(b"ordinary")
    try:
        os.setxattr(source, "user.avibe-synthetic", b"synthetic metadata")
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        pytest.skip("This filesystem has no extended attributes")
    destination = tmp_path / "destination"
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
    assert files.move_path(str(source), str(destination))["ok"]
    assert os.getxattr(destination, "user.avibe-synthetic") == b"synthetic metadata"


def test_cross_device_metadata_failure_keeps_source_and_discards_stage(store, tmp_path, monkeypatch):
    from core import file_browser_service as files

    source = tmp_path / "source"
    source.write_bytes(b"ordinary")
    destination = tmp_path / "destination"
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    def metadata_failure(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Synthetic metadata failure")

    monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
    monkeypatch.setattr(files.shutil, "copystat", metadata_failure)
    with pytest.raises(files.FileBrowserError, match="Synthetic metadata failure") as failure:
        files.move_path(str(source), str(destination))
    assert failure.value.code == "fs_error"
    assert source.read_bytes() == b"ordinary"
    assert not destination.exists()
    assert not list(tmp_path.glob(f"{files._COPY_TEMP_PREFIX}*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Requires real Linux procfs")
@pytest.mark.parametrize("namespace", ["fd", "map_files"])
def test_linux_proc_descriptor_namespaces_are_denied_before_output(store, tmp_path, namespace):
    """Use actual procfs links; an ordinary target must not erase namespace policy."""
    import mmap
    from core import file_browser_service as files
    from core.mobile_storage_boundary import MobileStorageBoundary

    ordinary = tmp_path / "mapped.txt"
    ordinary.write_bytes(b"ordinary mapped bytes")
    with ordinary.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ):
        if namespace == "fd":
            alias = Path(f"/proc/self/fd/{handle.fileno()}")
        else:
            alias = next(
                path for path in Path("/proc/self/map_files").iterdir()
                if os.readlink(path) == str(ordinary)
            )
        assert alias.is_symlink() and os.readlink(alias) == str(ordinary)
        assert MobileStorageBoundary().is_private_path(alias)
        with pytest.raises(files.FileBrowserError) as failure:
            files.file_content(str(alias))
        assert failure.value.status_code == 403
    assert files.file_content(str(ordinary)).data == b"ordinary mapped bytes"


@pytest.mark.parametrize("replacement_time", ["before_open", "after_open"])
def test_file_content_validates_and_consumes_the_same_open_object(
    store, tmp_path, monkeypatch, replacement_time,
):
    """A private object substituted at open is denied; a later path swap is never reopened."""
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_bytes(b"synthetic-private-marker")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_bytes(b"ordinary")
    original_open = os.open
    opened = []

    def swap_at_open(path, flags, *args, **kwargs):
        if Path(path) != ordinary:
            return original_open(path, flags, *args, **kwargs)
        opened.append(path)
        if replacement_time == "before_open":
            return original_open(secret, flags, *args, **kwargs)
        fd = original_open(path, flags, *args, **kwargs)
        ordinary.unlink()
        ordinary.symlink_to(secret)
        return fd

    monkeypatch.setattr(os, "open", swap_at_open)
    if replacement_time == "before_open":
        with pytest.raises(files.FileBrowserError) as failure:
            files.file_content(str(ordinary))
        assert failure.value.status_code == 403
    else:
        assert files.file_content(str(ordinary)).data == b"ordinary"
    assert len(opened) == 1


def test_copy_rejects_private_object_substituted_at_open_before_creating_output(store, tmp_path, monkeypatch):
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_bytes(b"synthetic-private-marker")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_bytes(b"ordinary")
    destination = tmp_path / "export.txt"
    original_open = os.open

    def substitute_private_object(path, flags, *args, **kwargs):
        return original_open(secret if Path(path) == ordinary else path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", substitute_private_object)
    with pytest.raises(files.FileBrowserError) as failure:
        files.copy_path(str(ordinary), str(destination))
    assert failure.value.status_code == 403
    assert not destination.exists()
    assert not list(tmp_path.glob(f"{files._COPY_TEMP_PREFIX}*"))
    assert ordinary.read_bytes() == b"ordinary"


@pytest.mark.parametrize("operation", ["copy", "exdev"])
def test_ordinary_large_directory_copy_and_cross_device_move_keep_working(store, tmp_path, monkeypatch, operation):
    from core import file_browser_service as files

    source = tmp_path / "ordinary-tree"
    for index in range(40):
        directory = source / str(index) / "nested"
        directory.mkdir(parents=True)
        for child in range(10):
            (directory / f"{child}.txt").write_bytes(b"ordinary")
    destination = tmp_path / "export"
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    if operation == "exdev":
        monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
        # A move does not inherit the copy endpoint's optional size limit.
        monkeypatch.setenv(files._COPY_TOTAL_SIZE_CAP_ENV, "0")
    action = files.copy_path if operation == "copy" else files.move_path
    assert action(str(source), str(destination))["ok"]
    assert len(list(destination.rglob("*.txt"))) == 400
    assert all(path.read_bytes() == b"ordinary" for path in destination.rglob("*.txt"))
    assert source.exists() == (operation == "copy")
    assert not list(tmp_path.glob(f"{files._COPY_TEMP_PREFIX}*"))


@pytest.mark.parametrize("operation", ["copy", "exdev"])
@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("link_kind", ["hardlink", "symlink-file", "symlink-directory"])
def test_recursive_export_refuses_private_descendants_without_publishing_or_removing_source(
    store, tmp_path, monkeypatch, operation, overwrite, link_kind,
):
    """Even small deeply nested aliases cannot be laundered into a new public inode."""
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_bytes(b"synthetic-private-marker")
    source = tmp_path / "public"
    nested = source / "one" / "two"
    nested.mkdir(parents=True)
    (source / "ordinary.txt").write_bytes(b"ordinary")
    alias = nested / "private-alias"
    if link_kind == "hardlink":
        os.link(secret, alias)
    else:
        target = secret if link_kind == "symlink-file" else secret.parent
        alias.symlink_to(target, target_is_directory=link_kind == "symlink-directory")
    destination = tmp_path / "export"
    if overwrite:
        destination.mkdir()
        (destination / "original.txt").write_bytes(b"original")
    monkeypatch.setenv(files._COPY_TOTAL_SIZE_CAP_ENV, "1024")
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    if operation == "exdev":
        monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
    action = files.copy_path if operation == "copy" else files.move_path
    with pytest.raises(files.FileBrowserError) as failure:
        action(str(source), str(destination), overwrite=overwrite)
    assert failure.value.status_code == 403
    assert secret.read_bytes() == b"synthetic-private-marker"
    assert source.is_dir() and alias.exists()
    assert (source / "ordinary.txt").read_bytes() == b"ordinary"
    if overwrite:
        assert [entry.name for entry in destination.iterdir()] == ["original.txt"]
        assert (destination / "original.txt").read_bytes() == b"original"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(f"{files._COPY_TEMP_PREFIX}*"))
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


@pytest.mark.parametrize("operation", ["copy", "exdev"])
def test_copy_checks_again_after_measurement_and_discards_partial_stage(
    store, tmp_path, monkeypatch, operation,
):
    """A measured tree is not authority to consume subsequently replaced descendants."""
    from core import file_browser_service as files

    secret = store.path.parent / "synthetic.txt"
    secret.write_bytes(b"synthetic-private-marker")
    source = tmp_path / "public"
    source.mkdir()
    (source / "ordinary.txt").write_bytes(b"ordinary")
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "original.txt").write_bytes(b"original")
    source_identity = source.stat()
    measure_name = "_measure_copy_directory_fd" if os.name == "posix" else "_measure_copy_directory_path"
    original_measure = getattr(files, measure_name)
    added = []

    def inject_after_measurement(path, *args, **kwargs):
        size = original_measure(path, *args, **kwargs)
        info = os.fstat(path) if isinstance(path, int) else Path(path).stat()
        if os.path.samestat(info, source_identity) and not added:
            os.link(secret, source / "late-private.txt")
            added.append(True)
        return size

    monkeypatch.setattr(files, measure_name, inject_after_measurement)
    original_rename = files._os_rename_noreplace

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Synthetic cross-device move")
        return original_rename(src, dst)

    if operation == "exdev":
        monkeypatch.setattr(files, "_os_rename_noreplace", cross_device)
    action = files.copy_path if operation == "copy" else files.move_path
    with pytest.raises(files.FileBrowserError) as failure:
        action(str(source), str(destination), overwrite=True)
    assert failure.value.status_code == 403 and added
    assert (source / "ordinary.txt").read_bytes() == b"ordinary"
    assert (source / "late-private.txt").read_bytes() == secret.read_bytes()
    assert [entry.name for entry in destination.iterdir()] == ["original.txt"]
    assert not list(tmp_path.glob(f"{files._COPY_TEMP_PREFIX}*"))
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_inaccessible_public_link_does_not_hide_other_listing_or_search_results(
    store, tmp_path, monkeypatch, error_number,
):
    """A target-specific failure refuses that entry, not the traversal's snapshot."""
    from core import file_browser_service as files
    from core.mobile_storage_boundary import MobileStorageBoundary

    public = tmp_path / "public"
    public.mkdir()
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    (restricted / "unreadable.txt").write_text("unreadable")
    bad = public / "000-denied.txt"
    bad.symlink_to(restricted / "unreadable.txt")
    good = public / "public-match.txt"
    good.write_text("public sentinel")
    original_stat = os.stat

    def permission_denied(path, *args, **kwargs):
        # chmod alone cannot reproduce TCC/ACL errors on Windows or as root.
        if not isinstance(path, int) and Path(path).is_relative_to(restricted):
            raise PermissionError(error_number, "Synthetic protected location")
        return original_stat(path, *args, **kwargs)

    restricted.chmod(0)
    try:
        monkeypatch.setattr(os, "stat", permission_denied)
        boundary = MobileStorageBoundary()
        assert boundary.is_private_path(bad)
        assert not boundary.is_private_path(good)
        assert {entry["name"] for entry in files.list_directory(str(public))["entries"]} == {good.name}
        assert {hit["rel"] for hit in files.search_names(str(public), "match")["results"]} == {good.name}
        assert {hit["rel"] for hit in files.search(str(public), "sentinel")["results"]} == {good.name}
    finally:
        restricted.chmod(0o700)


@pytest.mark.parametrize("failure", ["root", "scan"])
def test_unverifiable_private_snapshot_refuses_every_later_entry(store, tmp_path, monkeypatch, failure):
    from core.mobile_storage_boundary import MobileStorageBoundary

    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("ordinary")
    second.write_text("ordinary")
    original_stat, original_scandir = os.stat, os.scandir
    broken = [True]

    def root_stat(path, *args, **kwargs):
        if broken[0] and not isinstance(path, int) and Path(path) == store.path.parent:
            raise PermissionError(errno.EACCES, "Synthetic private root failure")
        return original_stat(path, *args, **kwargs)

    def private_scan(path):
        if broken[0] and Path(path) == store.path.parent:
            raise PermissionError(errno.EACCES, "Synthetic private snapshot failure")
        return original_scandir(path)

    monkeypatch.setattr(os, "stat", root_stat if failure == "root" else original_stat)
    monkeypatch.setattr(os, "scandir", private_scan if failure == "scan" else original_scandir)
    boundary = MobileStorageBoundary()
    assert boundary.is_private_path(first)
    broken[0] = False
    assert boundary.is_private_path(second)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS firmlink with an absent private root")
def test_absent_private_root_cannot_be_preplanted_through_a_firmlink(tmp_path, monkeypatch):
    from config import paths
    from core import file_browser_service as files
    from core.mobile_storage_boundary import MobileStorageBoundary

    state = tmp_path / "future-home" / "state"
    monkeypatch.setattr(paths, "get_state_dir", lambda: state)
    state.parent.mkdir(parents=True, exist_ok=True)
    alias_home = Path("/System/Volumes/Data" + os.path.realpath(state.parent))
    assert os.path.samefile(alias_home, state.parent)
    initial = MobileStorageBoundary()
    alias_missing_state = alias_home / state.name
    assert initial.is_private_path(alias_missing_state / "mobile" / "mobile.sqlite3")
    assert initial.is_private_path(alias_missing_state, include_ancestors=True)
    assert not initial.is_private_path(alias_home / "ordinary-neighbor")
    assert not state.exists()
    state.mkdir(parents=True, exist_ok=True)
    private = state / "mobile"
    assert not private.exists()
    canonical_state = Path(os.path.realpath(state))
    alias_state = Path("/System/Volumes/Data" + str(canonical_state))
    assert os.path.samefile(alias_state, state)
    alias = alias_state / private.name
    source = tmp_path / "synthetic-source"
    source.mkdir()
    with sqlite3.connect(source / "mobile.sqlite3") as conn:
        conn.execute("PRAGMA user_version=1")
        conn.execute("CREATE TABLE synthetic_fixture (id TEXT)")

    boundary = MobileStorageBoundary()
    assert boundary.is_private_path(alias)
    assert boundary.is_private_path(alias / "new-directory" / "new-file")
    assert boundary.is_private_path(alias_state, include_ancestors=True)
    assert not private.exists()  # Inspection must not create the root.
    for operation in (
        lambda: files.make_directory(str(alias)),
        lambda: files.copy_path(str(source), str(alias)),
        lambda: files.move_path(str(source), str(alias)),
        lambda: files.write_file(str(alias / "mobile.sqlite3"), "synthetic"),
    ):
        with pytest.raises(files.FileBrowserError, match="Permission denied"):
            operation()
        assert not private.exists() and source.exists()
    ordinary = alias_state / "ordinary-neighbor"
    assert not boundary.is_private_path(ordinary)
    files.make_directory(str(ordinary))
    files.write_file(str(ordinary / "ordinary.txt"), "ordinary")
    assert files.file_content(str(ordinary / "ordinary.txt")).data == b"ordinary"
    assert not private.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS filesystem identity aliases")
@pytest.mark.parametrize("kind", ["firmlink", "inode"])
def test_macos_identity_aliases_cannot_read_export_or_mutate_private_health(
    store, mobile_http, tmp_path, kind,
):
    from urllib.parse import quote
    from core import file_browser_service as files
    from core.mobile_storage_boundary import is_mobile_private_path
    from storage import media_service
    from storage.db import get_cached_sqlite_engine

    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    client, _ = mobile_http

    def alias(path):
        canonical = Path(os.path.realpath(path))
        if kind == "firmlink":
            result = Path("/System/Volumes/Data" + str(canonical))
        else:
            info = canonical.stat()
            result = Path(f"/.vol/{info.st_dev}/{info.st_ino}")
        assert result.exists() and os.path.samefile(canonical, result)
        return result

    db, directory, ancestor = alias(store.path), alias(store.path.parent), alias(store.path.parent.parent)
    assert is_mobile_private_path(db)
    assert is_mobile_private_path(directory)
    assert is_mobile_private_path(ancestor, include_ancestors=True)
    response = client.get("/api/files/content?path=" + quote(str(db), safe=""))
    assert response.status_code == 403 and "987654321" not in response.text
    for operation in (
        lambda: files.list_directory(str(directory), show_hidden=True),
        lambda: files.copy_path(str(db), str(tmp_path / "exported.db")),
        lambda: files.copy_path(str(ancestor), str(tmp_path / "exported-state")),
        lambda: files.move_path(str(directory), str(tmp_path / "moved-mobile")),
        lambda: files.write_file(str(directory / "new.txt"), "synthetic"),
        lambda: files.write_file(str(db), "replacement"),
        lambda: files.delete_path(str(directory), recursive=True),
    ):
        with pytest.raises(files.FileBrowserError):
            operation()
    if kind == "firmlink":
        listing = files.list_directory(str(ancestor), show_hidden=True)
        assert "mobile" not in {entry["name"] for entry in listing["entries"]}
    with get_cached_sqlite_engine().begin() as conn:
        with pytest.raises(ValueError, match="Private mobile storage"):
            media_service.register(conn, scope_id=None, session_id=None, kind="file",
                                   source="test", local_path=str(db))
    assert ui_server._is_show_page_runtime_denied_path(f"@fs{db}", session_id="ses_fake")
    assert store.path.exists() and not (tmp_path / "exported.db").exists()


def test_unknown_expired_and_used_pairing_codes_do_not_query_live_permissions(
    store, mobile_http, monkeypatch, clock,
):
    client, _ = mobile_http
    checks = []
    def current(_store):
        checks.append(True)
        return IDENTITY
    monkeypatch.setattr("vibe.ui_mobile_routes.current_identity", current)
    request, _ = store.create_request("health.connect", IDENTITY, names=["steps"])
    pairing = store.issue_pairing(request["id"], IDENTITY)
    payload = {"pairing_code": "unknown", "device_name": "假设备", "platform": "ios",
               "app_version": "1", "metrics": ["steps"]}
    route = "/api/mobile/device/v1/pair"
    assert client.post(route, json=payload).status_code == 401
    assert checks == []
    payload["pairing_code"] = pairing["pairing_code"]
    assert client.post(route, json=payload).status_code == 200
    assert len(checks) == 1
    assert client.post(route, json=payload).status_code == 401
    assert len(checks) == 1
    other, _ = store.create_request("health.connect", IDENTITY, names=["steps"])
    expiry = store.issue_pairing(other["id"], IDENTITY)
    clock[0] += timedelta(minutes=5)
    payload["pairing_code"] = expiry["pairing_code"]
    assert client.post(route, json=payload).status_code == 401
    assert len(checks) == 1


def test_invalid_callback_and_notification_failure_release_waiter(store, monkeypatch, capsys):
    """Failure before polling must leave a future callback free to continue."""
    monkeypatch.setattr(cli_mobile, "current_identity", lambda _store: IDENTITY)
    args = cli.build_parser().parse_args([
        "mobile", "health", "connect", "--callback-session-id", "ses_missing",
    ])
    assert cli_mobile.cmd_mobile(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_session_id"
    def failed_card(*_args):
        raise MobileError("request_notification_unavailable", 503)
    monkeypatch.setattr(cli_mobile, "publish_request_card", failed_card)
    args = cli.build_parser().parse_args(["mobile", "health", "connect", "--wait", "1", "--no-callback"])
    assert cli_mobile.cmd_mobile(args) == 1
    with store.transaction() as conn:
        assert conn.execute("SELECT waiter_id FROM requests").fetchone()[0] is None


@pytest.mark.parametrize("operation", ["health.connect", "health.refresh"])
@pytest.mark.parametrize("failed_step", ["publish_request_card", "notify_phone"])
def test_initial_delivery_failure_cancels_only_new_request_and_retry_can_deliver(
    store, monkeypatch, operation, failed_step,
):
    """A concurrent coalesced caller cannot cancel or strand the creator's request."""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    if operation == "health.refresh":
        pair_device(store)
    entered, release = threading.Event(), threading.Event()
    calls = []
    failed_request = []

    def card(request, _session):
        calls.append(("publish_request_card", request["id"]))
        fail_if_selected("publish_request_card", request)

    def phone(_store, request, _identity, **_kwargs):
        calls.append(("notify_phone", request["id"]))
        fail_if_selected("notify_phone", request)

    def fail_if_selected(step, request):
        if step == failed_step and not failed_request:
            failed_request.append(request["id"])
            entered.set()
            assert release.wait(5)
            raise MobileError("request_notification_unavailable", 503)

    monkeypatch.setattr(cli_mobile, "publish_request_card", card)
    monkeypatch.setattr(cli_mobile, "notify_phone", phone)
    monkeypatch.setattr(cli_mobile, "_callback", lambda _args: "ses_synthetic")
    initial_args = SimpleNamespace(wait=1)
    retry_args = SimpleNamespace(wait=0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cli_mobile._request, store, operation, IDENTITY, initial_args)
        try:
            assert entered.wait(5)
            coalesced = cli_mobile._request(store, operation, IDENTITY, retry_args)
            assert coalesced["id"] == failed_request[0] and coalesced["state"] == "queued"
            # Coalescing never reruns delivery or changes continuation ownership.
            assert sum(step == failed_step for step, _ in calls) == 1
        finally:
            release.set()
        with pytest.raises(MobileError, match="request_notification_unavailable"):
            future.result(timeout=5)
    assert store.show_request(failed_request[0])["state"] == "canceled"
    assert store.pending_callbacks() == []
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (failed_request[0],)).fetchone()
        assert row["waiter_id"] is None and row["callback_state"] == "consumed"
    retry = cli_mobile._request(store, operation, IDENTITY, retry_args)
    assert retry["id"] != failed_request[0] and retry["state"] == "queued"
    assert ("publish_request_card", retry["id"]) in calls
    assert ("notify_phone", retry["id"]) in calls


def test_native_health_push_filters_owner_and_keeps_budget_across_endpoint_rotation(
    store, mobile_http, monkeypatch, clock,
):
    from core import web_push
    from storage import remote_access_authorization_service as authorizations, web_push_service
    from storage.db import get_cached_sqlite_engine

    engine = get_cached_sqlite_engine()
    for subject, state, owner, email in (
        ("owner", "current", True, IDENTITY["owner"]),
        ("viewer", "current", False, "viewer@example.test"),
        ("stale", "revoked", True, IDENTITY["owner"]),
        ("wrong-account", "current", True, "wrong@example.test"),
    ):
        authorizations.upsert_scoped(
            reference=None, instance_id=IDENTITY["id"], subject=subject, email=email,
            scope_kind="instance", scope_ref=IDENTITY["id"], authorization_state=state,
            last_checked_at=1, updated_at=1,
            claims={"email": email, "vibe_instance_id": IDENTITY["id"],
                    "vibe_instance_role": "owner" if owner else "viewer",
                    "vibe_instance_access_source": "owner" if owner else "allowlist"},
        )
        with engine.begin() as conn:
            web_push_service.upsert_subscription(
                conn, user_key=f"remote:{subject}", device_id=subject,
                payload={"endpoint": "https://avibe.bot/api/push/apns/" + subject[0] * 43,
                         "keys": {"p256dh": "fake-key", "auth": "fake-auth"}},
            )
    with engine.begin() as conn:
        web_push_service.upsert_subscription(
            conn, user_key="remote:owner", device_id="web-browser",
            payload={"endpoint": "https://browser.example.test/push",
                     "keys": {"p256dh": "fake-key", "auth": "fake-auth"}},
        )
    sent = []
    monkeypatch.setattr(web_push, "send_web_push", lambda **kwargs: sent.append(kwargs))
    request, _ = store.create_request("health.connect", IDENTITY)
    for _ in range(4):
        mobile_health.notify_phone(store, request, IDENTITY, caller="synthetic Agent")
    assert len(sent) == 3
    assert {row["subscription"]["user_key"] for row in sent} == {"remote:owner"}
    assert all(row["payload"]["kind"] == "health.request" for row in sent)
    with engine.begin() as conn:
        web_push_service.upsert_subscription(
            conn, user_key="remote:owner", device_id="owner",
            payload={"endpoint": "https://avibe.bot/api/push/apns/" + "z" * 43,
                     "keys": {"p256dh": "fake-key", "auth": "fake-auth"}},
        )
    mobile_health.notify_phone(store, request, IDENTITY, caller="synthetic Agent")
    assert len(sent) == 3
    clock[0] += timedelta(hours=1)
    mobile_health.notify_phone(store, request, IDENTITY, caller="synthetic Agent")
    assert len(sent) == 4 and "987654321" not in json.dumps(sent)
