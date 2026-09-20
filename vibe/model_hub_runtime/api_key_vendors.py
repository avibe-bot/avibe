"""Shared shipped api-key vendor catalog for Model Hub observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from config.v2_config import normalize_model_hub_base_url, normalize_model_hub_vendor_id


_SUPPORTED_PROTOCOLS = {"anthropic", "openai_responses", "openai_chat"}
_LEGACY_OFFICIAL_BASE_URLS = {
    # ``codex`` remains a supported legacy vendor alias outside the shipped
    # api-key vendor preset catalog. Runtime official-URL fallback must keep
    # treating it like OpenAI for persisted Sources that omit ``base_url``.
    "codex": "https://api.openai.com/v1",
}


def validate_api_key_auth_scheme(
    vendor: str,
    protocol: str | None,
    base_url: str | None,
    secret: str | None,
    auth_scheme: str | None,
) -> str | None:
    """Validate explicit static transport without reinterpreting legacy keys.

    A missing protocol is only for an unbound observation credential. A missing
    secret is only for the credentialless contrast of an already validated
    transport. The pinned CPA sends ordinary custom Claude keys as Bearer, but
    interprets ``sk-ant-oat`` anywhere in a key as OAuth independently of kind.
    """
    if auth_scheme is None:
        return None
    try:
        if (
            auth_scheme != "bearer"
            or not isinstance(vendor, str)
            or vendor.strip().lower() != "anthropic"
            or protocol not in (None, "anthropic")
            or not isinstance(base_url, str)
            or any(ord(char) <= 32 or ord(char) == 127 for char in base_url)
        ):
            raise ValueError
        normalized = normalize_model_hub_base_url(base_url)
        if normalized is None:
            raise ValueError
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        # Inspect the parsed authority, never a substring of the whole URL.
        # Reject encoded/ambiguous authorities before a HTTP library normalizes
        # them differently. Match CPA's HTTPS/default-port first-party gate.
        if (
            not hostname
            or "%" in parsed.netloc
            or "\\" in parsed.netloc
            or parsed.port == 0
        ):
            raise ValueError
        if (
            parsed.scheme == "https"
            and hostname.encode("idna").decode("ascii").lower() == "api.anthropic.com"
            and parsed.port in (None, 443)
        ):
            raise ValueError
        if secret is not None and (
            not isinstance(secret, str)
            or not secret.strip()
            or "sk-ant-oat" in secret
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("unsupported API key authentication scheme") from None
    return "bearer"


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


def catalog_api_key_vendor_label(vendor: str) -> str | None:
    entry = api_key_vendor_entry(vendor)
    return entry.label if entry is not None else None


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
