"""Bounded, text-free diagnosis through the normal product data-query CLI.

The SQL projects counts and protocol categories inside the product, rather than
exporting a transcript/SQLite file and trying to redact secrets afterwards.
"""
from __future__ import annotations

import re

_STOPS = ("stop", "length", "tool_use", "error", "aborted", "refusal", "safety", "pause")
_COUNTS = (
    "text_blocks", "text_chars", "thinking_blocks", "thinking_chars",
    "tool_calls", "hosted_blocks", "input_tokens", "output_tokens",
    "cache_read_tokens", "reasoning_tokens", "has_error_message",
)


def response_query(session_id: str) -> str:
    # The CLI deliberately accepts SQL, not bound parameters. Only product IDs
    # are interpolated, after a strict ASCII alphabet and length check.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("invalid diagnostic session id")
    allowed = ",".join(repr(s) for s in _STOPS)
    fields = [
        "m.context_seq",
        "CASE WHEN m.type IN ('assistant','result','error') THEN m.type ELSE 'other' END AS row_type",
        f"CASE WHEN json_extract(m.content_json,'$.model.message.stop_reason') IN ({allowed}) "
        "THEN json_extract(m.content_json,'$.model.message.stop_reason') ELSE 'unknown' END AS stop_reason",
        "CASE WHEN json_extract(m.content_json,'$.model.message.error_message') IS NOT NULL "
        "THEN 1 ELSE 0 END AS has_error_message",
    ]
    for block, prefix in (("text", "text"), ("thinking", "thinking")):
        for expr, suffix in (("count(*)", "blocks"), ("coalesce(sum(length(json_extract(b.value,'$.text'))),0)", "chars")):
            fields.append(
                f"(SELECT {expr} FROM json_each(m.content_json,'$.model.message.content') b "
                f"WHERE json_extract(b.value,'$.type')='{block}') AS {prefix}_{suffix}"
            )
    for block, name in (("tool_call", "tool_calls"), ("hosted_tool", "hosted_blocks")):
        fields.append(
            "(SELECT count(*) FROM json_each(m.content_json,'$.model.message.content') b "
            f"WHERE json_extract(b.value,'$.type')='{block}') AS {name}"
        )
    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens"):
        path = f"$.model.message.usage.{name}"
        fields.append(
            f"CASE WHEN json_type(m.content_json,'{path}')='integer' "
            f"THEN json_extract(m.content_json,'{path}') ELSE NULL END AS {name}"
        )
    return (
        "WITH recent AS (SELECT context_seq,type,content_json FROM messages "
        f"WHERE session_id='{session_id}' AND context_seq IS NOT NULL "
        "ORDER BY context_seq DESC LIMIT 32) SELECT " + ", ".join(fields) +
        " FROM recent m WHERE json_extract(m.content_json,'$.model.message.role')='assistant' "
        "ORDER BY m.context_seq DESC LIMIT 9"
    )


def project_responses(payload: dict) -> dict:
    """Second allowlist fence: even a malformed/new CLI shape cannot leak text."""
    rows = payload.get("rows")
    if payload.get("ok") is not True or not isinstance(rows, list):
        raise ValueError("diagnostic query unavailable")
    output = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            raise ValueError("invalid diagnostic row")
        item = {"stop_reason": row.get("stop_reason") if row.get("stop_reason") in _STOPS else "unknown"}
        item["row_type"] = row.get("row_type") if row.get("row_type") in {"assistant", "result", "error"} else "other"
        for name in ("context_seq", *_COUNTS):
            value = row.get(name)
            item[name] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        output.append(item)
    return {
        "schema_version": 1,
        "status": "available" if output else "no_response_recorded",
        "source": "product_data_query",
        "scope": "last_8_responses_within_last_32_context_messages_of_root_session",
        "raw_text_exported": False,
        "responses_truncated": len(rows) > 8,
        "history_complete": False,
        "responses": output,
    }
