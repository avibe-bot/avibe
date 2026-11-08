"""Bounded storage inputs for the canonical tool activity projection.

No trace is removed here. Reader-first writers link their compatibility trace to
its marked response, while this service reads the canonical row/result instead.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from core.tool_activity import PROJECTION_KEY, capability, linked_step, object_value, project_response
from storage.agent_transcript import RESPONSE_TYPES
from storage.models import agent_events, media_objects, messages, session_turns


def response_results(conn, row: Mapping[str, Any]) -> list[dict]:
    """The call batch's result interval, not the first matching id in all history."""
    message = object_value(object_value(row.get("content_json")).get("model")).get("message")
    blocks = object_value(message).get("content")
    count = sum(isinstance(block, dict) and block.get("type") == "tool_call" for block in blocks) if isinstance(blocks, list) else 0
    if not count:
        return []
    following = conn.execute(select(messages.c.context_seq).where(
        messages.c.session_id == row["session_id"], messages.c.context_seq > row["context_seq"],
        messages.c.author == "agent", messages.c.type.in_(RESPONSE_TYPES),
    ).order_by(messages.c.context_seq).limit(1)).scalar()
    query = select(agent_events.c.id, agent_events.c.content_json, agent_events.c.metadata_json, agent_events.c.created_at, agent_events.c.context_seq).where(
        agent_events.c.session_id == row["session_id"], agent_events.c.context_seq > row["context_seq"],
        agent_events.c.event_type == "tool_result", agent_events.c.visibility == "context",
    )
    if following is not None:
        query = query.where(agent_events.c.context_seq < following)
    return [dict(result) for result in conn.execute(query.order_by(agent_events.c.context_seq).limit(count)).mappings()]


def _media_urls(conn, event: dict) -> dict:
    """Use the canonical registration; do not mint capabilities during a read."""
    card = event["content"].get("hosted")
    if event.get("platform") != "avibe" or not card or not card.get("images"):
        return event
    images = []
    for image in card["images"]:
        token = event.get("_media_tokens", {}).get(image.get("path"))
        media = conn.execute(select(media_objects).where(
            media_objects.c.token == token, media_objects.c.session_id == event["session_id"],
            media_objects.c.scope_id == event.get("scope_id"), media_objects.c.revoked_at.is_(None),
        )).mappings().first() if token else None
        if media is None:
            images.append(image)
            continue
        url = f"/api/media/{media['token']}"
        if media["width_px"] and media["height_px"]:
            url += f"?w={media['width_px']}&h={media['height_px']}"
        images.append({**image, "media_url": url, **({"bytes": media["size_bytes"]} if media["size_bytes"] is not None else {})})
    return {**event, "content": {**event["content"], "hosted": {**card, "images": images}}}


def response_steps(conn, *, session_id: str, response_ids: Iterable[str]) -> list[dict]:
    """Project exactly the caller's retained message window (one response live)."""
    ids = tuple(response_ids)
    if not ids:
        return []
    rows = [dict(row) for row in conn.execute(select(messages).where(
        messages.c.session_id == session_id, messages.c.id.in_(ids), messages.c.context_seq.is_not(None),
    )).mappings()]
    turn_ids = {object_value(row["metadata_json"]).get("turn_id") for row in rows}
    turns = {row["id"]: dict(row) for row in conn.execute(select(session_turns).where(
        session_turns.c.id.in_([value for value in turn_ids if isinstance(value, str)]),
        session_turns.c.session_id == session_id,
    )).mappings()}
    latest = {}
    for row in rows:
        turn_id = object_value(row["metadata_json"]).get("turn_id")
        if turn_id and object_value(object_value(object_value(row["content_json"]).get("model")).get("message")).get("role") == "assistant":
            latest[turn_id] = max(latest.get(turn_id, -1), row["context_seq"])
    result = []
    for row in rows:
        metadata = object_value(row["metadata_json"])
        cap = capability(metadata)
        if cap is None:
            continue
        turn_id = metadata.get("turn_id")
        if turn_id and latest.get(turn_id, -1) > row["context_seq"]:
            row["metadata_json"] = {**metadata, PROJECTION_KEY: {**cap, "retried": True}}
        result.extend(_media_urls(conn, event) for event in project_response(row, response_results(conn, row), turns.get(turn_id)))
    return result


def merge_steps(events: list[dict], projected: list[dict]) -> list[dict]:
    """Deduplicate only a verified call instance; never match tool names/arguments."""
    ids = {event["id"] for event in projected}
    return [event for event in events if linked_step(event.get("metadata")) not in ids] + projected


def record_display_facts(conn, *, session_id: str, response_id: str, call_id: str | None = None, **facts: Any) -> bool:
    """Compact execution/display evidence on the response; no copied text or args.

    This runs under the caller's write transaction. Only the small metadata column
    is read/updated, not the immutable canonical response. Unsupported rows retain
    the compatibility trace path.
    """
    raw = conn.execute(select(messages.c.metadata_json).where(
        messages.c.id == response_id, messages.c.session_id == session_id, messages.c.context_seq.is_not(None),
    )).scalar()
    metadata = object_value(raw)
    cap = capability(metadata)
    if cap is None:
        return False
    if call_id is None:
        cap = {**cap, **facts}
    else:
        calls = object_value(cap.get("calls"))
        cap = {**cap, "calls": {**calls, call_id: {**object_value(calls.get(call_id)), **facts}}}
    conn.execute(messages.update().where(messages.c.id == response_id, messages.c.session_id == session_id).values(
        metadata_json=json.dumps({**metadata, PROJECTION_KEY: cap}, ensure_ascii=False, separators=(",", ":")),
    ))
    return True
