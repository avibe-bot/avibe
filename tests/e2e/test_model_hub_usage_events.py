"""Model Hub usage and resolution-event E2E scenarios."""

from __future__ import annotations

import json

import pytest

from tests.e2e.test_model_hub_sources import (
    _configure_protocol,
    _create_source,
)


pytestmark = pytest.mark.e2e_model_hub


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
