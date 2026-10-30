"""Which Model Hub attempts may ask for a vendor's fast tier (core/handlers/model_hub/fast_mode.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.handlers.model_hub.fast_mode import FastModeCatalog

_VENDOR_MAP = {
    "families": [
        {"prefix": "gpt-", "vendor_id": "openai"},
        {"prefix": "claude-", "vendor_id": "anthropic"},
        {"prefix": "grok-", "vendor_id": "xai"},
    ]
}


def _fast(body: dict) -> dict:
    return {"experimental": {"modes": {"fast": {"provider": {"body": body}}}}}


_CATALOG = FastModeCatalog.from_models_dev(
    {
        "openai": {"models": {"gpt-5.6-sol": _fast({"service_tier": "priority"}), "gpt-5.4-mini": {}}},
        "anthropic": {"models": {"claude-opus-5-5": {**_fast({"speed": "fast"}), "id": "claude-opus-5-5"}}},
        "xai": {"models": {"grok-5": _fast({"service_tier": "priority"})}},
    },
    _VENDOR_MAP,
)


def _source(vendor="openai", protocol="openai_responses", kind="api_key", base_url=None, supply_channel="hub"):
    return SimpleNamespace(vendor=vendor, protocol=protocol, kind=kind, base_url=base_url, supply_channel=supply_channel)


@pytest.mark.parametrize(
    ("source", "model", "request_protocol", "expected"),
    [
        (_source(), "gpt-5.6-sol", "openai_responses", {"service_tier": "priority"}),
        (_source(protocol="openai_chat"), "gpt-5.6-sol", "openai_chat", {"service_tier": "priority"}),
        (_source(base_url="https://api.openai.com/v1"), "gpt-5.6-sol", "openai_responses", {"service_tier": "priority"}),
        (_source(kind="subscription"), "gpt-5.6-sol", "openai_responses", {"service_tier": "priority"}),
        (_source("anthropic", "anthropic", "subscription"), "claude-opus-5-5", "anthropic", {"speed": "fast"}),
        # A dated or context-tagged spelling of a listed model is the same model.
        (_source("anthropic", "anthropic"), "claude-opus-5-5[1m]", "anthropic", {"speed": "fast"}),
        # A relay or custom endpoint may front any deployment.
        (_source(base_url="https://relay.example/v1"), "gpt-5.6-sol", "openai_responses", None),
        # The native CLI's own login never reaches the gateway.
        (_source(kind="subscription", supply_channel="native_cli"), "gpt-5.6-sol", "openai_responses", None),
        # Never translated across protocols to reach a fast tier.
        (_source(), "gpt-5.6-sol", "anthropic", None),
        # A model its vendor lists without a fast tier, and one another vendor makes.
        (_source(), "gpt-5.4-mini", "openai_responses", None),
        (_source(), "claude-opus-5-5", "openai_responses", None),
        # Only the vendors whose field Avibe knows.
        (_source("xai", "openai_responses"), "grok-5", "openai_responses", None),
    ],
)
def test_fast_tier_is_offered_only_by_the_models_own_vendor_upstream(source, model, request_protocol, expected):
    assert _CATALOG.body_for(source, model, request_protocol) == expected


def test_catalog_entries_asking_for_other_fields_are_not_applied():
    catalog = FastModeCatalog.from_models_dev(
        {
            "openai": {"models": {
                "gpt-a": _fast({"service_tier": "priority", "store": True}),
                "gpt-b": _fast({"reasoning": {"mode": "pro"}}),
                "gpt-c": _fast({"service_tier": ""}),
            }},
            "anthropic": {"models": {"claude-x": _fast({"service_tier": "priority"})}},
        },
        _VENDOR_MAP,
    )
    assert catalog.bodies == {}
