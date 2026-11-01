"""Owning report tests: monetary estimates must preserve uncertainty and never fetch prices."""

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from core.handlers.cache_metrics import lifetime_cost, summarize, valuation
from core.handlers.model_hub.pricing import PriceTable
from tests.test_cache_metrics_storage import _response, _session, state_db  # noqa: F401
from vibe import cli


@pytest.fixture
def prices():
    return PriceTable(catalog={}, vendor_map={}, price_table_date=None, overrides={"models": {
        "claude-test": {"input": 1, "output": 5, "cache_read": .1, "cache_write": 1.25},
        "claude-alias": {"alias": "claude-test"},
    }})


def row(**values):
    return {"session_id": "ses-cache", "request_id": "r", "turn_id": "t", "purpose": "conversation",
            "provider": "anthropic", "protocol": "anthropic", "model": "claude-test", "source_id": "src",
            "token_reported": True, "data_basis": "observed", "classification": "cold_start", "causes": "[]",
            "input_tokens": 1000, "fresh_input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 1000,
            "output_tokens": 0, "writes_by_ttl": '{"5m":800,"1h":200}', "cache_write_basis": "reported",
            "static_tokens": 200, "reusable_tokens": 1000, "reusable_static_tokens": 200, "idle_s": None,
            **values}


@pytest.mark.parametrize("classification,idle,read,write,label,lifetime,cost,recovered,expired", [
    ("cold_start", None, 0, 1000, "5m", 300, .0014, 0, 0),
    ("cold_start", None, 0, 1000, "1h", 3600, .002, 0, 0),
    ("passive_expiry", 600, 0, 1000, "1h", 3600, .0001, 1000, 0),
    ("active_break", 600, 0, 1000, "1h", 3600, .002, 0, 0),
    ("provider_miss", 60, 0, 1000, "1h", 3600, .002, 0, 0),
    ("hit", 300, 1000, 0, "5m", 300, .0001, 0, 0),
    ("hit", 600, 1000, 0, "5m", 300, .00102, 0, 800),
])
def test_lifetime_cost_reprices_all_writes_keeps_static_at_one_hour_and_recovers_only_expiry(
    prices, classification, idle, read, write, label, lifetime, cost, recovered, expired,
):
    result = lifetime_cost([row(classification=classification, idle_s=idle, cache_read_tokens=read,
                                cache_write_tokens=write)], label=label, lifetime=lifetime, prices=prices)
    assert result["input_cost_usd"] == pytest.approx(cost)
    assert result["recovered_tokens"] == recovered
    assert result["newly_expired_tokens"] == expired
    assert result["estimated"] is True


@pytest.mark.parametrize("changes", [
    {"writes_by_ttl": '{"unknown":1000}'}, {"cache_write_basis": "incomplete_total"},
    {"token_reported": False}, {"model": "unlisted"},
])
def test_unknown_counts_prices_and_lifetimes_are_not_assigned_candidate_costs(prices, changes):
    result = lifetime_cost([row(**changes)], label="1h", lifetime=3600, prices=prices)
    assert result["input_cost_usd"] is None
    assert result["incomplete_requests"] == 1
    assert result["excluded_input_tokens"] == 1000


def test_valuation_exposes_real_alias_and_rate_provenance_without_inventing_legacy_write_loss(prices):
    result = valuation("claude-alias", {"input_tokens": 1000}, {}, prices, "timestamp_inferred")
    assert result["usd"] == .001
    assert result["lower_bound"] is False
    assert result["price_key"] == "claude-test"
    assert result["rate_sources"]["cache_read"] == "listed"
    assert result["rate_sources"]["cache_write_1h"] == "default"
    assert result["rate_multipliers"]["cache_write_1h"] == 2
    assert valuation("unlisted", {"input_tokens": 1000}, {}, prices)["usd"] is None
    assert valuation("claude-test", {}, {}, prices)["usd"] is None


def test_freshness_summarizes_per_turn_waits_and_ignores_damaged_optional_components(prices):
    events = [{"session_id": "ses-cache", "event_type": "prompt.applied", "created_at": "2026-10-01T01:00:00Z",
               "content_json": json.dumps({"reason": "deadline", "components": components})}
              for components in (None, 1, [{"tier": 2, "wait_s": 1800}], [{"tier": 2, "wait_s": 2000}])]
    result = summarize([row(prompt_notice=True), row(request_id="r2", prompt_notice=True)], events, prices)
    freshness = result["freshness"]
    assert freshness["turns"] == freshness["turns_with_pending_notice"] == 1
    assert freshness["tiers"]["2"] == {"applied_components": 2, "median_s": 1800, "p90_s": 2000, "max_s": 2000}
    assert result["routes"][0]["valuation"]["price_key"] == "claude-test"


def test_cache_cli_end_to_end_is_read_only_offline_and_honors_time_and_session_filters(state_db, monkeypatch, capsys):
    from vibe import models_dev_catalog

    def denied(*args, **kwargs):
        raise AssertionError("read-only report attempted a network refresh")

    monkeypatch.setattr(models_dev_catalog, "_refresh_in_background", denied)
    state_db.with_name("model_hub_prices.json").write_text(json.dumps({"models": {
        "claude-test": {"input": 1, "output": 5, "cache_read": .1, "cache_write": 1.25},
    }}))
    start = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    cache = {"version": 1, "started_at": start, "lifetime_s": 300, "idle_s": 600,
             "previous_input_tokens": 1000, "expected_read": 0, "reusable_tokens": 1000,
             "reusable_static_tokens": 200, "static_tokens": 200,
             "causes": [], "writes_by_ttl": {"5m": 800, "1h": 200}, "cache_write_basis": "reported"}
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        _session(conn, "excluded")
        for i, (session, at) in enumerate((("ses-cache", "2026-10-01T00:00:01Z"),
                                           ("ses-cache", "2026-09-01T00:00:01Z"),
                                           ("excluded", "2026-10-01T00:00:01Z"))):
            _response(conn, str(i), at, session_id=session, context_seq=i + 1,
                      cache=cache if i != 1 else None, usage={"cache_write_tokens": 1000})
        conn.commit()
    before = state_db.read_bytes()
    args = cli.build_parser().parse_args(["data", "cache", "--since", "2026-10-01T00:00:00Z",
                                         "--until", "2026-10-02T00:00:00Z", "--session-id", "ses-cache"])
    assert cli.cmd_data_cache(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "data_cache" and result["requests"] == 1
    route = result["routes"][0]
    assert route["classes"]["passive_expiry"]["requests"] == 1
    assert route["what_if"][1]["input_cost_usd"] == .0001
    assert state_db.read_bytes() == before
