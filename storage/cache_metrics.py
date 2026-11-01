"""Read-only cache diagnostic views and safe normalization of optional facts.

These are connection-local views, not persisted schema. Legacy timings use
response timestamps and current lifetime assumptions, explicitly labeled inferred.
"""

from __future__ import annotations

import json
import math
from typing import Any


def object_value(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def seconds(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def legacy_lifetime(provider: Any, protocol: Any, model: Any) -> float:
    """Best-effort current route lifetime for a row without request-start cache facts."""
    try:
        from core.agent_core.ai.provider import ModelEndpoint
        from core.agent_core.harness.context import cache_ttl_s

        endpoint = ModelEndpoint(
            str(protocol or ""), "http://diagnostic.invalid", str(model or ""), "",
            provider=str(provider or ""),
        )
        return cache_ttl_s(endpoint)
    except Exception:  # noqa: BLE001 - damaged legacy rows are diagnostic unknowns, not query failures
        return 3600.0


def observation(
    cache_raw: Any,
    usage_raw: Any,
    provider: Any,
    protocol: Any,
    model: Any,
    started_raw: Any,
    previous_started_raw: Any,
    previous_input_raw: Any,
) -> str:
    """Normalize one row into JSON so SQLite can expose stable scalar columns."""
    cache = object_value(cache_raw)
    usage = object_value(usage_raw)
    cache_started = seconds(cache.get("started_at"))
    observed = (
        cache.get("version") == 1
        and cache_started is not None
        and seconds(cache.get("lifetime_s")) is not None
    )
    started = cache_started if observed else seconds(started_raw)
    previous_started = seconds(previous_started_raw)
    idle = seconds(cache.get("idle_s")) if observed else (
        max(0.0, started - previous_started)
        if started is not None and previous_started is not None
        else None
    )
    lifetime = seconds(cache.get("lifetime_s")) if observed else legacy_lifetime(provider, protocol, model)
    reusable = count(cache.get("reusable_tokens")) if observed else count(previous_input_raw)
    expected = count(cache.get("expected_read")) if observed else (
        reusable if idle is not None and idle <= lifetime else 0
    )
    read = count(usage.get("cache_read_tokens"))
    writes = count(usage.get("cache_write_tokens"))
    fresh = count(usage.get("input_tokens"))
    total = fresh + read + writes
    causes = cache.get("causes", []) if observed else []
    if not isinstance(causes, list):
        causes = []
    causes = [{"kind": item["kind"], **({"reason": item["reason"]} if isinstance(item.get("reason"), str) else {})}
              for item in causes if isinstance(item, dict) and isinstance(item.get("kind"), str)]
    active = [item for item in causes if item["kind"] != "fork_start"
              and not (item["kind"] == "prompt_applied" and item.get("reason") == "session_start")]
    previous_input = count(cache.get("previous_input_tokens")) if observed else count(previous_input_raw)
    missed = max(0, min(previous_input, total) - read)
    tolerance = max(128, expected // 100)

    if not usage:
        classification = "unknown"
    elif active and missed > 0:
        classification = "active_break"
    elif read and missed == 0:
        classification = "hit"
    elif idle is not None and idle > lifetime:
        classification = "passive_expiry"
    elif expected - read > tolerance and cache.get("expected_read_basis") == "reported_input":
        classification = "provider_miss"
    elif expected > read and cache.get("expected_read_basis") != "reported_input":
        classification = "estimated_miss"
    elif read:
        classification = "hit"
    else:
        classification = "cold_start" if idle is None else "uncached"

    ttl_raw = cache.get("writes_by_ttl") if observed else None
    ttl = {
        key: value
        for key, value in (ttl_raw.items() if isinstance(ttl_raw, dict) else ())
        if key in {"5m", "1h", "30m", "12h"} and type(value) is int and value >= 0
    }
    incomplete = observed and cache.get("cache_write_basis") == "incomplete_total"
    if not incomplete and sum(ttl.values()) > writes:
        ttl = {}
    known_writes = sum(ttl.values())
    if writes > known_writes:
        ttl["unknown"] = writes - known_writes

    reusable_static = count(cache.get("reusable_static_tokens")) if observed else 0
    payload = {
        "idle_s": idle,
        "lifetime_s": lifetime,
        "expected_read": expected,
        "expected_read_basis": cache.get("expected_read_basis", "unknown") if observed else "timestamp_inferred",
        "data_basis": "observed" if observed else "timestamp_inferred",
        "classification": classification,
        "reusable_tokens": reusable,
        "reusable_static_tokens": reusable_static,
        "static_tokens": count(cache.get("static_tokens")) if observed else 0,
        "previous_input_tokens": previous_input,
        "missed_prefix_tokens": missed,
        "writes_by_ttl": ttl,
        "cache_write_basis": cache.get("cache_write_basis", "unknown") if observed else "timestamp_inferred",
        "causes": causes,
        "prompt_pending": bool(cache.get("prompt_pending")) if observed else False,
        "prompt_notice": bool(cache.get("prompt_notice")) if observed else False,
        "token_reported": bool(usage),
        "fresh_input_tokens": fresh,
        "cache_read_tokens": read,
        "cache_write_tokens": writes,
        "input_tokens": total,
        "output_tokens": count(usage.get("output_tokens")),
    }
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)


# Keep malformed optional JSON as empty objects. json_extract on a scalar string
# would otherwise feed non-JSON text into a second json_extract and abort a report.
_MODEL = (
    "CASE WHEN json_valid(m.content_json) AND json_type(m.content_json, '$.model') = 'object' "
    "THEN json_extract(m.content_json, '$.model') ELSE '{}' END"
)
_EVENT = (
    "CASE WHEN json_valid(a.content_json) AND json_type(a.content_json) = 'object' "
    "THEN a.content_json ELSE '{}' END"
)

_FIELDS = (
    "idle_s", "lifetime_s", "expected_read", "expected_read_basis", "data_basis", "classification",
    "reusable_tokens", "reusable_static_tokens", "static_tokens", "previous_input_tokens", "missed_prefix_tokens",
    "writes_by_ttl", "cache_write_basis", "causes", "prompt_pending", "prompt_notice", "token_reported", "fresh_input_tokens",
    "cache_read_tokens", "cache_write_tokens", "input_tokens", "output_tokens",
)

CACHE_VIEWS = {
    "_cache_rows": f"""
        SELECT m.id AS request_id, m.session_id,
               CASE WHEN json_valid(m.metadata_json) AND json_type(m.metadata_json) = 'object'
                    THEN json_extract(m.metadata_json, '$.turn_id') END AS turn_id,
               m.created_at AS at,
               CASE WHEN json_type({_MODEL}, '$.cache') = 'object' THEN json_extract({_MODEL}, '$.cache') ELSE '{{}}' END AS cache,
               CASE WHEN json_type({_MODEL}, '$.message.usage') = 'object' THEN json_extract({_MODEL}, '$.message.usage') ELSE '{{}}' END AS usage,
               json_extract({_MODEL}, '$.message.origin.provider') AS provider,
               json_extract({_MODEL}, '$.message.origin.api') AS protocol,
               json_extract({_MODEL}, '$.message.origin.model') AS model,
               'conversation' AS purpose
        FROM messages m
        WHERE m.context_seq IS NOT NULL
              AND m.author = 'agent' AND m.type IN ('assistant', 'output', 'result', 'error')
        UNION ALL
        SELECT a.id, a.session_id, a.turn_id, a.created_at,
               CASE WHEN json_type({_EVENT}, '$.cache') = 'object' THEN json_extract({_EVENT}, '$.cache') ELSE '{{}}' END,
               CASE WHEN json_type({_EVENT}, '$.usage') = 'object' THEN json_extract({_EVENT}, '$.usage') ELSE '{{}}' END,
               json_extract({_EVENT}, '$.cache.provider'), json_extract({_EVENT}, '$.cache.protocol'),
               json_extract({_EVENT}, '$.cache.model'), 'attempt'
        FROM agent_events a
        WHERE a.visibility = 'audit' AND a.event_type = 'model_attempt'
              AND json_type({_EVENT}, '$.cache') = 'object'
        UNION ALL
        SELECT a.id || ':' || j.key, a.session_id, a.turn_id, a.created_at,
               CASE WHEN json_type(j.value, '$.cache') = 'object' THEN json_extract(j.value, '$.cache') ELSE '{{}}' END,
               CASE WHEN json_type(j.value, '$.usage') = 'object' THEN json_extract(j.value, '$.usage') ELSE '{{}}' END,
               json_extract(j.value, '$.cache.provider'), json_extract(j.value, '$.cache.protocol'),
               json_extract(j.value, '$.cache.model'), 'fork_turn'
        FROM agent_events a,
             json_each(CASE WHEN json_type({_EVENT}, '$.cache_attempts') = 'array'
                            THEN json_extract({_EVENT}, '$.cache_attempts') ELSE '[]' END) AS j
        WHERE a.visibility = 'audit' AND a.event_type = 'fork_turn' AND j.type = 'object'
    """,
    "cache_requests": """
        WITH timed AS (
            SELECT *,
                   coalesce(json_extract(cache, '$.started_at'),
                            (julianday(at) - 2440587.5) * 86400) AS started_at,
                   coalesce(json_extract(usage, '$.input_tokens'), 0)
                   + coalesce(json_extract(usage, '$.cache_read_tokens'), 0)
                   + coalesce(json_extract(usage, '$.cache_write_tokens'), 0) AS total_input
            FROM _cache_rows
        ), compared AS (
            SELECT *,
                   lag(started_at) OVER history AS previous_started,
                   lag(total_input) OVER history AS previous_input
            FROM timed
            WINDOW history AS (PARTITION BY session_id, provider, model ORDER BY started_at, request_id)
        ), normalized AS (
            SELECT *, cache_observation(
                cache, usage, provider, protocol, model, started_at, previous_started, previous_input
            ) AS facts
            FROM compared
        )
        SELECT request_id, session_id, turn_id, at, started_at, provider, protocol, model, purpose,
               json_extract(cache, '$.source_id') AS source_id,
    """ + ", ".join(f"json_extract(facts, '$.{field}') AS {field}" for field in _FIELDS) + """,
               cache_valuation(model, usage, json_extract(facts, '$.writes_by_ttl'), json_extract(facts, '$.cache_write_basis')) AS valuation
        FROM normalized
    """,
}

# Explicit name used by read_only_query's connection-local UDF registration.
cache_observation = observation
