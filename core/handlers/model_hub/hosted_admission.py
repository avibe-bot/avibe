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
its hosted items need no replay evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Final, Mapping, Optional

from config.v2_config import ModelHubSourceConfig
from vibe.backend_model_catalog import HostedToolSpec, bundled_provider_overlay

from .capability_evidence import HOSTED_TOOL_ASPECTS, CapabilityEvidenceStore, EvidenceFeature
from .hosted_tools import runs_on

PIN_SOURCE_HEADER: Final = "x-avibe-pin-source"
# The served-hop model a continuation was rendered for; without it a pin is the launch's primary hop.
PIN_MODEL_HEADER: Final = "x-avibe-pin-model"
PIN_UNAVAILABLE_CODE: Final = "pin_unavailable"

# Responses items that carry a hosted call's native result, by catalog tool.
_RESPONSES_HOSTED_ITEMS: Final = {"web_search_call": "web_search", "image_generation_call": "image_generation"}
# xAI runs X search as custom tool calls; their names are the provider's.
_X_SEARCH_CALLS: Final = frozenset({"x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch"})
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


SpecLookup = Callable[[str, str, str, str], Optional[HostedToolSpec]]


@dataclass(frozen=True)
class HostedContent:
    """What a request's hosted content needs from a hop.

    ``calls`` holds each declared (catalog tool, declaration ``type``);
    ``replays`` the catalog tools whose native items it replays.
    """

    calls: frozenset[tuple[str, str]] = frozenset()
    replays: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.calls or self.replays)


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
    tools = request.get("tools")
    calls = frozenset(
        (declared[tool["type"]], tool["type"])
        for tool in (tools if isinstance(tools, list) else ())
        if isinstance(tool, Mapping) and isinstance(tool.get("type"), str) and tool["type"] in declared
    )
    if protocol == "openai_responses":
        replays = _responses_replays(request.get("input"), pinned=pinned)
    elif protocol == "anthropic":
        replays = _anthropic_replays(request.get("messages"), pinned=pinned)
    else:
        replays = frozenset()
    return HostedContent(calls, replays)


def _text(item: Any, key: str) -> str | None:
    """A request field read as text with content; anything else, blank text, or a non-object item is absent."""

    value = item.get(key) if isinstance(item, Mapping) else None
    return value if isinstance(value, str) and value.strip() else None


def _responses_hosted_tool(item: Any) -> str | None:
    kind = _text(item, "type")
    if kind == "custom_tool_call" and _text(item, "name") in _X_SEARCH_CALLS:
        return "x_search"
    return _RESPONSES_HOSTED_ITEMS.get(kind) if kind is not None else None


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


def _responses_replays(items: Any, *, pinned: bool) -> frozenset[str]:
    if not isinstance(items, list):
        return frozenset()
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
    tools.update(
        "image_generation" for item in items[end:] if _responses_hosted_tool(item) == "image_generation"
    )
    return frozenset(tools)


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


def _anthropic_replays(messages: Any, *, pinned: bool) -> frozenset[str]:
    if not isinstance(messages, list):
        return frozenset()
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
    for index, message in enumerate(messages):
        if index == in_flight or not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            if (tool := _anthropic_hosted_tool(block)) is not None:
                tools.add(tool)
    return frozenset(tools)


def admits(
    content: HostedContent,
    *,
    source: ModelHubSourceConfig,
    model_id: str,
    spec_for: SpecLookup,
    evidence: CapabilityEvidenceStore,
    engine: str,
    shape: int,
    now: datetime,
) -> bool:
    """Whether this hop runs every piece of hosted content and has current positive evidence for it.

    Where a tool may run is ``runs_on``, the rule hop resolution uses for
    native mode, so the two agree by construction; a declaration must also
    carry the type the overlay declares there. Evidence is read from memory.
    """

    if not content:
        return True

    def offered(tool: str) -> HostedToolSpec | None:
        return runs_on(source, model_id, tool, spec_for=spec_for)

    def verified(tool: str, aspect: str) -> bool:
        if aspect not in HOSTED_TOOL_ASPECTS.get(tool, ()):
            # A generated image is never replayed verbatim, so nothing can admit it.
            return False
        view = evidence.view(
            source, model_id, EvidenceFeature.hosted(tool, aspect), engine=engine, shape=shape, now=now,
        )
        return view.status == "verified"

    for tool, kind in content.calls:
        spec = offered(tool)
        if spec is None or spec.declaration["type"] != kind or not verified(tool, "call"):
            return False
    return all(offered(tool) is not None and verified(tool, "replay") for tool in content.replays)
