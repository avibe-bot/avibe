"""The gateway's hosted search executor (``vibey-hosted-tools.md`` §6).

When Vibey's hop runs a search tool as a proxy function, Vibey's tool runtime
asks the gateway to run it; the gateway tries the turn's executor chain. This
module owns what one executor request sends and how its response becomes the
canonical record; the service owns the chain and the calls, and Vibey renders
the record.

An executor request is one short native request on the vendor's executor
model that searches and lists results. It sends no reasoning effort, and the
declaration is the overlay's, exactly as a native turn sends it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Final, Mapping

from config.v2_config import ModelHubSourceConfig
from core.agent_core.ai.hosted import reported_counts
from core.agent_core.ai.sse import SSEParser
from vibe.backend_model_catalog import HostedToolSpec

from .events import bounded_diagnostic_detail
from .hosted_items import responses_item_tool
from .pricing import usd_from_xai_ticks
from .request import ModelHubRequest

EXECUTOR_TOOLS: Final = ("web_search", "x_search")
# One deadline for a search call's whole executor chain, not one per candidate.
SEARCH_TIMEOUT_SECONDS: Final = 180.0
# Responses counts reasoning against this cap, so it leaves room beyond the list itself.
_RESPONSES_OUTPUT_TOKENS: Final = 4096
_ANTHROPIC_OUTPUT_TOKENS: Final = 2048
# A search answer is a short cited list; this bounds a misbehaving response.
EXECUTOR_RESPONSE_MAX_BYTES: Final = 16 * 1024 * 1024
_SNIPPET_CHARS: Final = 500
# The url_citation annotations one response's record reads; an executor's answer cites tens.
_MAX_CITATIONS: Final = 256
_X_POST_HOSTS: Final = ("x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com")
_X_SEARCH_MODES: Final = ("Latest", "Top")

_SEARCH_INSTRUCTIONS: Final = {
    "web_search": (
        "You are a search executor. Search the web once for the user's query with the web_search tool. "
        "Then list what you found as a numbered list, most relevant first. For each result, write one or two "
        "sentences stating what the page says about the query, and cite the result in each sentence. "
        "Write nothing else."
    ),
    "x_search": (
        "You are a search executor. Search X once for the user's query with the x_search tool. "
        "Then list the posts you found as a numbered list, in the order the search returned them. For each post, "
        "write one or two sentences stating what the post says, and cite the post in each sentence. "
        "Write nothing else."
    ),
}


class ExecutorResponseRefused(ValueError):
    """The executor's response broke a bound; ``key`` names the failure to show."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class ExecutorCallRefused(ValueError):
    """A malformed proxy call: its machine code and HTTP status."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ExecutorCall:
    tool: str
    arguments: Mapping[str, Any]


def parse_call(tool: str, payload: object) -> ExecutorCall:
    """Validate one ``POST /vibey/v1/hosted-tools/{tool}`` body, ``{"arguments": ...}``, against the canonical schema."""

    if tool not in EXECUTOR_TOOLS:
        raise ExecutorCallRefused("not_found_error", 404)
    if not isinstance(payload, dict) or set(payload) != {"arguments"}:
        raise ExecutorCallRefused("invalid_request_error", 400)
    arguments = payload["arguments"]
    if not isinstance(arguments, dict):
        raise ExecutorCallRefused("invalid_request_error", 400)
    optional = {"web_search": set(), "x_search": {"limit", "mode"}}[tool]
    text = arguments.get("query")
    if set(arguments) - {"query"} - optional or not isinstance(text, str) or not text.strip():
        raise ExecutorCallRefused("invalid_request_error", 400)
    limit, mode = arguments.get("limit"), arguments.get("mode")
    if (
        (limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1))
        or (mode is not None and mode not in _X_SEARCH_MODES)
    ):
        raise ExecutorCallRefused("invalid_request_error", 400)
    return ExecutorCall(tool, dict(arguments))


def executor_streams(source: ModelHubSourceConfig) -> bool:
    """A Responses executor streams, so the idle bound stays fed; Anthropic is buffered."""

    return source.protocol == "openai_responses"


def call_timeout(tool: str) -> float:
    """The deadline of one call's whole executor chain."""

    return SEARCH_TIMEOUT_SECONDS


def executor_request(call: ExecutorCall, source: ModelHubSourceConfig, model_id: str, spec: HostedToolSpec) -> ModelHubRequest:
    """The one native request this call sends, in the executor Source's own protocol."""

    declaration = spec.declaration
    query = call.arguments["query"]
    if call.tool == "x_search":
        settings = [f"limit {call.arguments['limit']}" if "limit" in call.arguments else None,
                    f"mode {call.arguments['mode']}" if "mode" in call.arguments else None]
        chosen = ", ".join(item for item in settings if item)
        if chosen:
            query = f"{query}\n\n(Search with {chosen}.)"
    instructions = _SEARCH_INSTRUCTIONS[call.tool]
    if source.protocol == "anthropic":
        payload = {
            "model": model_id,
            "max_tokens": _ANTHROPIC_OUTPUT_TOKENS,
            "system": instructions,
            "messages": [{"role": "user", "content": query}],
            "tools": [declaration],
        }
    else:
        payload = {
            "model": model_id,
            "instructions": instructions,
            "input": [{"role": "user", "content": query}],
            "tools": [declaration],
            # Forced, as the call probe forces it: a model answering from memory runs no search.
            "tool_choice": "required",
            "store": False,
            "max_output_tokens": _RESPONSES_OUTPUT_TOKENS,
        }
        if spec.include:
            payload["include"] = list(spec.include)
    return ModelHubRequest(payload, protocol=source.protocol)


# Renders one of this module's failure keys (``modelHub.hostedTools.*``) in the user's language.
Say = Callable[..., str]


def failed_record(error: str, sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"status": "failed", "error": bounded_diagnostic_detail(error) or error[:400], "sources": sources or []}


@dataclass
class ResponsesReadout:
    """What a streamed Responses executor call reported: its output items and its final response.

    Error envelopes are not read here: the engine client's one extractor owns
    upstream error codes, and the call's outcome carries them.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    response: dict[str, Any] | None = None


class ResponsesStreamReader:
    """Read a Responses event stream into its output items.

    Each line and event keeps the SSE parser's default bound, and so is the
    whole stream.
    """

    def __init__(self) -> None:
        self._parser = SSEParser()
        self._read = 0
        self.readout = ResponsesReadout()

    def feed(self, chunk: bytes) -> None:
        self._read += len(chunk)
        if self._read > EXECUTOR_RESPONSE_MAX_BYTES:
            raise ExecutorResponseRefused("modelHub.hostedTools.tooLarge")
        try:
            events = self._parser.feed(chunk)
        except ValueError:
            # A line or event over its cap.
            raise ExecutorResponseRefused("modelHub.hostedTools.tooLarge") from None
        for event in events:
            self._event(event.data)

    def _event(self, data: str) -> None:
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            return
        except RecursionError:
            raise ExecutorResponseRefused("modelHub.hostedTools.unreadable") from None
        kind = payload.get("type") if isinstance(payload, dict) else None
        if not isinstance(kind, str):
            return
        item = payload.get("item")
        if kind == "response.output_item.done" and isinstance(item, dict):
            self.readout.items.append(item)
        elif kind in {"response.completed", "response.incomplete", "response.failed"} and isinstance(
            payload.get("response"), dict
        ):
            response = dict(payload["response"])
            output = response.pop("output", None)
            # The terminal output repeats the streamed items; only the items the
            # stream did not carry are taken from it.
            seen = {item.get("id") for item in self.readout.items if isinstance(item.get("id"), str)}
            streamed = bool(self.readout.items)
            for adopted in output if isinstance(output, list) else ():
                if not isinstance(adopted, dict):
                    continue
                ident = adopted.get("id")
                if (ident in seen) if isinstance(ident, str) else streamed:
                    # A repeat, or an item without an id the stream may already hold.
                    continue
                self.readout.items.append(adopted)
            self.readout.response = response


# --- results ---------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorResult:
    """The record for the call.

    ``usage`` is the response's own ``usage`` section, as the provider sent it,
    which the call's cost is read from. ``ran`` says whether the response ran
    the tool at all: one that never did is a model's choice, which ends the
    chain rather than moving on (§6).
    """

    record: dict[str, Any]
    usage: Mapping[str, Any] | None = None
    ran: bool = True


def provider_charges(protocol: str, usage: Mapping[str, Any] | None) -> tuple[dict[str, int], float | None]:
    """What a response's provider reported charging for: its hosted counters and, from xAI, its own cost."""

    if usage is None:
        return {}, None
    counters = usage.get("server_tool_use") if protocol == "anthropic" else usage.get("server_side_tool_usage_details")
    return reported_counts(counters), usd_from_xai_ticks(usage.get("cost_in_usd_ticks"))


def _usage_section(response: Any) -> Mapping[str, Any] | None:
    usage = response.get("usage") if isinstance(response, Mapping) else None
    return usage if isinstance(usage, Mapping) else None


def responses_result(tool: str, readout: ResponsesReadout, say: Say, *, failure: str | None = None) -> ExecutorResult:
    """The canonical record of one streamed Responses executor call.

    ``failure`` is how the call failed, from its outcome, when it did; it takes
    precedence over anything the stream emitted, whose sources a failed record
    keeps. A call the provider marked failed is failed too.
    """

    return replace(_responses_record(tool, readout, say, failure), usage=_usage_section(readout.response))


def _responses_record(tool: str, readout: ResponsesReadout, say: Say, failure: str | None) -> ExecutorResult:
    response = readout.response or {}
    calls = [item for item in readout.items if _responses_call_tool(item) == tool]
    if not calls:
        return ExecutorResult(failed_record(failure or _ended_without(tool, response, say)), ran=False)
    texts = [
        part
        for item in readout.items
        if item.get("type") == "message"
        for part in (item.get("content") if isinstance(item.get("content"), list) else ())
        if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
    ]
    cited = _cited_sources(texts, x_posts_only=tool == "x_search")
    sources = _merge_sources(cited, [] if tool == "x_search" else _call_sources(calls))
    if failure is not None:
        return ExecutorResult(failed_record(failure, sources))
    if any(call.get("status") == "failed" for call in calls):
        return ExecutorResult(failed_record(say("modelHub.hostedTools.callFailed", tool=tool), sources))
    return ExecutorResult(_settled(response, sources, say))


def _settled(response: Mapping[str, Any], sources: list[dict[str, Any]], say: Say) -> dict[str, Any]:
    """Success is an allow-list: only a response that says it completed completes the call.

    One that stopped early (output cap) is incomplete, and keeps what it found;
    any other or missing ending is a failure.
    """

    status = _text_field(response, "status")
    if status == "completed":
        return {"status": "completed", "sources": sources}
    ended = _ended_without("", response, say) if status in {"failed", "incomplete"} else say(
        "modelHub.hostedTools.responseEnded", status=str(status or "unknown"),
    )
    if status == "incomplete":
        return {"status": "incomplete", "error": failed_record(ended)["error"], "sources": sources}
    return failed_record(ended, sources)


def _text_field(item: Mapping[str, Any], key: str) -> str | None:
    """A provider field read as text; anything else is absent."""

    value = item.get(key)
    return value if isinstance(value, str) else None


def _responses_call_tool(item: Mapping[str, Any]) -> str | None:
    return responses_item_tool(_text_field(item, "type"), _text_field(item, "name"))


def _ended_without(tool: str, response: Mapping[str, Any], say: Say) -> str:
    status = _text_field(response, "status")
    if status in {"failed", "incomplete"}:
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        text = say("modelHub.hostedTools.responseEnded", status=status)
        return f"{text} ({reason})" if isinstance(reason, str) and reason else text
    return say("modelHub.hostedTools.didNotRun", tool=tool)


def _call_sources(calls: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sources = []
    for call in calls:
        action = call.get("action")
        for source in action.get("sources", ()) if isinstance(action, dict) else ():
            if isinstance(source, dict) and isinstance(source.get("url"), str) and source["url"]:
                sources.append({"url": source["url"]})
    return sources


def _cited_sources(parts: list[Mapping[str, Any]], *, x_posts_only: bool) -> list[dict[str, Any]]:
    """Each cited URL once, in citation order, with the sentence its span sits in as the snippet."""

    sources: list[dict[str, Any]] = []
    budget = _MAX_CITATIONS
    for part in parts:
        text = part["text"]
        citations = [
            note
            for note in (part.get("annotations") if isinstance(part.get("annotations"), list) else ())
            if isinstance(note, dict) and note.get("type") == "url_citation"
            and isinstance(note.get("url"), str) and note["url"]
        ][:budget]
        budget -= len(citations)
        spans = sorted(
            (note["start_index"], note["end_index"], note["url"])
            for note in citations
            if _valid_span(note.get("start_index"), note.get("end_index"), len(text))
        )
        for note in citations:
            url = note["url"]
            if x_posts_only and not _is_x_post(url):
                continue
            source: dict[str, Any] = {"url": url}
            title = note.get("title")
            # xAI numbers its citations ("1", "2"); a number is not a title.
            if isinstance(title, str) and title.strip() and not title.strip().isdigit():
                source["title"] = title.strip()
            start, end = note.get("start_index"), note.get("end_index")
            if _valid_span(start, end, len(text)):
                snippet = excerpt(text, start, end, url, spans)
                if snippet:
                    source["snippet"] = snippet
            sources.append(source)
    return sources


def _valid_span(start: Any, end: Any, length: int) -> bool:
    return (
        isinstance(start, int) and isinstance(end, int)
        and not isinstance(start, bool) and not isinstance(end, bool)
        and 0 <= start < end <= length
    )


def _is_x_post(url: str) -> bool:
    match = re.match(r"https?://([^/]+)/", url)
    return match is not None and match.group(1).lower() in _X_POST_HOSTS


def _merge_sources(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for source in group:
            known = merged.setdefault(source["url"], {"url": source["url"]})
            for key, value in source.items():
                known.setdefault(key, value)
    return list(merged.values())


_SENTENCE_END = re.compile(r"[.!?。！？](?=\s|$)")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def excerpt(text: str, start: int, end: int, url: str, spans: list[tuple[int, int, str]]) -> str | None:
    """The answer text a citation span belongs to, without citation markers (``vibey-hosted-tools.md`` §12 item 24).

    ``spans`` are the text's citation spans, in text order.

    The executor asks for one list item per result, each ending in its
    citation, so a span on a line that cites only its own URL takes that whole
    line. On a line that cites several sources, it takes its sentence: the one
    it sits in, or the one it follows (``It rose 5%. ([site](url))``). Every
    citation span, a leading list marker and bold markers are removed.
    """

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    on_line = [span for span in spans if span[0] >= line_start and span[1] <= line_end]
    if all(span[2] == url for span in on_line):
        region_start, region_end = line_start, line_end
    else:
        region_start, region_end = _sentence(text, start, end, line_start, line_end)
    pieces, cursor = [], region_start
    for span_start, span_end, _url in spans:
        if span_end <= region_start or span_start >= region_end:
            continue
        pieces.append(text[cursor:max(cursor, span_start)])
        cursor = max(cursor, span_end)
    if cursor < region_end:
        pieces.append(text[cursor:region_end])
    snippet = " ".join(_LIST_MARKER.sub("", "".join(pieces), count=1).replace("**", "").split())
    snippet = re.sub(r"\s+([.,;:!?])", r"\1", snippet)
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[: _SNIPPET_CHARS - 1].rstrip() + "…"
    return snippet or None


def _sentence(text: str, start: int, end: int, line_start: int, line_end: int) -> tuple[int, int]:
    """The sentence of a line a citation span belongs to: the one it sits in, or the one it follows."""

    before = text[line_start:start].rstrip()
    trailing = bool(before) and not _LIST_MARKER.fullmatch(before) and before[-1] in ".!?。！？"
    anchor = line_start + len(before) - 1 if trailing else start
    sentence_start = line_start
    for match in _SENTENCE_END.finditer(text, line_start, anchor):
        sentence_start = match.end()
    if trailing:
        return sentence_start, line_start + len(before)
    found = _SENTENCE_END.search(text, end, line_end)
    return sentence_start, found.end() if found is not None else line_end


def anthropic_result(body: bytes | bytearray, say: Say) -> ExecutorResult:
    """The canonical record of one buffered Anthropic web search executor call; parse it off the loop."""

    try:
        message = json.loads(body)
    except (ValueError, RecursionError):
        return ExecutorResult(failed_record(say("modelHub.hostedTools.unreadable")))
    return replace(_anthropic_record(message, say), usage=_usage_section(message))


def _anthropic_record(message: Any, say: Say) -> ExecutorResult:
    content = message.get("content") if isinstance(message, dict) else None
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    if not any(block.get("type") == "server_tool_use" and block.get("name") == "web_search" for block in blocks):
        return ExecutorResult(failed_record(say("modelHub.hostedTools.didNotRun", tool="web_search")), ran=False)
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for block in blocks:
        if block.get("type") != "web_search_tool_result":
            continue
        found = block.get("content")
        if isinstance(found, dict):
            errors.append(str(found.get("error_code") or "unknown"))
            continue
        if not isinstance(found, list):
            # Only a list, possibly empty, is a result; anything else is not an answer.
            return ExecutorResult(failed_record(say("modelHub.hostedTools.unreadable")))
        for result in found:
            if isinstance(result, dict) and isinstance(result.get("url"), str) and result["url"]:
                source = {"url": result["url"]}
                for key in ("title", "page_age"):
                    if isinstance(result.get(key), str) and result[key]:
                        source[key] = result[key]
                results.append(source)
    cited = [
        {"url": citation["url"], "snippet": " ".join(citation["cited_text"].split())}
        for block in blocks
        if block.get("type") == "text"
        for citation in (block.get("citations") if isinstance(block.get("citations"), list) else ())
        if isinstance(citation, dict)
        and isinstance(citation.get("url"), str)
        and citation["url"]
        and isinstance(citation.get("cited_text"), str)
        and citation["cited_text"].strip()
    ][:_MAX_CITATIONS]
    # Results keep the provider's order; a cited result gains the text it was cited for.
    sources = _merge_sources(results, cited)
    if errors:
        # Any search the provider marked failed fails the call, as on Responses;
        # the results that did arrive stay as partial sources.
        text = say("modelHub.hostedTools.callFailed", tool="web_search")
        return ExecutorResult(failed_record(f"{text} ({', '.join(errors)})", sources))
    # Success is an allow-list here too: an early stop is incomplete, anything else unknown fails.
    stop = _text_field(message, "stop_reason")
    if stop in {"end_turn", "stop_sequence"}:
        uses = [
            block.get("id") for block in blocks
            if block.get("type") == "server_tool_use" and block.get("name") == "web_search"
        ]
        answered = [block.get("tool_use_id") for block in blocks if block.get("type") == "web_search_tool_result"]
        if not all(isinstance(ident, str) and ident for ident in (*uses, *answered)) or not set(uses) <= set(answered):
            # A finished response must carry every search's result block, even an empty one.
            return ExecutorResult(failed_record(say("modelHub.hostedTools.unreadable"), sources))
        return ExecutorResult({"status": "completed", "sources": sources})
    ended = say("modelHub.hostedTools.responseEnded", status=str(stop or "unknown"))
    if stop in {"max_tokens", "pause_turn"}:
        return ExecutorResult({"status": "incomplete", "error": failed_record(ended)["error"], "sources": sources})
    return ExecutorResult(failed_record(ended, sources))
