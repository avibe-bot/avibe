"""Model Hub guard round-trip and route-contract E2E scenarios."""

from __future__ import annotations

import pytest

from tests.e2e.test_model_hub_sources import (
    MENU_MODEL,
    _configure_protocol,
    _create_source,
)


pytestmark = pytest.mark.e2e_model_hub


def _two_source_chain(app, upstream):
    _configure_protocol(
        upstream,
        "anthropic",
        models=[{"id": MENU_MODEL}],
    )
    first, _ = _create_source(
        app,
        upstream,
        nonce="scn_g000000000000001",
        vendor="anthropic",
        display_name="First guard source",
    )
    second, _ = _create_source(
        app,
        upstream,
        nonce="scn_g000000000000002",
        vendor="anthropic",
        display_name="Second guard source",
    )
    mode = app.client.patch(
        "/api/models/agents/claude/mode", {"mode": "hub"}
    )
    assert mode.status == 200, mode.json()
    hops = [
        {"source_id": first["id"], "model_id": MENU_MODEL},
        {"source_id": second["id"], "model_id": MENU_MODEL},
    ]
    chain = app.client.put(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}",
        {"hops": hops},
    )
    assert chain.status == 200, chain.json()
    return first, second, hops


@pytest.mark.xfail(
    reason=(
        "G1/B7 remains classified fix-first in the plan, although the current "
        "API accepts a guard echo with would_interrupt[].agents omitted"
    )
)
def test_g1_guard_echo_missing_agents_does_not_loop(
    mock_llm_upstream,
    model_hub_app,
) -> None:
    """G1: a legacy guard echo without agents remains confirmable."""

    first, _, _ = _two_source_chain(model_hub_app, mock_llm_upstream)
    endpoint = f"/api/models/sources/{first['id']}"
    refused = model_hub_app.client.delete(endpoint, {})
    refusal = refused.json()
    assert refused.status == 409, refusal
    echo_without_agents = [
        {
            key: value
            for key, value in interruption.items()
            if key != "agents"
        }
        for interruption in refusal["would_interrupt"]
    ]
    committed = model_hub_app.client.delete(
        f"{endpoint}?force=true",
        {
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": echo_without_agents,
        },
    )
    assert committed.status == 200, committed.json()


def test_g2_source_order_write_and_chain_reorder_are_divergent(
    mock_llm_upstream,
    model_hub_app,
) -> None:
    """G2: the orphan order endpoint persists order without applying it."""

    # Open decisions D-1..D-4 do not assign the B3 endpoint-divergence
    # decision; this baseline records the current split without inventing one.
    first, second, original_hops = _two_source_chain(
        model_hub_app, mock_llm_upstream
    )
    reversed_order = [second["id"], first["id"]]
    order_write = model_hub_app.client.put(
        "/api/models/agents/claude/sources",
        {"order": reversed_order},
    )
    assert order_write.status == 200, order_write.json()
    assert order_write.json()["agent"]["sources"]["order"] == reversed_order

    chain_before = model_hub_app.client.get(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}"
    )
    assert chain_before.status == 200, chain_before.json()
    assert [
        {
            "source_id": hop["source_id"],
            "model_id": hop["model_id"],
        }
        for hop in chain_before.json()["chain"]["chain"]
    ] == original_hops

    applied = model_hub_app.client.post(
        "/api/models/agents/claude/chains/reorder"
    )
    assert applied.status == 200, applied.json()
    chain_after = model_hub_app.client.get(
        f"/api/models/agents/claude/chain?model={MENU_MODEL}"
    )
    assert chain_after.status == 200, chain_after.json()
    assert [
        hop["source_id"] for hop in chain_after.json()["chain"]["chain"]
    ] == reversed_order


@pytest.mark.xfail(
    reason=(
        "G3/B13 fix-first: malformed chain JSON is currently folded into the "
        "same invalid_source_order code as a parsed business refusal"
    )
)
def test_g3_malformed_json_is_distinct_from_business_refusals(
    model_hub_app,
) -> None:
    """G3: syntax failures have a code distinct from parsed domain errors."""

    headers = {"Content-Type": "application/json"}
    malformed_mode = model_hub_app.client.request(
        "PATCH",
        "/api/models/agents/claude/mode",
        raw_body=b'{"mode":',
        headers=headers,
    )
    malformed_chain = model_hub_app.client.request(
        "POST",
        "/api/models/agents/claude/chains/reorder",
        raw_body=b'{"order":',
        headers=headers,
    )
    business_refusal = model_hub_app.client.post(
        "/api/models/agents/claude/chains/reorder",
        {"order": None},
    )
    assert malformed_mode.status == malformed_chain.status == 400
    assert business_refusal.status == 400
    assert malformed_mode.json()["error"] == "malformed_json"
    assert malformed_chain.json()["error"] == "malformed_json"
    assert business_refusal.json()["error"] == "invalid_source_order"
