"""Single-use Health proof and self-report are committed in one real registry transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time

import pytest
from sqlalchemy import select

from core.mobile_health_contract import MobileError
from storage import device_health_tickets as tickets
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import MobileStore
from storage.models import device_health_links as links
from tests.test_device_health_cleanup import code_for, phone
from tests.test_instance_device_registry import client, install, login, report  # noqa: F401


def legacy(config):
    store = MobileStore()
    _, viewer, _ = phone(config, "iphone-a")
    identity, payload = code_for(store, None)
    mid = store.pair(payload, identity)["device_id"]
    token = tickets.issue(store, {"id": mid}, viewer[0])["health_binding_ticket"]
    return store, mid, viewer, token


def test_native_bearer_to_browser_self_report(install, monkeypatch):
    from config.v2_config import V2Config
    from storage.models import remote_device_sessions as sessions
    from vibe import remote_access

    install.remote_access.vibe_cloud.instance_secret = "fixture-secret"
    monkeypatch.setattr(V2Config, "load", classmethod(lambda cls, *a, **kw: install))
    store = MobileStore()
    _, payload = code_for(store, None)
    cookie, viewer, _ = phone(install, "reporter")
    c = client(cookie)
    path = "/api/mobile/device/v1/"
    pair = c.post(path + "pair", json=payload)
    assert pair.status_code == 200
    mid = pair.get_json()["device_id"]
    bearer = {"Authorization": "Bearer " + pair.get_json()["credential"]}
    assert c.post(path + "binding-ticket", json={}).status_code == 401
    issued = c.post(path + "binding-ticket", json={}, headers=bearer)
    assert issued.status_code == 200
    token = issued.get_json()["health_binding_ticket"]
    engine = get_cached_sqlite_engine()
    # The signed cookie itself is valid, but its registration no longer is.
    with engine.begin() as conn:
        conn.execute(sessions.update().where(sessions.c.session_id == viewer[3]).values(cookie_exp=0))
    assert report(c, "reporter", health_binding_ticket=token).status_code == 404
    with engine.connect() as conn:
        assert conn.scalar(select(links.c.ticket_hash).where(links.c.mobile_id == mid)) == tickets.digest(token)
    fresh = client(login(install, "owner", "owner"))
    assert report(fresh, "reporter", health_binding_ticket=token).status_code == 200
    with engine.connect() as conn:
        assert conn.execute(select(links.c.ticket_hash, links.c.binding).where(links.c.mobile_id == mid)).one() == (
            None,
            remote_access.device_binding(install),
        )
    assert report(fresh, "reporter", health_binding_ticket=token).status_code == 403


def test_concurrent_reports_consume_only_one_ticket(install):
    _, mid, viewer, token = legacy(install)
    # Independent signed sessions, requests and SQLite connections on one installation.
    cookies = [login(install, "owner", "owner") for _ in range(2)]
    sync = Barrier(2)

    def submit(cookie):
        c = client(cookie)
        sync.wait(timeout=5)
        return report(c, "target", health_binding_ticket=token).status_code

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(submit, cookies)) == [200, 403]
    with get_cached_sqlite_engine().connect() as conn:
        row = conn.execute(select(links).where(links.c.mobile_id == mid)).mappings().one()
        assert row["ticket_hash"] is None and (row["binding"], row["subject"]) == viewer[:2]


@pytest.mark.parametrize("mismatch", ["member", "account", "binding", "instance", "expired", "other-device"])
def test_wrong_principal_scope_or_expiry_never_consumes(install, mismatch):
    _, mid, _, token = legacy(install)
    subject = "other" if mismatch == "account" else "owner"
    cookie = login(install, subject, "member" if mismatch == "member" else "owner")
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        if mismatch == "binding":
            conn.execute(links.update().where(links.c.mobile_id == mid).values(ticket_binding="different-binding"))
        elif mismatch == "expired":
            conn.execute(links.update().where(links.c.mobile_id == mid).values(expires_at=int(time.time()) - 1))
        elif mismatch == "other-device":
            conn.execute(
                links.update()
                .where(links.c.mobile_id == mid)
                .values(binding="prior", subject="owner", device_id="prior-phone")
            )
        elif mismatch == "instance":
            with MobileStore().transaction() as m:
                m.execute("UPDATE devices SET instance_id=? WHERE id=?", ("other-instance", mid))
    assert report(client(cookie), "target", health_binding_ticket=token).status_code == 403
    with engine.connect() as conn:
        assert conn.scalar(select(links.c.ticket_hash).where(links.c.mobile_id == mid)) == tickets.digest(token)


def test_issue_and_report_limits_supersession_and_description_errors(install):
    store, mid, viewer, first = legacy(install)
    device = {"id": mid}
    with pytest.raises(MobileError, match="binding_ticket_rate_limited"):
        tickets.issue(store, device, viewer[0])
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(links.update().where(links.c.mobile_id == mid).values(issued_at=int(time.time()) - 30))
    token = tickets.issue(store, device, viewer[0])["health_binding_ticket"]
    c = client(login(install, "owner", "owner", installation="current"))
    assert report(c, "current", health_binding_ticket=first).status_code == 403
    assert report(c, "changed", health_binding_ticket=token).status_code == 409
    assert report(c, "current", display_name="Different", health_binding_ticket=token).status_code == 429
    for _ in range(2):
        assert report(c, "current", health_binding_ticket=first).status_code == 403
    assert report(c, "current", health_binding_ticket=token).get_json()["error"] == "health_binding_rate_limited"
    # A different live session can still use the same unconsumed proof.
    assert report(client(login(install, "owner", "owner")), "current", health_binding_ticket=token).status_code == 200
