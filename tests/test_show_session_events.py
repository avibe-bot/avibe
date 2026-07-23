from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from core.show_session_events import ShowSessionEventError, ShowSessionEventStore, _format_transcript_text
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages, show_session_events
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_session(session_id: str = "ses_mark") -> str:
    from storage import messages_service

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    last_active_at = "2000-01-01T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_events",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_" + session_id,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=last_active_at,
            )
        )
    return scope_id


def test_show_event_store_records_assistant_mark_and_transcript_message(isolated_state):
    _seed_session()
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        previous_active_at = conn.execute(
            select(agent_sessions.c.last_active_at).where(agent_sessions.c.id == "ses_mark")
        ).scalar_one()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {
                    "target": "mark-default-summary",
                    "body": "Review this summary again.",
                },
                "anchor": {
                    "selector": "[mark-default='summary']",
                    "text": "Quarterly summary",
                },
            },
        )
    finally:
        store.close()

    assert event["type"] == "assistant.mark.created"
    assert event["scope_id"]
    assert event["scope"] == "default"
    assert event["message_id"]
    assert event["message"]["id"] == event["message_id"]
    assert "[agent-mark] mark-default-summary" in event["transcript_text"]
    assert "Anchor: [mark-default='summary']" in event["transcript_text"]

    with engine.connect() as conn:
        event_row = conn.execute(select(show_session_events)).mappings().one()
        message_row = conn.execute(select(messages).where(messages.c.id == event["message_id"])).mappings().one()
        last_active_at = conn.execute(
            select(agent_sessions.c.last_active_at).where(agent_sessions.c.id == "ses_mark")
        ).scalar_one()

    assert event_row["id"] == event["id"]
    assert json.loads(event_row["payload_json"])["body"] == "Review this summary again."
    assert message_row["author"] == "agent"
    assert message_row["platform"] == "avibe"
    assert message_row["native_message_id"] == f"show:{event['id']}"
    assert "Review this summary again." in message_row["content_text"]
    assert last_active_at != previous_active_at


def test_show_event_store_records_human_annotation_with_anchor_context(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "question",
                    "severity": "important",
                    "comment": "Clarify this claim.",
                    "anchor": {
                        "kind": "text-range",
                        "selector": "[mark-default='summary']",
                        "textQuote": "Quarterly summary",
                    },
                },
            },
        )
    finally:
        store.close()

    assert event["type"] == "human.annotation.created"
    assert event["actor"] == "human"
    assert event["scope"] == "default"
    assert event["payload"]["status"] == "pending"
    assert event["payload"]["author"] == {"kind": "local"}
    assert event["message_id"]
    assert "[show-annotation] question" in event["transcript_text"]
    assert "Clarify this claim." in event["transcript_text"]
    assert "Quote: Quarterly summary" in event["transcript_text"]

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        message_row = conn.execute(select(messages).where(messages.c.id == event["message_id"])).mappings().one()

    assert message_row["author"] == "user"
    assert json.loads(message_row["metadata_json"])["author"] == {"kind": "local"}


def test_show_event_store_records_remote_human_author_in_event_and_message(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.created",
                "annotation": {"comment": "Review this."},
            },
            author={"kind": "user", "email": "alex@example.com"},
        )
    finally:
        store.close()

    assert event["payload"]["author"] == {"kind": "user", "email": "alex@example.com"}
    assert event["message"]["metadata"]["author"] == {
        "kind": "user",
        "email": "alex@example.com",
    }


def test_show_event_store_keeps_remote_author_out_of_intent_fallback_text(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.intent.submitted",
                "payload": {
                    "intent": "choose",
                    "author": {"kind": "user", "email": "spoofed@example.com"},
                },
            },
            author={"kind": "user", "email": "alex@example.com"},
        )
    finally:
        store.close()

    assert event["payload"]["author"] == {"kind": "user", "email": "alex@example.com"}
    assert "alex@example.com" not in event["transcript_text"]
    assert "spoofed@example.com" not in event["transcript_text"]
    assert '"author"' not in event["transcript_text"]


def test_annotation_control_event_has_no_transcript_or_dispatch(isolated_state):
    from vibe.ui_server import _show_event_requests_dispatch

    _seed_session()
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "system.annotation.control",
                "payload": {"action": "enable", "mode": "screenshot"},
            },
        )
    finally:
        store.close()

    assert event["actor"] == "system"
    assert event["payload"] == {"action": "enable", "mode": "screenshot"}
    assert event["transcript_text"] == ""
    assert event["message_id"] is None
    assert event["message"] is None
    assert _show_event_requests_dispatch(event) is False

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).scalar_one() == event["id"]
        assert conn.execute(select(messages.c.id)).first() is None


@pytest.mark.parametrize(
    "control",
    [
        {"action": "toggle"},
        {"action": "enable", "mode": "area"},
        {"action": "set-mode"},
    ],
)
def test_annotation_control_event_rejects_invalid_payload(isolated_state, control):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_mark",
                {"type": "system.annotation.control", "payload": control},
            )
    finally:
        store.close()

    assert exc_info.value.code == "invalid_payload"


def test_show_event_store_rejects_mismatched_session_id(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_mark",
                {
                    "sessionId": "ses_other",
                    "type": "human.annotation.created",
                    "annotation": {"comment": "Wrong session."},
                },
            )
    finally:
        store.close()

    assert exc_info.value.code == "session_mismatch"
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).first() is None


@pytest.mark.parametrize(
    "event_payload",
    [
        {
            "type": "human.annotation.created",
            "payload": {"sessionId": "ses_other", "comment": "Wrong session."},
        },
        {
            "type": "human.annotation.created",
            "annotation": {"session_id": "ses_other", "comment": "Wrong session."},
        },
        {
            "type": "assistant.mark.created",
            "mark": {"sessionId": "ses_other", "target": "summary", "body": "Wrong session."},
        },
    ],
)
def test_show_event_store_rejects_nested_mismatched_session_id(isolated_state, event_payload):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append("ses_mark", event_payload)
    finally:
        store.close()

    assert exc_info.value.code == "session_mismatch"
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).first() is None


def test_show_event_store_records_element_group_annotation_context(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "change",
                    "comment": "Align these cards.",
                    "userRegion": {"x": 10, "y": 20, "width": 300, "height": 120},
                    "classification": {"mode": "element-group", "confidence": 0.82},
                    "matchedElements": [
                        {
                            "kind": "element",
                            "selector": "[data-card='summary']",
                            "text": "Summary",
                        },
                        {
                            "kind": "element",
                            "selector": "[data-card='details']",
                            "text": "Details",
                        },
                    ],
                },
            },
        )
    finally:
        store.close()

    assert event["payload"]["primaryAnchor"] == "element-group"
    assert event["payload"]["userRegion"]["width"] == 300
    assert len(event["payload"]["matchedElements"]) == 2
    assert event["anchor"]["selector"] == "[data-card='summary']"
    assert "Anchor kind: element-group" in event["transcript_text"]
    assert "Region: x:10, y:20, 300x120" in event["transcript_text"]
    assert "Selection: element-group" in event["transcript_text"]
    assert "Matched elements: 2" in event["transcript_text"]


def test_show_event_store_records_screenshot_annotation_batch(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "review",
                    "comment": "Review the captured area.",
                    "screenshot": {
                        "attachmentId": "show_asset_screenshot_1",
                        "region": {"x": 24, "y": 32, "width": 640, "height": 360},
                        "items": [
                            {
                                "label": "1",
                                "comment": "This counter looks stale.",
                                "point": {"x": 120, "y": 80},
                            },
                            {
                                "label": "2",
                                "comment": "Crop this empty area.",
                                "region": {"x": 420, "y": 240, "width": 160, "height": 72},
                            },
                        ],
                    },
                },
            },
        )
    finally:
        store.close()

    assert event["payload"]["primaryAnchor"] == "screenshot"
    assert event["payload"]["screenshot"]["attachmentId"] == "show_asset_screenshot_1"
    assert len(event["payload"]["screenshot"]["items"]) == 2
    assert "Anchor kind: screenshot" in event["transcript_text"]
    assert "Screenshot: show_asset_screenshot_1" in event["transcript_text"]
    assert "Screenshot region: x:24, y:32, 640x360" in event["transcript_text"]
    assert "1. This counter looks stale. (x:120, y:80)" in event["transcript_text"]
    assert "2. Crop this empty area. (x:420, y:240, 160x72)" in event["transcript_text"]


def test_show_event_store_records_annotation_resolution(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.resolved",
                "annotation": {
                    "id": "annotation_1",
                    "comment": "This is resolved.",
                },
            },
        )
    finally:
        store.close()

    assert event["payload"]["id"] == "annotation_1"
    assert event["payload"]["status"] == "resolved"
    assert "resolved" in event["transcript_text"]


def test_show_event_store_keeps_object_ids_separate_from_event_ids(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        created = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {
                    "id": "mark_1",
                    "target": "summary",
                    "body": "Created.",
                },
            },
        )
        resolved = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.resolved",
                "mark": {
                    "id": "mark_1",
                    "updatedAt": created["payload"]["updatedAt"],
                },
            },
        )
    finally:
        store.close()

    assert created["payload"]["id"] == "mark_1"
    assert resolved["payload"]["id"] == "mark_1"
    assert created["id"] != "mark_1"
    assert resolved["id"] != "mark_1"
    assert created["id"] != resolved["id"]


def test_show_event_store_hydrates_read_receipt_from_active_mark(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        created = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {"id": "mark_1", "target": "summary", "body": "Server body."},
                "anchor": {"selector": "#summary", "text": "Summary"},
            },
        )
        resolved = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.resolved",
                "mark": {
                    "id": "mark_1",
                    "updatedAt": created["payload"]["updatedAt"],
                    "target": "forged",
                    "body": "Forged body.",
                },
                "anchor": {"selector": "#forged"},
            },
            author={"kind": "user", "email": "reader@example.com"},
        )
    finally:
        store.close()

    assert resolved["payload"]["target"] == "summary"
    assert resolved["payload"]["body"] == "Server body."
    assert resolved["anchor"] == {"selector": "#summary", "text": "Summary"}
    assert resolved["payload"]["author"] == {"kind": "user", "email": "reader@example.com"}
    assert resolved["transcript_text"] == ""
    assert resolved["message_id"] is None
    assert resolved["message"] is None


@pytest.mark.parametrize(
    ("mark", "expected_code"),
    [
        ({"id": "missing", "updatedAt": "2026-07-23T00:00:00Z"}, "mark_not_active"),
        ({"id": "mark_1"}, "mark_version_required"),
    ],
)
def test_show_event_store_rejects_invalid_mark_resolution(isolated_state, mark, expected_code):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {"id": "mark_1", "target": "summary", "body": "Current."},
            },
        )
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append("ses_mark", {"type": "assistant.mark.resolved", "mark": mark})
    finally:
        store.close()

    assert exc_info.value.code == expected_code


def test_show_event_store_rejects_stale_mark_read_receipt(isolated_state):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        original = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {
                    "id": "mark_1",
                    "target": "summary",
                    "body": "Original.",
                    "createdAt": "2026-07-23T00:00:00Z",
                    "updatedAt": "2026-07-23T00:00:00Z",
                },
            },
        )
        store.append(
            "ses_mark",
            {
                "type": "assistant.mark.updated",
                "mark": {
                    "id": "mark_1",
                    "target": "summary",
                    "body": "Replacement.",
                    "createdAt": original["payload"]["createdAt"],
                    "updatedAt": "2026-07-23T00:00:01Z",
                },
            },
        )
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_mark",
                {
                    "type": "assistant.mark.resolved",
                    "mark": {"id": "mark_1", "updatedAt": original["payload"]["updatedAt"]},
                },
                author={"kind": "local"},
            )
        active = store.active_marks("ses_mark")
    finally:
        store.close()

    assert exc_info.value.code == "mark_version_conflict"
    assert [(mark["id"], mark["body"]) for mark in active] == [("mark_1", "Replacement.")]


def test_show_event_store_records_intent_dispatch_payload(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.intent.submitted",
                "payload": {
                    "component": "decision",
                    "intent": "choose",
                    "value": "B",
                    "comment": "Pick B.",
                    "dispatch": True,
                },
            },
        )
    finally:
        store.close()

    assert event["payload"]["dispatch"] is True
    assert "[show-intent] choose" in event["transcript_text"]
    assert "Pick B." in event["transcript_text"]


def test_show_event_store_records_assistant_page_update(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "assistant.page.updated",
                "payload": {
                    "summary": "Updated the Show Page with the revised flow.",
                },
            },
        )
    finally:
        store.close()

    assert event["actor"] == "assistant"
    assert event["message_id"]
    assert "[show-page-updated] Updated the Show Page" in event["transcript_text"]


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_header"),
    [
        (
            "assistant.mark.created",
            {"scope": "default", "target": "summary", "body": "Created."},
            "[agent-mark] summary",
        ),
        (
            "assistant.mark.resolved",
            {"scope": "default", "target": "summary", "body": "Resolved."},
            "[agent-mark resolved] summary",
        ),
        (
            "assistant.mark.updated",
            {"scope": "review", "target": "summary", "body": "Updated."},
            "[agent-mark updated scope=review] summary",
        ),
        (
            "human.intent.submitted",
            {"scope": "default", "intent": "question", "text": "Why?"},
            "[show-intent] question",
        ),
        (
            "human.intent.submitted",
            {"scope": "review", "intent": "question", "text": "Why?"},
            "[show-intent scope=review] question",
        ),
        (
            "human.annotation.created",
            {"scope": "default", "intent": "question", "comment": "Why?"},
            "[show-annotation] question",
        ),
        (
            "human.annotation.resolved",
            {"scope": "default", "intent": "comment", "comment": "Done."},
            "[show-annotation resolved] comment",
        ),
        (
            "human.annotation.created",
            {"scope": "review", "intent": "question", "comment": "Why?"},
            "[show-annotation scope=review] question",
        ),
        (
            "human.annotation.resolved",
            {"scope": "review", "intent": "comment", "comment": "Done."},
            "[show-annotation resolved scope=review] comment",
        ),
    ],
)
def test_show_event_transcript_headers_only_render_deviations(event_type, payload, expected_header):
    transcript = _format_transcript_text(event_type, payload, {})

    assert transcript.splitlines()[0] == expected_header


def test_show_event_store_rejects_unknown_session(isolated_state):
    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as raised:
            store.append(
                "ses_missing",
                {
                    "type": "assistant.mark.created",
                    "mark": {"target": "summary", "body": "body"},
                },
            )
    finally:
        store.close()

    assert raised.value.code == "session_not_found"


def test_show_event_store_uses_server_created_at_for_storage_cursor(monkeypatch, isolated_state):
    _seed_session()
    monkeypatch.setattr("core.show_session_events._utc_now_iso", lambda: "2026-05-30T10:00:00+00:00")

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {
                    "target": "summary",
                    "body": "body",
                    "createdAt": "1999-01-01T00:00:00+00:00",
                },
            },
        )
    finally:
        store.close()

    assert event["created_at"] == "2026-05-30T10:00:00+00:00"
    assert event["payload"]["createdAt"] == "1999-01-01T00:00:00+00:00"

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        event_row = conn.execute(select(show_session_events)).mappings().one()

    assert event_row["created_at"] == "2026-05-30T10:00:00+00:00"


def test_show_event_store_lists_after_cursor(isolated_state):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        first = store.append("ses_mark", {"type": "assistant.mark.created", "mark": {"target": "a", "body": "one"}})
        second = store.append("ses_mark", {"type": "assistant.mark.created", "mark": {"target": "b", "body": "two"}})
        page = store.list("ses_mark", after_id=first["id"])
    finally:
        store.close()

    assert [event["id"] for event in page["events"]] == [second["id"]]


def test_show_event_dispatch_streams_via_stream_dispatch(isolated_state, monkeypatch):
    """Regression guard: the Show-page dispatch flow MUST call
    ``internal_client.stream_dispatch`` and re-publish each turn event as
    ``show.dispatch``. Step 6 removed ``stream_dispatch`` as dead, but the merged
    show-annotation feature still depends on it — without this test that removal
    passed CI yet broke the Show page at runtime (Codex P2)."""
    import asyncio

    from vibe import internal_client, ui_server
    from vibe.sse_broker import broker

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(broker, "publish", lambda event, data: published.append((event, data)))

    async def fake_stream_dispatch(payload, **kwargs):
        assert payload["session_id"] == "ses_show" and payload["text"] == "do the thing"
        yield ("turn.start", {"session_id": "ses_show"})
        yield ("turn.chunk", {"text": "working", "kind": "notify"})
        yield ("turn.end", {"session_id": "ses_show"})

    # setattr requires the attribute to exist — so this also asserts stream_dispatch
    # wasn't removed again.
    monkeypatch.setattr(internal_client, "stream_dispatch", fake_stream_dispatch)

    asyncio.run(
        ui_server._run_show_event_dispatch(
            {
                "id": "evt1",
                "session_id": "ses_show",
                "scope_id": "scope1",
                "transcript_text": "do the thing",
                "message_id": "m1",
            }
        )
    )

    assert [d["event"] for (e, d) in published if e == "show.dispatch"] == [
        "turn.start",
        "turn.chunk",
        "turn.end",
    ]


@pytest.mark.parametrize(
    ("intent", "expects_guidance"),
    [
        ("question", True),
        ("comment", True),
        (None, True),
        ("fix", False),
        ("change", False),
        ("approve", False),
    ],
)
def test_annotation_dispatch_text_adds_event_id_and_optional_reply_guidance(monkeypatch, intent, expects_guidance):
    import asyncio

    from vibe import internal_client, ui_server

    captured = []

    async def fake_stream_dispatch(payload, **kwargs):
        captured.append(payload)
        if False:
            yield None

    monkeypatch.setattr(internal_client, "stream_dispatch", fake_stream_dispatch)
    label = intent or "comment"
    event = {
        "id": "show_evt_1a2b3c4d",
        "type": "human.annotation.created",
        "session_id": "ses_show",
        "scope_id": "scope1",
        "payload": {"intent": intent} if intent is not None else {},
        "transcript_text": f"[show-annotation] {label}\n\nReview this.",
        "message_id": "m1",
    }

    asyncio.run(ui_server._run_show_event_dispatch(event))

    assert event["transcript_text"] == f"[show-annotation] {label}\n\nReview this."
    assert "Show event id: show_evt_1a2b3c4d" in captured[0]["text"]
    guidance = (
        "如需在页面上原位回应，可执行：\n"
        "  vibe show reply show_evt_1a2b3c4d --message '<你的回答>'\n"
        "（也可以直接修改页面内容来响应，按场景选择。）"
    )
    assert (guidance in captured[0]["text"]) is expects_guidance
    if expects_guidance:
        assert captured[0]["text"].endswith(guidance)
