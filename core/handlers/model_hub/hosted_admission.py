"""Which hops may receive a Vibey request's hosted-tool content (``vibey-hosted-tools.md`` §3.5).

The gateway never rewrites hosted content; it only decides where a request may
go. A hop may receive a hosted declaration only with positive ``call``
evidence for it, and a replayed native payload only with positive ``replay``
evidence, both taken on the current engine and probe shape. Evidence is read,
never written here.

Vibey pins a request carrying such content to one Source with
``x-avibe-pin-source``. A pinned request goes only to that Source, and every
way it cannot be served there before output becomes ``pin_unavailable``, after
which Vibey re-renders it in portable form. A pinned request's in-flight
response (a ``pause_turn`` continuation, or client tool results after server
tools in the same response) is the provider's own continuation contract, so
its hosted items need no replay evidence, only the identity that response was
served under: a turn's native content on a Source never crosses an identity
change (the turn's pin identity claim, made atomically at final admission).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Final, Mapping, Optional

from config.v2_config import ModelHubSourceConfig
from vibe.backend_model_catalog import HostedToolSpec, bundled_provider_overlay

from .capability_evidence import HOSTED_TOOL_ASPECTS
from .hosted_items import responses_item_tool
from .hosted_tools import HostedEvidence, hosted_tool_facts
from .request import declared_tool_types

PIN_SOURCE_HEADER: Final = "x-avibe-pin-source"
# The served-hop model a continuation was rendered for; without it a pin is the launch's primary hop.
PIN_MODEL_HEADER: Final = "x-avibe-pin-model"
PIN_UNAVAILABLE_CODE: Final = "pin_unavailable"

# Client items that may follow an in-flight response.
_RESPONSES_TOOL_OUTPUTS: Final = frozenset({"function_call_output", "custom_tool_call_output"})
# Items a Responses model emits. Only these form an in-flight response: anything
# else ends it, so an unknown item can never exempt a hosted call from evidence.
_RESPONSES_MODEL_OUTPUTS: Final = frozenset({
    "reasoning", "function_call", "custom_tool_call", "web_search_call", "image_generation_call",
    "file_search_call", "computer_call", "code_interpreter_call", "local_shell_call", "mcp_call",
    "mcp_list_tools", "mcp_approval_request",
})
# Output items identified by the tool they name; one without a textual name is malformed.
_RESPONSES_NAMED_CALLS: Final = frozenset({"function_call", "custom_tool_call", "mcp_call"})
_ANTHROPIC_SERVER_RESULTS: Final = {"web_search_tool_result": "web_search"}
# Gemini's server-side tool parts, by the catalog tool their ``toolType`` ran.
_GOOGLE_SERVER_TOOLS: Final = {"GOOGLE_SEARCH_WEB": "web_search"}


SpecLookup = Callable[[str, str, str, str], Optional[HostedToolSpec]]


@dataclass(frozen=True)
class HostedContent:
    """What a request's hosted content needs from a hop.

    ``calls`` holds each declared (catalog tool, declaration ``type``);
    ``replays`` the catalog tools whose native items it replays; ``in_flight``
    those whose native items it carries in the in-flight response.
    """

    calls: frozenset[tuple[str, str]] = frozenset()
    replays: frozenset[str] = frozenset()
    in_flight: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.calls or self.replays or self.in_flight)


@lru_cache(maxsize=1)
def _declaration_tools() -> Mapping[str, Mapping[str, str]]:
    """Protocol -> declaration ``type`` -> catalog tool, across every overlay vendor.

    A declaration is recognized by any vendor's type, not only the hop's, so a
    tool its own vendor does not offer (``x_search`` to OpenAI) is never sent.
    """

    tools: dict[str, dict[str, str]] = {}
    for families in bundled_provider_overlay().values():
        for family in families:
            for protocol, specs in family.hosted_tools.items():
                for tool, spec in specs.items():
                    tools.setdefault(protocol, {})[spec.declaration["type"]] = tool
    return tools


def hosted_content(request: Mapping[str, Any], protocol: str, *, pinned: bool) -> HostedContent:
    """The hosted declarations and replayed native items in a request body, by tool."""

    declared = _declaration_tools().get(protocol, {})
    calls = frozenset((declared[kind], kind) for kind in declared_tool_types(request) if kind in declared)
    if protocol == "openai_responses":
        replays, in_flight = _responses_replays(request.get("input"), pinned=pinned)
    elif protocol == "anthropic":
        replays, in_flight = _anthropic_replays(request.get("messages"), pinned=pinned)
    elif protocol == "google":
        replays, in_flight = _google_replays(request.get("contents"), pinned=pinned)
    else:
        replays, in_flight = frozenset(), frozenset()
    return HostedContent(calls, replays, in_flight)


def _text(item: Any, key: str) -> str | None:
    """A request field read as text with content; anything else, blank text, or a non-object item is absent."""

    value = item.get(key) if isinstance(item, Mapping) else None
    return value if isinstance(value, str) and value.strip() else None


def _responses_hosted_tool(item: Any) -> str | None:
    return responses_item_tool(_text(item, "type"), _text(item, "name"))


def _responses_output_item(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    if item.get("type") in (None, "message"):
        return item.get("role") == "assistant"
    kind = _text(item, "type")
    if kind in _RESPONSES_NAMED_CALLS and _text(item, "name") is None:
        # A named call without a textual name cannot be classified, so it ends the run like any unknown item.
        return False
    return kind in _RESPONSES_MODEL_OUTPUTS


def _responses_replays(items: Any, *, pinned: bool) -> tuple[frozenset[str], frozenset[str]]:
    """The tools of hosted items to replay, and of those in the in-flight response."""

    if not isinstance(items, list):
        return frozenset(), frozenset()
    end = len(items)
    if pinned:
        # The in-flight response is the trailing run of output items, followed
        # only by the client's tool outputs.
        while end and _text(items[end - 1], "type") in _RESPONSES_TOOL_OUTPUTS:
            end -= 1
        while end and _responses_output_item(items[end - 1]):
            end -= 1
    tools = {tool for item in items[:end] if (tool := _responses_hosted_tool(item)) is not None}
    # An image call is never replayed verbatim, in flight or not: Vibey has
    # replaced its result with the path record, so no hop may receive it.
    in_flight = {tool for item in items[end:] if (tool := _responses_hosted_tool(item)) is not None}
    if "image_generation" in in_flight:
        in_flight.discard("image_generation")
        tools.add("image_generation")
    return frozenset(tools), frozenset(in_flight)


def _anthropic_hosted_tool(block: Any) -> str | None:
    kind = _text(block, "type")
    if kind == "server_tool_use":
        name = _text(block, "name")
        return name if name in HOSTED_TOOL_ASPECTS else None
    return _ANTHROPIC_SERVER_RESULTS.get(kind) if kind is not None else None


def _only_tool_results(message: Any) -> bool:
    if not isinstance(message, Mapping):
        return False
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and all(isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content)
    )


def _anthropic_replays(messages: Any, *, pinned: bool) -> tuple[frozenset[str], frozenset[str]]:
    """The tools of hosted blocks to replay, and of those in the in-flight response."""

    if not isinstance(messages, list):
        return frozenset(), frozenset()
    in_flight: int | None = None
    if pinned:
        # The in-flight response is the last assistant message, when only the
        # client's tool results (one message per call) follow it.
        index = len(messages)
        while index and _only_tool_results(messages[index - 1]):
            index -= 1
        if index and isinstance(messages[index - 1], Mapping) and messages[index - 1].get("role") == "assistant":
            in_flight = index - 1
    tools: set[str] = set()
    carried: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            if (tool := _anthropic_hosted_tool(block)) is not None:
                (carried if index == in_flight else tools).add(tool)
    return frozenset(tools), frozenset(carried)


def _google_server_tool(part: Any) -> str | None:
    for key in ("toolCall", "toolResponse"):
        item = part.get(key) if isinstance(part, Mapping) else None
        if isinstance(item, Mapping):
            return _GOOGLE_SERVER_TOOLS.get(item.get("toolType"))
    return None


def _only_function_responses(content: Any) -> bool:
    parts = content.get("parts") if isinstance(content, Mapping) else None
    return (
        content.get("role") == "user" and isinstance(parts, list) and bool(parts)
        and all(isinstance(part, Mapping) and "functionResponse" in part for part in parts)
    )


def _google_replays(contents: Any, *, pinned: bool) -> tuple[frozenset[str], frozenset[str]]:
    """The tools of Gemini server-side parts to replay, and of those in the in-flight response.

    The in-flight response is the last ``model`` content when only the client's function responses follow it.
    """

    if not isinstance(contents, list):
        return frozenset(), frozenset()
    in_flight: int | None = None
    if pinned:
        index = len(contents)
        while index and _only_function_responses(contents[index - 1]):
            index -= 1
        if index and isinstance(contents[index - 1], Mapping) and contents[index - 1].get("role") == "model":
            in_flight = index - 1
    tools: set[str] = set()
    carried: set[str] = set()
    for index, content in enumerate(contents):
        if not isinstance(content, Mapping) or content.get("role") != "model":
            continue
        parts = content.get("parts")
        for part in parts if isinstance(parts, list) else ():
            if (tool := _google_server_tool(part)) is not None:
                (carried if index == in_flight else tools).add(tool)
    return frozenset(tools), frozenset(carried)


def admits(
    content: HostedContent,
    *,
    source: ModelHubSourceConfig,
    model_id: str,
    spec_for: SpecLookup,
    evidence: HostedEvidence,
) -> bool:
    """Whether this hop runs every piece of hosted content, by the hop's own facts (``hosted_tool_facts``).

    Admission asks the one function that states a hop's facts, so the two agree
    by construction: a declaration needs ``native`` assumed and the type the
    overlay declares there, and a replayed item needs ``replay`` assumed, which
    a current ``call`` refusal also withdraws. Evidence is read from memory, and
    only refusals count: a tool the priors offer is assumed until a real request
    refuses it.
    """

    if not content:
        return True

    def facts(tool: str):
        if tool not in HOSTED_TOOL_ASPECTS:
            # A generated image is never replayed verbatim, so nothing can admit it.
            return None
        return hosted_tool_facts(source, model_id, tool, evidence=evidence, spec_for=spec_for)

    for tool, kind in content.calls:
        stated = facts(tool)
        if stated is None or stated.native != "assumed" or (stated.declaration or {}).get("type") != kind:
            return False
    return all((stated := facts(tool)) is not None and stated.replay == "assumed" for tool in content.replays)
