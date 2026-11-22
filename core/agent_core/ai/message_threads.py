"""Ephemeral single-use Anthropic parents. All comparisons use the rendered wire history, not a transcript cursor."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from config.paths import get_state_dir
from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER, WireRequest
from core.agent_core.ai.provider import ModelEndpoint

BETA = "message-threads-2026-08-12"
MIN_PARENT_TOKENS = 20_000
MIN_PARENT_TO_STATIC_RATIO = 3
MIN_USAGE_PERCENT = 90
PARENT_SECONDS = 300
COOLDOWN_SECONDS = 86400
MAX_ENTRIES = 4096


def encoded(obj: Any) -> bytes:
    # Same encoder options as httpx's compact JSON body.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(obj: Any) -> bytes:
    return hashlib.sha256(encoded(obj)).digest()


def message_digest(messages) -> bytes:
    h = hashlib.sha256()
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            # Strip only planner-owned block markers. Nested tool input named cache_control is user data.
            content = [{k: v for k, v in b.items() if k != "cache_control"} for b in content]
            if (
                msg["role"] == "user"
                and len(content) == 1
                and content[0].keys() == {"type", "text"}
                and content[0]["type"] == "text"
            ):
                content = content[0]["text"]
        h.update(digest([msg["role"], content]))
    return h.digest()


@dataclass(frozen=True)
class Parent:
    route: bytes
    fixed: bytes
    count: int
    history: bytes
    assistant: bytes
    message_id: str
    tokens: int
    expires: float


# Only bounded hashes and opaque ids, never prompts or credentials. Missing/evicted entries cost one full upload.
parents: OrderedDict[str, Parent] = OrderedDict()
failed_routes: OrderedDict[bytes, float] = OrderedDict()


def remember(table, key, value):
    table[key] = value
    table.move_to_end(key)
    if len(table) > MAX_ENTRIES:
        table.popitem(last=False)


def route_key(ep: ModelEndpoint, gateway: bool) -> bytes:
    return digest(
        [
            ep.base_url,
            ep.source_id,
            ep.source_identity,
            ep.provider,
            ep.model_id,
            None
            if gateway
            else [
                ep.token,
                sorted((k.lower(), v) for k, v in ep.request_headers.items() if k.lower() != "anthropic-beta"),
            ],
        ]
    )


@dataclass
class Attempt:
    wire: WireRequest
    mode: str
    reason: str | None
    prior: Parent | None
    fixed: bytes
    history: bytes
    record: dict


def _cache_plan(full: dict) -> tuple[str | None, int | None]:
    """CPA keeps three explicit breakpoints for threads. Keep the ordinary path when dropping the older
    user breakpoint could put its warmed prefix beyond the cache's 20-block lookup window."""
    static = sum("cache_control" in b for b in full.get("tools", ()))
    system = full.get("system")
    if isinstance(system, list):
        static += sum("cache_control" in b for b in system)
    marks = []
    offset = 0
    for i, msg in enumerate(full["messages"]):
        blocks = msg["content"]
        if isinstance(blocks, list):
            for block in blocks:
                if block.get("type") in ("server_tool_use", "web_search_tool_result"):
                    return "native_history", None
                if "cache_control" in block:
                    marks.append((offset, i))
                offset += 1
        else:
            offset += 1
    if static + len(marks) <= 3:
        return None, None
    if len(marks) != 2 or marks[-1][0] - marks[0][0] >= 20:
        return "preserve_cache_breakpoints", None
    return None, marks[0][1]


def prepare(
    wire: WireRequest, ep: ModelEndpoint, gateway: bool, parent: Parent | None, route: bytes, ordinary: str | None
) -> Attempt:
    full = wire.body
    msgs = full["messages"]
    headers = dict(wire.headers)
    names = [k for k in headers if k.lower() == "anthropic-beta"]
    betas = [t.strip() for k in names for t in headers.pop(k).split(",") if t.strip() and t.strip() != BETA]
    fixed = digest(
        [
            {
                k: v
                for k, v in full.items()
                if k not in ("messages", "max_tokens", "metadata", "stop_sequences", "stream")
            },
            sorted(set(betas)),
        ]
    )
    body = full
    reason = ordinary
    omitted_mark = None
    if not ordinary:
        ordinary, omitted_mark = _cache_plan(full)
    mode = "full" if ordinary or (get_state_dir() / "disable-message-threads").exists() else "create"
    if mode == "full":
        reason = ordinary or "disabled"
    elif parent is not None:
        n = parent.count
        if parent.expires <= time.monotonic():
            reason = "expired"
        elif route != parent.route:
            reason = "route_changed"
        elif fixed != parent.fixed:
            reason = "settings_changed"
        elif (
            len(msgs) <= n + 1
            or message_digest(msgs[:n]) != parent.history
            or message_digest([msgs[n]]) != parent.assistant
        ):
            reason = "history_changed"
        elif parent.tokens < MIN_PARENT_TOKENS:
            reason = "small_parent"
        else:
            suffix = msgs[n + 1 :]
            # UTF-8 bytes conservatively bound the text tokens; images or large tools simply cost a full send.
            static = len(encoded([full.get("tools"), full.get("system"), suffix]))
            if parent.tokens < MIN_PARENT_TO_STATIC_RATIO * static:
                reason = "large_static_or_delta"
            else:
                mode = "continue"
                body = {k: v for k, v in full.items() if k not in ("system", "tools")}
                body.update(messages=suffix, thread={"type": "continue", "previous_message_id": parent.message_id})
    if mode == "create":
        body = {**full, "thread": {"type": "create"}}
        if omitted_mark is not None:
            # Change only the copy used for create. The ordinary replay keeps every original byte.
            msg = msgs[omitted_mark]
            unmarked = [{k: v for k, v in b.items() if k != "cache_control"} for b in msg["content"]]
            body["messages"] = [*msgs[:omitted_mark], {**msg, "content": unmarked}, *msgs[omitted_mark + 1 :]]
    if mode != "full":
        betas.append(BETA)
        if gateway:
            headers[PIN_SOURCE_HEADER] = ep.source_id
            headers.setdefault(PIN_MODEL_HEADER, ep.model_id)
            if mode == "continue":
                headers["x-avibe-thread-declarations"] = encoded(
                    [[tool["name"], tool["type"]] for tool in full.get("tools", ()) if tool.get("type")]
                ).decode("ascii")
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    # The transport records actual bytes only once a send is handed to HTTPX.
    # A plain send needs no extra serialization to measure the counterfactual.
    record = {"mode": mode, "reason": reason, "full_bytes": len(encoded(full)) if mode != "full" else 0}
    return Attempt(
        replace(wire, headers=headers, body=body),
        mode,
        reason,
        parent,
        fixed,
        message_digest(msgs) if mode != "full" else b"",
        record,
    )
