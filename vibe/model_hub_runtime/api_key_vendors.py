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


def _validated_cpa_anthropic_origin(base_url: str | None) -> bool:
    """Share CPA's authority gate, rejecting ambiguous HTTP URL spellings."""
    if not isinstance(base_url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in base_url):
        raise ValueError
    normalized = normalize_model_hub_base_url(base_url)
    if normalized is None:
        raise ValueError
    parsed = urlsplit(normalized)
    hostname = parsed.hostname
    if not hostname or "%" in parsed.netloc or "\\" in parsed.netloc or parsed.port == 0:
        raise ValueError
    ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    if ascii_hostname == "api.anthropic.com" and hostname.lower() != ascii_hostname:
        # Python's HTTP client can normalize these to the official host while
        # Go's URL authority gate sees the original spelling. Admit neither.
        raise ValueError
    return (
        parsed.scheme == "https"
        and hostname.lower() == "api.anthropic.com"
        # CPA compares URL.Port() as text: :0443 is not its official origin.
        and (parsed.port is None or parsed.netloc.rsplit(":", 1)[-1] == "443")
    )


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
        ):
            raise ValueError
        if _validated_cpa_anthropic_origin(base_url):
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


def validate_migration_api_key_transport(
    vendor: str,
    protocol: str,
    base_url: str | None,
    secret: str | None,
    auth_scheme: str | None,
) -> None:
    """Admit native static auth only when proof and pinned CPA preserve it.

    This is a migration gate, not a new default for public Sources or legacy
    credential metadata. Anthropic SDK/API_KEY inputs mean x-api-key; CPA uses
    that header only at its official origin. Static keys matching its OAuth
    heuristic are unsafe at either origin, regardless of successful proof.
    """
    try:
        validate_api_key_auth_scheme(vendor, protocol, base_url, secret, auth_scheme)
        if protocol != "anthropic":
            return
        if not isinstance(secret, str) or not secret.strip() or "sk-ant-oat" in secret:
            raise ValueError
        if auth_scheme is None and not _validated_cpa_anthropic_origin(
            base_url if base_url is not None else official_api_key_base_url(vendor)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("unsupported native API key transport") from None


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
