"""Bounded storage inputs for the canonical tool activity projection.

No trace is removed here. Reader-first writers link their compatibility trace to
its marked response, while this service reads the canonical row/result instead.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from sqlalchemy import literal, select, union_all

from core.tool_activity import PROJECTION_KEY, capability, linked_step, object_value, project_response
from storage.agent_transcript import RESPONSE_TYPES
from storage.models import agent_events, media_objects, messages, session_turns


# Keep each compound SELECT and its bindings below SQLite's portable limits.
_READ_BATCH_SIZE = 64


def _response_results(conn, rows: list[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """Batch bounded seeks, never join against a Session's entire result history.

    The next response may be unmarked, opaque, or outside the retained window.
    Each result interval still has its own LIMIT (the number of requested calls),
    including the final open interval. UNION ALL combines those independent seeks
    without a window-function sort or a scan over unrelated historical results.
    """
    counts = {}
    for row in rows:
        message = object_value(object_value(row.get("content_json")).get("model")).get("message")
        blocks = object_value(message).get("content")
        count = sum(isinstance(block, dict) and block.get("type") == "tool_call" for block in blocks) if isinstance(blocks, list) else 0
        if count:
            counts[row["id"]] = count
    wanted = [row for row in rows if row["id"] in counts]
    results: dict[str, list[dict]] = {row["id"]: [] for row in rows}
    next_response = messages.alias("next_response")
    following = select(next_response.c.context_seq).where(
        next_response.c.session_id == messages.c.session_id,
        next_response.c.context_seq > messages.c.context_seq,
        next_response.c.author == "agent", next_response.c.type.in_(RESPONSE_TYPES),
    ).order_by(next_response.c.context_seq).limit(1).correlate(messages).scalar_subquery()
    for start in range(0, len(wanted), _READ_BATCH_SIZE):
        batch = wanted[start:start + _READ_BATCH_SIZE]
        boundaries = dict(conn.execute(select(messages.c.id, following).where(
            messages.c.id.in_([row["id"] for row in batch]),
        )).all())
        queries = []
        for row in batch:
            if row["id"] not in boundaries:
                # A concurrent delete may retire a response after the window was
                # read. Missing is not an open interval: never borrow a retry's
                # result when that response can no longer bound its own calls.
                continue
            query = select(
                literal(row["id"]).label("response_id"), agent_events.c.id,
                agent_events.c.content_json, agent_events.c.metadata_json,
                agent_events.c.created_at, agent_events.c.context_seq,
            ).where(
                agent_events.c.session_id == row["session_id"],
                agent_events.c.context_seq > row["context_seq"],
                agent_events.c.event_type == "tool_result", agent_events.c.visibility == "context",
            )
            end = boundaries.get(row["id"])
            if end is not None:
                query = query.where(agent_events.c.context_seq < end)
            queries.append(select(query.order_by(agent_events.c.context_seq).limit(counts[row["id"]]).subquery()))
        if not queries:
            continue
        for result in conn.execute(union_all(*queries)).mappings():
            value = dict(result)
            results[value.pop("response_id")].append(value)
    for values in results.values():
        values.sort(key=lambda value: value["context_seq"])
    return results


def response_results(conn, row: Mapping[str, Any]) -> list[dict]:
    """The call batch's result interval, not the first matching id in all history."""
    return _response_results(conn, [row])[row["id"]]


def _media_urls(event: dict, media_by_token: Mapping[str, Any]) -> dict:
    """Use the canonical registration; do not mint capabilities during a read."""
    card = event["content"].get("hosted")
    if event.get("platform") != "avibe" or not card or not card.get("images"):
        return event
    images = []
    for image in card["images"]:
        token = event.get("_media_tokens", {}).get(image.get("path"))
        media = media_by_token.get(token) if isinstance(token, str) else None
        if (media is None or media["session_id"] != event["session_id"]
                or media["scope_id"] != event.get("scope_id") or media["revoked_at"] is not None):
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
    # These caches belong to this projection only: no connection/global cache can
    # safely retain a pending result, a Turn state, or a revoked media capability.
    for row in rows:
        row["content_json"] = object_value(row["content_json"])
        row["metadata_json"] = object_value(row["metadata_json"])
    turn_ids = {row["metadata_json"].get("turn_id") for row in rows}
    turns = {row["id"]: dict(row) for row in conn.execute(select(session_turns).where(
        session_turns.c.id.in_([value for value in turn_ids if isinstance(value, str)]),
        session_turns.c.session_id == session_id,
    )).mappings()}
    latest = {}
    for row in rows:
        turn_id = object_value(row["metadata_json"]).get("turn_id")
        if turn_id and object_value(object_value(object_value(row["content_json"]).get("model")).get("message")).get("role") == "assistant":
            latest[turn_id] = max(latest.get(turn_id, -1), row["context_seq"])
    capable = [row for row in rows if capability(row["metadata_json"]) is not None]
    results = _response_results(conn, capable)
    formatters = {}
    result = []
    for row in capable:
        metadata = object_value(row["metadata_json"])
        cap = capability(metadata)
        if cap is None:
            continue
        turn_id = metadata.get("turn_id")
        if turn_id and latest.get(turn_id, -1) > row["context_seq"]:
            row["metadata_json"] = {**metadata, PROJECTION_KEY: {**cap, "retried": True}}
        result.extend(project_response(row, results[row["id"]], turns.get(turn_id), formatters=formatters))
    tokens = sorted({
        token for event in result if event.get("platform") == "avibe"
        for image in (event["content"].get("hosted") or {}).get("images", [])
        if isinstance(token := event.get("_media_tokens", {}).get(image.get("path")), str) and token
    })
    media_by_token = {}
    for start in range(0, len(tokens), _READ_BATCH_SIZE):
        media_by_token.update({row["token"]: row for row in conn.execute(select(media_objects).where(
            media_objects.c.token.in_(tokens[start:start + _READ_BATCH_SIZE]),
            media_objects.c.session_id == session_id, media_objects.c.revoked_at.is_(None),
        )).mappings()})
    return [_media_urls(event, media_by_token) for event in result]


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
