from __future__ import annotations

import pytest

from core import web_push_notifications
from storage import message_deliveries, messages_service, project_access_service, web_push_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages
from storage.settings_service import upsert_scope
from vibe import remote_access
from vibe.authorization import AuthorizationContext


@pytest.fixture(autouse=True)
def _clear_recent_delivery_dispositions():
    web_push_notifications._RECENT_DELIVERY_DISPOSITIONS.clear()
    yield


def _remote_authorization_record(
    user_key: str,
    *,
    claims_age_seconds: int = 0,
    authorization_revision: int | None = None,
    instance_access_source: str = "email",
    organization: bool = False,
) -> dict:
    subject = user_key.removeprefix("remote:")
    record = web_push_notifications.web_push_authorization_context_record(
        user_key,
        AuthorizationContext(
            instance_role="editor",
            subject=subject,
            email=f"{subject}@example.com",
            instance_access_source=instance_access_source,
            claims_issued_at=int(web_push_notifications.time.time()) - claims_age_seconds,
            authorization_revision=authorization_revision,
            organization_id="org_1" if organization else None,
            organization_member_id="member_1" if organization else None,
            organization_role="member" if organization else None,
            is_remote=True,
        ),
    )
    assert record is not None
    return record


def _paired_revision_config(revision: int, *, instance_kind: str = ""):
    from core.services.settings import default_config

    config = default_config()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = "inst-push"
    cloud.instance_secret = "device-secret"
    cloud.backend_url = "https://backend.test"
    cloud.instance_kind = instance_kind
    config.save()
    remote_access._clear_authorization_revision_cache()
    remote_access._replace_authorization_revision(config, revision)
    return config


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

    # badge_count is intentionally NOT set at schedule time. It is computed for
    # each subscription owner after the debounce delay.
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


def test_maybe_notify_inbox_message_schedules_backend_failure_notify(monkeypatch):
    calls = []

    class _Thread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.args = args

        def start(self):
            calls.append(self.args[0])

    monkeypatch.setattr(web_push_notifications.threading, "Thread", _Thread)

    web_push_notifications.maybe_notify_inbox_message(
        {
            "id": "msg_failure",
            "platform": "avibe",
            "author": "agent",
            "type": "notify",
            "session_id": "ses_1",
            "text": "Codex backend failed",
            "metadata": {"event": "backend_failure", "failure_id": "failure_1"},
        },
        {"title": "Build fix"},
    )

    assert [call["message_id"] for call in calls] == ["msg_failure"]


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


def test_backend_failure_notify_passes_durable_web_push_gates(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-07-11T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_failure", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses_failure",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_failure",
                native_session_id="",
                title="Failure",
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
            session_id="ses_failure",
            platform="avibe",
            author="user",
            source="user",
            metadata={"_web_push_user_key": "remote:user-a"},
            message_type="user",
            text="Please finish",
        )
        failure = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_failure",
            platform="avibe",
            author="agent",
            source="agent",
            metadata={"event": "backend_failure", "failure_id": "failure_1"},
            message_type="notify",
            text="Codex backend failed",
        )
        ordinary_notify = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_failure",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="notify",
            text="Still working",
        )

    with engine.connect() as conn:
        assert web_push_notifications._message_still_unread(conn, failure["id"]) is True
        assert web_push_notifications._web_push_user_keys_for_message(conn, failure["id"]) == ["remote:user-a"]
        assert web_push_notifications._message_still_unread(conn, ordinary_notify["id"]) is False
        assert web_push_notifications._web_push_user_keys_for_message(conn, ordinary_notify["id"]) == []
    engine.dispose()


def test_web_push_owner_uses_transcript_acceptance_order(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_acceptance_owner",
            now="2026-08-04T00:00:00.000000Z",
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_acceptance_owner",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_acceptance_owner",
                native_session_id="",
                title="Acceptance owner",
                status="active",
                metadata_json="{}",
                created_at="2026-08-04T00:00:00.000000Z",
                updated_at="2026-08-04T00:00:00.000000Z",
                last_active_at="2026-08-04T00:00:00.000000Z",
            )
        )
        before = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_acceptance_owner",
            platform="avibe",
            author="user",
            source="user",
            metadata={"_web_push_user_key": "remote:before"},
            message_type="user",
            text="accepted before result",
        )
        after = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_acceptance_owner",
            platform="avibe",
            author="user",
            source="user",
            metadata={"_web_push_user_key": "remote:after"},
            message_type="user",
            text="accepted after result",
        )
        result = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_acceptance_owner",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="done",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == before["id"])
            .values(
                created_at="2026-08-04T00:00:01.000000Z",
                delivered_at="2026-08-04T00:00:02.000000Z",
            )
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == after["id"])
            .values(
                created_at="2026-08-04T00:00:02.000000Z",
                delivered_at="2026-08-04T00:00:04.000000Z",
            )
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == result["id"])
            .values(created_at="2026-08-04T00:00:03.000000Z")
        )

    with engine.connect() as conn:
        assert web_push_notifications._web_push_user_keys_for_message(
            conn,
            result["id"],
        ) == ["remote:before"]
    engine.dispose()


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
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [
                    _remote_authorization_record("remote:user-a")
                ],
            },
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

    with engine.begin() as conn:
        expired_message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done later",
        )
    issued_at = int(web_push_notifications.time.time())
    monkeypatch.setattr(
        web_push_notifications.time,
        "time",
        lambda: issued_at + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS,
    )
    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Push",
            "body": "Done later",
            "session_id": "ses_push",
            "message_id": expired_message["id"],
        }
    )
    # The persisted Personal prompt snapshot aging past the interactive
    # authorization refresh cutoff must not silently drop the recipient: the
    # PWA subscription is durable state (#1434).
    assert [send[0]["endpoint"] for send in sends] == [
        "https://push.example.test/a",
        "https://push.example.test/a",
    ]
    recent = web_push_notifications.recent_delivery_dispositions()
    assert [entry["disposition"] for entry in recent] == [
        web_push_notifications.WEB_PUSH_DISPOSITION_SENT,
        web_push_notifications.WEB_PUSH_DISPOSITION_SENT,
    ]


def test_send_to_enabled_subscriptions_rejects_stale_instance_authorization_revision(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_revision_config(41, instance_kind="organization")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = web_push_notifications.web_push_authorization_context_record(
        "remote:user-a",
        AuthorizationContext(
            instance_role="editor",
            subject="user-a",
            email="member@example.com",
            instance_access_source="email",
            # Claims issued well before the interactive refresh cutoff: claim
            # age alone must not drop the recipient on an Organization instance
            # either; only a confirmed revision change may.
            claims_issued_at=int(web_push_notifications.time.time())
            - remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS
            - 3600,
            authorization_revision=41,
            is_remote=True,
        ),
    )
    assert authorization_record is not None
    assert authorization_record["vibe_instance_authorization_revision"] == 41

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_revision",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_revision",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_revision",
                native_session_id="",
                title="Revision Push",
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
            session_id="ses_push_revision",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        first_result = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_revision",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done before revocation",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/revision",
                "keys": {"p256dh": "revision-key", "auth": "revision-auth"},
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
            "title": "Revision Push",
            "body": "Done before revocation",
            "session_id": "ses_push_revision",
            "message_id": first_result["id"],
        }
    )
    assert len(sends) == 1

    remote_access._replace_authorization_revision(config, 42)
    with engine.begin() as conn:
        stale_result = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_revision",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done after revocation",
        )
    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Revision Push",
            "body": "Done after revocation",
            "session_id": "ses_push_revision",
            "message_id": stale_result["id"],
        }
    )

    # A confirmed authorization change (fresh revision differs from the signed
    # one) stops protected delivery for the Organization policy.
    assert len(sends) == 1
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVOKED
    assert recent[0]["owners"]["remote:user-a"]["policy"] == "organization"
    engine.dispose()


def test_personal_policy_ignores_revision_state_and_refresh_cutoff(monkeypatch, tmp_path):
    """Personal notification authorization is isolated from Organization gates."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_revision_config(41, instance_kind="personal")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record(
        "remote:user-a",
        claims_age_seconds=remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS + 3600,
        authorization_revision=41,
    )

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_personal",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_personal",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_personal",
                native_session_id="",
                title="Personal Push",
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
            session_id="ses_push_personal",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-a",
            payload={
                "endpoint": "https://push.example.test/personal",
                "keys": {"p256dh": "personal-key", "auth": "personal-auth"},
            },
        )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    def _result_message() -> str:
        with engine.begin() as conn:
            row = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id="ses_push_personal",
                platform="avibe",
                author="agent",
                source="agent",
                message_type="result",
                text="Done",
            )
        return row["id"]

    first_message = _result_message()
    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Personal Push", "body": "Done", "session_id": "ses_push_personal", "message_id": first_message}
    )
    assert len(sends) == 1

    # An instance-wide revision bump is Organization authorization state: it
    # must not strand a Personal owner's subscription.
    remote_access._replace_authorization_revision(config, 42)
    second_message = _result_message()
    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Personal Push", "body": "Done again", "session_id": "ses_push_personal", "message_id": second_message}
    )
    assert len(sends) == 2
    recent = web_push_notifications.recent_delivery_dispositions()
    assert all(
        entry["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_SENT
        for entry in recent
    )
    assert all(
        entry["owners"]["remote:user-a"]["policy"] == "personal" for entry in recent
    )
    engine.dispose()


def test_unknown_instance_kind_selects_policy_from_record_claim_shape(monkeypatch, tmp_path):
    """Legacy pairings without a known kind fall back to the record's claims."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_revision_config(41)
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    personal_record = _remote_authorization_record("remote:user-personal", authorization_revision=41)
    organization_record = _remote_authorization_record(
        "remote:user-org",
        authorization_revision=41,
        instance_access_source="organization_group",
        organization=True,
    )

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_unknown_kind",
            now=now,
        )
        for session_id, user_key, record in (
            ("ses_push_unknown_personal", "remote:user-personal", personal_record),
            ("ses_push_unknown_org", "remote:user-org", organization_record),
        ):
            conn.execute(
                agent_sessions.insert().values(
                    id=session_id,
                    scope_id=scope_id,
                    agent_backend="codex",
                    agent_variant="default",
                    session_anchor=session_id,
                    native_session_id="",
                    title=session_id,
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
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                author_id=user_key,
                metadata={
                    "_web_push_user_key": user_key,
                    "_web_push_authorization_contexts": [record],
                },
                message_type="user",
                text="Please finish",
            )
            web_push_service.upsert_subscription(
                conn,
                user_key=user_key,
                payload={
                    "endpoint": f"https://push.example.test/{user_key}",
                    "keys": {"p256dh": f"{user_key}-p256dh", "auth": f"{user_key}-auth"},
                },
            )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    def _result_message(session_id: str) -> str:
        with engine.begin() as conn:
            row = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="agent",
                source="agent",
                message_type="result",
                text="Done",
            )
        return row["id"]

    personal_message = _result_message("ses_push_unknown_personal")
    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Unknown kind", "body": "Done", "session_id": "ses_push_unknown_personal", "message_id": personal_message}
    )
    assert len(sends) == 1

    remote_access._replace_authorization_revision(config, 42)

    personal_after = _result_message("ses_push_unknown_personal")
    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Unknown kind", "body": "Done again", "session_id": "ses_push_unknown_personal", "message_id": personal_after}
    )
    org_after = _result_message("ses_push_unknown_org")
    web_push_notifications._send_to_enabled_subscriptions(
        {"title": "Unknown kind", "body": "Done org", "session_id": "ses_push_unknown_org", "message_id": org_after}
    )

    # The email-shaped owner keeps receiving; the organization-group owner is
    # blocked by the confirmed revision change.
    assert [send[0]["endpoint"] for send in sends] == [
        "https://push.example.test/remote:user-personal",
        "https://push.example.test/remote:user-personal",
    ]
    recent = web_push_notifications.recent_delivery_dispositions()
    org_entry = next(
        entry
        for entry in recent
        if "remote:user-org" in entry["owners"]
    )
    assert org_entry["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVOKED
    assert org_entry["owners"]["remote:user-org"]["policy"] == "organization"
    engine.dispose()


def test_organization_signed_snapshot_without_config_records_unavailable(monkeypatch, tmp_path):
    """A revision-signed Organization snapshot must not pass without config.

    The snapshot proves the instance was revision-synced when it was minted;
    a missing or unreadable paired config is unavailability, not proof that
    no sync applies, and must not authorize protected delivery.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    # No `_paired_revision_config`: this AVIBE_HOME has no paired config at all.
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record(
        "remote:user-a",
        authorization_revision=41,
        instance_access_source="organization_group",
        organization=True,
    )
    assert authorization_record["vibe_instance_authorization_revision"] == 41

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_no_config",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_no_config",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_no_config",
                native_session_id="",
                title="No Config Push",
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
            session_id="ses_push_no_config",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_no_config",
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
                "endpoint": "https://push.example.test/no-config",
                "keys": {"p256dh": "no-config-key", "auth": "no-config-auth"},
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
            "title": "No Config Push",
            "body": "Done",
            "session_id": "ses_push_no_config",
            "message_id": message["id"],
        }
    )

    assert sends == []
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE
    assert recent[0]["owners"]["remote:user-a"]["policy"] == "organization"
    engine.dispose()


def test_organization_revision_unavailable_retries_once_then_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_revision_config(41, instance_kind="organization")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record(
        "remote:user-a",
        authorization_revision=41,
    )

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_unavailable_retry",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_unavailable_retry",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_unavailable_retry",
                native_session_id="",
                title="Unavailable Retry",
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
            session_id="ses_push_unavailable_retry",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_unavailable_retry",
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
                "endpoint": "https://push.example.test/unavailable-retry",
                "keys": {"p256dh": "retry-key", "auth": "retry-auth"},
            },
        )

    # The fresh watermark reads as unavailable exactly once (stale snapshot or
    # control-plane outage); the bounded sync retry refreshes it.
    watermark_reads = []

    def _current_revision(config, *, now=None):
        watermark_reads.append(now)
        return 41 if len(watermark_reads) > 1 else None

    monkeypatch.setattr(remote_access, "current_authorization_revision", _current_revision)
    sync_calls = []
    monkeypatch.setattr(
        web_push_notifications,
        "_retry_authorization_revision_sync",
        lambda config: sync_calls.append(config),
    )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Unavailable Retry",
            "body": "Done",
            "session_id": "ses_push_unavailable_retry",
            "message_id": message["id"],
        }
    )

    assert len(sync_calls) == 1
    assert [send[0]["endpoint"] for send in sends] == [
        "https://push.example.test/unavailable-retry"
    ]
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_SENT
    engine.dispose()


def test_organization_revision_unavailable_after_bounded_retry_skips_with_disposition(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_revision_config(41, instance_kind="organization")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record(
        "remote:user-a",
        authorization_revision=41,
    )

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_unavailable",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_unavailable",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_unavailable",
                native_session_id="",
                title="Unavailable Push",
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
            session_id="ses_push_unavailable",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_unavailable",
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
                "endpoint": "https://push.example.test/unavailable",
                "keys": {"p256dh": "unavailable-key", "auth": "unavailable-auth"},
            },
        )

    monkeypatch.setattr(
        remote_access,
        "current_authorization_revision",
        lambda config, *, now=None: None,
    )
    sync_calls = []
    monkeypatch.setattr(
        web_push_notifications,
        "_retry_authorization_revision_sync",
        lambda config: sync_calls.append(config),
    )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Unavailable Push",
            "body": "Done",
            "session_id": "ses_push_unavailable",
            "message_id": message["id"],
        }
    )

    # Temporary unavailability is a per-delivery decision, not confirmed
    # revocation and not permanent loss: exactly one bounded retry, an explicit
    # disposition, and the subscription stays enabled for the next attempt.
    assert sends == []
    assert len(sync_calls) == 1
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE
    owner = recent[0]["owners"]["remote:user-a"]
    assert owner["policy"] == "organization"
    assert owner["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE
    with engine.connect() as conn:
        assert web_push_service.count_enabled(conn, user_key="remote:user-a") == 1
    engine.dispose()


def test_organization_unavailable_retry_is_shared_across_merged_owners(monkeypatch, tmp_path):
    """One merged prompt with several Organization owners retries the watermark once."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_revision_config(41, instance_kind="organization")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_shared_retry",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_shared_retry",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_shared_retry",
                native_session_id="",
                title="Shared Retry",
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
            session_id="ses_push_shared_retry",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_keys": ["remote:user-a", "remote:user-b"],
                "_web_push_authorization_contexts": [
                    _remote_authorization_record("remote:user-a", authorization_revision=41),
                    _remote_authorization_record("remote:user-b", authorization_revision=41),
                ],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_shared_retry",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        for user_key in ("remote:user-a", "remote:user-b"):
            web_push_service.upsert_subscription(
                conn,
                user_key=user_key,
                payload={
                    "endpoint": f"https://push.example.test/shared-{user_key}",
                    "keys": {"p256dh": f"{user_key}-key", "auth": f"{user_key}-auth"},
                },
            )

    monkeypatch.setattr(
        remote_access,
        "current_authorization_revision",
        lambda config, *, now=None: None,
    )
    sync_calls = []
    monkeypatch.setattr(
        web_push_notifications,
        "_retry_authorization_revision_sync",
        lambda config: sync_calls.append(config),
    )

    sends = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Shared Retry",
            "body": "Done",
            "session_id": "ses_push_shared_retry",
            "message_id": message["id"],
        }
    )

    # One control-plane outage costs one bounded sync request for the whole
    # owner set, not one request timeout per merged owner.
    assert sends == []
    assert len(sync_calls) == 1
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE
    assert set(recent[0]["owners"]) == {"remote:user-a", "remote:user-b"}
    engine.dispose()


def test_organization_unsigned_record_reports_refresh_required(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_revision_config(41, instance_kind="organization")
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record("remote:user-a")

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_unsigned",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_unsigned",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_unsigned",
                native_session_id="",
                title="Unsigned Push",
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
            session_id="ses_push_unsigned",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_unsigned",
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
                "endpoint": "https://push.example.test/unsigned",
                "keys": {"p256dh": "unsigned-key", "auth": "unsigned-auth"},
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
            "title": "Unsigned Push",
            "body": "Done",
            "session_id": "ses_push_unsigned",
            "message_id": message["id"],
        }
    )

    # Organization claims that predate revision signing cannot be confirmed
    # current at delivery time; skip them with an explicit, visible reason.
    assert sends == []
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED
    assert recent[0]["owners"]["remote:user-a"]["policy"] == "organization"
    engine.dispose()


def test_authorized_owner_without_subscription_records_no_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-08-04T00:00:00Z"
    authorization_record = _remote_authorization_record("remote:user-a")

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_no_sub",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_no_sub",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="ses_push_no_sub",
                native_session_id="",
                title="No Subscription",
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
            session_id="ses_push_no_sub",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_no_sub",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Done",
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-b",
            payload={
                "endpoint": "https://push.example.test/other",
                "keys": {"p256dh": "other-key", "auth": "other-auth"},
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
            "title": "No Subscription",
            "body": "Done",
            "session_id": "ses_push_no_sub",
            "message_id": message["id"],
        }
    )

    assert sends == []
    recent = web_push_notifications.recent_delivery_dispositions(user_key="remote:user-a")
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION
    assert recent[0]["owners"]["remote:user-a"]["disposition"] is None
    engine.dispose()


def test_send_to_enabled_subscriptions_rechecks_restricted_project_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    context = AuthorizationContext(
        instance_role="editor",
        subject="user-a",
        email="member@example.com",
        instance_access_source="email",
        claims_issued_at=int(web_push_notifications.time.time()),
        is_remote=True,
    )
    authorization_record = web_push_notifications.web_push_authorization_context_record(
        "remote:user-a",
        context,
    )
    assert authorization_record is not None

    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_acl",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_acl",
                scope_id=scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_push_acl",
                native_session_id="",
                title="Push ACL",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        assert project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": "proj_push_acl",
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "email",
                        "principal_value": "member@example.com",
                        "access_role": "editor",
                    }
                ],
            },
        ).outcome == "applied"
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_acl",
            platform="avibe",
            author="user",
            source="user",
            author_id="remote:user-a",
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
            message_type="user",
            text="Please finish",
        )
        allowed_message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_acl",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Allowed",
        )
        hidden_scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_push_hidden",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_push_hidden",
                scope_id=hidden_scope_id,
                agent_backend="claude",
                agent_variant="default",
                session_anchor="ses_push_hidden",
                native_session_id="",
                title="Hidden Push",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        assert project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": "proj_push_hidden",
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "email",
                        "principal_value": "someone-else@example.com",
                        "access_role": "editor",
                    }
                ],
            },
        ).outcome == "applied"
        messages_service.append(
            conn,
            scope_id=hidden_scope_id,
            session_id="ses_push_hidden",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Hidden unread",
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
        {
            "title": "Push ACL",
            "body": "Allowed",
            "session_id": "ses_push_acl",
            "message_id": allowed_message["id"],
        }
    )
    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/a"]
    assert [send[1]["badge_count"] for send in sends] == [1]

    with engine.begin() as conn:
        assert project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": "proj_push_acl",
                "revision": 2,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "email",
                        "principal_value": "someone-else@example.com",
                        "access_role": "editor",
                    }
                ],
            },
        ).outcome == "applied"
        revoked_message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_push_acl",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Revoked",
        )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Push ACL",
            "body": "Revoked",
            "session_id": "ses_push_acl",
            "message_id": revoked_message["id"],
        }
    )
    assert [send[0]["endpoint"] for send in sends] == ["https://push.example.test/a"]


def test_send_to_enabled_subscriptions_sets_visible_badge_count(monkeypatch, tmp_path):
    """One Project's badge includes every visible unread session in it."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-06-04T00:00:00Z"
    context = AuthorizationContext(
        instance_role="editor",
        subject="user-a",
        email="member@example.com",
        instance_access_source="email",
        claims_issued_at=int(web_push_notifications.time.time()),
        is_remote=True,
    )
    authorization_record = web_push_notifications.web_push_authorization_context_record(
        "remote:user-a",
        context,
    )
    assert authorization_record is not None
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
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [authorization_record],
            },
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


def test_send_to_enabled_subscriptions_rejects_legacy_session_owner(monkeypatch, tmp_path):
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

    assert sends == []


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
            metadata={
                "_web_push_user_key": "remote:user-b",
                "_web_push_authorization_contexts": [
                    _remote_authorization_record("remote:user-b")
                ],
            },
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
            metadata={
                "_web_push_user_keys": ["remote:user-a", "remote:user-b"],
                "_web_push_authorization_contexts": [
                    _remote_authorization_record("remote:user-a"),
                    _remote_authorization_record("remote:user-b"),
                ],
            },
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
        message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_queued_owner",
            session_id="ses_queued_owner",
            priority="p3",
            state="queued",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id="ses_queued_owner",
                platform="avibe",
                author="user",
                source="user",
                metadata={"_web_push_user_key": "remote:user-b"},
                text="queued while prior turn runs",
            ),
            dispatch_text="queued while prior turn runs",
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
    recent = web_push_notifications.recent_delivery_dispositions()
    assert recent[0]["disposition"] == web_push_notifications.WEB_PUSH_DISPOSITION_SENT
    # A local owner is locally authorized — never labeled as needing a remote
    # authorization refresh.
    assert recent[0]["owners"]["local"] == {
        "policy": "local",
        "disposition": None,
        "reason": "local fallback with remote access disabled",
    }


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
            metadata={
                "_web_push_user_key": "remote:user-a",
                "_web_push_authorization_contexts": [
                    _remote_authorization_record("remote:user-a")
                ],
            },
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
