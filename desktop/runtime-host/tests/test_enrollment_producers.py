"""AUTH-SETUP-172/173: actual B2 HTTP handlers consumed by exact R1 handlers.

Run with the pinned R1 checkout as cwd/PYTHONPATH and its tests.conftest plugin.
Only transport routing/response timing and external connector startup are
fixture adapters. Every redeem response is produced by the built B2 server.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

import pytest
import requests

from config.v2_config import V2Config
from tests.test_desktop_local_instance import desktop_host as desktop_host
from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access, ui_server

ACCOUNT = "79bba974-c760-47b8-90d9-e8ec11b887d5"


@pytest.fixture(scope="session")
def producer(tmp_path_factory):
    runtime_root = Path(ui_server.__file__).resolve().parent.parent
    expected_runtime = os.environ["DESKTOP_R1_HEAD"]
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=runtime_root, text=True,
    ).strip() == expected_runtime, "R1 consumer head changed"
    assert not subprocess.check_output(["git", "diff", "--name-only"], cwd=runtime_root)
    checkout = Path(os.environ["DESKTOP_B2_CHECKOUT"]).resolve()
    expected = os.environ["DESKTOP_B2_HEAD"]
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert actual == expected, "B2 producer head changed"
    assert not subprocess.check_output(["git", "diff", "--name-only"], cwd=checkout)
    root = tmp_path_factory.mktemp("b2-producer")
    log_path = root / "producer.log"
    ready = queue.Queue()
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["node", "--import", "tsx", "scripts/desktop-enrollment-fixture.ts"],
            cwd=checkout,
            env={"PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def collect():
            for line in process.stdout:
                log.write(line)
                log.flush()
                try:
                    value = json.loads(line)
                    if value.get("event") == "desktop_enrollment_ready":
                        ready.put(value)
                except (ValueError, AttributeError):
                    pass

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            info = ready.get(timeout=60)
            session = requests.Session()
            session.trust_env = False
            info["http"] = session
            credentials = session.get(info["provider"] + "/fixture/account", timeout=10).json()
            info["cookie"] = credentials["cookie"]
            yield info
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            print(f"actual B2 {expected}: {log_path}")


def control(producer, **body):
    response = producer["http"].post(producer["provider"] + "/fixture/control", json=body, timeout=10)
    response.raise_for_status()
    return response.json()


def ensure(producer, prepared):
    local = prepared["local"]
    response = producer["http"].post(
        producer["base"] + "/api/desktop/instances/ensure-local",
        headers={"Origin": "https://avibe.bot", "Cookie": producer["cookie"]},
        json={
            "installation_id": local["installation_id"],
            "operation_id": prepared["operation_id"],
            "device_name": local["device_name"],
            "local_instance_id": None,
        },
        timeout=20,
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def prepare(client):
    local = client.get("/api/desktop/local-instance").get_json()
    result = client.post(
        "/api/desktop/local-instance/prepare",
        json={"account_id": ACCOUNT, "expected_binding_revision": local["binding_revision"]},
        headers=csrf_headers(client),
    )
    assert result.status_code == 200, result.get_json()
    assert result.get_json()["local"]["device_name"] == "小明的 Mac"
    return result.get_json()


def pair_body(prepared, grant):
    local = prepared["local"]
    return {
        "pairing_key": grant["pairing"]["pairing_key"],
        "backend_url": "https://avibe.bot",
        "device_name": local["device_name"],
        "desktop_enrollment": {
            "installation_id": local["installation_id"],
            "operation_id": prepared["operation_id"],
            "account_id": ACCOUNT,
            "expected_binding_revision": local["binding_revision"],
        },
    }


def pair(body):
    client = ui_server.app.test_client()
    return client.post("/api/remote-access/vibe-cloud/pair", json=body, headers=csrf_headers(client))


class Wire:
    def __init__(self, producer):
        self.producer = producer
        self.requests = []
        self.after = lambda _payload, _response: None
        self.transform = lambda payload: payload
        self.lock = threading.Lock()

    def __call__(self, url, payload, **_kwargs):
        assert url == "https://avibe.bot/api/v1/pairing/redeem"
        with self.lock:
            self.requests.append(copy.deepcopy(payload))
        # Mapping only the socket preserves the real control-plane authority.
        request = self.transform(copy.deepcopy(payload))
        response = self.producer["http"].post(
            self.producer["base"] + "/api/v1/pairing/redeem", json=request,
            headers={"Host": "avibe.bot"}, timeout=20,
        )
        assert response.headers["Cache-Control"] == "no-store"
        self.after(payload, response)
        data = response.json()
        if response.status_code >= 400:
            raise remote_access.BackendRequestError(response.status_code, data)
        assert response.status_code == 200, response.text
        return data


@pytest.fixture
def chain(desktop_host, producer, monkeypatch):
    wire = Wire(producer)
    monkeypatch.setattr(remote_access, "_json_request", wire)
    # The fixture holds the external provisioner's exact local Runtime origin.
    config = V2Config.load()
    config.ui.setup_port = 1984
    config.ui.setup_host = "127.0.0.1"
    monkeypatch.setenv("VIBE_UI_PORT", "1984")
    config.save()
    assert remote_access.origin_service_for_pairing() == "http://127.0.0.1:1984"
    prepared = prepare(desktop_host)
    grant = ensure(producer, prepared)
    yield desktop_host, wire, prepared, grant
    control(producer, hold_origin=False)


def test_a2_actual_pending_and_exact_replay_publish_one_device(chain, producer):
    client, wire, prepared, grant = chain
    body = pair_body(prepared, grant)
    before = control(producer)
    control(producer, hold_origin=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(pair, body)
        try:
            deadline = time.monotonic() + 10
            while control(producer)["held"] == 0:
                assert time.monotonic() < deadline
                time.sleep(0.02)
            pending = pair(body)
            assert pending.status_code == 400
            assert pending.get_json() == {
                "ok": False, "error": "pairing_redeem_pending", "status": 409,
                "retry_after_seconds": 1,
                "pairing": {"ok": False, "recoverable": True, "cause": "pairing_redeem_pending"},
            }
            assert not V2Config.load().remote_access.vibe_cloud.instance_id
            assert control(producer)["devices"] == before["devices"]
        finally:
            control(producer, hold_origin=False)
        success = first.result(timeout=20)
    assert success.status_code == 200, success.get_json()
    replay = wire("https://avibe.bot/api/v1/pairing/redeem", wire.requests[0])
    assert replay["instance_id"] == grant["instance"]["id"]
    assert control(producer)["devices"] == before["devices"] + 1
    assert client.get("/api/desktop/local-instance").get_json()["pairing"]["instance_id"] == grant["instance"]["id"]


def test_a3_expired_unused_renews_same_reserved_instance(chain, producer):
    client, _, prepared, grant = chain
    before = control(producer)
    control(producer, pairing_key=grant["pairing"]["pairing_key"], expire_key=True)
    expired = pair(pair_body(prepared, grant))
    assert expired.get_json() == {"ok": False, "error": "pairing_key_expired", "status": 400}
    renewed = ensure(producer, prepared)
    assert renewed["instance"] == grant["instance"]
    assert renewed["pairing"]["pairing_key"] != grant["pairing"]["pairing_key"]
    paired = pair(pair_body(prepared, renewed))
    assert paired.status_code == 200, paired.get_json()
    after = control(producer)
    assert after["instances"] == before["instances"]
    assert after["devices"] == before["devices"] + 1
    assert client.get("/api/desktop/local-instance").get_json()["pairing"]["instance_id"] == grant["instance"]["id"]


@pytest.mark.parametrize("fault", ["missing_receipt", "digest_mismatch"])
def test_a3_actual_uncertainty_preserves_exact_runtime_request(chain, producer, fault):
    _, wire, prepared, grant = chain
    body = pair_body(prepared, grant)

    def lose_reply(_payload, response):
        assert response.status_code == 200
        raise ConnectionError("fixture lost actual successful reply")

    wire.after = lose_reply
    lost = pair(body)
    assert lost.get_json()["error"] == "pairing_redeem_indeterminate"
    saved = remote_access._read_pending_pairing_record()[0]
    before = control(producer)
    wire.after = lambda *_: None
    if fault == "missing_receipt":
        control(producer, pairing_key=body["pairing_key"], drop_receipt=True)
    else:
        wire.transform = lambda payload: {**payload, "device_name": "different digest"}
    retry = pair({**body, "pairing_key": ""})
    assert retry.get_json() == {
        "ok": False, "error": "pairing_redeem_indeterminate", "status": 409,
        "pairing": {"ok": False, "recoverable": True, "cause": "registration_indeterminate"},
    }
    assert remote_access._read_pending_pairing_record()[0] == saved
    refused = pair({**body, "pairing_key": "new-grant-is-not-authorized"})
    assert refused.get_json()["error"] == "local_instance_conflict"
    assert control(producer)["devices"] == before["devices"]


def test_a3_two_actual_old_results_cannot_invalidate_lost_replacement(chain, producer):
    _, wire, prepared, grant = chain
    body = pair_body(prepared, grant)
    old_key = body["pairing_key"]
    control(producer, pairing_key=old_key, expire_key=True)
    arrived = queue.Queue()
    gates = [threading.Event(), threading.Event()]
    count = 0
    lock = threading.Lock()
    replacement_response = []

    def hold(payload, response):
        nonlocal count
        if payload["pairing_key"] == old_key:
            assert response.status_code == 400
            assert response.json() == {"error": "pairing_key_expired"}
            with lock:
                index = count
                count += 1
            arrived.put(index)
            assert gates[index].wait(20)
        elif not replacement_response:
            assert response.status_code == 200
            replacement_response.append(response.json())
            raise ConnectionError("fixture lost actual replacement reply")

    wire.after = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pair, body)
        assert arrived.get(timeout=10) == 0
        second = pool.submit(pair, body)
        assert arrived.get(timeout=10) == 1
        try:
            gates[0].set()
            assert first.result(timeout=10).get_json()["error"] == "pairing_key_expired"
            renewed = ensure(producer, prepared)
            assert renewed["instance"] == grant["instance"]
            lost = pair(pair_body(prepared, renewed))
            assert lost.get_json()["error"] == "pairing_redeem_indeterminate"
            replacement = remote_access._read_pending_pairing_record()[0]
        finally:
            for gate in gates:
                gate.set()
        assert second.result(timeout=10).get_json()["error"] == "pairing_superseded_after_redeem"
    assert remote_access._read_pending_pairing_record()[0] == replacement
    assert pair({**body, "pairing_key": "third-grant"}).get_json()["error"] == "local_instance_conflict"
    recovered = pair({**body, "pairing_key": ""})
    assert recovered.status_code == 200, recovered.get_json()
    assert V2Config.load().remote_access.vibe_cloud.instance_id == replacement_response[0]["instance_id"]
    assert wire.requests[0] == wire.requests[1]
    assert wire.requests[2] == wire.requests[3]
