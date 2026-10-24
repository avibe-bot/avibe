"""Frozen reader boundary from core.agent_core.messages at b83d62de60f23e0894f321348400a1b9207f0011 (gh-v3.2.3rc4).

Exact source fragments, not a simulated old reader. Unchanged supporting types,
validation and I/O helpers come from the current module; the released vocabulary,
projection and identity functions below own the downgrade behavior under test.
Do not update this fixture when current readers change.
"""
from __future__ import annotations

from typing import Any, Mapping
from core.agent_core.messages import (
    AssistantMessage, FailedAttempt, HOSTED_TOOLS, HostedFallback, HostedResult,
    HostedSummary, HostedToolBlock, MAX_HOSTED_COUNT, Message, ProxyExecution,
    ToolResultMessage, UserMessage, _action_from_dict, _block_from_dict,
    _cost_from_dict, _read, _record_from_dict, _source_from_dict,
    executor_origin_from_dict, logger, origin_from_dict, usage_from_dict,
)

HOSTED_RESULT_TOOLS: tuple[str, ...] = (*HOSTED_TOOLS, "image_generate")


def message_from_dict(data: Mapping[str, Any]) -> Message:
    role = data.get("role") if isinstance(data, Mapping) else None
    if role == "user":
        f = _read(data, "user message", {"role": str, "content": list})
        return UserMessage(content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]))
    if role == "assistant":
        f = _read(
            data,
            "assistant message",
            {"role": str, "content": list, "origin": dict, "stop_reason": str},
            {"usage": dict, "error_message": str, "hosted_summary": dict, "source_identity": str},
        )
        # Decode calls before their aggregate: unknown calls make response-wide facts unusable.
        unknown_hosted_tools: list[str] = []
        content = tuple(
            _block_from_dict(
                b, ("text", "thinking", "tool_call", "hosted_tool"), unknown_hosted_tools=unknown_hosted_tools
            )
            for b in f["content"]
        )
        summary = None
        if unknown_hosted_tools:
            logger.warning("stored message contains an unrecognized hosted tool; loading it portably")
            # Do not interpret a discarded aggregate's future shape. Keep an empty envelope for known blocks,
            # but do not invent a missing summary; retained blocks and the message envelope remain strict.
            if "hosted_summary" in f and any(isinstance(block, HostedToolBlock) for block in content):
                summary = HostedSummary()
        elif "hosted_summary" in f:
            h = _read(f["hosted_summary"], "hosted summary", {"counts": dict, "cited_sources": list}, {"cost": dict})
            summary = HostedSummary(
                # A release before the bound may have stored a count past it: that counter is dropped, not refused,
                # so its transcript still loads.
                counts={
                    name: value
                    for name, value in h["counts"].items()
                    if not (isinstance(value, int) and value > MAX_HOSTED_COUNT)
                },
                cited_sources=tuple(_source_from_dict(source) for source in h["cited_sources"]),
                cost=_cost_from_dict(h["cost"]) if "cost" in h else None,
            )
        return AssistantMessage(
            content=content,
            origin=origin_from_dict(f["origin"]),
            stop_reason=f["stop_reason"],
            usage=usage_from_dict(f["usage"]) if "usage" in f else None,
            error_message=f.get("error_message"),
            hosted_summary=summary,
            source_identity=f.get("source_identity"),
        )
    if role == "tool_result":
        f = _read(
            data,
            "tool result message",
            {"role": str, "tool_call_id": str, "tool_name": str, "content": list, "is_error": bool},
            {"hosted": dict},
        )
        hosted_result = None
        unknown_hosted_tool = "hosted" in f and f["tool_name"] not in HOSTED_RESULT_TOOLS
        if "hosted" in f and not unknown_hosted_tool:
            h = _read(
                f["hosted"], "hosted result", {"record": dict}, {"execution": dict, "action": dict, "fallback_from": dict}
            )
            execution = None
            if "execution" in h:
                e = _read(
                    h["execution"],
                    "proxy execution",
                    {
                        "mode": str,
                        "executor_origin": dict,
                        "executor_source_id": str,
                        "executor_billing": str,
                    },
                    {"cost": dict, "failed_attempts": list},
                )
                attempts = []
                for attempt in e.get("failed_attempts", ()):
                    a = _read(attempt, "failed attempt", {"source_id": str, "billing": str}, {"cost": dict})
                    attempts.append(
                        FailedAttempt(
                            source_id=a["source_id"],
                            billing=a["billing"],
                            cost=_cost_from_dict(a["cost"]) if "cost" in a else None,
                        )
                    )
                execution = ProxyExecution(
                    executor_origin=executor_origin_from_dict(e["executor_origin"]),
                    executor_source_id=e["executor_source_id"],
                    executor_billing=e["executor_billing"],
                    cost=_cost_from_dict(e["cost"]) if "cost" in e else None,
                    mode=e["mode"],
                    failed_attempts=tuple(attempts),
                )
            fallback = None
            if "fallback_from" in h:
                fallback = HostedFallback(_read(h["fallback_from"], "hosted fallback", {"source_id": str})["source_id"])
            hosted_result = HostedResult(
                record=_record_from_dict(h["record"]),
                execution=execution,
                action=_action_from_dict(h["action"]) if "action" in h else None,
                fallback_from=fallback,
            )
        if unknown_hosted_tool:
            logger.warning("stored message contains an unrecognized hosted tool; loading it portably")
        return ToolResultMessage(
            tool_call_id=f["tool_call_id"],
            tool_name=f["tool_name"],
            content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]),
            is_error=f["is_error"],
            hosted=hosted_result,
        )
    raise ValueError(f"unknown message role: {role!r}")
