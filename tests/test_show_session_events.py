from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest
from sqlalchemy import select

from core.show_session_events import (
    ANCHOR_HUMAN_COPY_KEYS,
    ASSISTANT_MARK_EVENT_TYPES,
    MARK_LOCATOR_MAX_LENGTH,
    MARK_PAYLOAD_KEYS,
    SHOW_TRIGGER_KIND,
    ShowSessionEventError,
    ShowSessionEventStore,
    _annotation_attachments,
    _annotation_display,
    _format_dispatch_text,
    _format_transcript_text,
    localized_show_event_error,
    show_event_requests_dispatch,
)
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, media_objects, messages, show_session_events
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


def _png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = (b"\x00" + b"\x00\x00\x00" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def _image_data_url(content_type: str, raw: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"


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
    assert event["transcript_text"] == "Review this summary again."
    assert "mark-default-summary" not in event["transcript_text"]
    assert "[mark-default='summary']" not in event["transcript_text"]
    assert event["message"]["type"] == "annotation"
    assert event["message"]["content"]["annotation"] == {
        "direction": "agent",
        "action": "created",
        "quote": "Quarterly summary",
    }

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


@pytest.mark.parametrize(
    ("event_type", "payload", "direction"),
    [
        (
            "human.annotation.created",
            {"annotation": {"comment": "", "anchor": {"text": "Summary"}}},
            "user",
        ),
        (
            "assistant.mark.created",
            {"mark": {"target": "#summary", "body": ""}, "anchor": {"text": "Summary"}},
            "agent",
        ),
    ],
)
def test_annotation_card_can_have_empty_authored_text(
    isolated_state,
    event_type,
    payload,
    direction,
):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {"type": event_type, **payload},
        )
    finally:
        store.close()

    assert event["transcript_text"] == ""
    assert event["message"]["text"] == ""
    assert event["message"]["content"] == {
        "text": "",
        "annotation": {
            "direction": direction,
            "action": "created",
            "quote": "Summary",
        },
    }


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
    assert event["transcript_text"] == "Clarify this claim."
    assert event["message"]["type"] == "annotation"
    assert event["message"]["content"]["annotation"] == {
        "direction": "user",
        "action": "created",
        "quote": "Quarterly summary",
    }

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
    assert show_event_requests_dispatch(event) is False

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
    assert event["transcript_text"] == "Align these cards."
    assert event["message"]["metadata"]["anchor_kind"] == "element-group"
    assert event["message"]["metadata"]["user_region"] == "x:10, y:20, 300x120"
    assert event["message"]["metadata"]["classification"] == "element-group"
    assert event["message"]["metadata"]["matched_element_count"] == 2


def test_show_event_store_materializes_screenshot_attachment(isolated_state):
    scope_id = _seed_session()
    raw = _png_bytes(4, 3)

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
                        "attachmentId": "screenshot_client_only",
                        "mimeType": "image/png",
                        "width": 4,
                        "height": 3,
                        "capturedRegion": {"x": 24, "y": 32, "width": 640, "height": 360},
                        "dataUrl": _image_data_url("image/png", raw),
                        "items": [{"label": "1", "comment": "This counter looks stale."}],
                    },
                },
            },
        )
    finally:
        store.close()

    screenshot = event["payload"]["screenshot"]
    local_path = Path(screenshot["path"])
    assert screenshot["attachmentId"] != "screenshot_client_only"
    assert screenshot["mimeType"] == "image/png"
    assert screenshot["width"] == 4
    assert screenshot["height"] == 3
    assert screenshot["capturedRegion"] == {"x": 24, "y": 32, "width": 640, "height": 360}
    assert screenshot["items"] == [{"label": "1", "comment": "This counter looks stale."}]
    assert "dataUrl" not in screenshot
    assert local_path.is_absolute()
    assert local_path.parent == isolated_state / "attachments" / "avibe" / "ses_mark"
    assert local_path.read_bytes() == raw
    assert event["transcript_text"] == "Review the captured area."
    assert event["message"]["content"]["attachments"] == [
        {
            "url": f"/api/media/{screenshot['attachmentId']}",
            "name": "annotation-region.png",
            "mime": "image/png",
            "kind": "image",
            "width": 4,
            "height": 3,
        }
    ]
    assert event["message"]["metadata"]["screenshot_region"] == (
        "x:24, y:32, 640x360"
    )

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        media_row = conn.execute(select(media_objects)).mappings().one()
        stored_payload = json.loads(conn.execute(select(show_session_events.c.payload_json)).scalar_one())
    assert media_row["token"] == screenshot["attachmentId"]
    assert media_row["scope_id"] == scope_id
    assert media_row["session_id"] == "ses_mark"
    assert media_row["source"] == "show_annotation"
    assert media_row["local_path"] == str(local_path)
    assert "dataUrl" not in stored_payload["screenshot"]


def test_show_event_store_rolls_back_media_from_losing_idempotent_insert(
    isolated_state,
    monkeypatch,
):
    import core.show_session_events as show_events

    _seed_session()
    payload = {
        "id": "show_evt_screenshot_race",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Keep only the winning screenshot.",
            "screenshot": {
                "dataUrl": _image_data_url("image/png", _png_bytes(4, 3)),
            },
        },
    }
    store = ShowSessionEventStore()
    try:
        winner = store.append("ses_mark", payload)
        real_existing = show_events._existing_event_payload
        calls = 0

        def miss_preflight_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return real_existing(*args, **kwargs)

        monkeypatch.setattr(show_events, "_existing_event_payload", miss_preflight_once)
        replay = store.append("ses_mark", payload)
    finally:
        store.close()

    with create_sqlite_engine().connect() as conn:
        media_rows = conn.execute(select(media_objects)).mappings().all()
    attachment_dir = Path(winner["payload"]["screenshot"]["path"]).parent
    assert replay["id"] == winner["id"]
    assert len(media_rows) == 1
    assert [path.resolve() for path in attachment_dir.iterdir()] == [
        Path(winner["payload"]["screenshot"]["path"]).resolve()
    ]


def test_show_event_store_materializes_webp_without_conversion(isolated_state):
    _seed_session()
    raw = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "comment": "Review this image.",
                    "screenshot": {
                        "mimeType": "image/webp",
                        "width": 1,
                        "height": 1,
                        "capturedRegion": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "dataUrl": _image_data_url("image/webp", raw),
                        "items": [],
                    },
                },
            },
        )
    finally:
        store.close()

    screenshot = event["payload"]["screenshot"]
    local_path = Path(screenshot["path"])
    assert screenshot["mimeType"] == "image/webp"
    assert local_path.suffix == ".webp"
    assert local_path.read_bytes() == raw


@pytest.mark.parametrize(
    "data_url,mime_type,width,height",
    [
        ("not-a-data-url", "image/png", 1, 1),
        ("data:image/png;base64,%%%%", "image/png", 1, 1),
        (_image_data_url("image/webp", _png_bytes(1, 1)), "image/webp", 1, 1),
        (_image_data_url("image/png", _png_bytes(1, 1)), "image/webp", 1, 1),
        (_image_data_url("image/png", _png_bytes(2049, 1)), "image/png", 2049, 1),
    ],
)
def test_show_event_store_rejects_invalid_screenshot_data_url(
    isolated_state, data_url, mime_type, width, height
):
    _seed_session()
    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_mark",
                {
                    "type": "human.annotation.created",
                    "annotation": {
                        "comment": "Invalid screenshot.",
                        "screenshot": {
                            "mimeType": mime_type,
                            "width": width,
                            "height": height,
                            "capturedRegion": {"x": 0, "y": 0, "width": width, "height": height},
                            "dataUrl": data_url,
                            "items": [],
                        },
                    },
                },
            )
    finally:
        store.close()

    assert exc_info.value.code == "invalid_payload"
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events)).first() is None
        assert conn.execute(select(media_objects)).first() is None
    attachment_root = isolated_state / "attachments"
    assert not attachment_root.exists() or not any(path.is_file() for path in attachment_root.rglob("*"))


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
    assert event["transcript_text"] == "Review the captured area."
    dispatch_text = _format_dispatch_text(
        event["type"],
        event["payload"],
        event["anchor"],
        event_id=event["id"],
    )
    assert "Anchor kind: screenshot" in dispatch_text
    assert "Screenshot: show_asset_screenshot_1" in dispatch_text
    assert "Screenshot region: x:24, y:32, 640x360" in dispatch_text
    assert "1. This counter looks stale. (x:120, y:80)" in dispatch_text
    assert "2. Crop this empty area. (x:420, y:240, 160x72)" in dispatch_text


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
    assert event["message_id"] is None
    assert event["message"] is None


@pytest.mark.parametrize(
    "event_type",
    [
        "human.annotation.updated",
        "human.annotation.dismissed",
    ],
)
def test_show_event_store_does_not_duplicate_forward_annotation_lifecycle(
    isolated_state,
    event_type,
):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": event_type,
                "annotation": {
                    "id": "annotation_1",
                    "comment": "Existing annotation words.",
                },
            },
        )
    finally:
        store.close()

    assert event["transcript_text"]
    assert event["message_id"] is None
    assert event["message"] is None


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
            reserve_dispatch=True,
        )
    finally:
        store.close()

    assert event["payload"]["dispatch"] is True
    assert "[show-intent] choose" in event["transcript_text"]
    assert "Pick B." in event["transcript_text"]
    assert event["message"]["author"] == "harness"
    assert event["message"]["source"] == "harness"
    assert event["message"]["author_name"] == "show_intent"
    assert event["message"]["author_id"] == event["id"]


def test_show_trigger_kind_is_closed_over_dispatching_event_types():
    assert SHOW_TRIGGER_KIND == {
        "human.annotation.created": "show_annotation",
        "human.intent.submitted": "show_intent",
    }
    for event_type in SHOW_TRIGGER_KIND:
        assert show_event_requests_dispatch(
            {
                "type": event_type,
                "actor": "human",
                "payload": {"dispatch": True},
            }
        )


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
    assert event["message"]["type"] == "assistant"
    assert "[show-page-updated] Updated the Show Page" in event["transcript_text"]


def test_show_event_store_records_runtime_error_as_hidden_activity(isolated_state):
    _seed_session()

    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "system.runtime.error",
                "payload": {"error": "Show Runtime failed to render."},
            },
        )
    finally:
        store.close()

    assert event["actor"] == "system"
    assert event["message"]["type"] == "assistant"
    assert event["transcript_text"] == (
        "[show-runtime-error] Show Runtime failed to render."
    )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_header"),
    [
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
    ],
)
def test_show_intent_transcript_headers_only_render_deviations(
    event_type,
    payload,
    expected_header,
):
    transcript = _format_transcript_text(event_type, payload, {})

    assert transcript.splitlines()[0] == expected_header


@pytest.mark.parametrize(
    "target",
    [
        "#root > div.grid > section:nth-child(3) .cta-button",  # compound selector
        "mark-default-summary",  # synthetic mark handle
        "mark_9f2a7c1d",  # synthetic mark handle, underscore form
        "[data-testid='cta']",  # attribute selector
        "button",  # bare type selector -- indistinguishable from a page label
        "summary",  # ditto, and the documented example in `vibe show mark --help`
        "Get started",  # reads like copy, but the field's contract is machine text
        None,  # nothing to say
    ],
)
def test_assistant_mark_transcript_never_shows_the_mark_target(target):
    """The hard invariant, enforced structurally: ``target`` is never printed.

    Every value here is what an agent may legitimately pass to ``vibe show mark``.
    ``button`` and ``summary`` are the reason this is a blanket rule rather than a
    predicate: as strings they are both valid bare type selectors and plausible page
    labels, so no test on the text can separate the safe case from the unsafe one.
    The last case shows the cost we accepted -- copy in the wrong field is dropped
    too, because the field cannot promise it is copy.
    """
    transcript = _format_transcript_text("assistant.mark.created", {"target": target, "body": "Aligned it."}, {})

    assert transcript == "Aligned it."


@pytest.mark.parametrize(
    "scope",
    [
        "#hero",  # a selector used as a filing key
        "mark_9f2a7c1d",  # a synthetic id used as a filing key
        "summary",  # indistinguishable from a page label, exactly like `target`
        "review",  # reads perfectly well -- and still names nothing on the page
    ],
)
def test_assistant_mark_transcript_never_shows_the_mark_scope(scope):
    """``scope`` is dropped for a reason ``target`` did not even need.

    It is unvalidated free text that namespaces the synthetic mark id, so it is
    machine text by contract. But it fails a second test too: it is rendered
    nowhere on the page, so even the well-behaved ``review`` names nothing the
    reader could go and look at. A locator has to point at something visible.

    The same value stays visible in the *agent*-facing direction -- see the
    ``[show-intent scope=review]`` rows in the shared header table -- because
    there the reader can act on it.
    """
    payload = {"scope": scope, "target": "cta", "body": "Aligned it."}

    transcript = _format_transcript_text("assistant.mark.created", payload, {})

    assert transcript == "Aligned it."


def test_no_machine_field_of_a_mark_ever_reaches_the_chat():
    """Closes the class instead of patching one member of it.

    Two rounds of review found the same leak in two different fields. Rather than
    wait for a third, this drives every field a stored mark is made of, each
    holding text a user must never be shown, and asserts the chat message is the
    header plus the agent's own words. The equality check makes the enumeration
    binding: adding a key to ``MARK_PAYLOAD_KEYS`` fails here until someone
    classifies it as the agent's words or as machine text.
    """
    machine_text = {
        "id": "mark_9f2a7c1d",
        "scope": "#hero",
        "target": "#root > div.grid > section:nth-child(3) .cta-button",
        "createdAt": "2026-07-26T10:00:00+00:00",
        "updatedAt": "2026-07-26T10:05:00+00:00",
        "replyTo": "mark_0badc0de",
    }
    assert set(machine_text) | {"body"} == set(MARK_PAYLOAD_KEYS)

    transcript = _format_transcript_text(
        "assistant.mark.created", {**machine_text, "body": "Aligned it."}, {}
    )

    assert transcript == "Aligned it."
    for key, value in machine_text.items():
        assert value not in transcript, f"{key} leaked into the chat"


def test_assistant_mark_card_quotes_page_copy_the_user_can_see():
    """The anchor is the only human source, so it is the only thing that locates."""
    transcript = _format_transcript_text(
        "assistant.mark.created",
        {"target": "cta", "body": "Aligned it."},
        {"selector": "#root .cta-button", "text": "Get started"},
    )

    display = _annotation_display(
        "assistant.mark.created",
        {"selector": "#root .cta-button", "text": "Get started"},
    )
    assert transcript == "Aligned it."
    assert display["quote"] == "Get started"
    assert "#root .cta-button" not in transcript


def test_assistant_mark_title_is_not_localized_at_write_time():
    transcript = _format_transcript_text(
        "assistant.mark.updated",
        {"target": "cta", "body": "改了按钮的对齐方式，和设计稿一致了"},
        {"text": "开始使用"},
    )

    assert transcript == "改了按钮的对齐方式，和设计稿一致了"
    assert _annotation_display(
        "assistant.mark.updated",
        {"text": "开始使用"},
    ) == {
        "direction": "agent",
        "action": "updated",
        "quote": "开始使用",
    }


def test_assistant_mark_transcript_keeps_the_whole_body():
    body = "Detail. " * 200
    transcript = _format_transcript_text(
        "assistant.mark.created",
        {"target": "cta", "body": body},
        {"text": "Get started"},
    )

    assert transcript.endswith(body.strip())


def test_assistant_mark_card_quotes_a_text_range_anchor():
    """`vibe show reply` copies the annotation anchor, which carries ``textQuote``."""
    transcript = _format_transcript_text(
        "assistant.mark.created",
        {"target": "#revenue-card", "body": "Q3 restated the figure."},
        {"kind": "text-range", "selector": "#revenue-card", "textQuote": "Revenue"},
    )

    display = _annotation_display(
        "assistant.mark.created",
        {"kind": "text-range", "selector": "#revenue-card", "textQuote": "Revenue"},
    )
    assert transcript == "Q3 restated the figure."
    assert display["quote"] == "Revenue"
    assert "#revenue-card" not in transcript


@pytest.mark.parametrize("copy_key", ANCHOR_HUMAN_COPY_KEYS)
def test_both_annotation_directions_read_the_same_anchor_copy_fields(copy_key):
    """The user-facing and agent-facing cards must agree on where copy lives.

    Adding a third anchor copy field to one side and not the other silently drops
    the locator from whichever card forgot it, so pin both here.
    """
    anchor = {"selector": "#revenue-card", copy_key: "Revenue"}
    mark = _annotation_display(
        "assistant.mark.created",
        anchor,
    )
    annotation = _annotation_display(
        "human.annotation.created",
        anchor,
    )

    assert mark["quote"] == annotation["quote"] == "Revenue"


@pytest.mark.parametrize("anchor", [{}, {"text": ""}, {"selector": "#cta"}], ids=["absent", "blank", "selector-only"])
def test_assistant_mark_without_page_copy_is_just_the_agents_words(anchor):
    """No human locator available: say what happened, then get out of the way."""
    transcript = _format_transcript_text("assistant.mark.created", {"target": "cta", "body": "Aligned it."}, anchor)

    assert transcript == "Aligned it."
    assert _annotation_display("assistant.mark.created", anchor) == {
        "direction": "agent",
        "action": "created",
    }


@pytest.mark.parametrize("copy_key", ANCHOR_HUMAN_COPY_KEYS)
def test_assistant_mark_condenses_every_locator_source(copy_key):
    """Whichever copy field fills the locator, the card stays bounded."""
    display = _annotation_display(
        "assistant.mark.created",
        {copy_key: "Get\n started " + "x" * MARK_LOCATOR_MAX_LENGTH},
    )
    quote = display["quote"]
    assert quote.startswith("Get started x")
    assert quote.endswith("…")
    assert len(quote) == MARK_LOCATOR_MAX_LENGTH


def test_every_assistant_mark_event_has_annotation_display_data():
    assert {
        event_type: _annotation_display(event_type, {})
        for event_type in ASSISTANT_MARK_EVENT_TYPES
    } == {
        "assistant.mark.created": {"direction": "agent", "action": "created"},
        "assistant.mark.updated": {"direction": "agent", "action": "updated"},
        "assistant.mark.resolved": {"direction": "agent", "action": "resolved"},
    }


def test_appended_mark_stores_direction_instead_of_localized_title(isolated_state):
    from config.v2_config import (
        AgentsConfig,
        PlatformsConfig,
        RuntimeConfig,
        SlackConfig,
        UiConfig,
        V2Config,
    )

    V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        language="zh",
    ).save()

    _seed_session()
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_mark",
            {
                "type": "assistant.mark.created",
                "mark": {"target": "#hero > .cta", "body": "按钮对齐改好了"},
                "anchor": {"text": "开始使用"},
            },
        )
    finally:
        store.close()

    assert event["transcript_text"] == "按钮对齐改好了"
    assert event["message"]["content"]["annotation"] == {
        "direction": "agent",
        "action": "created",
        "quote": "开始使用",
    }


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


def test_dispatching_show_event_reserves_pending_transcript_row(isolated_state):
    from storage import messages_service

    _seed_session("ses_show")
    store = ShowSessionEventStore()
    try:
        dispatching = store.append(
            "ses_show",
            {
                "type": "human.annotation.created",
                "annotation": {"intent": "comment", "comment": "Queue this.", "dispatch": True},
            },
            reserve_dispatch=True,
        )
        non_dispatching = store.append(
            "ses_show",
            {
                "type": "human.annotation.created",
                "annotation": {"intent": "comment", "comment": "Record only.", "dispatch": False},
            },
        )
    finally:
        store.close()

    assert dispatching["message"]["type"] == messages_service.PENDING_TYPE
    assert dispatching["message"]["author"] == messages_service.HARNESS_TYPE
    assert dispatching["message"]["source"] == messages_service.HARNESS_TYPE
    assert dispatching["message"]["author_name"] == "show_annotation"
    assert dispatching["message"]["author_id"] == dispatching["id"]
    assert non_dispatching["message"]["type"] == messages_service.ANNOTATION_TYPE
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        visible = messages_service.list_session_messages(
            conn,
            session_id="ses_show",
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
        queued = messages_service.list_queued(conn, "ses_show")
    assert [message["text"] for message in visible["messages"]] == [non_dispatching["transcript_text"]]
    assert queued == []


def test_dispatching_show_event_retry_reuses_event_and_transcript_row(isolated_state):
    from storage import messages_service

    _seed_session("ses_show_retry")
    payload = {
        "id": "show_evt_retry_identity",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Deliver exactly once.",
            "dispatch": True,
        },
    }
    store = ShowSessionEventStore()
    try:
        first = store.append("ses_show_retry", payload, reserve_dispatch=True)
        replay = store.append("ses_show_retry", payload, reserve_dispatch=True)
    finally:
        store.close()

    assert replay["id"] == first["id"]
    assert replay["message_id"] == first["message_id"]
    assert replay["message"]["type"] == messages_service.PENDING_TYPE
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        event_rows = conn.execute(
            select(show_session_events).where(
                show_session_events.c.id == "show_evt_retry_identity"
            )
        ).mappings().all()
        message_rows = conn.execute(
            select(messages).where(
                messages.c.native_message_id == "show:show_evt_retry_identity"
            )
        ).mappings().all()
    assert len(event_rows) == 1
    assert len(message_rows) == 1


def test_show_event_store_rejects_reused_id_with_different_contents(isolated_state):
    _seed_session("ses_show_conflict")
    first_payload = {
        "id": "show_evt_bound_identity",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Deliver the original annotation.",
            "dispatch": True,
        },
    }
    conflicting_payload = {
        **first_payload,
        "annotation": {
            **first_payload["annotation"],
            "comment": "Silently replace it with different work.",
        },
    }

    store = ShowSessionEventStore()
    try:
        first = store.append("ses_show_conflict", first_payload, reserve_dispatch=True)
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_show_conflict",
                conflicting_payload,
                reserve_dispatch=True,
            )
        stored = store.get_event("ses_show_conflict", first["id"])
    finally:
        store.close()

    assert exc_info.value.code == "event_id_conflict"
    assert stored is not None
    assert stored["payload"]["comment"] == "Deliver the original annotation."


def test_show_event_store_fingerprint_includes_sibling_anchor(isolated_state):
    _seed_session("ses_show_anchor_conflict")
    original = {
        "id": "show_evt_anchor_identity",
        "type": "human.annotation.created",
        "anchor": {"selector": "#summary"},
        "annotation": {
            "intent": "comment",
            "comment": "Review this target.",
            "dispatch": True,
        },
    }
    store = ShowSessionEventStore()
    try:
        store.append("ses_show_anchor_conflict", original, reserve_dispatch=True)
        with pytest.raises(ShowSessionEventError) as exc_info:
            store.append(
                "ses_show_anchor_conflict",
                {**original, "anchor": {"selector": "#other"}},
                reserve_dispatch=True,
            )
    finally:
        store.close()

    assert exc_info.value.code == "event_id_conflict"


def test_localized_show_event_errors_follow_configured_language(
    monkeypatch,
):
    class _Config:
        language = "zh"

    monkeypatch.setattr(
        "core.show_session_events.V2Config.load",
        lambda: _Config(),
    )

    conflict = localized_show_event_error("event_id_conflict")
    pending = localized_show_event_error("show_event_dispatch_pending")

    assert conflict.code == "event_id_conflict"
    assert str(conflict) == "此 Show 事件 ID 已绑定到不同的事件内容。"
    assert pending.code == "show_event_dispatch_pending"
    assert str(pending) == "Show 事件可能仍在处理中，未在本地重复提交。"


def test_direct_dispatching_show_event_is_visible_annotation_input(isolated_state):
    from storage import messages_service

    _seed_session("ses_show")
    store = ShowSessionEventStore()
    try:
        event = store.append(
            "ses_show",
            {
                "type": "human.annotation.created",
                "annotation": {
                    "intent": "comment",
                    "comment": "Record this visibly.",
                    "dispatch": True,
                },
            },
        )
        events = store.list("ses_show")
    finally:
        store.close()

    assert event["message"]["type"] == messages_service.ANNOTATION_TYPE
    assert event["message"]["author"] == messages_service.HARNESS_TYPE
    assert event["message"]["source"] == messages_service.HARNESS_TYPE
    assert [item["id"] for item in events["events"]] == [event["id"]]
    with create_sqlite_engine().connect() as conn:
        assert messages_service.list_queued(conn, "ses_show") == []


def test_startup_repairs_stranded_pending_rows_by_origin(isolated_state):
    from storage import messages_service
    from vibe import ui_server

    scope_id = _seed_session("ses_show_restart")
    store = ShowSessionEventStore()
    try:
        show_event = store.append(
            "ses_show_restart",
            {
                "id": "show_evt_restart_reconcile",
                "type": "human.annotation.created",
                "annotation": {
                    "comment": "Recover my harness prompt after restart.",
                    "dispatch": True,
                },
            },
            reserve_dispatch=True,
        )
    finally:
        store.close()

    with create_sqlite_engine().begin() as conn:
        chat_message = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id="ses_show_restart",
            platform="avibe",
            author="user",
            source="user",
            message_type=messages_service.PENDING_TYPE,
            text="Recover my chat prompt after restart.",
        )

    # Ordinary Chat becomes visible. Harness-owned Show input stays retryable
    # because startup has no queue drain that owns it.
    summary = ui_server._recover_stale_pending_messages()
    assert summary == {"promoted": 1, "deleted": 0, "skipped": 1}

    with create_sqlite_engine().connect() as conn:
        repaired = {
            row["id"]: row
            for row in messages_service.list_session_messages(
                conn,
                session_id="ses_show_restart",
                limit=50,
                types=messages_service.TRANSCRIPT_TYPES,
                tail=True,
            )["messages"]
        }
        queued = messages_service.list_queued(conn, "ses_show_restart")
    assert repaired[chat_message["id"]]["type"] == "user"
    assert repaired[chat_message["id"]]["author"] == "user"
    assert show_event["message_id"] not in repaired
    assert queued == []

    store = ShowSessionEventStore()
    try:
        retryable = store.get_event("ses_show_restart", show_event["id"])
    finally:
        store.close()
    assert retryable is not None
    assert retryable["message"]["type"] == messages_service.PENDING_TYPE
    assert retryable["message"]["metadata"][
        messages_service.QUEUED_DISPATCH_TEXT_KEY
    ].startswith("[show-annotation] comment")


def test_record_local_show_event_uses_synchronous_unified_entry(
    isolated_state,
    monkeypatch,
):
    from storage import messages_service
    from vibe import internal_client, ui_server
    from vibe.sse_broker import broker

    _seed_session("ses_show")
    dispatches = []
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(broker, "publish", lambda event, data: published.append((event, data)))

    async def fake_dispatch_async(payload, **_kwargs):
        dispatches.append(payload)
        return {"status_code": 202, "body": {"ok": True}}

    monkeypatch.setattr(internal_client, "dispatch_async", fake_dispatch_async)
    event = ui_server.record_local_show_event(
        "ses_show",
        {
            "type": "human.annotation.created",
            "annotation": {
                "intent": "comment",
                "comment": "Deliver this.",
                "dispatch": True,
            },
        },
    )

    assert dispatches[0]["user_message_id"] == event["message_id"]
    assert dispatches[0]["text"].startswith(
        "[show-annotation] comment\n\nDeliver this."
    )
    assert f"Show event id: {event['id']}" in dispatches[0]["text"]
    assert (
        messages_service.QUEUED_DISPATCH_TEXT_KEY
        not in event["message"]["metadata"]
    )
    assert "dispatch_owner" not in dispatches[0]
    assert event["message"]["type"] == "annotation"
    assert [name for name, _data in published] == [
        "show.event",
        "message.new",
        "session.activity",
    ]
    assert published[1][1]["id"] == event["message_id"]


def test_failed_local_show_dispatch_keeps_row_pending_so_replay_retries(
    isolated_state,
    monkeypatch,
):
    from storage import messages_service
    from vibe import internal_client, ui_server

    _seed_session("ses_show")
    attempts = 0

    async def fake_dispatch_async(_payload, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise internal_client.InternalServerUnavailable("controller unavailable")
        return {"status_code": 202, "body": {"ok": True}}

    monkeypatch.setattr(internal_client, "dispatch_async", fake_dispatch_async)
    payload = {
        "id": "show_evt_failed_once",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Record this failure once.",
            "dispatch": True,
        },
    }
    with pytest.raises(ShowSessionEventError) as exc_info:
        ui_server.record_local_show_event("ses_show", payload)
    assert exc_info.value.code == "show_event_dispatch_failed"

    with create_sqlite_engine().connect() as conn:
        stranded = messages_service.list_session_messages(
            conn,
            session_id="ses_show",
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
    assert stranded["messages"] == []

    replay = ui_server.record_local_show_event("ses_show", payload)

    assert attempts == 2
    assert replay["message"]["type"] == messages_service.ANNOTATION_TYPE
    with create_sqlite_engine().connect() as conn:
        visible = messages_service.list_session_messages(
            conn,
            session_id="ses_show",
            limit=50,
            types=messages_service.TRANSCRIPT_TYPES,
            tail=True,
        )
    assert [row["id"] for row in visible["messages"]] == [replay["message_id"]]


def test_ambiguous_local_show_dispatch_replays_under_the_same_reservation(
    isolated_state,
    monkeypatch,
):
    from storage import messages_service
    from vibe import internal_client, ui_server

    _seed_session("ses_show")
    seen_message_ids: list[str] = []

    async def fake_dispatch_async(dispatch_payload, **_kwargs):
        seen_message_ids.append(dispatch_payload["user_message_id"])
        if len(seen_message_ids) == 1:
            raise internal_client.InternalServerTimeout("acceptance unknown")
        return {"status_code": 202, "body": {"ok": True, "duplicate": True}}

    monkeypatch.setattr(internal_client, "dispatch_async", fake_dispatch_async)
    payload = {
        "id": "show_evt_ambiguous_timeout",
        "type": "human.annotation.created",
        "annotation": {
            "intent": "comment",
            "comment": "Do not submit this twice.",
            "dispatch": True,
        },
    }

    with pytest.raises(ShowSessionEventError) as exc_info:
        ui_server.record_local_show_event("ses_show", payload)
    assert exc_info.value.code == "show_event_dispatch_pending"

    replay = ui_server.record_local_show_event("ses_show", payload)

    assert len(seen_message_ids) == 2
    assert seen_message_ids[0] == seen_message_ids[1]
    assert replay["message"]["type"] == messages_service.ANNOTATION_TYPE


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
def test_annotation_dispatch_text_adds_event_id_and_optional_reply_guidance(intent, expects_guidance):
    label = intent or "comment"
    payload = {
        "comment": "Review this.",
        **({"intent": intent} if intent is not None else {}),
    }
    dispatch_text = _format_dispatch_text(
        "human.annotation.created",
        payload,
        {},
        event_id="show_evt_1a2b3c4d",
    )

    assert dispatch_text.startswith(
        f"[show-annotation] {label}\n\nReview this."
    )
    assert "Show event id: show_evt_1a2b3c4d" in dispatch_text
    guidance = (
        "如需在页面上原位回应，可执行：\n"
        "  vibe show reply show_evt_1a2b3c4d --message '<你的回答>'\n"
        "（也可以直接修改页面内容来响应，按场景选择。）"
    )
    assert (guidance in dispatch_text) is expects_guidance
    if expects_guidance:
        assert dispatch_text.endswith(guidance)


def test_annotation_builders_match_frozen_examples():
    from storage import messages_service

    # This docs fixture is the single authoritative row copy shared by both lanes.
    fixture_path = (
        Path(__file__).parents[1]
        / "docs/plans/show-annotation-message-type/examples.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    frozen_fields = fixture["_frozen_fields"]
    frozen_rows = [
        example["row"]
        for example in fixture["examples"]
    ]

    forward = frozen_rows[0]
    forward_anchor = {
        "selector": forward["metadata"]["anchor_selector"],
        "text": forward["content"]["annotation"]["quote"],
    }
    forward_payload = {
        "scope": forward["metadata"]["show_event_scope"],
        "intent": "comment",
        "comment": forward["text"],
        "primaryAnchor": forward["metadata"]["anchor_kind"],
    }

    queued = frozen_rows[1]
    queued_attachment = queued["content"]["attachments"][0]
    queued_dispatch = queued["metadata"][messages_service.QUEUED_DISPATCH_TEXT_KEY]
    screenshot_line = next(
        line for line in queued_dispatch.splitlines() if line.startswith("Screenshot: ")
    )
    assert screenshot_line == (
        "Screenshot: state/media/med_9a71c33f8b2e.png (1240x620)"
    )
    origin_x, origin_y, dimensions = queued["metadata"]["screenshot_region"].split(
        ", "
    )
    width, height = dimensions.split("x", 1)
    screenshot_region = {
        "x": int(origin_x.removeprefix("x:")),
        "y": int(origin_y.removeprefix("y:")),
        "width": int(width),
        "height": int(height),
    }
    queued_payload = {
        "scope": queued["metadata"]["show_event_scope"],
        "intent": "comment",
        "comment": queued["text"],
        "primaryAnchor": queued["metadata"]["anchor_kind"],
        "screenshot": {
            "attachmentId": queued_attachment["url"].removeprefix("/api/media/"),
            "path": "state/media/med_9a71c33f8b2e.png",
            "mimeType": queued_attachment["mime"],
            "width": queued_attachment["width"],
            "height": queued_attachment["height"],
            "region": screenshot_region,
        },
    }

    produced_rows = [
        {
            "type": messages_service.ANNOTATION_TYPE,
            "author": messages_service.HARNESS_TYPE,
            "content": {
                "text": _format_transcript_text(
                    forward["metadata"]["show_event_type"],
                    forward_payload,
                    forward_anchor,
                ),
                "annotation": _annotation_display(
                    forward["metadata"]["show_event_type"],
                    forward_anchor,
                ),
            },
            "metadata": {
                messages_service.QUEUED_DISPATCH_TEXT_KEY: _format_dispatch_text(
                    forward["metadata"]["show_event_type"],
                    forward_payload,
                    forward_anchor,
                    event_id=forward["metadata"]["show_event_id"],
                )
            },
        },
        {
            "type": messages_service.QUEUED_TYPE,
            "author": messages_service.HARNESS_TYPE,
            "content": {
                "text": _format_transcript_text(
                    queued["metadata"]["show_event_type"],
                    queued_payload,
                    {},
                ),
                "annotation": _annotation_display(
                    queued["metadata"]["show_event_type"],
                    {},
                ),
                "attachments": _annotation_attachments(queued_payload),
            },
            "metadata": {
                messages_service.QUEUED_DISPATCH_TEXT_KEY: _format_dispatch_text(
                    queued["metadata"]["show_event_type"],
                    queued_payload,
                    {},
                    event_id=queued["metadata"]["show_event_id"],
                )
            },
        },
    ]

    for frozen in frozen_rows[2:]:
        event_type = frozen["metadata"]["show_event_type"]
        quote = frozen["content"]["annotation"].get("quote")
        anchor = {"text": quote} if quote else {}
        produced_rows.append(
            {
                "type": messages_service.ANNOTATION_TYPE,
                "author": "agent",
                "content": {
                    "text": _format_transcript_text(
                        event_type,
                        {"body": frozen["text"]},
                        anchor,
                    ),
                    "annotation": _annotation_display(event_type, anchor),
                },
                "metadata": {},
            }
        )

    def frozen_value(row, path):
        current = row
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    assert frozen_fields == [
        "type",
        "author",
        "content.text",
        "content.annotation",
        "content.attachments",
        "metadata._queued_dispatch_text",
    ]
    for produced, frozen in zip(produced_rows, frozen_rows, strict=True):
        for field in frozen_fields:
            produced_present, produced_value = frozen_value(produced, field)
            frozen_present, frozen_value_at_path = frozen_value(frozen, field)
            assert produced_present is frozen_present, field
            if field == "metadata._queued_dispatch_text":
                # The prompt body is machine-only; its presence is the contract.
                continue
            assert produced_value == frozen_value_at_path, field

    machine_markers = (
        "[show-annotation]",
        "Anchor kind:",
        "Quote:",
        "Anchor:",
        "Screenshot:",
        "Screenshot region:",
        "Show event id:",
        "vibe show reply",
    )
    for produced in produced_rows[:2]:
        display_text = produced["content"]["text"]
        dispatch_text = produced["metadata"][
            messages_service.QUEUED_DISPATCH_TEXT_KEY
        ]
        assert display_text in dispatch_text
        assert all(marker not in display_text for marker in machine_markers)
