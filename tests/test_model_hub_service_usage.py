"""Search-service usage in the Model Hub ledger (`web-search-services.md` §4.3).

A sent search request reaches no model, so the ledger keeps it in its own row per
Source and catalog `service_id`, through the same daily and hourly lifecycle as
model calls. These tests hold the service half of that contract at the ledger and
writer boundary: its counts and their repaired relations, its separation from
model usage, its valuation at the current catalog, and the summary's presence
rules. The model half is held by `test_model_hub_usage.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.handlers.model_hub.pricing import PriceTable
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from core.handlers.model_hub.usage import (
    BoundedUsageLedger,
    ServiceCatalogEntry,
    ServiceUsageCall,
    SourceIdentity,
    UsageCall,
    UsageWriter,
    local_usage_day,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CATALOG = (
    ServiceCatalogEntry(service_id="deepseek/search", label="DeepSeek", usd_per_1k=0.75, price_date="2026-10-19"),
    ServiceCatalogEntry(service_id="exa/search", label="Exa", usd_per_1k=7.0, price_date="2026-10-18"),
    ServiceCatalogEntry(service_id="tavily/search", label="Tavily", usd_per_1k=8.0, price_date="2026-10-19"),
)
# deepseek-chat: $1 per million input tokens, $2 per million output tokens.
PRICES = PriceTable(
    catalog={},
    vendor_map={},
    overrides={"models": {"deepseek-chat": {"input": 1, "output": 2}}},
    price_table_date="2026-10-20",
)
MODEL_COUNTERS = {
    "requests",
    "token_reports",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cache_write_input_tokens",
    "cache_write_1h_input_tokens",
    "cache_write_uncaptured_reports",
}


def _ledger(tmp_path: Path, **kwargs) -> BoundedUsageLedger:
    return BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW, **kwargs)


def _service(summary: dict, service_id: str) -> dict:
    (entry,) = [entry for entry in summary["services"] if entry["service_id"] == service_id]
    return entry


def _assert_valid(payload: dict) -> None:
    from jsonschema import Draft7Validator, FormatChecker

    schema = json.loads(
        (Path(__file__).parents[1] / "docs" / "plans" / "model-hub-contracts" / "usage-summary.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = [error.message for error in Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(payload)]
    assert not errors, errors


def test_service_requests_are_their_own_rows_beside_model_usage(tmp_path):
    """A service request never counts as a model call, a Source row or quota value; its list price joins the totals."""

    ledger = _ledger(tmp_path)
    ledger.record_many([
        UsageCall(
            source_id="src_deepseek",
            model_id="deepseek-chat",
            usage=ProtocolUsageReport.of(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=500_000),
            at=NOW,
        ),
        ServiceUsageCall(source_id="src_deepseek", service_id="deepseek/search", at=NOW, billable_requests=1),
        # An empty DeepSeek answer: sent, not billed.
        ServiceUsageCall(source_id="src_deepseek", service_id="deepseek/search", at=NOW),
        # An unreadable DeepSeek 200: sent, billing unknown.
        ServiceUsageCall(source_id="src_deepseek", service_id="deepseek/search", at=NOW, uncertain_requests=1),
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW,
            billable_requests=1, reported_requests=1, reported_usd=0.0065,
        ),
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW,
            billable_requests=1, reported_requests=1, reported_usd=0.0075,
        ),
    ])
    identities = [
        SourceIdentity("src_deepseek", "DeepSeek key", ["deepseek-chat"]),
        SourceIdentity("src_exa", "Exa key"),
    ]

    # Read back after a restart: the reported figures are the ledger's, not memory's.
    reopened = BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW)
    summary = reopened.summary(days=7, now=NOW, identities=identities, prices=PRICES, service_catalog=CATALOG)

    assert summary["totals"]["requests"] == 1
    # A tool-only Source is no Source row; a DeepSeek Source keeps only its model usage there.
    assert [source["source_id"] for source in summary["sources"]] == ["src_deepseek"]
    assert summary["sources"][0]["requests"] == 1
    assert summary["sources"][0]["api_cost_usd"] == pytest.approx(2.0)
    metered_at = NOW.isoformat()
    assert summary["services"] == [
        {
            "source_id": "src_deepseek",
            "service_id": "deepseek/search",
            "label": "DeepSeek key",
            "service_label": "DeepSeek",
            "requests": 3,
            "billable_requests": 1,
            "uncertain_requests": 1,
            "reported_requests": 0,
            "last_metered_at": metered_at,
            "reported_usd": 0.0,
            "api_cost_usd": 0.00075,
            "api_cost_lower_bound": True,
            "priced": True,
        },
        {
            "source_id": "src_exa",
            "service_id": "exa/search",
            "label": "Exa key",
            "service_label": "Exa",
            "requests": 2,
            "billable_requests": 2,
            "uncertain_requests": 0,
            "reported_requests": 2,
            "last_metered_at": metered_at,
            "reported_usd": 0.014,
            # The list valuation, never the reported figure.
            "api_cost_usd": 0.014,
            "api_cost_lower_bound": False,
            "priced": True,
        },
    ]
    totals = summary["totals"]
    assert totals["api_cost_usd"] == pytest.approx(2.0 + 0.00075 + 0.014)
    assert totals["service_reported_usd"] == 0.014
    assert totals["excluded_service_requests"] == 0
    assert totals["api_cost_lower_bound"] is True
    assert sum(day["api_cost_usd"] for day in summary["days"]) == pytest.approx(totals["api_cost_usd"])
    assert summary["pricing"] == {
        "currency": "USD",
        "price_table_date": "2026-10-20",
        "service_price_date": "2026-10-18",
    }
    # A plan's quota covers model calls; search requests on the same Source are not one.
    costs = reopened.daily_costs(days=31, now=NOW, identities=identities[:1], prices=PRICES)
    (day_cost,) = costs["src_deepseek"].values()
    assert day_cost.api_cost_usd == pytest.approx(2.0)
    assert day_cost.api_cost_lower_bound is False


def test_service_rows_reach_daily_and_hourly_buckets_in_their_own_shape(tmp_path):
    """Buckets carry `kind: "service"` rows after the model rows, and the 24-hour view keeps only recent hours."""

    ledger = _ledger(tmp_path)
    ledger.record_many([
        UsageCall(source_id="src_deepseek", model_id="deepseek-chat", usage=None, at=NOW - timedelta(hours=1)),
        ServiceUsageCall(source_id="src_tavily", service_id="tavily/search", at=NOW - timedelta(hours=1), billable_requests=1),
        ServiceUsageCall(source_id="src_tavily", service_id="tavily/search", at=NOW - timedelta(hours=1), billable_requests=1),
        ServiceUsageCall(source_id="src_tavily", service_id="tavily/search", at=NOW - timedelta(hours=30), billable_requests=1),
    ])
    identities = [SourceIdentity("src_deepseek", "DeepSeek", ["deepseek-chat"]), SourceIdentity("src_tavily", "Tavily")]

    hourly = ledger.report(window="24h", now=NOW, identities=identities, prices=PRICES, service_catalog=CATALOG)

    assert all(bucket["history_complete"] for bucket in hourly["buckets"])
    (rows,) = [bucket["rows"] for bucket in hourly["buckets"] if bucket["rows"]]
    assert [row.get("kind") for row in rows] == [None, "service"]
    assert rows[0]["model_id"] == "deepseek-chat"
    assert rows[1] == {
        "kind": "service",
        "source_id": "src_tavily",
        "service_id": "tavily/search",
        "requests": 2,
        "billable_requests": 2,
        "uncertain_requests": 0,
        "reported_requests": 0,
        "reported_usd": 0.0,
        "api_cost_usd": 0.016,
        "api_cost_lower_bound": False,
    }
    assert _service(hourly, "tavily/search")["requests"] == 2
    assert hourly["totals"]["requests"] == 1

    daily = ledger.report(window="7d", now=NOW, identities=identities, prices=PRICES, service_catalog=CATALOG)
    service_rows = [row for bucket in daily["buckets"] for row in bucket["rows"] if row.get("kind") == "service"]
    assert sum(row["requests"] for row in service_rows) == 3
    assert _service(daily, "tavily/search")["api_cost_usd"] == 0.024
    assert daily["totals"]["api_cost_usd"] == pytest.approx(0.024)


def test_without_a_price_table_service_usage_carries_counts_only(tmp_path):
    """Pricing presence is unchanged: no model price table, no service cost field anywhere, counts still exact."""

    ledger = _ledger(tmp_path)
    ledger.record_many([
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW,
            billable_requests=1, reported_requests=1, reported_usd=0.007,
        ),
    ])
    identities = [SourceIdentity("src_exa", "Exa")]

    summary = ledger.summary(days=7, now=NOW, identities=identities, service_catalog=CATALOG)
    report = ledger.report(window="7d", now=NOW, identities=identities, service_catalog=CATALOG)

    assert "pricing" not in summary
    assert summary["services"] == [
        {
            "source_id": "src_exa",
            "service_id": "exa/search",
            "label": "Exa",
            "service_label": "Exa",
            "requests": 1,
            "billable_requests": 1,
            "uncertain_requests": 0,
            "reported_requests": 1,
            "last_metered_at": NOW.isoformat(),
        }
    ]
    assert set(summary["totals"]) == MODEL_COUNTERS
    assert all(set(day) == {"day", *MODEL_COUNTERS} for day in summary["days"])
    (service_row,) = [row for bucket in report["buckets"] for row in bucket["rows"]]
    assert set(service_row) == {
        "kind", "source_id", "service_id", "requests", "billable_requests", "uncertain_requests", "reported_requests",
    }


def _persisted_service_row(**counts) -> dict:
    return {
        "day": local_usage_day(NOW).isoformat(),
        "source_id": "src_exa",
        "service_id": "exa/search",
        "last_metered_at": NOW.isoformat(),
        **counts,
    }


# (stored fields) -> (billable, uncertain, reported_requests, reported_usd, lower_bound); requests is always kept.
REPAIRS = {
    "consistent": (
        {"requests": 3, "billable_requests": 2, "uncertain_requests": 0, "reported_requests": 1, "reported_usd": 0.5},
        (2, 0, 1, 0.5, False),
    ),
    "a row without the additive fields loads as zero": ({"requests": 2}, (0, 0, 0, 0.0, False)),
    "billing unknown is always a floor": (
        {"requests": 3, "billable_requests": 1, "uncertain_requests": 1, "reported_requests": 0, "reported_usd": 0},
        (1, 1, 0, 0.0, True),
    ),
    "a stored floor stays a floor": (
        {"requests": 1, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 0, "reported_usd": 0,
         "lower_bound": True},
        (1, 0, 0, 0.0, True),
    ),
    "billable past requests drops the reports it no longer covers": (
        {"requests": 2, "billable_requests": 5, "uncertain_requests": 0, "reported_requests": 3, "reported_usd": 0.3},
        (2, 0, 0, 0.0, True),
    ),
    "reducing billable drops every dependent report even inside the new bound": (
        {"requests": 2, "billable_requests": 3, "uncertain_requests": 0, "reported_requests": 1, "reported_usd": 0.2},
        (2, 0, 0, 0.0, True),
    ),
    "invalid count marks a floor": (
        {"requests": 2, "billable_requests": -1, "uncertain_requests": 0, "reported_requests": 0, "reported_usd": 0},
        (0, 0, 0, 0.0, True),
    ),
    "uncertain past what billing leaves": (
        {"requests": 3, "billable_requests": 2, "uncertain_requests": 4, "reported_requests": 0, "reported_usd": 0},
        (2, 1, 0, 0.0, True),
    ),
    "reported past billable": (
        {"requests": 3, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 2, "reported_usd": 0.2},
        (1, 0, 0, 0.0, True),
    ),
    "orphaned reported dollars": (
        {"requests": 1, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 0, "reported_usd": 0.4},
        (1, 0, 0, 0.0, True),
    ),
    "reported past a dollar a request": (
        {"requests": 2, "billable_requests": 2, "uncertain_requests": 0, "reported_requests": 1, "reported_usd": 1.5},
        (2, 0, 0, 0.0, True),
    ),
    "a non-finite reported amount": (
        {"requests": 1, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 1,
         "reported_usd": float("nan")},
        (1, 0, 0, 0.0, True),
    ),
    "a negative reported amount": (
        {"requests": 1, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 1, "reported_usd": -0.1},
        (1, 0, 0, 0.0, True),
    ),
}


@pytest.mark.parametrize("stored, expected", list(REPAIRS.values()), ids=list(REPAIRS))
@pytest.mark.parametrize("door", ["read", "write"])
def test_a_service_row_keeps_its_relations_on_read_and_on_write(tmp_path, stored, expected, door):
    """An impossible counter set is published, and rewritten, as the smaller true statement, marked a floor."""

    path = tmp_path / "usage.json"
    # `json.dumps` spells a non-finite float as `NaN`, which a hand-edited file can hold.
    ledger = BoundedUsageLedger(path, now=lambda: NOW)
    if door == "read" or stored.get("lower_bound"):
        # lower_bound is a persisted fact, not a caller-supplied usage quantity.
        path.write_text(json.dumps([_persisted_service_row(**stored)]), encoding="utf-8")
    else:
        ledger.record_many([ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=NOW, **stored)])
    billable, uncertain, reported_requests, reported_usd, lower_bound = expected

    service = _service(
        ledger.summary(days=7, now=NOW, identities=[SourceIdentity("src_exa", "Exa")], prices=PRICES,
                       service_catalog=CATALOG),
        "exa/search",
    )
    assert (
        service["requests"], service["billable_requests"], service["uncertain_requests"],
        service["reported_requests"], service["reported_usd"], service["api_cost_lower_bound"],
    ) == (stored["requests"], billable, uncertain, reported_requests, reported_usd, lower_bound)
    assert service["api_cost_usd"] == pytest.approx(billable * 7.0 / 1000)

    # Any write rewrites the file; what it keeps is the repaired row, never the stored one.
    ledger.record_many([UsageCall(source_id="src_other", model_id="m", usage=None, at=NOW)])
    (persisted,) = [row for row in json.loads(path.read_text(encoding="utf-8")) if "service_id" in row]
    assert "model_id" not in persisted
    assert (
        persisted["requests"], persisted["billable_requests"], persisted["uncertain_requests"],
        persisted["reported_requests"], persisted["reported_usd"], persisted["lower_bound"],
    ) == (stored["requests"], billable, uncertain, reported_requests, reported_usd, lower_bound)


def test_a_repaired_row_leaves_its_hours_partial(tmp_path):
    """Counts reduced on read no longer match the hours written beside them, so the 24-hour view says it is partial."""

    path = tmp_path / "usage.json"
    hour = (NOW - timedelta(hours=1)).replace(minute=0)
    slice_counts = {"requests": 1, "billable_requests": 1, "uncertain_requests": 0, "reported_requests": 0,
                    "reported_usd": 0.0}
    row = _persisted_service_row(
        requests=1, billable_requests=2, uncertain_requests=0, reported_requests=0, reported_usd=0.0,
        last_metered_at=hour.isoformat(),
        hours=[{"key": hour.isoformat(timespec="seconds"), **slice_counts, "last_metered_at": hour.isoformat()}],
        hourly_expired_totals={**slice_counts, "requests": 0, "billable_requests": 0},
        hourly_history_complete=True,
    )
    path.write_text(json.dumps([row]), encoding="utf-8")

    report = BoundedUsageLedger(path, now=lambda: NOW).report(
        window="24h", now=NOW, identities=[SourceIdentity("src_exa", "Exa")], prices=PRICES, service_catalog=CATALOG
    )

    assert not all(bucket["history_complete"] for bucket in report["buckets"])
    assert report["totals"]["api_cost_lower_bound"] is True




def test_an_uncertain_service_hour_does_not_floor_an_exact_sibling(tmp_path):
    """Hourly cost floors follow the slice's uncertainty, not a different slice's daily witness."""
    ledger = _ledger(tmp_path)
    ledger.record_many([
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW - timedelta(hours=2), uncertain_requests=1,
        ),
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW - timedelta(hours=1), billable_requests=1,
        ),
    ])

    report = ledger.report(window="24h", now=NOW, prices=PRICES, service_catalog=CATALOG)
    service_rows = [row for bucket in report["buckets"] for row in bucket["rows"] if row.get("kind") == "service"]
    uncertain = next(row for row in service_rows if row["uncertain_requests"] == 1)
    exact = next(row for row in service_rows if row["billable_requests"] == 1)
    assert uncertain["api_cost_lower_bound"] is True
    assert exact["api_cost_lower_bound"] is False
    assert report["totals"]["api_cost_lower_bound"] is True


@pytest.mark.parametrize("billable", [0, 1])
def test_a_removed_catalog_row_is_unpriced_even_without_billable_requests(tmp_path, billable):
    """§4.3 marks a removed service unpriced and lower-bound, independent of its historical billable count."""
    ledger = _ledger(tmp_path)
    ledger.record_many([ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=NOW,
                                       billable_requests=billable)])
    summary = ledger.summary(now=NOW, prices=PRICES)
    service = _service(summary, "exa/search")
    assert (service["priced"], service["api_cost_lower_bound"]) == (False, True)
    assert summary["totals"]["excluded_service_requests"] == billable
    _assert_valid(summary)


def test_the_current_catalog_values_service_rows_and_a_dropped_one_stays_counted(tmp_path):
    """Usage values stored requests at today's catalog; a removed Source or catalog row keeps counts and reported cost."""

    ledger = _ledger(tmp_path)
    ledger.record_many([
        ServiceUsageCall(
            source_id="src_exa", service_id="exa/search", at=NOW,
            billable_requests=1, reported_requests=1, reported_usd=0.007,
        ),
        ServiceUsageCall(source_id="src_gone", service_id="tavily/search", at=NOW, billable_requests=1),
        ServiceUsageCall(source_id="src_gone", service_id="tavily/search", at=NOW, billable_requests=1),
    ])
    identities = [SourceIdentity("src_exa", "Exa")]
    tavily_only = [ServiceCatalogEntry(service_id="tavily/search", label="Tavily", usd_per_1k=8.0, price_date="2026-10-19")]
    repriced = [ServiceCatalogEntry(service_id="tavily/search", label="Tavily", usd_per_1k=5.0, price_date="2026-11-01")]

    summary = ledger.summary(days=7, now=NOW, identities=identities, prices=PRICES, service_catalog=tavily_only)

    exa = _service(summary, "exa/search")
    assert (exa["priced"], exa["service_label"], exa["api_cost_usd"], exa["api_cost_lower_bound"]) == (
        False, None, 0.0, True,
    )
    assert exa["reported_usd"] == 0.007
    tavily = _service(summary, "tavily/search")
    # The Source was removed: no label, still its own row with its own counts.
    assert (tavily["label"], tavily["requests"], tavily["api_cost_usd"], tavily["priced"]) == (None, 2, 0.016, True)
    totals = summary["totals"]
    assert (totals["api_cost_usd"], totals["excluded_service_requests"], totals["service_reported_usd"]) == (
        0.016, 1, 0.007,
    )
    assert totals["api_cost_lower_bound"] is True
    assert summary["pricing"]["service_price_date"] == "2026-10-19"

    later = ledger.summary(days=7, now=NOW, identities=identities, prices=PRICES, service_catalog=repriced)
    assert _service(later, "tavily/search")["api_cost_usd"] == 0.01
    assert later["pricing"]["service_price_date"] == "2026-11-01"
    # A summary that values no service has no service price to date.
    assert "service_price_date" not in ledger.summary(days=7, now=NOW, identities=identities, prices=PRICES)["pricing"]


def test_hourly_history_reconciles_although_reported_sums_depend_on_order(tmp_path):
    """Counts reconcile exactly; a reported float sum is bounded, never compared, so its order cannot mark hours partial."""

    early, late = NOW - timedelta(hours=3), NOW - timedelta(hours=2)
    ledger = _ledger(tmp_path)
    # The day adds 0.001 + 0.001 + 0.008; its hours add (0.001 + 0.008) + 0.001, a different double.
    ledger.record_many([
        ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=early, billable_requests=1,
                         reported_requests=1, reported_usd=0.001),
        ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=late, billable_requests=1,
                         reported_requests=1, reported_usd=0.001),
        ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=early, billable_requests=1,
                         reported_requests=1, reported_usd=0.008),
    ])

    report = ledger.report(
        window="24h", now=NOW, identities=[SourceIdentity("src_exa", "Exa")], prices=PRICES, service_catalog=CATALOG
    )

    assert all(bucket["history_complete"] for bucket in report["buckets"])
    assert report["totals"]["api_cost_lower_bound"] is False
    assert _service(report, "exa/search")["reported_usd"] == 0.01


def test_the_writer_folds_service_requests_exactly_and_apart_from_a_same_named_model(tmp_path):
    """Queued requests fold into the row their separate writes would reach; a model ID equal to a service id stays a model."""

    ledger = _ledger(tmp_path)
    writer = UsageWriter(ledger)

    async def meter() -> None:
        writer.record_service(source_id="src_exa", service_id="exa/search", billable=True, reported_usd=0.007, at=NOW)
        writer.record_service(source_id="src_exa", service_id="exa/search", billable=True, at=NOW)
        writer.record_service(source_id="src_exa", service_id="exa/search", billable=False, uncertain=True, at=NOW)
        writer.record_service(source_id="src_exa", service_id="exa/search", billable=False, at=NOW)
        writer.record(source_id="src_exa", model_id="exa/search", usage=None, at=NOW)
        assert writer.unpersisted == 5
        assert await writer.drain(timeout=5) == 0

    asyncio.run(meter())
    summary = ledger.summary(days=7, now=NOW, identities=[SourceIdentity("src_exa", "Exa", ["exa/search"])],
                             prices=PRICES, service_catalog=CATALOG)

    service = _service(summary, "exa/search")
    assert (
        service["requests"], service["billable_requests"], service["uncertain_requests"],
        service["reported_requests"], service["reported_usd"], service["api_cost_lower_bound"],
    ) == (4, 2, 1, 1, 0.007, True)
    assert summary["totals"]["requests"] == 1
    assert summary["sources"][0]["models"][0]["model_id"] == "exa/search"


@pytest.mark.parametrize("first, expected", [
    ({"billable": True, "reported_usd": 1.5}, (2, 1, 0.25)),
    ({"billable": False, "reported_usd": 0.5}, (1, 1, 0.25)),
    ({"billable": True, "reported_usd": -0.1}, (2, 1, 0.25)),
    ({"billable": True, "reported_usd": True}, (2, 1, 0.25)),
    ({"billable": True, "reported_usd": float("nan")}, (2, 1, 0.25)),
    ({"billable": True, "reported_usd": float("inf")}, (2, 1, 0.25)),
    ({"billable": True, "uncertain": True}, (2, 1, 0.25)),
    ({"billable": 1, "reported_usd": 0.5}, (1, 1, 0.25)),
    ({"billable": 1.5}, (1, 1, 0.25)),
    ({"billable": "yes"}, (1, 1, 0.25)),
    ({"billable": None}, (1, 1, 0.25)),
    ({"billable": True, "uncertain": "x"}, (2, 1, 0.25)),
])
def test_writer_repairs_each_request_before_a_fold_can_hide_its_corruption(tmp_path, first, expected):
    """Queue folding must equal separate writes: valid later calls cannot legitimize an invalid earlier report."""
    ledger = _ledger(tmp_path)
    writer = UsageWriter(ledger)

    async def meter():
        writer.record_service(source_id="src_exa", service_id="exa/search", at=NOW, **first)
        writer.record_service(source_id="src_exa", service_id="exa/search", at=NOW,
                              billable=True, reported_usd=0.25)
        assert await writer.drain(timeout=5) == 0

    asyncio.run(meter())
    service = _service(ledger.summary(now=NOW, prices=PRICES, service_catalog=CATALOG), "exa/search")
    assert (service["billable_requests"], service["reported_requests"], service["reported_usd"]) == expected
    assert service["requests"] == 2
    assert service["api_cost_lower_bound"] is True


def test_service_rows_share_the_ledger_retention_and_capacity(tmp_path):
    """A service row ages out of the window and competes for the row capacity by recency, as a model row does."""

    ledger = _ledger(tmp_path, max_rows=2)
    ledger.record_many([
        ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=NOW - timedelta(days=70), billable_requests=1),
        UsageCall(source_id="src_deepseek", model_id="deepseek-chat", usage=None, at=NOW - timedelta(hours=2)),
        ServiceUsageCall(source_id="src_tavily", service_id="tavily/search", at=NOW - timedelta(hours=1)),
        ServiceUsageCall(source_id="src_deepseek", service_id="deepseek/search", at=NOW),
    ])

    persisted = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))

    assert sorted(row.get("service_id") or row["model_id"] for row in persisted) == [
        "deepseek/search", "tavily/search",
    ]


def test_service_usage_reads_stay_inside_the_usage_summary_contract(tmp_path):
    """A tool-only Source's usage validates against the closed schema, priced or not, in every read shape."""

    ledger = _ledger(tmp_path)
    ledger.record_many([
        ServiceUsageCall(source_id="src_exa", service_id="exa/search", at=NOW - timedelta(hours=1), billable_requests=1,
                         reported_requests=1, reported_usd=0.007),
        ServiceUsageCall(source_id="src_gone", service_id="retired/search", at=NOW, uncertain_requests=1),
    ])
    identities = [SourceIdentity("src_exa", "Exa")]

    for prices in (PRICES, None):
        _assert_valid(ledger.summary(days=30, now=NOW, identities=identities, prices=prices, service_catalog=CATALOG))
        for window in ("24h", "7d"):
            _assert_valid(
                ledger.report(window=window, now=NOW, identities=identities, prices=prices, service_catalog=CATALOG)
            )


def test_the_usage_read_values_service_rows_at_the_release_catalog(tmp_path, monkeypatch):
    """The Model Hub usage read labels and prices a service row from the bundled catalog, not from Source config."""

    from tests.scenario_harness.model_hub import (
        MemoryModelHubStore,
        ModelHubScenarioAdapter,
        config_with_sources,
        service_for,
    )
    from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_entry

    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    service = service_for(
        tmp_path, MemoryModelHubStore(config_with_sources([])), ModelHubScenarioAdapter(), now=lambda: NOW
    )
    service.price_table = lambda: PRICES
    service.usage.record_many([
        ServiceUsageCall(source_id="src_tavily", service_id="tavily/search", at=NOW, billable_requests=1),
    ])
    tavily = api_key_vendor_entry("tavily")

    for usage in (service.usage_summary(window="7d"), service.usage_summary(days=7)):
        (entry,) = usage["services"]
        assert entry["service_label"] == tavily.label
        assert entry["priced"] is True
        assert entry["api_cost_usd"] == pytest.approx(tavily.search["price"]["usd_per_1k"] / 1000)
        assert usage["pricing"]["service_price_date"] == tavily.search["price"]["as_of"]
