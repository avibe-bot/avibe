"""Fast-tier usage: recorded by what the response said, valued at fast rates (docs/plans/fast-mode.md).

A call whose response named the vendor's fast tier is its own ledger row
(`speed: "fast"`), valued at the models.dev `experimental.modes.fast.cost`
rates, and counted as `fast_requests` in every published aggregate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import jsonschema
import pytest

from core.handlers.model_hub.pricing import PriceTable, model_price, row_cost
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from core.handlers.model_hub.usage import BoundedUsageLedger, SourceIdentity, UsageCall

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
VENDOR_MAP = {"families": [{"prefix": "claude-", "vendor_id": "anthropic"}, {"prefix": "gpt-", "vendor_id": "openai"}]}
CATALOG = {
    "anthropic": {"models": {
        "claude-opus-5-5": {
            "cost": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5},
            "experimental": {"modes": {"fast": {
                "cost": {"input": 8, "output": 40, "cache_read": 0.4, "cache_write": 10},
                "provider": {"body": {"speed": "fast"}},
            }}},
        },
        # Listed without a fast tier.
        "claude-sonnet-5-5": {"cost": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}},
    }},
    "openai": {"models": {
        "gpt-6-astra": {
            "cost": {
                "input": 10, "output": 50, "cache_read": 1,
                "tiers": [{"input": 20, "output": 75, "cache_read": 2, "tier": {"type": "context", "size": 272000}}],
            },
            # The fast tier lists no context tier: it applies across the whole context.
            "experimental": {"modes": {"fast": {"cost": {"input": 20, "output": 100, "cache_read": 2}}}},
        },
    }},
}
MILLION = ProtocolUsageReport.of(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=1_000_000)


def _table() -> PriceTable:
    return PriceTable(catalog=CATALOG, vendor_map=VENDOR_MAP, overrides={}, price_table_date="2026-10-31")


def _row(model_id: str, *, speed: str | None = None, context_tier: int = 0) -> dict:
    return {
        "model_id": model_id, "requests": 1, "token_reports": 1,
        "input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 1_000_000,
        **({"speed": speed} if speed else {}), **({"context_tier": context_tier} if context_tier else {}),
    }


def test_the_price_table_reads_each_models_fast_tier_rates():
    table = _table()
    opus = table.price("claude-opus-5-5")
    assert (opus.input, opus.output) == (4, 20)
    assert (opus.fast.input, opus.fast.output, opus.fast.cache_read) == (8, 40, 0.4)
    assert table.price("claude-sonnet-5-5").fast is None
    # Read by the same rules: no usable standard price, no fast one either.
    assert model_price({"output": 1}, fast_cost={"input": 1, "output": 1}) is None


@pytest.mark.parametrize(
    ("model_id", "speed", "context_tier", "usd", "lower_bound"),
    [
        ("claude-opus-5-5", None, 0, 24.0, False),
        ("claude-opus-5-5", "fast", 0, 48.0, False),
        # Past a standard context tier, a fast call stays at the fast tier's flat rates.
        ("gpt-6-astra", None, 272000, 95.0, False),
        ("gpt-6-astra", "fast", 272000, 120.0, False),
        # A fast row for a model the table lists no fast tier for: standard rates, as a floor.
        ("claude-sonnet-5-5", "fast", 0, 12.0, True),
    ],
)
def test_a_fast_row_is_valued_at_the_fast_tier_rates(model_id, speed, context_tier, usd, lower_bound):
    cost = row_cost(_row(model_id, speed=speed, context_tier=context_tier), _table().price(model_id))
    assert cost.api_cost_usd == pytest.approx(usd)
    assert cost.api_cost_lower_bound is lower_bound


def test_fast_and_standard_calls_keep_their_own_rows_and_every_aggregate_counts_the_fast_ones(tmp_path):
    path = tmp_path / "usage.json"
    ledger = BoundedUsageLedger(path, now=lambda: NOW)
    ledger.record_many([
        UsageCall("src_relay", "claude-opus-5-5", MILLION, NOW),
        UsageCall("src_relay", "claude-opus-5-5", MILLION, NOW, speed="fast"),
        UsageCall("src_relay", "claude-opus-5-5", MILLION, NOW, speed="fast"),
    ])
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert sorted((row.get("speed", ""), row["requests"]) for row in persisted) == [("", 1), ("fast", 2)]

    identities = [SourceIdentity("src_relay", "Relay", ["claude-opus-5-5"])]
    summary = ledger.summary(days=1, now=NOW, identities=identities, prices=_table())
    model = summary["sources"][0]["models"][0]
    for aggregate in (summary["totals"], summary["sources"][0], model, summary["days"][0]):
        assert (aggregate["requests"], aggregate["fast_requests"]) == (3, 2)
    # One standard million-and-million, two fast ones.
    assert model["api_cost_usd"] == pytest.approx(24 + 2 * 48)
    for window in ("24h", "7d"):
        rows = [row for bucket in ledger.report(window=window, now=NOW, identities=identities)["buckets"]
                for row in bucket["rows"]]
        assert [(row["requests"], row["fast_requests"]) for row in rows] == [(3, 2)]


def test_an_aggregate_without_a_fast_request_keeps_the_released_shape(tmp_path):
    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW)
    ledger.record_many([UsageCall("src_a", "claude-opus-5-5", MILLION, NOW)])
    summary = ledger.summary(days=1, now=NOW)
    assert "fast_requests" not in summary["totals"]
    assert "fast_requests" not in summary["sources"][0]["models"][0]


def test_the_summary_still_matches_its_published_schema(tmp_path):
    with open("docs/plans/model-hub-contracts/usage-summary.schema.json", encoding="utf-8") as handle:
        schema = json.load(handle)
    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW)
    ledger.record_many([UsageCall("src_a", "claude-opus-5-5", MILLION, NOW, speed="fast")])
    summary = ledger.summary(
        days=1, now=NOW, identities=[SourceIdentity("src_a", "A", ["claude-opus-5-5"])], prices=_table(),
    )
    jsonschema.validate({**summary, "pricing": {"currency": "USD", "price_table_date": "2026-10-31"}}, schema)


def test_a_persisted_speed_is_read_back_and_an_unknown_one_drops_only_its_row(tmp_path):
    path = tmp_path / "usage.json"
    ledger = BoundedUsageLedger(path, now=lambda: NOW)
    ledger.record_many([UsageCall("src_a", "claude-opus-5-5", MILLION, NOW, speed="fast")])
    rows = json.loads(path.read_text(encoding="utf-8"))
    unknown = {**rows[0], "model_id": "claude-sonnet-5-5", "speed": "turbo"}
    path.write_text(json.dumps([*rows, unknown]), encoding="utf-8")

    summary = BoundedUsageLedger(path, now=lambda: NOW).summary(days=1, now=NOW)
    assert summary["totals"]["requests"] == 1
    assert summary["totals"]["fast_requests"] == 1


def test_a_call_the_resolver_meters_is_billed_at_the_tier_its_outcome_reported(tmp_path):
    """The resolver's half of metering reads the tier the same way the gateway does."""

    import asyncio
    from types import SimpleNamespace

    from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
    from core.handlers.model_hub.service import ModelHubService
    from core.handlers.model_hub.stream_wire import ProtocolSSEState
    from core.handlers.model_hub.usage import UsageWriter

    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW)

    async def meter() -> None:
        owner = SimpleNamespace(usage_writer=UsageWriter(ledger), now=lambda: NOW)
        served = RawCallOutcome(
            kind=RawOutcomeKind.SUCCESS, http_status=200, error_code=None, redacted_message=None,
            stream_started=False, model_id="claude-opus-5-5", source_id="src_a", usage=MILLION, served_tier="fast",
        )
        await ModelHubService._meter_call(owner, source_id="src_a", model_id="claude-opus-5-5", outcome=served, observed=None)
        # A stream the resolver closed itself: its tracker's tier.
        observed = ProtocolSSEState("anthropic")
        observed.usage, observed.served_tier, observed.model_output_started = MILLION, "standard", True
        await ModelHubService._meter_call(owner, source_id="src_a", model_id="claude-opus-5-5", outcome=None, observed=observed)

    asyncio.run(meter())
    totals = ledger.summary(days=1, now=NOW)["totals"]
    assert (totals["requests"], totals["fast_requests"]) == (2, 1)
