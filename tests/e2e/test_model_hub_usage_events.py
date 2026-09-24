"""Model Hub usage and resolution-event E2E scenarios."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from core.handlers.model_hub.usage import BoundedUsageLedger, UsageWriter
from tests.e2e.test_model_hub_sources import (
    _configure_protocol,
    _create_source,
)


pytestmark = pytest.mark.e2e_model_hub


@pytest.fixture
def usage_analytics_app(model_hub_app_factory):
    """Seed configured identities without depending on live credential discovery."""

    def seed_sources(app) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from config.v2_config import (
    MODEL_HUB_BACKENDS, ModelHubModelConfig, ModelHubSourceConfig,
    ModelHubSourceStateConfig, V2Config,
)
config = V2Config.default()
config.model_hub.enabled = False
for backend in MODEL_HUB_BACKENDS:
    config.model_hub.agents[backend].mode = "direct"
config.model_hub.sources = [
    ModelHubSourceConfig(
        id=source_id, kind="api_key", vendor="custom",
        display_name="供应商 · 同名", protocol="openai_chat",
        supply_channel="hub", billing="metered",
        state=ModelHubSourceStateConfig(),
        models=[
            ModelHubModelConfig(id=model_id, provenance="manual")
            for model_id in ("shared-model", "模型-β")
        ],
        credential_ref="synthetic-unavailable-" + source_id,
    )
    for source_id in ("src_usageaaa1", "src_usagebbb2")
]
config.update.auto_update = False
config.update.check_interval_minutes = 0
config.save()
""",
            ],
            cwd=app.repo_root,
            env=app.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    with model_hub_app_factory(before_start=seed_sources) as app:
        yield app


def test_e2_usage_windows_are_bounded(
    model_hub_app,
) -> None:
    """E2: valid, garbage, and pathological windows stay bounded."""

    for query, expected in (
        ("1", 1),
        ("62", 62),
        ("garbage", 30),
        ("100000", 62),
    ):
        response = model_hub_app.client.get(
            f"/api/models/usage?days={query}"
        )
        body = response.json()
        assert response.status == 200, body
        assert body["usage"]["window_days"] == expected
        assert body["usage"]["totals"] == {
            "requests": 0,
            "token_reports": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }


def test_e2_usage_reports_reconcile_across_http_ipc_and_restart(
    usage_analytics_app,
) -> None:
    """MH-USAGE-E2E-001: the matrix retains exact pair identities through real IPC."""

    model_hub_app = usage_analytics_app
    common_model = "shared-model"
    unicode_model = "模型-β"
    sources = model_hub_app.client.get("/api/models/sources").json()["sources"]
    first = next(source for source in sources if source["id"] == "src_usageaaa1")
    second = next(source for source in sources if source["id"] == "src_usagebbb2")
    assert first["id"] != second["id"]
    now = datetime.now(timezone.utc)
    ledger = BoundedUsageLedger(
        model_hub_app.avibe_home / "state" / "model_hub_usage.json"
    )
    calls = (
        (first["id"], common_model, timedelta(minutes=70), (150, 100, 25)),
        (first["id"], common_model, timedelta(minutes=130), (40, 20, 5)),
        (second["id"], common_model, timedelta(minutes=130), (80, 0, 10)),
        (second["id"], unicode_model, timedelta(minutes=190), None),
        (first["id"], common_model, timedelta(hours=48), (200, 40, 30)),
    )

    async def meter_calls() -> None:
        # Queue together so the production writer must preserve different hours
        # for the same pair before flushing one atomic ledger transaction.
        writer = UsageWriter(ledger)
        for source_id, model_id, ago, counts in calls:
            writer.record(
                source_id=source_id,
                model_id=model_id,
                usage=(
                    ProtocolUsageReport.of(
                        input_tokens=counts[0],
                        cached_input_tokens=counts[1],
                        output_tokens=counts[2],
                    )
                    if counts is not None
                    else None
                ),
                at=now - ago,
            )
        assert await writer.drain(timeout=5) == 0

    # The test runtime has no active turns, so the controller cannot race this
    # fixture with another metering write.
    asyncio.run(meter_calls())

    expected_hourly = {
        "requests": 4,
        "token_reports": 3,
        "input_tokens": 270,
        "cached_input_tokens": 120,
        "output_tokens": 40,
    }
    expected_daily = {
        "requests": 5,
        "token_reports": 4,
        "input_tokens": 470,
        "cached_input_tokens": 160,
        "output_tokens": 70,
    }
    for window, count in (("24h", 24), ("7d", 7), ("30d", 30), ("60d", 60)):
        response = model_hub_app.client.get(f"/api/models/usage?window={window}")
        body = response.json()
        assert response.status == 200, body
        report = body["usage"]
        expected = expected_hourly if window == "24h" else expected_daily
        assert report["window_key"] == window
        assert report["granularity"] == ("hour" if window == "24h" else "day")
        assert report["totals"] == expected
        buckets = report["buckets"]
        assert len(buckets) == count
        assert len({bucket["key"] for bucket in buckets}) == count
        assert buckets[0]["start_at"] == report["from_at"]
        assert buckets[-1]["end_at"] == report["to_at"]
        for before, after in zip(buckets, buckets[1:]):
            assert before["end_at"] == after["start_at"]
        for bucket in buckets:
            assert isinstance(bucket["history_complete"], bool)
            start = datetime.fromisoformat(bucket["start_at"])
            end = datetime.fromisoformat(bucket["end_at"])
            assert start.utcoffset() is not None
            assert end.utcoffset() is not None
            assert start <= end
            pairs = [
                (row["source_id"], row["model_id"]) for row in bucket["rows"]
            ]
            assert len(pairs) == len(set(pairs))
        rows = [row for bucket in buckets for row in bucket["rows"]]
        if window == "24h":
            assert len([
                row for row in rows
                if row["source_id"] == first["id"] and row["model_id"] == common_model
            ]) == 2
        assert {(row["source_id"], row["model_id"]) for row in rows} == {
            (first["id"], common_model),
            (second["id"], common_model),
            (second["id"], unicode_model),
        }
        for key, value in expected.items():
            assert sum(row[key] for row in rows) == value
            assert sum(source[key] for source in report["sources"]) == value
            assert sum(day[key] for day in report["days"]) == value
        assert {source["label"] for source in report["sources"]} == {
            "供应商 · 同名"
        }
        for source in report["sources"]:
            for key in expected:
                assert sum(model[key] for model in source["models"]) == source[key]

    for query in ("window=1d", "window=", "window=24h&days=7"):
        response = model_hub_app.client.get(f"/api/models/usage?{query}")
        assert response.status == 400, response.json()
    legacy = model_hub_app.client.get("/api/models/usage?days=7").json()["usage"]
    assert legacy["totals"] == expected_daily
    assert "buckets" not in legacy

    model_hub_app.restart_controller()
    for window, expected in (("24h", expected_hourly), ("7d", expected_daily)):
        response = model_hub_app.client.get(f"/api/models/usage?window={window}")
        assert response.status == 200, response.json()
        assert response.json()["usage"]["totals"] == expected


def test_e2_daily_only_history_is_not_invented_as_hourly_usage(
    model_hub_app_factory,
) -> None:
    """MH-USAGE-E2E-002: released daily files never fabricate hourly history."""

    now = datetime.now(timezone.utc)
    counts = {
        "requests": 3,
        "token_reports": 2,
        "input_tokens": 150,
        "cached_input_tokens": 100,
        "output_tokens": 25,
    }

    def seed_legacy(app) -> None:
        path = app.avibe_home / "state" / "model_hub_usage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{
                "day": now.astimezone().date().isoformat(),
                "source_id": "historical-source",
                "model_id": "历史模型",
                "last_metered_at": now.isoformat(),
                **counts,
            }]),
            encoding="utf-8",
        )

    with model_hub_app_factory(before_start=seed_legacy) as app:
        daily = app.client.get("/api/models/usage?window=7d")
        assert daily.status == 200, daily.json()
        assert daily.json()["usage"]["totals"] == counts
        hourly = app.client.get("/api/models/usage?window=24h")
        assert hourly.status == 200, hourly.json()
        report = hourly.json()["usage"]
        assert report["totals"] == dict.fromkeys(counts, 0)
        assert any(not bucket["history_complete"] for bucket in report["buckets"])
        assert not any(bucket["rows"] for bucket in report["buckets"])


def test_e3_event_feed_paginates_and_never_persists_credentials(
    mock_llm_upstream,
    model_hub_app,
) -> None:
    """E3: event pages are cursor-stable and credential-free on disk/wire."""

    _configure_protocol(mock_llm_upstream)
    source, _ = _create_source(model_hub_app, mock_llm_upstream)
    secret = "sk-model-hub-e2e-not-real"
    mock_llm_upstream.configure(models_endpoint="http_500")
    for _ in range(3):
        response = model_hub_app.client.post(
            f"/api/models/sources/{source['id']}/refresh", {}
        )
        assert response.status == 502, response.json()
        assert response.json()["error"] == "discovery_failed"

    first = model_hub_app.client.get("/api/models/events?limit=2")
    first_body = first.json()
    assert first.status == 200, first_body
    assert len(first_body["events"]) == 2
    assert all(event["kind"] == "needs_action" for event in first_body["events"])
    assert all(
        event["reason"] == "unclassified_error"
        for event in first_body["events"]
    )

    cursor = first_body["events"][-1]["id"]
    second = model_hub_app.client.get(
        f"/api/models/events?limit=2&before={cursor}"
    )
    second_body = second.json()
    assert second.status == 200, second_body
    assert len(second_body["events"]) == 1
    assert second_body["events"][0]["id"] not in {
        event["id"] for event in first_body["events"]
    }

    wire = json.dumps(
        [*first_body["events"], *second_body["events"]],
        sort_keys=True,
    )
    assert secret not in wire
    assert "authorization" not in wire.lower()
    event_file = (
        model_hub_app.avibe_home
        / "state"
        / "model_hub_resolution_events.json"
    )
    assert event_file.is_file()
    assert secret not in event_file.read_text(encoding="utf-8")
