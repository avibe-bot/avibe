"""Changing this Avibe's address: Host roles, the journal, cleaning and the Owner's list.

Each test names the rule of ``docs/plans/instance-address-change.md`` it holds.
The control plane is a fake of the agreed wire contract; everything on this
side of it is the real code, config file and SQLite state.
"""

import json
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import select

from core.services import settings as settings_service
from storage import migrations
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.mobile_store import MobileStore
from storage.models import (
    device_health_owner_checks,
    instance_address_clearings,
    remote_account_revocations,
    remote_device_sessions,
    web_push_subscriptions,
)
from tests.test_remote_access_vibe_cloud import _config, _session_claims
from tests.ui_server_test_helpers import csrf_headers
from vibe import direct_lifecycle, instance_address, instance_address_responses, remote_access, ui_server

OLD = "old-app.avibe.bot"
NEW = "new-app.avibe.bot"
THIRD = "third-app.avibe.bot"
RELEASE = "2099-12-23T12:00:00Z"
LOCAL = "http://localhost"


class ControlPlane:
    """The agreed ``hostname-state`` / ``hostname-renames`` contract, in memory."""

    def __init__(self, instance_id, active):
        self.instance_id = instance_id
        self.active = active
        self.generation = 7
        self.cleanup = {}
        self.operations = {}
        self.last_operation = None
        self.calls = []
        self.fail = None
        self.refuse = None
        self.pending_answers = 0

    def __call__(self, _config, method, suffix, payload=None, *, timeout=8.0):
        self.calls.append((method, suffix, payload))
        if self.fail is not None:
            raise self.fail
        if suffix == "hostname-state":
            return self.state()
        assert suffix == "hostname-renames"
        return self.rename(payload)

    def state(self):
        return {
            "instance_id": self.instance_id,
            "generation": self.generation,
            "active_hostname": self.active,
            "active_release_at_if_renamed_now": RELEASE,
            "release_delay_seconds": 30 * 86400,
            "cleanup_hostnames": [{"hostname": h, "release_at": r} for h, r in self.cleanup.items()],
            "last_rename_operation_id": self.last_operation,
            "oidc_client_id": "vr_client_123",
        }

    def move(self, target, operation_id):
        previous = self.active
        self.cleanup.pop(target, None)
        self.cleanup[previous] = RELEASE
        self.active = target
        self.generation += 1
        self.last_operation = operation_id
        return previous

    def rename(self, body):
        if body["operation_id"] in self.operations:
            return self.operations[body["operation_id"]]
        if self.pending_answers:
            self.pending_answers -= 1
            return {"status": "pending", "operation_id": body["operation_id"], "retry_after_seconds": 1}
        if self.refuse is not None:
            raise remote_access.BackendRequestError(*self.refuse)
        if body["expected_generation"] != self.generation or body["expected_hostname"] != self.active:
            raise remote_access.BackendRequestError(409, {"error": "binding_changed"})
        target = body["new_prefix"] + "-app.avibe.bot"
        previous = self.move(target, body["operation_id"])
        receipt = {
            "status": "committed",
            "operation_id": body["operation_id"],
            "instance_id": self.instance_id,
            "committed_generation": self.generation,
            "previous_hostname": previous,
            "hostname": target,
            "public_url": f"https://{target}",
            "previous_release_at": RELEASE,
            "oidc_client_id": "vr_client_123",
        }
        self.operations[body["operation_id"]] = receipt
        return receipt


class Clocks:
    def __init__(self):
        self.monotonic = 1000.0
        self.wall = time.time()  # sessions are issued on the real clock

    def advance(self, seconds, *, asleep=False):
        self.wall += seconds
        if not asleep:
            self.monotonic += seconds


@pytest.fixture
def address(monkeypatch):
    """A paired personal instance at ``old-app.avibe.bot`` with a fake control plane."""

    ensure_sqlite_state()
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.public_url = f"https://{OLD}"
    cloud.redirect_uri = f"https://{OLD}/auth/callback"
    cloud.backend_url = "https://issuer.invalid"
    cloud.instance_secret = "test-device-secret"
    cloud.instance_kind = "personal"
    config.save()
    plane = ControlPlane(cloud.instance_id, OLD)
    clocks = Clocks()
    monkeypatch.setattr(remote_access, "_device_json_request", plane)
    monkeypatch.setattr(instance_address, "_monotonic", lambda: clocks.monotonic)
    monkeypatch.setattr(instance_address, "_wall", lambda: clocks.wall)
    monkeypatch.setattr(instance_address, "_sleep", lambda seconds: clocks.advance(seconds))
    monkeypatch.setattr(instance_address, "_FIRST_ANSWER_WAIT_SECONDS", 0.0)
    # avibe.bot has answered for this address before (the address can be changed
    # only then); what follows is a process that has just started.
    monkeypatch.setattr(instance_address, "_BINDING_CACHE", None)
    assert instance_address.refresh_state(force=True)
    plane.calls.clear()
    for name, value in (
        ("_authority", None), ("_quiet", set()), ("_quiet_loaded_for", None), ("_inflight", {}),
        ("_last_read_started", None), ("_retired_at_flip", {}), ("_confirmations", {}),
        ("_BINDING_CACHE", None), ("_hooks", {}),
        ("_last_read_failed", None), ("_known_cleanup", None), ("_binding_failure_logged", None),
        ("_read_refused", False), ("_standing_refusal", None), ("_read_refusal", None), ("_endpoint_absent", None), ("_held_checked_at", None),
    ):
        monkeypatch.setattr(instance_address, name, value)
    instance_address._worker_read_requested.clear()
    for name, value in ():
        monkeypatch.setattr(instance_address, name, value)
    monkeypatch.setattr(ui_server, "_start_instance_address", lambda **_: True)
    return plane, clocks


def binding():
    return instance_address.current_binding()


def sign_in(subject="owner", role="owner"):
    config = settings_service.load_config()
    cookie = remote_access.make_session_cookie(
        config, f"{subject}@test.invalid", subject, session_claims=_session_claims(config, role=role),
        device_headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/100.0 Safari/537.36"},
    )
    return cookie, remote_access.parse_session_identity(config, cookie)["browser_session_id"]


def rename_to(prefix, actor=("loopback",)):
    confirmation = instance_address.preview(prefix, actor)["confirmation_id"]
    status, body = instance_address.change(confirmation, actor)
    assert status == 202 and body["state"] == "pending"
    return confirmation


def run():
    """What the worker thread does, on this thread: read when asked to, then advance the journal."""

    while True:
        read = instance_address._read_when_asked()
        if instance_address._advance_operation() <= 0 and (
            not read or not instance_address._worker_read_requested.is_set()
        ):
            return


def get(host, path="/", **kwargs):
    return ui_server.app.test_client().get(path, base_url=f"https://{host}", **kwargs)


def is_cleaning(response):
    return response.headers.get("Clear-Site-Data") == '"cache", "storage"'


ROUTES = ["/", "/api/session", "/api/csrf-token", "/auth/logout", "/auth/callback?code=x", "/assets/app.js",
          "/p/share1", "/health", "/api/devices", "/api/events", "/remote/claim/callback"]


# --- B2, B7, E1, E8: roles ---------------------------------------------------------


def test_managed_grammar_matches_the_direct_lifecycle():
    assert instance_address.MANAGED_HOSTNAME.pattern == direct_lifecycle._MANAGED_HOSTNAME.pattern


def test_b2_active_name_is_served_and_never_cleaned_whatever_a_state_lists(address):
    plane, _ = address
    plane.cleanup[OLD] = RELEASE  # overlap: the state is unreadable, not a permission
    assert instance_address.refresh_state(force=True) is False
    for path in ROUTES:
        response = get(OLD, path)
        assert not is_cleaning(response), path
        assert "X-Avibe-Address" not in response.headers
    assert get(OLD, "/health").status_code == 200


@pytest.mark.parametrize("spelling", [OLD.upper(), OLD + ".", OLD + ":443", "Old-App.Avibe.Bot.:443"])
def test_b2_e1_a_cleanup_name_gets_one_cleaning_answer_on_every_path(address, spelling):
    plane, _ = address
    rename_to("new")
    run()
    assert binding().active_host == NEW
    for path in ROUTES:
        response = ui_server.app.test_client().get(path, base_url=f"https://{spelling}")
        assert response.status_code == 410, path
        assert is_cleaning(response)
        assert response.headers["X-Avibe-Address"] == "retired"
        assert response.headers["Cache-Control"] == "no-store"
        assert NEW not in response.text
        assert "inst_123" not in response.text
        assert all(NEW not in value for value in response.headers.values())
    posted = ui_server.app.test_client().post("/auth/logout", base_url=f"https://{OLD}", json={})
    assert posted.status_code == 410 and is_cleaning(posted)


def test_b2_cleaning_needs_a_state_read_started_within_sixty_seconds_on_both_clocks(address):
    plane, clocks = address
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    clocks.advance(61)
    stale = get(OLD, "/api/session")
    assert stale.status_code == 503 and not is_cleaning(stale) and "Set-Cookie" not in stale.headers
    plane.fail = None
    clocks.advance(6)
    assert is_cleaning(get(OLD))
    # A laptop that slept: the monotonic clock stood still, the wall clock did not.
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    clocks.advance(120, asleep=True)
    assert not is_cleaning(get(OLD))
    # A wall clock that went backwards is no better.
    plane.fail = None
    clocks.advance(6)
    assert is_cleaning(get(OLD))
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    clocks.wall -= 30
    assert not is_cleaning(get(OLD))


def test_b2_state_reads_are_one_per_five_seconds_under_a_burst(address):
    plane, clocks = address
    rename_to("new")
    run()
    clocks.advance(61)
    plane.calls.clear()
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    for _ in range(20):
        assert get(OLD).status_code == 503
    assert len([call for call in plane.calls if call[1] == "hostname-state"]) == 1


def test_e7_a_slow_control_plane_cannot_stall_requests_on_the_active_name(address):
    """Concurrent requests on an old name share one read; none of them holds up the active name."""

    import asyncio
    import threading

    plane, clocks = address
    rename_to("new")
    run()
    clocks.advance(61)
    plane.calls.clear()
    release = threading.Event()
    original = plane.state

    def slow_state():
        release.wait(timeout=5)
        return original()

    plane.state = slow_state

    async def scenario():
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"served"})

        middleware = ui_server._InstanceAddressRoleMiddleware(app)

        async def request(host):
            sent = []

            async def send(message):
                sent.append(message)

            scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "headers": [(b"host", host.encode())]}
            await middleware(scope, None, send)
            return sent[0]["status"], dict(sent[0]["headers"]).get(b"clear-site-data")

        old = [asyncio.create_task(request(OLD)) for _ in range(40)]
        await asyncio.sleep(0.2)  # the read is in flight and every old-name request is waiting on it
        for _ in range(20):
            assert await asyncio.wait_for(request(NEW), timeout=2) == (200, None)
        # A bounded number wait for the one read; the rest were answered at once with nothing.
        assert sum(not task.done() for task in old) == ui_server._ADDRESS_STATE_WAITERS_MAX
        release.set()
        answers = await asyncio.wait_for(asyncio.gather(*old), timeout=5)
        cleaned = [answer for answer in answers if answer == (410, b'"cache", "storage"')]
        refused = [answer for answer in answers if answer == (503, None)]
        assert len(cleaned) == ui_server._ADDRESS_STATE_WAITERS_MAX and len(cleaned) + len(refused) == 40

    asyncio.run(scenario())
    assert len([call for call in plane.calls if call[1] == "hostname-state"]) == 1


def test_b2_a_failed_read_is_not_repeated_until_five_seconds_after_it_ended(address):
    plane, clocks = address
    rename_to("new")
    run()
    clocks.advance(61)
    plane.calls.clear()

    def slow_failure():
        clocks.advance(9)  # the read itself took longer than the interval
        raise RuntimeError("resource_acl_device_unavailable")

    plane.state = slow_failure
    assert get(OLD).status_code == 503
    assert get(OLD).status_code == 503
    assert len(plane.calls) == 1
    clocks.advance(6)
    assert get(OLD).status_code == 503 and len(plane.calls) == 2


def test_e2_a_deep_link_cannot_grow_the_headers_past_what_an_edge_delivers():
    path = "/" + "/".join("s" * 300 for _ in range(8))
    cookies = "; ".join(f"cookie{i}=1" for i in range(30))
    values = instance_address_responses.expiring_cookies(path, cookies)
    assert sum(len(value) for value in values) <= 16 * 1024
    assert "cookie0=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure" in values
    headers = instance_address_responses.cleanup_headers(path, cookies, content_type="text/html; charset=utf-8")
    assert sum(len(key) + len(value) for key, value in headers) < 20 * 1024


def test_b2_a_401_drops_every_permission_and_a_listing_past_its_date_is_not_cleaned(address):
    plane, clocks = address
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    plane.fail = remote_access.BackendRequestError(401, {"error": "invalid_device_secret"})
    assert instance_address.refresh_state(force=True) is False
    assert get(OLD).status_code == 503
    plane.fail = None
    plane.cleanup[OLD] = "2020-01-01T00:00:00Z"
    clocks.advance(6)
    assert get(OLD).status_code == 503


def test_b5_an_older_generation_does_not_replace_the_roles(address):
    plane, clocks = address
    rename_to("new")
    run()
    plane.generation -= 3
    plane.cleanup.clear()
    assert instance_address.refresh_state(force=True) is False
    assert is_cleaning(get(OLD))


def test_b7_the_hostname_snapshot_cannot_keep_a_former_managed_name_served(address):
    plane, _ = address
    config = settings_service.load_config()
    remote_access._replace_active_hostnames(config, [OLD, "custom.example.com"])
    rename_to("new")
    run()
    # A heartbeat that still lists the old name does not bring it back either.
    remote_access._replace_active_hostnames(settings_service.load_config(), [OLD, NEW, "custom.example.com"])
    assert remote_access.active_hostnames(settings_service.load_config()) == frozenset({"custom.example.com"})
    response = get(OLD, "/api/session")
    assert response.status_code == 410 and is_cleaning(response)


@pytest.mark.parametrize("host", ["someone-else-app.avibe.bot", THIRD])
def test_e8_any_other_managed_name_gets_nothing_on_every_path(address, host):
    for path in ROUTES:
        response = get(host, path)
        assert response.status_code == 503, path
        assert not is_cleaning(response)
        assert "Set-Cookie" not in response.headers
        assert response.get_json() == {"ok": False, "error": "remote_access_host_mismatch"}
    spoofed = ui_server.app.test_client().get("/api/session", base_url=LOCAL, headers={"X-Forwarded-Host": THIRD})
    assert spoofed.status_code == 503


def test_b6_a_name_withdrawn_from_the_list_gets_nothing_and_its_list_is_closed(address):
    plane, clocks = address
    sign_in()
    rename_to("new")
    run()
    assert instance_address.describe()["cleanup"][0]["hostname"] == OLD
    del plane.cleanup[OLD]
    plane.generation += 1
    clocks.advance(6)
    assert instance_address.refresh_state(force=True)
    for path in ("/", "/api/session", "/assets/app.js"):
        response = get(OLD, path)
        assert response.status_code == 503 and not is_cleaning(response)
    assert instance_address.describe()["cleanup"] == []
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(instance_address_clearings)).all() == []


# --- E2-E5: what the old address answers ------------------------------------------------


def test_e2_cookies_expire_by_name_and_path_without_touching_the_parent_domain(address):
    rename_to("new")
    run()
    client = ui_server.app.test_client()
    client.set_cookie("__Secure-vibe_show_guest_abc", "guest", domain=OLD, path="/p/share1")
    client.set_cookie("__Host-vibe_remote_session", "x.y", domain=OLD)
    response = client.get("/p/share1/", base_url=f"https://{OLD}")
    cookies = response.headers.getlist("Set-Cookie")
    assert "__Secure-vibe_show_guest_abc=; Path=/p/share1; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure" in cookies
    assert any(value.startswith("__Host-vibe_remote_session=; Path=/;") for value in cookies)
    assert not any(value.startswith("__Host-vibe_remote_session=; Path=/p") for value in cookies)
    assert all("Domain" not in value and "Secure" in value for value in cookies)
    assert response.headers["Clear-Site-Data"] == '"cache", "storage"'
    assert "cookies" not in response.headers["Clear-Site-Data"] and "*" not in response.headers["Clear-Site-Data"]


def test_e2_cookie_names_and_counts_are_bounded_and_well_formed():
    header = "; ".join([f"c{i}=1" for i in range(400)] + ["bad name=1", "evil\r\nSet-Cookie=1", "x" * 300 + "=1"])
    values = instance_address_responses.expiring_cookies("/a/b/c/d/e/f/g/h/i/j/k/l/m;x/,y?q=1", header)
    assert len(values) <= 160
    assert all("\r" not in value and "\n" not in value and "bad name" not in value for value in values)
    assert not any(";x" in value.split("; Max-Age")[0].split("Path=")[1] for value in values)
    assert "c0=; Path=/a/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure" in values


def test_e3_the_cleaning_script_leaves_the_write_fence_the_app_reads():
    from pathlib import Path

    ui = Path(__file__).resolve().parents[1] / "ui" / "src" / "lib"
    sign_out, epoch, boot = instance_address_responses.WRITE_FENCE_KEYS
    marker = instance_address_responses.RETIRED_MARKER_KEY
    assert f"SIGN_OUT_GENERATION_KEY = '{sign_out}'" in (ui / "signOutGeneration.ts").read_text(encoding="utf-8")
    assert f"STORE_EPOCH_KEY = '{epoch}'" in (ui / "localStore" / "index.ts").read_text(encoding="utf-8")
    assert f"BOOT_SESSION_GENERATION_KEY = '{boot}'" in (ui / "bootSession.ts").read_text(encoding="utf-8")
    page = (ui / "addressRetired.ts").read_text(encoding="utf-8")
    assert all(f"'{key}'" in page for key in (epoch, boot, marker))
    assert json.dumps([sign_out, epoch, boot, marker]) in instance_address_responses.CLEANING_SCRIPT


def test_e3_the_replacement_worker_and_the_inline_script_match_the_policy(address):
    rename_to("new")
    run()
    worker = get(OLD, "/push-sw.js")
    assert worker.status_code == 200 and worker.headers["Content-Type"].startswith("text/javascript")
    assert "unregister" in worker.text and is_cleaning(worker)
    page = get(OLD, "/", headers={"Accept-Language": "zh-CN"})
    body = page.text
    assert "这个 Avibe 换了新地址。能访问它的人，在 avibe.bot 登录后可以打开。" in body
    assert 'href="https://issuer.invalid"' in body
    assert f"<script>{instance_address_responses.CLEANING_SCRIPT}</script>" in body
    assert instance_address_responses.CONTENT_SECURITY_POLICY == page.headers["Content-Security-Policy"]
    assert "This Avibe has a new address." in get(OLD, "/").text


def test_e5_health_device_routes_get_401_and_their_body_is_not_read(address, monkeypatch):
    rename_to("new")
    run()
    consumed = []
    original = ui_server._bounded_request_text

    async def spy(receive, limit):
        consumed.append(limit)
        return await original(receive, limit)

    monkeypatch.setattr(ui_server, "_bounded_request_text", spy)
    response = ui_server.app.test_client().put(
        "/api/mobile/device/v1/health/days", base_url=f"https://{OLD}", data=b"x" * 4096,
        headers={"Authorization": "Bearer still-valid", "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "code": "device_unauthorized", "error": "device_unauthorized"}
    assert not is_cleaning(response) and consumed == []


# --- A1-A3: who may ask -------------------------------------------------------------


def test_a1_only_the_owner_or_this_computer_may_use_the_routes(address):
    plane, _ = address
    local = ui_server.app.test_client()
    described = local.get("/api/instance-address", base_url=LOCAL)
    assert described.status_code == 200 and described.get_json()["hostname"] == OLD
    assert described.headers["Cache-Control"] == "no-store"
    cookie, _sid = sign_in("member", "member")
    member = ui_server.app.test_client()
    member.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain=OLD)
    origin = f"https://{OLD}"
    assert member.get("/api/instance-address", base_url=origin).status_code == 403
    refused = member.post("/api/instance-address/preview", base_url=origin, headers=csrf_headers(member, origin), json={"prefix": "new"})
    assert refused.status_code == 403
    forwarded = local.get("/api/instance-address", base_url=LOCAL, headers={"X-Forwarded-For": "203.0.113.9"})
    assert forwarded.status_code in {403, 503}
    missing_csrf = local.post("/api/instance-address/preview", base_url=LOCAL, json={"prefix": "new"})
    assert missing_csrf.status_code == 403
    assert instance_address._journal(binding()) is None
    assert not [call for call in plane.calls if call[1] == "hostname-renames"]


def test_a2_change_takes_only_a_fresh_confirmation_of_its_own_actor(address):
    plane, clocks = address
    local = ui_server.app.test_client()
    headers = csrf_headers(local, LOCAL)
    assert local.post("/api/instance-address/change", base_url=LOCAL, headers=headers, json={}).get_json()["error"] == "confirmation_expired"
    preview = local.post("/api/instance-address/preview", base_url=LOCAL, headers=headers, json={"prefix": "new"}).get_json()
    assert preview["hostname"] == NEW and preview["is_return"] is False and preview["hold_days"] == 30
    assert preview["release_at_if_changed_now"] == RELEASE
    with pytest.raises(instance_address.AddressError) as other_actor:
        instance_address.change(preview["confirmation_id"], ("remote", "another-session"))
    assert other_actor.value.code == "confirmation_expired"
    stale = instance_address.preview("new", ("loopback",))["confirmation_id"]
    clocks.advance(601)
    with pytest.raises(instance_address.AddressError) as expired:
        instance_address.change(stale, ("loopback",))
    assert expired.value.code == "confirmation_expired"
    first = instance_address.preview("new", ("loopback",))["confirmation_id"]
    accepted = local.post("/api/instance-address/change", base_url=LOCAL, headers=headers, json={"confirmation_id": first})
    assert accepted.status_code == 202
    with pytest.raises(instance_address.AddressError) as reused:
        instance_address.change(first, ("loopback",))
    assert reused.value.code == "confirmation_expired"
    with pytest.raises(instance_address.AddressError) as second:
        instance_address.preview("third", ("loopback",))
    assert second.value.code == "operation_in_progress"
    assert len([call for call in plane.calls if call[1] == "hostname-renames"]) == 0  # nothing sent before the worker runs


@pytest.mark.parametrize("change_config", ["organization", "unpaired", "unmanaged"])
def test_a3_ineligible_instances_are_refused_without_asking_avibe_bot(address, change_config):
    plane, _ = address

    def mutate(config):
        cloud = config.remote_access.vibe_cloud
        if change_config == "organization":
            cloud.instance_kind = "organization"
        elif change_config == "unpaired":
            cloud.instance_secret = ""
        else:
            cloud.public_url = "https://alex.example.com"

    from config.v2_config import update_config_fields

    update_config_fields(mutate)
    plane.calls.clear()
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("new", ("loopback",))
    assert refused.value.code == "rename_ineligible" and plane.calls == []
    assert instance_address.describe()["eligible"] is False


@pytest.mark.parametrize("prefix", ["", "a", "-ab", "ab-", "UPPER", "a" * 47, "has.dot", None, 5])
def test_invalid_prefixes_are_refused_locally(address, prefix):
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview(prefix, ("loopback",))
    assert refused.value.code == "invalid_prefix" and refused.value.status == 400


# --- B1, C1, C2, C4, F1-F3: the switch ---------------------------------------------------


def health_pairing():
    store = MobileStore()
    identity = {"id": "inst_123", "owner": "owner@test.invalid", "name": "Fixture"}
    request, _ = store.create_request("health.connect", identity, names=["steps"])
    code = store.issue_pairing(request["id"], identity)["pairing_code"]
    return store.pair(
        {"pairing_code": code, "device_name": "Fixture", "platform": "ios", "app_version": "1",
         "metrics": ["steps"], "installation_id": "fixture-" + request["id"]},
        identity,
    )["device_id"]


def active_health_devices():
    with MobileStore().transaction() as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM devices WHERE revoked_at IS NULL")]


def test_b1_c1_an_own_change_switches_ends_sessions_and_lists_the_devices(address):
    plane, _ = address
    owner_cookie, owner_sid = sign_in()
    sign_in("member", "member")
    health_pairing()
    config = settings_service.load_config()
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(web_push_subscriptions.insert().values(
            id="remote-sub", user_key="remote:owner", endpoint="https://push.invalid/remote", p256dh="k", auth="a",
            enabled=1, failure_count=0, created_at="t", updated_at="t"))
        conn.execute(web_push_subscriptions.insert().values(
            id="local-sub", user_key="local", endpoint="https://push.invalid/local", p256dh="k", auth="a",
            enabled=1, failure_count=0, created_at="t", updated_at="t"))
    woken, closed = [], []
    instance_address._hooks.update(wake_pages=lambda: woken.append(binding().active_host), close_connections=closed.append)
    rename_to("new")
    run()
    switched = settings_service.load_config().remote_access.vibe_cloud
    assert (switched.public_url, switched.redirect_uri) == (f"https://{NEW}", f"https://{NEW}/auth/callback")
    assert switched.session_secret == config.remote_access.vibe_cloud.session_secret  # C1: the registry binding is kept
    assert woken[0] == NEW and closed == [{OLD}]
    assert remote_access.parse_session_identity(settings_service.load_config(), owner_cookie) is None
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions)).all() == []
        assert conn.execute(select(remote_account_revocations)).all() == []  # no account sign-out delegated
        assert conn.execute(select(device_health_owner_checks)).all() == []  # no Health owner check armed
        enabled = dict(conn.execute(select(web_push_subscriptions.c.id, web_push_subscriptions.c.enabled)).all())
    assert enabled == {"remote-sub": 0, "local-sub": 1}
    assert active_health_devices() == []  # C4: an own change ends Health and deletes its data
    described = instance_address.describe()
    assert described["hostname"] == NEW and described["operation"] is None and described["unrecognized_change"] is None
    (old,) = described["cleanup"]
    assert old["hostname"] == OLD and old["release_at"] == RELEASE
    assert old["complete"] is False and old["incomplete_reason"] == "sessions_expired_before_rename_not_listed"
    assert old["unconfirmed"] == 3 and {device["state"] for device in old["devices"]} == {"unconfirmed"}
    assert sorted(device["account"]["email"] for device in old["devices"] if device["account"]) == [
        "member@test.invalid", "owner@test.invalid"]
    # A push registration made under the old address with no live sign-in is listed; one made on
    # this computer's local address is not.
    assert [device["push_only"] for device in old["devices"] if device["account"] is None] == [True]
    assert json.loads(json.dumps(described)) == described and "complete\": true" not in json.dumps(described)


@pytest.mark.parametrize("crash_after", ["receipt", "flip", "sessions", "closed", "health"])
def test_b1_a_crash_at_any_step_resumes_to_the_same_end_without_a_second_operation(address, monkeypatch, crash_after):
    plane, _ = address
    sign_in()
    first_phone = health_pairing()
    rename_to("new")
    original = instance_address._set_journal
    crashed = []

    def crashing(bound, journal, **kwargs):
        original(bound, journal, **kwargs)
        marker = "receipt" if (journal or {}).get("phase") == "committed" and not (journal or {}).get("steps") else None
        if journal and crash_after in (journal.get("steps") or {}):
            marker = crash_after
        if marker == crash_after and not crashed:
            crashed.append(marker)
            raise KeyboardInterrupt("process killed")

    monkeypatch.setattr(instance_address, "_set_journal", crashing)
    with pytest.raises(KeyboardInterrupt):
        run()
    later_phone = None
    if crash_after == "health":
        later_phone = health_pairing()  # paired after the deletion ran: a repeat must not touch it
    # A restart: memory is gone, the journal is not.
    for name, value in (("_authority", None), ("_retired_at_flip", {}), ("_BINDING_CACHE", None), ("_last_read_started", None), ("_last_read_failed", None)):
        monkeypatch.setattr(instance_address, name, value)
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
    renames = [call[2]["operation_id"] for call in plane.calls if call[1] == "hostname-renames"]
    assert len(set(renames)) == 1
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions)).all() == []
        assert len(conn.execute(select(instance_address_clearings)).all()) == 1
    assert active_health_devices() == ([later_phone] if later_phone else [])
    assert first_phone not in active_health_devices()
    assert is_cleaning(get(OLD))


def test_b1_a_pending_answer_repeats_the_same_request_and_a_refusal_changes_nothing(address):
    plane, _ = address
    sign_in()
    plane.pending_answers = 2
    rename_to("new")
    run()
    sent = [call[2] for call in plane.calls if call[1] == "hostname-renames"]
    assert len(sent) == 3 and len({json.dumps(body, sort_keys=True) for body in sent}) == 1
    assert binding().active_host == NEW
    plane.refuse = (409, {"error": "name_holding_limit_reached", "limit": 5, "held_count": 5, "earliest_release_at": RELEASE})
    rename_to("third")
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
    failed = instance_address.describe()["operation"]
    assert failed == {"state": "failed", "hostname": THIRD, "error": "name_holding_limit_reached",
                      "limit": 5, "held_count": 5, "earliest_release_at": RELEASE}


@pytest.mark.parametrize(("reply", "code", "status"), [
    ((409, {"error": "taken"}), "taken", 409),
    ((400, {"error": "invalid_prefix"}), "invalid_prefix", 400),
    ((403, {"error": "rename_ineligible"}), "rename_ineligible", 409),
    ((409, {"error": "binding_changed"}), "address_changed", 409),
    ((401, {"error": "invalid_device_secret"}), "pairing_invalid", 409),
    ((409, {"error": "remote_access_configuration_mismatch"}), "remote_access_configuration_mismatch", 409),
    ((409, {"error": "rebind_required"}), "rebind_required", 409),
    # Definitive refusals without an own word on the page: said as what they are, never as "unavailable".
    ((400, {"error": "invalid_request"}), "invalid_request", 409),
    ((405, {"error": "method_not_allowed"}), "method_not_allowed", 409),
    ((415, {"error": "unsupported_media_type"}), "unsupported_media_type", 409),
    ((400, {"error": "resource_acl_device_rejected"}), "rename_refused", 409),  # a body that was not JSON
    ((400, {"error": "Not A Code!"}), "rename_refused", 409),
    ((400, {}), "rename_refused", 409),
    ((404, {}), "rename_ineligible", 409),
])
def test_refusals_reach_the_page_as_stable_codes(address, monkeypatch, reply, code, status):
    plane, _ = address
    plane.refuse = reply
    monkeypatch.setattr(instance_address, "_FIRST_ANSWER_WAIT_SECONDS", 5.0)
    confirmation = instance_address.preview("new", ("loopback",))["confirmation_id"]
    waits = []

    def advance_instead_of_waiting(timeout=None):
        waits.append(timeout)
        run()

    monkeypatch.setattr(instance_address._first_answer, "wait", advance_instead_of_waiting)
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.change(confirmation, ("loopback",))
    assert (refused.value.code, refused.value.status) == (code, status)
    assert binding().active_host == OLD and instance_address._journal(binding()) is None


def test_a_tunnel_configuration_mismatch_settles_the_operation_and_is_never_sent_again(address):
    plane, _ = address
    _cookie, sid = sign_in()
    # The answer to the first sending. (To a repeat after "pending" it holds the
    # journal instead: test_a_refused_repeat_after_pending_holds_the_change.)
    plane.refuse = (409, {"error": "remote_access_configuration_mismatch"})
    rename_to("new")
    run()

    def sent():
        return [call[2] for call in plane.calls if call[1] == "hostname-renames"]

    assert len(sent()) == 1
    # Settled: no journal, so nothing is left to send; the page reads the code.
    assert instance_address._journal(binding()) is None
    assert instance_address.describe()["operation"] == {
        "state": "failed", "hostname": NEW, "error": "remote_access_configuration_mismatch"}
    assert instance_address._advance_operation() <= 0
    run()
    assert len(sent()) == 1
    # Nothing of the change happened: the address, its sessions, and no cleaning anywhere.
    assert binding().active_host == OLD and not is_cleaning(get(OLD)) and get(NEW).status_code == 503
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions.c.session_id)).scalars().all() == [sid]
        assert conn.execute(select(instance_address_clearings)).all() == []
    # The way out is a new attempt after binding again: a new operation, not the settled one.
    plane.refuse = None
    rename_to("new")
    run()
    assert binding().active_host == NEW
    assert len(sent()) == 2 and sent()[1]["operation_id"] != sent()[0]["operation_id"]


@pytest.mark.parametrize("no_answer", [
    TimeoutError("timed out"),
    ConnectionError("partial read"),
    remote_access.BackendRequestError(503, {"error": "remote_access_configuration_mismatch"}),
    remote_access.BackendRequestError(429, {"retry_after_seconds": 2}),
])
def test_an_unanswered_rename_is_not_taken_for_a_configuration_mismatch(address, no_answer):
    plane, _ = address
    rename_to("new")
    answer = plane.rename

    def unanswered(_body):
        raise no_answer

    plane.rename = unanswered
    for _ in range(3):
        assert instance_address._advance_operation() > 0  # the same request is owed again
    journal = instance_address._journal(binding())
    assert journal["phase"] == "intent" and binding().active_host == OLD
    assert instance_address.describe()["operation"]["state"] == "pending"
    plane.rename = answer
    run()
    sent = [call[2] for call in plane.calls if call[1] == "hostname-renames"]
    assert len(sent) == 4 and {body["operation_id"] for body in sent} == {journal["operation_id"]}
    assert binding().active_host == NEW and instance_address._journal(binding()) is None


REBIND = remote_access.BackendRequestError(409, {"error": "rebind_required"})


def asked(plane, suffix):
    return sum(1 for call in plane.calls if call[1] == suffix)


def bind_again():
    from config.v2_config import update_config_fields

    update_config_fields(lambda config: setattr(config.remote_access.vibe_cloud, "instance_secret", "a-new-device-secret"))


def test_a_rename_refused_for_more_than_one_pairing_is_settled_and_nothing_asks_again(address):
    plane, clocks = address
    _cookie, sid = sign_in()
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    # A second pairing of this instance appears at avibe.bot; this computer's credential is still valid.
    plane.refuse = (409, {"error": "rebind_required"})
    rename_to("third")
    plane.fail = None
    reads = asked(plane, "hostname-state")
    run()
    assert asked(plane, "hostname-renames") == 2  # the first change, then this one, refused at once
    assert instance_address._journal(binding()) is None and binding().active_host == NEW
    told = instance_address.describe()
    assert told["operation"] == {"state": "failed", "hostname": THIRD, "error": "rebind_required"}
    assert told["state_error"] == "rebind_required"
    # Settled: the request is not sent again, and the state is not asked for either.
    assert instance_address._advance_operation() <= 0
    instance_address.note_generation_hint(plane.generation + 5)
    run()
    clocks.advance(120)
    assert not is_cleaning(get(OLD)) and get(OLD).status_code == 503  # no permission is given, none is held
    assert asked(plane, "hostname-renames") == 2 and asked(plane, "hostname-state") == reads
    with get_cached_sqlite_engine().connect() as conn:
        assert sid not in conn.execute(select(remote_device_sessions.c.session_id)).scalars().all()  # ended by the first change only
    # The way out: remote access bound again is another pairing, which is asked and answered.
    plane.refuse = None
    bind_again()
    clocks.advance(6)
    assert is_cleaning(get(OLD))
    assert instance_address.describe()["state_error"] is None
    rename_to("third")
    run()
    assert binding().active_host == THIRD


def test_a_state_read_refused_for_more_than_one_pairing_stands_until_remote_access_is_bound_again(address):
    plane, clocks = address
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    plane.fail = REBIND
    instance_address._wake_worker(read_state=True)
    reads = asked(plane, "hostname-state")
    assert instance_address._read_when_asked() is False
    assert asked(plane, "hostname-state") == reads + 1
    assert instance_address.describe()["state_error"] == "rebind_required"
    # Nothing asks again by itself: not the worker, not a hint, not the page's poll, not a visitor of the old name.
    for _ in range(3):
        clocks.advance(3600)
        instance_address.note_generation_hint(plane.generation + 1)
        assert instance_address._read_when_asked() is False
        assert instance_address.refresh_state() is False and instance_address.refresh_state(force=True) is False
        assert instance_address.describe()["state_error"] == "rebind_required"
        response = get(OLD)
        assert response.status_code == 503 and not is_cleaning(response)
    assert asked(plane, "hostname-state") == reads + 1
    # The Owner asking for a change is a person asking now: one read, and the page gets the code.
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    assert (refused.value.status, refused.value.code) == (409, "rebind_required")
    assert asked(plane, "hostname-state") == reads + 2 and asked(plane, "hostname-renames") == 1
    # No answer says nothing about it.
    plane.fail = TimeoutError("timed out")
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    assert refused.value.code == "rebind_required" and asked(plane, "hostname-state") == reads + 3
    # Bound again: another pairing, read at once and cleaned for as before.
    plane.fail = None
    bind_again()
    clocks.advance(6)
    assert is_cleaning(get(OLD))
    assert instance_address.describe()["state_error"] is None
    assert instance_address._read_when_asked() is True


@pytest.mark.parametrize(("answer", "told"), [
    (None, None),
    (remote_access.BackendRequestError(401, {"error": "invalid_device_secret"}), (409, "pairing_invalid")),
    (remote_access.BackendRequestError(403, {"error": "rename_ineligible"}), (409, "rename_ineligible")),
    (remote_access.BackendRequestError(403, {"error": "forbidden"}), (409, "rename_ineligible")),
    (remote_access.BackendRequestError(400, {"error": "invalid_request"}), (503, "unavailable")),
    # No such endpoint: as an address avibe.bot has not answered for.
    (remote_access.BackendRequestError(404, {"error": "resource_acl_device_rejected"}), (409, "rename_ineligible")),
    (remote_access.BackendRequestError(405, {}), (503, "unavailable")),
    (remote_access.BackendRequestError(415, {}), (503, "unavailable")),
    (remote_access.BackendRequestError(409, {"error": "binding_changed"}), (503, "unavailable")),
])
def test_another_answer_to_a_person_asking_ends_the_standing_refusal_and_is_worded_as_at_a_rename(address, answer, told):
    plane, clocks = address
    plane.fail = REBIND
    assert instance_address.refresh_state(force=True) is False
    assert instance_address.describe()["state_error"] == "rebind_required"
    clocks.advance(61)  # what was read before is too old to go by
    plane.fail = answer
    if answer is None:
        assert instance_address.preview("third", ("loopback",))["hostname"] == THIRD
    else:
        with pytest.raises(instance_address.AddressError) as refused:
            instance_address.preview("third", ("loopback",))
        assert (refused.value.status, refused.value.code) == told
    assert instance_address.describe()["state_error"] is None
    # Not a standing refusal: it is asked again, and an answer that says nothing more ends the wording.
    reads = asked(plane, "hostname-state")
    plane.fail = TimeoutError("timed out")
    clocks.advance(6)
    assert instance_address.refresh_state() is False and asked(plane, "hostname-state") == reads + 1
    clocks.advance(61)
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    # An endpoint known to be absent stays so until it answers; no answer does not change that.
    assert refused.value.code == ("rename_ineligible" if getattr(answer, "status", None) == 404 else "unavailable")


@pytest.mark.parametrize("error", [{"code": "conflict"}, ["rebind_required"], 409, None, True])
@pytest.mark.parametrize("status", [401, 403, 409])
def test_a_refusal_whose_code_is_not_a_string_names_nothing(address, status, error):
    plane, clocks = address
    plane.fail = remote_access.BackendRequestError(status, {"error": error})
    assert instance_address.refresh_state(force=True) is False
    told = instance_address.describe()  # the page's route still answers
    assert told["state_error"] is None and told["eligible"] is True
    assert instance_address._standing_refusal is None and instance_address._read_refusal is None
    clocks.advance(61)
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    assert refused.value.code == "unavailable"
    plane.fail = None
    plane.refuse = (status, {"error": error})
    clocks.advance(6)
    rename_to("third")
    run()
    assert instance_address._journal(binding()) is None and binding().active_host == OLD
    expected = "pairing_invalid" if status == 401 else "rename_refused"  # a refusal, never "unavailable"
    assert instance_address.describe()["operation"]["error"] == expected
    assert instance_address._standing_refusal is None


def test_a_state_reply_older_than_the_refusal_does_not_end_it(address):
    plane, _ = address
    answer = plane.state

    def overtaken():
        # While this read is on its way, a rename is answered rebind_required.
        state = answer()
        instance_address._note_standing_refusal(binding(), "rebind_required")
        return state

    plane.state = overtaken
    assert instance_address.refresh_state(force=True) is False
    assert instance_address.describe()["state_error"] == "rebind_required"
    reads = asked(plane, "hostname-state")
    plane.state = answer
    assert instance_address.refresh_state(force=True) is False and asked(plane, "hostname-state") == reads


# --- before avibe.bot has ever answered for this address ---------------------------------

REAL_DEVICE_REQUEST = remote_access._device_json_request
# What avibe.bot answers today for an endpoint it does not have: 404 with the site's HTML
# "not found" page (avibe-bot-backend origin/main has no hostname-state route, and its
# middleware does not run on /api/v1/). ``_device_json_request`` turns that into this.
NO_SUCH_ENDPOINT = (404, {"error": "resource_acl_device_rejected"})


def never_answered():
    """A paired instance whose stored state holds no state read: one upgraded to this version."""

    instance_address._state_path().unlink()
    for name in ("_authority", "_known_cleanup", "_last_read_started", "_last_read_failed"):
        setattr(instance_address, name, None)


def test_todays_answer_for_a_missing_endpoint_is_a_404_and_never_a_lost_binding(address, monkeypatch):
    from types import SimpleNamespace

    def not_found_page(*_args, **_kwargs):
        def no_json():
            raise ValueError("not JSON")

        return SimpleNamespace(status_code=404, json=no_json, text="<!DOCTYPE html><html>not found</html>")

    monkeypatch.setattr(remote_access.requests, "request", not_found_page)
    with pytest.raises(remote_access.BackendRequestError) as answer:
        REAL_DEVICE_REQUEST(settings_service.load_config(), "POST", "hostname-state", {})
    assert (answer.value.status, answer.value.payload) == NO_SUCH_ENDPOINT
    never_answered()
    monkeypatch.setattr(remote_access, "_device_json_request", REAL_DEVICE_REQUEST)
    epoch = instance_address._authority_epoch
    assert instance_address.refresh_state(force=True) is False
    # Definitive, and nothing else: the binding is not taken for refused, nothing is dropped, nothing stands.
    assert instance_address._read_refused is True and instance_address._authority_epoch == epoch
    assert instance_address._standing_refusal is None and instance_address._read_refusal is None
    assert binding().paired and get(OLD, "/health").status_code == 200


def test_an_instance_upgraded_before_avibe_bot_answers_offers_no_change_and_is_otherwise_as_before(address):
    plane, clocks = address
    never_answered()
    plane.fail = remote_access.BackendRequestError(*NO_SUCH_ENDPOINT)
    told = instance_address.describe()
    assert told["eligible"] is False and told["ineligible_reason"] == "address_state_unknown"
    assert told["hostname"] == OLD and told["public_url"] == f"https://{OLD}"
    assert told["cleanup"] == [] and told["operation"] is None and told["state_error"] is None
    assert told["hold_days"] is None and told["release_at_if_changed_now"] is None
    # Neither request of a change asks avibe.bot anything.
    reads = len(plane.calls)
    for attempt in (lambda: instance_address.preview("new", ("loopback",)),
                    lambda: instance_address.change("made-up-confirmation", ("loopback",))):
        with pytest.raises(instance_address.AddressError) as refused:
            attempt()
        assert (refused.value.status, refused.value.code) == (409, "rename_ineligible")
        assert refused.value.extra == {"reason": "address_state_unknown"}
    assert len(plane.calls) == reads and instance_address._journal(binding()) is None
    # The address in use is served, another managed name gets the empty refusal, nothing is cleaned.
    assert get(OLD, "/health").status_code == 200 and "X-Avibe-Address" not in get(OLD).headers
    clocks.advance(6)
    for name in (NEW, THIRD):
        response = get(name)
        assert response.status_code == 503 and not is_cleaning(response)
    assert binding().active_host == OLD and binding().paired


def test_the_change_is_offered_by_itself_once_avibe_bot_answers(address):
    plane, clocks = address
    never_answered()
    plane.fail = remote_access.BackendRequestError(*NO_SUCH_ENDPOINT)
    instance_address._wake_worker(read_state=True)  # as at every start
    assert instance_address._read_when_asked() is False  # refused: the read stays owed
    assert asked(plane, "hostname-state") == 1 and instance_address._read_refused is True
    # Opening the page asks again, no more often than the read interval.
    assert instance_address.describe()["ineligible_reason"] == "address_state_unknown"
    assert asked(plane, "hostname-state") == 1
    for opened in range(2, 5):
        clocks.advance(6)
        assert instance_address.describe()["eligible"] is False
        assert instance_address.describe()["eligible"] is False
        assert asked(plane, "hostname-state") == opened
    assert instance_address._worker_read_requested.is_set()
    # avibe.bot gains the endpoints. No restart, nothing reset by hand: the next opening of the page reads.
    plane.fail = None
    clocks.advance(6)
    told = instance_address.describe()
    assert told["eligible"] is True and told["ineligible_reason"] is None and told["hold_days"] == 30
    rename_to("new")
    run()
    assert binding().active_host == NEW and is_cleaning(get(OLD))


def test_endpoints_that_go_away_hide_the_change_until_they_answer_again(address):
    """avibe.bot rolled back after this instance had read the state once."""

    import json

    plane, clocks = address
    rename_to("new")
    run()
    assert instance_address.describe()["eligible"] is True
    stored = json.loads(instance_address._state_path().read_text(encoding="utf-8"))
    plane.fail = remote_access.BackendRequestError(*NO_SUCH_ENDPOINT)
    clocks.advance(61)
    told = instance_address.describe()  # opening the page reads, and the endpoint is not there
    assert told["eligible"] is False and told["ineligible_reason"] == "address_state_unknown"
    # What was recorded stays: the address, the old name and its list.
    assert told["hostname"] == NEW and [entry["hostname"] for entry in told["cleanup"]] == [OLD]
    assert json.loads(instance_address._state_path().read_text(encoding="utf-8")) == stored
    assert binding().active_host == NEW and binding().paired
    # Neither request of a change asks anything.
    calls = len(plane.calls)
    for attempt in (lambda: instance_address.preview("third", ("loopback",)),
                    lambda: instance_address.change("made-up-confirmation", ("loopback",))):
        with pytest.raises(instance_address.AddressError) as refused:
            attempt()
        assert (refused.value.status, refused.value.code) == (409, "rename_ineligible")
        assert refused.value.extra == {"reason": "address_state_unknown"}
    assert len(plane.calls) == calls and instance_address._journal(binding()) is None
    # No answer says nothing new; it stays hidden. Another definitive answer is not this fact.
    plane.fail = TimeoutError("timed out")
    clocks.advance(6)
    assert instance_address.describe()["ineligible_reason"] == "address_state_unknown"
    plane.fail = remote_access.BackendRequestError(403, {"error": "rename_ineligible"})
    clocks.advance(6)
    assert instance_address.describe()["eligible"] is True
    plane.fail = remote_access.BackendRequestError(*NO_SUCH_ENDPOINT)
    clocks.advance(6)
    assert instance_address.describe()["eligible"] is False
    # A 404 that arrives while the Owner asks is said the same way, not as "unavailable".
    instance_address._endpoint_absent = None
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    assert refused.value.extra == {"reason": "address_state_unknown"}
    # The endpoints come back: the next opening of the page offers the change, with no restart.
    plane.fail = None
    clocks.advance(6)
    told = instance_address.describe()
    assert told["eligible"] is True and told["ineligible_reason"] is None
    rename_to("third")
    run()
    assert binding().active_host == THIRD


def test_without_the_page_the_owed_read_finds_avibe_bot_within_the_hour(address):
    plane, clocks = address
    never_answered()
    plane.fail = remote_access.BackendRequestError(*NO_SUCH_ENDPOINT)
    instance_address._wake_worker(read_state=True)
    for _ in range(3):
        assert instance_address._read_when_asked() is False and instance_address._read_refused is True
        clocks.advance(instance_address._OWED_READ_RETRY_MAX_SECONDS)
    # The worker asks a refused read again after 30 seconds, doubling, and never later than this.
    assert (instance_address._OWED_READ_RETRY_SECONDS, instance_address._OWED_READ_RETRY_MAX_SECONDS) == (30.0, 3600.0)
    plane.fail = None
    assert instance_address._read_when_asked() is True and not instance_address._worker_read_requested.is_set()
    reads = asked(plane, "hostname-state")
    told = instance_address.describe()
    assert told["eligible"] is True and asked(plane, "hostname-state") == reads


@pytest.mark.parametrize(("no_answer", "told"), [
    (TimeoutError("timed out"), "unavailable"),
    (ConnectionError("partial read"), "unavailable"),
    (remote_access.BackendRequestError(503, {"error": "rebind_required"}), "unavailable"),
    (remote_access.BackendRequestError(429, {"error": "rate_limited"}), "unavailable"),
    (remote_access.BackendRequestError(401, {"error": "invalid_device_secret"}), "pairing_invalid"),
    (remote_access.BackendRequestError(403, {"error": "forbidden"}), "rename_ineligible"),
    (remote_access.BackendRequestError(404, {"error": "resource_acl_device_rejected"}), "rename_ineligible"),
    (remote_access.BackendRequestError(409, {"error": "binding_changed"}), "unavailable"),
])
def test_other_failures_of_the_state_read_are_asked_again_as_before(address, no_answer, told):
    plane, clocks = address
    plane.fail = no_answer
    instance_address._wake_worker(read_state=True)
    for attempt in range(1, 4):
        assert instance_address._read_when_asked() is False
        assert asked(plane, "hostname-state") == attempt
        assert instance_address.describe()["state_error"] is None
        clocks.advance(61)
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview("third", ("loopback",))
    assert refused.value.code == told
    # Once the endpoint is known to be absent, the Owner's request asks nothing more.
    absent = getattr(no_answer, "status", None) == 404
    assert asked(plane, "hostname-state") == (3 if absent else 4) and asked(plane, "hostname-renames") == 0


def test_a_committed_change_waits_for_a_new_binding_without_asking(address):
    plane, clocks = address
    sign_in()
    rename_to("new")
    answer = plane.state

    def refused():
        raise REBIND

    plane.state = refused  # the rename is answered and committed; the state is refused from then on
    reads = asked(plane, "hostname-state")
    for _ in range(4):
        assert instance_address._advance_operation() > 0
        clocks.advance(30)
    assert asked(plane, "hostname-renames") == 1 and asked(plane, "hostname-state") == reads + 1
    assert instance_address._journal(binding())["phase"] == "committed" and binding().active_host == OLD
    told = instance_address.describe()
    assert told["operation"]["state"] == "applying" and told["state_error"] == "rebind_required"
    plane.state = answer
    bind_again()
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
    assert instance_address.describe()["state_error"] is None


# avibe.bot's rule for a rename request, copied from avibe-bot-backend
# lib/hostnames/wire.ts (``renameTuple``, at 8d805492): ``operation_id`` is
# ``z.string().uuid()`` with zod 3.25.76, whose test is this expression
# (node_modules/zod/v3/types.js:368); the other three fields as below; and
# ``.strict()``: no other field.
BACKEND_UUID = re.compile(r"^[0-9a-fA-F]{8}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{4}\b-[0-9a-fA-F]{12}$", re.I)


def renames_sent(plane):
    return [call[2] for call in plane.calls if call[1] == "hostname-renames"]


def test_the_rename_request_passes_avibe_bots_rule_and_a_repeat_sends_the_same_id(address, monkeypatch):
    plane, _ = address
    plane.pending_answers = 2
    rename_to("new")
    run()
    sent = renames_sent(plane)
    assert len(sent) == 3 and binding().active_host == NEW
    for body in sent:
        assert set(body) == {"operation_id", "expected_generation", "expected_hostname", "new_prefix"}
        assert isinstance(body["operation_id"], str) and BACKEND_UUID.match(body["operation_id"])
        generation = body["expected_generation"]
        assert isinstance(generation, int) and not isinstance(generation, bool) and 0 <= generation <= 2**53 - 1
        assert isinstance(body["expected_hostname"], str) and 1 <= len(body["expected_hostname"]) <= 253
        assert isinstance(body["new_prefix"], str)
    assert len({body["operation_id"] for body in sent}) == 1  # one intent, one id, however often it is sent
    # Each new intent gets its own; across a restart a repeat keeps the one in the journal.
    plane.pending_answers = 1
    rename_to("third")
    assert instance_address._advance_operation() > 0
    for name, value in (("_authority", None), ("_BINDING_CACHE", None)):
        monkeypatch.setattr(instance_address, name, value)
    run()
    later = renames_sent(plane)[3:]
    assert len(later) == 2 and later[0]["operation_id"] == later[1]["operation_id"] != sent[0]["operation_id"]
    assert BACKEND_UUID.match(later[0]["operation_id"]) and binding().active_host == THIRD


def held_journal():
    journal = instance_address._journal(binding())
    assert journal is not None and journal["phase"] == "intent" and isinstance(journal.get("held"), dict)
    return journal


@pytest.mark.parametrize(("refusal", "said"), [
    ((409, {"error": "binding_changed"}), "address_changed"),
    ((409, {"error": "remote_access_configuration_mismatch"}), "remote_access_configuration_mismatch"),
    ((409, {"error": "rebind_required"}), "rebind_required"),
    ((403, {"error": "forbidden"}), "rename_ineligible"),
])
def test_a_refused_repeat_after_pending_holds_the_change_until_the_state_says_what_became_of_it(address, refusal, said):
    """avibe.bot may answer a repeat of a request it took (202) with a 409 that does not cancel it."""

    plane, clocks = address
    _cookie, sid = sign_in()
    phone = health_pairing()
    plane.pending_answers = 1
    plane.refuse = refusal
    rename_to("new")
    run()
    sent = renames_sent(plane)
    assert len(sent) == 2 and sent[0] == sent[1]
    journal = held_journal()
    assert journal["operation_id"] == sent[0]["operation_id"] and journal["origin"] == "local"
    told = instance_address.describe()["operation"]
    assert told == {"state": "pending", "hostname": NEW, "error": said, "unconfirmed": True}
    # Not sent again, by the worker or anything else; nothing of the change has happened.
    for _ in range(3):
        assert instance_address._advance_operation() == 0.0
        clocks.advance(60)
    run()
    assert len(renames_sent(plane)) == 2 and instance_address.describe()["operation"]["state"] == "pending"
    assert binding().active_host == OLD
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions.c.session_id)).scalars().all() == [sid]
    with pytest.raises(instance_address.AddressError) as busy:
        instance_address.preview("third", ("loopback",))
    assert busy.value.code in {"operation_in_progress", said}
    # avibe.bot finishes the operation it took. The Owner looking at the page reads
    # the state (also past a standing refusal), which names it: the address is ours.
    plane.refuse = None
    plane.move(NEW, journal["operation_id"])
    clocks.advance(61)
    instance_address.describe()
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
    assert len(renames_sent(plane)) == 2
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions.c.session_id)).scalars().all() == []
    # Not the permission to delete Health data: the generation it was committed at is unknown.
    assert phone in active_health_devices()


def test_a_lost_receipt_then_a_refused_credential_stops_sending_and_the_state_settles_it(address, monkeypatch):
    plane, clocks = address
    answer = plane.rename

    def committed_but_lost(body):
        answer(body)  # avibe.bot commits it
        raise TimeoutError("the answer was lost")

    plane.rename = committed_but_lost
    rename_to("new")
    assert instance_address._advance_operation() > 0  # no answer: owed again
    journal = instance_address._journal(binding())
    assert journal["may_have_landed"] is True and binding().active_host == OLD

    def credential_refused(_body):
        raise remote_access.BackendRequestError(401, {"error": "invalid_device_secret"})

    plane.rename = credential_refused
    state = plane.state
    plane.state = lambda: credential_refused(None)  # the state read is refused the same way
    epoch = instance_address._authority_epoch
    instance_address._advance_operation()
    held = held_journal()
    assert held["held"]["error"] == "pairing_invalid" and instance_address._authority_epoch > epoch
    assert instance_address.describe()["operation"]["unconfirmed"] is True
    for _ in range(3):
        assert instance_address._advance_operation() == 0.0
    assert sum(1 for call in plane.calls if call[1] == "hostname-renames") == 2
    # The state, once read (here: the credential is accepted again), says the change was
    # made by this very operation: it is applied as our own.
    plane.state = state
    clocks.advance(6)
    assert instance_address.refresh_state(force=True)
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None


def test_a_held_change_is_not_given_the_generation_of_a_later_operation(address):
    """A→B committed at 8, its receipt lost; a later operation raises the generation to 9 while B stays."""

    plane, clocks = address
    phone = health_pairing()
    answer = plane.rename

    def committed_but_lost(body):
        answer(body)
        raise TimeoutError("the answer was lost")

    plane.rename = committed_but_lost
    rename_to("new")
    assert instance_address._advance_operation() > 0
    plane.rename = lambda _body: (_ for _ in ()).throw(remote_access.BackendRequestError(409, {"error": "binding_changed"}))
    plane.state_answer = plane.state
    plane.state = lambda: (_ for _ in ()).throw(TimeoutError("not now"))
    instance_address._advance_operation()
    held_journal()
    plane.generation += 1  # another operation only prepared, the address unchanged
    plane.state = plane.state_answer
    clocks.advance(61)
    instance_address.describe()
    run()
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
    assert phone in active_health_devices()


def test_an_id_of_the_earlier_form_refused_by_the_wire_rule_never_landed_and_settles(address):
    plane, _ = address
    rename_to("new")
    journal = instance_address._journal(binding())
    old = {**journal, "operation_id": "x" * 43}  # as an earlier version made it
    instance_address._set_journal(binding(), old)
    plane.rename = lambda _body: (_ for _ in ()).throw(TimeoutError("no answer"))
    assert instance_address._advance_operation() > 0
    assert instance_address._journal(binding())["may_have_landed"] is True
    plane.rename = lambda _body: (_ for _ in ()).throw(remote_access.BackendRequestError(400, {"error": "invalid_request"}))
    instance_address._advance_operation()
    assert instance_address._journal(binding()) is None
    assert instance_address.describe()["operation"] == {"state": "failed", "hostname": NEW, "error": "invalid_request"}
    # Only that: a UUID refused the same way after an uncertain sending is held.
    plane.rename = lambda _body: (_ for _ in ()).throw(TimeoutError("no answer"))
    rename_to("new")
    assert instance_address._advance_operation() > 0
    plane.rename = lambda _body: (_ for _ in ()).throw(remote_access.BackendRequestError(400, {"error": "invalid_request"}))
    instance_address._advance_operation()
    assert held_journal()["held"]["error"] == "invalid_request"


def test_the_owner_can_check_a_held_change_past_a_standing_refusal_without_sending_it(address):
    plane, clocks = address
    plane.pending_answers = 1
    plane.refuse = (409, {"error": "rebind_required"})
    rename_to("new")
    run()
    journal = held_journal()
    assert instance_address._standing_refusal is not None
    reads, renames = asked(plane, "hostname-state"), len(renames_sent(plane))
    # Nothing asks by itself.
    clocks.advance(3600)
    instance_address.note_generation_hint(plane.generation + 1)
    run()
    assert asked(plane, "hostname-state") == reads
    # The Owner opens the page: one read; again within the minute: none.
    instance_address.describe()
    instance_address.describe()
    assert asked(plane, "hostname-state") == reads + 1 and held_journal()
    # The Owner asks for a change: one read, and the answer is that one is held.
    with pytest.raises(instance_address.AddressError) as busy:
        instance_address.preview("third", ("loopback",))
    assert busy.value.code == "operation_in_progress" and asked(plane, "hostname-state") == reads + 2
    # Once avibe.bot has settled it, that read finds it; nothing was ever sent again.
    plane.refuse = None
    plane.move(NEW, journal["operation_id"])
    with pytest.raises(instance_address.AddressError):
        instance_address.preview("third", ("loopback",))
    run()
    assert binding().active_host == NEW and len(renames_sent(plane)) == renames


@pytest.mark.parametrize("read", ["old address", "fails"])
def test_opening_the_page_reads_the_state_once_for_a_held_change(address, read):
    plane, clocks = address
    plane.pending_answers = 1
    plane.refuse = (409, {"error": "binding_changed"})  # no standing refusal
    rename_to("new")
    run()
    held_journal()
    assert instance_address._standing_refusal is None
    plane.refuse = None
    if read == "fails":
        plane.state = lambda: (_ for _ in ()).throw(TimeoutError("no answer"))
    renames = len(renames_sent(plane))
    clocks.advance(120)  # what was read is past the display interval
    reads = asked(plane, "hostname-state")
    instance_address.describe()
    assert asked(plane, "hostname-state") == reads + 1
    for _ in range(3):
        clocks.advance(10)
        instance_address.describe()
    assert asked(plane, "hostname-state") == reads + 1  # within the minute, no more
    clocks.advance(61)
    instance_address.describe()
    assert asked(plane, "hostname-state") == reads + 2
    assert len(renames_sent(plane)) == renames and held_journal()


def test_a_held_change_that_the_state_shows_went_another_way_is_adopted_as_made_elsewhere(address):
    plane, clocks = address
    plane.pending_answers = 1
    plane.refuse = (409, {"error": "binding_changed"})
    rename_to("new")
    run()
    held_journal()
    plane.refuse = None
    plane.move(THIRD, "an-operation-from-elsewhere")
    clocks.advance(6)
    instance_address.refresh_state(force=True)
    run()
    assert binding().active_host == THIRD and instance_address._journal(binding()) is None
    assert len(renames_sent(plane)) == 2


@pytest.mark.parametrize("before", ["nothing", "throttled"])
def test_a_refusal_of_a_request_that_never_landed_still_settles(address, before):
    plane, _ = address
    if before == "throttled":
        # Turned away before it was looked at: that sending did not land either.
        plane.refuse = (429, {"error": "rate_limited", "retry_after_seconds": 1})
        rename_to("new")
        assert instance_address._advance_operation() > 0
        assert instance_address._journal(binding())["may_have_landed"] is False
        plane.refuse = (409, {"error": "taken"})
    else:
        plane.refuse = (409, {"error": "taken"})
        rename_to("new")
    run()
    assert instance_address._journal(binding()) is None
    assert instance_address.describe()["operation"] == {"state": "failed", "hostname": NEW, "error": "taken"}


def test_a_crash_while_the_request_was_out_counts_as_not_knowing(address, monkeypatch):
    plane, _ = address
    rename_to("new")
    answer = plane.rename

    def crash(_body):
        raise KeyboardInterrupt("process killed while the request was out")

    plane.rename = crash
    with pytest.raises(KeyboardInterrupt):
        instance_address._advance_operation()
    assert instance_address._journal(binding())["may_have_landed"] is True  # written before it left
    plane.rename = answer
    plane.refuse = (409, {"error": "binding_changed"})
    instance_address._advance_operation()
    assert held_journal()["held"]["error"] == "address_changed"


@pytest.mark.parametrize("prefix", ["ab--cd", "xn--abc", "a1--"])
def test_a_prefix_avibe_bot_refuses_is_refused_here_first(address, prefix):
    # avibe-bot-backend lib/claims/prefix.ts: ``<prefix>-app`` must not have "--" at its third and fourth character.
    with pytest.raises(instance_address.AddressError) as refused:
        instance_address.preview(prefix, ("loopback",))
    assert (refused.value.status, refused.value.code) == (400, "invalid_prefix")
    assert not any(call[1] == "hostname-renames" for call in address[0].calls)


def test_c4_a_phone_paired_on_the_new_address_before_the_health_step_is_kept(address):
    plane, _ = address
    old_phone = health_pairing()
    paired_after = []
    # Pages are woken as soon as the new address is live; a phone can pair there from then on.
    instance_address._hooks.update(
        wake_pages=lambda: paired_after or paired_after.append(health_pairing()),
        close_connections=lambda hosts: None,
    )
    rename_to("new")
    run()
    assert binding().active_host == NEW
    assert active_health_devices() == paired_after and old_phone not in paired_after


def test_f2_a_page_that_reports_the_moment_it_is_woken_is_marked(address):
    plane, _ = address
    _cookie, sid = sign_in()
    receipt = instance_address.receipt_for_session(sid)
    accepted = []
    # Pages are woken before the sessions are ended; a fast page reports at once.
    instance_address._hooks.update(
        wake_pages=lambda: accepted or accepted.append(instance_address.record_cleared(OLD, receipt)),
        close_connections=lambda hosts: None,
    )
    rename_to("new")
    run()
    assert accepted == [True]
    (old,) = instance_address.describe()["cleanup"]
    assert [device["state"] for device in old["devices"]] == ["cleared"]


def test_b5_an_older_reply_cannot_name_the_address_or_decide_what_is_deleted(address, monkeypatch):
    plane, _ = address
    phone = health_pairing()
    rename_to("new")
    original = instance_address._read_state_once
    overtaken = []

    def read_then_be_overtaken(bound):
        read = original(bound)
        if read is not None and plane.active == NEW and not overtaken:
            # While this reply was on its way another change followed, and another read recorded it.
            overtaken.append(plane.move(THIRD, "someone-elses-operation"))
            instance_address._update_state(
                bound.instance_id, lambda state: state.__setitem__("generation", plane.generation))
        return read

    monkeypatch.setattr(instance_address, "_read_state_once", read_then_be_overtaken)
    assert instance_address._advance_operation() > 0  # the older reply is not applied
    assert binding().active_host == OLD and active_health_devices() == [phone]
    run()
    assert binding().active_host == THIRD and active_health_devices() == [phone]
    assert instance_address.describe()["unrecognized_change"]["hostname"] == THIRD


def test_c4_a_health_store_that_cannot_be_read_is_not_taken_for_no_pairings(address, monkeypatch):
    from storage import mobile_store

    plane, _ = address
    phone = health_pairing()
    rename_to("new")
    real = mobile_store.MobileStore

    class Locked(real):
        def __init__(self, *args, **kwargs):
            raise mobile_store.MobileError("private_storage_unavailable", 503)

    monkeypatch.setattr(mobile_store, "MobileStore", Locked)
    with pytest.raises(mobile_store.MobileError):
        run()
    # The address moved; the Health step did not, and the state says so rather than "none".
    assert binding().active_host == NEW and instance_address._journal(binding()) is not None
    monkeypatch.setattr(mobile_store, "MobileStore", real)
    assert active_health_devices() == [phone]
    assert instance_address.describe()["health"]["cleanup"] == "pending"
    run()
    assert active_health_devices() == [] and instance_address.describe()["health"]["cleanup"] == "none"


def test_a5_a_return_made_elsewhere_is_drained_and_settled_like_our_own(address, monkeypatch):
    plane, clocks = address
    rename_to("new")
    run()
    assert instance_address.begin_cleanup_response(OLD)  # a cleaning response on the old name is still being written
    monkeypatch.setattr(instance_address._DRAINED, "wait", lambda timeout=None: clocks.advance(timeout or 1))
    plane.move(OLD, "an-operation-this-computer-never-wrote")
    clocks.advance(6)
    assert instance_address.refresh_state(force=True)
    with pytest.raises(RuntimeError, match="did not drain"):
        run()
    assert binding().active_host == NEW  # not served while that response is open
    instance_address.end_cleanup_response(OLD)
    before = clocks.monotonic
    run()
    assert binding().active_host == OLD
    assert clocks.monotonic - before >= instance_address.REACTIVATION_SETTLE_SECONDS


def test_b1_a_refused_request_reads_the_state_and_adopts_what_changed_meanwhile(address):
    plane, _ = address
    rename_to("new")
    # A change made elsewhere lands while ours is unanswered: ours is refused as stale.
    plane.move(THIRD, "someone-elses-operation")
    run()  # refused, and the state read at once: the change made elsewhere is journaled
    assert instance_address._journal(binding())["target_hostname"] == THIRD
    run()
    assert binding().active_host == THIRD  # seen at the refusal, not at some later hint
    assert instance_address.describe()["unrecognized_change"]["hostname"] == THIRD


def test_b1_a_journal_is_not_left_idle_while_the_pairing_is_unreadable(address):
    from config.v2_config import update_config_fields

    plane, _ = address
    rename_to("new")
    update_config_fields(lambda config: setattr(config.remote_access.vibe_cloud, "enabled", False))
    assert instance_address._advance_operation() > 0  # looked at again; nothing else would wake it
    update_config_fields(lambda config: setattr(config.remote_access.vibe_cloud, "enabled", True))
    run()
    assert binding().active_host == NEW


def test_rejected_committed_rename_retires_reconciliation_not_cloud_change(address, monkeypatch):
    from vibe import binding_health

    plane, _ = address
    phone = health_pairing()
    rename_to('new')
    before_commit = plane.state()
    monkeypatch.setattr(plane, 'state', lambda: before_commit)
    assert instance_address._advance_operation() > 0
    committed = instance_address._journal(binding())
    assert committed['phase'] == 'committed' and plane.active == NEW
    binding_health._set(settings_service.load_config(), rejected=True)
    assert binding_health.rejected_at(settings_service.load_config()) is not None
    calls = len(plane.calls)
    assert instance_address._advance_operation() == 0
    assert instance_address._journal(binding()) is None
    last = instance_address._load_state(binding().instance_id)['last_operation']
    assert last['state'] == 'interrupted' and last['operation_id'] == committed['operation_id']
    assert binding().active_host == OLD and plane.active == NEW and len(plane.calls) == calls
    assert phone and active_health_devices()


def test_b5_the_state_from_before_our_own_commit_does_not_end_the_journal(address):
    plane, _ = address
    phone = health_pairing()
    rename_to("new")
    before_commit, current, served = plane.state(), plane.state, []

    def lagging_state():
        # The first read after the commit is answered from before it: the old name, the old generation.
        if plane.active == NEW and not served:
            served.append(True)
            return before_commit
        return current()

    plane.state = lagging_state
    assert instance_address._advance_operation() > 0  # read again; the journal is kept
    journal = instance_address._journal(binding())
    assert journal["origin"] == "local" and journal["phase"] == "committed" and binding().active_host == OLD
    run()
    assert binding().active_host == NEW and active_health_devices() == [] and phone
    assert instance_address.describe()["unrecognized_change"] is None


def test_f1_a_read_from_before_the_switch_does_not_prune_the_list_just_written(address):
    plane, _ = address
    sign_in()
    # The switch has snapshotted the devices of the name about to retire; a state read taken
    # before the commit, and applied only now, does not list that name yet.
    instance_address._snapshot_devices(binding(), OLD)
    assert instance_address.refresh_state(force=True)
    with get_cached_sqlite_engine().connect() as conn:
        assert [row.hostname for row in conn.execute(select(instance_address_clearings))] == [OLD]


def test_a5_a_return_is_drained_whatever_order_the_journal_learned_of_it(address, monkeypatch):
    plane, clocks = address
    rename_to("new")
    run()
    assert instance_address.begin_cleanup_response(OLD)  # still being written on the old name
    monkeypatch.setattr(instance_address._DRAINED, "wait", lambda timeout=None: clocks.advance(timeout or 1))
    plane.move(THIRD, "an-outside-operation")
    clocks.advance(6)
    assert instance_address.refresh_state(force=True)  # journaled as a change to the third name
    plane.move(OLD, "another-outside-operation")
    clocks.advance(6)
    assert instance_address.refresh_state(force=True)  # the journal exists already; the return is still noted
    with pytest.raises(RuntimeError, match="did not drain"):
        run()
    assert binding().active_host == NEW
    instance_address.end_cleanup_response(OLD)
    run()
    assert binding().active_host == OLD


def test_b4_a_further_change_seen_while_a_journal_is_open_is_adopted_when_it_closes(address):
    plane, _ = address
    seen = []

    def another_change_lands_right_after_the_switch():
        if not seen:
            seen.append(plane.move(THIRD, "an-outside-operation"))
            instance_address.refresh_state(force=True)  # recorded, but one journal is still open

    instance_address._hooks.update(wake_pages=another_change_lands_right_after_the_switch,
                                   close_connections=lambda hosts: None)
    rename_to("new")
    run()
    assert binding().active_host == THIRD
    assert [entry["hostname"] for entry in instance_address.describe()["cleanup"]] == [OLD, NEW]


def test_c4_a_phone_paired_on_the_old_address_while_a_returning_name_settles_is_ended_too(address, monkeypatch):
    plane, clocks = address
    rename_to("new")
    run()
    before = health_pairing()
    during, after = [], []
    settle = instance_address._sleep

    def pair_while_the_name_settles(seconds):
        # The address being left is still served during the settle; a phone can finish pairing there.
        if seconds == instance_address.REACTIVATION_SETTLE_SECONDS and not during:
            during.append(health_pairing())
        settle(seconds)

    monkeypatch.setattr(instance_address, "_sleep", pair_while_the_name_settles)
    instance_address._hooks.update(
        wake_pages=lambda: after or after.append(health_pairing()), close_connections=lambda hosts: None)
    rename_to("old")
    run()
    assert binding().active_host == OLD and during and before not in active_health_devices()
    assert active_health_devices() == after  # only the phone paired on the new address is left


def test_b4_an_owed_read_is_not_asked_for_ever_of_an_instance_that_has_no_former_names(address):
    from config.v2_config import update_config_fields

    plane, _ = address
    update_config_fields(lambda config: setattr(config.remote_access.vibe_cloud, "instance_kind", "organization"))
    plane.calls.clear()
    instance_address._wake_worker(read_state=True)
    assert instance_address._read_when_asked() is True and not instance_address._worker_read_requested.is_set()
    assert plane.calls == []


def test_b4_a_definitive_refusal_of_the_state_read_is_told_apart_from_a_fault(address):
    plane, _ = address
    plane.fail = remote_access.BackendRequestError(404, {})
    instance_address._wake_worker(read_state=True)
    assert instance_address._read_when_asked() is False and instance_address._read_refused is True
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    assert instance_address._read_when_asked() is False and instance_address._read_refused is False
    plane.fail = remote_access.BackendRequestError(503, {})
    assert instance_address._read_when_asked() is False and instance_address._read_refused is False


def test_service_retries_worker_start_and_wakes_an_already_running_address_reader(monkeypatch):
    # Service-side sink only: no real connector, address daemon, service restart or HTTP.
    events = []

    class Worker:
        def __init__(self, *, name, target, daemon):
            assert name == "instance-address" and target is instance_address._worker_loop and daemon

        def start(self):
            events.append("start")
            if events.count("start") == 1:
                raise RuntimeError("fixture thread unavailable")

    owed, wake = threading.Event(), threading.Event()
    monkeypatch.setattr(instance_address, "threading", SimpleNamespace(Thread=Worker))
    monkeypatch.setattr(instance_address, "_worker_started", False)
    monkeypatch.setattr(instance_address, "_worker_read_requested", owed)
    monkeypatch.setattr(instance_address, "_worker_wake", wake)
    monkeypatch.setattr(instance_address, "_hooks", {})
    def monitor(config):
        events.append("monitors")
        return True

    monkeypatch.setattr(ui_server, "_start_remote_monitors", monitor)
    monkeypatch.setattr(ui_server, "_UI_RUNTIME_ACTIVE", False)
    assert not ui_server._on_claim_bound() and not events
    monkeypatch.setattr(ui_server, "_UI_RUNTIME_ACTIVE", True)
    assert not ui_server._on_claim_bound() and not owed.is_set()
    assert ui_server._on_claim_bound() and owed.is_set() and wake.is_set()
    owed.clear()
    wake.clear()
    assert ui_server._ensure_remote_access_monitoring() and not wake.is_set() and not owed.is_set()
    assert ui_server._on_claim_bound() and wake.is_set() and owed.is_set()
    assert events == ["monitors", "start", "monitors", "start", "monitors", "monitors"]
    monkeypatch.setattr(ui_server, "_start_remote_monitors", lambda config: False)
    assert not ui_server._on_claim_bound()  # monitor startup refusal cannot acknowledge both workers


@pytest.mark.parametrize("old_read", ["unpaired", "finished"])
def test_publication_during_an_older_address_read_is_still_owed(address, monkeypatch, old_read):
    plane, _ = address

    def overlapping():
        instance_address._wake_worker(read_state=True)  # the newly committed binding became visible
        return None

    with monkeypatch.context() as old:
        if old_read == "unpaired":
            old.setattr(instance_address, "current_binding", overlapping)
        else:
            def completed(*, force):
                assert force
                instance_address._wake_worker(read_state=True)
                return True
            old.setattr(instance_address, "refresh_state", completed)
        instance_address._wake_worker(read_state=True)
        assert instance_address._read_when_asked()
    assert instance_address._worker_read_requested.is_set()
    plane.calls.clear()
    assert instance_address._read_when_asked()
    assert not instance_address._worker_read_requested.is_set()
    assert plane.calls == [("POST", "hostname-state", {})]


def test_b4_a_read_that_is_owed_survives_a_failed_request(address):
    plane, _ = address
    instance_address.note_generation_hint(plane.generation + 1)  # avibe.bot says something changed
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    assert instance_address._read_when_asked() is False and instance_address._worker_read_requested.is_set()
    plane.fail = None
    plane.move(NEW, "an-outside-operation")
    run()
    assert binding().active_host == NEW


def test_a5_a_change_made_elsewhere_is_adopted_without_deleting_anything(address):
    plane, _ = address
    sign_in()
    phone = health_pairing()
    plane.move(NEW, "an-operation-this-computer-never-wrote")
    assert instance_address.refresh_state(force=True)
    run()
    assert binding().active_host == NEW
    assert active_health_devices() == [phone]
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(remote_device_sessions)).all() == []
        assert conn.execute(select(device_health_owner_checks)).all() == []
    described = instance_address.describe()
    assert described["unrecognized_change"]["previous_hostname"] == OLD
    assert described["unrecognized_change"]["hostname"] == NEW
    assert is_cleaning(get(OLD))  # the old name is cleaned like any cleanup name


def test_a5_our_own_operation_overtaken_by_another_deletes_nothing(address):
    plane, _ = address
    phone = health_pairing()
    rename_to("new")
    original = plane.rename

    def commit_then_move_again(body):
        receipt = original(body)
        plane.move(THIRD, "someone-elses-operation")
        return receipt

    plane.rename = commit_then_move_again
    run()
    assert binding().active_host == THIRD
    assert active_health_devices() == [phone]
    assert instance_address.describe()["unrecognized_change"]["hostname"] == THIRD


def test_b3_b4_taking_a_name_back_quiets_it_first_and_serves_it_only_after_the_commit(address):
    plane, clocks = address
    sign_in()
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    preview = instance_address.preview("old", ("loopback",))
    assert preview["is_return"] is True
    instance_address.change(preview["confirmation_id"], ("loopback",))
    seen = []
    original = plane.rename

    def observe(body):
        quiet = get(OLD, "/api/session")
        seen.append((quiet.status_code, is_cleaning(quiet)))
        return original(body)

    plane.rename = observe
    before = clocks.monotonic
    run()
    assert seen == [(503, False)]  # quiet before the request: no content, no clearing
    assert clocks.monotonic - before >= instance_address.REACTIVATION_SETTLE_SECONDS
    assert binding().active_host == OLD
    assert not is_cleaning(get(OLD, "/health")) and get(OLD, "/health").status_code == 200
    assert is_cleaning(get(NEW))
    assert [entry["hostname"] for entry in instance_address.describe()["cleanup"]] == [NEW]
    with get_cached_sqlite_engine().connect() as conn:
        # The returned name's list is closed at the switch, not at some later read: a mark
        # kept there would be read as true of a later retirement of the same name.
        assert OLD not in {row.hostname for row in conn.execute(select(instance_address_clearings))}
    # A to B to C: both former names are cleaned, each with its own date.
    rename_to("third")
    run()
    assert binding().active_host == THIRD and is_cleaning(get(OLD)) and is_cleaning(get(NEW))


def test_b3_a_slow_cleaning_response_holds_the_switch_back(address, monkeypatch):
    plane, clocks = address
    rename_to("new")
    run()
    assert instance_address.begin_cleanup_response(OLD)  # a cleaning response is still being written
    monkeypatch.setattr(instance_address._DRAINED, "wait", lambda timeout=None: clocks.advance(timeout or 1))
    rename_to("old")
    with pytest.raises(RuntimeError, match="did not drain"):
        run()
    assert not [call for call in plane.calls if call[1] == "hostname-renames" and call[2]["new_prefix"] == "old"]
    assert instance_address.begin_cleanup_response(OLD) is False  # quiet: no new cleaning response starts
    instance_address.end_cleanup_response(OLD)
    run()
    assert binding().active_host == OLD


def test_e7_a_request_that_meets_a_busy_role_lock_asks_again_and_is_cleaned(address):
    import asyncio
    import threading

    plane, _ = address
    rename_to("new")
    run()
    held = threading.Event()

    def hold_the_role_lock_briefly():
        with instance_address._LOCK:
            held.set()
            time.sleep(0.03)

    async def scenario():
        async def app(scope, receive, send):
            raise AssertionError("the application is never entered on a cleanup name")

        sent = []

        async def send(message):
            sent.append(message)

        holder = threading.Thread(target=hold_the_role_lock_briefly)
        middleware = ui_server._InstanceAddressRoleMiddleware(app)
        scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "headers": [(b"host", OLD.encode())]}
        assert await asyncio.to_thread(instance_address.cleanup_ready, OLD)
        holder.start()
        held.wait(timeout=5)
        assert await middleware._clean(scope, None, send, OLD) is True
        holder.join()
        return sent[0]["status"]

    assert asyncio.run(scenario()) == 410


def test_b3_a_crash_while_refusing_a_return_leaves_no_name_quiet_for_ever(address, monkeypatch):
    plane, clocks = address
    rename_to("new")
    run()
    plane.refuse = (409, {"error": "taken"})
    rename_to("old")
    original = instance_address.refresh_state

    def killed_after_the_journal_write(**kwargs):
        raise KeyboardInterrupt("process killed")

    monkeypatch.setattr(instance_address, "refresh_state", killed_after_the_journal_write)
    with pytest.raises(KeyboardInterrupt):
        run()
    monkeypatch.setattr(instance_address, "refresh_state", original)
    # A restart: the journal and the quiet state ended in the same write.
    for name, value in (("_authority", None), ("_quiet", set()), ("_quiet_loaded_for", None), ("_last_read_started", None)):
        monkeypatch.setattr(instance_address, name, value)
    stored = json.loads(instance_address._state_path().read_text(encoding="utf-8"))
    assert stored["journal"] is None and stored["quiet"] == []
    assert is_cleaning(get(OLD))


def test_b2_a_read_disowned_by_a_401_cannot_grant_cleaning_or_move_the_address(address, monkeypatch):
    import config.v2_config as v2_config

    plane, clocks = address
    rename_to("new")
    run()
    rename_to("old")
    # While the returning name settles, another read is answered 401: the binding is refused.
    settle = instance_address._sleep
    refused = []

    def settle_then_be_refused(seconds):
        settle(seconds)
        if not refused:
            refused.append(instance_address._drop_authority())

    monkeypatch.setattr(instance_address, "_sleep", settle_then_be_refused)
    assert instance_address._advance_operation() > 0  # the read taken before the 401 is not applied
    assert binding().active_host == NEW and instance_address._authority is None
    assert get(NEW, "/health").status_code == 200 and not is_cleaning(get(OLD))
    # And when the 401 lands during the config write itself: the address has moved, nothing is cleaned.
    write = v2_config.update_config_fields

    def write_then_be_refused(mutator, **kwargs):
        result = write(mutator, **kwargs)
        instance_address._drop_authority()
        return result

    monkeypatch.setattr(v2_config, "update_config_fields", write_then_be_refused)
    while instance_address._advance_operation() > 0:  # the journal only; the worker's next read comes below
        pass
    monkeypatch.setattr(v2_config, "update_config_fields", write)
    assert binding().active_host == OLD and instance_address._authority is None
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    assert get(NEW).status_code == 503 and not is_cleaning(get(NEW))
    plane.fail = None
    assert instance_address._worker_read_requested.is_set()  # the worker was asked to read again
    assert instance_address.refresh_state(force=True)
    assert is_cleaning(get(NEW))  # only a read made after the refusal grants it again


def test_rename_intent_blocks_pairing_before_redeem_and_releases_after_apply(address, monkeypatch):
    rename_to("new")
    calls = []
    monkeypatch.setattr(remote_access, "_normalize_pairing_backend_url", lambda value: calls.append(value) or (None, "test_boundary"))
    assert remote_access.pair("fixture-key", "https://issuer.invalid")["error"] == "operation_in_progress"
    assert not calls
    run()
    assert binding().active_host == NEW
    assert remote_access.pair("fixture-key", "https://issuer.invalid")["error"] == "test_boundary"
    assert calls == ["https://issuer.invalid"]


def test_prepared_pairing_blocks_rename_without_consuming_confirmation(address):
    from config.v2_config import V2Config

    plane, _ = address
    confirmation = instance_address.preview("new", ("loopback",))["confirmation_id"]
    record = remote_access._new_pairing_claim(V2Config.load(), "https://issuer.invalid", "fixture", pairing_key="fixture-key")
    remote_access._write_pending_pairing_record(record)
    before = list(plane.calls)
    with pytest.raises(instance_address.AddressError) as caught:
        instance_address.change(confirmation, ("loopback",))
    assert caught.value.code == "operation_in_progress" and plane.calls == before
    assert remote_access._read_pending_pairing_record() == (record, None)
    assert remote_access._retire_pending_pairing(operation_id=record["operation_id"])
    assert instance_address.change(confirmation, ("loopback",))[0] == 202


def test_rename_worker_retries_unreadable_binding_without_another_hint(address, monkeypatch):
    plane, _ = address
    rename_to("new")
    read_binding = instance_address.current_binding
    interrupted = False

    def intermittent_read():
        nonlocal interrupted
        if plane.operations and not interrupted:
            interrupted = True  # first guard after the issuer committed
            return None
        return read_binding()

    monkeypatch.setattr(instance_address, "current_binding", intermittent_read)
    waits = []

    def wait(timeout):
        if waits and timeout is None:
            raise StopIteration  # no external wake arrives: worker would now stay idle
        assert len(waits) < 10, "rename did not finish"
        waits.append(timeout)

    monkeypatch.setattr(instance_address._worker_wake, "wait", wait)
    with pytest.raises(StopIteration):
        instance_address._worker_loop()
    assert interrupted and len(waits) > 1
    assert binding().active_host == NEW
    requests = [payload["operation_id"] for _, suffix, payload in plane.calls if suffix == "hostname-renames"]
    assert len(requests) == 2 and requests[0] == requests[1]


def test_e7_the_last_check_before_the_headers_never_waits_for_the_role_lock(address):
    import threading

    plane, _ = address
    rename_to("new")
    run()
    assert instance_address.cleanup_ready(OLD)
    held, release = threading.Event(), threading.Event()

    def hold_the_role_lock():
        with instance_address._LOCK:
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_the_role_lock)
    holder.start()
    held.wait(timeout=5)
    started = time.monotonic()
    try:
        assert instance_address.begin_cleanup_response(OLD) is None  # a role is changing: no answer, at once
        assert time.monotonic() - started < 1
        instance_address.end_cleanup_response(OLD)  # and completion never takes that lock either
        assert time.monotonic() - started < 1
    finally:
        release.set()
        holder.join()
    assert instance_address.begin_cleanup_response(OLD) is True
    instance_address.end_cleanup_response(OLD)


def test_b3_a_refused_return_cleans_again_only_when_a_fresh_state_lists_the_name(address):
    plane, clocks = address
    rename_to("new")
    run()
    plane.refuse = (409, {"error": "taken"})
    rename_to("old")
    run()
    assert binding().active_host == NEW
    assert is_cleaning(get(OLD))
    plane.cleanup.pop(OLD)
    plane.generation += 1
    rename_to("old")
    run()
    assert get(OLD).status_code == 503


def test_b8_a_settings_save_cannot_write_the_address_and_a_new_pairing_inherits_nothing(address):
    from config.v2_config import update_config_fields
    from vibe import api

    plane, _ = address
    rename_to("new")
    run()
    assert is_cleaning(get(OLD))
    for field, value in (("public_url", f"https://{OLD}"), ("redirect_uri", f"https://{OLD}/auth/callback"), ("client_id", "other")):
        with pytest.raises(ValueError, match="not by a settings save"):
            api.save_config({"remote_access": {"vibe_cloud": {field: value}}}, generic_remote_access=True)
    assert binding().active_host == NEW
    # AV-13 supersedes B8's empty-value exception: Off is not an unpair.
    with pytest.raises(ValueError, match="binding is not changed by saving settings"):
        api.save_config({"remote_access": {"vibe_cloud": {"redirect_uri": ""}}}, generic_remote_access=True)
    with pytest.raises(ValueError, match="not by a settings save"):
        api.save_config({"remote_access": {"vibe_cloud": {"redirect_uri": f"https://{OLD}/auth/callback"}}},
                        generic_remote_access=True)
    assert settings_service.load_config().remote_access.vibe_cloud.redirect_uri == f"https://{NEW}/auth/callback"
    # Turning remote access off through the same save still works.
    api.save_config({"remote_access": {"vibe_cloud": {"enabled": False}}}, generic_remote_access=True)
    assert settings_service.load_config().remote_access.vibe_cloud.enabled is False

    def repair(config):
        cloud = config.remote_access.vibe_cloud
        cloud.enabled = True
        cloud.instance_id = "inst_other"
        cloud.instance_secret = "another-secret"

    update_config_fields(repair)
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    assert get(OLD).status_code == 503  # the old pairing's permission does not carry over
    assert instance_address.describe()["cleanup"] == []


def test_a6_after_binding_again_the_old_name_is_still_listed_and_can_be_taken_back(address):
    from config.v2_config import update_config_fields

    plane, clocks = address
    _cookie, sid = sign_in()
    rename_to("new")
    run()
    receipt = instance_address.receipt_for_session(sid)
    update_config_fields(lambda config: setattr(config.remote_access.vibe_cloud, "instance_secret", "a-new-device-secret"))
    plane.fail = RuntimeError("resource_acl_device_unavailable")
    clocks.advance(6)
    assert get(OLD).status_code == 503  # the old secret's permission is gone with the old secret
    plane.fail = None
    clocks.advance(6)
    assert is_cleaning(get(OLD))  # read again under the new secret: the same instance, the same names
    assert [entry["hostname"] for entry in instance_address.describe()["cleanup"]] == [OLD]
    assert instance_address.record_cleared(OLD, receipt)  # receipts do not depend on the binding
    rename_to("old")
    run()
    assert binding().active_host == OLD and instance_address.describe()["unrecognized_change"] is None


def test_c1_requests_open_on_the_old_name_are_ended_at_the_switch(address):
    """A stream admitted without any session (a public Show page's) has nothing else to end it."""

    import asyncio

    async def scenario():
        started, cancelled = asyncio.Event(), []

        async def never_ending_app(scope, receive, send):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(scope["path"])
                raise

        middleware = ui_server._InstanceAddressRoleMiddleware(never_ending_app)
        sent = []

        async def send(message):
            sent.append(message)

        def request(host):
            scope = {"type": "http", "method": "GET", "path": "/show/x/events", "headers": [(b"host", host.encode())]}
            return asyncio.create_task(middleware(scope, None, send))

        old_stream, local_stream = request(OLD), request("localhost:5123")
        # Behind a trusted proxy the public name arrives as a forwarded Host.
        forwarded_scope = {"type": "http", "method": "GET", "path": "/show/y/events",
                           "headers": [(b"host", b"127.0.0.1:5123"), (b"x-forwarded-host", OLD.encode())]}
        forwarded_stream = asyncio.create_task(middleware(forwarded_scope, None, send))
        await started.wait()
        await asyncio.sleep(0.05)
        instance_address._hooks.update(wake_pages=lambda: None, close_connections=ui_server._close_address_connections)
        await asyncio.to_thread(lambda: (rename_to("new"), run()))
        # Ended, not failed: no exception leaves the handler (also on Python 3.10, which has no ``uncancel``).
        assert await asyncio.wait_for(asyncio.gather(old_stream, forwarded_stream), timeout=5) == [None, None]
        assert sorted(cancelled) == ["/show/x/events", "/show/y/events"]
        assert not local_stream.done()  # a page on this computer's own address is untouched
        local_stream.cancel()
        await asyncio.gather(local_stream, return_exceptions=True)

    asyncio.run(scenario())


# --- D1, F2: receipts and the final frame -------------------------------------------------


def test_f2_only_a_sessions_own_receipt_marks_its_own_device_once(address):
    plane, _ = address
    owner_cookie, owner_sid = sign_in()
    _member_cookie, member_sid = sign_in("member", "member")
    owner_receipt = instance_address.receipt_for_session(owner_sid)
    assert owner_receipt != owner_sid and owner_sid not in owner_receipt
    rename_to("new")
    run()

    def states():
        (old,) = instance_address.describe()["cleanup"]
        return {device["account"]["email"]: device["state"] for device in old["devices"]}

    client = ui_server.app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, owner_cookie, domain=OLD)
    page = client.get("/", base_url=f"https://{OLD}").text
    assert f'<meta name="avibe-address-receipt" content="{owner_receipt}">' in page
    assert states() == {"owner@test.invalid": "unconfirmed", "member@test.invalid": "unconfirmed"}  # a visit marks nothing
    stranger = get(OLD).text
    assert '<meta name="avibe-address-receipt"' not in stranger
    report = lambda body: ui_server.app.test_client().post(  # noqa: E731
        instance_address.CLEARED_REPORT_PATH, base_url=f"https://{OLD}", data=body)
    for forged in ("x" * 43, owner_sid, "", "y" * 5000):
        assert report(forged).status_code == 204
        assert report(forged).headers["X-Avibe-Address"] == "retired"
    assert states()["owner@test.invalid"] == "unconfirmed"
    reported = report(owner_receipt)
    # The page already emptied its storage; this answer clears the HTTP cache, which its script cannot reach.
    assert reported.status_code == 204 and reported.headers["Clear-Site-Data"] == '"cache"' and reported.content == b""
    assert states() == {"owner@test.invalid": "cleared", "member@test.invalid": "unconfirmed"}
    assert instance_address.record_cleared(OLD, owner_receipt) is False  # once
    assert instance_address.record_cleared(NEW, instance_address.receipt_for_session(member_sid)) is False


def test_b2_a_401_on_the_request_itself_ends_cleaning_at_once(address):
    plane, _ = address
    rename_to("new")
    run()
    assert instance_address.cleanup_ready(OLD)
    rename_to("third")
    plane.refuse = (401, {"error": "invalid_device_secret"})
    original = plane.state
    plane.state = lambda: (_ for _ in ()).throw(RuntimeError("resource_acl_device_unavailable"))
    run()  # refused, and the read after the refusal fails too
    plane.state = original
    assert instance_address.describe()["operation"]["error"] == "pairing_invalid"
    assert not instance_address.cleanup_ready(OLD) and instance_address._authority is None


def test_f2_a_report_whose_answer_was_not_admitted_marks_nothing(address, monkeypatch):
    plane, clocks = address
    _cookie, sid = sign_in()
    rename_to("new")
    run()
    receipt = instance_address.receipt_for_session(sid)
    read_body = ui_server._bounded_request_text

    async def permission_lapses_while_the_body_is_read(receive, limit):
        text = await read_body(receive, limit)
        plane.fail = RuntimeError("resource_acl_device_unavailable")
        clocks.advance(61)
        return text

    monkeypatch.setattr(ui_server, "_bounded_request_text", permission_lapses_while_the_body_is_read)
    answer = ui_server.app.test_client().post(
        instance_address.CLEARED_REPORT_PATH, base_url=f"https://{OLD}", data=receipt)
    # No cache-clearing answer went out, so the browser's HTTP cache was not cleared.
    assert answer.status_code == 503 and "Clear-Site-Data" not in answer.headers
    (old,) = instance_address.describe()["cleanup"]
    assert [device["state"] for device in old["devices"]] == ["unconfirmed"]


def test_b8_work_begun_for_one_instance_never_writes_over_the_next_pairing(address, monkeypatch):
    from config.v2_config import update_config_fields

    plane, clocks = address
    sign_in()
    phone = health_pairing()
    rename_to("new")
    settle = instance_address._sleep
    repaired = []

    def paired_as_another_instance_during_the_grace(seconds):
        settle(seconds)
        if seconds == instance_address.FINAL_FRAME_GRACE_SECONDS and not repaired:
            def repair(config):
                cloud = config.remote_access.vibe_cloud
                cloud.instance_id, cloud.instance_secret = "inst_other", "another-secret"
                cloud.public_url = "https://other-app.avibe.bot"

            update_config_fields(repair)
            # The new pairing already has state of its own.
            instance_address._update_state(
                "inst_other", lambda state: state.__setitem__("last_operation", {"state": "applied", "theirs": True}))
            repaired.append(True)

    monkeypatch.setattr(instance_address, "_sleep", paired_as_another_instance_during_the_grace)
    with pytest.raises(instance_address._PairingChanged):
        run()
    run()  # nothing left to do for this pairing
    stored = json.loads(instance_address._state_path().read_text(encoding="utf-8"))
    assert stored["instance_id"] == "inst_other" and stored["last_operation"] == {"state": "applied", "theirs": True}
    assert stored["journal"] is None
    assert active_health_devices() == [phone]  # the old change's Health step did not run against the new pairing


def test_d1_only_streams_open_at_the_switch_are_told_the_new_origin(address):
    plane, _ = address
    _cookie, sid = sign_in()
    assert instance_address.retired_destination(OLD) is None
    rename_to("new")
    run()
    assert instance_address.retired_destination(OLD + ":443") == f"https://{NEW}"
    assert instance_address.retired_destination(NEW) is None and instance_address.retired_destination("localhost") is None
    frame = ui_server._address_retired_frame(f"https://{NEW}", instance_address.receipt_for_session(sid))
    assert frame.startswith("event: instance.address-retired\ndata: ")
    assert json.loads(frame.split("data: ", 1)[1]) == {
        "moved_to": f"https://{NEW}", "receipt": instance_address.receipt_for_session(sid)}
    connected = ui_server._workbench_event_connected_frame(3, address_receipt="r")
    assert json.loads(connected.split("data: ", 1)[1])["address_receipt"] == "r"
    assert "address_receipt" not in ui_server._workbench_event_connected_frame(3)
    from vibe.authorization import can_receive_workbench_event, context_from_session_payload

    member = context_from_session_payload({**_session_claims(settings_service.load_config(), role="member"),
                                           "sub": "member", "email": "member@test.invalid"})
    assert not can_receive_workbench_event(member, instance_address.ADDRESS_RETIRED_EVENT)
    assert not can_receive_workbench_event(member, instance_address.ADDRESS_CHANGED_EVENT)


def test_the_browser_suite_replays_exactly_what_the_instance_answers():
    """``ui/e2e/address-retired`` drives real browsers with these bytes; they must be the real ones."""

    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "ui" / "e2e" / "address-retired" / "write_fixture.py"
    spec = importlib.util.spec_from_file_location("address_retired_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads(module.FIXTURE.read_text(encoding="utf-8")) == module.build(), (
        "run: python ui/e2e/address-retired/write_fixture.py"
    )


# --- migration ---------------------------------------------------------------------------


@pytest.mark.no_sqlite_template
def test_clearing_list_migration_round_trip_keeps_the_registry(tmp_path):
    path = tmp_path / "registry.sqlite"
    previous, head = "20261121_0096", "20261121_0097"
    migrations.run_migrations(path, revision=previous)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO remote_device_sessions(session_id,binding,subject,email,login_role,device_id,identity_source,kind,"
                   "signed_in_at,last_active_at,cookie_exp,expires_at) VALUES ('old','binding','person','fixture','owner','device','session','browser',1,1,10,50)")
    migrations.run_migrations(path, revision=head)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert db.execute("SELECT count(*) FROM instance_address_clearings").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM instance_address_clearing_receipts").fetchone() == (0,)
        assert db.execute("SELECT session_id FROM remote_device_sessions").fetchall() == [("old",)]
    command.downgrade(migrations.alembic_config(path), previous)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT session_id FROM remote_device_sessions").fetchall() == [("old",)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'instance_address%'").fetchall() == []


@pytest.mark.parametrize("hint,delay", [(48, 5.0), (2, 2.0), (None, 5.0)])
def test_an_accepted_rename_says_applying_at_once_and_is_asked_again_within_seconds(address, monkeypatch, hint, delay):
    plane, _ = address
    monkeypatch.setattr(instance_address, "_FIRST_ANSWER_WAIT_SECONDS", 12.0)
    answer = plane.rename
    pending = [True]

    def accepted_first(body):
        if pending.pop() if pending else False:
            return {"status": "pending", "operation_id": body["operation_id"],
                    **({"retry_after_seconds": hint} if hint is not None else {})}
        return answer(body)

    plane.rename = accepted_first
    confirmation = instance_address.preview("new", ("loopback",))["confirmation_id"]
    steps = []

    def one_worker_step(timeout=None):
        # The worker sends once; the route must not wait for avibe.bot's commit.
        steps.append(instance_address._advance_operation())

    monkeypatch.setattr(instance_address._first_answer, "wait", one_worker_step)
    status, body = instance_address.change(confirmation, ("loopback",))
    assert (status, body) == (202, {"ok": True, "state": "applying"})
    assert steps == [delay]  # answered after the first reply, not after 12 seconds
    journal = instance_address._journal(binding())
    assert journal["phase"] == "intent" and journal["accepted"] and journal["may_have_landed"]
    assert instance_address.describe()["operation"] == {"state": "applying", "hostname": NEW, "error": None}
    # A restart keeps the operation: the same request is sent again and its receipt applied.
    monkeypatch.setattr(instance_address, "_BINDING_CACHE", None)
    monkeypatch.setattr(instance_address, "_confirmations", {})
    run()
    sent = [call[2]["operation_id"] for call in plane.calls if call[1] == "hostname-renames"]
    assert sent == [journal["operation_id"]] * 2
    assert binding().active_host == NEW and instance_address._journal(binding()) is None
