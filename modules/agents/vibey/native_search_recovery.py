"""Turn-scoped, immutable response facts for interrupted native hosted searches.

Neither the count nor a selected proxy route is capability/refusal evidence.
Early switches below four survive a successful response's episode-budget reset
because each committed response records the routes its request actually used.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Optional

from core.agent_core.ai.errors import classify_error
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import AssistantMessage, HostedToolBlock

NATIVE_SEARCH_SWITCH_THRESHOLD = 4
SEARCH_TOOLS = frozenset({"web_search", "x_search"})
TRANSIENT_NATIVE_SEARCH_ERROR_KINDS = frozenset({
    "overloaded", "server", "network", "stalled", "interrupted",
})


def _transient_error(message: AssistantMessage) -> bool:
    if message.stop_reason != "error":
        return False
    if message.error_kind is not None:
        return message.error_kind in TRANSIENT_NATIVE_SEARCH_ERROR_KINDS
    if not message.error_message:
        return False
    classified = classify_error(body=message.error_message)
    if classified.kind in TRANSIENT_NATIVE_SEARCH_ERROR_KINDS:
        return True
    lowered = message.error_message.lower()
    return classified.kind == "unknown" and any(
        phrase in lowered for phrase in ("stream disconnected", "stream ended", "connection reset")
    )


def interrupted_native_searches(message: Optional[AssistantMessage]) -> set[str]:
    """One qualifying response counts at most once for each pending native tool."""
    if not isinstance(message, AssistantMessage) or not _transient_error(message):
        return set()
    return {
        block.tool for block in message.content
        if isinstance(block, HostedToolBlock) and block.record.status == "pending" and block.tool in SEARCH_TOOLS
    }


def native_search_interruptions(entries: Iterable[ContextEntry]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for entry in entries:
        counts.update(interrupted_native_searches(entry.message))
    return counts


def response_recovery_metadata(message: AssistantMessage, proxied: Iterable[str] = ()) -> dict[str, Any]:
    """Small projection beside the response, outside the frozen canonical payload."""
    return {"native_search_recovery": {
        "interrupted": sorted(interrupted_native_searches(message)),
        "proxied": sorted(SEARCH_TOOLS.intersection(proxied)),
    }}


def turn_recovery_facts(
    rows: Iterable[tuple[Mapping[str, Any], Optional[AssistantMessage]]],
) -> tuple[Counter[str], set[str]]:
    """Read metadata-only new rows and original response bodies for older rows."""
    counts: Counter[str] = Counter()
    proxied: set[str] = set()
    for metadata, legacy in rows:
        facts = metadata.get("native_search_recovery")
        if isinstance(facts, Mapping):
            counts.update(SEARCH_TOOLS.intersection(facts.get("interrupted", ())))
            proxied.update(SEARCH_TOOLS.intersection(facts.get("proxied", ())))
        else:
            counts.update(interrupted_native_searches(legacy))
    return counts, proxied


def should_switch(count: int, *, last_permitted_retry: bool = False) -> bool:
    return count >= NATIVE_SEARCH_SWITCH_THRESHOLD or (count > 0 and last_permitted_retry)
