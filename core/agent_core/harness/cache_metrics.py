"""Request-start cache observations; diagnostics never control C-9's clearing clock.

The durable fingerprint is a chain of hashes and C-9 token estimates, not another
copy of the prompt. A whole shared request uses its reported input count; partial
prefixes are estimates, explicitly distinguished from provider measurements.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from core.agent_core.ai._common import native_hosted_tools
from core.agent_core.ai.provider import ModelRequest
from core.agent_core.harness.context import cache_ttl_s, message_tokens, request_tokens
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import AssistantMessage, message_to_dict


MAX_PREFIX_BOUNDARIES = 32


def _digest(value: Any) -> str:
    # 128-bit digests are sufficient for non-adversarial diagnostic comparisons.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]


def prefix_fingerprint(request: ModelRequest) -> list[list[Any]]:
    native = native_hosted_tools(request)
    definitions = [dict(native[tool.name].declaration or {}) if tool.name in native else asdict(tool) for tool in request.tools]
    parts = [(definitions, request_tokens("", request.tools, (), native)),
             (request.system, request_tokens(request.system, (), ()))]
    parts.extend((message_to_dict(message), message_tokens(message)) for message in request.messages)
    chain, digest, tokens = [], "", 0
    for index, (part, size) in enumerate(parts):
        digest = _digest([digest, part])
        tokens += size
        chain.append([digest, tokens, index])
    return chain


def compact_prefix(prefix: list[list[Any]]) -> list[list[Any]]:
    """Keep tools, system and the exact final boundary, plus bounded partial-prefix samples."""
    if len(prefix) <= MAX_PREFIX_BOUNDARIES:
        return prefix
    stride = math.ceil((len(prefix) - 3) / (MAX_PREFIX_BOUNDARIES - 3))
    return [*prefix[:2], *prefix[2:-1:stride], prefix[-1]]


def route_fingerprint(request: ModelRequest) -> str:
    endpoint = request.endpoint
    return _digest([endpoint.provider, endpoint.protocol, endpoint.model_id, endpoint.base_url,
                    endpoint.cache_ttl, endpoint.cache_ttl_wire, endpoint.source_id,
                    endpoint.source_identity, request.reasoning_effort])


def valid_record(value: Any) -> bool:
    """Optional old/damaged metadata is absent, never a Turn failure.

    A missing prefix is a valid historical observation, not a reusable baseline.
    Keeping its clock prevents resurrection of an older inline prefix after a
    representation switch, a historical fork, or an older writer's intervening work.
    """
    try:
        return (
            isinstance(value, Mapping) and value.get("version") == 1
            and all(isinstance(value.get(key), str) for key in ("provider", "model", "route"))
            and type(value["started_at"]) in (float, int) and math.isfinite(value["started_at"])
            and value["started_at"] >= 0 and type(value["context_seq"]) is int and value["context_seq"] >= 0
            and (
                "prefix" not in value
                or (
                    isinstance(value["prefix"], list) and bool(value["prefix"])
                    and all(isinstance(part, list) and len(part) == 3 and isinstance(part[0], str)
                            and type(part[1]) is int and part[1] >= 0
                            and type(part[2]) is int and part[2] >= 0 for part in value["prefix"])
                    and all(a[1] <= b[1] and a[2] < b[2] for a, b in zip(value["prefix"], value["prefix"][1:]))
                )
            )
            and (value.get("input_tokens") is None or (type(value["input_tokens"]) is int and value["input_tokens"] >= 0))
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def write_breakdown(message: AssistantMessage | None) -> dict[str, int]:
    usage = message.usage if message is not None else None
    if usage is None:
        return {}
    known = dict(usage.cache_writes_by_ttl or {})
    unknown = max(0, usage.cache_write_tokens - sum(known.values()))
    if unknown:
        known["unknown"] = unknown
    return known


class CacheTracker:
    """One Session's most recent request per served provider/model, across restarts."""

    def __init__(self) -> None:
        self.latest: dict[tuple[str, str], Mapping[str, Any]] = {}
        # A runtime checkpoint may omit edits of discarded results. Their
        # diagnostic sequence still matters when returning to an older route.
        self.change_seqs: dict[str, int] = {}

    def restore(self, rows: Sequence[ContextEntry], attempts: Sequence[Mapping[str, Any]]) -> None:
        for raw in [*(row.payload.get("cache") for row in rows if row.kind == "response"), *attempts]:
            if not valid_record(raw) or raw.get("input_tokens") is None:
                continue
            key = (raw["provider"], raw["model"])
            old = self.latest.get(key)
            if (old is None or raw["started_at"] > old["started_at"]
                    or (raw["started_at"] == old["started_at"] and ("prefix" in raw or "prefix" not in old))):
                self.latest[key] = raw

    def begin(
        self, request: ModelRequest, rows: Sequence[ContextEntry], *, now: float,
        causes: Sequence[Mapping[str, Any]] = (), fork: bool = False,
    ) -> dict[str, Any]:
        endpoint = request.endpoint
        record = {
            "version": 1, "started_at": now, "provider": endpoint.provider,
            "protocol": endpoint.protocol, "model": endpoint.model_id, "source_id": endpoint.source_id,
            "route": route_fingerprint(request), "prefix": prefix_fingerprint(request),
            "context_seq": max((row.context_seq for row in rows), default=0),
            "lifetime_s": cache_ttl_s(endpoint, request=request), "input_tokens": None,
            "causes": [dict(cause) for cause in causes], "writes_by_ttl": {}, "cache_write_basis": "unknown",
        }
        self._changes(record, rows)
        if fork:
            record["causes"].append({"kind": "fork_start"})
        self._expectation(record)
        return record

    def _changes(self, record: dict[str, Any], rows: Sequence[ContextEntry]) -> None:
        # Recomputed after a gateway supplies the actual served provider/model.
        computed = {"route_changed", "tools_changed", "tool_results_cleared", "checkpoint"}
        record["causes"] = [cause for cause in record["causes"] if cause.get("kind") not in computed]
        previous = self.latest.get((record["provider"], record["model"]))
        if previous is not None:
            if record["route"] != previous["route"]:
                record["causes"].append({"kind": "route_changed"})
            if previous.get("prefix") and record["prefix"][0][0] != previous["prefix"][0][0]:
                record["causes"].append({"kind": "tools_changed"})
            for kind, cause in (("context_edit", "tool_results_cleared"), ("compaction", "checkpoint")):
                if self.change_seqs.get(kind, 0) > previous["context_seq"] or any(
                    row.kind == kind and row.context_seq > previous["context_seq"] for row in rows
                ):
                    record["causes"].append({"kind": cause})

    def _expectation(self, record: dict[str, Any]) -> None:
        old = self.latest.get((record["provider"], record["model"]))
        idle = max(0.0, record["started_at"] - old["started_at"]) if old is not None else None
        record.update(idle_s=idle, expected_read=0, expected_read_basis="no_previous_request", reusable_tokens=0,
                      reusable_static_tokens=0, previous_input_tokens=old.get("input_tokens") if old else None)
        if old is None:
            return
        if not old.get("prefix"):
            # A newer prefixless observation supersedes an older inline/state
            # prefix. Historical forks must not borrow the parent's future state.
            record["expected_read_basis"] = "missing_prefix"
            return
        shared = 0
        shared_static = 0
        complete = False
        if old["route"] == record["route"]:
            current = {part[2]: part for part in record["prefix"]}
            for before in old["prefix"]:
                after = current.get(before[2])
                if after is None or before[0] != after[0]:
                    break
                shared = before[1]
                if before[2] <= 1:
                    shared_static = before[1]
            else:
                complete = True
        total = old["prefix"][-1][1]
        reported = old.get("input_tokens")
        expected = shared * reported // total if reported is not None and total else shared
        basis = "reported_input" if reported is not None and complete else "estimated_prefix"
        static = shared_static * reported // total if reported is not None and total else shared_static
        record.update(reusable_tokens=expected, reusable_static_tokens=static, expected_read_basis=basis)
        if idle <= record["lifetime_s"]:
            record["expected_read"] = expected
        else:
            record["expected_read_basis"] = "expired"

    def finish(
        self, record: dict[str, Any], message: AssistantMessage | None, *, rows: Sequence[ContextEntry] = (),
    ) -> None:
        if message is not None:
            # A gateway can serve a different provider/model from the requested alias.
            record.update(provider=message.origin.provider, model=message.origin.model)
            self._changes(record, rows)
            self._expectation(record)
            if message.usage is not None:
                usage = message.usage
                record["input_tokens"] = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
                total = record["prefix"][-1][1]
                static = record["prefix"][min(1, len(record["prefix"]) - 1)][1]
                record["static_tokens"] = static * record["input_tokens"] // total if total else 0
        record["writes_by_ttl"] = write_breakdown(message)
        usage = message.usage if message is not None else None
        if usage is None:
            record["cache_write_basis"] = "unknown"
        elif not usage.cache_write_total_reported:
            record["cache_write_basis"] = "incomplete_total"
        elif usage.cache_write_tokens == 0:
            record["cache_write_basis"] = "none"
        elif usage.cache_writes_by_ttl is None:
            record["cache_write_basis"] = "unknown"
        else:
            record["cache_write_basis"] = "reported"
        record["prefix"] = compact_prefix(record["prefix"])
        # A refusal without usage is no evidence that the provider populated its cache.
        if usage is not None:
            self.latest[(record["provider"], record["model"])] = record
