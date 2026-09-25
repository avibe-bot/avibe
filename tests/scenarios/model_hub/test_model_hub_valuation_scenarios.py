"""Model Hub API-price valuation, closed loop: metered wire to served usage and quota value.

One upstream response travels the production path: protocol usage extraction,
the shared usage writer, the persisted ledger, the service's price lookup, and the
usage and quota reads the Web UI renders. The unit suites pin each layer; this
proves the layers are wired to each other.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource

from core.handlers.model_hub.pricing import PriceTable
from core.handlers.model_hub.stream_wire import extract_protocol_usage
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    config_with_sources,
    service_for,
    source,
)


CONTRACTS = Path("docs/plans/model-hub-contracts")
NOW = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)
CATALOG = {
    "anthropic": {
        "models": {"claude-opus-5": {"cost": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}}}
    }
}
VENDOR_MAP = {"families": [{"prefix": "claude-", "vendor_id": "anthropic"}]}
# An Anthropic message_start as the upstream streams it: fresh input, a cache read,
# and a cache write split into its five-minute and one-hour lifetimes.
MESSAGE_START = {
    "type": "message_start",
    "message": {
        "usage": {
            "input_tokens": 100_000,
            "cache_read_input_tokens": 600_000,
            "cache_creation_input_tokens": 300_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 200_000, "ephemeral_1h_input_tokens": 100_000},
            "output_tokens": 0,
        }
    },
}
MESSAGE_DELTA = {"type": "message_delta", "usage": {"output_tokens": 100_000}}
# fresh 100k × 5 + read 600k × 0.5 + 5m 200k × 6.25 + 1h 100k × 10 + out 100k × 25
EXPECTED_USD = 0.5 + 0.3 + 1.25 + 1.0 + 2.5


@pytest.fixture(autouse=True)
def _enable_model_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


def _assert_valid(name: str, payload: dict) -> None:
    registry = Registry()
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    schema = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema, registry=registry, format_checker=FormatChecker()).iter_errors(payload))
    assert not errors, [error.message for error in errors]


class QuotaAdapter(ModelHubScenarioAdapter):
    async def subscription_quota(self, source_id, vendor, credential_ref):
        return {"plan": "max_20x", "windows": [{
            "id": "five_hour", "kind": "session", "label": "five_hour", "used_pct": 12.0,
            "window_seconds": 18000, "resets_at": "2026-09-25T08:00:00Z",
        }]}


def _service(tmp_path: Path):
    config = config_with_sources([source("src_max", ["claude-opus-5"], kind="subscription")])
    service = service_for(tmp_path, MemoryModelHubStore(config), QuotaAdapter(), now=lambda: NOW)
    service.price_table = lambda: PriceTable(
        catalog=CATALOG, vendor_map=VENDOR_MAP, overrides={}, price_table_date="2026-09-23"
    )
    return service


def test_mh_price_scenario_001_a_metered_stream_is_valued_through_usage_and_quota(tmp_path):
    """MH-PRICE-SCENARIO-001: A metered stream's cache writes reach the served usage and quota value at API price."""

    service = _service(tmp_path)
    report = extract_protocol_usage("anthropic", MESSAGE_START).merge(
        extract_protocol_usage("anthropic", MESSAGE_DELTA)
    )

    async def meter() -> None:
        service.usage_writer.record(source_id="src_max", model_id="claude-opus-5", usage=report, at=NOW)
        # A second call whose stream ended without a usage report.
        service.usage_writer.record(source_id="src_max", model_id="claude-opus-5", usage=None, at=NOW)
        assert await service.usage_writer.drain(timeout=5) == 0

    asyncio.run(meter())

    for window in ("24h", "7d"):
        usage = service.usage_summary(window=window)
        _assert_valid("usage-summary.schema.json", usage)
        totals = usage["totals"]
        assert totals["cache_write_input_tokens"] == 300_000
        assert totals["cache_write_1h_input_tokens"] == 100_000
        assert totals["api_cost_usd"] == pytest.approx(EXPECTED_USD)
        # The unreported call makes every figure above it a floor, never exact.
        assert totals["api_cost_lower_bound"] is True
        assert usage["pricing"]["price_table_date"] == "2026-09-23"

    quota = asyncio.run(service.quota_summary())
    _assert_valid("quota-summary.schema.json", quota)
    value = quota["sources"][0]["value"]
    assert value["plan_key"] == "claude_max_20x"
    assert value["fee_usd"] == 200
    assert value["week"]["api_cost_usd"] == pytest.approx(EXPECTED_USD)
    assert value["week"]["api_cost_lower_bound"] is True
    assert value["period"]["api_cost_lower_bound"] is True
    assert quota["value"]["period"]["api_cost_lower_bound"] is True
    assert "cred_" not in json.dumps(quota)
