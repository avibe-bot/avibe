"""Tool-step presentation from committed Vibey context, never from execution replay.

The versioned response capability is the adoption boundary. This pure projection
is shared by Activity's durable/live readers and the read-only SQL view. Storage
supplies only the response's own context interval; provider ids alone are not
call identities. Legacy/non-Vibey traces remain their own presentation source.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from core.agent_core.messages import AssistantMessage, HostedToolBlock, ToolCallBlock, ToolResultMessage, message_from_dict
from core.hosted_activity import hosted_line, native_activities, proxy_activity
from core.reply_enhancer import strip_silent_blocks

PROJECTION_KEY = "tool_projection"
SOURCE_KEY = "tool_call_source"
PROJECTION_VERSION = 1
ACTIVITY_FIELDS = ("order_micros", "order_index", "status", "started_at", "ended_at", "duration_ms")


def object_value(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def capability(metadata: Any) -> dict | None:
    value = object_value(metadata).get(PROJECTION_KEY)
    if (isinstance(value, dict) and type(value.get("version")) is int
            and value["version"] == PROJECTION_VERSION and value.get("backend") == "vibey"):
        return value
    return None


def source_link(response_id: str, call_id: str) -> dict:
    return {"version": PROJECTION_VERSION, "response_id": response_id, "call_id": call_id}


def step_id(response_id: str, call_id: str) -> str:
    return f"tool:{response_id}:{quote(call_id, safe='')}"


def linked_step(metadata: Any) -> str | None:
    source = object_value(metadata).get(SOURCE_KEY)
    if (isinstance(source, dict) and type(source.get("version")) is int
            and source["version"] == PROJECTION_VERSION
            and isinstance(source.get("response_id"), str) and source["response_id"]
            and isinstance(source.get("call_id"), str) and source["call_id"]):
        return step_id(source["response_id"], source["call_id"])
    return None


def relative_to(cwd: str) -> Callable[[str], str]:
    """The same path shortening used by the live IM tool formatter."""
    def relative(path: str) -> str:
        absolute = os.path.abspath(os.path.join(cwd, os.path.expanduser(path)) if cwd else os.path.expanduser(path))
        if not cwd:
            return absolute
        try:
            shown = os.path.relpath(absolute, cwd)
        except ValueError:
            return absolute
        return absolute if shown.startswith(os.path.join(os.pardir, os.pardir)) else shown
    return relative


def formatter_for(platform: str):
    from modules.im.formatters.avibe_formatter import AvibeFormatter
    from modules.im.formatters.discord_formatter import DiscordFormatter
    from modules.im.formatters.feishu_formatter import FeishuFormatter
    from modules.im.formatters.slack_formatter import SlackFormatter
    from modules.im.formatters.telegram_formatter import TelegramFormatter
    from modules.im.formatters.wechat_formatter import WeChatFormatter

    return {"discord": DiscordFormatter, "feishu": FeishuFormatter, "lark": FeishuFormatter,
            "slack": SlackFormatter, "telegram": TelegramFormatter, "wechat": WeChatFormatter}.get(platform, AvibeFormatter)()


def response_clock(row_id: str, created_at: str) -> int:
    if len(row_id) >= 19 and row_id[3] == "_":
        try:
            return int(row_id[4:19], 16)
        except ValueError:
            pass
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return int(stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp() * 1_000_000)
    except (TypeError, ValueError):
        return 0


def project_response(row: Mapping[str, Any], results: list[dict], turn: Mapping[str, Any] | None = None) -> list[dict]:
    """Event-shaped steps, with explicit ordering and no assumed tool start.

    ``results`` contains only this Session's rows between this response and the
    next response, in context order. It never includes a fork child's settlement.
    Malformed/unknown response shapes retain their real compatibility traces.
    """
    metadata = object_value(row.get("metadata_json"))
    cap = capability(metadata)
    if cap is None or row.get("context_seq") is None:
        return []
    try:
        message = message_from_dict(object_value(row.get("content_json"))["model"]["message"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    if not isinstance(message, AssistantMessage):
        return []
    formatter = formatter_for(str(cap.get("platform") or row.get("platform") or "avibe"))
    states = object_value(cap.get("calls"))
    settled = {}
    for result in results:
        payload = object_value(result.get("content_json"))
        try:
            parsed = message_from_dict(payload.get("message") or {})
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if isinstance(parsed, ToolResultMessage):
            details = object_value(payload.get("details"))
            if parsed.hosted is not None and details.get("hosted_route_switch") == "native_interrupted":
                parsed = replace(parsed, hosted=replace(parsed.hosted, route_switch="native_interrupted"))
            settled.setdefault(parsed.tool_call_id, (parsed, result, details))
    names = object_value(cap.get("source_names"))
    native = {activity.call_id: activity for activity in native_activities(message, source_names=names.get, response_id=row["id"])}
    clock = response_clock(row["id"], row.get("created_at") or "")
    projected = []
    for index, block in enumerate(message.content):
        if not isinstance(block, (ToolCallBlock, HostedToolBlock)):
            continue
        is_native = isinstance(block, HostedToolBlock)
        call_id = block.id
        state = object_value(states.get(call_id))
        activity = native.get(call_id) if is_native else None
        if is_native and activity is None:
            # Deferred hosted exchanges are not display calls (same rule as IM).
            continue
        started = state.get("started_at")
        ended = None
        duration = state.get("duration_ms")
        result = settled.get(call_id) if not is_native else None
        media_tokens = object_value(metadata.get("hosted_media")) if is_native else {}
        if result is not None:
            media_tokens = object_value(object_value(result[1].get("metadata_json")).get("hosted_media"))
        if activity is not None:
            interrupted = activity.status == "incomplete" or (
                activity.status == "pending" and (activity.interruption or (turn and turn.get("state") == "terminal"))
            )
            status = "interrupted" if interrupted else activity.status
        elif result is not None:
            parsed, result_row, details = result
            status = "failed" if parsed.is_error else "completed"
            if details.get("recovered") and parsed.is_error:
                status = "interrupted"
            ended = state.get("ended_at") or result_row.get("created_at")
            if parsed.hosted is not None:
                call_names = object_value(state.get("source_names"))
                source_states = state.get("source_states")
                activity = proxy_activity(
                    block, parsed.hosted, source_names=call_names.get,
                    source_state=source_states.get if isinstance(source_states, dict) else None,
                    duration_ms=duration,
                )
        elif turn and turn.get("state") == "terminal":
            status = "interrupted"
        else:
            status = "running" if started else "pending" if turn and turn.get("state") else "unknown"
        if activity is not None:
            text = hosted_line(activity, str(cap.get("language") or "en"), escape=formatter.escape_special_chars)
            hosted = activity.to_payload()
            # The same retained-window retry evidence used for legacy native cards.
            if is_native and activity.interruption and cap.get("retried"):
                hosted["retried"] = True
        else:
            text = formatter.format_toolcall(block.name, dict(block.arguments), get_relative_path=relative_to(str(cap.get("cwd") or "")))
            hosted = None
        text = strip_silent_blocks(text)
        if not text.strip():
            continue
        facts = {
            "order_micros": clock + (-1 if is_native else 1), "order_index": index,
            "status": status, "started_at": started, "ended_at": ended, "duration_ms": duration,
        }
        projected.append({
            "id": step_id(row["id"], call_id), "session_id": row.get("session_id"),
            "scope_id": row.get("scope_id"), "platform": row.get("platform"),
            "agent_name": row.get("author_name"), "backend": "vibey", "event_type": "tool_call",
            "visibility": "trace", "turn_id": metadata.get("turn_id"), "text": text,
            "created_at": row.get("created_at"), "updated_at": ended or started or row.get("created_at"),
            "content": {"kind": "toolcall", "activity": facts, **({"hosted": hosted} if hosted else {})},
            "metadata": {SOURCE_KEY: source_link(row["id"], call_id),
                         **({"turn_id": metadata["turn_id"]} if metadata.get("turn_id") else {}),
                         **({"precedes_message_id": row["id"]} if is_native else {})},
            "response_id": row["id"], "call_id": call_id, "context_seq": row["context_seq"],
            "name": block.tool if is_native else block.name,
            "_media_tokens": media_tokens,
            "arguments": None if is_native else dict(block.arguments),
            **facts,
        })
    return projected
