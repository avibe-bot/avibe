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
    transport. Bearer is the Anthropic interface's custom-origin header, for any
    vendor not pinned to another protocol. The pinned CPA sends ordinary custom
    Claude keys as Bearer, but interprets ``sk-ant-oat`` anywhere in a key as
    OAuth independently of kind.
    """
    if auth_scheme is None:
        return None
    try:
        if (
            auth_scheme != "bearer"
            or not isinstance(vendor, str)
            or not vendor.strip()
            # Any Anthropic interface: custom relays and Anthropic-pinned vendors.
            or pinned_api_key_protocol(vendor) not in (None, "anthropic")
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
    # The models.dev provider whose entries describe what this vendor's
    # official endpoint accepts; ``None`` when no mapping has been verified.
    models_dev_provider: str | None = None


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
        models_dev_provider = item.get("models_dev_provider")
        if (
            vendor_id == "custom"
            or vendor_id in seen_ids
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(protocol, str)
            or protocol not in _SUPPORTED_PROTOCOLS
            or official_base_url is None
            or (
                models_dev_provider is not None
                and (not isinstance(models_dev_provider, str) or not models_dev_provider.strip())
            )
        ):
            raise ValueError("api-key vendor catalog is invalid")
        entries.append(
            APIKeyVendorCatalogEntry(
                id=vendor_id,
                label=label.strip(),
                official_base_url=official_base_url,
                protocol=protocol,
                models_dev_provider=models_dev_provider.strip() if models_dev_provider else None,
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


def openai_compatible_endpoint(base_url: str) -> str:
    """The API root the engine calls for an ``openai_chat`` Source's base URL.

    CLIProxyAPI appends ``/chat/completions``; a Source origin without a path
    uses the standard ``/v1`` root that discovery and probes use.
    """
    endpoint = normalize_model_hub_base_url(base_url)
    assert endpoint is not None
    if not urlsplit(endpoint).path.rstrip("/"):
        endpoint = normalize_model_hub_base_url(endpoint, append_path="/v1")
        assert endpoint is not None
    return endpoint


def _endpoint_identity(base_url: str) -> tuple[str, str, int | None, str, str] | None:
    parts = urlsplit(openai_compatible_endpoint(base_url))
    try:
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        # A stored URL may carry a port no connection can use; it names no
        # official endpoint.
        return None
    return parts.scheme, parts.hostname or "", port, parts.path, parts.query


def official_models_dev_provider(vendor: str, base_url: str | None) -> str | None:
    """The models.dev provider describing an ``openai_chat`` Source's upstream, or ``None``.

    Only a Source on its vendor's official endpoint is described by that
    vendor's models.dev entries: a custom URL may front any deployment. The
    endpoint is the one the engine calls, so spellings of one URL that differ
    only by host case, a default port, or the implied ``/v1`` root agree.
    """
    entry = api_key_vendor_entry(vendor)
    if entry is None or entry.models_dev_provider is None:
        return None
    if base_url is not None and (
        _endpoint_identity(base_url) is None
        or _endpoint_identity(base_url) != _endpoint_identity(entry.official_base_url)
    ):
        return None
    return entry.models_dev_provider


def official_api_key_base_urls() -> dict[str, str]:
    return {
        **{entry.id: entry.official_base_url for entry in api_key_vendor_catalog()},
        **_LEGACY_OFFICIAL_BASE_URLS,
    }
