"""The same browser signing in again keeps its one device row; its old session is signed out.

Every route test runs the real `/auth/login` -> `/auth/callback` handshake (state, PKCE, nonce)
in one cookie jar; only the exchange with avibe.bot is replaced.
"""

import json
import threading
import time
import urllib.parse

import pytest
from sqlalchemy import event, func, select, update

from storage import device_health_registry, device_registry
from storage.db import get_cached_sqlite_engine
from storage.models import (
    device_activity_events as events,
    device_function_settings as function_settings,
    device_health_links as health,
    device_session_extensions as ext,
    remote_access_authorizations as authorizations,
    remote_account_refs as refs,
    remote_device_push_links as push_links,
    remote_device_sessions as sessions,
    state_meta,
)
from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX
from tests.test_instance_device_registry import ORIGIN, install, listing, report, sign_out  # noqa: F401
from tests.test_remote_access_vibe_cloud import _session_claims
from tests.ui_server_test_helpers import csrf_headers
from storage.device_health_tickets import digest
from vibe import remote_access, ui_server

CHROME = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
REF_A = "asr1_" + "a" * 43
REF_B = "asr1_" + "b" * 43


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(ui_server, "_auth_rate_limited", lambda: False)
    published = []
    monkeypatch.setattr(remote_access, "publish_browser_session_revoked", published.append)
    return published


def identity_of(cfg, cookie):
    return remote_access.parse_session_identity(cfg, cookie, allow_revoked=True)


def sid_of(cfg, cookie):
    return identity_of(cfg, cookie)["browser_session_id"]


def browser(cookie=None):
    c = ui_server.app.test_client()
    if cookie:
        c.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    return c


def held(c):
    values = [k.value for k in c._client.cookies.jar if k.name == remote_access.SESSION_COOKIE_NAME]
    assert len(values) <= 1, "one session cookie per jar"
    return values[0] if values else None


def first_login(cfg, subject="owner", role="owner", ref=None):
    return remote_access.make_session_cookie(cfg, subject + "@test.invalid", subject,
                                             session_claims=_session_claims(cfg, role=role),
                                             device_headers=CHROME, account_session_ref=ref)


def set_authorization(cfg, cookie, *, lapsed):
    """The authorization behind a cookie lapses (so /auth/login really starts OAuth), or comes back."""
    reference = identity_of(cfg, cookie)["authorization_ref"]
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(update(authorizations).where(authorizations.c.id == reference)
                     .values(scope_kind=None, expires_at=1 if lapsed else 2**40))


def start(c):
    """`/auth/login` in this cookie jar: the real handshake (state, PKCE, nonce)."""
    started = c.get("/auth/login?next=/", base_url=ORIGIN, headers=CHROME)
    assert started.status_code == 302 and started.headers["Location"] != "/", "login must start OAuth"
    return urllib.parse.parse_qs(urllib.parse.urlsplit(started.headers["Location"]).query)


def finish(c, monkeypatch, cfg, asked, subject="owner", role="owner", ref=None):
    """`/auth/callback` for that handshake; only avibe.bot's code exchange is replaced."""
    claims = {"nonce": asked["nonce"][0], "email": subject + "@test.invalid", "sub": subject}
    session_claims = _session_claims(cfg, role=role)
    if ref:
        claims.update(avibe_account_session_ref=ref, vibe_instance_access_source="owner", vibe_instance_role="owner")
    monkeypatch.setattr(remote_access, "exchange_oauth_code",
                        lambda *_a, **_k: {"claims": claims, "session_claims": session_claims})
    return c.get("/auth/callback?code=fixture&state=" + urllib.parse.quote(asked["state"][0]),
                 base_url=ORIGIN, headers=CHROME)


def sign_in(c, monkeypatch, cfg, **kw):
    """One real sign-in in this cookie jar. Returns the callback response."""
    return finish(c, monkeypatch, cfg, start(c), **kw)


def begin_relogin(c, cfg):
    """Start signing in again with the jar's current cookie: its authorization lapses just long enough
    for `/auth/login` to start OAuth, then comes back, so only a sign-out keeps the cookie out."""
    old = held(c)
    set_authorization(cfg, old, lapsed=True)
    asked = start(c)
    set_authorization(cfg, old, lapsed=False)
    return asked


def relogin(c, monkeypatch, cfg, **kw):
    return finish(c, monkeypatch, cfg, begin_relogin(c, cfg), **kw)


def row(sid):
    with get_cached_sqlite_engine().connect() as conn:
        return conn.execute(select(sessions).where(sessions.c.session_id == sid)).mappings().first()


def tombstone(sid):
    with get_cached_sqlite_engine().connect() as conn:
        return conn.scalar(select(state_meta.c.value_json).where(
            state_meta.c.key == REVOKED_BROWSER_SESSION_KEY_PREFIX + sid))


def activity(device):
    with get_cached_sqlite_engine().connect() as conn:
        return dict(conn.execute(select(events.c.kind, func.count()).where(events.c.device_id == device)
                                 .group_by(events.c.kind)).all())


def authenticated(cookie):
    return browser(cookie).get("/api/session", base_url=ORIGIN).get_json()["authenticated"]


def mine(c):
    return [d for d in listing(c)["mine"] if d["id"] != "host"]


@pytest.fixture
def issuer(install):
    """An issuer pairing names the account sign-out epoch, so account references are enrolled."""
    install.remote_access.vibe_cloud.backend_url = "https://issuer.invalid"
    install.remote_access.vibe_cloud.instance_secret = "test-device-secret"
    install.save()
    return install


def in_lock_order(monkeypatch, first, second, *, first_in, second_in):
    """Run `first()` on another thread and `second()` here so that, inside `first_in` and
    `second_in`: `second` has done everything before its lock (verified the old cookie, stored its
    authorization), `first` then takes the write lock, `second` reaches its own lock and waits on
    it, and only then does `first` commit. Two connections, real SQLite locking."""
    local = threading.local()
    entered, held, go = threading.Event(), threading.Event(), threading.Event()

    def tagged(module, name, tag):
        real = getattr(module, name)

        def wrapped(*args, **kwargs):
            if tag == "first":
                assert entered.wait(10), "the second never got to its lock"
            else:
                entered.set()
                assert held.wait(10), "the first never took the lock"
            local.tag = tag
            try:
                return real(*args, **kwargs)
            finally:
                local.tag = None
        monkeypatch.setattr(module, name, wrapped)

    tagged(*first_in, "first")
    tagged(*second_in, "second")

    def before(conn, cursor, statement, *args):
        if statement == "BEGIN IMMEDIATE" and getattr(local, "tag", None) == "second" and not go.is_set():
            out["first held the lock"] = held.is_set() and worker.is_alive()
            go.set()

    def after(conn, cursor, statement, *args):
        if statement == "BEGIN IMMEDIATE" and getattr(local, "tag", None) == "first" and not held.is_set():
            held.set()
            assert go.wait(10), "the second never reached its lock"

    engine = get_cached_sqlite_engine()
    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    out = {}

    def run():
        try:
            out["first"] = first()
        except BaseException as exc:  # surfaced below
            out["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    try:
        result = second()
    finally:
        entered.set()
        held.set()
        go.set()
        worker.join(10)
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
    if "error" in out:
        raise out["error"]
    assert out.get("first held the lock"), "the second took its lock without waiting on the first"
    return out["first"], result


class Hold:
    """A plain write transaction to hold the lock with, running `inside` while it is held."""

    def __init__(self, inside=lambda: None):
        self.inside = inside

    def run(self):
        with get_cached_sqlite_engine().begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            self.inside()


def refs_of(sid):
    with get_cached_sqlite_engine().connect() as conn:
        return conn.execute(select(refs.c.reference).where(refs.c.sid == sid)).scalars().all()


# ── 1. The plain case ───────────────────────────────────────────────────────────────────────────

def test_signing_in_again_keeps_one_row_and_signs_the_old_session_out(install, monkeypatch, quiet):
    old = first_login(install)
    c = browser(old)
    (before,) = mine(c)
    old_sid = sid_of(install, old)
    old_row = row(old_sid)
    answer = relogin(c, monkeypatch, install)
    assert answer.status_code == 302
    new = held(c)
    assert new and new != old and authenticated(new)
    (after,) = mine(c)
    assert after["id"] == before["id"]
    assert row(old_sid) is None and int(tombstone(old_sid)) >= old_row["expires_at"]
    assert not authenticated(old)
    assert browser(old).get("/api/devices", base_url=ORIGIN).status_code in (401, 403)
    assert quiet == [old_sid]
    assert activity(before["id"]) == {"signedIn": 2}


# ── 2. Push, settings and Health stay with the row ──────────────────────────────────────────────

def subscribe(c, endpoint, push_id="stable-push-id"):
    return c.post("/api/web-push/subscriptions", base_url=ORIGIN, headers=csrf_headers(c, ORIGIN), json={
        "device_id": push_id, "device_label": "Display only",
        "subscription": {"endpoint": endpoint, "keys": {"auth": "test-auth", "p256dh": "test-public"}},
    })


def test_push_and_settings_follow_the_row_and_a_stopped_one_stays_stopped(install, monkeypatch):
    from storage.web_push_service import count_enabled, disable_subscription

    c = browser(first_login(install, "member", "member"))
    assert subscribe(c, "https://example.invalid/on").status_code == 200
    (before,) = mine(c)
    assert before["capabilities"]["push_enabled"]
    old_sid = sid_of(install, held(c))
    binding = remote_access.device_binding(install)
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(function_settings.insert().values(binding=binding, subject="member", device_id=before["id"],
                                                       notifications_on=0, schedule_on=0, schedule_capable=1))
    relogin(c, monkeypatch, install, subject="member", role="viewer")
    (after,) = mine(c)
    assert after["id"] == before["id"] and after["capabilities"]["push_enabled"]
    new_sid = sid_of(install, held(c))
    with get_cached_sqlite_engine().begin() as conn:
        link = conn.execute(select(push_links)).mappings().one()
        assert (link["session_id"], link["login_role"]) == (new_sid, "viewer") and old_sid != new_sid
        assert count_enabled(conn, user_key="remote:member") == 1
        assert conn.execute(select(function_settings.c.notifications_on, function_settings.c.schedule_on)
                            .where(function_settings.c.device_id == before["id"])).one() == (0, 0)
        disable_subscription(conn, endpoint="https://example.invalid/on", user_key="remote:member")
    relogin(c, monkeypatch, install, subject="member", role="viewer")
    with get_cached_sqlite_engine().connect() as conn:
        assert count_enabled(conn, user_key="remote:member") == 0


def test_another_sessions_and_unproven_push_links_are_not_taken(issuer, monkeypatch):
    install = issuer
    # Same account, two cookie jars, one logical device (a shared push id); each has its own data.
    a = browser(first_login(install, ref=REF_A))
    b = browser(first_login(install, ref=REF_B))
    assert subscribe(a, "https://example.invalid/a").status_code == 200
    assert subscribe(b, "https://example.invalid/b").status_code == 200
    a_sid, b_sid = sid_of(install, held(a)), sid_of(install, held(b))
    assert row(a_sid)["device_id"] == row(b_sid)["device_id"]
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(ext.insert().values(session_id=b_sid, ticket_window=3, ticket_attempts=1, schedule_principal="b"))
        # A's own link loses its proof: a different push id than its subscription's.
        conn.execute(update(push_links).where(push_links.c.session_id == a_sid).values(push_id="someone-else"))
        b_link = dict(conn.execute(select(push_links).where(push_links.c.session_id == b_sid)).mappings().one())
        b_ext = dict(conn.execute(select(ext).where(ext.c.session_id == b_sid)).mappings().one())
    assert refs_of(b_sid) == [REF_B]
    relogin(a, monkeypatch, install, ref="asr1_" + "c" * 43)
    with get_cached_sqlite_engine().connect() as conn:
        assert dict(conn.execute(select(push_links).where(push_links.c.subscription_id == b_link["subscription_id"]))
                    .mappings().one()) == b_link
        assert dict(conn.execute(select(ext).where(ext.c.session_id == b_sid)).mappings().one()) == b_ext
        assert conn.execute(select(push_links.c.session_id).where(push_links.c.push_id == "someone-else")).scalar() == a_sid
    assert refs_of(b_sid) == [REF_B] and refs_of(a_sid) == []
    assert refs_of(sid_of(install, held(a))) == ["asr1_" + "c" * 43]
    assert row(b_sid) is not None and tombstone(b_sid) is None and authenticated(held(b))


# ── 3. Health: completed pairings stay, unused codes go with the old session ───────────────────
# Two real SQLite files: the registry and the private Health store (as tests/test_device_health_cleanup.py).

def health_phone(install, store):
    """An iPhone App session that issued a pairing code."""
    from tests.test_device_health_cleanup import code_for, phone

    cookie, viewer, device = phone(install, "phone")
    identity, payload = code_for(store, viewer)
    return cookie, viewer, device, identity, payload


def interrupted_pair(store, payload, identity):
    """The private store paired, the registry transaction did not: its link still holds the code."""
    with get_cached_sqlite_engine().connect() as conn:
        link = conn.execute(select(health).where(health.c.code_hash == digest(payload["pairing_code"]))).mappings().one()
    return store.pair(payload, identity, reserved_device_id=link["mobile_id"])["device_id"]


def mobile_revoked(store, mid):
    with store.transaction() as mobile:
        return mobile.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0] is not None


def code_kept(store, payload):
    with store.transaction() as mobile:
        return mobile.execute("SELECT 1 FROM pairings WHERE code_hash=?", (digest(payload["pairing_code"]),)).fetchone() is not None


def test_an_unused_code_is_cleared_from_both_stores_and_cannot_be_redeemed(install, monkeypatch):
    from core.mobile_health_contract import MobileError
    from storage.mobile_store import MobileStore

    store = MobileStore()
    cookie, viewer, device, identity, payload = health_phone(install, store)
    assert code_kept(store, payload)
    c = browser(cookie)
    relogin(c, monkeypatch, install)
    assert mine(c)[0]["id"] == device
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(health)).first() is None
    assert not code_kept(store, payload)
    with pytest.raises(MobileError):
        device_health_registry.pair(store, payload, identity)


def test_an_unused_code_is_refused_from_the_hand_over_on_even_while_its_cleanup_waits(install, monkeypatch):
    from core.mobile_health_contract import MobileError
    from storage.mobile_store import MobileStore

    store = MobileStore()
    cookie, viewer, device, identity, payload = health_phone(install, store)
    real = device_health_registry.MobileStore
    monkeypatch.setattr(device_health_registry, "MobileStore", None)  # the cleanup cannot run yet
    c = browser(cookie)
    relogin(c, monkeypatch, install)
    assert code_kept(store, payload) and authenticated(held(c))
    with pytest.raises(MobileError) as refused:
        device_health_registry.pair(store, payload, identity)
    assert refused.value.status == 401
    with store.transaction() as mobile:
        assert mobile.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    monkeypatch.setattr(device_health_registry, "MobileStore", real)
    device_health_registry.maintain(get_cached_sqlite_engine(), int(time.time()))
    assert not code_kept(store, payload)
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(health)).first() is None


def test_a_code_that_paired_before_the_registry_saw_it_survives_signing_in_again(install, monkeypatch):
    from storage.mobile_store import MobileStore

    store = MobileStore()
    cookie, viewer, device, identity, payload = health_phone(install, store)
    mid = interrupted_pair(store, payload, identity)
    # The private store cannot be cleaned while signing in again: the code is refused at once.
    real = device_health_registry.MobileStore
    monkeypatch.setattr(device_health_registry, "MobileStore", None)
    c = browser(cookie)
    relogin(c, monkeypatch, install)
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(health.c.cleanup_pending).where(health.c.mobile_id == mid)) == 2
    # The ordinary retry: the pairing the private store completed stays.
    monkeypatch.setattr(device_health_registry, "MobileStore", real)
    device_health_registry.maintain(get_cached_sqlite_engine(), int(time.time()))
    assert not mobile_revoked(store, mid)
    with get_cached_sqlite_engine().connect() as conn:
        link = conn.execute(select(health).where(health.c.mobile_id == mid)).mappings().one()
    assert (link["code_hash"], link["cleanup_pending"], link["device_id"]) == (None, None, device)


@pytest.mark.parametrize("local", [False, True])
def test_signing_the_device_out_before_the_retry_decides_the_pending_code(install, monkeypatch, local):
    from storage.mobile_store import MobileStore

    store = MobileStore()
    cookie, viewer, device, identity, payload = health_phone(install, store)
    mid = interrupted_pair(store, payload, identity)
    real = device_health_registry.MobileStore
    monkeypatch.setattr(device_health_registry, "MobileStore", None)
    c = browser(cookie)
    relogin(c, monkeypatch, install)
    new_sid = sid_of(install, held(c))
    if local:
        # Direct loopback: codes only, no unpair, and it never lowers an unpair.
        ids, *_ = device_registry.sign_out(remote_access.device_binding(install), "owner", owner=True, caller=None,
                                           device_id=device, now=int(time.time()), lifetime=86400)
        assert ids == [new_sid]
    else:
        owner = browser(first_login(install, "admin", "owner"))
        assert sign_out(owner, device + "/sign-out").status_code == 200
    assert tombstone(new_sid) is not None and not authenticated(held(c))
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(health.c.cleanup_pending).where(health.c.mobile_id == mid)) == (2 if local else 1)
    monkeypatch.setattr(device_health_registry, "MobileStore", real)
    device_health_registry.maintain(get_cached_sqlite_engine(), int(time.time()))
    assert mobile_revoked(store, mid) is (not local)


def test_a_full_unpair_is_never_lowered(install):
    from storage.mobile_store import MobileStore

    store = MobileStore()
    cookie, viewer, device, identity, payload = health_phone(install, store)
    binding, subject, _, sid = viewer
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(update(health).values(cleanup_pending=1))
        device_health_registry.queue_disconnect(conn, binding, subject, device, sids=[sid],
                                                remove_pairing=False, codes_only=True)
        assert set(conn.scalars(select(health.c.cleanup_pending))) == {1}


def test_ticket_counters_move_and_the_schedule_principal_does_not(install, monkeypatch):
    c = browser(first_login(install))
    old_sid = sid_of(install, held(c))
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(ext.insert().values(session_id=old_sid, ticket_window=7, ticket_attempts=2,
                                         schedule_principal='{"role":"owner"}'))
    relogin(c, monkeypatch, install, role="viewer")
    with get_cached_sqlite_engine().connect() as conn:
        counters = conn.execute(select(ext).where(ext.c.session_id == sid_of(install, held(c)))).mappings().one()
        assert conn.execute(select(ext).where(ext.c.session_id == old_sid)).first() is None
    assert (counters["ticket_window"], counters["ticket_attempts"], counters["schedule_principal"]) == (7, 2, None)


# ── 4. What does nothing ────────────────────────────────────────────────────────────────────────

def test_another_accounts_cookie_is_left_alone(install, monkeypatch, quiet):
    old = first_login(install, "someone", "member")
    c = browser(old)
    old_sid = sid_of(install, old)
    relogin(c, monkeypatch, install, subject="owner")
    assert row(old_sid) is not None and tombstone(old_sid) is None and authenticated(old)
    assert quiet == []


@pytest.mark.parametrize("how", ["signed-out", "expired", "fenced", "re-paired", "unregistered"])
def test_an_old_cookie_that_no_longer_counts_changes_nothing(install, monkeypatch, quiet, how):
    old = first_login(install)
    c = browser(old)
    old_sid = sid_of(install, old)
    binding = remote_access.device_binding(install)
    before = row(old_sid)
    set_authorization(install, old, lapsed=True)
    if how == "signed-out":
        remote_access.revoke_browser_session(identity_of(install, old))
    elif how == "expired":
        real = remote_access.time.time
        monkeypatch.setattr(remote_access.time, "time", lambda: real() + 91 * 86400)
    elif how == "fenced":
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(sessions.delete().where(sessions.c.session_id == old_sid))
            device_registry._deadline(conn, device_registry._cutoff_key(binding, "owner"), before["signed_in_at"] + 1,
                                      before["signed_in_at"] + 86400)
    elif how == "re-paired":
        install.remote_access.vibe_cloud.session_secret = "another-secret"
        install.save()
    elif how == "unregistered":
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(sessions.delete().where(sessions.c.session_id == old_sid))
    previous = remote_access.same_browser_session(install, old, "owner")
    assert previous is None or how == "unregistered"
    if how == "expired":
        return  # nothing else to see: the cookie is not presented as current at all
    quiet.clear()
    sign_in(c, monkeypatch, install)
    assert quiet == []
    if how in ("signed-out", "fenced", "unregistered"):
        assert row(old_sid) is None
    else:
        assert row(old_sid) == before and tombstone(old_sid) is None


# ── 5. Issuance failure leaves the old session as it was ────────────────────────────────────────

@pytest.mark.parametrize("why", ["sid-of-another-session", "sid-tombstoned", "sid-of-unregistered-cookie"])
def test_a_new_session_that_cannot_be_issued_replaces_nothing(issuer, monkeypatch, quiet, why):
    install = issuer
    old = first_login(install, ref=REF_A)
    c = browser(old)
    old_sid = sid_of(install, old)
    if why == "sid-of-unregistered-cookie":
        # A current, verified cookie of this account with no registry row: nothing to replace, and
        # still its id cannot be the new session's.
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(refs.delete().where(refs.c.sid == old_sid))
            conn.execute(sessions.delete().where(sessions.c.session_id == old_sid))
    sibling = first_login(install, ref="asr1_" + "s" * 43)
    sibling_sid = sid_of(install, sibling)
    sibling_before, sibling_refs = row(sibling_sid), refs_of(sibling_sid)
    before, old_refs = row(old_sid), refs_of(old_sid)
    # Another registered session holds the id: the insert itself finds it taken.
    taken = {"sid-of-another-session": sibling_sid, "sid-tombstoned": "x" * 32}.get(why, old_sid)
    if why == "sid-tombstoned":
        with get_cached_sqlite_engine().begin() as conn:
            device_registry._tombstone(conn, taken, 2**40, 0)
    real_token = remote_access.secrets.token_urlsafe
    # Every 24-byte token is fixed (the browser session id, and the handshake nonce, which only has to
    # match itself); other sizes stay random.
    monkeypatch.setattr(remote_access.secrets, "token_urlsafe", lambda n=None: taken if n == 24 else real_token(n))
    # Without an account reference to enroll, so only the insert can notice the taken id.
    ref = None if why == "sid-of-another-session" else REF_B
    answer = finish(c, monkeypatch, install, begin_relogin(c, install), ref=ref)
    assert answer.status_code >= 400
    assert not any(h.startswith(remote_access.SESSION_COOKIE_NAME + "=") and "Max-Age=0" not in h
                   for h in answer.headers.getlist("Set-Cookie"))
    assert held(c) == old and row(old_sid) == before and refs_of(old_sid) == old_refs and quiet == []
    assert tombstone(old_sid) is None and REF_B not in json.dumps([refs_of(taken)])
    assert row(sibling_sid) == sibling_before and refs_of(sibling_sid) == sibling_refs
    assert tombstone(sibling_sid) is None and authenticated(sibling)
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(func.count()).select_from(sessions)) == (1 if before is None else 2)


# ── 6. Races, in real lock order ────────────────────────────────────────────────────────────────
# The callback verifies the old cookie, then waits on the write lock while another transaction holds
# it; what that transaction commits decides.

RECORD = (device_registry, "record_session")
SIGN_OUT = (device_registry, "sign_out")


def fixed_clock(monkeypatch, at):
    clock = [at]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    return clock


def test_the_cookie_deadline_passing_while_the_login_waits_means_no_hand_over(install, monkeypatch, quiet):
    old = first_login(install)
    c = browser(old)
    old_sid = sid_of(install, old)
    # Another, renewed copy raised the row's cookie_exp: only this cookie's own deadline counts.
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(update(sessions).values(cookie_exp=2**40))
    deadline = remote_access.same_browser_session(install, old, "owner")["valid_until"]
    clock = fixed_clock(monkeypatch, deadline - 30)
    asked = begin_relogin(c, install)

    def cross():
        clock[0] = deadline

    _, answer = in_lock_order(monkeypatch, lambda: Hold(cross).run(), lambda: finish(c, monkeypatch, install, asked),
                              first_in=(Hold, "run"), second_in=RECORD)
    assert answer.status_code == 302 and held(c) != old
    new_sid = sid_of(install, held(c))
    assert row(old_sid) is not None and tombstone(old_sid) is None and quiet == []
    assert row(new_sid)["device_id"] != row(old_sid)["device_id"]


def test_an_organization_cookie_counts_only_to_its_organization_limit(install):
    install.remote_access.vibe_cloud.instance_kind = "personal"
    old = first_login(install)
    install.remote_access.vibe_cloud.instance_kind = "organization"
    previous = remote_access.same_browser_session(install, old, "owner")
    assert previous["valid_until"] == identity_of(install, old)["iat"] + remote_access.ORGANIZATION_SESSION_TTL_SECONDS
    assert previous["valid_until"] < identity_of(install, old)["exp"]


def test_a_binding_retired_while_the_login_waits_refuses_it(install, monkeypatch, quiet):
    old = first_login(install)
    c = browser(old)
    asked = begin_relogin(c, install)
    binding = remote_access.device_binding(install)
    _, answer = in_lock_order(monkeypatch, lambda: device_registry.retire_binding(binding, now=int(time.time())),
                              lambda: finish(c, monkeypatch, install, asked),
                              first_in=(device_registry, "retire_binding"), second_in=RECORD)
    assert answer.status_code >= 400 and held(c) == old and quiet == []
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(func.count()).select_from(sessions)) == 0


@pytest.mark.parametrize("same_ref", [True, False])
def test_signing_the_device_out_while_the_login_waits(issuer, monkeypatch, quiet, same_ref):
    install = issuer
    c = browser(first_login(install, ref=REF_A))
    assert subscribe(c, "https://example.invalid/on").status_code == 200
    other = browser(first_login(install, ref=REF_B))
    old = held(c)
    old_sid = sid_of(install, old)
    (device,) = [d for d in mine(other) if not d["current"]]
    asked = begin_relogin(c, install)
    # This account's own other device signs it out, which queues avibe.bot's sign-out of REF_A.
    signed_out, answer = in_lock_order(
        monkeypatch, lambda: sign_out(other, device["id"] + "/sign-out"),
        lambda: finish(c, monkeypatch, install, asked, ref=REF_A if same_ref else "asr1_" + "c" * 43),
        first_in=SIGN_OUT, second_in=RECORD)
    assert signed_out.status_code == 200
    assert signed_out.get_json()["sign_out_results"][0]["account_results"][0]["status"] == "pending"
    assert row(old_sid) is None and tombstone(old_sid) is not None
    assert quiet == [old_sid]  # the sign-out's own event; the login replaced nothing
    if same_ref:
        assert answer.status_code == 403 and held(c) == old
        with get_cached_sqlite_engine().connect() as conn:
            assert conn.scalar(select(func.count()).select_from(sessions)) == 1  # only the other device
        return
    assert answer.status_code == 302 and held(c) != old
    (again,) = [d for d in mine(c) if d["current"]]
    assert again["id"] != device["id"] and not again["capabilities"]["push_enabled"]
    assert refs_of(sid_of(install, held(c))) == ["asr1_" + "c" * 43]


def test_signing_the_device_out_after_the_hand_over_takes_the_new_session(install, monkeypatch, quiet):
    c = browser(first_login(install, "member", "member"))
    owner = browser(first_login(install, "admin", "owner"))
    old = held(c)
    asked = begin_relogin(c, install)
    (device,) = [d for d in listing(owner)["others"] if d["account"]["email"].startswith("member")]
    answer, signed_out = in_lock_order(
        monkeypatch, lambda: finish(c, monkeypatch, install, asked, subject="member", role="member"),
        lambda: sign_out(owner, device["id"] + "/sign-out"), first_in=RECORD, second_in=SIGN_OUT)
    assert answer.status_code == 302 and signed_out.status_code == 200
    new = held(c)
    # Each publishes after its own commit, from its own thread: both, once each, in either order.
    assert sorted(quiet) == sorted([sid_of(install, old), sid_of(install, new)])
    assert not authenticated(new) and not authenticated(old)


@pytest.mark.parametrize("login_first", [False, True])
def test_sign_out_others_in_the_same_second_as_a_login(install, monkeypatch, quiet, login_first):
    keeper = browser(first_login(install))
    c = browser(first_login(install))
    old = held(c)
    fixed_clock(monkeypatch, int(time.time()))
    asked = begin_relogin(c, install)
    bulk = lambda: sign_out(keeper, "sign-out-others")  # noqa: E731
    login = lambda: finish(c, monkeypatch, install, asked)  # noqa: E731
    if login_first:
        answer, result = in_lock_order(monkeypatch, login, bulk, first_in=RECORD, second_in=SIGN_OUT)
    else:
        result, answer = in_lock_order(monkeypatch, bulk, login, first_in=SIGN_OUT, second_in=RECORD)
    assert answer.status_code == 302 and result.status_code == 200
    new = held(c)
    assert authenticated(held(keeper)) and not authenticated(old)
    # Committed before the bulk sign-out, the new session is one of the others; after it, in the same
    # second, it is a new login the cutoff does not reach.
    assert authenticated(new) is (not login_first)
    assert result.get_json()["revoked_sessions"] == 1


# ── 7. Two tabs ─────────────────────────────────────────────────────────────────────────────────

def test_two_callbacks_with_the_same_old_cookie_replace_it_once(install, monkeypatch, quiet):
    old = first_login(install)
    tab1, tab2 = browser(old), browser(old)
    old_sid = sid_of(install, old)
    old_device = row(old_sid)["device_id"]
    set_authorization(install, old, lapsed=True)
    sign_in(tab1, monkeypatch, install)
    sign_in(tab2, monkeypatch, install)
    a, b = held(tab1), held(tab2)
    assert authenticated(a) and authenticated(b) and quiet == [old_sid]
    assert row(sid_of(install, a))["device_id"] == old_device != row(sid_of(install, b))["device_id"]


def test_signing_in_again_with_the_newer_cookie_replaces_that_one(install, monkeypatch, quiet):
    c = browser(first_login(install))
    relogin(c, monkeypatch, install)
    a = held(c)
    relogin(c, monkeypatch, install)
    assert not authenticated(a) and authenticated(held(c)) and len(mine(c)) == 1
    assert quiet[-1] == sid_of(install, a)


# ── 8. The iPhone App ───────────────────────────────────────────────────────────────────────────

def test_an_app_that_described_itself_keeps_its_name_kind_and_installation(install, monkeypatch):
    c = browser(first_login(install, "member", "member"))
    assert report(c, "install-1").status_code == 200
    (before,) = mine(c)
    relogin(c, monkeypatch, install, subject="member", role="member")
    (after,) = mine(c)
    assert (after["id"], after["display_name"], after["kind"], after["app_version"]) == \
        (before["id"], "Work device", "iphone_app", "1.0 (42)")
    assert report(c, "install-1").status_code == 200
    assert report(c, "install-2").status_code == 409
