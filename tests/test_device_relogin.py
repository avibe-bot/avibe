"""The same browser signing in again keeps its one device row; its old session is signed out.

Every route test runs the real `/auth/login` -> `/auth/callback` handshake (state, PKCE, nonce)
in one cookie jar; only the exchange with avibe.bot is replaced.
"""

import urllib.parse

import pytest
from sqlalchemy import func, select, update

from storage import device_health_registry, device_registry
from storage.db import get_cached_sqlite_engine
from storage.models import (
    device_activity_events as events,
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


def sign_in(c, monkeypatch, cfg, subject="owner", role="owner", ref=None, *, start=True):
    """One real sign-in in this cookie jar. Returns the callback response."""
    started = c.get("/auth/login?next=/", base_url=ORIGIN, headers=CHROME)
    assert started.status_code == 302 and started.headers["Location"] != "/", "login must start OAuth"
    asked = urllib.parse.parse_qs(urllib.parse.urlsplit(started.headers["Location"]).query)
    claims = {"nonce": asked["nonce"][0], "email": subject + "@test.invalid", "sub": subject}
    session_claims = _session_claims(cfg, role=role)
    if ref:
        claims.update(avibe_account_session_ref=ref, vibe_instance_access_source="owner", vibe_instance_role="owner")
    monkeypatch.setattr(remote_access, "exchange_oauth_code",
                        lambda *_a, **_k: {"claims": claims, "session_claims": session_claims})
    return c.get("/auth/callback?code=fixture&state=" + urllib.parse.quote(asked["state"][0]),
                 base_url=ORIGIN, headers=CHROME)


def relogin(c, monkeypatch, cfg, **kw):
    old = held(c)
    set_authorization(cfg, old, lapsed=True)
    answer = sign_in(c, monkeypatch, cfg, **kw)
    # The old session's own authorization comes back: from here on only the sign-out keeps it out.
    set_authorization(cfg, old, lapsed=False)
    return answer


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


def test_push_follows_the_row_with_this_logins_account_and_a_stopped_one_stays_stopped(install, monkeypatch):
    from storage.web_push_service import count_enabled, disable_subscription

    c = browser(first_login(install, "member", "member"))
    assert subscribe(c, "https://example.invalid/on").status_code == 200
    (before,) = mine(c)
    assert before["capabilities"]["push_enabled"]
    old_sid = sid_of(install, held(c))
    relogin(c, monkeypatch, install, subject="member", role="viewer")
    (after,) = mine(c)
    assert after["id"] == before["id"] and after["capabilities"]["push_enabled"]
    new_sid = sid_of(install, held(c))
    with get_cached_sqlite_engine().begin() as conn:
        link = conn.execute(select(push_links)).mappings().one()
        assert (link["session_id"], link["login_role"]) == (new_sid, "viewer") and old_sid != new_sid
        assert count_enabled(conn, user_key="remote:member") == 1
        disable_subscription(conn, endpoint="https://example.invalid/on", user_key="remote:member")
    relogin(c, monkeypatch, install, subject="member", role="viewer")
    with get_cached_sqlite_engine().connect() as conn:
        assert count_enabled(conn, user_key="remote:member") == 0


def test_another_sessions_and_unproven_push_links_are_not_taken(install, monkeypatch):
    # Same account, two cookie jars, one logical device (a shared push id).
    a = browser(first_login(install, "member", "member"))
    b = browser(first_login(install, "member", "member"))
    assert subscribe(a, "https://example.invalid/a").status_code == 200
    assert subscribe(b, "https://example.invalid/b").status_code == 200
    b_sid = sid_of(install, held(b))
    with get_cached_sqlite_engine().begin() as conn:
        b_link = dict(conn.execute(select(push_links).where(push_links.c.session_id == b_sid)).mappings().one())
        b_refs = conn.execute(select(refs).where(refs.c.sid == b_sid)).all()
        # A's own link loses its proof: a different push id than its subscription.
        conn.execute(update(push_links).where(push_links.c.session_id == sid_of(install, held(a)))
                     .values(push_id="someone-else"))
    a_sid = sid_of(install, held(a))
    relogin(a, monkeypatch, install, subject="member", role="member")
    with get_cached_sqlite_engine().connect() as conn:
        assert dict(conn.execute(select(push_links).where(push_links.c.subscription_id == b_link["subscription_id"]))
                    .mappings().one()) == b_link
        assert conn.execute(select(push_links.c.session_id).where(push_links.c.push_id == "someone-else")).scalar() == a_sid
        assert conn.execute(select(refs).where(refs.c.sid == b_sid)).all() == b_refs
    assert row(b_sid) is not None and tombstone(b_sid) is None


# ── 3. Health codes, ticket counters, schedule principal ────────────────────────────────────────

def health_rows(binding, subject, device, sid):
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(health.insert().values(mobile_id="mdev_unused", binding=binding, subject=subject, device_id=device,
                                            issuer_sid=sid, code_hash=digest("unused-code"), code_expires_at=2**40,
                                            linked_at="2026-11-25T00:00:00+00:00"))
        conn.execute(health.insert().values(mobile_id="mdev_paired", binding=binding, subject=subject, device_id=device,
                                            issuer_sid=sid, linked_at="2026-11-25T00:00:00+00:00"))
        conn.execute(ext.insert().values(session_id=sid, ticket_window=7, ticket_attempts=2,
                                         schedule_principal='{"role":"owner"}'))


def test_unused_health_codes_stay_with_the_old_session_and_cannot_be_redeemed(install, monkeypatch):
    from core.mobile_health_contract import MobileError

    c = browser(first_login(install))
    old_sid = sid_of(install, held(c))
    (device,) = mine(c)
    binding = remote_access.device_binding(install)
    health_rows(binding, "owner", row(old_sid)["device_id"], old_sid)
    # The private store cannot be cleaned right now: the code is still refused.
    monkeypatch.setattr(device_health_registry, "MobileStore", None)
    relogin(c, monkeypatch, install, role="viewer")
    new_sid = sid_of(install, held(c))
    with get_cached_sqlite_engine().connect() as conn:
        links = {r["mobile_id"]: r for r in conn.execute(select(health)).mappings()}
        counters = conn.execute(select(ext).where(ext.c.session_id == new_sid)).mappings().one()
    assert links["mdev_unused"]["issuer_sid"] == old_sid and links["mdev_unused"]["cleanup_pending"] == 2
    assert links["mdev_paired"]["cleanup_pending"] is None and links["mdev_paired"]["code_hash"] is None
    assert (counters["ticket_window"], counters["ticket_attempts"], counters["schedule_principal"]) == (7, 2, None)
    with pytest.raises(MobileError) as refused:
        device_health_registry.pair(None, {"pairing_code": "unused-code"}, {})
    assert refused.value.status == 401
    assert authenticated(held(c)) and mine(c)[0]["id"] == device["id"]


@pytest.mark.parametrize("local", [False, True])
def test_a_later_unpair_of_the_device_takes_the_pending_code_and_never_lowers_it(install, monkeypatch, local):
    c = browser(first_login(install))
    owner = browser(first_login(install, "admin", "owner"))
    old_sid = sid_of(install, held(c))
    binding = remote_access.device_binding(install)
    health_rows(binding, "owner", row(old_sid)["device_id"], old_sid)
    monkeypatch.setattr(device_health_registry, "MobileStore", None)
    relogin(c, monkeypatch, install)
    (device,) = mine(c)
    if local:
        # A local administrative stop is codes-only: it does not unpair, and does not raise the intent.
        device_registry.sign_out(binding, "owner", owner=True, caller=None, device_id=device["id"], now=10**10,
                                 lifetime=86400)
        expected = 2
    else:
        assert sign_out(owner, device["id"] + "/sign-out").status_code == 200
        expected = 1
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(health.c.cleanup_pending).where(health.c.mobile_id == "mdev_unused")) == expected
    # Once full, a codes-only pass does not lower it.
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(update(health).values(cleanup_pending=1))
        device_health_registry.queue_disconnect(conn, binding, "owner", device["id"], sids=[old_sid],
                                                remove_pairing=False, codes_only=True)
        assert set(conn.scalars(select(health.c.cleanup_pending))) == {1}


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

@pytest.mark.parametrize("why", ["signed-out-ref", "sid-taken", "sid-tombstoned"])
def test_a_new_session_that_cannot_be_issued_replaces_nothing(install, monkeypatch, quiet, why):
    if why == "signed-out-ref":
        # Account sign-out needs the issuer pairing that names its epoch.
        install.remote_access.vibe_cloud.backend_url = "https://issuer.invalid"
        install.remote_access.vibe_cloud.instance_secret = "test-device-secret"
        install.save()
    old = first_login(install, ref=REF_A if why == "signed-out-ref" else None)
    c = browser(old)
    old_sid = sid_of(install, old)
    before = row(old_sid)
    with get_cached_sqlite_engine().connect() as conn:
        old_refs = conn.execute(select(refs).where(refs.c.sid == old_sid)).all()
    if why == "signed-out-ref":
        # avibe.bot's account session was signed out from here: the same reference is refused.
        from storage.models import remote_account_revocations as jobs
        from storage.device_account_signout import AccountSessionSignedOut  # noqa: F401
        import hashlib
        import json
        from vibe.account_session_revoke import pairing_epoch
        with get_cached_sqlite_engine().begin() as conn:
            digest = hashlib.sha256(json.dumps([pairing_epoch(install), "owner", REF_B]).encode()).hexdigest()
            conn.execute(jobs.insert().values(digest=digest, **_job_fields(conn, jobs)))
        set_authorization(install, old, lapsed=True)
        answer = sign_in(c, monkeypatch, install, ref=REF_B)
        assert answer.status_code == 403
    else:
        taken = old_sid if why == "sid-taken" else "x" * 32
        if why == "sid-tombstoned":
            with get_cached_sqlite_engine().begin() as conn:
                device_registry._tombstone(conn, taken, 2**40, 0)
        monkeypatch.setattr(remote_access.secrets, "token_urlsafe", lambda *_: taken)
        set_authorization(install, old, lapsed=True)
        answer = sign_in(c, monkeypatch, install)
        assert answer.status_code >= 400
    assert not any(h.startswith(remote_access.SESSION_COOKIE_NAME + "=") and "Max-Age=0" not in h
                   for h in answer.headers.getlist("Set-Cookie"))
    assert held(c) == old and row(old_sid) == before and tombstone(old_sid) is None and quiet == []
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(refs).where(refs.c.sid == old_sid)).all() == old_refs


def _job_fields(conn, jobs):
    """The non-null columns of an account sign-out job, filled with fixture values."""
    values = {}
    for column in jobs.columns:
        if column.name == "digest" or column.nullable or column.server_default is not None or column.default is not None:
            continue
        values[column.name] = 0 if str(column.type).upper().startswith("INT") else "fixture"
    return values


# ── 6. Races: decided under the write lock ──────────────────────────────────────────────────────

def _record(install, previous, *, sid="n" * 32, clock=None, monkeypatch=None, ref=None):
    import time
    if clock is not None:
        monkeypatch.setattr(device_registry.time, "time", lambda: clock)
    now = int(time.time()) if clock is None else clock - 60
    identity = {"browser_session_id": sid, "sub": "owner", "email": "owner@test.invalid", "auth_time": now,
                "exp": now + 86400}
    return device_registry.record_session(remote_access.device_binding(install), identity, "owner", {"kind": "browser"},
                                          expires_at=now + 90 * 86400, now=now, fresh=True, previous=previous)


def test_a_cookie_that_expires_while_waiting_for_the_lock_is_not_replaced(install, monkeypatch):
    old = first_login(install)
    previous = remote_access.same_browser_session(install, old, "owner")
    # Its row's cookie_exp was raised by another, renewed copy: the cookie's own deadline counts.
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(update(sessions).values(cookie_exp=2**40))
    assert _record(install, previous, clock=previous["valid_until"], monkeypatch=monkeypatch) is None
    assert row(previous["sid"]) is not None and row("n" * 32)["device_id"] != row(previous["sid"])["device_id"]


def test_an_organization_cookie_counts_only_to_its_organization_limit(install, monkeypatch):
    install.remote_access.vibe_cloud.instance_kind = "personal"
    old = first_login(install)
    install.remote_access.vibe_cloud.instance_kind = "organization"
    previous = remote_access.same_browser_session(install, old, "owner")
    assert previous["valid_until"] == identity_of(install, old)["iat"] + remote_access.ORGANIZATION_SESSION_TTL_SECONDS
    assert previous["valid_until"] < identity_of(install, old)["exp"]


def test_a_binding_retired_while_waiting_refuses_the_login(install):
    old = first_login(install)
    previous = remote_access.same_browser_session(install, old, "owner")
    device_registry.retire_binding(remote_access.device_binding(install), now=1)
    with pytest.raises(PermissionError):
        _record(install, previous)
    assert row("n" * 32) is None


def test_signing_the_device_out_first_leaves_the_login_on_its_own(install, monkeypatch, quiet):
    c = browser(first_login(install, "member", "member"))
    owner = browser(first_login(install, "admin", "owner"))
    assert subscribe(c, "https://example.invalid/on").status_code == 200
    (device,) = [d for d in listing(owner)["others"] if d["account"]["email"].startswith("member")] or [None]
    old = held(c)
    assert sign_out(owner, device["id"] + "/sign-out").status_code == 200
    # The sign-in that was already under way: its old cookie no longer counts.
    previous = remote_access.same_browser_session(install, old, "member")
    assert previous is None
    quiet.clear()
    sign_in(c, monkeypatch, install, subject="member", role="member")
    (again,) = mine(c)
    assert again["id"] != device["id"] and not again["capabilities"]["push_enabled"]
    assert quiet == []


def test_signing_the_device_out_after_the_hand_over_takes_the_new_session(install, monkeypatch):
    c = browser(first_login(install, "member", "member"))
    owner = browser(first_login(install, "admin", "owner"))
    relogin(c, monkeypatch, install, subject="member", role="member")
    new = held(c)
    (device,) = [d for d in listing(owner)["others"] if d["account"]["email"].startswith("member")]
    assert sign_out(owner, device["id"] + "/sign-out").status_code == 200
    assert not authenticated(new)


def test_sign_out_others_keeps_the_caller_and_a_login_after_it(install, monkeypatch):
    keeper = browser(first_login(install))
    other = browser(first_login(install))
    assert sign_out(keeper, "sign-out-others").get_json()["revoked_sessions"] == 1
    assert authenticated(held(keeper)) and not authenticated(held(other))
    # A sign-in after it, in the same second, from the signed-out browser: a row of its own.
    sign_in(other, monkeypatch, install)
    assert authenticated(held(other)) and len(mine(keeper)) == 2


# ── 7. Two tabs ─────────────────────────────────────────────────────────────────────────────────

def test_two_callbacks_with_the_same_old_cookie_replace_it_once(install, monkeypatch, quiet):
    old = first_login(install)
    tab1, tab2 = browser(old), browser(old)
    old_sid = sid_of(install, old)
    set_authorization(install, old, lapsed=True)
    sign_in(tab1, monkeypatch, install)
    sign_in(tab2, monkeypatch, install)
    a, b = held(tab1), held(tab2)
    assert authenticated(a) and authenticated(b) and quiet == [old_sid]
    assert row(sid_of(install, a))["device_id"] == row(sid_of(install, a))["device_id"] != row(sid_of(install, b))["device_id"]


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
