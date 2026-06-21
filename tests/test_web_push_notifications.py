from __future__ import annotations

from core import web_push_notifications
from storage import messages_service, web_push_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.settings_service import upsert_scope


def test_maybe_notify_inbox_message_schedules_agent_result(monkeypatch):
    calls = []

    class _Thread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.target = target
            self.args = args

        def start(self):
            calls.append(self.args[0])

    monkeypatch.setattr(web_push_notifications.threading, "Thread", _Thread)

    web_push_notifications.maybe_notify_inbox_message(
        {
            "id": "msg_1",
            "platform": "avibe",
            "author": "agent",
            "type": "result",
            "session_id": "ses_1",
            "text": "Done",
        },
        {
            "title": "Build fix",
            "project_name": "Vibe Remote",
            "preview_text": "Done",
            "unread_count": 2,
        },
    )

    # badge_count is intentionally NOT set at schedule time: the app-icon badge
    # is one global number, computed fresh at send time (post-debounce), not this
    # one session's unread count.
    assert calls == [
        {
            "title": "Build fix",
            "body": "Done",
            "url": "/chat/ses_1",
            "tag": "session:ses_1",
            "message_id": "msg_1",
            "session_id": "ses_1",
        }
    ]


def test_maybe_notify_inbox_message_skips_non_notifiable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_push_notifications.threading,
        "Thread",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    web_push_notifications.maybe_notify_inbox_message(
        {
            "id": "msg_1",
            "platform": "avibe",
            "author": "agent",
            "type": "assistant",
            "session_id": "ses_1",
            "text": "thinking",
        },
        {"title": "Build fix"},
    )

    assert calls == []

    web_push_notifications.maybe_notify_inbox_message(
        {
            "id": "msg_2",
            "platform": "avibe",
            "author": "agent",
            "type": "notify",
            "session_id": "ses_1",
            "text": "process log",
        },
        {"title": "Build fix"},
    )

    assert calls == []


def test_send_to_enabled_subscriptions_waits_then_sends_to_owner_devices(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_x", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_push",
                native_session_id="",
                title="Push",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={"_web_push_user_key": "remote:user-a"},
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/a",
                "keys": {"p256dh": "a-key", "auth": "a-auth"},
            },
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-b",
            payload={
                "endpoint": "https://push.example.test/b",
                "keys": {"p256dh": "b-key", "auth": "b-auth"},
            },
        )

    sleeps = []
    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Push", "body": "Done", "session_id": "ses_push", "message_id": message["id"]}
    )

    assert sleeps == [3.0]
    assert [send[0]["endpoint"] for send in sends] == [
        "https://push.example.test/a",
    ]


def test_send_to_enabled_subscriptions_sets_global_badge_count(monkeypatch, tmp_path):
    """badge_count in the sent payload is the GLOBAL unread total, not the
    triggering session's per-session count — the app-icon badge is one number."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_badge", now=now)
        for sid in ("ses_badge_a", "ses_badge_b"):
            conn.execute(
                agent_sessions.insert().values(
                    id=sid,
                    scope_id=scope_id,
                    agent_backend="claude",
                    agent_variant="default",
                    session_anchor=sid,
                    native_session_id="",
                    title=sid,
                    status="active",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_badge_a",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={"_web_push_user_key": "remote:user-a"},
            message_type="user",
            text="Please finish",
        )
        # Triggering reply: session A holds ONE unread result...
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_badge_a",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done A",
        )
        # ...and an unrelated session B holds another, so the global total is 2
        # while session A's per-session count is only 1.
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_badge_b",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done B",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/a",
                "keys": {"p256dh": "a-key", "auth": "a-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Badge", "body": "Done A", "session_id": "ses_badge_a", "message_id": message["id"]}
    )

    assert [send[1]["badge_count"] for send in sends] == [2]


def test_send_to_enabled_subscriptions_uses_legacy_session_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_legacy", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_legacy_owner",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_legacy_owner",
                native_session_id="",
                title="Legacy Owner",
                status="active",
                metadata_json='{"_web_push_user_key":"remote:user-a"}',
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_legacy_owner",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for key in ("remote:user-a", "remote:user-b"):
            web_push_service.upsert_subscription(
                conn,
                user_key=key,
                payload={
                    "endpoint": f"https://push.example.test/{key}",
                    "keys": {"p256dh": f"{key}-p256dh", "auth": f"{key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(web_push_notifications, "_remote_access_enabled", lambda: True)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Legacy Owner",
            "body": "Done",
            "session_id": "ses_legacy_owner",
            "message_id": message["id"],
        }
    )

    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/remote:user-a"]


def test_send_to_enabled_subscriptions_prefers_message_owner_over_legacy_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_new_owner", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_new_owner",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_new_owner",
                native_session_id="",
                title="New Owner",
                status="active",
                metadata_json='{"_web_push_user_key":"remote:user-a"}',
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_new_owner",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-b",
            metadata={"_web_push_user_key": "remote:user-b"},
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_new_owner",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for key in ("remote:user-a", "remote:user-b"):
            web_push_service.upsert_subscription(
                conn,
                user_key=key,
                payload={
                    "endpoint": f"https://push.example.test/{key}",
                    "keys": {"p256dh": f"{key}-p256dh", "auth": f"{key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "New Owner", "body": "Done", "session_id": "ses_new_owner", "message_id": message["id"]}
    )

    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/remote:user-b"]


def test_send_to_enabled_subscriptions_sends_to_merged_prompt_owners(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_multi_owner", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_multi_owner",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_multi_owner",
                native_session_id="",
                title="Multi Owner",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_multi_owner",
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text="u1\nu2",
            metadata={"_web_push_user_keys": ["remote:user-a", "remote:user-b"]},
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_multi_owner",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for key in ("remote:user-a", "remote:user-b", "remote:user-c"):
            web_push_service.upsert_subscription(
                conn,
                user_key=key,
                payload={
                    "endpoint": f"https://push.example.test/{key}",
                    "keys": {"p256dh": f"{key}-p256dh", "auth": f"{key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Multi Owner", "body": "Done", "session_id": "ses_multi_owner", "message_id": message["id"]}
    )

    assert [send[0]["endpoint"] for send in sends] == [
        "https://push.example.test/remote:user-a",
        "https://push.example.test/remote:user-b",
    ]


def test_send_to_enabled_subscriptions_ignores_untrusted_author_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_spoof", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_spoof",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_spoof",
                native_session_id="",
                title="Spoof",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_spoof",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-b",
            message_type="user",
            text="Spoof owner",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_spoof",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for key in ("remote:user-a", "remote:user-b"):
            web_push_service.upsert_subscription(
                conn,
                user_key=key,
                payload={
                    "endpoint": f"https://push.example.test/{key}",
                    "keys": {"p256dh": f"{key}-p256dh", "auth": f"{key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Spoof", "body": "Done", "session_id": "ses_spoof", "message_id": message["id"]}
    )

    assert sends == []


def test_send_to_enabled_subscriptions_ignores_queued_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_queued", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_queued_owner",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_queued_owner",
                native_session_id="",
                title="Queued Owner",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_queued_owner",
            platform="avibe",
            author="user",
            source="user",
            message_type=messages_service.QUEUED_TYPE,
            metadata={"_web_push_user_key": "remote:user-b"},
            text="queued while prior turn runs",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_queued_owner",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Prior turn result",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-b",
            payload={
                "endpoint": "https://push.example.test/b",
                "keys": {"p256dh": "b-key", "auth": "b-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Queued Owner",
            "body": "Prior turn result",
            "session_id": "ses_queued_owner",
            "message_id": message["id"],
        }
    )

    assert sends == []


def test_send_to_enabled_subscriptions_skips_messages_marked_read_during_delay(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_x", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_read",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_read",
                native_session_id="",
                title="Read",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_read",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        messages_service.mark_session_read(conn, "ses_read", until_message_id=message["id"])
        web_push_service.upsert_subscription(
            conn,
            user_key="local",
            payload={
                "endpoint": "https://push.example.test/local",
                "keys": {"p256dh": "local-key", "auth": "local-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Read", "body": "Done", "session_id": "ses_read", "message_id": message["id"]}
    )

    assert sends == []


def test_send_to_enabled_subscriptions_skips_unowned_remote_single_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_single", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_legacy",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_legacy",
                native_session_id="",
                title="Legacy",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_legacy",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/a",
                "keys": {"p256dh": "a-key", "auth": "a-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Legacy", "body": "Done", "session_id": "ses_legacy", "message_id": message["id"]}
    )

    assert sends == []


def test_send_to_enabled_subscriptions_falls_back_to_local_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_local", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_local",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_local",
                native_session_id="",
                title="Local",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_local",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="local",
            payload={
                "endpoint": "https://push.example.test/local",
                "keys": {"p256dh": "local-key", "auth": "local-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(web_push_notifications, "_remote_access_enabled", lambda: False)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Local", "body": "Done", "session_id": "ses_local", "message_id": message["id"]}
    )

    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/local"]


def test_send_to_enabled_subscriptions_skips_local_fallback_when_remote_access_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_remote", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_remote_unowned",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_remote_unowned",
                native_session_id="",
                title="Remote",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_remote_unowned",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="local",
            payload={
                "endpoint": "https://push.example.test/local",
                "keys": {"p256dh": "local-key", "auth": "local-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(web_push_notifications, "_remote_access_enabled", lambda: True)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Remote", "body": "Done", "session_id": "ses_remote_unowned", "message_id": message["id"]}
    )

    assert sends == []


def test_send_to_enabled_subscriptions_sends_terminal_error_with_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_error", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_error",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_error",
                native_session_id="",
                title="Error",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_error",
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text="Run it",
            metadata={"_web_push_user_key": "remote:user-a"},
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_error",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="error",
            text="Failed",
            read_at=now,
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/a",
                "keys": {"p256dh": "a-key", "auth": "a-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Error", "body": "Failed", "session_id": "ses_error", "message_id": message["id"]}
    )

    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/a"]


def test_send_to_enabled_subscriptions_skips_ambiguous_legacy_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_ambiguous", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_ambiguous",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_ambiguous",
                native_session_id="",
                title="Ambiguous",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_ambiguous",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for key in ("remote:user-a", "remote:user-b"):
            web_push_service.upsert_subscription(
                conn,
                user_key=key,
                payload={
                    "endpoint": f"https://push.example.test/{key}",
                    "keys": {"p256dh": f"{key}-p256dh", "auth": f"{key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Ambiguous", "body": "Done", "session_id": "ses_ambiguous", "message_id": message["id"]}
    )

    assert sends == []
