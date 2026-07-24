from __future__ import annotations

from tests.scenario_harness.model_hub_native_oauth import (
    HubOAuthScenarioHarness,
    NativeOAuthScenarioHarness,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.ui_server import app

BASE_URL = "http://127.0.0.1:15131"


def _client(monkeypatch, service):
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    return client, csrf_headers(client, BASE_URL)


def test_mh_oauth_native_001_claude_paste_code_happy_path(monkeypatch, tmp_path):
    """Scenario: MH-OAUTH-NATIVE-001."""

    harness = NativeOAuthScenarioHarness(tmp_path)
    harness.auth_status["claude"] = {"active_auth_mode": "oauth"}
    client, headers = _client(monkeypatch, harness.service)

    started = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "native_cli"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    assert started["state"] == "awaiting_action"
    assert started["presentation"] == {
        "auth_url": "https://claude.ai/oauth/authorize?test=true",
        "device_code": None,
        "expects": "paste_code",
        "instructions_key": "settings.models.oauth.pasteCode.hint",
    }

    invalid = client.post(
        "/api/models/oauth/submit",
        json={"flow_id": started["flow_id"], "value": "not-a-callback"},
        headers=headers,
        base_url=BASE_URL,
    )
    assert invalid.status_code == 200
    assert invalid.get_json()["flow"]["state"] == "awaiting_action"
    assert harness.agent_auth.submissions == []

    submitted = client.post(
        "/api/models/oauth/submit",
        json={"flow_id": started["flow_id"], "value": "code-value#state-value"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    assert submitted["state"] == "verifying"
    assert harness.agent_auth.submissions == [(started["flow_id"], "code-value#state-value")]

    harness.agent_auth.complete(started["flow_id"])
    completed = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    ).get_json()["flow"]
    sources = client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"]

    assert completed["state"] == "success"
    assert len(sources) == 1
    assert sources[0]["id"] == started["source_id"]
    assert sources[0]["state"] == {"status": "active", "retry_at": None, "detail_key": None}
    assert sources[0]["account_label"] is None
    assert sources[0]["credential_ref"] is None
    assert harness.store.config.sources[0].id == started["source_id"]
    harness.adapter._flows.clear()
    harness.agent_auth.flows.clear()
    repeated = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    )
    assert repeated.status_code == 200
    assert repeated.get_json()["flow"]["state"] == "success"
    assert len(client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"]) == 1
    claude = next(
        agent
        for agent in client.get("/api/models/agents", base_url=BASE_URL).get_json()["agents"]
        if agent["backend"] == "claude"
    )
    assert claude["current"]["source_id"] == started["source_id"]


def test_mh_oauth_native_002_codex_device_code_self_completes(monkeypatch, tmp_path):
    """Scenario: MH-OAUTH-NATIVE-002."""

    harness = NativeOAuthScenarioHarness(tmp_path)
    harness.auth_status["codex"] = {
        "active_auth_mode": "oauth",
        "chatgpt_account": {
            "email": "chatgpt-owner@example.com",
            "plan_type": "plus",
            "organizations": [{"title": "Example Org", "is_default": True}],
        },
    }
    client, headers = _client(monkeypatch, harness.service)

    started = client.post(
        "/api/models/oauth/start",
        json={"vendor": "openai", "channel": "native_cli"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    assert started["state"] == "starting"
    assert started["presentation"]["expects"] == "none"

    harness.agent_auth.expose_codex_device_flow(started["flow_id"])
    awaiting = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    ).get_json()["flow"]
    assert awaiting["state"] == "awaiting_action"
    assert awaiting["presentation"]["auth_url"] == "https://auth.openai.com/codex/device"
    assert awaiting["presentation"]["device_code"] == "T74L-XU61D"
    assert awaiting["presentation"]["expects"] == "none"

    harness.agent_auth.complete(started["flow_id"])
    completed = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    ).get_json()["flow"]
    source = client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"][0]

    assert completed["state"] == "success"
    assert harness.agent_auth.submissions == []
    assert source["state"]["status"] == "active"
    assert source["account_label"] == "chatgpt-owner@example.com \u00b7 plus \u00b7 Example Org"
    assert source["credential_ref"] is None


def test_mh_oauth_native_003_cancel_and_timeout_terminate_cleanly(monkeypatch, tmp_path):
    """Scenario: MH-OAUTH-NATIVE-003."""

    harness = NativeOAuthScenarioHarness(tmp_path)
    client, headers = _client(monkeypatch, harness.service)

    timed = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "native_cli"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    harness.agent_auth.timeout(timed["flow_id"])
    timed_out = client.get(
        f"/api/models/oauth/status/{timed['flow_id']}",
        base_url=BASE_URL,
    ).get_json()["flow"]
    assert timed_out["state"] == "failed"
    assert timed_out["error_key"] == "settings.models.oauth.error.timeout"
    assert client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"] == []

    signed_out = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "native_cli"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    harness.auth_status["claude"] = {"active_auth_mode": "none"}
    harness.agent_auth.complete(signed_out["flow_id"])
    client.get(f"/api/models/oauth/status/{signed_out['flow_id']}", base_url=BASE_URL)
    source = client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"][0]
    assert source["state"] == {
        "status": "error",
        "retry_at": None,
        "detail_key": "settings.models.source.oauthSignedOut",
    }
    claude = next(
        agent
        for agent in client.get("/api/models/agents", base_url=BASE_URL).get_json()["agents"]
        if agent["backend"] == "claude"
    )
    assert claude["current"] is None

    cancelled = client.post(
        "/api/models/oauth/start",
        json={"vendor": "openai", "channel": "native_cli"},
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    response = client.post(
        "/api/models/oauth/cancel",
        json={"flow_id": cancelled["flow_id"]},
        headers=headers,
        base_url=BASE_URL,
    )
    assert response.status_code == 200
    assert harness.agent_auth.cancelled == [cancelled["flow_id"]]
    assert client.get(
        f"/api/models/oauth/status/{cancelled['flow_id']}",
        base_url=BASE_URL,
    ).status_code == 404


def test_mh_oauth_consent_001_is_required_and_persisted(monkeypatch, tmp_path):
    """Scenario: MH-OAUTH-CONSENT-001."""

    harness = HubOAuthScenarioHarness(tmp_path)
    client, headers = _client(monkeypatch, harness.service)

    rejected = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "hub"},
        headers=headers,
        base_url=BASE_URL,
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error"] == "consent_required"
    assert harness.adapter.flows == {}

    started = client.post(
        "/api/models/oauth/start",
        json={
            "vendor": "anthropic",
            "channel": "hub",
            "experimental_consent": True,
        },
        headers=headers,
        base_url=BASE_URL,
    ).get_json()["flow"]
    binding = harness.service.oauth_flows.binding(started["flow_id"])
    assert binding is not None
    assert binding.experimental_consent is True

    harness.adapter.complete(started["flow_id"])
    completed = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    ).get_json()["flow"]
    source = client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"][0]

    assert completed["state"] == "success"
    assert source["id"] == started["source_id"]
    assert source["supply_channel"] == "hub"
    assert source["experimental_consent_at"] == "2026-07-25T00:00:00+00:00"

    harness.adapter.flows.clear()
    repeated = client.get(
        f"/api/models/oauth/status/{started['flow_id']}",
        base_url=BASE_URL,
    )
    assert repeated.status_code == 200
    assert repeated.get_json()["flow"]["state"] == "success"
    assert len(client.get("/api/models/sources", base_url=BASE_URL).get_json()["sources"]) == 1
