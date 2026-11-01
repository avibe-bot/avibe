"""Read-only Vibey cache report: current-list valuation, never a historical bill."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from storage.cache_metrics import count, object_value, seconds
from .model_hub.pricing import (
    Cost, PriceTable, context_tier_price, context_tier_size, load_price_table, row_cost,
)


def cached_prices(state_dir: Path) -> PriceTable:
    from vibe.models_dev_catalog import cached_models_dev_catalog, load_model_vendor_map

    # The cached-only API exposes no fetch date. File mtime is not a price-table date.
    return load_price_table(
        state_dir, catalog_loader=lambda: (cached_models_dev_catalog(), None, False),
        vendor_map_loader=load_model_vendor_map,
    )


def _rate_metadata(price) -> dict[str, dict[str, float | str | None]]:
    input_rate = price.input

    def multiplier(rate: float) -> float | None:
        return rate / input_rate if input_rate else None

    rates = {
        "input": price.input, "output": price.output, "cache_read": price.cache_read,
        "cache_write": price.cache_write, "cache_write_1h": price.cache_write_1h,
        "cache_write_30m": price.cache_write_30m, "cache_write_12h": price.cache_write_12h,
    }
    sources = {key: "listed" if key in price.listed_cache_rates else "default"
               for key in rates if key.startswith("cache_")}
    return {
        "multipliers": {key: multiplier(rate) for key, rate in rates.items()},
        "sources": sources,
    }


def ledger_row(usage: Mapping[str, Any], ttls: Mapping[str, Any], price, *, write_basis: str = "reported") -> dict[str, Any]:
    total = sum(count(usage.get(key)) for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    return {
        "requests": 1, "token_reports": int(bool(usage)), "input_tokens": total,
        "output_tokens": count(usage.get("output_tokens")), "cached_input_tokens": count(usage.get("cache_read_tokens")),
        "cache_write_input_tokens": count(usage.get("cache_write_tokens")),
        "cache_write_1h_input_tokens": count(ttls.get("1h")),
        "cache_write_30m_input_tokens": count(ttls.get("30m")), "cache_write_12h_input_tokens": count(ttls.get("12h")),
        "cache_write_ttl_uncaptured_reports": int(
            write_basis == "incomplete_total" or (
                count(usage.get("cache_write_tokens")) > 0
                and (write_basis not in {"reported", "none"} or count(ttls.get("unknown")) > 0)
            )
        ),
        "context_tier": context_tier_size(price, total),
    }


def valuation(model: str | None, usage_raw: Any, ttls_raw: Any, prices: PriceTable, write_basis: str = "reported") -> dict[str, Any]:
    usage, ttls = object_value(usage_raw), object_value(ttls_raw)
    price = prices.price(model) if isinstance(model, str) else None
    row = ledger_row(usage, ttls, price, write_basis=write_basis)
    cost = row_cost(row, price)
    result = {"basis": "current_standard_list", "usd": cost.api_cost_usd if price and usage else None,
              "lower_bound": cost.api_cost_lower_bound or cost.excluded_tokens > 0,
              "excluded_tokens": cost.excluded_tokens, "price_table_date": prices.price_table_date,
              "price_key": prices.price_key(model) if price and isinstance(model, str) else None,
              "cache_write_basis": write_basis}
    if price:
        rate = context_tier_price(price, row["context_tier"])
        result["rates_per_million"] = {
            key: getattr(rate, key) for key in ("input", "output", "cache_read", "cache_write", "cache_write_1h", "cache_write_30m", "cache_write_12h")
        }
        metadata = _rate_metadata(rate)
        result["rate_multipliers"] = metadata["multipliers"]
        result["rate_sources"] = metadata["sources"]
        result["read_discount_known"] = "cache_read" in rate.listed_cache_rates
        result["context_tier"] = row["context_tier"]
    return result


def _usage(row: Mapping[str, Any]) -> dict[str, int]:
    return {"input_tokens": count(row.get("fresh_input_tokens")), "output_tokens": count(row.get("output_tokens")),
            "cache_read_tokens": count(row.get("cache_read_tokens")), "cache_write_tokens": count(row.get("cache_write_tokens"))} if row.get("token_reported") else {}


def _band(idle: float | None) -> str:
    if idle is None:
        return "unknown"
    for upper, label in ((300, "up_to_5m"), (1800, "5_to_30m"), (3600, "30_to_60m"), (43200, "1_to_12h")):
        if idle <= upper:
            return label
    return "over_12h"


def _options(provider: str, protocol: str, model: str) -> tuple[tuple[str, int], ...]:
    if provider == "anthropic" and protocol == "anthropic" and model.startswith("claude-"):
        return (("5m", 300), ("1h", 3600))
    gpt = re.fullmatch(r"gpt-(\d+)(?:\.(\d+))?(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?", model)
    if provider == "openai" and protocol == "openai_responses" and gpt and (int(gpt[1]), int(gpt[2] or 0)) >= (5, 6):
        return (("30m", 1800), ("12h", 43200))
    return ()


def lifetime_cost(rows: Sequence[Mapping[str, Any]], *, label: str, lifetime: int, prices: PriceTable) -> dict[str, Any]:
    """Counterfactual input cost: only expiry recovers writes; other misses stay misses.

    All writes pay the candidate's price, not just recovered tokens. Anthropic's
    static prefix remains at one hour; its size is an explicitly estimated share.
    """
    usd, priced, unpriced, recovered, expired, incomplete = 0.0, 0, 0, 0, 0, 0
    rates = {}
    for row in rows:
        price = prices.price(row["model"])
        writes = object_value(row.get("writes_by_ttl"))
        if (price is None or not row.get("token_reported")
                or row.get("cache_write_basis") == "incomplete_total"
                or count(writes.get("unknown")) > 0):
            unpriced += count(row.get("input_tokens"))
            incomplete += 1
            continue
        price = context_tier_price(price, context_tier_size(price, count(row.get("input_tokens"))))
        priced += 1
        fresh, read, write = (count(row.get(key)) for key in ("fresh_input_tokens", "cache_read_tokens", "cache_write_tokens"))
        eligible = min(count(row.get("reusable_tokens")), fresh + read + write)
        static = min(eligible, count(row.get("reusable_static_tokens"))) if row["protocol"] == "anthropic" else 0
        idle = row.get("idle_s")
        capacity = eligible if idle is not None and idle <= lifetime else (static if idle is not None and idle <= 3600 else 0)
        if row["classification"] == "passive_expiry":
            recover = min(max(0, capacity - read), fresh + write)
            from_write = min(write, recover)
            write -= from_write
            fresh -= recover - from_write
            read += recover
            recovered += recover
        if idle is not None and idle > lifetime:
            expire = max(0, min(read, eligible) - capacity)
            read -= expire
            write += expire
            expired += expire
        write_rate = {"5m": price.cache_write, "1h": price.cache_write_1h,
                      "30m": price.cache_write_30m, "12h": price.cache_write_12h}[label]
        static_write = min(write, max(0, count(row.get("static_tokens")) - read)) if row["protocol"] == "anthropic" else 0
        usd += (fresh * price.input + read * price.cache_read + (write - static_write) * write_rate + static_write * price.cache_write_1h) / 1e6
        incomplete += int(row.get("cache_write_basis") not in {"reported", "none"})
        rate_meta = _rate_metadata(price)
        rates[str(context_tier_size(prices.price(row["model"]), count(row.get("input_tokens"))))] = {
            "input": price.input, "read": price.cache_read, "write": write_rate,
            "static_write_1h": price.cache_write_1h if row["protocol"] == "anthropic" else None,
            "multipliers": rate_meta["multipliers"], "sources": rate_meta["sources"],
        }
    return {"lifetime": label, "lifetime_s": lifetime, "input_cost_usd": round(usd, 6) if priced else None,
            "estimated": True, "incomplete_requests": incomplete, "excluded_input_tokens": unpriced,
            "recovered_tokens": recovered, "newly_expired_tokens": expired, "rates_per_million_by_context_tier": rates,
            "rate_basis": "local catalog/overrides including catalog defaults; standard speed"}


def _percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], prices: PriceTable) -> dict[str, Any]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.get("provider"), row.get("protocol"), row.get("model"), row.get("source_id"))].append(row)
    routes = []
    for (provider, protocol, model, source), requests in sorted(grouped.items(), key=lambda item: repr(item[0])):
        totals = Cost()
        classes, causes, bands, ttls = {}, {}, {}, defaultdict(int)
        actual_reads = total_input = 0
        for row in requests:
            usage, writes = _usage(row), object_value(row.get("writes_by_ttl"))
            price = prices.price(model) if isinstance(model, str) else None
            cost = row_cost(ledger_row(usage, writes, price, write_basis=str(row.get("cache_write_basis") or "unknown")), price)
            totals.add(cost)
            actual_reads += count(row.get("cache_read_tokens"))
            total_input += count(row.get("input_tokens"))
            for ttl, tokens in writes.items():
                ttls[ttl] += count(tokens)
            cause_names = set()
            try:
                parsed_causes = json.loads(row.get("causes") or "[]")
                cause_names = {item["kind"] + (":" + item["reason"] if item.get("reason") else "") for item in parsed_causes}
            except (TypeError, ValueError, KeyError):
                pass
            for target, keys in ((classes, [row["classification"]]), (bands, [_band(row.get("idle_s"))]), (causes, cause_names)):
                for key in keys:
                    bucket = target.setdefault(key, {"requests": 0, "cache_write_tokens": 0, "uncached_input_tokens": 0,
                                                     "missed_prefix_tokens": 0, "reusable_tokens": 0, "cost": Cost()})
                    bucket["requests"] += 1
                    for output, column in (("cache_write_tokens", "cache_write_tokens"), ("uncached_input_tokens", "fresh_input_tokens"),
                                           ("missed_prefix_tokens", "missed_prefix_tokens"), ("reusable_tokens", "reusable_tokens")):
                        bucket[output] += count(row.get(column))
                    bucket["cost"].add(cost)
        for buckets in (classes, causes, bands):
            for bucket in buckets.values():
                cost = bucket.pop("cost")
                bucket.update(cost.fields())
        routes.append({"provider": provider, "protocol": protocol, "model": model, "source_id": source,
                       "requests": len(requests), "timestamp_inferred_requests": sum(row.get("data_basis") != "observed" for row in requests),
                       "cache_read_tokens": actual_reads, "input_tokens": total_input,
                       "hit_rate_tokens": actual_reads / total_input if total_input else None,
                       "classes": classes, "causes": causes, "idle_bands": bands, "writes_by_ttl": dict(ttls),
                       "valuation": {**totals.fields(), "price_key": prices.price_key(model) if isinstance(model, str) else None},
                       "what_if": [lifetime_cost(requests, label=label, lifetime=ttl, prices=prices)
                                   for label, ttl in _options(provider or "", protocol or "", model or "")]})
    waits = defaultdict(list)
    forced = defaultdict(int)
    for event in events:
        data = object_value(event.get("content_json"))
        if event.get("event_type") != "prompt.applied":
            continue
        components = data.get("components")
        for item in components if isinstance(components, list) else ():
            wait = seconds(item.get("wait_s")) if isinstance(item, dict) else None
            if wait is not None and type(item.get("tier")) is int and item["tier"] in (1, 2, 3):
                waits[item["tier"]].append(wait)
        if data.get("reason") == "deadline":
            forced[(event["session_id"], str(event.get("created_at", ""))[:10])] += 1
    turns = {(row["session_id"], row.get("turn_id") or row["request_id"]) for row in rows if row["purpose"] == "conversation"}
    pending_turns = {(row["session_id"], row.get("turn_id") or row["request_id"]) for row in rows
                     if row["purpose"] == "conversation" and row.get("prompt_notice")}
    return {
        "routes": routes, "requests": len(rows), "pricing": prices.fields(),
        "freshness": {"tiers": {str(tier): {"applied_components": len(waits[tier]), "median_s": _percentile(waits[tier], .5),
                                             "p90_s": _percentile(waits[tier], .9), "max_s": max(waits[tier], default=None)} for tier in (1, 2, 3)},
                      "forced_refreshes": [{"session_id": session, "day_utc": day, "count": amount} for (session, day), amount in sorted(forced.items())],
                      "turns": len(turns), "turns_with_pending_notice": len(pending_turns),
                      "pending_notice_share": len(pending_turns) / len(turns) if turns else None},
        "notes": [
            "Current standard list valuations, not historical or subscription bills; aliases/default prices may apply. Fast-mode premiums are not observed.",
            "Legacy rows infer gaps from response timestamps and use current route lifetime assumptions; they cannot prove an active break.",
            "Expected reads and static-prefix sizes can be token estimates. A provider_miss classification is diagnostic, not proof of provider fault.",
            "Lifetime what-ifs are estimates: only passive expiry recovers tokens; every write pays the proposed rate. Claude static prefixes stay at one hour.",
            "Unknown TTL writes and missing aggregate write totals are excluded from lifetime what-ifs, never assigned a lifetime.",
            "OpenAI retention is a minimum, not a deterministic eviction deadline; estimated expiry counts are upper bounds.",
            "Cause buckets overlap when one request has multiple causes; do not sum them. Model Hub totals include additional hosted/proxy traffic.",
        ],
    }


def report(*, since: datetime, until: datetime, session_id: str | None, model: str | None, user_context, db_path: Path) -> dict[str, Any]:
    from storage.read_only_query import run_read_only_query

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    filters = [f"started_at >= {since.timestamp()}", f"started_at < {until.timestamp()}"]
    if session_id:
        filters.append("session_id = " + literal(session_id))
    if model:
        filters.append("model = " + literal(model))
    rows = run_read_only_query("SELECT * FROM cache_requests WHERE " + " AND ".join(filters),
                               page_request=None, db_path=db_path, user_context=user_context).rows
    event_filters = ["event_type IN ('prompt.pending', 'prompt.applied')",
                     "created_at >= " + literal(since.astimezone(timezone.utc).isoformat()),
                     "created_at < " + literal(until.astimezone(timezone.utc).isoformat())]
    if session_id:
        event_filters.append("session_id = " + literal(session_id))
    elif model:
        sessions = sorted({row["session_id"] for row in rows})
        event_filters.append("session_id IN (" + ",".join(literal(session) for session in sessions) + ")" if sessions else "0")
    events = run_read_only_query("SELECT session_id, event_type, created_at, content_json FROM agent_events WHERE " + " AND ".join(event_filters),
                                 page_request=None, db_path=db_path, user_context=user_context).rows
    return {"since": since.isoformat(), "until": until.isoformat(), **summarize(rows, events, cached_prices(db_path.parent))}
