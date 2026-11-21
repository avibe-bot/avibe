"""Real schedule store: live device preferences do not veto another phone or an old lease."""

from types import SimpleNamespace

from storage import device_functions as gates, system_schedule_service as schedule
from storage.db import get_cached_sqlite_engine
from tests.test_instance_device_registry import login
from tests.test_remote_access_vibe_cloud import _config
from tests.test_system_schedule_service import NOW, _create, _get, _receipt, _record, isolated_state  # noqa: F401
from vibe import remote_access
from vibe.authorization import AuthorizationContext


def test_two_phones_recheck_current_gate_and_preserve_old_work(isolated_state, monkeypatch):
    from core import web_push_notifications as authority
    from storage.models import remote_device_sessions as sessions
    from sqlalchemy import select

    cfg = _config()
    cfg.remote_access.vibe_cloud.instance_id = "instance-a"
    monkeypatch.setattr(authority, "_load_notification_config", lambda: (cfg, False))
    ctx = AuthorizationContext(
        instance_role="owner",
        subject="owner",
        is_remote=True,
        instance_id="instance-a",
        instance_access_source="owner",
        claims_issued_at=int(NOW.timestamp()),
    )
    monkeypatch.setattr(
        authority, "_evaluate_record_authorization", lambda *_a, **_kw: SimpleNamespace(authorized=True, context=ctx)
    )
    engine = get_cached_sqlite_engine()

    def handset(name):
        cookie = login(cfg, "owner", "owner")
        sid = remote_access.parse_session_identity(cfg, cookie)["browser_session_id"]
        viewer = (remote_access.device_binding(cfg), "owner", True, sid)
        with engine.connect() as conn:
            key = conn.scalar(select(sessions.c.device_id).where(sessions.c.session_id == sid))

        def poll(**kw):
            return schedule.claim_next(
                instance_id="instance-a", device_id=name, now=NOW, claimant=ctx, device_viewer=viewer, **kw
            )

        def toggle(on):
            with engine.begin() as conn:
                gates.preference(conn, *viewer[:2], key, schedule_on=int(on))

        poll()  # A signed, capable claimant with no outstanding work.
        return poll, toggle

    a, toggle_a = handset("device-a")
    _create(operation_id="old")
    old = a()
    toggle_a(False)
    assert a() == old
    _record(old, _receipt(old, state="scheduled", system_identifier="fixture-old"))
    schedule.cancel_operation(session_id="ses-schedule", instance_id="instance-a", operation_id="old", now=NOW)
    cancellation = a(allow_create=False)
    assert cancellation["request"]["operationId"] == "old"
    _record(cancellation, _receipt(cancellation, state="cancelled", system_identifier="fixture-old"))
    _create()
    assert a() is None and _get()["status_message"]
    toggle_a(True)
    assert _get()["status_message"] is None  # No fresh poll is needed to clear the reason.
    toggle_a(False)
    # Register B without leasing the existing proposal; its eligibility alone suffices.
    cookie = login(cfg, "owner", "owner")
    sid = remote_access.parse_session_identity(cfg, cookie)["browser_session_id"]
    viewer = (remote_access.device_binding(cfg), "owner", True, sid)

    def poll_b(**kw):
        return schedule.claim_next(
            instance_id="instance-a", device_id="device-b", now=NOW, claimant=ctx, device_viewer=viewer, **kw
        )

    assert poll_b(allow_create=False) is None
    assert a() is None and _get()["status_message"] is None
    assert poll_b()["request"]["operationId"] == "wake-up"
    assert (
        schedule.claim_next(
            instance_id="instance-a",
            device_id="gone",
            now=NOW,
            claimant=ctx,
            device_viewer=(*viewer[:3], "missing-sid"),
        )
        is None
    )
