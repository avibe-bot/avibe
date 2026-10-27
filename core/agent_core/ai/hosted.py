"""Hosted tool calls on the wire and in portable history (``vibey-hosted-tools.md`` §4.2, §4.4, §5).

Two directions, one owner each:

* **Parsing.** Every item a provider emits for a hosted tool the request declared natively becomes exactly one
  ``HostedToolBlock`` with a provider-neutral action, its per-call ``HostedRecord``, and the provider's own items
  verbatim in ``native.payload``. An action this module does not know becomes ``other`` and is kept.
* **Rendering.** ``record_text`` is the one function that turns a record into model-facing text: a synthetic replay
  result, the in-place text of an in-flight call, and (with the proxy executor) a proxied call's result.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence
from urllib.parse import urlsplit

from core.agent_core.ai.errors import redact_provider_text
from core.agent_core.messages import (
    HOSTED_ERROR_MAX_CHARS,
    MAX_HOSTED_COUNT,
    AssistantMessage,
    Citation,
    HostedAction,
    HostedCost,
    HostedImage,
    HostedNative,
    HostedRecord,
    WebFetchRecord,
    HostedSource,
    HostedToolBlock,
    ToolCallBlock,
)
from core.agent_core.tools.read import MAX_INLINE_IMAGE_BYTES
from core.agent_core.tools.truncate import format_size

#: xAI reports a response's cost in ticks of 1e-10 USD (``usage.cost_in_usd_ticks``). Past the bound (about $100M
#: for one response) a reported figure is not a cost.
_TICKS_PER_USD = 10_000_000_000
_MAX_TICKS = 10**18
_X_HOSTS = frozenset({"x.com", "twitter.com"})
#: The counter names a summary holds (``message.schema.json``); a provider's other keys are not counters.
_COUNTER_NAME = re.compile(r"[a-z0-9_]{1,64}")
#: The most counters a hosted summary keeps, and the most entries of a usage section read to find them.
MAX_HOSTED_COUNTERS = 32
_COUNTERS_SCANNED = 256
_X_SEARCH_VARIANTS = {"x_keyword_search": "keyword", "x_semantic_search": "semantic"}
#: Where an ``x_thread_fetch`` call names its thread, in the order tried.
_THREAD_REFS = ("ref", "post_id", "thread_id", "conversation_id", "url", "id")


# --- parsing -------------------------------------------------------------------------------------------------


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def failure(reason: Any, code: str | None = None) -> tuple[str, str | None]:
    """A failed record's ``error`` and ``error_code``: the provider's own words, with no code, or the English reason
    Avibe wrote for ``code`` (``HOSTED_ERROR_CODES``). The text has any credential redacted and is never longer than
    the record allows; it is persisted and replayed to later models. A reason that says nothing is
    ``provider_no_reason``."""

    message = " ".join(redact_provider_text(reason).split()) if isinstance(reason, str) else ""
    if not message:
        return "the provider reported no reason", "provider_no_reason"
    return (message if len(message) <= HOSTED_ERROR_MAX_CHARS else message[: HOSTED_ERROR_MAX_CHARS - 1] + "…"), code


def _item_error(item: Mapping[str, Any], status: str) -> tuple[str, str | None]:
    error = item.get("error")
    if isinstance(error, Mapping):
        error = error.get("message") or error.get("code") or error.get("type")
    if isinstance(error, str):
        return failure(error)
    return failure(f"the provider ended the call {status}", "provider_no_reason")


def _payload(*items: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(json.dumps(item)) for item in items)


def responses_hosted_tool(item: Mapping[str, Any]) -> Optional[str]:
    """The catalog tool a Responses output item belongs to, or None for an item that is not a hosted call."""

    kind = item.get("type")
    if kind == "web_search_call":
        return "web_search"
    if kind == "custom_tool_call" and str(item.get("name") or "").startswith("x_"):
        return "x_search"
    return None


def responses_hosted_block(
    item: Mapping[str, Any],
    *,
    source_id: str,
    call_id: str,
    finished: bool = True,
) -> HostedToolBlock:
    """One Responses hosted-call item (a ``web_search_call``, or xAI's X-search ``custom_tool_call``) as its block.

    ``call_id`` names the block when the item carries no id of its own. An item that has only been added
    (``finished`` false) holds the call's place in the output order as a pending call. An ``image_generation_call``
    is no hosted item: Vibey never declares a native image tool (§4.2).
    """

    tool = responses_hosted_tool(item)
    if tool is None:
        raise ValueError(f"not a hosted call item: {item.get('type')!r}")
    status = item.get("status")
    if not finished:
        record_status = "failed" if status == "failed" else "pending"
    elif status in ("completed", "failed", "incomplete"):
        record_status = status
    elif status in (None, "") and tool == "x_search":
        # xAI's X-search calls carry no output item: the call is complete when its item is.
        record_status = "completed"
    else:
        record_status = "pending"
    error, error_code = _item_error(item, record_status) if record_status in ("failed", "incomplete") else (None, None)
    if tool == "web_search":
        action, sources = _web_search_action(item)
    else:
        action, sources = _x_search_action(item), ()
    return HostedToolBlock(
        id=_text(item.get("id")) or _text(item.get("call_id")) or call_id,
        tool=tool,  # type: ignore[arg-type]
        action=action,
        record=HostedRecord(status=record_status, error=error, sources=sources, error_code=error_code),  # type: ignore[arg-type]
        native=HostedNative(source_id=source_id, payload=_payload(item)),
    )


def generate_action(item: Mapping[str, Any]) -> HostedAction:
    """An ``image_generate`` call as it ran: the image model, the prompt the endpoint rendered (its revision of the
    model's), and the output's resolved size and quality, which pricing reads (§7.3). ``item`` is the executor's
    ``generation`` facts."""

    return HostedAction(
        "generate",
        prompt=_text(item.get("revised_prompt")),
        size=_text(item.get("size")),
        quality=_text(item.get("quality")),
        model=_text(item.get("model")),
    )


def image_dimensions(size: Any) -> tuple[int | None, int | None]:
    """An output's ``WIDTHxHEIGHT`` size as two numbers, or nothing for ``auto`` or a size that is not one."""

    width, separator, height = str(size or "").partition("x")
    if separator and width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0:
        return int(width), int(height)
    return None, None


def _web_search_action(item: Mapping[str, Any]) -> tuple[HostedAction, tuple[HostedSource, ...]]:
    action = item.get("action")
    action = action if isinstance(action, Mapping) else {}
    kind = action.get("type")
    sources = tuple(
        HostedSource(url=source["url"], title=_text(source.get("title")))
        for source in action.get("sources") or ()
        if isinstance(source, Mapping) and _text(source.get("url"))
    )
    if kind == "search":
        queries = [query for query in action.get("queries") or () if _text(query)]
        if not queries and _text(action.get("query")):
            queries = [action["query"]]
        if queries:
            return HostedAction("search", queries=tuple(queries)), sources
    elif kind == "open_page" and _text(action.get("url")):
        return HostedAction("open_page", url=action["url"]), sources
    elif kind == "find_in_page" and _text(action.get("url")) and _text(action.get("pattern")):
        return HostedAction("find_in_page", url=action["url"], pattern=action["pattern"]), sources
    return HostedAction("other", name=_text(kind) or "web_search_call"), sources


def _x_search_action(item: Mapping[str, Any]) -> HostedAction:
    name = str(item.get("name"))
    try:
        arguments = json.loads(item.get("input") or "{}")
    except (TypeError, ValueError):
        arguments = None
    if not isinstance(arguments, Mapping):
        return HostedAction("other", name=name)
    query = _text(arguments.get("query"))
    if name in _X_SEARCH_VARIANTS and query:
        limit = arguments.get("limit")
        if isinstance(limit, str) and limit.isdigit():
            limit = int(limit)
        mode = arguments.get("mode")
        return HostedAction(
            "x_search",
            variant=_X_SEARCH_VARIANTS[name],
            query=query,
            limit=limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else None,
            mode=mode if mode in ("Latest", "Top") else None,
        )
    if name == "x_user_search" and query:
        return HostedAction("x_user_search", query=query)
    if name == "x_thread_fetch":
        ref = next((str(arguments[key]) for key in _THREAD_REFS if _text(str(arguments.get(key) or ""))), None)
        if ref:
            return HostedAction("x_thread_fetch", ref=ref)
    return HostedAction("other", name=name)


def anthropic_hosted_block(
    use: Mapping[str, Any], result: Optional[Mapping[str, Any]], *, source_id: str
) -> HostedToolBlock:
    """An Anthropic ``server_tool_use`` named ``web_search`` and its ``web_search_tool_result``, once one arrived."""

    query = _text((use.get("input") or {}).get("query")) if isinstance(use.get("input"), Mapping) else None
    action = HostedAction("search", queries=(query,)) if query else HostedAction("other", name="web_search")
    if result is None:
        record = HostedRecord(status="pending")
    else:
        content = result.get("content")
        if isinstance(content, list):
            record = HostedRecord(
                status="completed",
                sources=tuple(
                    HostedSource(url=hit["url"], title=_text(hit.get("title")), page_age=_text(hit.get("page_age")))
                    for hit in content
                    if isinstance(hit, Mapping) and _text(hit.get("url"))
                ),
            )
        else:
            # The provider's own error code is its reason, in its words.
            error, error_code = failure(content.get("error_code") if isinstance(content, Mapping) else None)
            record = HostedRecord(status="failed", error=error, error_code=error_code)
    payload = (use,) if result is None else (use, result)
    return HostedToolBlock(
        id=str(use["id"]),
        tool="web_search",
        action=action,
        record=record,
        native=HostedNative(source_id=source_id, payload=_payload(*payload)),
    )


#: Gemini's server-side tool type for a Google Search it ran.
GOOGLE_SEARCH_TOOL_TYPE = "GOOGLE_SEARCH_WEB"


def google_hosted_block(
    call: Mapping[str, Any],
    response: Optional[Mapping[str, Any]],
    *,
    source_id: str,
    call_id: str,
    sources: tuple[HostedSource, ...] = (),
) -> HostedToolBlock:
    """A Gemini Google Search: its ``toolCall`` part and, once one arrived, its ``toolResponse`` part, both verbatim
    with their signatures. ``sources`` are the response's grounding chunks, which Gemini reports once per response."""

    tool_call = call.get("toolCall") if isinstance(call.get("toolCall"), Mapping) else {}
    arguments = tool_call.get("args") if isinstance(tool_call.get("args"), Mapping) else {}
    raw_queries = arguments.get("queries")
    queries = tuple(query for query in raw_queries if _text(query)) if isinstance(raw_queries, list) else ()
    action = HostedAction("search", queries=queries) if queries else HostedAction("other", name="google_search")
    record = HostedRecord(status="pending") if response is None else HostedRecord(status="completed", sources=sources)
    return HostedToolBlock(
        id=_text(tool_call.get("id")) or call_id,
        tool="web_search",
        action=action,
        record=record,
        native=HostedNative(source_id=source_id, payload=_payload(call) if response is None else _payload(call, response)),
    )


def google_grounding_sources(metadata: Any) -> tuple[HostedSource, ...]:
    """The web sources a Gemini response's ``groundingMetadata`` lists, in its order, each once."""

    chunks = metadata.get("groundingChunks") if isinstance(metadata, Mapping) else None
    sources: dict[str, HostedSource] = {}
    for chunk in chunks if isinstance(chunks, list) else ():
        web = chunk.get("web") if isinstance(chunk, Mapping) else None
        url = _text(web.get("uri")) if isinstance(web, Mapping) else None
        if url is not None and url not in sources:
            sources[url] = HostedSource(url=url, title=_text(web.get("title")))
    return tuple(sources.values())


def google_search_count(calls: Iterable[Mapping[str, Any]]) -> int:
    """The searches Google bills a Gemini response for: the distinct queries each of its search ``toolCall`` parts
    ran, at least one per call.

    The response that ran them says so; ``groundingMetadata.webSearchQueries`` comes with the grounded answer, often
    a later response of the turn, and lists only the queries that grounded it (measured 2026-10-27).
    """

    count = 0
    for part in calls:
        call = part.get("toolCall") if isinstance(part, Mapping) else None
        raw = call.get("args", {}).get("queries") if isinstance(call, Mapping) and isinstance(call.get("args"), Mapping) else None
        queries = {query.strip() for query in raw if isinstance(query, str) and query.strip()} if isinstance(raw, list) else set()
        count += max(1, len(queries))
    return count


def google_grounding_citations(metadata: Any) -> list[tuple[Optional[str], tuple[Citation, ...]]]:
    """Each ``groundingSupports`` entry of a Gemini answer: the text it grounds (``segment.text``, or None) and a
    citation, without a range, for every web chunk it names."""

    if not isinstance(metadata, Mapping):
        return []
    chunks = metadata.get("groundingChunks") if isinstance(metadata.get("groundingChunks"), list) else []
    supports = metadata.get("groundingSupports") if isinstance(metadata.get("groundingSupports"), list) else []
    cited = []
    for support in supports:
        if not isinstance(support, Mapping) or not isinstance(support.get("groundingChunkIndices"), list):
            continue
        segment = support.get("segment")
        text = _text(segment.get("text")) if isinstance(segment, Mapping) else None
        items = []
        for number in support["groundingChunkIndices"]:
            if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number < len(chunks):
                continue
            web = chunks[number].get("web") if isinstance(chunks[number], Mapping) else None
            url = _text(web.get("uri")) if isinstance(web, Mapping) else None
            if url is not None:
                items.append(Citation(url=url, title=_text(web.get("title"))))
        if items:
            cited.append((text, tuple(items)))
    return cited


def citation(raw: Mapping[str, Any], *, offset: int = 0) -> Optional[Citation]:
    """A provider citation on assistant text: an OpenAI/xAI ``url_citation`` with its range, shifted by ``offset``
    into the block's text, or an Anthropic ``web_search_result_location``, which covers its whole text block."""

    url = _text(raw.get("url"))
    if url is None:
        return None
    title = _text(raw.get("title"))
    # xAI titles a citation with its number ("1", "2"): that is no title.
    title = None if title is not None and title.isdigit() else title
    start, end = raw.get("start_index"), raw.get("end_index")
    if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool) and 0 <= start <= end:
        return Citation(url=url, title=title, start=offset + start, end=offset + end)
    return Citation(url=url, title=title, cited_text=_text(raw.get("cited_text")))


def x_cited_sources(citations: Iterable[Citation]) -> tuple[HostedSource, ...]:
    """The posts an X-search answer cites (§4.2 ``cited_sources``): its citations to X, once each, in first-seen order."""

    seen: dict[str, HostedSource] = {}
    for item in citations:
        try:
            host = (urlsplit(item.url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue  # a URL that does not parse cites no post
        if host in _X_HOSTS and item.url not in seen:
            seen[item.url] = HostedSource(url=item.url, title=item.title)
    return tuple(seen.values())


def reported_counts(*sections: Any, bound: Optional[int] = MAX_HOSTED_COUNT) -> dict[str, int]:
    """Provider-reported hosted counters (xAI ``server_side_tool_usage_details``, Anthropic ``server_tool_use``),
    keeping the ones that counted something. A count past ``bound`` is refused here, where counters enter, so it reads
    as absent to everything that prices or keeps them. A caller that adds responses together reads them unbounded and
    bounds the total, so a counter past the bound in one response stays past it."""

    counts: dict[str, int] = {}
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        # A summary holds a small fixed set of counters: the cap holds while collecting, over a bounded scan.
        for scanned, (name, value) in enumerate(section.items()):
            if len(counts) >= MAX_HOSTED_COUNTERS or scanned >= _COUNTERS_SCANNED:
                break
            counted = isinstance(value, int) and not isinstance(value, bool) and value > 0
            if counted and bound is not None:
                counted = value <= bound
            if counted and isinstance(name, str) and _COUNTER_NAME.fullmatch(name):
                counts[name] = value
    return counts


def reported_cost(usage: Any) -> Optional[HostedCost]:
    """The response's cost when the provider reports one (xAI ``cost_in_usd_ticks``); never a priced guess.

    Vibey's native call and the gateway's proxied one both read it here. A figure that is not a whole number of ticks
    within the bound is no figure.
    """

    ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, Mapping) else None
    if isinstance(ticks, int) and not isinstance(ticks, bool) and 0 <= ticks <= _MAX_TICKS:
        return HostedCost(basis="reported", usd=ticks / _TICKS_PER_USD)
    return None


# --- rendering (§3.4, §4.4, §5) --------------------------------------------------------------------------------


def record_text(
    tool: str,
    record: HostedRecord | WebFetchRecord,
    *,
    query: Optional[str],
    further_queries: Sequence[str] = (),
    notes: Sequence[str] = (),
    cited: Sequence[HostedSource] = (),
) -> str:
    """The model-facing text of one finished hosted call: its status line first, then everything it carries.

    A failed or incomplete record says so first (``[<tool> failed: <error>]``), so no protocol without an error flag
    can read it as an empty success. Whatever the status, every line attached to the call follows: further queries,
    folded actions, the sources it found (titles, links, excerpts and ages) and the posts its answer cites.
    """

    if isinstance(record, WebFetchRecord):
        from core.agent_core.web_fetch import frame_fetch_record
        record.check_tool(tool)
        return frame_fetch_record(record)
    if record.status in ("failed", "incomplete"):
        lines = [f"[{tool} failed: {record.error}]"]
        if query:
            lines.append(f"Query: {json.dumps(query, ensure_ascii=False)}")
    else:
        lines = [f"[{tool}: {json.dumps(query, ensure_ascii=False)}]" if query else f"[{tool}]"]
    if further_queries:
        lines.append("Also searched: " + "; ".join(json.dumps(item, ensure_ascii=False) for item in further_queries))
    lines.extend(notes)
    lines.extend(_numbered(record.sources))
    for image in record.images or ():
        lines.extend(_image_lines(image))
    if cited:
        lines.append("Posts cited in the answer:")
        lines.extend(_numbered(cited))
    if record.status == "completed" and not record.sources and not record.images and not cited and not notes:
        lines.append("No results were returned.")
    return "\n".join(lines)


def _image_lines(image: HostedImage) -> list[str]:
    """A generated image as the model sees it: where it is saved and what it is, never the image itself (§7.3). The
    model opens the file with ``read`` to look at it, unless it is over ``read``'s inline limit."""

    facts = [f"{image.width}×{image.height}" if image.width and image.height else None, image.mime]
    if image.bytes is not None:
        facts.append(format_size(image.bytes))
    lines = [f"Image saved: {image.path} ({', '.join(fact for fact in facts if fact)})"]
    if image.bytes is not None and image.bytes > MAX_INLINE_IMAGE_BYTES:
        lines.append(
            f"   It is over the {format_size(MAX_INLINE_IMAGE_BYTES)} inline image limit, so read cannot show it. "
            "The user sees it."
        )
    return lines


def _numbered(sources: Sequence[HostedSource]) -> list[str]:
    lines: list[str] = []
    for number, source in enumerate(sources, 1):
        lines.append(f"{number}. {source.title or source.url}")
        if source.title:
            lines.append(f"   {source.url}")
        if source.snippet:
            lines.append(f"   {source.snippet}")
        if source.page_age:
            lines.append(f"   Published: {source.page_age}")
    return lines


def action_note(action: HostedAction) -> str:
    """A non-search action as a line in the result of its tool's preceding call."""

    if action.type == "open_page":
        return f"Opened {action.url}"
    if action.type == "find_in_page":
        return f"Searched {action.url} for {json.dumps(action.pattern, ensure_ascii=False)}"
    if action.type == "x_thread_fetch":
        return f"Fetched thread {action.ref}"
    return f"Ran {action.name or action.type}"


def call_arguments(tool: str, action: HostedAction) -> Optional[dict[str, Any]]:
    """The synthetic same-named call's arguments for a calling action; None for a non-search action, which folds
    into its tool's preceding call."""

    if action.type == "search":
        return {"query": (action.queries or ("",))[0]}
    if action.type in ("x_search", "x_user_search"):
        arguments: dict[str, Any] = {"query": action.query}
        if action.limit is not None:
            arguments["limit"] = action.limit
        if action.mode is not None:
            arguments["mode"] = action.mode
        return arguments
    if action.type == "generate":
        generated: dict[str, Any] = {"prompt": action.prompt or ""}
        if action.size is not None:
            generated["size"] = action.size
        return generated
    return None


def standalone_query(action: HostedAction) -> str:
    """A non-search action with no preceding call of its tool becomes its own call with this query."""

    target = action.url or action.ref or action.name or ""
    return f"{action.type} {target}".strip()


def _unit(block: HostedToolBlock) -> tuple[dict[str, Any], tuple[str, ...]]:
    """A hosted call's synthetic arguments, and the line a completed non-search action shows (its query names it)."""

    arguments = call_arguments(block.tool, block.action)
    if arguments is None:
        done = block.record.status == "completed"
        return {"query": standalone_query(block.action)}, (action_note(block.action),) if done else ()
    return arguments, ()


def _unit_text(block: HostedToolBlock, *, cited: Sequence[HostedSource], pending: str) -> str:
    """The text one hosted call goes back as, in either portable form: ``pending`` for a call cut off with its turn,
    else its record with every line attached to it."""

    if block.record.status == "pending":
        return pending
    arguments, notes = _unit(block)
    query = arguments.get("query") or arguments.get("prompt")
    further = (block.action.queries or ())[1:]
    return record_text(block.tool, block.record, query=query, further_queries=further, notes=notes, cited=cited)


def in_place_text(block: HostedToolBlock, *, cited: Sequence[HostedSource] = ()) -> str:
    """A hosted call replaced in place by one text block (§4.4); its status always shows."""

    arguments, _ = _unit(block)
    query = arguments.get("query") or arguments.get("prompt")
    return _unit_text(block, cited=cited, pending=f"[{block.tool}: {json.dumps(query, ensure_ascii=False)}] (no result)")


class PortableHosted(NamedTuple):
    """How one hosted call goes back in portable form: a synthetic same-named ``call`` with its result, or
    ``in_place`` as one text block."""

    kind: str
    name: str = ""
    arguments: Mapping[str, Any] = {}
    text: str = ""
    is_error: bool = False


def portable_hosted(
    message: AssistantMessage, *, in_flight: bool, interrupted: str, parallel_group: bool = False
) -> dict[int, PortableHosted]:
    """Each hosted call of a replayed response in portable form (§4.4, §5), keyed by the block's identity.

    Every call is exactly one unit with its own record:
    * in place as text, for a response its turn still waits on, and for any call that follows a client tool call of
      the same response (a split there would separate that call from its result);
    * otherwise a synthetic same-named call and its result.

    The posts the answer cites are listed once, on its first call that was not cut off, whichever form it takes.
    ``interrupted`` is the result of a synthetic call cut off with its turn. A deferred mixed ``parallel_group``
    is rendered intact by the caller, so calls after a client call can also be synthetic without splitting it.
    """

    blocks = [block for block in message.content if isinstance(block, HostedToolBlock)]
    summary = message.hosted_summary
    owner = next((block for block in blocks if block.record.status != "pending"), None)
    plan: dict[int, PortableHosted] = {}
    in_place = in_flight
    for block in message.content:
        if not isinstance(block, HostedToolBlock):
            in_place = in_place or (not parallel_group and isinstance(block, ToolCallBlock))
            continue
        cited = summary.cited_sources if summary is not None and block is owner else ()
        if in_place:
            plan[id(block)] = PortableHosted("in_place", text=in_place_text(block, cited=cited))
            continue
        arguments, _ = _unit(block)
        failed = block.record.status in ("failed", "incomplete", "pending")
        text = _unit_text(block, cited=cited, pending=interrupted)
        plan[id(block)] = PortableHosted("call", block.tool, arguments, text, failed)
    return plan


def portable_sizes(message: AssistantMessage, *, interrupted: str) -> dict[int, int]:
    """What each hosted call of ``message`` costs on the wire in UTF-8 bytes, by the block's identity: the larger of
    its two portable forms, so an estimate holds wherever the message sits in a request."""

    sizes: dict[int, int] = {}
    for in_flight in (False, True):
        for key, unit in portable_hosted(message, in_flight=in_flight, interrupted=interrupted).items():
            arguments = json.dumps(dict(unit.arguments), ensure_ascii=False, separators=(",", ":")) if unit.kind == "call" else ""
            sizes[key] = max(sizes.get(key, 0), len((unit.name + arguments + unit.text).encode("utf-8")))
    return sizes
