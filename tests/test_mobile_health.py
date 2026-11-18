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
    projection["access"]["entries"] = [{"kind": "email", "value": "other@example.test", "role": "viewer"}]
    denied = client.put("/api/mobile/device/v1/health/days", json=upload, headers=auth)
    assert denied.status_code == 403 and denied.get_json()["code"] == "instance_not_eligible"
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM health_days").fetchone()[0] == 0


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
