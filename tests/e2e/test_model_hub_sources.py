"""Model Hub API-key Source scenarios over real HTTP and controller IPC."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream


pytestmark = pytest.mark.e2e_model_hub

CONTRACT_VERSION = 9
MENU_MODEL = "claude-sonnet-4-6"
SYNTHETIC_API_KEY = "sk-model-hub-e2e-not-real"


def _configure_protocol(
    upstream: MockLLMUpstream,
    protocol: str = "anthropic",
    *,
    models: list[dict[str, Any]] | None = None,
) -> None:
    upstream.configure(
        auth="ok",
        stream="healthy",
        protocol=protocol,
        models_endpoint="ok",
        model_errors={},
        models=models
        if models is not None
        else [{"id": "mock-model"}],
    )
    upstream.reset_requests()


def _source_payload(
    upstream: MockLLMUpstream,
    *,
    protocol: str = "anthropic",
    nonce: str = "scn_01j5w8z7p4n6q2rt",
    vendor: str = "custom",
    display_name: str = "E2E mock source",
) -> dict[str, Any]:
    return {
        "kind": "api_key",
        "vendor": vendor,
        "display_name": display_name,
        "base_url": upstream.url,
        "key": SYNTHETIC_API_KEY,
        "protocol": protocol,
        "client_nonce": nonce,
    }


def _create_source(
    app,
    upstream: MockLLMUpstream,
    *,
    protocol: str = "anthropic",
    nonce: str = "scn_01j5w8z7p4n6q2rt",
    vendor: str = "custom",
    display_name: str = "E2E mock source",
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        **_source_payload(
            upstream,
            protocol=protocol,
            nonce=nonce,
            vendor=vendor,
            display_name=display_name,
        ),
        **(extra or {}),
    }
    response = app.client.post("/api/models/sources", payload)
    body = response.json()
    assert response.status == 201, body
    assert body["ok"] is True
    assert body["contract_version"] == CONTRACT_VERSION
    return body["source"], payload


@pytest.mark.parametrize(
    "protocol",
    ["anthropic", "openai_responses", "openai_chat"],
)
def test_b1_auto_observe_selects_protocol_from_response_shape(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
    protocol: str,
) -> None:
    """B1: auto observation proves each protocol from response evidence."""

    _configure_protocol(mock_llm_upstream, protocol)
    request = _source_payload(mock_llm_upstream, protocol=protocol)
    request.pop("kind")
    request.pop("display_name")
    request.pop("client_nonce")
    request.pop("protocol")  # Wire form for the UI's `auto` selection.

    response = model_hub_app.client.post(
        "/api/models/sources/observe", request
    )
    body = response.json()
    assert response.status == 200, body
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["observation"] == {
        "contract_version": CONTRACT_VERSION,
        "outcome": "observed",
        "reachable": True,
        "authenticated": "authenticated",
        "protocol": protocol,
        "discovery": "succeeded",
        "models": ["mock-model"],
        "model_metadata": [
            {"id": "mock-model", "supported_parameters": None}
        ],
    }
    captured_paths = [item["path"] for item in mock_llm_upstream.requests()]
    assert "/v1/models" in captured_paths
    assert any(path != "/v1/models" for path in captured_paths)


def test_b2_manual_protocol_mismatch_is_refused_with_proof_error(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B2: a manual protocol cannot persist contrary response evidence."""

    _configure_protocol(mock_llm_upstream, "openai_chat")
    response = model_hub_app.client.post(
        "/api/models/sources",
        _source_payload(mock_llm_upstream, protocol="anthropic"),
    )
    body = response.json()
    assert response.status == 422, body
    assert body["ok"] is False
    assert body["error"] == "discovery_failed"
    assert body["detail"].endswith("ambiguous_source")
    assert body["observation"]["protocol"] is None
    assert body["observation"]["outcome"] == "ambiguous"
    listed = model_hub_app.client.get("/api/models/sources").json()
    assert listed["sources"] == []


def test_b3_discovered_model_persists_only_contract_fields(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B3: the one supported extension survives; unrelated metadata does not."""

    _configure_protocol(
        mock_llm_upstream,
        "anthropic",
        models=[
            {
                "id": "relay-model",
                "display_name": "Upstream display",
                "context_length": 128_000,
                "pricing": {"input": "0.01", "output": "0.02"},
                "supported_parameters": ["reasoning"],
            }
        ],
    )
    source, _ = _create_source(model_hub_app, mock_llm_upstream)
    [model] = source["models"]
    assert set(model) == {
        "id",
        "display_name",
        "origin",
        "reasoning_efforts",
        "reasoning_efforts_source",
        "discovered_at",
        "retired",
    }
    assert model["id"] == "relay-model"
    assert model["display_name"] is None
    assert model["origin"] == "discovered"
    assert model["reasoning_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert model["reasoning_efforts_source"] == "upstream"
    assert model["retired"] is False
    assert model["discovered_at"]
    serialized = str(model)
    assert "context_length" not in serialized
    assert "pricing" not in serialized


@pytest.mark.parametrize(
    "models_endpoint",
    ["http_404", "http_500", "malformed_json"],
)
def test_b4_inventory_failure_requires_consent_then_commits_error_source(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
    models_endpoint: str,
) -> None:
    """B4: protocol proof survives an independently failed inventory."""

    _configure_protocol(mock_llm_upstream, "openai_chat")
    mock_llm_upstream.configure(models_endpoint=models_endpoint)
    payload = _source_payload(
        mock_llm_upstream,
        protocol="openai_chat",
        nonce={
            "http_404": "scn_b400000000000001",
            "http_500": "scn_b400000000000002",
            "malformed_json": "scn_b400000000000003",
        }[models_endpoint],
    )

    rejected = model_hub_app.client.post(
        "/api/models/sources", payload
    )
    rejected_body = rejected.json()
    assert rejected.status == 422, rejected_body
    assert rejected_body["error"] == "discovery_failed"
    assert rejected_body["detail"].endswith("inventory_unavailable")
    assert rejected_body["observation"]["protocol"] == "openai_chat"
    assert rejected_body["observation"]["discovery"] == "failed"
    assert model_hub_app.client.get(
        "/api/models/sources"
    ).json()["sources"] == []

    accepted = model_hub_app.client.post(
        "/api/models/sources",
        {**payload, "accept_unavailable_inventory": True},
    )
    accepted_body = accepted.json()
    assert accepted.status == 201, accepted_body
    source = accepted_body["source"]
    assert source["protocol"] == "openai_chat"
    assert source["models"] == []
    assert source["state"] == {
        "status": "error",
        "retry_at": None,
        "detail_key": "models.source.error.unclassified",
    }


@pytest.mark.xfail(
    reason=(
        "B5 product gap: a byte-equivalent replay of a committed Source "
        "nonce currently returns 409 source_nonce_conflict"
    )
)
def test_b5_source_create_nonce_is_idempotent_across_http_replay(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B5: replaying a committed client nonce cannot duplicate a Source."""

    _configure_protocol(mock_llm_upstream)
    first, payload = _create_source(
        model_hub_app,
        mock_llm_upstream,
        nonce="scn_b500000000000001",
    )
    replay = model_hub_app.client.post("/api/models/sources", payload)
    replay_body = replay.json()
    assert replay.status == 201, replay_body
    assert replay_body["source"]["id"] == first["id"]
    sources = model_hub_app.client.get(
        "/api/models/sources"
    ).json()["sources"]
    assert [source["id"] for source in sources] == [first["id"]]


def test_b6_replace_key_rolls_back_then_rename_and_retarget_commit(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B6: failed edits roll back; valid credential and metadata edits commit."""

    _configure_protocol(mock_llm_upstream)
    source, _ = _create_source(model_hub_app, mock_llm_upstream)
    original_ref = source["credential_ref"]
    original_mask = source["masked_credential"]
    revocation_journal = (
        model_hub_app.avibe_home
        / "state"
        / "model_hub_pending_revocations.json"
    )
    assert revocation_journal.is_file()
    assert json.loads(revocation_journal.read_text(encoding="utf-8")) == []
    journal_mtime = revocation_journal.stat().st_mtime_ns

    time.sleep(0.01)
    mock_llm_upstream.configure(auth="401")
    failed = model_hub_app.client.put(
        f"/api/models/sources/{source['id']}/credential",
        {"key": "sk-model-hub-e2e-rejected"},
    )
    failed_body = failed.json()
    assert failed.status == 502, failed_body
    assert failed_body["error"] == "discovery_failed"
    after_failure = model_hub_app.client.get(
        "/api/models/sources"
    ).json()["sources"][0]
    assert after_failure["credential_ref"] == original_ref
    assert after_failure["masked_credential"] == original_mask
    # The journal is the public rollback evidence boundary. An empty, rewritten
    # journal means the rejected replacement was queued before cleanup and then
    # observably removed; engine-private credential storage stays opaque.
    assert revocation_journal.stat().st_mtime_ns > journal_mtime
    assert json.loads(revocation_journal.read_text(encoding="utf-8")) == []

    mock_llm_upstream.configure(auth="ok")
    replaced = model_hub_app.client.put(
        f"/api/models/sources/{source['id']}/credential",
        {"key": "sk-model-hub-e2e-replacement"},
    )
    replaced_body = replaced.json()
    assert replaced.status == 200, replaced_body
    assert replaced_body["source"]["credential_ref"] != original_ref

    renamed = model_hub_app.client.patch(
        f"/api/models/sources/{source['id']}",
        {"display_name": "Renamed E2E source"},
    )
    assert renamed.status == 200, renamed.json()
    assert renamed.json()["source"]["display_name"] == "Renamed E2E source"

    with MockLLMUpstream() as replacement_upstream:
        _configure_protocol(replacement_upstream)
        retargeted = model_hub_app.client.patch(
            f"/api/models/sources/{source['id']}",
            {"base_url": replacement_upstream.url},
        )
        retargeted_body = retargeted.json()
        assert retargeted.status == 200, retargeted_body
        assert retargeted_body["source"]["base_url"] == (
            replacement_upstream.url
        )
        assert any(
            request["path"] == "/v1/models"
            for request in replacement_upstream.requests()
        )


def _create_guarded_source(app, upstream: MockLLMUpstream) -> dict[str, Any]:
    _configure_protocol(
        upstream,
        "anthropic",
        models=[{"id": MENU_MODEL}],
    )
    source, _ = _create_source(
        app,
        upstream,
        nonce="scn_guardedsource0001",
        vendor="anthropic",
    )
    mode = app.client.patch(
        "/api/models/agents/claude/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()
    chain = app.client.put(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}",
        {
            "hops": [
                {"source_id": source["id"], "model_id": MENU_MODEL}
            ]
        },
    )
    assert chain.status == 200, chain.json()
    return source


def test_b7_delete_guard_requires_exact_echo_before_force(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B7: destructive chain impact must be echoed exactly before commit."""

    source = _create_guarded_source(model_hub_app, mock_llm_upstream)
    endpoint = f"/api/models/sources/{source['id']}"
    refused = model_hub_app.client.delete(endpoint, {})
    refusal = refused.json()
    assert refused.status == 409, refusal
    assert refusal["error"] == "source_in_route_chain"
    assert refusal["would_remove_hops"]
    assert refusal["would_interrupt"] == [
        {"backend": "claude", "model_id": MENU_MODEL, "agents": []}
    ]

    stale_hops = [dict(hop) for hop in refusal["would_remove_hops"]]
    stale_hops[0]["model_id"] = "stale-confirmed-model"
    wrong_hops = model_hub_app.client.delete(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": stale_hops,
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    wrong_hops_body = wrong_hops.json()
    assert wrong_hops.status == 409, wrong_hops_body
    assert wrong_hops_body["would_remove_hops"] == refusal["would_remove_hops"]
    assert wrong_hops_body["would_interrupt"] == refusal["would_interrupt"]

    stale_interruptions = [
        dict(interruption) for interruption in refusal["would_interrupt"]
    ]
    stale_interruptions[0]["model_id"] = "stale-confirmed-model"
    wrong_interruptions = model_hub_app.client.delete(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": stale_interruptions,
        },
    )
    wrong_interruptions_body = wrong_interruptions.json()
    assert wrong_interruptions.status == 409, wrong_interruptions_body
    assert wrong_interruptions_body["would_remove_hops"] == refusal[
        "would_remove_hops"
    ]
    assert wrong_interruptions_body["would_interrupt"] == refusal[
        "would_interrupt"
    ]

    committed = model_hub_app.client.delete(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    committed_body = committed.json()
    assert committed.status == 200, committed_body
    assert committed_body["removed_hops"] == refusal["would_remove_hops"]
    assert committed_body["interrupted"] == refusal["would_interrupt"]
    assert model_hub_app.client.get(
        "/api/models/sources"
    ).json()["sources"] == []


def test_b8_delete_force_is_query_only_in_the_current_http_contract(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B8: DELETE force is query-only in the current HTTP contract."""

    # Open decision B6 has no D-1..D-4 identifier in plan section 5:
    # normalize destructive force transport or retain the current split.
    source = _create_guarded_source(model_hub_app, mock_llm_upstream)
    endpoint = f"/api/models/sources/{source['id']}"
    refusal = model_hub_app.client.delete(endpoint, {}).json()

    body_force = model_hub_app.client.delete(
        endpoint,
        {
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    assert body_force.status == 400
    assert body_force.json()["error"] == "invalid_source_order"

    query_force = model_hub_app.client.delete(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    assert query_force.status == 200, query_force.json()


def test_b8_refresh_force_is_body_only_in_the_current_http_contract(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B8: refresh force is body-only in the current HTTP contract."""

    # Open decision B6 has no D-1..D-4 identifier in plan section 5:
    # normalize destructive force transport or retain the current split.
    source = _create_guarded_source(model_hub_app, mock_llm_upstream)
    endpoint = f"/api/models/sources/{source['id']}/refresh"
    mock_llm_upstream.configure(models=[])
    refused = model_hub_app.client.post(endpoint, {})
    refusal = refused.json()
    assert refused.status == 409, refusal
    assert refusal["error"] == "source_model_in_route_chain"

    query_force = model_hub_app.client.post(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    assert query_force.status == 409, query_force.json()
    assert query_force.json()["error"] == "source_model_in_route_chain"

    body_force = model_hub_app.client.post(
        endpoint,
        {
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
    )
    body = body_force.json()
    assert body_force.status == 200, body
    assert body["removed_hops"] == refusal["would_remove_hops"]
    assert body["interrupted"] == refusal["would_interrupt"]


def test_b9_refetch_updates_added_and_removed_inventory(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B9: refetch adds new inventory rows and removes vanished rows."""

    _configure_protocol(
        mock_llm_upstream,
        models=[{"id": "stable-model"}, {"id": "removed-model"}],
    )
    source, _ = _create_source(model_hub_app, mock_llm_upstream)
    mock_llm_upstream.configure(
        models=[{"id": "stable-model"}, {"id": "added-model"}]
    )
    refreshed = model_hub_app.client.post(
        f"/api/models/sources/{source['id']}/refresh", {}
    )
    body = refreshed.json()
    assert refreshed.status == 200, body
    assert {model["id"] for model in body["source"]["models"]} == {
        "stable-model",
        "added-model",
    }


@pytest.mark.xfail(
    reason=(
        "B9 fix-first: refresh currently overwrites discovered_at for "
        "pre-existing models"
    )
)
def test_b9_refetch_preserves_existing_model_discovery_timestamp(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B9: inventory diff must preserve timestamps of existing rows."""

    _configure_protocol(
        mock_llm_upstream,
        models=[{"id": "stable-model"}, {"id": "removed-model"}],
    )
    source, _ = _create_source(model_hub_app, mock_llm_upstream)
    before = {
        model["id"]: model["discovered_at"] for model in source["models"]
    }
    time.sleep(0.01)
    mock_llm_upstream.configure(
        models=[{"id": "stable-model"}, {"id": "added-model"}]
    )
    refreshed = model_hub_app.client.post(
        f"/api/models/sources/{source['id']}/refresh", {}
    )
    refreshed_body = refreshed.json()
    models = {
        model["id"]: model for model in refreshed_body["source"]["models"]
    }
    assert models["stable-model"]["discovered_at"] == before[
        "stable-model"
    ]


def test_b10_custom_models_and_free_text_tiers_obey_ownership_rules(
    mock_llm_upstream: MockLLMUpstream,
    model_hub_app,
) -> None:
    """B10: managed tiers lock; user tiers edit; refresh restores catalog truth."""

    _configure_protocol(
        mock_llm_upstream,
        "openai_responses",
        models=[{"id": "gpt-5.6-sol"}, {"id": "relay-user-model"}],
    )
    source, _ = _create_source(
        model_hub_app,
        mock_llm_upstream,
        protocol="openai_responses",
    )
    endpoint = f"/api/models/sources/{source['id']}/models"

    catalog_model = next(
        model for model in source["models"] if model["id"] == "gpt-5.6-sol"
    )
    assert catalog_model["reasoning_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert catalog_model["reasoning_efforts_source"] == "catalog"
    managed = model_hub_app.client.patch(
        f"{endpoint}/gpt-5.6-sol",
        {"reasoning_efforts": ["turbo"]},
    )
    managed_body = managed.json()
    assert managed.status == 409, managed_body
    assert managed_body["error"] == "source_model_tiers_managed"
    assert managed_body["detail"] == (
        "settings.models.sourceDetail.tiers.managed.catalog"
    )
    assert managed_body["reasoning_efforts_source"] == "catalog"

    user_updated = model_hub_app.client.patch(
        f"{endpoint}/relay-user-model",
        {"reasoning_efforts": ["turbo", "careful"]},
    )
    assert user_updated.status == 200, user_updated.json()
    user_model = next(
        model
        for model in user_updated.json()["source"]["models"]
        if model["id"] == "relay-user-model"
    )
    assert user_model["reasoning_efforts"] == ["turbo", "careful"]
    assert user_model["reasoning_efforts_source"] == "user"

    added = model_hub_app.client.post(
        endpoint,
        {
            "model_id": "manual-model",
            "display_name": "Manual model",
            "reasoning_efforts": ["turbo", "careful"],
        },
    )
    added_body = added.json()
    assert added.status == 201, added_body
    manual = next(
        model
        for model in added_body["source"]["models"]
        if model["id"] == "manual-model"
    )
    assert manual["origin"] == "manual"
    assert manual["reasoning_efforts"] == ["turbo", "careful"]
    assert manual["reasoning_efforts_source"] == "user"

    updated = model_hub_app.client.patch(
        f"{endpoint}/manual-model",
        {"reasoning_efforts": ["experimental-tier"]},
    )
    assert updated.status == 200, updated.json()
    manual = next(
        model
        for model in updated.json()["source"]["models"]
        if model["id"] == "manual-model"
    )
    assert manual["reasoning_efforts"] == ["experimental-tier"]

    deleted = model_hub_app.client.delete(
        f"{endpoint}/manual-model", {}
    )
    assert deleted.status == 200, deleted.json()
    assert "manual-model" not in {
        model["id"] for model in deleted.json()["source"]["models"]
    }

    # Emulate the exact released-v6 persisted shape: a non-empty declaration
    # without provenance loads as user-owned, then the first refresh backfills.
    config_path = model_hub_app.avibe_home / "config" / "config.json"
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    persisted_source = next(
        item
        for item in persisted["model_hub"]["sources"]
        if item["id"] == source["id"]
    )
    persisted_model = next(
        model
        for model in persisted_source["models"]
        if model["id"] == "gpt-5.6-sol"
    )
    persisted_model["reasoning_efforts"] = ["legacy-user-tier"]
    persisted_model.pop("reasoning_efforts_source")
    replacement = config_path.with_suffix(".reasoning-tiers.tmp")
    replacement.write_text(json.dumps(persisted), encoding="utf-8")
    replacement.replace(config_path)
    model_hub_app.restart_controller()

    refreshed = model_hub_app.client.post(
        f"/api/models/sources/{source['id']}/refresh", {}
    )
    refreshed_body = refreshed.json()
    assert refreshed.status == 200, refreshed_body
    refreshed_catalog_model = next(
        model
        for model in refreshed_body["source"]["models"]
        if model["id"] == "gpt-5.6-sol"
    )
    assert refreshed_catalog_model["reasoning_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert refreshed_catalog_model["reasoning_efforts_source"] == "catalog"
    events = model_hub_app.client.get("/api/models/events?limit=20").json()[
        "events"
    ]
    overrides = [
        event
        for event in events
        if event["kind"] == "reasoning_efforts_override"
        and event["from_source"] == source["id"]
        and event["model_id"] == "gpt-5.6-sol"
    ]
    assert len(overrides) == 1
    assert overrides[0]["reason"] == "catalog_tiers"
    assert overrides[0]["severity"] == "info"
    assert SYNTHETIC_API_KEY not in json.dumps(overrides)
