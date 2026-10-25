"""Bundled web-fetch capability/identity catalog; no transport or activation.

Synthetic identities are catalog facts even when a build cannot execute fetch.
Availability affects candidates, never preservation of saved choices/history.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def fetch_service_catalog() -> tuple[dict[str, Any], ...]:
    path = Path(__file__).resolve().parents[1] / "data" / "web_fetch_services.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def builtin_tool_identity(source_id: str) -> dict[str, Any] | None:
    return next((entry for entry in fetch_service_catalog() if entry.get("source_id") == source_id), None)


def builtin_tool_identities() -> tuple[dict[str, Any], ...]:
    return tuple(entry for entry in fetch_service_catalog() if entry.get("source_id"))


def fetch_service(source: Any) -> dict[str, Any] | None:
    """Capability of this exact Source; a relay/subscription never inherits an API."""
    from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_entry, is_official_api_key_endpoint

    entry = next((row for row in fetch_service_catalog() if row["vendor"] == source.vendor), None)
    if entry is None or source.supply_channel != "hub" or source.kind != "api_key":
        return None
    if entry.get("source_id"):
        return entry if (
            source.id == entry["source_id"] and source.protocol == "search"
            and source.credential_ref is None and source.base_url is None
        ) else None
    if not source.credential_ref:
        return None
    vendor = api_key_vendor_entry(source.vendor)
    if vendor is None:
        return None
    if vendor.protocol == "search":
        return entry if source.protocol == "search" and source.base_url is None else None
    return entry if source.protocol != "search" and (
        source.base_url is None or is_official_api_key_endpoint(source.vendor, source.base_url)
    ) else None
