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
import re
import time
import unicodedata
from itertools import accumulate
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping, Optional, Protocol, Sequence

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
from core.reply_enhancer import strip_silent_blocks

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
#: Each artifact list a checkpoint row keeps this many paths, most recently touched first, and marks whether earlier
#: ones were pushed out; what the model reads shows a 4,000th of the route's window of them, between
#: ``ARTIFACTS_SHOWN_MIN`` and this (section 7).
ARTIFACTS_LISTED = 50
ARTIFACTS_SHOWN_MIN = 5
ARTIFACTS_SHOWN_RATIO = 4_000
#: Every path an artifact list carries, and every field of the state's environment block, is cut in the middle to this
#: many UTF-8 bytes: at most about 40 tokens each under the estimate, whatever the script (section 7).
ITEM_BYTES = 160
#: A checkpoint lists the skills its summarized rows loaded by name, at most this many, the most recent first, and
#: marks whether earlier ones were pushed out (section 7).
SKILLS_LISTED = 20

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
CHECKPOINT_TRUNCATED = (
    "[Output truncated to fit this checkpoint turn: showing about {shown} of {total} tokens. "
    "Read a smaller range if you need more.]"
)
#: What a tool result cut to fit a conversation request says (section 3).
TOOL_TRUNCATED = (
    "[Output truncated to fit the context window; re-run with offset/limit or a narrower command to see more.]"
)
MAX_ROLLS = 2
MAX_OVERFLOWS = 4
#: Unproductive checkpoint attempts (failed, or normal with a result still at least ``INEFFECTIVE_RATIO * T``) a run
#: makes before it stops compacting for the rest of the run; held in memory, never persisted (section 10).
UNPRODUCTIVE_CHECKPOINTS = 2
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
#: The last line of a checkpoint that keeps the in-flight Turn's inputs (section 7): they come just before it, and a
#: record that misstates the Turn (as finished, or as more than was asked) must not override them.
KEPT_INPUTS_WIN = (
    "The user message(s) placed just before this record are the current request and are still in progress; follow "
    "them exactly as written. Where this record disagrees with them, they win."
)
#: A drop that leaves out a previous checkpoint too large for the route says so (section 8 c).
CHECKPOINT_OMITTED = "An earlier checkpoint was too large for this model and was omitted"
SKILLS_LEAD = "Skills you had loaded are listed by name; run `vibe skill load <name>` again before you rely on one."


# --- what the adapter supplies ----------------------------------------------------


@dataclass(frozen=True)
class StateRequest:
    """What ``ContextHost.render_state`` renders for a new checkpoint (context.md section 7)."""

    session_id: str


class ContextHost(Protocol):
    def earlier_record(self, session_id: str, through_seq: int) -> Optional[str]:
        """A short hint saying where the moved-out rows through ``through_seq`` are stored, or None."""
        ...

    async def render_state(self, request: StateRequest) -> Sequence[str]:
        """State texts from their own stores (the Avibe Agent's: the environment's core fields)."""
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


def fit_result(
    content: Sequence[UserContent], limit: int, *, note: str = CHECKPOINT_TRUNCATED, cut: bool = False
) -> tuple[UserContent, ...]:
    """A tool result cut to ``limit`` tokens, head kept, ending with ``note`` to say so: a checkpoint turn's (section
    6), and through ``fit_batch`` a conversation's (section 3). ``cut``: the caller already dropped part of it (an
    image a text replacement cannot carry), so it says so even when what is left fits."""
    total = message_tokens(ToolResultMessage("fit", "fit", tuple(content)))
    if total <= limit and not cut:
        return tuple(content)
    note = note.format(shown=max(0, limit), total=total)
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
                and entry.row_id not in view.cleared
            ):
                eligible.append((index, entry, message))
    candidates = [item for item in eligible[: max(0, len(eligible) - CLEAR_KEEP_RESULTS)] if item[0] < protected_from]
    placeholder = text_tokens(CLEARED_PLACEHOLDER)
    freed = sum(message_tokens(message) - placeholder for _, _, message in candidates)
    return tuple(entry for _, entry, _ in candidates) if freed >= CLEAR_MIN_TOKENS else ()


def clear_edit(entry: ContextEntry) -> dict[str, Any]:
    return _edit(entry, CLEARED_PLACEHOLDER, "clear_old_tool_result")


def fit_edit(entry: ContextEntry, text: str) -> dict[str, Any]:
    """The edit that shows a tool result cut to fit (``fit_batch``, section 3); the row keeps the whole output."""
    return _edit(entry, text, "fit_tool_result")


def _edit(entry: ContextEntry, text: str, reason: str) -> dict[str, Any]:
    return {"version": 1, "target_event_id": entry.row_id, "replacement": {"text": text}, "reason": reason}


def notes_tokens(results: Sequence[ToolResultMessage]) -> int:
    """The tokens of a tool batch with every result cut to its note, the least ``fit_batch`` can make of it."""
    floor = message_tokens(ToolResultMessage("fit", "fit", (text(TOOL_TRUNCATED),)))
    return sum(min(message_tokens(result), floor) for result in results)


def fit_batch(results: Sequence[ToolResultMessage], room: int) -> Optional[list[Optional[str]]]:
    """A tool batch cut to ``room`` tokens (section 3): the largest results cut to one common cap, water-filling, each
    ending with ``TOOL_TRUNCATED``; the others whole. Each result's cut text, or None where it stays whole; None for the
    batch when even every result cut to its note cannot fit (``notes_tokens``)."""
    sizes = [message_tokens(result) for result in results]
    if sum(sizes) <= room:
        return [None] * len(results)
    floor = message_tokens(ToolResultMessage("fit", "fit", (text(TOOL_TRUNCATED),)))

    def total(cap: int) -> int:
        return sum(min(size, cap) for size in sizes)

    if notes_tokens(results) > room:
        return None
    low, high = floor, max(sizes)  # total(low) <= room < total(high)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if total(middle) <= room else (low, middle)
    cut: list[Optional[str]] = []
    for result, size in zip(results, sizes):
        if size <= low:
            cut.append(None)
            continue
        # The replacement is one text: join the result's blocks first, so the cut measures what the model reads, and
        # it always says it was cut, an image dropped included.
        body = "\n".join(block.text or "" for block in result.content if isinstance(block, TextBlock))
        kept = fit_result((text(body),), low, note=TOOL_TRUNCATED, cut=True)
        cut.append("\n".join(block.text or "" for block in kept if isinstance(block, TextBlock)))
    return cut


# --- cut points (context.md section 5) ----------------------------------------------


def unit_tokens(unit: Unit) -> int:
    return messages_tokens(unit.messages)


def _turn(units: Sequence[Unit], turn_inputs: Collection[int]) -> tuple[list[int], list[int]]:
    """How many of the in-flight Turn's inputs ``units[:cut]`` holds, and their tokens, for each ``cut``: a cut inside
    the Turn keeps each of them as it was (section 5)."""
    counts, sizes = [0], [0]
    for unit in units:
        kept = unit.lead.kind == "input" and unit.seq in turn_inputs
        counts.append(counts[-1] + kept)
        sizes.append(sizes[-1] + (unit_tokens(unit) if kept else 0))
    return counts, sizes


def _moves(counts: Sequence[int], cut: int, pinned: int) -> bool:
    """Whether a cut before ``units[cut]`` moves out more than the Turn's inputs it keeps, and falls after the inputs
    the latest checkpoint kept (``pinned`` of them, first in ``units``): one between them would bring back what that
    checkpoint summarized."""
    return cut >= pinned and cut > counts[cut]


def normal_cut(
    units: Sequence[Unit], keep: int, *, turn_inputs: Collection[int] = (), pinned: int = 0
) -> Optional[int]:
    """Index of the first kept unit: the longest tail of whole units within ``keep``, at least the last, with the
    in-flight Turn's inputs a cut keeps counted once at their own size (section 5), never inside the ``pinned``
    inputs the latest checkpoint kept.

    None when nothing but the Turn's inputs would move out.
    """
    if not units:
        return None
    sizes = [unit_tokens(unit) for unit in units]
    tails = list(accumulate(reversed(sizes)))[::-1]  # tails[cut]: the tokens of units[cut:]
    counts, kept = _turn(units, turn_inputs)
    cut = len(units) - 1
    while cut > max(0, pinned) and tails[cut - 1] + kept[cut - 1] <= keep:
        cut -= 1
    return cut if _moves(counts, cut, pinned) else None


def half_cut(units: Sequence[Unit], *, turn_inputs: Collection[int] = (), pinned: int = 0) -> Optional[int]:
    """The cut nearest to half the tokens, never past the last unit nor inside the ``pinned`` kept inputs; None when
    nothing but the Turn's kept inputs could move out."""
    sizes = [unit_tokens(unit) for unit in units]
    counts, _ = _turn(units, turn_inputs)
    half = sum(sizes) / 2
    best: Optional[int] = None
    best_distance = 0.0
    before = 0
    for cut in range(1, len(units)):
        before += sizes[cut - 1]
        if _moves(counts, cut, pinned) and (best is None or abs(before - half) < best_distance):
            best, best_distance = cut, abs(before - half)
    return best


def rolling_cut(
    units: Sequence[Unit], fits: Callable[[int], bool], *, turn_inputs: Collection[int] = (), pinned: int = 0
) -> Optional[int]:
    """The largest cut at or before ``half_cut`` whose forked request over the head fits."""
    counts, _ = _turn(units, turn_inputs)
    cut = half_cut(units, turn_inputs=turn_inputs, pinned=pinned)
    while cut is not None and cut > 0:
        if _moves(counts, cut, pinned) and fits(cut):
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


#: The approved template's top-level section markers (section 11), whatever language the headings are in.
_SECTION = re.compile(r"^#[ \t]*([123])\.", re.M)


def checkpoint_problem(reply: str) -> Optional[str]:
    """Why a checkpoint turn's reply is not a checkpoint, or None when it is (section 6), judged on what the user
    would see of it: the product's one silent-block grammar (``strip_silent_blocks``, which delivery uses) removes its
    ``<silent>`` notes first. Then the template's three top-level markers (``# 1.``, ``# 2.``, ``# 3.``) appear
    exactly once each, in order, each section with content under its heading (a line that is not itself a heading).
    The one check every checkpoint path applies."""
    visible = strip_silent_blocks(reply)
    markers = list(_SECTION.finditer(visible))
    numbers = [marker.group(1) for marker in markers]
    for number in "123":
        if number not in numbers:
            return f"section {number} is missing"
    if numbers != ["1", "2", "3"]:
        return "a section marker is repeated or out of order"
    starts = [marker.start() for marker in markers]
    for number, (start, end) in enumerate(zip(starts, [*starts[1:], len(visible)]), 1):
        body = visible[start:end].splitlines()[1:]  # the heading line itself is not content
        if not any(line.strip() and not line.lstrip().startswith("#") for line in body):
            return f"section {number} is empty"
    return None


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def truncate_middle_bytes(text: str, limit: int) -> str:
    """``text`` cut in the middle to at most ``limit`` UTF-8 bytes with an ellipsis, on character boundaries.

    Its head and its tail (a file name) stay. Bytes are counted as the estimate counts them (section 2, lone
    surrogates included, as a POSIX path's undecodable bytes arrive), so a byte bound is a token bound whatever the
    script.
    """
    if _utf8(text) <= limit:
        return text
    room = limit - _utf8("…")
    head = _within(text, room - room // 2)
    tail = _within(text[::-1], room // 2)[::-1]
    return f"{head}…{tail}"


def _within(text: str, limit: int) -> str:
    """The longest prefix of ``text`` within ``limit`` estimate bytes."""
    used = 0
    for index, char in enumerate(text):
        used += _utf8(char)
        if used > limit:
            return text[:index]
    return text


@dataclass(frozen=True)
class _Artifacts:
    """Each list most recently touched first, ``ARTIFACTS_LISTED`` kept; ``*_omitted`` marks that a path was ever
    pushed out. A mark, not a count: a path pushed out and touched again is the same path, so no count stays true."""

    read: list[str]
    read_omitted: bool
    modified: list[str]
    modified_omitted: bool


def _recent(touched: Sequence[str], earlier: Sequence[str], omitted: bool) -> tuple[list[str], bool]:
    """``touched`` paths (in touch order) ahead of ``earlier`` ones (stored, most recent first), at most
    ``ARTIFACTS_LISTED`` kept, and whether any was ever pushed out. Paths are originals: only the display is cut."""
    paths = _unique([*reversed(touched), *earlier])
    return paths[:ARTIFACTS_LISTED], omitted or len(paths) > ARTIFACTS_LISTED


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
            if call.name == "read":
                read.append(path)
            elif call.name in {"write", "edit"}:
                modified.append(path)
    modified_listed, modified_omitted = _recent(
        modified, previous.get("files_modified", ()), previous.get("files_modified_omitted", False)
    )
    changed = {*modified, *modified_listed}
    read_listed, read_omitted = _recent(
        [path for path in read if path not in changed],
        [path for path in previous.get("files_read", ()) if path not in changed],
        previous.get("files_read_omitted", False),
    )
    return _Artifacts(read_listed, read_omitted, modified_listed, modified_omitted)


def _skills(entry: ContextEntry) -> tuple[str, ...]:
    """The names of the skills a tool result loaded (``details.skills``, which the adapter marks), in load order."""
    details = entry.payload.get("details")
    skills = details.get("skills") if isinstance(details, Mapping) else None
    if not isinstance(skills, list):
        return ()
    return tuple(
        skill["name"] for skill in skills if isinstance(skill, Mapping) and isinstance(skill.get("name"), str)
    )


def carried_skills(view: ContextView, cut: int) -> tuple[tuple[str, ...], bool]:
    """The names of the skills loaded before the cut (and listed by the previous checkpoint), minus those loaded
    after it: the most recently loaded first, at most ``SKILLS_LISTED``, as loaded; and whether any was ever pushed
    out (section 7)."""
    loaded: dict[str, None] = {}  # the latest load last
    previous = view.compaction.payload if view.compaction is not None else {}
    for skill in reversed(previous.get("skills", ())):  # stored most recent first
        loaded.pop(skill["name"], None)
        loaded[skill["name"]] = None
    for unit in view.units[:cut]:
        for entry, _ in unit.entries[1:]:
            for name in _skills(entry) if entry is not None else ():
                loaded.pop(name, None)
                loaded[name] = None
    for unit in view.units[cut:]:
        for entry, _ in unit.entries[1:]:
            for name in _skills(entry) if entry is not None else ():
                loaded.pop(name, None)
    omitted = bool(previous.get("skills_omitted", False)) or len(loaded) > SKILLS_LISTED
    return tuple(list(reversed(loaded))[:SKILLS_LISTED]), omitted


def summarized_to_seq(view: ContextView, cut: int) -> int:
    previous = view.compaction.payload.get("summarized_to_seq", 0) if view.compaction is not None else 0
    seqs = [entry.context_seq for unit in view.units[:cut] for entry, _ in unit.entries if entry is not None]
    return max([previous, *seqs])


def display(text: str) -> str:
    """One line of plain text for a path or a name the model reads (section 7): ``escape``d, then cut in the middle to
    ``ITEM_BYTES``. The row keeps the original."""
    return truncate_middle_bytes(escape(text), ITEM_BYTES)


def escape(text: str) -> str:
    """``text`` as one line of plain text that closes no tag, injectively: a backslash doubled, then control
    characters and the markup delimiters ``<`` and ``>`` as ``\\uXXXX`` (``\\UXXXXXXXX`` past the BMP). Two texts
    never escape alike, so only a cut can make two displays look the same (section 7)."""
    return "".join(_escaped(char) for char in text)


def _escaped(char: str) -> str:
    if char == "\\":
        return "\\\\"
    if char in "<>" or unicodedata.category(char).startswith("C"):
        code = ord(char)
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
    return char


def artifacts_shown(window: int) -> int:
    """How many paths of each artifact list the model reads on a route with ``window`` tokens (section 7)."""
    return min(ARTIFACTS_LISTED, max(ARTIFACTS_SHOWN_MIN, window // ARTIFACTS_SHOWN_RATIO))


def render_summary(
    *,
    checkpoint: str,
    files_read: Sequence[str],
    files_read_omitted: bool,
    files_modified: Sequence[str],
    files_modified_omitted: bool,
    shown: int,
    skills: Sequence[str],
    skills_omitted: bool,
    earlier_record: Optional[str],
    checkpoint_omitted: bool = False,
    keeps_inputs: bool = False,
) -> str:
    lines = ["<context-checkpoint>"]
    if checkpoint:
        lines += [CHECKPOINT_FRAMING, "", checkpoint, ""]
    elif checkpoint_omitted:
        lines.append(CHECKPOINT_OMITTED + ("; see the earlier record." if earlier_record else "."))
    # Only what is exact is counted: the stored paths the route does not show. Ones pushed out are marked, never
    # counted, and the earlier record, when there is one, holds them.
    earlier = "- and earlier ones" + (" (see the earlier record)" if earlier_record else "")
    lines.append("<artifacts>")
    lists = (("Read", files_read, files_read_omitted), ("Modified", files_modified, files_modified_omitted))
    for label, paths, omitted in lists:
        # The row keeps up to ``ARTIFACTS_LISTED``; the model reads the route's share of them (``shown``).
        hidden = max(0, len(paths) - shown)
        listed = [
            *(f"- {display(path)}" for path in paths[:shown]),
            *([f"- and {hidden} more"] if hidden else []),
            *([earlier] if omitted else []),
        ]
        lines += [f"{label}:", *listed] if listed else [f"{label}: (none)"]
    lines.append("</artifacts>")
    if skills or skills_omitted:
        # Names only: a skill's instructions are loaded again by the model, never injected (section 7).
        named = [*(f"- {display(name)}" for name in skills), *([earlier] if skills_omitted else [])]
        lines += ["<skills-loaded>", SKILLS_LEAD, *named, "</skills-loaded>"]
    if earlier_record:
        lines += ["<earlier-record>", earlier_record, "</earlier-record>"]
    if keeps_inputs:
        lines.append(KEPT_INPUTS_WIN)
    lines.append("</context-checkpoint>")
    return "\n".join(lines)


def compaction_payload(
    view: ContextView,
    cut: int,
    *,
    mode: str,
    reason: str,
    checkpoint: str,
    state: Sequence[str],
    earlier_record: Optional[str],
    tokens_before: int,
    threshold: int,
    window: int,
    summarizer: Optional[Mapping[str, Any]],
    usage: Optional[Usage],
    checkpoint_omitted: bool = False,
    turn_inputs: Collection[int] = (),
) -> dict[str, Any]:
    """The ``Compaction`` row for a cut before ``view.units[cut]``; ``tokens_after_estimate`` is the caller's.

    ``window`` is that of the route the conversation's next request goes to, which reads the summary.
    ``checkpoint_omitted``: a drop left out the previous checkpoint's text, too large for that route (section 8 c).
    ``turn_inputs``: the ``context_seq`` of each input the in-flight Turn consumed, its first and every steer; those
    in the head stay as they were (``kept_inputs``, section 5).
    """
    previous_row = view.compaction
    previous = previous_row.payload if previous_row is not None else {}
    if cut < view.pinned:
        raise ValueError(f"a cut at {cut} falls inside the {view.pinned} inputs the latest checkpoint kept")
    head = view.units[:cut]
    files = _files(previous, head)
    skills, skills_omitted = carried_skills(view, cut)
    kept = [unit.seq for unit in head if unit.lead.kind == "input" and unit.seq in turn_inputs]
    payload: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "reason": reason,
        "summary": render_summary(
            checkpoint=checkpoint,
            files_read=files.read,
            files_modified=files.modified,
            files_read_omitted=files.read_omitted,
            files_modified_omitted=files.modified_omitted,
            shown=artifacts_shown(window),
            skills=skills,
            skills_omitted=skills_omitted,
            earlier_record=earlier_record,
            checkpoint_omitted=checkpoint_omitted,
            keeps_inputs=bool(kept),
        ),
        "checkpoint": checkpoint,
        "state": list(state),
        "first_kept_seq": view.units[cut].seq,
        "summarized_to_seq": summarized_to_seq(view, cut),
        "previous_compaction_id": previous_row.row_id if previous_row is not None else None,
        "kept_inputs": kept,
        "files_read": files.read,
        "files_read_omitted": files.read_omitted,
        "files_modified": files.modified,
        "files_modified_omitted": files.modified_omitted,
        "skills": [{"name": name} for name in skills],
        "skills_omitted": skills_omitted,
        "tokens_before": tokens_before,
        "tokens_after_estimate": 0,
        "threshold": threshold,
        "summarizer": dict(summarizer) if summarizer is not None else None,
    }
    if usage is not None:
        payload["usage"] = usage_to_dict(usage)
    return payload
