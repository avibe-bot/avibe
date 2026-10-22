"""C-1 Anthropic deferred hosted calls: immutable fragments, one logical call.

The first response owns the use; the next valid continued response owns its result.
Only model-replay projections join them. Neither stored messages nor their usage are rewritten.
"""

from dataclasses import dataclass
from collections.abc import Sequence

from core.agent_core.messages import AssistantMessage, HostedToolBlock, Message, ToolResultMessage, UserMessage


def pending_hosted(message: AssistantMessage) -> dict[str, HostedToolBlock]:
    """Anthropic server searches deferred at a tool boundary, not cut-off searches."""
    if message.origin.api != "anthropic" or message.stop_reason != "tool_use":
        return {}
    return {
        block.id: block for block in message.content
        if isinstance(block, HostedToolBlock) and block.tool == "web_search" and block.record.status == "pending"
        and len(block.native.payload) == 1
        and block.native.payload[0].get("type") == "server_tool_use"
        and block.native.payload[0].get("id") == block.id
    }


def failed_deferred_attempt(previous: AssistantMessage, current: AssistantMessage) -> bool:
    """An error partial is retained for audit, not a response that closes this native exchange.

    This permits reconstructing an already-authorized retry, not granting one.
    Stop/aborted responses and another origin or account still close the exchange.
    """
    return (
        current.stop_reason == "error" and bool(pending_hosted(previous))
        and current.origin == previous.origin and current.source_identity == previous.source_identity
    )


def effective_deferred_history(messages: Sequence[Message]) -> tuple[tuple[int, Message], ...]:
    """Return the request history after removing failed attempts and their owned results.

    A failed Anthropic response is not a valid response boundary when it follows a
    pending server search from the same Source identity. Its client-tool results are
    likewise failed-attempt facts, not results for the preceding valid response. Keep
    raw positions so callers that index the original request can still address rows;
    callers compiling a wire request should use the yielded message view itself.
    """
    effective: list[tuple[int, Message]] = []
    previous: AssistantMessage | None = None
    skipped_tool_ids: set[str] = set()
    for position, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            if previous is not None and failed_deferred_attempt(previous, message):
                skipped_tool_ids.update(call.id for call in message.tool_calls)
                continue
            previous = message
            skipped_tool_ids.clear()
            effective.append((position, message))
            continue
        if isinstance(message, UserMessage):
            previous = None
            skipped_tool_ids.clear()
            effective.append((position, message))
            continue
        if isinstance(message, ToolResultMessage) and message.tool_call_id in skipped_tool_ids:
            continue
        if not isinstance(message, ToolResultMessage):
            previous = None
            skipped_tool_ids.clear()
        effective.append((position, message))
    return tuple(effective)


def result_fragment(block: HostedToolBlock) -> bool:
    return (
        block.tool == "web_search" and block.record.status != "pending" and len(block.native.payload) == 1
        and block.native.payload[0].get("type") == "web_search_tool_result"
        and block.native.payload[0].get("tool_use_id") == block.id
    )


@dataclass(frozen=True)
class HostedCompletion:
    use_position: int
    result_position: int
    call: HostedToolBlock
    result: HostedToolBlock


def hosted_completions(messages: Sequence[Message]) -> tuple[HostedCompletion, ...]:
    """Pair only a real continuation on the same origin/account/Source, across its client results.

    A new user input, another valid assistant response, missing client result, or moved Source cannot finish an old
    call. Failed attempts on that same deferred exchange are not valid responses. Call ids alone never authorize
    cross-Source pairing.
    """
    return _hosted_completions(effective_deferred_history(messages))


def _hosted_completions(history: Sequence[tuple[int, Message]]) -> tuple[HostedCompletion, ...]:
    pairs = []
    previous: tuple[int, AssistantMessage] | None = None
    results: list[str] = []
    for position, message in history:
        if isinstance(message, ToolResultMessage):
            results.append(message.tool_call_id)
            continue
        if isinstance(message, AssistantMessage):
            if previous is not None:
                before_position, before = previous
                pending = pending_hosted(before)
                if (
                    pending and message.origin == before.origin and message.source_identity == before.source_identity
                    and sorted(results) == sorted(call.id for call in before.tool_calls)
                ):
                    seen = set()
                    for block in message.content:
                        if not isinstance(block, HostedToolBlock) or not result_fragment(block):
                            continue
                        call = pending.get(block.id)
                        if (call is not None and block.id not in seen and call.native.source_id == block.native.source_id
                                and call.tool == block.tool and call.action == block.action):
                            pairs.append(HostedCompletion(before_position, position, call, block))
                            seen.add(block.id)
            previous = (position, message)
        else:
            previous = None
        results = []
    return tuple(pairs)
