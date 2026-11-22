"""Contract peer for the personal Runtime gates; never contacts a real account or connector."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import threading
from uuid import uuid4

import pytest

from config import paths
from config.v2_config import V2Config
from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.ui_server_test_helpers import csrf_headers
from vibe import desktop_local_instance as local, personal_claims as claims, remote_access, ui_server
from vibe.desktop_claim_admission import ClaimHome
from vibe.personal_claim_client import local_origin


def _desktop_claim(**kwargs):
    # Machine-only tests; the pipe suite exercises the real admission handoff.
    root = paths.physical_home(paths.get_vibe_remote_dir())
    info = root.stat()
    return claims.DesktopClaim(home=ClaimHome(root, (info.st_dev, info.st_ino)), **kwargs)


class Peer:
    def __init__(self):
        self.calls = []
        self.outcomes = []
        self.delays = []
        self.challenges = []
        self.code = "ABCD234567"
        self.account = {"subject": "owner-id", "email": "actual@fixture.test"}
        self.hostname = "fixture.avibe.bot"
        self.expiry = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        self.key = "fixture-claim-pairing-material"
        self.result = None
        self.start_fields = {}

    def connection(self, *_args):
        return self

    def connect(self):
        pass

    def close(self):
        pass

    def request(self, method, path, body, headers):
        assert method == "POST" and "?" not in path
        assert not {"authorization", "origin", "cookie"}.intersection(k.lower() for k in headers)
        d = json.loads(body)
        self.calls.append((path, d))
        if path == "/api/v1/claims":
            self.id = str(uuid4())
            self.challenges.append(d["code_challenge"])
            self.result = (201, {"claim_id": self.id, "user_code": self.code, "user_url": f"https://avibe.bot/connect/{self.code}",
                                 "expires_at": self.expiry, "poll_interval_seconds": 5, **self.start_fields}, None)
        else:
            assert path == "/api/v1/claims/redeem" and d["claim_id"] == self.id
            verifier = d["code_verifier"]
            assert len(base64.urlsafe_b64decode(verifier + "=")) >= 32
            assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == self.challenges[-1]
            if self.outcomes:
                self.result = self.outcomes.pop(0)
            elif "accepted_subject" in d:
                self.result = (200, {"status": "redeemed", "account": self.account,
                                     "instance": {"id": "inst_A", "hostname": self.hostname, "slug": "fixture",
                                                  "public_url": "https://" + self.hostname},
                                     "pairing": {"pairing_key": self.key, "expires_at": self.expiry}}, None)
            elif d.get("decline"):
                self.result = (200, {"status": "declined"}, None)
            else:
                self.result = (200, {"status": "granted", "account": self.account, "hostname": self.hostname}, None)

    def getresponse(self):
        if isinstance(self.result, Exception):
            raise self.result
        self.status, self.data, self.hint = self.result
        return self

    def getheader(self, name):
        assert name == "Retry-After"
        return self.hint

    def read(self, n):
        return json.dumps(self.data).encode()[:n]

    @property
    def decisions(self):
        return [d.get("accepted_subject", "decline") for _, d in self.calls if "accepted_subject" in d or "decline" in d]


@pytest.fixture
def claim_peer(pairing_host, monkeypatch):
    peer = Peer()
    monkeypatch.setattr(remote_access, "_validated_backend_connection", peer.connection)
    monkeypatch.setattr(remote_access, "_validated_backend_proxy_url", lambda _: None)
    monkeypatch.setattr(claims._Flow, "_wait", lambda self, delay, **kw: peer.delays.append(delay))
    monkeypatch.setattr(claims, "redirect_claims", claims.RedirectClaims())
    return peer


@pytest.fixture
def service_adoptions(pairing_host):
    from vibe import service_claim_binding

    calls = []

    def activate():
        calls.append(V2Config.load().remote_access.vibe_cloud.instance_id)
        return True

    with service_claim_binding.serving(activate):
        yield calls


@pytest.fixture(params=["loopback", "private_network"])
def owner_browser(request, claim_peer, monkeypatch):
    """The initiating owner's browser: on this machine, or another computer on an admitted home network."""
    if request.param == "loopback":
        return "http://localhost", {}
    config = V2Config.load()
    config.ui.setup_host = "192.168.2.3"
    config.save()
    # Once bound, the tunnel's wildcard bind also requires the peer on the setup host's interface subnet.
    # Pin that interface instead of reading this machine's.
    monkeypatch.setattr(ui_server, "_local_interface_network", lambda *_a, **_k: ipaddress.ip_network("192.168.2.0/24"))
    return "http://192.168.2.3:15131", {"REMOTE_ADDR": "192.168.2.4"}


@pytest.mark.parametrize("mode,expected,approve,decision", [
    ("desktop", "other-id", None, "decline"), ("link", "other-id", None, "decline"),
    ("link", None, False, "decline"), ("link", None, 1, "decline"),
    ("link", None, True, "owner-id"), ("desktop", "owner-id", None, "owner-id"),
])
def test_grant_never_suffices_without_the_mode_gate(claim_peer, mode, expected, approve, decision):
    shown, prompts = [], []
    def confirm(email, hostname):
        assert not claim_peer.decisions
        prompts.append((email, hostname))
        return approve
    if mode == "desktop":
        def review(ticket):
            shown.append(ticket["user_url"])
            flow.arm(claim_id=ticket["claim_id"], kind="new", hostname=claim_peer.hostname, existing_instance_id=None)
        flow = _desktop_claim(signed_in_subject=expected, publish_ticket=review)
        result = flow.run()
    else:
        result = claims.run_link(expected_subject=expected, confirm=confirm if expected is None else None, show_link=shown.append)
    assert shown == ["https://avibe.bot/connect/ABCD234567"]
    assert prompts == ([("actual@fixture.test", "fixture.avibe.bot")] if expected is None else [])
    assert claim_peer.decisions == [decision]
    assert result["state"] == ("declined" if decision == "decline" else "bound")
    assert bool(V2Config.load().remote_access.vibe_cloud.instance_id) == (decision != "decline")
    assert claim_peer.key not in json.dumps(result)


@pytest.mark.parametrize("ack,kind,target_id,host,state", [
    (None, "new", None, "different.avibe.bot", "declined"),
    (None, "existing", "inst_A", "fixture.avibe.bot", "unsupported"),
    (1, "existing", "inst_A", "fixture.avibe.bot", "unsupported"),
    ("true", "existing", "inst_A", "fixture.avibe.bot", "unsupported"),
    (True, "existing", "inst_A", "fixture.avibe.bot", "bound"),
    (True, "existing", "another-instance", "fixture.avibe.bot", "ambiguous"),
])
def test_desktop_machine_checks_its_own_ack_reviewed_host_and_receipt(claim_peer, ack, kind, target_id, host, state):
    if ack is not None:
        claim_peer.start_fields["ordinary_decision_readback"] = ack
    seen = []

    def reviewed(ticket):
        seen.append(ticket)
        assert set(ticket) == {"claim_id", "user_code", "user_url", "device_name", "mode", "expires_at", "ordinary_decision_readback"}
        assert ticket["ordinary_decision_readback"] is (ack is True)
        args = dict(kind=kind, hostname=host, existing_instance_id=target_id)
        with pytest.raises(claims.ClaimError, match="decision_changed"):
            flow.arm(claim_id="a-different-attempt", **args)
        if state == "unsupported":
            with pytest.raises(claims.ClaimError, match="local_runtime_upgrade_required"):
                flow.arm(claim_id=ticket["claim_id"], **args)
            flow.cancel()
        else:
            flow.arm(claim_id=ticket["claim_id"], **args)
            with pytest.raises(claims.ClaimError, match="decision_changed"):
                flow.arm(claim_id=ticket["claim_id"], **args)

    flow = _desktop_claim(signed_in_subject="owner-id", publish_ticket=reviewed)
    result = flow.run()
    assert len(seen) == 1
    if state in ("declined", "unsupported"):
        assert result["state"] == "declined" and claim_peer.decisions == ["decline"]
    elif state == "ambiguous":
        assert result["code"] == "fresh_claim_required" and claim_peer.decisions == ["owner-id"]
    else:
        assert result["state"] == "bound" and claim_peer.decisions == ["owner-id"]
    assert bool(V2Config.load().remote_access.vibe_cloud.instance_id) == (state == "bound")


def test_desktop_close_before_arm_never_observes_or_accepts(claim_peer):
    shown = threading.Event()
    messages, outcomes = [], []

    def publish(ticket):
        messages.append(ticket)
        shown.set()

    flow = _desktop_claim(signed_in_subject="owner-id", publish_ticket=publish)
    worker = threading.Thread(target=lambda: outcomes.append(flow.run()))
    worker.start()
    try:
        assert shown.wait(5)
        assert [path for path, _ in claim_peer.calls] == ["/api/v1/claims"]
        flow.cancel()
        with pytest.raises(claims.ClaimError, match="decision_changed"):
            flow.arm(claim_id=messages[0]["claim_id"], kind="new", hostname=claim_peer.hostname, existing_instance_id=None)
    finally:
        flow.cancel()
        worker.join(5)
    assert not worker.is_alive() and outcomes[0]["state"] == "declined"
    assert claim_peer.decisions == ["decline"] and not claims._marker().exists()


def test_desktop_rejects_noncanonical_backend_before_start_or_identity_creation(claim_peer):
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.backend_url = "https://other.example"
    cfg.save()
    flow = _desktop_claim(signed_in_subject="owner-id", publish_ticket=lambda _: pytest.fail("unexpected ticket"))
    assert flow.run()["code"] == "invalid_backend"
    assert not local._identity_path().exists() and not claim_peer.calls


def test_unknown_account_is_never_noninteractively_accepted(claim_peer):
    for kwargs in ({}, {"expected_subject": ""}, {"expected_subject": "owner-id", "confirm": lambda *_: True}):
        assert claims.run_link(show_link=lambda _: None, **kwargs)["code"] == "link_confirmation_required"
    with pytest.raises(claims.ClaimError, match="invalid_request"):
        _desktop_claim(signed_in_subject="", publish_ticket=lambda _: None)
    assert claim_peer.calls == []


@pytest.mark.parametrize("status,upstream,code", [(404, "claim_unavailable", "claim_unavailable"), (409, "taken", "name_taken"),
    (409, "binding_unavailable", "binding_unavailable"), (410, "already_redeemed", "fresh_claim_required"),
    (409, "already_granted", "invalid_claim_response")])
def test_observable_error_is_not_a_reason_to_replace_installation(claim_peer, status, upstream, code):
    installation = local.ensure_installation_id()
    claim_peer.outcomes.append((status, {"error": upstream, "detail": claim_peer.key}, None))
    result = claims.run_link(show_link=lambda _: None, expected_subject="owner-id")
    assert result["code"] == code and "owner-id" not in claim_peer.decisions
    assert local._installation_id() == installation
    assert claim_peer.key not in json.dumps(result)


def test_backoff_and_lost_accept_reply_require_fresh_claim(claim_peer, caplog):
    claim_peer.outcomes.extend([(202, {"status": "pending"}, "17"), (429, {"error": "rate_limited"}, "31"),
        (503, {"error": "unavailable"}, "9"), (200, {"status": "granted", "account": claim_peer.account,
        "hostname": claim_peer.hostname}, None), OSError(claim_peer.key), (410, {"error": "already_redeemed"}, None)])
    result = claims.run_link(show_link=lambda _: None, expected_subject="owner-id")
    assert result["code"] == "fresh_claim_required" and claim_peer.decisions == ["owner-id", "owner-id"]
    assert claim_peer.delays[:3] == [5, 17, 31] and claim_peer.delays[3] >= 9
    assert claims.RedirectClaims().state(claim_peer.id)["code"] == "fresh_claim_required"
    assert claim_peer.key not in caplog.text and claim_peer.key not in json.dumps(result)
    assert remote_access._read_pending_pairing_record() == (None, None)
    client = ui_server.app.test_client()
    previous_id = claim_peer.id
    next_start = _start(client).get_json()
    assert next_start["claim_id"] != previous_id and next_start["previous_claim_expires_at"] == claim_peer.expiry
    path = "/api/remote-access/claims/" + next_start["claim_id"]
    client.post(path + "/cancel", json={}, headers=csrf_headers(client))
    flow = claims.redirect_claims._get(next_start["claim_id"])
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view["state"] == "declined", timeout=3)


def test_pinned_connect_fails_over_without_reposting_an_ambiguous_start(claim_peer, monkeypatch):
    import ipaddress
    from vibe.personal_claim_client import ClaimError, ClaimsClient
    hosts = (ipaddress.ip_address("2606:4700:4700::1111"), ipaddress.ip_address("1.1.1.1"))
    monkeypatch.setattr(remote_access, "_resolve_pairing_backend_addresses", lambda *_: hosts)
    attempts = []
    class Offline:
        def connect(self):
            raise OSError("unavailable address family")
        def close(self):
            pass
    def dial(host, target, _timeout, _proxy):
        assert target.hostname == "avibe.bot"
        attempts.append(host)
        return Offline() if host == str(hosts[0]) else claim_peer
    monkeypatch.setattr(remote_access, "_validated_backend_connection", dial)
    client = ClaimsClient("https://avibe.bot")
    assert client.start(str(uuid4()), "device", "link", "en").id == claim_peer.id
    assert attempts == list(map(str, hosts)) and len(claim_peer.calls) == 1
    attempts.clear()
    monkeypatch.setattr(remote_access, "_validated_backend_connection", lambda host, *_: (attempts.append(host), claim_peer)[1])
    def lost():
        raise OSError("post dispatched")
    monkeypatch.setattr(claim_peer, "getresponse", lost)
    with pytest.raises(ClaimError, match="control_plane_unreachable"):
        client.start(str(uuid4()), "device", "link", "en")
    assert attempts == [str(hosts[0])] and len(claim_peer.calls) == 2


def test_ui_policy_workers_notice_link_pairing_without_ui_restart(claim_peer, monkeypatch):
    # Capture the UI startup worker entry points, not real threads, HTTP or host state.
    workers, requests, acl_reads = [], [], []
    class Worker:
        def __init__(self, *, target, **_):
            self.target = target
        def start(self):
            workers.append(self.target)
    with monkeypatch.context() as startup:
        startup.setattr(remote_access.threading, "Thread", Worker)
        startup.setattr(remote_access, "_AUTHORIZATION_REVISION_POLL_STARTED", False)
        startup.setattr(remote_access, "_RESOURCE_ACL_SYNC_POLL_STARTED", False)
        for _ in range(2):
            remote_access.start_authorization_revision_polling()
            remote_access.start_resource_acl_sync_polling()
        assert len(workers) == 2
    from storage import resource_access_service
    monkeypatch.setattr(remote_access, "_device_json_request", lambda cfg, *args: (requests.append((cfg.remote_access.vibe_cloud.instance_id, *args)), {"authorization_revision": 1})[1])
    monkeypatch.setattr(resource_access_service, "list_resource_organization_ids", lambda: (acl_reads.append(True), [])[1])
    class TickDone(Exception):
        pass
    def stop_tick(_):
        raise TickDone
    def tick():
        with monkeypatch.context() as clock:
            clock.setattr(remote_access.time, "sleep", stop_tick)
            for worker in workers:
                with pytest.raises(TickDone):
                    worker()
    tick()
    assert not requests and not acl_reads
    assert claims.run_link(show_link=lambda _: None, expected_subject="owner-id")["state"] == "bound"
    tick()
    assert requests == [("inst_A", "GET", "authorization-revision")] and acl_reads == [True]


def test_unpublished_accept_continues_only_the_original_decision(claim_peer):
    claim_peer.outcomes.extend([(200, {"status": "granted", "account": claim_peer.account,
        "hostname": claim_peer.hostname}, None), OSError("ambiguous"), (202, {"status": "pending"}, "8")])
    result = claims.run_link(show_link=lambda _: None, expected_subject="owner-id")
    assert result["state"] == "bound" and claim_peer.decisions == ["owner-id"] * 3
    assert all(d == claim_peer.calls[-1][1] for _, d in claim_peer.calls[-3:])
    assert claim_peer.delays[-1] >= 8


def test_valid_browser_return_before_grant_is_not_armed(claim_peer, owner_browser):
    import time
    origin, environ = owner_browser
    client = ui_server.app.test_client()
    body = _start(client, origin, environ).get_json()
    claim_peer.outcomes.append((202, {"status": "pending"}, "5"))
    url = "/remote/claim/callback?claim_id=" + body["claim_id"] + "&state=" + callback_proof(body)
    assert client.get(url, base_url=origin, environ_base=environ).status_code == 303
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not claim_peer.decisions:
        time.sleep(.01)
    assert claim_peer.decisions == ["decline"]
    assert sum(path.endswith("/redeem") and "decline" not in d for path, d in claim_peer.calls) == 1
    _assert_bad_return(client.get(url, base_url=origin, environ_base=environ))


def test_expiry_does_not_invent_a_browser_decision(claim_peer):
    claim_peer.expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    result = claims.run_link(show_link=lambda _: None, expected_subject="owner-id")
    assert result["code"] == "expired" and len(claim_peer.calls) == 1


def test_fresh_s256_but_same_installation_between_claims(claim_peer):
    for _ in range(2):
        assert claims.run_link(show_link=lambda _: None, expected_subject="wrong")["state"] == "declined"
    starts = [v for url, v in claim_peer.calls if url == "/api/v1/claims"]
    assert starts[0]["installation_id"] == starts[1]["installation_id"]
    assert len(set(claim_peer.challenges)) == 2


def test_new_claim_cannot_replace_bound_or_pending_local_state(claim_peer):
    assert claims.run_link(show_link=lambda _: None, expected_subject="owner-id")["state"] == "bound"
    before = len(claim_peer.calls)
    config = V2Config.load()
    identity = config.remote_access.vibe_cloud.instance_secret
    assert claims.run_link(show_link=lambda _: None, expected_subject="owner-id")["code"] == "already_bound"
    assert len(claim_peer.calls) == before and V2Config.load().remote_access.vibe_cloud.instance_secret == identity


def test_other_workflow_cannot_start_while_link_is_waiting(claim_peer):
    ready, finish = threading.Event(), threading.Event()
    def show(_):
        ready.set()
        assert finish.wait(3)
    worker = threading.Thread(target=lambda: claims.run_link(show_link=show, expected_subject="wrong"))
    worker.start()
    try:
        assert ready.wait(3)
        flow = _desktop_claim(publish_ticket=lambda _: None, signed_in_subject="owner-id")
        assert flow.run()["code"] == "claim_in_progress"
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive() and len(claim_peer.challenges) == 1


@pytest.mark.parametrize("origin,allowed", [("http://localhost:5123", True), ("http://127.2.3.4", True),
    ("http://192.168.1.10", True), ("http://172.31.255.255", True), ("http://[fd01::abc]", True), ("http://[::1]", True),
    ("http://127.1", False), ("http://0x7f000001", False), ("http://127.0.0.1.", False), ("https://localhost", False),
    ("http://100.64.1.2", False), ("http://172.32.0.1", False), ("http://[::ffff:127.0.0.1]", False),
    ("http://[fe80::1]", False), ("http://localhost/other", False), ("http://user@localhost", False)])
def test_literal_redirect_origin_contract(origin, allowed):
    assert local_origin(origin) is allowed


def _start(client, origin="http://localhost", environ=None):
    return client.post("/api/remote-access/claims", base_url=origin, environ_base=environ, json={"locale": "en"},
                       headers=csrf_headers(client, origin))


def callback_proof(body):
    state = body["form"]["fields"]["redirect_state"]
    return base64.urlsafe_b64encode(hmac.digest(state.encode(), ("avibe-claim-return:v1:" + body["claim_id"]).encode(), "sha256")).decode().rstrip("=")


def _assert_bad_return(response, code="invalid_return"):
    assert response.status_code == 303
    assert response.headers["Location"] == f"/settings/remote-access?claim_error={code}"
    assert response.headers["Referrer-Policy"] == "no-referrer" and response.headers["Cache-Control"] == "no-store"


def test_cancel_and_return_require_current_browser_and_claim(claim_peer, owner_browser):
    origin, environ = owner_browser
    client, stranger = ui_server.app.test_client(), ui_server.app.test_client()
    response = _start(client, origin, environ)
    assert response.status_code == 201, response.get_json()
    assert claim_peer.calls[0][1]["return_origin"] == origin
    body = response.get_json()
    nonce = response.headers["Set-Cookie"].split(";")[0]
    url = "/remote/claim/callback?claim_id=" + body["claim_id"] + "&state=" + callback_proof(body)
    def back(c, u, **kw):
        return c.get(u, base_url=kw.pop("base_url", origin), environ_base=environ, **kw)
    _assert_bad_return(back(stranger, url))
    # The initiator's cookie at another port of the same admitted host is a different origin.
    _assert_bad_return(back(stranger, url, base_url=origin.split(":15131")[0] + ":15139", headers={"Cookie": nonce}))
    _assert_bad_return(back(client, url + "x"))
    _assert_bad_return(back(client, url + "&state=duplicate"))
    _assert_bad_return(back(client, url.replace(body["claim_id"], str(uuid4()))), "claim_unavailable")
    conflict = _start(stranger, origin, environ)
    assert conflict.status_code == 409
    assert conflict.get_json() == {"state": "error", "code": "claim_in_progress", "claim_id": body["claim_id"], "expires_at": body["expires_at"]}
    assert "avibe_claim_browser" not in str(conflict.headers)
    path = "/api/remote-access/claims/" + conflict.get_json()["claim_id"]
    assert "form" not in client.get(path, base_url=origin, environ_base=environ).get_json()
    cancel = stranger.post(path + "/cancel", base_url=origin, environ_base=environ, json={}, headers=csrf_headers(stranger, origin))
    assert cancel.status_code == 200
    _assert_bad_return(back(client, url))
    assert not any(d.get("accepted_subject") for _, d in claim_peer.calls)


@pytest.mark.parametrize("setup_host,peer,forwarded", [("100.100.1.2", "100.100.1.3", {}),
    ("169.254.10.2", "169.254.10.3", {}), ("192.168.2.3", "192.168.2.4", {"X-Forwarded-For": "192.168.2.9", "X-Forwarded-Host": "192.168.2.3:15131", "X-Forwarded-Proto": "http"})])
def test_admitted_owner_outside_the_redirect_contract_gets_link_required(claim_peer, monkeypatch, setup_host, peer, forwarded):
    config = V2Config.load()
    config.ui.setup_host = setup_host
    config.save()
    if forwarded:
        monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, peer)
    origin = f"http://{setup_host}:15131"
    client = ui_server.app.test_client()
    response = client.post("/api/remote-access/claims", base_url=origin, headers={**csrf_headers(client, origin), **forwarded},
                           environ_base={"REMOTE_ADDR": peer}, json={"locale": "en"})
    assert response.status_code == 409
    assert response.get_json() == {"state": "error", "code": "link_required"}
    assert not claim_peer.calls and not local._identity_path().exists()


def test_redirect_start_has_no_csrf_or_host_exception(claim_peer):
    client = ui_server.app.test_client()
    for extra in ({"Origin": "null"}, {"Origin": "http://foreign.test"}, {"X-Vibe-CSRF-Token": "bad"}, {"Host": "foreign.test"}):
        response = client.post("/api/remote-access/claims", base_url="http://localhost", json={}, headers={**csrf_headers(client), **extra})
        assert response.status_code == (503 if "Host" in extra else 403), (extra, response.status_code)
    for extra in ({"mode": "desktop"}, {"return_origin": "http://localhost"}, {"accepted_subject": "owner-id"}):
        assert client.post("/api/remote-access/claims", json=extra, headers=csrf_headers(client)).status_code == 400
    assert not claim_peer.calls
