"""Narrow Anthropic search-history refusal admission, corroborated by the submitted wire.

A prefill error is not a search diagnosis. Its one recovery trial needs a valid mixed
continuation and actual completed search history which the existing compiler can demote
without changing the in-flight exchange. No capability evidence is written here.
"""

from collections.abc import Mapping
import re
from typing import Any

from core.agent_core.ai._common import endpoint_origin, search_history_endpoint
from core.agent_core.ai.provider import ModelRequest, ProviderError, Refusal
from core.agent_core.ai.transform import transform_messages
from core.agent_core.messages import AssistantMessage, HostedToolBlock, ToolResultMessage

_SEARCH_ERROR = re.compile(
    r"invalid `?encrypted_content`? in `?search_result`? block|invalid `?encrypted_index`? in (?:a )?(?:`?web_search_result_location`? )?citation",
    re.IGNORECASE,
)
_PREFILL_ERROR = re.compile(
    r"This model does not support assistant message prefill\.\s*The conversation must end with a user message\.",
    re.IGNORECASE,
)
_BLOCK_PATH = re.compile(r"messages\.(\d+)\.content\.(\d+)")


def _blocks(message: Any) -> list[Mapping[str, Any]]:
    content = message.get("content") if isinstance(message, Mapping) else None
    return [block for block in content if isinstance(block, Mapping)] if isinstance(content, list) else []


def _search_id(block: Mapping[str, Any]) -> str | None:
    if block.get("type") == "server_tool_use" and block.get("name") == "web_search":
        value = block.get("id")
    elif block.get("type") == "web_search_tool_result":
        value = block.get("tool_use_id")
    else:
        return None
    return value if isinstance(value, str) and value else None


def _mixed_continuation(messages: list[Any]) -> bool:
    """A tool-result-only user tail answers every client call beside a still-pending server search."""
    if len(messages) < 2 or any(not isinstance(message, Mapping) for message in messages[-2:]):
        return False
    before, tail = messages[-2:]
    blocks, results = _blocks(before), _blocks(tail)
    if (before.get("role") != "assistant" or tail.get("role") != "user" or not results
            or len(results) != len(tail.get("content", ())) or any(b.get("type") != "tool_result" for b in results)):
        return False
    calls = [b.get("id") for b in blocks if b.get("type") == "tool_use"]
    answered = [b.get("tool_use_id") for b in results]
    if not calls or any(not isinstance(value, str) or not value for value in (*calls, *answered)):
        return False
    if len(set(calls)) != len(calls) or sorted(calls) != sorted(answered):
        return False
    pending = {_search_id(b) for b in blocks if b.get("type") == "server_tool_use"} - {None}
    done = {_search_id(b) for b in blocks if b.get("type") == "web_search_tool_result"}
    return bool(pending - done)


def search_replay_refusal(error: ProviderError, request: ModelRequest, body: Mapping[str, Any]) -> Refusal | None:
    """Admit a search repair only for ciphertext the request actually carries; preserve other refusal kinds.

    Called for HTTP rejection and upstream SSE error after normalization. Never inspect echoed input or arbitrary
    response fields for keywords. The history-only compiler identifies exchanges that can be demoted without changing
    the current continuation. Explicit rejection of remaining in-flight ciphertext retains the opaque fallback.
    """
    endpoint = request.endpoint
    if error.kind != "invalid_request" or error.status not in (None, 400):
        return error.refusal
    explicit = _SEARCH_ERROR.search(error.message) is not None
    prefill = _PREFILL_ERROR.search(error.message) is not None
    if not explicit and not prefill:
        return error.refusal
    if not endpoint.source_id or not endpoint.source_identity:
        return None
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    if not explicit and not (
        request.messages and isinstance(request.messages[-1], ToolResultMessage) and _mixed_continuation(messages)
    ):
        return None
    uses, completed = set(), set()
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        for block in _blocks(message):
            call_id = _search_id(block)
            if call_id is None:
                continue
            if block.get("type") == "server_tool_use":
                uses.add(call_id)
            elif isinstance(block.get("content"), list) and any(
                isinstance(item, Mapping) and item.get("type") == "web_search_result"
                and isinstance(item.get("encrypted_content"), str) and item["encrypted_content"]
                for item in block["content"]
            ):
                completed.add(call_id)
    candidates = uses & completed
    if not candidates:
        return None
    if explicit and (path := _BLOCK_PATH.search(error.message)) is not None:
        try:
            named = messages[int(path[1])]["content"][int(path[2])]
        except (IndexError, KeyError, TypeError):
            named = None
        # Intermediaries can change message/block indices. A resolvable path narrows the explicit rejection,
        # but a stale path must not erase recovery for search ciphertext that this request really carries.
        named_id = _search_id(named) if isinstance(named, Mapping) else None
        if named_id in candidates:
            candidates = {named_id}
    compiled = transform_messages(
        request.messages, target=endpoint_origin(endpoint), protocol="anthropic",
        supports_images=request.supports_images, endpoint=search_history_endpoint(endpoint),
    )
    kept = {
        _search_id(item) for message in compiled.messages if isinstance(message, AssistantMessage)
        for block in message.content if isinstance(block, HostedToolBlock) for item in block.native.payload
    }
    if candidates - kept:
        return Refusal("search_history", endpoint.source_id, endpoint.source_identity, "web_search", "replay")
    if explicit:
        # History-only demotion cannot repair an explicitly rejected in-flight ciphertext. Keep the existing
        # portable opaque fallback available, including after a history trial; never permanently wedge a Session.
        return Refusal("opaque_refused", endpoint.source_id, endpoint.source_identity, "web_search", "replay", ("hosted",))
    return None
