"""The local UI listener may only acknowledge an outstanding helper from the same root."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import os
import subprocess

import httpx
import pytest

from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.ui_server_test_helpers import csrf_headers
from vibe import authorization, desktop_claim_admission as gate, desktop_local_instance, ui_server

ORIGIN = "http://127.0.0.1:5123"
ROUTE = "/api/desktop/claim/v1/admit"


@pytest.fixture
def client(pairing_host, monkeypatch):
    monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda: None)
    return ui_server.app.test_client()


def test_listener_can_ack_once_but_only_live_helper_can_consume(client, pairing_host, tmp_path, monkeypatch):
    headers = csrf_headers(client, ORIGIN)

    def post(challenge, **extra):
        return client.post(ROUTE, base_url=ORIGIN, json={"challenge": challenge, **extra}, headers=headers)

    assert post("c" * 64).status_code == 409
    assert not gate._slot().parent.exists()
    permit = gate.HelperPermit.register(ORIGIN)
    slot = gate._slot()
    saved = slot.read_text()
    assert permit.challenge not in saved and permit.challenge not in repr(permit)
    assert json.loads(saved) == {
        "phase": "pending",
        "origin": ORIGIN,
        "until": json.loads(saved)["until"],
        "digest": hashlib.sha256(permit.challenge.encode()).hexdigest(),
    }
    assert not permit.consume()  # a synthetic 2xx from a different listener would not help
    with monkeypatch.context() as p:
        p.setenv("AVIBE_HOME", str(tmp_path / "another-root"))
        assert not gate.acknowledge(ORIGIN, permit.challenge)
        assert not gate._slot().parent.exists()
    assert post("c" * 64).status_code == 409
    assert post(permit.challenge, kind="new").status_code == 400
    res = post(permit.challenge)
    assert res.status_code == 204 and not res.content and res.headers["Cache-Control"] == "no-store"
    assert post(permit.challenge).status_code == 409
    assert permit.consume() and not permit.consume()
    assert post(permit.challenge).status_code == 409
    assert not desktop_local_instance._identity_path().exists() and not pairing_host[1]


@pytest.mark.parametrize(
    "case",
    [
        "nonloopback_peer",
        "dns_host",
        "other_port",
        "nonliteral_origin",
        "referer_only",
        "forwarded",
        "empty_forwarded",
        "represented",
        "member",
        "missing_csrf",
        "mismatched_csrf",
        "no_cookie",
        "not_json",
        "query",
    ],
)
def test_full_middleware_refusals_leave_permit_pending(client, case, monkeypatch):
    permit = gate.HelperPermit.register(ORIGIN)
    origin = {"dns_host": "http://localhost:5123", "other_port": "http://127.0.0.1:5124"}.get(case, ORIGIN)
    headers = csrf_headers(client, origin)
    headers.update(
        {
            "forwarded": {"Forwarded": "for=127.0.0.1"},
            "empty_forwarded": {"X-Forwarded-Host": ""},
            "represented": {"Authorization": "Bearer fixture"},
            "nonliteral_origin": {"Origin": origin + "/"},
            "not_json": {"Content-Type": "text/plain"},
            "mismatched_csrf": {"X-Vibe-CSRF-Token": "wrong"},
        }.get(case, {})
    )
    if case == "referer_only":
        headers["Referer"] = headers.pop("Origin")
    if case == "member":
        monkeypatch.setattr(
            authorization, "instance_owner_context", lambda: authorization.AuthorizationContext(instance_role="member")
        )
    if case == "missing_csrf":
        headers.pop("X-Vibe-CSRF-Token")
    if case == "no_cookie":
        client = ui_server.app.test_client()
    r = client.post(
        ROUTE + ("?test=1" if case == "query" else ""),
        base_url=origin,
        environ_base={"REMOTE_ADDR": "192.0.2.4" if case == "nonloopback_peer" else "127.0.0.1"},
        json={"challenge": permit.challenge},
        headers=headers,
    )
    assert r.status_code in (400, 403, 409) and not permit.consume()
    assert permit.challenge.encode() not in r.content and r.headers["Cache-Control"] == "no-store"
    assert gate.acknowledge(ORIGIN, permit.challenge) and permit.consume()


@pytest.mark.asyncio
async def test_network_ingress_never_admits_even_if_it_delivers_over_loopback(client):
    from vibe.direct_ingress import DIRECT_INGRESS_SCOPE_KEY

    permit = gate.HelperPermit.register(ORIGIN)

    async def incoming(scope, receive, send):
        await ui_server.app({**scope, DIRECT_INGRESS_SCOPE_KEY: {"peer": "192.0.2.5"}}, receive, send)

    transport = httpx.ASGITransport(app=incoming, client=("127.0.0.1", 4500))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as c:
        r = await c.post(
            ROUTE,
            json={"challenge": permit.challenge},
            headers={"Origin": ORIGIN, "X-Vibe-CSRF-Token": "fixture", "Cookie": "vibe_csrf_token=fixture"},
        )
    assert r.status_code >= 400 and not permit.consume()


def test_close_expiry_and_late_cleanup_only_retire_the_same_generation(pairing_host, monkeypatch):
    now = 1_000_000_000_000
    monkeypatch.setattr(gate, "monotonic_ns", lambda: now)
    first = gate.HelperPermit.register(ORIGIN)
    with pytest.raises(gate.AdmissionUnavailable):
        gate.HelperPermit.register(ORIGIN)
    assert gate.acknowledge(ORIGIN, first.challenge)
    now += 15_000_000_001
    assert not first.consume()
    second = gate.HelperPermit.register(ORIGIN)
    assert not gate.acknowledge(ORIGIN, first.challenge)
    first.close()
    assert gate.acknowledge(ORIGIN, second.challenge)
    second.close()
    assert not second.consume()
    third = gate.HelperPermit.register(ORIGIN)
    second.close()
    third.close()


def _other_process_ack(pipe):
    with pipe:
        origin, challenge = pipe.recv()
        pipe.send(gate.acknowledge(origin, challenge))


def test_one_winner_across_service_and_helper_processes(pairing_host):
    p = gate.HelperPermit.register(ORIGIN)
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_other_process_ack, args=(child,))
    worker.start()
    child.close()
    try:
        parent.send((ORIGIN, p.challenge))
        ours = gate.acknowledge(ORIGIN, p.challenge)
        assert parent.poll(10) and sorted([ours, parent.recv()]) == [False, True]
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: p.consume(), range(2))) == [False, True]
    finally:
        parent.close()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert worker.exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL fixture")
@pytest.mark.parametrize("entry", ["directory", "slot", "lock"])
def test_widened_windows_dacl_cannot_admit_or_replace(pairing_host, entry):
    p = gate.HelperPermit.register(ORIGIN)
    slot = gate._slot()
    target = {"directory": slot.parent, "slot": slot, "lock": slot.with_name("slot.lock")}[entry]
    subprocess.run(["icacls", str(target), "/grant", "*S-1-1-0:R"], capture_output=True, check=True)
    assert not gate.acknowledge(ORIGIN, p.challenge)
    with pytest.raises(gate.AdmissionUnavailable):
        gate.HelperPermit.register(ORIGIN)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode fixture; Windows checks actual owner and DACL")
@pytest.mark.parametrize("entry", ["directory", "slot", "lock", "symlink"])
def test_untrusted_private_state_is_not_used_or_repaired(pairing_host, entry):
    p = gate.HelperPermit.register(ORIGIN)
    slot = gate._slot()
    if entry == "symlink":
        target = slot.with_name("prior")
        slot.rename(target)
        slot.symlink_to(target)
    elif entry == "directory":
        slot.parent.chmod(0o755)
    else:
        (slot if entry == "slot" else slot.with_name("slot.lock")).chmod(0o644)
    assert not gate.acknowledge(ORIGIN, p.challenge)
    with pytest.raises(gate.AdmissionUnavailable):
        gate.HelperPermit.register(ORIGIN)
