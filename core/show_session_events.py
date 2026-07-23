from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from config import paths
from core.services import sessions as workbench_sessions_service
from storage import messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.models import agent_sessions, show_session_events

DEFAULT_MARK_SCOPE = "default"
HUMAN_EVENT_TYPES = {
    "human.intent.submitted",
    "human.annotation.created",
    "human.annotation.updated",
    "human.annotation.resolved",
    "human.annotation.dismissed",
}
SUPPORTED_EVENT_TYPES = {
    "assistant.mark.created",
    "assistant.mark.updated",
    "assistant.mark.resolved",
    "assistant.page.updated",
    "system.runtime.status",
    "system.runtime.error",
    "system.annotation.control",
    *HUMAN_EVENT_TYPES,
}
ANNOTATION_EVENT_TYPES = {
    "human.annotation.created",
    "human.annotation.updated",
    "human.annotation.resolved",
    "human.annotation.dismissed",
}
ANNOTATION_PRIMARY_ANCHORS = {
    "mark",
    "element",
    "text-range",
    "element-group",
    "area",
    "screenshot",
}
ASSISTANT_MARK_EVENT_TYPES = {
    "assistant.mark.created",
    "assistant.mark.updated",
    "assistant.mark.resolved",
}


class ShowSessionEventError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ShowSessionEventStore:
    db_path: Path | None = None

    def __post_init__(self) -> None:
        if self.db_path is None:
            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        else:
            from storage.migrations import run_migrations

            run_migrations(self.db_path)
        object.__setattr__(self, "engine", create_sqlite_engine(self.db_path))

    def close(self) -> None:
        self.engine.dispose()

    def append(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        author: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        validate_show_event_payload_session(session_id, payload)
        event_type = _validate_event_type(payload.get("type"))
        actor = _actor_for_event(event_type)
        records_author = actor == "human" or (event_type == "assistant.mark.resolved" and author is not None)
        event_id = _event_id(payload, {})
        created_at = _utc_now_iso()

        with self.engine.begin() as conn:
            session = conn.execute(
                select(agent_sessions.c.id, agent_sessions.c.scope_id, agent_sessions.c.status)
                .where(agent_sessions.c.id == session_id)
                .limit(1)
            ).mappings().first()
            if session is None:
                raise ShowSessionEventError("Agent session not found.", code="session_not_found")
            # Archive is terminal: a still-open Show Page must not keep writing
            # events (which dispatch as new agent work) into an archived session.
            if session["status"] == "archived":
                raise ShowSessionEventError("Agent session is archived.", code="session_archived")

            stored_payload = payload
            if event_type == "assistant.mark.resolved":
                stored_payload = _prepare_mark_resolution(conn, session_id, payload)
            event_payload = _normalize_event_payload(event_type, stored_payload)
            if records_author:
                event_payload.pop("author", None)
            anchor = _event_anchor(event_type, stored_payload, event_payload)
            scope = _event_scope(event_type, event_payload)
            transcript_text = (
                ""
                if event_type == "assistant.mark.resolved" and author is not None
                else _format_transcript_text(event_type, event_payload, anchor)
            )
            if records_author:
                event_payload["author"] = _normalize_human_author(author)

            conn.execute(
                show_session_events.insert().values(
                    id=event_id,
                    session_id=session_id,
                    event_type=event_type,
                    actor=actor,
                    scope=scope,
                    anchor_json=_json_dumps(anchor),
                    payload_json=_json_dumps(event_payload),
                    transcript_text=transcript_text,
                    message_id=None,
                    created_at=created_at,
                )
            )
            message: dict[str, Any] | None = None
            message_id: str | None = None
            if transcript_text:
                message = messages_service.append(
                    conn,
                    scope_id=session["scope_id"],
                    session_id=session_id,
                    platform="avibe",
                    author="agent" if actor in {"assistant", "system"} else "user",
                    text=transcript_text,
                    content={"text": transcript_text, "show_event_type": event_type},
                    metadata={
                        "source": "show_page",
                        "show_event_id": event_id,
                        "show_event_type": event_type,
                        "show_event_scope": scope,
                        **({"author": event_payload["author"]} if "author" in event_payload else {}),
                    },
                    native_message_id=f"show:{event_id}",
                )
                message_id = message["id"]
                conn.execute(
                    update(show_session_events).where(show_session_events.c.id == event_id).values(message_id=message_id)
                )
                workbench_sessions_service.touch_session(conn, session_id)

        event = {
            "id": event_id,
            "session_id": session_id,
            "scope_id": session["scope_id"],
            "type": event_type,
            "actor": actor,
            "scope": scope,
            "anchor": anchor,
            "payload": event_payload,
            "transcript_text": transcript_text,
            "message_id": message_id,
            "message": message,
            "created_at": created_at,
        }
        return event

    def list(self, session_id: str, *, after_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        effective_limit = min(max(int(limit), 1), 500)
        with self.engine.connect() as conn:
            query = select(show_session_events).where(show_session_events.c.session_id == session_id)
            if after_id:
                anchor = conn.execute(
                    select(show_session_events.c.created_at).where(show_session_events.c.id == after_id)
                ).scalar_one_or_none()
                if anchor is not None:
                    query = query.where(
                        (show_session_events.c.created_at > anchor)
                        | (
                            (show_session_events.c.created_at == anchor)
                            & (show_session_events.c.id > after_id)
                        )
                    )
            query = query.order_by(show_session_events.c.created_at.asc(), show_session_events.c.id.asc()).limit(
                effective_limit
            )
            rows = [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
        return {
            "events": rows,
            "next_after_id": rows[-1]["id"] if len(rows) == effective_limit else None,
        }

    def get_annotation_event(self, session_id: str, event_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(show_session_events)
                .where(
                    show_session_events.c.session_id == session_id,
                    show_session_events.c.id == event_id,
                    show_session_events.c.event_type == "human.annotation.created",
                )
                .limit(1)
            ).mappings().first()
        return _row_to_payload(dict(row)) if row is not None else None

    def recent_annotation_event_ids(self, session_id: str, *, limit: int = 10) -> list[str]:
        effective_limit = min(max(int(limit), 1), 50)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(show_session_events.c.id)
                .where(
                    show_session_events.c.session_id == session_id,
                    show_session_events.c.event_type == "human.annotation.created",
                )
                .order_by(show_session_events.c.created_at.desc(), show_session_events.c.id.desc())
                .limit(effective_limit)
            ).all()
        return [str(row[0]) for row in rows]

    def active_marks(self, session_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return _active_marks_from_connection(conn, session_id)


def stable_assistant_mark_id(*, scope: str, target: str | None = None, reply_to: str | None = None) -> str:
    normalized_scope = _text_or_none(scope) or DEFAULT_MARK_SCOPE
    if reply_to:
        identity = f"reply:{reply_to.strip()}"
    elif target:
        identity = f"note:{target.strip()}"
    else:
        raise ValueError("target or reply_to is required")
    digest = hashlib.sha256(f"{normalized_scope}\0{identity}".encode()).hexdigest()[:16]
    return f"mark_{digest}"


def project_active_assistant_marks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    key_by_mark_id: dict[str, tuple[str, str, str]] = {}

    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in ASSISTANT_MARK_EVENT_TYPES:
            continue
        payload = event.get("payload") or event.get("mark")
        if not isinstance(payload, dict):
            continue
        mark_id = _text_or_none(payload.get("id"))
        if not mark_id:
            continue

        if event_type == "assistant.mark.resolved" or payload.get("status") == "resolved":
            key = key_by_mark_id.pop(mark_id, None)
            if key is not None:
                active_by_key.pop(key, None)
            continue

        key = _assistant_mark_replacement_key(payload)
        previous = active_by_key.pop(key, None)
        if previous is not None:
            key_by_mark_id.pop(str(previous["id"]), None)
        mark = {
            **payload,
            "id": mark_id,
            "kind": "reply" if _text_or_none(payload.get("replyTo")) else "note",
            "anchor": dict(event.get("anchor") or {}),
            "event_id": event.get("id"),
            "read_state": "unread",
        }
        mark.pop("author", None)
        active_by_key[key] = mark
        key_by_mark_id[mark_id] = key

    return list(active_by_key.values())


def _active_marks_from_connection(conn: Any, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(show_session_events)
        .where(
            show_session_events.c.session_id == session_id,
            show_session_events.c.event_type.in_(ASSISTANT_MARK_EVENT_TYPES),
        )
        .order_by(show_session_events.c.created_at.asc(), show_session_events.c.id.asc())
    ).mappings().all()
    return project_active_assistant_marks([_row_to_payload(dict(row)) for row in rows])


def _prepare_mark_resolution(conn: Any, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    requested_mark = _normalize_json_object(payload.get("mark") or payload.get("payload"))
    mark_id = _required_text(requested_mark.get("id"), "mark.id")
    active_mark = next(
        (mark for mark in _active_marks_from_connection(conn, session_id) if mark.get("id") == mark_id),
        None,
    )
    if active_mark is None:
        raise ShowSessionEventError("Assistant mark is not active.", code="mark_not_active")

    requested_version = _text_or_none(requested_mark.get("updatedAt"))
    if requested_version is None:
        raise ShowSessionEventError("mark.updatedAt is required.", code="mark_version_required")
    if requested_version != _text_or_none(active_mark.get("updatedAt")):
        raise ShowSessionEventError("Assistant mark has changed.", code="mark_version_conflict")

    mark = {
        key: active_mark[key]
        for key in ("id", "scope", "target", "body", "createdAt", "updatedAt", "replyTo")
        if active_mark.get(key) is not None
    }
    return {
        "type": "assistant.mark.resolved",
        "mark": mark,
        "anchor": dict(active_mark.get("anchor") or {}),
    }


def _assistant_mark_replacement_key(payload: dict[str, Any]) -> tuple[str, str, str]:
    scope = _text_or_none(payload.get("scope")) or DEFAULT_MARK_SCOPE
    reply_to = _text_or_none(payload.get("replyTo"))
    if reply_to:
        return (scope, "reply", reply_to)
    return (scope, "note", _text_or_none(payload.get("target")) or "")


def show_event_payload_session_mismatch(session_id: str, payload: dict[str, Any]) -> str | None:
    for candidate in _show_event_session_id_containers(payload):
        mismatch = _show_event_container_session_mismatch(session_id, candidate)
        if mismatch:
            return mismatch
    return None


def _show_event_session_id_containers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [payload]
    for key in ("payload", "annotation", "mark"):
        wrapped = payload.get(key)
        if isinstance(wrapped, dict):
            candidates.append(wrapped)
    return candidates


def _show_event_container_session_mismatch(session_id: str, payload: dict[str, Any]) -> str | None:
    for key in ("sessionId", "session_id"):
        if key not in payload or payload.get(key) is None:
            continue
        value = str(payload.get(key) or "").strip()
        if value and value != session_id:
            return value
    return None


def validate_show_event_payload_session(session_id: str, payload: dict[str, Any]) -> None:
    if show_event_payload_session_mismatch(session_id, payload):
        raise ShowSessionEventError(
            "Show event sessionId must match the target session.",
            code="session_mismatch",
        )


def _validate_event_type(raw: Any) -> str:
    event_type = str(raw or "").strip()
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ShowSessionEventError(f"Unsupported show event type: {event_type}", code="unsupported_event_type")
    return event_type


def _actor_for_event(event_type: str) -> str:
    if event_type.startswith("assistant."):
        return "assistant"
    if event_type.startswith("system."):
        return "system"
    return "human"


def _normalize_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type.startswith("assistant.mark."):
        mark = _normalize_json_object(payload.get("mark") or payload.get("payload"))
        target = _required_text(mark.get("target"), "mark.target")
        body = _required_text(mark.get("body") or mark.get("comment"), "mark.body")
        created_at = _text_or_none(mark.get("createdAt")) or _utc_now_iso()
        normalized = {
            "id": _text_or_none(mark.get("id")) or _new_id("mark"),
            "role": "assistant",
            "scope": _text_or_none(mark.get("scope")) or DEFAULT_MARK_SCOPE,
            "target": target,
            "body": body,
            "status": "resolved" if event_type == "assistant.mark.resolved" else _text_or_none(mark.get("status")) or "active",
            "createdAt": created_at,
            "updatedAt": _text_or_none(mark.get("updatedAt")) or created_at,
            "resolvedAt": _text_or_none(mark.get("resolvedAt")) if event_type != "assistant.mark.resolved" else _text_or_none(mark.get("resolvedAt")) or _utc_now_iso(),
        }
        reply_to = _text_or_none(mark.get("replyTo"))
        if reply_to:
            normalized["replyTo"] = reply_to
        return normalized
    if event_type == "human.intent.submitted":
        intent_payload = _normalize_json_object(payload.get("payload") or payload)
        created_at = _text_or_none(intent_payload.get("createdAt")) or _utc_now_iso()
        normalized = dict(intent_payload)
        normalized.setdefault("id", _new_id("intent"))
        normalized["scope"] = _text_or_none(normalized.get("scope")) or DEFAULT_MARK_SCOPE
        normalized["createdAt"] = created_at
        return normalized
    if event_type in ANNOTATION_EVENT_TYPES:
        annotation = _normalize_json_object(payload.get("annotation") or payload.get("payload") or payload)
        created_at = _text_or_none(annotation.get("createdAt")) or _utc_now_iso()
        normalized = dict(annotation)
        normalized.setdefault("id", _new_id("annotation"))
        normalized["scope"] = _text_or_none(normalized.get("scope")) or DEFAULT_MARK_SCOPE
        primary_anchor = _infer_annotation_primary_anchor(
            normalized,
            default="element" if event_type in {"human.annotation.created", "human.annotation.updated"} else None,
        )
        if primary_anchor:
            normalized["primaryAnchor"] = primary_anchor
        normalized["status"] = _annotation_status_for_event(event_type, _text_or_none(normalized.get("status")))
        normalized["createdAt"] = created_at
        normalized["updatedAt"] = _text_or_none(normalized.get("updatedAt")) or created_at
        if event_type == "human.annotation.resolved":
            normalized["resolvedAt"] = _text_or_none(normalized.get("resolvedAt")) or _utc_now_iso()
        return normalized
    if event_type == "system.annotation.control":
        control = _normalize_json_object(payload.get("payload") or payload)
        action = _text_or_none(control.get("action"))
        mode = _text_or_none(control.get("mode"))
        if action not in {"enable", "disable", "set-mode"}:
            raise ShowSessionEventError("annotation control action is invalid.", code="invalid_payload")
        if mode is not None and mode not in {"smart", "screenshot"}:
            raise ShowSessionEventError("annotation control mode is invalid.", code="invalid_payload")
        if action == "set-mode" and mode is None:
            raise ShowSessionEventError("annotation control mode is required.", code="invalid_payload")
        return {"action": action, **({"mode": mode} if mode is not None else {})}
    normalized = _normalize_json_object(payload.get("payload") or payload)
    if not normalized:
        normalized = {}
    normalized.setdefault("id", _new_id("runtime" if event_type.startswith("system.") else "page"))
    normalized.setdefault("createdAt", _utc_now_iso())
    normalized.setdefault("scope", DEFAULT_MARK_SCOPE)
    return normalized


def _normalize_json_object(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _normalize_human_author(author: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(author, dict) or author.get("kind") != "user":
        return {"kind": "local"}
    email = _text_or_none(author.get("email"))
    if not email:
        return {"kind": "local"}
    return {"kind": "user", "email": email}


def _json_object_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _event_anchor(event_type: str, payload: dict[str, Any], event_payload: dict[str, Any]) -> dict[str, Any]:
    anchor = _normalize_json_object(payload.get("anchor") or event_payload.get("anchor"))
    if anchor or event_type not in ANNOTATION_EVENT_TYPES:
        return anchor

    anchors = _json_object_list(event_payload.get("anchors"))
    if anchors:
        return anchors[0]

    matched_elements = _json_object_list(event_payload.get("matchedElements"))
    if matched_elements:
        return matched_elements[0]

    return {}


def _event_scope(event_type: str, payload: dict[str, Any]) -> str:
    if event_type.startswith("assistant.mark."):
        return _text_or_none(payload.get("scope")) or DEFAULT_MARK_SCOPE
    return _text_or_none(payload.get("scope")) or DEFAULT_MARK_SCOPE


def _event_id(original_payload: dict[str, Any], event_payload: dict[str, Any]) -> str:
    return _text_or_none(original_payload.get("id")) or _new_id("show_evt")


def _format_transcript_header(
    family: str,
    *,
    scope: Any,
    action: str | None = None,
    default_action: str | None = None,
) -> str:
    parts = [family]
    if action and action != default_action:
        parts.append(action)
    normalized_scope = _text_or_none(scope) or DEFAULT_MARK_SCOPE
    if normalized_scope != DEFAULT_MARK_SCOPE:
        parts.append(f"scope={normalized_scope}")
    return f"[{' '.join(parts)}]"


def _format_transcript_text(event_type: str, payload: dict[str, Any], anchor: dict[str, Any]) -> str:
    if event_type == "system.annotation.control":
        return ""
    if event_type.startswith("assistant.mark."):
        action = event_type.split(".")[-1]
        header = _format_transcript_header(
            "agent-mark",
            scope=payload.get("scope"),
            action=action,
            default_action="created",
        )
        lines = [
            f"{header} {payload.get('target')}",
            "",
            str(payload.get("body") or "").strip(),
        ]
        selector = _text_or_none(anchor.get("selector"))
        if selector:
            lines.extend(["", f"Anchor: {selector}"])
        text = _text_or_none(anchor.get("text"))
        if text:
            lines.append(f"Text: {text}")
        return "\n".join(lines)

    if event_type == "human.intent.submitted":
        text = _text_or_none(payload.get("text") or payload.get("comment") or payload.get("value"))
        label = _text_or_none(payload.get("intent") or payload.get("component")) or "intent"
        header = _format_transcript_header("show-intent", scope=payload.get("scope"))
        return f"{header} {label}\n\n{text or _json_dumps(payload)}"

    if event_type in ANNOTATION_EVENT_TYPES:
        action = event_type.split(".")[-1]
        text = _text_or_none(payload.get("text") or payload.get("comment"))
        label = _text_or_none(payload.get("intent")) or "comment"
        header = _format_transcript_header(
            "show-annotation",
            scope=payload.get("scope"),
            action=action,
            default_action="created",
        )
        lines = [f"{header} {label}"]
        if text:
            lines.extend(["", text])
        primary_anchor = _normalize_annotation_primary_anchor(payload.get("primaryAnchor"))
        if primary_anchor:
            lines.extend(["", f"Anchor kind: {primary_anchor}"])
        screenshot = _normalize_json_object(payload.get("screenshot"))
        if screenshot:
            screenshot_ref = _text_or_none(
                screenshot.get("attachmentId")
                or screenshot.get("assetId")
                or screenshot.get("id")
                or screenshot.get("url")
                or screenshot.get("src")
            )
            lines.append(f"Screenshot: {screenshot_ref or 'captured region'}")
            screenshot_region = _format_rect(screenshot.get("region") or screenshot.get("rect"))
            if screenshot_region:
                lines.append(f"Screenshot region: {screenshot_region}")
            screenshot_items = _json_object_list(screenshot.get("items"))
            if screenshot_items:
                lines.append("Screenshot comments:")
                for index, item in enumerate(screenshot_items, start=1):
                    item_label = _text_or_none(item.get("label")) or str(index)
                    item_text = _text_or_none(item.get("comment") or item.get("text") or item.get("body"))
                    line = f"{item_label}. {item_text or 'comment'}"
                    item_region = _format_rect(item.get("region") or item.get("rect"))
                    item_point = _format_point(item.get("point"))
                    if item_region:
                        line += f" ({item_region})"
                    elif item_point:
                        line += f" ({item_point})"
                    lines.append(line)
        region = _format_rect(payload.get("userRegion") or payload.get("region"))
        if region:
            lines.append(f"Region: {region}")
        classification = _format_classification(payload.get("classification"))
        if classification:
            lines.append(f"Selection: {classification}")
        matched_elements = _json_object_list(payload.get("matchedElements"))
        anchor_list = _json_object_list(payload.get("anchors"))
        matched_count = len(matched_elements) or (len(anchor_list) if primary_anchor == "element-group" else 0)
        if matched_count:
            lines.append(f"Matched elements: {matched_count}")
        quote = _text_or_none(anchor.get("textQuote") or anchor.get("text"))
        if quote:
            lines.extend(["", f"Quote: {quote}"])
        selector = _text_or_none(anchor.get("selector"))
        if selector:
            lines.append(f"Anchor: {selector}")
        return "\n".join(lines)

    if event_type == "assistant.page.updated":
        summary = _text_or_none(payload.get("summary") or payload.get("text") or payload.get("body"))
        return f"[show-page-updated] {summary or _json_dumps(payload)}"

    if event_type == "system.runtime.error":
        text = _text_or_none(payload.get("error") or payload.get("message") or payload.get("status"))
        return f"[show-runtime-error] {text or _json_dumps(payload)}"

    if event_type == "system.runtime.status":
        return ""

    return _json_dumps(payload)


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "type": row["event_type"],
        "actor": row["actor"],
        "scope": row["scope"],
        "anchor": _json_loads(row.get("anchor_json"), {}),
        "payload": _json_loads(row.get("payload_json"), {}),
        "transcript_text": row.get("transcript_text"),
        "message_id": row.get("message_id"),
        "created_at": row.get("created_at"),
    }


def _required_text(raw: Any, field: str) -> str:
    value = _text_or_none(raw)
    if not value:
        raise ShowSessionEventError(f"{field} is required.", code="invalid_payload")
    return value


def _text_or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _normalize_annotation_primary_anchor(raw: Any) -> str | None:
    value = _text_or_none(raw)
    if value == "group":
        return "element-group"
    if value in ANNOTATION_PRIMARY_ANCHORS:
        return value
    return None


def _infer_annotation_primary_anchor(annotation: dict[str, Any], *, default: str | None = None) -> str | None:
    explicit = _normalize_annotation_primary_anchor(annotation.get("primaryAnchor"))
    if explicit:
        return explicit

    if _normalize_json_object(annotation.get("screenshot")):
        return "screenshot"

    classification = _normalize_annotation_primary_anchor(_format_classification(annotation.get("classification")))
    if classification:
        return classification

    anchors = _json_object_list(annotation.get("anchors"))
    matched_elements = _json_object_list(annotation.get("matchedElements"))
    if len(anchors) > 1 or len(matched_elements) > 1:
        return "element-group"

    anchor = _normalize_json_object(annotation.get("anchor"))
    anchor_kind = _normalize_annotation_primary_anchor(anchor.get("kind"))
    if anchor_kind:
        return anchor_kind

    if len(anchors) == 1:
        single_anchor_kind = _normalize_annotation_primary_anchor(anchors[0].get("kind"))
        if single_anchor_kind:
            return single_anchor_kind

    if len(matched_elements) == 1:
        single_match_kind = _normalize_annotation_primary_anchor(matched_elements[0].get("kind"))
        return single_match_kind or "element"

    if _normalize_json_object(annotation.get("userRegion") or annotation.get("region")):
        return "area"

    return default


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _format_scalar(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return _text_or_none(raw)
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw.is_integer():
            return str(int(raw))
        return f"{raw:.2f}".rstrip("0").rstrip(".")
    return _text_or_none(raw)


def _format_rect(raw: Any) -> str | None:
    rect = _normalize_json_object(raw)
    if not rect:
        return None
    x = _first_present(rect, ("x", "left"))
    y = _first_present(rect, ("y", "top"))
    width = _first_present(rect, ("width", "w"))
    height = _first_present(rect, ("height", "h"))
    if x is None or y is None or width is None or height is None:
        return None
    return f"x:{_format_scalar(x)}, y:{_format_scalar(y)}, {_format_scalar(width)}x{_format_scalar(height)}"


def _format_point(raw: Any) -> str | None:
    point = _normalize_json_object(raw)
    if not point:
        return None
    x = _first_present(point, ("x", "left"))
    y = _first_present(point, ("y", "top"))
    if x is None or y is None:
        return None
    return f"x:{_format_scalar(x)}, y:{_format_scalar(y)}"


def _format_classification(raw: Any) -> str | None:
    if isinstance(raw, dict):
        return _text_or_none(raw.get("mode") or raw.get("kind") or raw.get("type"))
    return _text_or_none(raw)


def _annotation_status_for_event(event_type: str, requested: str | None) -> str:
    if event_type == "human.annotation.resolved":
        return "resolved"
    if event_type == "human.annotation.dismissed":
        return "dismissed"
    return requested or "pending"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback
