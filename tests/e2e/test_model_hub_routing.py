"""API-feasible Model Hub routing and fallback E2E scenarios."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream
from tests.e2e.test_model_hub_migration import _write
from tests.e2e.test_model_hub_runtime import (
    _engine_app,
    _install_engine,
)
from tests.e2e.test_model_hub_sources import (
    MENU_MODEL,
    SYNTHETIC_API_KEY,
    _configure_protocol,
    _create_source,
)


pytestmark = pytest.mark.e2e_model_hub


def _seed_codex_native_login(home) -> None:
    _write(
        home / ".codex" / "auth.json",
        (
            '{"auth_mode":"chatgpt","tokens":'
            '{"access_token":"codex-native-e2e-123456"}}'
        ),
    )
    _write(
        home / ".codex" / "config.toml",
        'cli_auth_credentials_store = "file"\n',
    )


def test_d1_subscription_added_after_api_key_is_appended_at_tail(
    mock_llm_upstream,
    model_hub_app,
) -> None:
    """D1: current placement appends a later native subscription."""

    # D-1 remains open: current shipped behavior appends at tail, so the API
    # key precedes and can burn before the later subscription.
    _configure_protocol(
        mock_llm_upstream,
        "openai_responses",
        models=[{"id": "gpt-5.3-codex"}],
    )
    api_source, _ = _create_source(
        model_hub_app,
        mock_llm_upstream,
        protocol="openai_responses",
        nonce="scn_d100000000000001",
    )
    _seed_codex_native_login(model_hub_app.home)
    scan = model_hub_app.client.post("/api/models/migration/scan", {})
    scan_body = scan.json()
    assert scan.status == 200, scan_body
    [native_item] = [
        item
        for item in scan_body["scan"]["items"]
        if item["kind"] == "oauth_native"
    ]
    applied = model_hub_app.client.post(
        "/api/models/migration/apply", {"item_ids": [native_item["id"]]}
    )
    applied_body = applied.json()
    assert applied.status == 200, applied_body
    native_source = next(
        source
        for source in applied_body["sources"]
        if (
            source["vendor"] == "openai"
            and source["kind"] == "subscription"
            and source["supply_channel"] == "native_cli"
        )
    )

    mode = model_hub_app.client.patch(
        "/api/models/agents/codex/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()

    agent = model_hub_app.client.get(
        "/api/models/agents/codex/sources"
    )
    assert agent.status == 200, agent.json()
    agent_body = agent.json()
    assert agent_body["agent"]["sources"]["order"] == [
        api_source["id"],
        native_source["id"],
    ]


def _prepare_probe_chain(
    app,
    first_upstream: MockLLMUpstream,
    second_upstream: MockLLMUpstream | None = None,
):
    _install_engine(app)
    _configure_protocol(
        first_upstream,
        "anthropic",
        models=[{"id": MENU_MODEL}],
    )
    first, _ = _create_source(
        app,
        first_upstream,
        nonce="scn_dprobe0000000001",
        vendor="anthropic",
        display_name="First probe source",
    )
    sources = [first]
    if second_upstream is not None:
        _configure_protocol(
            second_upstream,
            "anthropic",
            models=[{"id": MENU_MODEL}],
        )
        second, _ = _create_source(
            app,
            second_upstream,
            nonce="scn_dprobe0000000002",
            vendor="anthropic",
            display_name="Second probe source",
        )
        sources.append(second)
    mode = app.client.patch(
        "/api/models/agents/claude/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()
    chain = app.client.put(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}",
        {
            "hops": [
                {"source_id": source["id"], "model_id": MENU_MODEL}
                for source in sources
            ]
        },
    )
    assert chain.status == 200, chain.json()
    started = app.client.post("/api/models/runtime/start", {})
    assert started.status == 200, started.json()
    assert started.json()["runtime"]["status"]["health"] == "ok"
    first_upstream.reset_requests()
    if second_upstream is not None:
        second_upstream.reset_requests()
    return sources


def _probe(app):
    return app.client.post(
        "/api/models/agents/claude/probe", {"model": MENU_MODEL}
    )


def _source_by_id(app, source_id: str):
    sources = app.client.get("/api/models/sources")
    assert sources.status == 200, sources.json()
    return next(
        source
        for source in sources.json()["sources"]
        if source["id"] == source_id
    )


def test_d2_healthy_hop_is_served_by_the_real_engine_probe(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D2 API subset: a healthy exact hop reaches the mock through the engine."""

    with _engine_app(model_hub_app_factory) as app:
        [source] = _prepare_probe_chain(app, mock_llm_upstream)
        response = _probe(app)
        body = response.json()
        assert response.status == 200, body
        assert body["probe"]["reachable"] is True
        assert body["probe"]["source_id"] == source["id"]
        assert body["probe"]["model_id"] == MENU_MODEL
        captured = [
            item
            for item in mock_llm_upstream.requests()
            if item["path"] == "/v1/messages"
        ]
        assert captured
        # The engine forwards Anthropic credentials as Bearer; x-api-key is
        # used only by Avibe's direct discovery probe path.
        assert all(
            item["headers"].get("authorization")
            == f"Bearer {SYNTHETIC_API_KEY}"
            for item in captured
        )
        assert all(
            item["headers"].get("anthropic-version") for item in captured
        )


def _assert_probe_cooldown_and_next_request_selection(
    *,
    app,
    first_upstream,
    second_upstream,
    auth_behavior: str,
    expected_detail: str,
    expected_reason: str,
    expected_delay: int,
) -> None:
    first, second = _prepare_probe_chain(
        app, first_upstream, second_upstream
    )
    first_upstream.configure(auth=auth_behavior)
    failed = _probe(app)
    failed_body = failed.json()
    assert failed.status == 200, failed_body
    assert failed_body["probe"]["reachable"] is False
    assert failed_body["probe"]["source_id"] == first["id"]
    first_state = _source_by_id(app, first["id"])["state"]
    assert first_state["status"] == "cooldown"
    assert first_state["detail_key"] == expected_detail
    retry_at = datetime.fromisoformat(first_state["retry_at"])
    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    assert expected_delay - 8 <= delay <= expected_delay + 2

    next_probe = _probe(app)
    next_body = next_probe.json()
    assert next_probe.status == 200, next_body
    assert next_body["probe"]["reachable"] is True
    assert next_body["probe"]["source_id"] == second["id"]
    events = app.client.get("/api/models/events?limit=10").json()["events"]
    assert any(
        event["kind"] == "cooldown"
        and event["from_source"] == first["id"]
        and event["reason"] == expected_reason
        for event in events
    )


def test_d3_rate_limit_moves_the_next_api_probe_to_hop_one(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D3 partial API evidence: cooldown written; next-request selection proven.

    The same-turn hop walk is not covered here; it requires the private turn
    gateway credential issued only to a live backend turn.
    """

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="429",
                expected_detail="models.source.cooldown.rate_limited",
                expected_reason="rate_limited",
                expected_delay=60,
            )


def test_d4_quota_moves_the_next_api_probe_to_hop_one(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D4 partial API evidence: cooldown written; next-request selection proven.

    The same-turn hop walk is not covered here; it requires the private turn
    gateway credential issued only to a live backend turn.
    """

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="quota_message",
                expected_detail="models.source.cooldown.quota_exhausted",
                expected_reason="quota_exhausted",
                expected_delay=300,
            )


def test_d5_nonrefreshable_401_marks_the_key_for_repair(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D5 API subset: a static key is revoked without an in-probe refresh."""

    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            first, second = _prepare_probe_chain(
                app, mock_llm_upstream, second_upstream
            )
            mock_llm_upstream.configure(auth="401")
            failed = _probe(app)
            assert failed.status == 200, failed.json()
            assert failed.json()["probe"]["reachable"] is False
            state = _source_by_id(app, first["id"])["state"]
            assert state == {
                "status": "needs_action",
                "retry_at": None,
                "detail_key": (
                    "models.source.needs_action.credential_revoked"
                ),
            }
            inference_requests = [
                item
                for item in mock_llm_upstream.requests()
                if item["path"] == "/v1/messages"
            ]
            assert len(inference_requests) == 1
            next_probe = _probe(app)
            assert next_probe.status == 200, next_probe.json()
            assert next_probe.json()["probe"]["source_id"] == second["id"]


def test_d6_server_error_ignores_retry_after_and_uses_flat_cooldown(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """D6 API subset: a 5xx uses 30s despite Retry-After: 600."""

    # D-4 is open. The current contract deliberately records the flat 30s
    # server-error cooldown rather than honoring the upstream Retry-After.
    with MockLLMUpstream() as second_upstream:
        with _engine_app(model_hub_app_factory) as app:
            _assert_probe_cooldown_and_next_request_selection(
                app=app,
                first_upstream=mock_llm_upstream,
                second_upstream=second_upstream,
                auth_behavior="5xx",
                expected_detail="models.source.cooldown.server_error",
                expected_reason="server_error",
                expected_delay=30,
            )
