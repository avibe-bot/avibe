"""C-1 Anthropic deferred hosted calls: immutable fragments, one logical call.

The first response owns the use; the immediately continued response owns its result.
Only projections join them. Neither the stored messages nor their usage are rewritten.
"""

from dataclasses import dataclass
from collections.abc import Sequence

from core.agent_core.messages import AssistantMessage, HostedToolBlock, Message, ToolResultMessage


def pending_hosted(message: AssistantMessage) -> dict[str, HostedToolBlock]:
    """Server searches deferred behind client calls, not unfinished calls from another protocol/stop."""
    if message.origin.api != "anthropic" or message.stop_reason != "tool_use" or not message.tool_calls:
        return {}
    return {
        block.id: block for block in message.content
        if isinstance(block, HostedToolBlock) and block.tool == "web_search" and block.record.status == "pending"
        and len(block.native.payload) == 1
        and block.native.payload[0].get("type") == "server_tool_use"
        and block.native.payload[0].get("id") == block.id
    }


def result_fragment(block: HostedToolBlock) -> bool:
    return (
        block.tool == "web_search" and block.record.status != "pending" and len(block.native.payload) == 1
        and block.native.payload[0].get("type") == "web_search_tool_result"
        and block.native.payload[0].get("tool_use_id") == block.id
    )


def deferred_completion_matches(
    previous: AssistantMessage,
    current: AssistantMessage,
    result: HostedToolBlock,
) -> bool:
    """Whether ``result`` is the adjacent continuation of ``previous``'s pending call."""
    if previous.origin != current.origin or previous.source_identity != current.source_identity:
        return False
    call = pending_hosted(previous).get(result.id)
    return (
        call is not None
        and result_fragment(result)
        and call.native.source_id == result.native.source_id
        and call.tool == result.tool
        and call.action == result.action
    )


@dataclass(frozen=True)
class HostedCompletion:
    use_position: int
    result_position: int
    call: HostedToolBlock
    result: HostedToolBlock


def hosted_completions(messages: Sequence[Message]) -> tuple[HostedCompletion, ...]:
    """Pair only a real continuation on the same origin/account/Source, across its client results.

    A new user input, another assistant response, missing client result, or moved Source cannot finish an old call.
    Call ids alone are not globally unique and never authorize cross-Source pairing.
    """
    pairs = []
    previous: tuple[int, AssistantMessage] | None = None
    results: list[str] = []
    for position, message in enumerate(messages):
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
