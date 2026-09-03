"""Shared shipped api-key vendor catalog for Model Hub observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config.v2_config import normalize_model_hub_base_url, normalize_model_hub_vendor_id


_SUPPORTED_PROTOCOLS = {"anthropic", "openai_responses", "openai_chat"}
_LEGACY_OFFICIAL_BASE_URLS = {
    # ``codex`` remains a supported legacy vendor alias outside the shipped
    # api-key vendor preset catalog. Runtime official-URL fallback must keep
    # treating it like OpenAI for persisted Sources that omit ``base_url``.
    "codex": "https://api.openai.com/v1",
}


@dataclass(frozen=True)
class APIKeyVendorCatalogEntry:
    id: str
    label: str
    official_base_url: str
    protocol: str


def _catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "api_key_vendors.json"


@lru_cache(maxsize=1)
def api_key_vendor_catalog() -> tuple[APIKeyVendorCatalogEntry, ...]:
    payload = json.loads(_catalog_path().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("api-key vendor catalog is invalid")

    entries: list[APIKeyVendorCatalogEntry] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("api-key vendor catalog is invalid")
        vendor_id = normalize_model_hub_vendor_id(item.get("id"))
        label = item.get("label")
        protocol = item.get("protocol")
        official_base_url = normalize_model_hub_base_url(item.get("official_base_url"))
        if (
            vendor_id == "custom"
            or vendor_id in seen_ids
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(protocol, str)
            or protocol not in _SUPPORTED_PROTOCOLS
            or official_base_url is None
        ):
            raise ValueError("api-key vendor catalog is invalid")
        entries.append(
            APIKeyVendorCatalogEntry(
                id=vendor_id,
                label=label.strip(),
                official_base_url=official_base_url,
                protocol=protocol,
            )
        )
        seen_ids.add(vendor_id)
    return tuple(entries)


@lru_cache(maxsize=1)
def _catalog_by_id() -> dict[str, APIKeyVendorCatalogEntry]:
    return {entry.id: entry for entry in api_key_vendor_catalog()}


def api_key_vendor_entry(vendor: str) -> APIKeyVendorCatalogEntry | None:
    return _catalog_by_id().get(vendor.strip().lower())


def pinned_api_key_protocol(vendor: str) -> str | None:
    entry = api_key_vendor_entry(vendor)
    return entry.protocol if entry is not None else None


def official_api_key_base_url(vendor: str) -> str | None:
    normalized_vendor = vendor.strip().lower()
    entry = _catalog_by_id().get(normalized_vendor)
    if entry is not None:
        return entry.official_base_url
    return _LEGACY_OFFICIAL_BASE_URLS.get(normalized_vendor)


def official_api_key_base_urls() -> dict[str, str]:
    return {
        **{entry.id: entry.official_base_url for entry in api_key_vendor_catalog()},
        **_LEGACY_OFFICIAL_BASE_URLS,
    }
