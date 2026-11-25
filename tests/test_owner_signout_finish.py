"""An owner's "sign out this device" of a session with no account reference (one signed in before
this instance recorded them, such as everything from 3.2.7) is finished at the browser's next
owner sign-in: refused once, avibe.bot's sign-out of that exact account session queued, the old
cookie cleared. Real `/auth/login` -> `/auth/callback`; only the code exchange is replaced.
"""

import threading
import urllib.parse
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from storage import device_registry
from storage.db import get_cached_sqlite_engine
from storage.device_account_signout import OWNER_SIGNED_OUT_PREFIX
from storage.models import (
    remote_account_revocations as jobs,
    remote_device_sessions as sessions,
    state_meta,
)
from tests.test_device_relogin import (  # noqa: F401
    CHROME, ORIGIN, browser, finish, first_login, held, install, issuer, listing, quiet, sid_of, sign_out, start, tombstone,
)
from vibe import account_session_revoke, remote_access

REF = "asr1_" + "o" * 43
NEW_REF = "asr1_" + "n" * 43


def mark(sid):
    with get_cached_sqlite_engine().connect() as conn:
        return conn.scalar(select(state_meta.c.value_json).where(state_meta.c.key == OWNER_SIGNED_OUT_PREFIX + sid))


def job_rows():
    with get_cached_sqlite_engine().connect() as conn:
        return [dict(r) for r in conn.execute(select(jobs.c.digest, jobs.c.status, jobs.c.reference)).mappings()]


def digest(cfg, ref, subject="owner"):
    import hashlib
    import json
    return hashlib.sha256(json.dumps([account_session_revoke.pairing_epoch(cfg), subject, ref]).encode()).hexdigest()


def session_count():
    with get_cached_sqlite_engine().connect() as conn:
        return len(conn.execute(select(sessions.c.session_id)).all())


def cleared(answer):
    return any(h.startswith(remote_access.SESSION_COOKIE_NAME + "=") and "Max-Age=0" in h
               for h in answer.headers.getlist("Set-Cookie"))


def issued(answer):
    return any(h.startswith(remote_access.SESSION_COOKIE_NAME + "=") and "Max-Age=0" not in h
               for h in answer.headers.getlist("Set-Cookie"))


def signed_out_by_owner(cfg, subject="owner", role="owner"):
    """An old session (no account reference) that the owner signs out from another device."""
    old = first_login(cfg, subject, role)
    owner = browser(first_login(cfg, "owner", "owner", ref="asr1_" + "k" * 43))
    sid = sid_of(cfg, old)
    group = "mine" if subject == "owner" else "others"
    (device,) = [d for d in listing(owner)[group] if d["kind"] != "host" and not d["current"]
                 and d["account"]["subject"] == subject]
    response = sign_out(owner, device["id"] + "/sign-out")
    assert response.status_code == 200
    if subject == "owner":
        assert response.get_json()["sign_out_results"][0]["account_results"][0]["reason"] == "no_reference"
    return old, sid


# ── The case it is for ──────────────────────────────────────────────────────────────────────────

def test_the_owners_signed_out_browser_coming_back_is_refused_once_and_its_sign_in_ended(issuer, monkeypatch, quiet):
    from tests.test_device_account_signout import fake_issuer
    from vibe import account_signout_worker
    cfg = issuer
    old, sid = signed_out_by_owner(cfg)
    stone = tombstone(sid)
    assert mark(sid) is not None
    c = browser(old)
    before = session_count()
    # The page sends it to sign in; avibe.bot signs it in silently with the same account session.
    answer = finish(c, monkeypatch, cfg, start(c), ref=REF)
    assert answer.status_code == 403 and "account_session_signed_out" in answer.text
    assert cleared(answer) and not issued(answer) and held(c) is None
    assert session_count() == before
    assert mark(sid) is None and tombstone(sid) == stone
    assert [(j["digest"], j["status"], j["reference"]) for j in job_rows()] == [(digest(cfg, REF), "pending", REF)]
    calls = fake_issuer(monkeypatch, 204)
    assert account_signout_worker.run_once(lambda: cfg)
    assert [call[2] for call in calls] == [{"account_session_ref": REF}]


def test_after_that_the_same_cookie_is_an_ordinary_sign_in(issuer, monkeypatch, quiet):
    cfg = issuer
    old, sid = signed_out_by_owner(cfg)
    c = browser(old)
    assert finish(c, monkeypatch, cfg, start(c), ref=REF).status_code == 403
    # The 403 was lost: the browser still holds the old cookie and comes straight back.
    again = browser(old)
    same = finish(again, monkeypatch, cfg, start(again), ref=REF)
    assert same.status_code == 403 and not cleared(same) and not issued(same)  # the existing fence on that ref
    assert len(job_rows()) == 1
    # Signed in again for real at avibe.bot: a new account session, not ended a second time.
    fresh = finish(again, monkeypatch, cfg, start(again), ref=NEW_REF)
    assert fresh.status_code == 302 and issued(fresh)
    assert digest(cfg, NEW_REF) not in [j["digest"] for j in job_rows()]


# ── What it does not touch ──────────────────────────────────────────────────────────────────────

def test_a_member_coming_back_with_a_cookie_the_owner_signed_out_signs_in_as_today(issuer, monkeypatch, quiet):
    cfg = issuer
    old, sid = signed_out_by_owner(cfg, "member", "member")
    c = browser(old)
    answer = finish(c, monkeypatch, cfg, start(c), subject="member", role="member")
    assert answer.status_code == 302 and issued(answer)
    assert job_rows() == [] or all(j["reference"] != REF for j in job_rows())
    assert mark(sid) is not None  # only an owner sign-in finishes it


@pytest.mark.parametrize("how", ["own-logout", "member-own-device", "address-change", "had-a-reference"])
def test_sign_outs_that_are_not_the_owners_leave_nothing_to_finish(issuer, monkeypatch, quiet, how):
    cfg = issuer
    binding = remote_access.device_binding(cfg)
    if how == "own-logout":
        old = first_login(cfg)
        remote_access.revoke_browser_session(remote_access.parse_session_identity(cfg, old))
    elif how == "member-own-device":
        old = first_login(cfg, "member", "member")
        member = browser(first_login(cfg, "member", "member"))
        (device,) = [d for d in listing(member)["mine"] if not d["current"]]
        assert sign_out(member, device["id"] + "/sign-out").status_code == 200
    elif how == "had-a-reference":
        # Its avibe.bot sign-in was ended at sign-out: nothing is left to finish.
        old = first_login(cfg, ref=REF)
        owner = browser(first_login(cfg, "owner", "owner", ref="asr1_" + "k" * 43))
        (device,) = [d for d in listing(owner)["mine"] if d["kind"] != "host" and not d["current"]]
        assert sign_out(owner, device["id"] + "/sign-out").status_code == 200
    else:
        old = first_login(cfg)
        device_registry.sign_out(binding, None, owner=True, caller=None, device_id=None, now=int(time.time()),
                                 lifetime=86400)  # as the address change does: not "sign out this device"
    sid = sid_of(cfg, old)
    assert tombstone(sid) is not None and mark(sid) is None
    if how in ("member-own-device", "had-a-reference"):
        return
    c = browser(old)
    answer = finish(c, monkeypatch, cfg, start(c), ref=REF)
    assert answer.status_code == 302 and issued(answer) and job_rows() == []


# ── The existing refusals come first ────────────────────────────────────────────────────────────

def test_a_retired_binding_refuses_as_before_and_keeps_the_mark(issuer, monkeypatch, quiet):
    cfg = issuer
    old, sid = signed_out_by_owner(cfg)
    c = browser(old)
    asked = start(c)
    device_registry.retire_binding(remote_access.device_binding(cfg), now=int(time.time()))
    answer = finish(c, monkeypatch, cfg, asked, ref=REF)
    assert answer.status_code >= 400 and not cleared(answer) and not issued(answer)
    assert mark(sid) is not None and job_rows() == []


def test_a_handshake_past_its_deadline_refuses_as_before_and_keeps_the_mark(issuer, monkeypatch, quiet):
    cfg = issuer
    old, sid = signed_out_by_owner(cfg)
    c = browser(old)
    asked = start(c)
    real = time.time
    late = real() + remote_access.OAUTH_HANDSHAKE_TTL_SECONDS + 5
    # Only the decision under the lock is late: the handshake had expired while it waited.
    monkeypatch.setattr(device_registry, "time", SimpleNamespace(time=lambda: late))
    answer = finish(c, monkeypatch, cfg, asked, ref=REF)
    assert answer.status_code == 403 and not cleared(answer) and not issued(answer)
    assert mark(sid) is not None and job_rows() == []


# ── One mark, two callbacks at once ─────────────────────────────────────────────────────────────

def test_two_callbacks_with_the_same_cookie_use_the_mark_once(issuer, monkeypatch, quiet):
    cfg = issuer
    old, sid = signed_out_by_owner(cfg)
    tabs = [browser(old), browser(old)]
    asked = [start(tab) for tab in tabs]
    # Both callbacks have verified everything before the registry and race for its write lock.
    entered, go, real = threading.Semaphore(0), threading.Event(), device_registry.record_session

    def gated(*args, **kwargs):
        entered.release()
        assert go.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(device_registry, "record_session", gated)
    answers = [None, None]
    # One exchange for both: each code answers with its own handshake's nonce.
    from tests.test_remote_access_vibe_cloud import _session_claims
    nonces = {str(i): asked[i]["nonce"][0] for i in range(2)}
    monkeypatch.setattr(remote_access, "exchange_oauth_code", lambda _cfg, code, *_a, **_k: {
        "claims": {"nonce": nonces[code], "email": "owner@test.invalid", "sub": "owner", "avibe_account_session_ref": REF,
                   "vibe_instance_access_source": "owner", "vibe_instance_role": "owner"},
        "session_claims": _session_claims(cfg, role="owner")})

    def run(i):
        answers[i] = tabs[i].get(f"/auth/callback?code={i}&state=" + urllib.parse.quote(asked[i]["state"][0]),
                                 base_url=ORIGIN, headers=CHROME)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    assert entered.acquire(timeout=10) and entered.acquire(timeout=10)
    go.set()
    for thread in threads:
        thread.join(15)
    assert sorted(a.status_code for a in answers) == [403, 403]
    assert sum(cleared(a) for a in answers) == 1 and not any(issued(a) for a in answers)
    assert mark(sid) is None and len(job_rows()) == 1


def test_the_mark_goes_with_its_tombstone(issuer):
    old, sid = signed_out_by_owner(issuer)
    assert mark(sid) is not None
    late = int(tombstone(sid)) + 1
    with get_cached_sqlite_engine().begin() as conn:
        device_registry.prune(conn, late)
    assert tombstone(sid) is None and mark(sid) is None
