"""Model Hub API-price valuation: price resolution, cost math, and subscription value.

A valuation is a report, never a bill. The properties under test are that a price
comes from the right place (override file, then models.dev's first-party
provider, then an alias), that a model without a price is excluded and counted
rather than priced as zero, and that every total is the sum of its rows.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.handlers.model_hub.pricing import (
    PRICE_OVERRIDE_FILENAME,
    Cost,
    ModelPrice,
    PriceTable,
    billing_period,
    load_price_table,
    model_price,
    normalize_model_id,
    plan_key,
    quota_values,
    row_cost,
)
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from core.handlers.model_hub.usage import BoundedUsageLedger, SourceIdentity, UsageCall

# Shapes as models.dev publishes them (USD per million tokens), at 2026-09 prices.
CATALOG = {
    "anthropic": {
        "models": {
            "claude-opus-5": {"cost": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}},
            "claude-fable-5-1": {"cost": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5}},
            "claude-haiku-4-5": {"cost": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25}},
        }
    },
    "openai": {
        "models": {
            "gpt-5": {"cost": {"input": 1.25, "output": 10, "cache_read": 0.125}},
            "gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}},
            "gpt-5.6-sol": {"cost": {"input": 4, "output": 20, "cache_read": 0.4, "cache_write": 5}},
            "gpt-5.6-luna": {"cost": {"input": 0.2, "output": 1.2, "cache_read": 0.02, "cache_write": 0.25}},
            "gpt-6-astra": {"cost": {"input": 10, "output": 50, "cache_read": 1, "cache_write": 12.5}},
        }
    },
    "xai": {"models": {"grok-4.6": {"cost": {"input": 2, "output": 6, "cache_read": 0.5}}}},
    # An aggregator relisting a first-party model at its own price is never read.
    "openrouter": {"models": {"claude-opus-5": {"cost": {"input": 99, "output": 99}}}},
}
VENDOR_MAP = {
    "families": [
        {"prefix": "claude-", "vendor_id": "anthropic"},
        {"prefix": "gpt-", "vendor_id": "openai"},
        {"prefix": "grok-", "vendor_id": "xai"},
    ]
}
FETCHED_AT = datetime(2026, 9, 23, 12, 0).timestamp()


def _table(overrides=None) -> PriceTable:
    return PriceTable(catalog=CATALOG, vendor_map=VENDOR_MAP, overrides=overrides or {}, price_table_date="2026-09-23")


def test_model_price_reads_models_dev_cost_and_fills_missing_cache_prices():
    """MH-PRICE-001: A missing cache price is input; a one-hour cache write is twice input unless listed."""

    assert model_price({"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}) == ModelPrice(
        input=5, output=25, cache_read=0.5, cache_write=6.25, cache_write_1h=10, charges_cache_writes=True
    )
    assert model_price({"input": 2, "output": 6}) == ModelPrice(
        input=2, output=6, cache_read=2, cache_write=2, cache_write_1h=4
    )
    assert model_price({"input": 2, "output": 6, "cache_write_1h": 3}).cache_write_1h == 3
    for bad in (None, {}, {"input": 1}, {"input": -1, "output": 1}, {"input": True, "output": 1},
                {"input": float("nan"), "output": 1}, {"input": 1e9, "output": 1}):
        assert model_price(bad) is None


@pytest.mark.parametrize(
    ("reported", "normalized"),
    [
        ("claude-opus-5[1m]", "claude-opus-5"),
        ("anthropic/claude-opus-5", "claude-opus-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-opus-4-1@20250805", "claude-opus-4-1"),
        ("gpt-5-2025-08-07", "gpt-5"),
        ("  GPT-5.5  ", "gpt-5.5"),
    ],
)
def test_model_ids_normalize_to_the_price_table_spelling(reported, normalized):
    """MH-PRICE-002: Context tags, snapshot dates, provider prefixes, and case do not change a list price."""

    assert normalize_model_id(reported) == normalized


@pytest.mark.parametrize(
    ("ledger_model_id", "input_price"),
    [
        # Every model ID found in a real usage ledger, and the price it resolves to.
        ("gpt-5.6-sol", 4),
        ("gpt-6-astra", 10),
        ("grok-4.6", 2),
        ("claude-fable-5-1", 10),
        ("claude-opus-5", 5),
        ("claude-opus-5-5", 5),  # not yet on models.dev: aliased to its predecessor
        ("gpt-5.5", 5),
        ("gpt-5.6-luna", 0.2),
        ("relay-model", None),  # a relay's own name has no list price
        # Names Claude Code and Codex send.
        ("claude-opus-5[1m]", 5),
        ("claude-haiku-4-5-20251001", 1),
        ("opus", 5),
        ("haiku", 1),
        ("gpt-5-codex", 1.25),
        ("gpt-5.1-codex-max", None),  # alias target gpt-5.1 is not in this fixture
    ],
)
def test_real_ledger_model_ids_resolve_through_the_alias_map(ledger_model_id, input_price):
    """MH-PRICE-003: Real ledger IDs price from the first-party provider, directly or through an alias."""

    price = _table().price(ledger_model_id)
    assert (price.input if price else None) == input_price


def test_override_file_wins_over_models_dev_and_can_alias():
    """MH-PRICE-004: An override price or alias wins; an unusable override falls through to models.dev."""

    table = _table({
        "models": {
            "claude-opus-5": {"input": 1, "output": 2},
            "relay-model": {"alias": "claude-fable-5-1"},
            "grok-4.6": {"input": "free"},
            "loop-a": {"alias": "loop-b"},
            "loop-b": {"alias": "loop-a"},
        }
    })
    assert table.price("claude-opus-5[1m]").input == 1
    # The alias from the built-in map now lands on the override too.
    assert table.price("claude-opus-5-5").input == 1
    assert table.price("relay-model").input == 10
    assert table.price("grok-4.6").input == 2
    assert table.price("loop-a") is None


def test_price_table_reads_the_override_file_and_survives_a_bad_one(tmp_path):
    """MH-PRICE-004: The state-dir override file is read per table; an unreadable one prices from models.dev."""

    def load():
        return load_price_table(
            tmp_path,
            catalog_loader=lambda: (CATALOG, FETCHED_AT),
            vendor_map_loader=lambda: VENDOR_MAP,
        )

    assert load().price("claude-opus-5").input == 5
    assert load().price_table_date == "2026-09-23"
    (tmp_path / PRICE_OVERRIDE_FILENAME).write_text(
        json.dumps({"models": {"claude-opus-5": {"input": 3, "output": 4}}}), encoding="utf-8"
    )
    assert load().price("claude-opus-5").input == 3
    (tmp_path / PRICE_OVERRIDE_FILENAME).write_text("{not json", encoding="utf-8")
    assert load().price("claude-opus-5").input == 5

    def broken():
        raise OSError("offline")

    bare = load_price_table(tmp_path, catalog_loader=broken, vendor_map_loader=lambda: VENDOR_MAP)
    assert bare.price("claude-opus-5") is None
    assert bare.price_table_date is None


def test_row_cost_prices_each_token_class_and_excludes_unknown_models():
    """MH-PRICE-005: Fresh, cache-read, 5m and 1h cache-write, and output tokens each take their own price."""

    opus = _table().price("claude-opus-5")
    row = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 600_000,
        "cache_write_input_tokens": 300_000,
        "cache_write_1h_input_tokens": 100_000,
        "output_tokens": 100_000,
    }
    # fresh 100k × 5 + read 600k × 0.5 + 5m 200k × 6.25 + 1h 100k × 10 + out 100k × 25
    assert row_cost(row, opus) == Cost(api_cost_usd=0.5 + 0.3 + 1.25 + 1.0 + 2.5)
    unknown = row_cost(row, None)
    assert unknown == Cost(api_cost_usd=0, excluded_tokens=1_100_000)


def test_usage_metered_before_cache_writes_were_captured_is_a_lower_bound():
    """MH-PRICE-005: Uncaptured reports make a total a lower bound only where cache writes cost extra."""

    row = {"input_tokens": 1000, "output_tokens": 10, "cache_write_uncaptured_reports": 1}
    assert row_cost(row, _table().price("claude-opus-5")).api_cost_lower_bound is True
    assert row_cost(row, _table().price("grok-4.6")).api_cost_lower_bound is False
    # A listed one-hour premium counts even when the five-minute write is priced as input.
    assert row_cost(row, model_price({"input": 2, "output": 6, "cache_write_1h": 4})).api_cost_lower_bound is True
    assert row_cost(row, model_price({"input": 2, "output": 6})).api_cost_lower_bound is False


def test_priced_usage_with_unknown_parts_is_a_floor():
    """MH-PRICE-005: Requests without a token report, or tokens with no price, make the priced figure a lower bound."""

    grok = _table().price("grok-4.6")
    reported = {"requests": 2, "token_reports": 2, "input_tokens": 1000, "output_tokens": 10}
    assert row_cost(reported, grok).api_cost_lower_bound is False
    unreported = {**reported, "requests": 3}
    assert row_cost(unreported, grok).api_cost_lower_bound is True
    # An unpriced model's unreported request is a floor too, not $0 exact.
    silent = {"requests": 1, "token_reports": 0}
    assert row_cost(silent, None).fields() == {"api_cost_usd": 0, "excluded_tokens": 0, "api_cost_lower_bound": True}
    assert Cost(1.0, excluded_tokens=5).fields()["api_cost_lower_bound"] is True
    assert Cost(1.0).fields()["api_cost_lower_bound"] is False


def test_usage_report_prices_totals_as_the_sum_of_their_rows(tmp_path):
    """MH-PRICE-006: A fixture with a known total: every level sums its rows, unknown models are counted apart."""

    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: now)
    anthropic = ProtocolUsageReport.of(
        input_tokens=1_000_000, cached_input_tokens=600_000, output_tokens=100_000,
        cache_write_input_tokens=300_000, cache_write_1h_input_tokens=100_000,
    )
    ledger.record_many([
        UsageCall(source_id="src_claude", model_id="claude-opus-5[1m]", usage=anthropic, at=now),
        UsageCall(source_id="src_codex", model_id="gpt-5.5",
                  usage=ProtocolUsageReport.of(input_tokens=2_000_000, cached_input_tokens=1_000_000,
                                               output_tokens=100_000), at=now),
        UsageCall(source_id="src_relay", model_id="relay-model",
                  usage=ProtocolUsageReport.of(input_tokens=700, cached_input_tokens=0, output_tokens=300), at=now),
    ])
    summary = ledger.summary(days=7, now=now, identities=[
        SourceIdentity("src_claude", "Claude", ["claude-opus-5[1m]"]),
        SourceIdentity("src_codex", "Codex", ["gpt-5.5"]),
        SourceIdentity("src_relay", "Relay", ["relay-model"]),
    ], prices=_table())
    # claude 5.55; codex 1M × 5 + 1M × 0.5 + 100k × 30 = 5 + 0.5 + 3 = 8.5
    assert summary["totals"]["api_cost_usd"] == pytest.approx(14.05)
    assert summary["totals"]["excluded_tokens"] == 1000
    assert summary["totals"]["cache_write_input_tokens"] == 300_000
    assert summary["pricing"] == {"currency": "USD", "price_table_date": "2026-09-23"}
    by_source = {source["source_id"]: source for source in summary["sources"]}
    assert by_source["src_claude"]["api_cost_usd"] == pytest.approx(5.55)
    assert by_source["src_relay"]["models"][0]["priced"] is False
    assert by_source["src_relay"]["api_cost_usd"] == 0
    assert sum(day["api_cost_usd"] for day in summary["days"]) == pytest.approx(14.05)
    # Without a table nothing is priced and the shape is the released one.
    assert "api_cost_usd" not in ledger.summary(days=7, now=now)["totals"]


def test_rows_written_before_cache_writes_read_as_uncaptured(tmp_path):
    """MH-PRICE-007: A released row has no cache-write counters; it reads as zero writes, all reports uncaptured."""

    path = tmp_path / "usage.json"
    path.write_text(json.dumps([{
        "day": "2026-09-25", "source_id": "src_claude", "model_id": "claude-opus-5",
        "requests": 2, "token_reports": 2, "input_tokens": 1_000_000, "cached_input_tokens": 0,
        "output_tokens": 0, "last_metered_at": "2026-09-25T10:00:00+00:00",
    }]), encoding="utf-8")
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    ledger = BoundedUsageLedger(path, now=lambda: now)
    before = path.read_bytes()
    summary = ledger.summary(days=1, now=now, prices=_table())
    assert path.read_bytes() == before
    assert summary["totals"]["cache_write_input_tokens"] == 0
    assert summary["totals"]["cache_write_uncaptured_reports"] == 2
    assert summary["totals"]["api_cost_usd"] == pytest.approx(5.0)
    assert summary["totals"]["api_cost_lower_bound"] is True


@pytest.mark.parametrize(
    ("vendor", "reported", "key"),
    [
        ("anthropic", "max_20x", "claude_max_20x"),
        ("anthropic", "Max 5x", "claude_max_5x"),
        ("anthropic", "pro", "claude_pro"),
        ("openai", "plus", "chatgpt_plus"),
        ("openai", "pro", "chatgpt_pro"),
        ("openai", "team", None),
        ("codex", "pro", "chatgpt_pro"),
        ("gemini", "pro", None),
        ("xai", "plus", None),
        ("anthropic", None, None),
    ],
)
def test_reported_plans_resolve_to_fee_table_keys(vendor, reported, key):
    """MH-PRICE-008: Known plans resolve to the built-in fee table; anything else is no plan."""

    assert plan_key(vendor, reported) == key


def test_override_file_sets_a_sources_plan_fee_and_renewal_day():
    """MH-PRICE-008: A per-Source plan wins over the reported one, and an override fee wins over the table."""

    table = _table({
        "plans": {"chatgpt_pro": {"fee_usd": 229}, "team_seat": {"fee_usd": 30}, "bad": {"fee_usd": -1}},
        "sources": {"src_a": {"plan": "team_seat", "renewal_day": 31}, "src_b": {"plan": "max 20x"}},
    })
    assert table.resolve_plan_key("src_a", "openai", "plus") == "team_seat"
    assert table.fee("team_seat") == 30
    assert table.fee("chatgpt_pro") == 229
    assert table.fee("bad") is None
    assert table.resolve_plan_key("src_b", "anthropic", None) == "claude_max_20x"
    assert table.resolve_plan_key("src_c", "openai", "plus") == "chatgpt_plus"
    assert table.source_plan("src_a").renewal_day == 31


def test_an_invalid_override_entry_is_ignored_and_the_rest_still_apply(tmp_path):
    """MH-PRICE-013: A huge, non-finite, or over-long override entry is dropped alone; the other entries still apply."""

    huge = 10**400  # a JSON integer too large for a float
    (tmp_path / PRICE_OVERRIDE_FILENAME).write_text(
        json.dumps({
            "models": {
                "claude-opus-5": {"input": huge, "output": 25},
                "claude-haiku-4-5": {"input": 1e308 * 10, "output": 5},
                "x" * 129: {"input": 1, "output": 2},
                "claude-fable-5-1": {"input": 7, "output": 8},
            },
            "plans": {"claude_max_20x": {"fee_usd": huge}, "team_seat": {"fee_usd": 30}},
            "sources": {"src_a": {"plan": "  team_seat \n", "renewal_day": 14}, "src_b": {"plan": "   "}},
        }),
        encoding="utf-8",
    )
    table = load_price_table(tmp_path, catalog_loader=lambda: (CATALOG, FETCHED_AT), vendor_map_loader=lambda: VENDOR_MAP)

    # Rejected entries fall through to models.dev, as an absent override would.
    assert table.price("claude-opus-5").input == 5
    assert table.price("claude-haiku-4-5").input == 1
    assert table.price("x" * 129) is None
    assert table.fee("claude_max_20x") == 200
    # Their neighbours still apply.
    assert table.price("claude-fable-5-1").input == 7
    assert table.fee("team_seat") == 30
    assert table.source_plan("src_a").plan == "team_seat"
    assert table.resolve_plan_key("src_a", "anthropic", None) == "team_seat"
    assert table.source_plan("src_a").renewal_day == 14
    assert table.source_plan("src_b").plan is None


def test_an_integer_past_the_json_digit_limit_is_ignored_alone(tmp_path):
    """MH-PRICE-013: An integer too long for the JSON parser drops only its entry, not the file."""

    digits = "9" * 5000
    (tmp_path / PRICE_OVERRIDE_FILENAME).write_text(
        '{"models": {"claude-opus-5": {"input": %s, "output": 25}, "claude-fable-5-1": {"input": 7, "output": 8}},'
        ' "plans": {"claude_max_20x": {"fee_usd": %s}, "team_seat": {"fee_usd": 30}}}' % (digits, digits),
        encoding="utf-8",
    )
    table = load_price_table(tmp_path, catalog_loader=lambda: (CATALOG, FETCHED_AT), vendor_map_loader=lambda: VENDOR_MAP)
    assert table.price("claude-opus-5").input == 5
    assert table.price("claude-fable-5-1").input == 7
    assert table.fee("claude_max_20x") == 200
    assert table.fee("team_seat") == 30


def test_a_padded_plan_key_still_matches_the_source_plan_that_names_it():
    """MH-PRICE-013: Plan keys are trimmed like a Source's plan, and an exact key wins over a padded one."""

    table = _table({
        "plans": {" team ": {"fee_usd": 30}, " solo ": {"fee_usd": 10}, "solo": {"fee_usd": 12}},
        "sources": {"src_a": {"plan": " team "}, "src_b": {"plan": "solo"}},
    })
    assert table.resolve_plan_key("src_a", "anthropic", None) == "team"
    assert table.fee("team") == 30
    assert table.fee("solo") == 12


def test_a_128_character_override_model_key_is_the_longest_honoured():
    """MH-PRICE-013: A model key of up to 128 characters takes an override; a longer one is ignored alone."""

    at_limit, over = "m" * 128, "n" * 129
    table = _table({"models": {at_limit: {"input": 1, "output": 2}, over: {"input": 3, "output": 4}}})
    assert table.price(at_limit).input == 1
    assert table.price(over) is None


@pytest.mark.parametrize(
    ("today", "renewal_day", "expected"),
    [
        (date(2026, 9, 25), None, ("rolling_30d", date(2026, 8, 27), None)),
        (date(2026, 9, 25), 10, ("billing_cycle", date(2026, 9, 10), date(2026, 10, 10))),
        (date(2026, 9, 5), 10, ("billing_cycle", date(2026, 8, 10), date(2026, 9, 10))),
        (date(2026, 9, 10), 10, ("billing_cycle", date(2026, 9, 10), date(2026, 10, 10))),
        (date(2026, 2, 28), 31, ("billing_cycle", date(2026, 2, 28), date(2026, 3, 31))),
        (date(2026, 3, 15), 31, ("billing_cycle", date(2026, 2, 28), date(2026, 3, 31))),
        (date(2026, 1, 3), 5, ("billing_cycle", date(2025, 12, 5), date(2026, 1, 5))),
        (date(2026, 12, 20), 5, ("billing_cycle", date(2026, 12, 5), date(2027, 1, 5))),
    ],
)
def test_billing_period_is_the_current_cycle_or_the_trailing_30_days(today, renewal_day, expected):
    """MH-PRICE-009: A renewal day gives the current cycle, clamped to short months; none gives 30 days."""

    assert billing_period(today, renewal_day) == expected


def test_quota_values_compare_each_period_with_its_plan_fee():
    """MH-PRICE-010: Each Source gets week and period value; totals count only known-fee Sources for payback."""

    today = date(2026, 9, 25)
    sources = [
        {"source_id": "src_max", "vendor": "anthropic", "plan": "max_20x"},
        {"source_id": "src_plus", "vendor": "openai", "plan": "plus"},
        {"source_id": "src_team", "vendor": "openai", "plan": "team"},
    ]
    days = {
        "src_max": {"2026-09-25": Cost(300.0), "2026-09-10": Cost(100.0), "2026-08-01": Cost(999.0)},
        "src_plus": {"2026-09-20": Cost(10.0, excluded_tokens=5, api_cost_lower_bound=True)},
        "src_team": {"2026-09-24": Cost(40.0)},
    }
    table = _table({"sources": {"src_max": {"renewal_day": 5}}})
    totals = quota_values(sources, daily_costs=days, prices=table, today=today)

    max_value = sources[0]["value"]
    assert max_value["plan_key"] == "claude_max_20x" and max_value["fee_usd"] == 200
    assert max_value["week"]["api_cost_usd"] == 300
    assert max_value["period"] == {
        "basis": "billing_cycle", "from_day": "2026-09-05", "to_day": "2026-09-25", "renews_on": "2026-10-05",
        "api_cost_usd": 400.0, "excluded_tokens": 0, "api_cost_lower_bound": False,
    }
    assert max_value["multiple"] == 2.0
    plus_value = sources[1]["value"]
    assert plus_value["period"]["basis"] == "rolling_30d" and plus_value["period"]["renews_on"] is None
    assert plus_value["multiple"] == 0.5
    team_value = sources[2]["value"]
    assert (team_value["plan_key"], team_value["fee_usd"], team_value["multiple"]) == (None, None, None)
    assert team_value["week"]["api_cost_usd"] == 40

    assert totals["week"] == {"api_cost_usd": 350.0, "excluded_tokens": 5, "api_cost_lower_bound": True}
    assert totals["period"] == {
        "sources": 2, "fee_usd": 220.0, "multiple": round(410 / 220, 4),
        "api_cost_usd": 410.0, "excluded_tokens": 5, "api_cost_lower_bound": True,
    }
    assert totals["price_table_date"] == "2026-09-23"
    no_fee = [{"source_id": "src_team", "vendor": "openai", "plan": None}]
    assert quota_values(no_fee, daily_costs=days, prices=table, today=today)["period"] is None


def test_daily_costs_answer_only_for_the_given_sources(tmp_path):
    """MH-PRICE-010: The ledger prices each configured Source's days by its config model ID."""

    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: now)
    report = ProtocolUsageReport.of(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
    ledger.record_many([
        UsageCall(source_id="src_claude", model_id="claude-opus-5", usage=report, at=now),
        UsageCall(source_id="src_claude", model_id="claude-opus-5", usage=report, at=now - timedelta(days=3)),
        UsageCall(source_id="src_other", model_id="claude-opus-5", usage=report, at=now),
    ])
    costs = ledger.daily_costs(
        days=31, now=now, identities=[SourceIdentity("src_claude", "Claude", ["claude-opus-5"])], prices=_table()
    )
    assert set(costs) == {"src_claude"}
    assert sum(cost.api_cost_usd for cost in costs["src_claude"].values()) == pytest.approx(10.0)
    assert len(costs["src_claude"]) == 2


def _assert_valid(name: str, payload: dict) -> None:
    from jsonschema import Draft7Validator, FormatChecker

    schema = json.loads(
        (Path(__file__).parents[1] / "docs" / "plans" / "model-hub-contracts" / name).read_text(encoding="utf-8")
    )
    errors = [error.message for error in Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(payload)]
    assert not errors, errors


def test_priced_usage_reads_match_the_usage_summary_contract(tmp_path):
    """MH-PRICE-011: Daily and hourly priced reads, with unknown and uncaptured rows, stay inside the schema."""

    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    ledger = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: now)
    ledger.record_many([
        UsageCall(source_id="src_claude", model_id="claude-opus-5", at=now, usage=ProtocolUsageReport.of(
            input_tokens=5000, cached_input_tokens=1000, output_tokens=200,
            cache_write_input_tokens=800, cache_write_1h_input_tokens=300)),
        UsageCall(source_id="src_relay", model_id="relay-model", at=now,
                  usage=ProtocolUsageReport.of(input_tokens=10, cached_input_tokens=0, output_tokens=1)),
        UsageCall(source_id="src_relay", model_id="relay-model", at=now, usage=None),
    ])
    identities = [SourceIdentity("src_claude", "Claude", ["claude-opus-5"])]
    _assert_valid("usage-summary.schema.json", ledger.summary(days=30, now=now, identities=identities, prices=_table()))
    for window in ("24h", "7d"):
        report = ledger.report(window=window, now=now, identities=identities, prices=_table())
        _assert_valid("usage-summary.schema.json", report)
        assert report["totals"]["excluded_tokens"] == 11


def _priced_aggregates(document: dict):
    """Every priced aggregate a usage read publishes: totals, sources, models, days, buckets, rows."""

    yield document["totals"]
    for source in document.get("sources", []):
        yield source
        yield from source.get("models", [])
    yield from document.get("days", [])
    for bucket in document.get("buckets", []):
        yield from bucket.get("rows", [])


@pytest.mark.parametrize(
    ("model_id", "usage"),
    [
        # Unpriced: the model has no price anywhere.
        ("relay-model", ProtocolUsageReport.of(input_tokens=1000, cached_input_tokens=0, output_tokens=10)),
        # Unreported: the call happened, its size is unknown.
        ("grok-4.6", None),
        ("relay-model", None),
        # Uncaptured write: a row metered before cache writes were counted, on a model that charges for them.
        ("claude-opus-5", "uncaptured"),
    ],
    ids=["unpriced", "unreported-priced", "unreported-unpriced", "uncaptured-write"],
)
def test_no_aggregate_over_an_unknown_part_is_published_as_exact(tmp_path, model_id, usage):
    """MH-PRICE-012: Any unknown part reaches every aggregate and quota value above it as a floor."""

    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "usage.json"
    if usage == "uncaptured":
        path.write_text(json.dumps([{
            "day": "2026-09-25", "source_id": "src_a", "model_id": model_id,
            "requests": 1, "token_reports": 1, "input_tokens": 1000, "cached_input_tokens": 0,
            "output_tokens": 10, "last_metered_at": "2026-09-25T10:00:00+00:00",
        }]), encoding="utf-8")
    ledger = BoundedUsageLedger(path, now=lambda: now)
    known = ProtocolUsageReport.of(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0)
    calls = [UsageCall(source_id="src_a", model_id="grok-4.6", usage=known, at=now)]
    if usage != "uncaptured":
        calls.append(UsageCall(source_id="src_a", model_id=model_id, usage=usage, at=now))
    ledger.record_many(calls)
    identities = [SourceIdentity("src_a", "A", ["grok-4.6", model_id])]
    documents = [ledger.summary(days=7, now=now, identities=identities, prices=_table())]
    documents += [ledger.report(window=w, now=now, identities=identities, prices=_table()) for w in ("7d",)]
    hourly = ledger.report(window="24h", now=now, identities=identities, prices=_table())
    if usage == "uncaptured":
        # A legacy daily row has no hours to place, so the 24 hours it overlaps are
        # incomplete and their valuation is a floor even though the row is not in them.
        assert hourly["totals"]["api_cost_lower_bound"] is True
    else:
        documents.append(hourly)
    for document in documents:
        covering = [
            aggregate for aggregate in _priced_aggregates(document)
            if aggregate.get("model_id", model_id) == model_id and aggregate.get("requests", 1) > 0
        ]
        assert covering
        assert all(aggregate["api_cost_lower_bound"] is True for aggregate in covering), covering

    costs = ledger.daily_costs(days=31, now=now, identities=identities, prices=_table())
    sources = [{"source_id": "src_a", "vendor": "xai", "plan": None}]
    table = _table({"plans": {"xai_seat": {"fee_usd": 1000}}, "sources": {"src_a": {"plan": "xai_seat"}}})
    totals = quota_values(sources, daily_costs=costs, prices=table, today=date(2026, 9, 25))
    value = sources[0]["value"]
    assert value["multiple"] is not None
    assert value["week"]["api_cost_lower_bound"] is True
    assert value["period"]["api_cost_lower_bound"] is True
    assert totals["week"]["api_cost_lower_bound"] is True
    assert totals["period"]["api_cost_lower_bound"] is True


def test_a_degraded_ledger_read_values_nothing_as_exact(tmp_path):
    """MH-PRICE-012: Rows a ledger read had to drop are unknown usage, so every valuation over the read is a floor."""

    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = BoundedUsageLedger(path, now=lambda: now)
    identities = [SourceIdentity("src_a", "A", ["claude-opus-5"])]
    documents = [ledger.summary(days=7, now=now, identities=identities, prices=_table())]
    documents += [ledger.report(window=w, now=now, identities=identities, prices=_table()) for w in ("24h", "7d")]
    for document in documents:
        assert document["totals"]["api_cost_usd"] == 0
        assert document["totals"]["api_cost_lower_bound"] is True

    costs = ledger.daily_costs(days=31, now=now, identities=identities, prices=_table())
    sources = [{"source_id": "src_a", "vendor": "anthropic", "plan": "max_20x"}]
    totals = quota_values(sources, daily_costs=costs, prices=_table(), today=date(2026, 9, 25))
    value = sources[0]["value"]
    assert value["fee_usd"] == 200
    assert value["week"]["api_cost_lower_bound"] is True
    assert value["period"]["api_cost_lower_bound"] is True
    assert totals["period"]["api_cost_lower_bound"] is True
