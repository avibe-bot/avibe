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
        [ep.base_url, ep.source_id, ep.source_identity, ep.provider, ep.model_id, None if gateway else ep.token]
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
    if mode != "full":
        betas.append(BETA)
        if gateway:
            headers[PIN_SOURCE_HEADER] = ep.source_id
            headers.setdefault(PIN_MODEL_HEADER, ep.model_id)
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    full_size = len(encoded(full))
    record = {
        "mode": mode,
        "reason": reason,
        "body_bytes": full_size if body is full else len(encoded(body)),
        "full_bytes": full_size,
    }
    return Attempt(
        replace(wire, headers=headers, body=body),
        mode,
        reason,
        parent,
        fixed,
        message_digest(msgs) if mode != "full" else b"",
        record,
    )
