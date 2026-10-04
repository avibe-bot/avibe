"""C-9 context management rules (``agent-core-contracts/context.md``).

Pure functions over the projected ``ContextView``: the limits, the token
estimate, which tool results to clear, where to cut, and the checkpoint's
model-facing text and row. The loop applies them before every model request;
the adapter supplies a ``ContextHost`` for what only it knows.

The cut rule and cumulative file lists follow Pi (MIT, Copyright (c) 2025
Mario Zechner, ``packages/coding-agent/src/core/compaction/`` at ``7fbbd5f``);
the checkpoint request is the owner-approved prompt ``checkpoint-v2``.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from core.agent_core.ai._common import endpoint_origin
from core.agent_core.ai.provider import ModelCapabilities, ModelRequest
from core.agent_core.harness.projection import ContextView, Unit, context_view
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserContent,
    UserMessage,
    Usage,
    text,
    usage_to_dict,
)
from core.agent_core.tools.base import ToolSpec

# --- constants (context.md) -----------------------------------------------------

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_192
MARGIN_MIN = 8_000
MARGIN_RATIO = 0.03
#: The margin is at most an eighth of the window: with ``O <= W / 4`` (the Avibe Agent's budget), ``T >= 0.625 * W``.
MARGIN_CAP_RATIO = 0.125
THRESHOLD_RATIO = 0.9
KEEP_MAX = 20_000
KEEP_RATIO = 0.25
IMAGE_TOKENS = 1_600
#: Each artifact list a checkpoint carries keeps this many paths, most recently touched first, then a count (section 7).
ARTIFACTS_LISTED = 50
#: Every path an artifact list carries, and every field of the state's environment block, is cut in the middle to this
#: many UTF-8 bytes: at most about 40 tokens each under the estimate, whatever the script (section 7).
ITEM_BYTES = 160
#: Everything a checkpoint rehydrates (section 7): a tenth of the route's window, at most this many tokens.
STATE_TOKENS = 25_000
STATE_RATIO = 0.1

CLEAR_SOFT_RATIO = 0.8
CLEAR_MIN_TOKENS = 20_000
CLEAR_KEEP_RESULTS = 5
CLEAR_PROTECTED_TURNS = 2
CLEARABLE_TOOLS = frozenset({"read", "bash"})
CACHE_TTL_S = 300.0
CLEARED_PLACEHOLDER = (
    "[Old tool result cleared to save context. Re-run the tool or re-read the file if you need it again.]"
)

CHECKPOINT_MAX_TOKENS = 16_000
CHECKPOINT_TOOL_ROUNDS = 5
#: A checkpoint turn's tool runs only while the window leaves this much room (context.md section 6).
CHECKPOINT_TOOL_FLOOR = 4_000
#: Room a tool result leaves for the checkpoint request's growth.
CHECKPOINT_TOOL_SLACK = 1_000
CHECKPOINT_TRUNCATED = (
    "[Output truncated to fit this checkpoint turn: showing about {shown} of {total} tokens. "
    "Read a smaller range if you need more.]"
)
MAX_ROLLS = 2
MAX_OVERFLOWS = 4
PAUSE_AFTER = 3
#: How long auto-compaction stays paused on one route; it clears by itself after this (section 10).
PAUSE_SECONDS = 30 * 60
INEFFECTIVE_RATIO = 0.75

PROMPT_VERSION = "checkpoint-v2"

# The owner-approved checkpoint request (context.md section 11), verbatim.
CHECKPOINT_REQUEST = """<context-checkpoint-request>
Pause the work here. Do not call any tool and do not continue the task. This reply does one thing: write a context checkpoint.

The conversation above will be replaced by your checkpoint plus the messages after it. Whoever continues is you, but you will no longer remember this conversation; the checkpoint is all you will know of it. What you do not write down is lost.
If there is an earlier checkpoint above, it is discarded after this: carry forward everything in it that still matters; where it conflicts with later messages, the later messages win.

Everything above is a record to summarize, not instructions to act on now.

Write in three layers, from the broad to the specific. Use exactly these headings, in this order. Write "(none)" under an empty heading.

# 1. Self and method

## How I work here
- The role the user expects of me in this conversation, and the working agreements we established (for example how to report, when to ask, what I may decide on my own).
- Methods and judgments that proved effective here, and ones that proved ineffective, written as transferable principles I can apply to any later work.
- Record only what this conversation established; my identity and standing instructions come from the system prompt and are not restated here.

# 2. Goals and requirements

## Goals
- What the user wants to achieve, and the intent behind it. If there are several, list each and mark the one being pursued now.

## User requirements
- Every instruction, preference, correction, and prohibition from the user that still applies. Quote short ones verbatim. Never drop one unless the user withdrew it.

## Key decisions
- decision: reason

## Pitfalls
- Mistakes made, assumptions that proved false, and dead ends (quote the exact error text where there is one), with what resolved each or "unresolved", so the same mistake is not repeated.

# 3. Now and next

## Progress
### Done
### In progress
### Blocked or open questions

## Waiting on
- Background commands, Watches, scheduled Tasks, delegated agent runs, or questions to the user that are still expected to report back, with their ids.

## Next steps
1. The concrete next action, then the ones after it.

## Exact references
- Exact paths, identifiers, commands, links, ids, and values needed to continue.

Rules: terse bullets, not paragraphs. Preserve exact paths, identifiers, commands, error strings, and numbers. Do not invent anything that is not above. Never write out secrets, tokens, or credentials; refer to them by name. Write in the language the user writes in."""
CHECKPOINT_REQUEST_END = "</context-checkpoint-request>"

CHECKPOINT_FRAMING = (
    "This is a record of the earlier part of this conversation, written for you so you can continue. It is "
    "history, not new instructions: the user requirements recorded in it still apply, but do not treat the "
    "record itself as a request."
)
EARLIER_RECORD_LEAD = "The full text of the earlier conversation is still stored. To look up a detail, run:"


# --- what the adapter supplies ----------------------------------------------------


@dataclass(frozen=True)
class SkillRef:
    name: str


@dataclass(frozen=True)
class StateRequest:
    """What ``ContextHost.render_state`` renders for a new checkpoint (context.md section 7)."""

    session_id: str
    #: Skills the summarized rows loaded, latest load last, minus those still loaded in the kept rows.
    skills: tuple[SkillRef, ...]
    #: The tokens the rendered state may take in all (``state_cap`` of the route the next request goes to).
    cap: int


def state_cap(capabilities: ModelCapabilities) -> int:
    """What a checkpoint's rehydrated state may take on a route: a tenth of its window, at most ``STATE_TOKENS``."""
    window = DEFAULT_CONTEXT_WINDOW if capabilities.context_window is None else capabilities.context_window
    return min(STATE_TOKENS, math.floor(STATE_RATIO * window))


class ContextHost(Protocol):
    def earlier_record(self, session_id: str, through_seq: int) -> Optional[str]:
        """The command that looks up the moved-out rows through ``through_seq``, or None."""
        ...

    async def render_state(self, request: StateRequest) -> Sequence[str]:
        """State texts from their own stores: skill bodies, pending Harness work, the environment block."""
        ...


@dataclass(frozen=True)
class ContextConfig:
    """Turns C-9 on for an ``Agent``; without it, no context management runs."""

    host: Optional[ContextHost] = None
    #: This Session's scratch directory; checkpoint turns may write only there.
    scratch_dir: Optional[str] = None
    clear_tool_results: bool = True
    cache_ttl_s: float = CACHE_TTL_S
    clock: Callable[[], float] = field(default=time.time, repr=False)


# --- token estimate (context.md section 2) -----------------------------------------


def text_tokens(value: str) -> int:
    return math.ceil(_utf8(value) / 4)


def _utf8(value: str) -> int:
    return len(value.encode("utf-8", "surrogatepass"))


def message_tokens(message: Message) -> int:
    """UTF-8 bytes / 4 of everything replayed, with a fixed cost per image."""
    size = 0
    images = 0
    for block in message.content:
        if isinstance(block, TextBlock):
            size += _utf8(block.text) if block.text is not None else block.ref.bytes
        elif isinstance(block, ImageBlock):
            images += 1
        elif isinstance(block, ThinkingBlock):
            size += _utf8(block.text) + _utf8(block.signature or "")
        elif isinstance(block, ToolCallBlock):
            arguments = json.dumps(block.arguments, ensure_ascii=False, separators=(",", ":"))
            size += _utf8(block.name) + _utf8(arguments) + _utf8(block.signature or "")
    return math.ceil(size / 4) + images * IMAGE_TOKENS


def messages_tokens(messages: Sequence[Message]) -> int:
    return sum(message_tokens(message) for message in messages)


def request_tokens(system: str, tools: Sequence[ToolSpec], messages: Sequence[Message]) -> int:
    """The whole request: system prompt, tool definitions, and messages."""
    definitions = sum(
        _utf8(json.dumps([spec.name, spec.description, dict(spec.input_schema)], ensure_ascii=False))
        for spec in tools
    )
    return math.ceil((_utf8(system) + definitions) / 4) + messages_tokens(messages)


def usage_total(usage: Usage) -> int:
    return usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.output_tokens


def add_usage(total: Optional[Usage], usage: Optional[Usage]) -> Optional[Usage]:
    """Sum every field; reasoning tokens are the sum of those reported, unknown only when none were."""
    if usage is None:
        return total
    if total is None:
        return usage
    reasoning = (
        None
        if total.reasoning_tokens is None and usage.reasoning_tokens is None
        else (total.reasoning_tokens or 0) + (usage.reasoning_tokens or 0)
    )
    return Usage(
        input_tokens=total.input_tokens + usage.input_tokens,
        output_tokens=total.output_tokens + usage.output_tokens,
        cache_read_tokens=total.cache_read_tokens + usage.cache_read_tokens,
        cache_write_tokens=total.cache_write_tokens + usage.cache_write_tokens,
        reasoning_tokens=reasoning,
    )


# --- request accounting (context.md sections 1 and 2) ---------------------------------


@dataclass(frozen=True)
class Anchor:
    """The latest response in the context with valid usage and request facts on its row (section 2).

    Rebuilt from the rows, so it holds across a new Agent and a new Turn.
    """

    #: What its request carried from the transcript, before the response.
    transcript: tuple[Message, ...]
    response: AssistantMessage
    #: UTF-8/4 of the whole request it answered, as sent (``ModelResponse.request.tokens``).
    request_tokens: int


def _valid_usage(message: Optional[Message]) -> bool:
    return (
        isinstance(message, AssistantMessage)
        and message.stop_reason not in {"error", "aborted"}
        and message.usage is not None
        and usage_total(message.usage) > 0
    )


def sent_tokens(request: ModelRequest) -> int:
    """UTF-8/4 of a whole request: system prompt, tool definitions, and messages."""
    return request_tokens(request.system, request.tools, request.messages)


def request_facts(request: ModelRequest) -> dict[str, int]:
    """``ModelResponse.request``: what a response's row records about the request it answered."""
    return {"tokens": sent_tokens(request)}


def last_anchor(rows: Sequence[ContextEntry], view: ContextView) -> Optional[Anchor]:
    """The anchor the rows hold: their latest response in the context with valid usage and request facts."""
    for unit in reversed(view.units):
        lead = unit.lead
        facts = lead.payload.get("request") if lead.kind == "response" else None
        if facts is None or not _valid_usage(lead.message):
            continue
        # Projection is pure: the rows before the response give exactly the transcript its request carried.
        before = context_view([row for row in rows if row.context_seq < lead.context_seq]).messages
        return Anchor(before, lead.message, facts["tokens"])
    return None


def output_tokens(capabilities: ModelCapabilities, configured: int) -> int:
    """``O`` of a conversation request: the hop's ``max_output_tokens`` (8,192 when unknown), capped by the
    Agent's output budget."""
    hop = capabilities.max_output_tokens
    return min(configured, DEFAULT_MAX_OUTPUT_TOKENS if hop is None else hop)


def checkpoint_max_tokens(capabilities: ModelCapabilities, configured: int) -> int:
    """A checkpoint request's ``max_tokens``: ``min(16,000, O)`` (section 6)."""
    return min(CHECKPOINT_MAX_TOKENS, output_tokens(capabilities, configured))


def _estimate(request: ModelRequest, transcript: Sequence[Message], anchor: Optional[Anchor]) -> int:
    """The anchored usage while it holds, adjusted by the UTF-8/4 delta of everything else; else UTF-8/4.

    The anchor holds while the request goes to the route that answered it and the transcript up to its response
    is unchanged. A changed system prompt, tool set, or rehydrated state does not invalidate it: the request's
    own UTF-8/4 size carries the delta.
    """
    whole = sent_tokens(request)
    if anchor is not None and anchor.response.origin == endpoint_origin(request.endpoint):
        sent = len(anchor.transcript)
        if (
            len(transcript) > sent
            and transcript[sent] == anchor.response
            and tuple(transcript[:sent]) == anchor.transcript
        ):
            usage = usage_total(anchor.response.usage)
            return max(0, usage + whole - anchor.request_tokens - message_tokens(anchor.response))
    return whole


def fit_result(content: Sequence[UserContent], limit: int) -> tuple[UserContent, ...]:
    """A checkpoint turn's tool result cut to ``limit`` tokens, head kept, saying what was cut (section 6)."""
    total = message_tokens(ToolResultMessage("fit", "fit", tuple(content)))
    if total <= limit:
        return tuple(content)
    note = CHECKPOINT_TRUNCATED.format(shown=max(0, limit), total=total)
    room = max(0, limit - text_tokens(note) - 1) * 4
    kept: list[UserContent] = []
    for block in content:
        if isinstance(block, TextBlock) and block.text is not None and room > 0:
            data = block.text.encode("utf-8", "surrogatepass")[:room]
            kept.append(TextBlock(text=data.decode("utf-8", "ignore")))
            room -= len(data)
        elif isinstance(block, ImageBlock) and room >= IMAGE_TOKENS * 4:
            kept.append(block)
            room -= IMAGE_TOKENS * 4
    return (*kept, TextBlock(text=note))


@dataclass(frozen=True)
class Budget:
    """One composed request measured against the route resolved for it (sections 1 and 2): the request to send,
    or one the stage only considers (a dry fork, the stop check's minimal request)."""

    window: int
    input_limit: int
    #: ``O``: the request's own ``max_tokens``.
    output: int
    margin: int
    threshold: int
    keep: int
    est: int

    @property
    def fits(self) -> bool:
        """A conversation request stays inside ``L_in`` with the margin."""
        return self.est + self.output + self.margin <= self.input_limit

    @property
    def can_fit(self) -> bool:
        """The request can fit at all, without the margin (a checkpoint request's admission test)."""
        return self.est + self.output <= self.input_limit


def budget(
    request: ModelRequest,
    capabilities: ModelCapabilities,
    *,
    transcript: Sequence[Message],
    anchor: Optional[Anchor] = None,
) -> Budget:
    """``W, L_in, O, M, T, keep`` and ``est`` of a composed ``request`` on the route resolved for it.

    Sections 1 and 2: it derives ``W, L_in, M, T, keep`` and ``est`` and reads ``O`` from ``request.max_tokens``,
    which ``output_tokens`` or ``checkpoint_max_tokens`` set when the request was built. ``transcript`` is the part
    of the request's messages that comes from the rows, which is what an anchor is checked against.
    """
    window = DEFAULT_CONTEXT_WINDOW if capabilities.context_window is None else capabilities.context_window
    limit = window if capabilities.input_limit is None else capabilities.input_limit
    output = request.max_tokens
    margin = min(max(MARGIN_MIN, math.ceil(MARGIN_RATIO * window)), math.floor(MARGIN_CAP_RATIO * window))
    threshold = min(limit - output - margin, math.floor(THRESHOLD_RATIO * window))
    keep = max(0, min(KEEP_MAX, math.floor(KEEP_RATIO * threshold)))
    return Budget(window, limit, output, margin, threshold, keep, _estimate(request, transcript, anchor))


# --- clearing (context.md section 4) ------------------------------------------------


def _skills(entry: ContextEntry) -> tuple[SkillRef, ...]:
    """The skills a tool result loaded (``details.skills``, which the adapter marks), in load order."""
    details = entry.payload.get("details")
    skills = details.get("skills") if isinstance(details, Mapping) else None
    if not isinstance(skills, list):
        return ()
    return tuple(
        SkillRef(skill["name"]) for skill in skills if isinstance(skill, Mapping) and isinstance(skill.get("name"), str)
    )


def clearable_results(view: ContextView) -> tuple[ContextEntry, ...]:
    """The tool results to clear now, or none when clearing them would free under 20,000 tokens."""
    inputs = [index for index, unit in enumerate(view.units) if unit.lead.kind == "input"]
    protected_from = inputs[-CLEAR_PROTECTED_TURNS] if len(inputs) >= CLEAR_PROTECTED_TURNS else 0
    eligible: list[tuple[int, ContextEntry, ToolResultMessage]] = []
    for index, unit in enumerate(view.units):
        for entry, message in unit.entries[1:]:
            if (
                entry is not None
                and isinstance(message, ToolResultMessage)
                and message.tool_name in CLEARABLE_TOOLS
                and entry.row_id not in view.edited
                and not _skills(entry)
            ):
                eligible.append((index, entry, message))
    candidates = [item for item in eligible[: max(0, len(eligible) - CLEAR_KEEP_RESULTS)] if item[0] < protected_from]
    placeholder = text_tokens(CLEARED_PLACEHOLDER)
    freed = sum(message_tokens(message) - placeholder for _, _, message in candidates)
    return tuple(entry for _, entry, _ in candidates) if freed >= CLEAR_MIN_TOKENS else ()


def clear_edit(entry: ContextEntry) -> dict[str, Any]:
    return {
        "version": 1,
        "target_event_id": entry.row_id,
        "replacement": {"text": CLEARED_PLACEHOLDER},
        "reason": "clear_old_tool_result",
    }


# --- cut points (context.md section 5) ----------------------------------------------


def unit_tokens(unit: Unit) -> int:
    return messages_tokens(unit.messages)


def normal_cut(units: Sequence[Unit], keep: int) -> Optional[int]:
    """Index of the first kept unit: the longest tail of whole units within ``keep``, at least the last.

    None when the head would be empty.
    """
    if not units:
        return None
    cut = len(units) - 1
    kept = unit_tokens(units[cut])
    while cut > 0 and kept + unit_tokens(units[cut - 1]) <= keep:
        cut -= 1
        kept += unit_tokens(units[cut])
    return cut or None


def half_cut(units: Sequence[Unit]) -> Optional[int]:
    """The cut nearest to half the tokens, never past the last unit; None when only one unit is left."""
    if len(units) < 2:
        return None
    sizes = [unit_tokens(unit) for unit in units]
    half = sum(sizes) / 2
    best, before = 1, sizes[0]
    best_distance = abs(before - half)
    for cut in range(2, len(units)):
        before += sizes[cut - 1]
        if abs(before - half) < best_distance:
            best, best_distance = cut, abs(before - half)
    return best


def rolling_cut(units: Sequence[Unit], fits: Callable[[int], bool]) -> Optional[int]:
    """The largest cut at or before ``half_cut`` whose forked request over the head fits."""
    cut = half_cut(units)
    while cut is not None and cut > 0:
        if fits(cut):
            return cut
        cut -= 1
    return None


# --- the checkpoint (context.md sections 6 and 7) ----------------------------------


def checkpoint_request() -> UserMessage:
    return UserMessage((text(f"{CHECKPOINT_REQUEST}\n{CHECKPOINT_REQUEST_END}"),))


def checkpoint_text(message: AssistantMessage) -> str:
    return "\n\n".join(
        block.text.strip() for block in message.content if isinstance(block, TextBlock) and block.text and block.text.strip()
    )


def _input_text(message: UserMessage) -> str:
    parts = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text is not None:
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            parts.append(f"[image: {block.name or block.mime_type}]")
    return "\n".join(parts)


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def truncate_middle_bytes(text: str, limit: int) -> str:
    """``text`` cut in the middle to at most ``limit`` UTF-8 bytes with an ellipsis, on character boundaries.

    Its head and its tail (a file name) stay. The estimate counts UTF-8 bytes (section 2), so a byte bound is a
    token bound whatever the script.
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    room = limit - len("…".encode("utf-8"))
    tail = room // 2
    head = data[: room - tail].decode("utf-8", "ignore")
    return f"{head}…{data[len(data) - tail :].decode('utf-8', 'ignore')}"


@dataclass(frozen=True)
class _Artifacts:
    """Each list most recently touched first, ``ARTIFACTS_LISTED`` kept; ``*_more`` counts the paths pushed out."""

    read: list[str]
    read_more: int
    modified: list[str]
    modified_more: int


def _recent(touched: Sequence[str], earlier: Sequence[str], more: int) -> tuple[list[str], int]:
    """``touched`` (in touch order) ahead of ``earlier`` (most recent first), the first ``ARTIFACTS_LISTED`` kept."""
    paths = _unique([*reversed(touched), *earlier])
    return paths[:ARTIFACTS_LISTED], more + max(0, len(paths) - ARTIFACTS_LISTED)


def _files(previous: Mapping[str, Any], head: Sequence[Unit]) -> _Artifacts:
    """Cumulative artifacts: the path of each call that succeeded (no hook rewrites arguments under C-9).

    Bounded across checkpoints (section 7): a long-lived Session would otherwise carry every path it ever touched.
    """
    read: list[str] = []
    modified: list[str] = []
    for unit in head:
        message = unit.lead.message
        if not isinstance(message, AssistantMessage):
            continue
        for call, (entry, result) in zip(message.tool_calls, unit.entries[1:]):
            path = call.arguments.get("path")
            if entry is None or result.is_error or not isinstance(path, str) or not path:
                continue
            path = truncate_middle_bytes(path, ITEM_BYTES)
            if call.name == "read":
                read.append(path)
            elif call.name in {"write", "edit"}:
                modified.append(path)
    modified_listed, modified_more = _recent(
        modified, previous.get("files_modified", ()), previous.get("files_modified_more", 0)
    )
    changed = {*modified, *modified_listed}
    read_listed, read_more = _recent(
        [path for path in read if path not in changed],
        [path for path in previous.get("files_read", ()) if path not in changed],
        previous.get("files_read_more", 0),
    )
    return _Artifacts(read_listed, read_more, modified_listed, modified_more)


def carried_skills(view: ContextView, cut: int) -> tuple[SkillRef, ...]:
    """Skills loaded before the cut (and carried by the previous checkpoint), minus those loaded after it."""
    loaded: dict[str, None] = {}
    previous = view.compaction.payload if view.compaction is not None else {}
    for skill in previous.get("skills", ()):
        loaded.pop(skill["name"], None)
        loaded[skill["name"]] = None
    for unit in view.units[:cut]:
        for entry, _ in unit.entries[1:]:
            for skill in _skills(entry) if entry is not None else ():
                loaded.pop(skill.name, None)
                loaded[skill.name] = None
    for unit in view.units[cut:]:
        for entry, _ in unit.entries[1:]:
            for skill in _skills(entry) if entry is not None else ():
                loaded.pop(skill.name, None)
    return tuple(SkillRef(name) for name in loaded)


def summarized_to_seq(view: ContextView, cut: int) -> int:
    previous = view.compaction.payload.get("summarized_to_seq", 0) if view.compaction is not None else 0
    seqs = [entry.context_seq for unit in view.units[:cut] for entry, _ in unit.entries if entry is not None]
    return max([previous, *seqs])


def render_summary(
    *,
    checkpoint: str,
    files_read: Sequence[str],
    files_read_more: int,
    files_modified: Sequence[str],
    files_modified_more: int,
    earlier_record: Optional[str],
    current_request: Optional[str],
) -> str:
    lines = ["<context-checkpoint>"]
    if checkpoint:
        lines += [CHECKPOINT_FRAMING, "", checkpoint, ""]
    lines.append("<artifacts>")
    lists = (("Read", files_read, files_read_more), ("Modified", files_modified, files_modified_more))
    for label, paths, more in lists:
        listed = [*(f"- {path}" for path in paths), *([f"- and {more} more"] if more else [])]
        lines += [f"{label}:", *listed] if listed else [f"{label}: (none)"]
    lines.append("</artifacts>")
    if earlier_record:
        lines += ["<earlier-record>", EARLIER_RECORD_LEAD, earlier_record, "</earlier-record>"]
    if current_request is not None:
        lines += ["<current-request>", current_request, "</current-request>"]
    lines.append("</context-checkpoint>")
    return "\n".join(lines)


def compaction_payload(
    view: ContextView,
    cut: int,
    *,
    mode: str,
    reason: str,
    checkpoint: str,
    skills: Sequence[SkillRef],
    state: Sequence[str],
    earlier_record: Optional[str],
    tokens_before: int,
    threshold: int,
    summarizer: Optional[Mapping[str, Any]],
    usage: Optional[Usage],
) -> dict[str, Any]:
    """The ``Compaction`` row for a cut before ``view.units[cut]``; ``tokens_after_estimate`` is the caller's."""
    previous_row = view.compaction
    previous = previous_row.payload if previous_row is not None else {}
    head = view.units[:cut]
    files = _files(previous, head)
    request_id: Optional[str] = None
    request: Optional[str] = None
    if view.units[cut].lead.kind != "input":
        inputs = [unit.lead for unit in head if unit.lead.kind == "input"]
        if inputs:
            request_id, request = inputs[-1].row_id, _input_text(inputs[-1].message)
        else:
            request_id, request = previous.get("current_request_message_id"), previous.get("current_request")
    payload: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "reason": reason,
        "summary": render_summary(
            checkpoint=checkpoint,
            files_read=files.read,
            files_modified=files.modified,
            files_read_more=files.read_more,
            files_modified_more=files.modified_more,
            earlier_record=earlier_record,
            current_request=request,
        ),
        "checkpoint": checkpoint,
        "state": list(state),
        "first_kept_seq": view.units[cut].seq,
        "summarized_to_seq": summarized_to_seq(view, cut),
        "previous_compaction_id": previous_row.row_id if previous_row is not None else None,
        "current_request": request,
        "current_request_message_id": request_id,
        "files_read": files.read,
        "files_read_more": files.read_more,
        "files_modified": files.modified,
        "files_modified_more": files.modified_more,
        "skills": [{"name": skill.name} for skill in skills],
        "tokens_before": tokens_before,
        "tokens_after_estimate": 0,
        "threshold": threshold,
        "summarizer": dict(summarizer) if summarizer is not None else None,
    }
    if usage is not None:
        payload["usage"] = usage_to_dict(usage)
    return payload
