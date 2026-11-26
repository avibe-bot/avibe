"""Ephemeral single-use Anthropic parents. All comparisons use the rendered wire history, not a transcript cursor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from config.paths import get_state_dir
from core.anthropic_thread_errors import thread_error_fields
from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER, WireRequest, _header_value
from core.agent_core.ai.provider import ModelEndpoint

BETA = "message-threads-2026-08-12"
MIN_PARENT_TOKENS = 20_000
MIN_PARENT_TO_STATIC_RATIO = 3
MAX_RESTORABLE_PERCENT = 75
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
    message_id: str | None  # None: ordinary full witness, never a server parent.
    tokens: int | None  # Only a server parent needs a size for delta admission.
    expires: float
    reads: int | None = None
    terminal_mark: bool = False
    pinned_drop_block: bool = False
    carry_projection: str = ""


# Only bounded hashes and opaque ids, never prompts or credentials. Missing/evicted entries cost one full upload.
parents: OrderedDict[str, Parent | str] = OrderedDict()
claims: OrderedDict[str, object] = OrderedDict()


def parent_entry(session: str | None, *, claim: bool) -> tuple[Parent | None, str]:
    if not session:
        return None, "no_session"
    entry = parents.pop(session, None) if claim else parents.get(session)
    if isinstance(entry, Parent):
        return entry, "bound" if entry.message_id else "full_witness"
    return None, entry or "no_local_entry"


failed_routes: OrderedDict[bytes, float] = OrderedDict()


def remember(table, key, value):
    table[key] = value
    table.move_to_end(key)
    if len(table) > MAX_ENTRIES:
        table.popitem(last=False)


def route_key(ep: ModelEndpoint, gateway: bool) -> bytes:
    return digest(
        [
            # A gateway launch and its read-only preview have different transport URLs, not different Sources.
            None if gateway else ep.base_url,
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
class ReplayShape:
    """Only bind a response whose block stream the ordinary renderer consumed in order."""

    started: bool = False
    index: int = -1
    block: str | None = None
    has_delta: bool = False
    veto: str = "stream_lifecycle"

    def accepts(self, kind, chunk):
        if kind == "ping":
            return True
        if kind == "message_start":
            ok = not self.started and isinstance(chunk.get("message"), dict) and not chunk["message"].get("content")
            self.started = True
            return ok
        if not self.started:
            return False
        if kind == "content_block_start":
            block = chunk["content_block"]
            allowed = {
                "text": {"type", "text", "citations"},
                "thinking": {"type", "thinking", "signature"},
                "redacted_thinking": {"type", "data"},
                "tool_use": {"type", "name", "id", "input"},
            }
            tag = block["type"]
            if tag == "tool_use" and "caller" in block:
                # An explicit direct invocation is the ordinary client tool call; it has no server-side
                # execution context. Never erase an executor identity or an unknown caller extension.
                if block["caller"] != {"type": "direct"}:
                    self.veto = "unsupported_tool_caller"
                    return False
                allowed[tag].add("caller")
            if tag not in allowed or set(block) - allowed[tag]:
                self.veto = "unsupported_block_shape"
                return False
            if self.has_delta or self.block is not None or chunk["index"] != self.index + 1:
                return False
            self.index += 1
            self.block = tag
            return True
        if kind == "content_block_stop":
            if self.block is None or self.index != chunk["index"]:
                return False
            self.block = None
            return True
        if kind == "content_block_delta":
            delta = chunk["delta"]
            expected = {
                "text_delta": ("text", "text"),
                "thinking_delta": ("thinking", "thinking"),
                "signature_delta": ("thinking", "signature"),
                "input_json_delta": ("tool_use", "partial_json"),
            }.get(delta.get("type"))
            return bool(
                expected
                and self.index == chunk["index"]
                and self.block == expected[0]
                and set(delta) == {"type", expected[1]}
            )
        if kind == "message_delta":
            if self.has_delta:
                return False
            self.has_delta = True
            usage = chunk.get("usage") or {}
            iterations = usage.get("iterations") or []
            if isinstance(iterations, list) and any(
                isinstance(i, dict) and i.get("type") == "fallback_message" for i in iterations
            ):
                self.veto = "provider_fallback"
                return False
            return self.block is None and isinstance(iterations, list) and all(isinstance(i, dict) for i in iterations)
        return kind == "message_stop" and self.has_delta and self.block is None


@dataclass
class Attempt:
    wire: WireRequest
    mode: str
    reason: str | None
    prior: Parent | None
    fixed: bytes
    history: bytes
    record: dict
    capture_witness: bool
    pinned_drop_block: bool = False
    reported_reads: int | None = None

    def observe_usage(self, raw, usage, *, attributable: bool) -> None:
        # Missing counters are unknown, not zero. Start and delta may report different fields.
        if not isinstance(raw, dict):
            return
        reads = raw.get("cache_read_input_tokens")
        if type(reads) is int and reads >= 0:
            self.reported_reads = reads
            if self.mode == "create":
                self.record["create_cache_reads"] = reads
        if self.mode != "create":
            return
        self.record["create_usage_attribution"] = "verified" if attributable else "unverified"
        if not attributable:
            # The request was pinned, but these counters did not come from a verified serving hop.
            for key in ("route_hash", "model", "prior_cache_reads"):
                self.record.pop(key, None)
        # Project the canonical stream revision; do not merge a second copy of the provider's write counters.
        if usage is not None and (usage.cache_write_total_reported or usage.cache_writes_by_ttl is not None):
            self.record["create_cache_writes"] = usage.cache_write_tokens
            self.record["create_cache_write_basis"] = "reported" if usage.cache_write_total_reported else "derived"
        else:
            self.record.pop("create_cache_writes", None)
            self.record.pop("create_cache_write_basis", None)
        if usage is not None and usage.cache_writes_by_ttl is not None:
            self.record["create_cache_writes_by_ttl"] = dict(usage.cache_writes_by_ttl)
        else:
            self.record.pop("create_cache_writes_by_ttl", None)


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
    if (len(marks) != 2 or static != 2 or marks[0][1] == marks[1][1]
            or any(full["messages"][index]["role"] != "user" for _, index in marks)):
        return "preserve_cache_breakpoints", None
    if offset > 20 or marks[-1][0] - marks[0][0] >= 20:
        return "preserve_cache_breakpoints", marks[0][1]
    return None, marks[0][1]


def _reaches_parent_mark(assistant: dict, suffix: list) -> bool:
    # Positions after the previous request's final block: a lookup covers itself and 19 predecessors.
    content = assistant["content"]
    distance = len(content) if isinstance(content, list) else 1
    for msg in suffix:
        blocks = msg["content"]
        for block in blocks if isinstance(blocks, list) else (blocks,):
            distance += 1
            if distance >= 20:
                return False
            if isinstance(block, dict) and "cache_control" in block:
                return True
    return False


def terminal_mark(body: dict) -> bool:
    messages = body["messages"]
    blocks = messages[-1]["content"] if messages else None
    return bool(isinstance(blocks, list) and blocks and "cache_control" in blocks[-1])


def _engine_adds_drop_block(full: dict) -> bool:
    """Mirror the shipped CPA preserved-thinking default on the supported gateway model IDs.

    On a delta CPA no longer sees the signed blocks that caused it to derive the create's fixed fields.
    Never invent this policy for unknown models, disabled thinking, explicit bindings or forced tool use.
    """
    model = full["model"].lower().rsplit("/", 1)[-1].split("[", 1)[0]
    # Only identify provider IDs whose wire capability is known. Other models still try threads normally.
    families = (
        "claude-opus-5-5",
        "claude-fable-5-2",
        "claude-fable-5.2",
        "claude-fable-5-1",
        "claude-fable-5.1",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-haiku-5.5",
    )
    if not any(
        model == family or (family != "claude-opus-5-5" and model.startswith(family + "-")) for family in families
    ):
        return False
    thinking = full.get("thinking", {"type": "adaptive"})
    if not isinstance(thinking, dict) or "block_binding" in thinking:
        return False
    if thinking.get("type") != "adaptive" and not (
        thinking.get("type") == "enabled" and ("opus" in model or "fable" in model)
    ):
        return False
    choice = full.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in ("tool", "any"):
        return False
    return any(
        isinstance(block.get(key), str) and block[key].strip()
        for msg in full["messages"]
        if isinstance(msg["content"], list)
        for block in msg["content"]
        for tag, key in (("thinking", "signature"), ("redacted_thinking", "data"))
        if block.get("type") == tag
    )


def record_http_error(record: dict, status: int, body: str, *, hub: bool = False) -> None:
    record.update(thread_error_fields(status, body, hub=hub))


def _continuation_budget(full: dict, suffix: list, parent_tokens: int) -> str | None:
    # Same four UTF-8 bytes/token estimate as the context planner, but over rendered JSON
    # so tool schemas, block framing and structured results are included. Not a tokenizer.
    suffix_bytes = len(encoded(suffix))
    static_bytes = len(encoded([full.get("tools"), full.get("system")]))
    restorable_bytes = static_bytes + 1 + suffix_bytes  # comma before the final array element
    estimated_tokens = (restorable_bytes + 3) // 4
    if parent_tokens < MIN_PARENT_TO_STATIC_RATIO * estimated_tokens:
        return "large_static_or_delta"
    # Bound the bare suffix independently. A relay can also restore the real system/tools
    # without restoring the conversation: budget all that text at one token per UTF-8 byte,
    # leaving headroom below the first-event floor. Neither estimate proves provenance.
    if parent_tokens < MIN_PARENT_TO_STATIC_RATIO * suffix_bytes:
        return "dense_delta_risk"
    if 100 * restorable_bytes > MAX_RESTORABLE_PERCENT * parent_tokens:
        return "restorable_text_risk"
    return None


def _contains_image(messages: list) -> bool:
    def has_image(content) -> bool:
        return isinstance(content, list) and any(
            block.get("type") == "image" or (block.get("type") == "tool_result" and has_image(block.get("content")))
            for block in content
        )

    return any(has_image(message["content"]) for message in messages)


async def plan_send(
    wire,
    ep,
    *,
    gateway: bool,
    session: str | None,
    parent: Parent | None,
    repair: bool = False,
    ordinary: str | None = None,
    carry_projection: str = "",
) -> Attempt:
    route = route_key(ep, gateway)
    if not ordinary:
        if not session:
            ordinary = "side_or_pause"
        elif gateway and (not ep.source_id or ep.portable):
            ordinary = "unbound_source"
        elif parent is not None and parent.carry_projection != carry_projection:
            ordinary = "carry_projection_changed"
        elif repair:
            ordinary = "history_repair"
        elif failed_routes.get(route, 0) > time.monotonic():
            ordinary = "route_cooldown"
    return await asyncio.to_thread(prepare, wire, ep, gateway, parent, route, ordinary)


def prepare(
    wire: WireRequest,
    ep: ModelEndpoint,
    gateway: bool,
    parent: Parent | None,
    route: bytes,
    ordinary: str | None,
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
    cache_reason = None
    omitted_mark = None
    if not ordinary:
        cache_reason, omitted_mark = _cache_plan(full)
        if cache_reason == "native_history":
            ordinary = cache_reason
    matched_append = False
    prior_reads = None
    capture_witness = False
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
        else:
            matched_append = True
            prior_reads = parent.reads
            suffix = msgs[n + 1 :]
            # Encoded bytes bound text, not the tokens billed for decoded pixels. Never rely on the
            # usage heuristic for a suffix containing images, including a tool's returned images.
            if _contains_image(suffix):
                mode, reason = "full", "image_delta"
            elif parent.message_id is None:
                pass  # A full-history create neither restores parent tokens nor needs a small suffix.
            elif parent.tokens is None or parent.tokens < MIN_PARENT_TOKENS:
                reason = "small_parent"
            elif budget_reason := _continuation_budget(full, suffix, parent.tokens):
                reason = budget_reason
            elif parent.terminal_mark and not _reaches_parent_mark(msgs[n], suffix):
                mode, reason = "full", "terminal_marker_out_of_reach"
                capture_witness = omitted_mark is not None
            else:
                mode = "continue"
                body = {k: v for k, v in full.items() if k not in ("system", "tools")}
                body.update(messages=suffix, thread={"type": "continue", "previous_message_id": parent.message_id})
    # Only a create actually sends the marker-stripped full history. For a verified delta,
    # the unsent full body's lookup windows are irrelevant.
    delta_rejection = (
        reason if parent is not None and parent.message_id and mode != "continue" and not ordinary else None
    )
    if mode == "create" and cache_reason:
        reason = cache_reason
        if matched_append and omitted_mark is not None:
            reason = "full_history_create"
        else:
            mode = "full"
            capture_witness = omitted_mark is not None
    if mode == "create":
        body = {**full, "thread": {"type": "create"}}
        if omitted_mark is not None:
            # Change only the copy used for create. The ordinary replay keeps every original byte.
            msg = msgs[omitted_mark]
            unmarked = [{k: v for k, v in b.items() if k != "cache_control"} for b in msg["content"]]
            body["messages"] = [*msgs[:omitted_mark], {**msg, "content": unmarked}, *msgs[omitted_mark + 1 :]]
    pinned_drop_block = gateway and (
        (mode == "create" and _engine_adds_drop_block(body))
        or (mode == "continue" and parent is not None and parent.pinned_drop_block)
    )
    if pinned_drop_block:
        body = {
            **body,
            "thinking": {
                **body.get("thinking", {"type": "adaptive"}),
                "block_binding": {"prefix_mismatch_behavior": "drop_block"},
            },
        }
    if mode != "full":
        betas.append(BETA)
        if gateway:
            headers[PIN_SOURCE_HEADER] = ep.source_id
            if _header_value(ep.model_id):
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
    if delta_rejection and delta_rejection != reason:
        record["delta_rejection"] = delta_rejection
    if mode == "create":
        record.update(route_hash=route.hex(), model=ep.model_id, create_usage_attribution="unobserved")
        if prior_reads is not None:
            record["prior_cache_reads"] = prior_reads
    return Attempt(
        replace(wire, headers=headers, body=body),
        mode,
        reason,
        parent,
        fixed,
        message_digest(msgs) if mode != "full" or capture_witness else b"",
        record,
        capture_witness,
        pinned_drop_block,
    )
