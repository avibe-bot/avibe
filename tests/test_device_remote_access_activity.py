"""Connector state transitions are authoritative; activity recording isn't."""

from contextlib import nullcontext

from sqlalchemy import select

from storage import device_activity
from storage.db import get_cached_sqlite_engine
from storage.models import device_activity_events as events
from tests.test_device_health_cleanup import phone
from tests.test_instance_device_registry import install  # noqa: F401
from vibe import remote_access as remote, ui_server
from vibe.ui_compat import g


def test_only_successful_requested_state_changes_are_attributed(install, tmp_path, monkeypatch, caplog):
    _, viewer, key = phone(install, "switcher")
    install.remote_access.vibe_cloud.instance_secret = "fixture"
    path = tmp_path / "request"
    monkeypatch.setattr(remote, "_connector_requested_path", lambda: path)
    monkeypatch.setattr(remote, "_connector_lifecycle", nullcontext)
    monkeypatch.setattr(remote, "_ensure_cloudflared_pin", lambda *_: None)

    def start(*args, **kw):
        path.touch()
        return {"ok": True}

    monkeypatch.setattr(remote, "_start", start)
    monkeypatch.setattr(remote, "_stop_connector", lambda *_: {"ok": True})
    with ui_server.app.test_request_context("/api/remote-access"):
        g.remote_session_identity = {"sub": viewer[1], "browser_session_id": viewer[3]}
        for switch in [remote.start, remote.start, remote.stop, remote.stop]:
            assert switch(install)["ok"]

        def fail(*args, **kw):
            raise RuntimeError("private-payload-canary")

        monkeypatch.setattr(device_activity, "record", fail)
        assert remote.start(install)["ok"] and path.exists()
        assert remote.stop(install)["ok"] and not path.exists()
    assert "private-payload-canary" not in caplog.text
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(
            select(events.c.kind, events.c.device_id, events.c.subject)
            .where(events.c.kind.in_(["remoteAccessOn", "remoteAccessOff"]))
            .order_by(events.c.id)
        ).all() == [("remoteAccessOn", key, viewer[1]), ("remoteAccessOff", key, viewer[1])]
