"""Shared shipped api-key vendor catalog for Model Hub observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from config.v2_config import normalize_model_hub_base_url, normalize_model_hub_vendor_id


_SUPPORTED_PROTOCOLS = {"anthropic", "openai_responses", "openai_chat", "google", "search"}
_LEGACY_OFFICIAL_BASE_URLS = {
    # ``codex`` remains a supported legacy vendor alias outside the shipped
    # api-key vendor preset catalog. Runtime official-URL fallback must keep
    # treating it like OpenAI for persisted Sources that omit ``base_url``.
    "codex": "https://api.openai.com/v1",
}


def google_api_base_url(base_url: str | None) -> str:
    """CPA's Gemini executor appends /v1beta itself, including on custom roots."""
    normalized = normalize_model_hub_base_url(base_url)
    if normalized is None:
        raise ValueError("Google source requires a base URL")
    if urlsplit(normalized).query:
        raise ValueError("Google source Base URL cannot contain a query")
    return normalized.removesuffix("/v1beta")


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


def anthropic_api_key_header(base_url: str, secret: str) -> dict[str, str]:
    """The header the pinned engine sends a static Anthropic key in.

    Observation authenticates exactly as the engine will, so a verdict at add
    predicts every later call. CPA (``applyClaudeHeadersWithNativeProfile``)
    sends ``x-api-key`` only to its official origin, and Bearer to every other
    origin and for any key its ``sk-ant-oat`` heuristic takes as OAuth.
    """
    if _validated_cpa_anthropic_origin(base_url) and "sk-ant-oat" not in secret:
        return {"x-api-key": secret}
    return {"Authorization": f"Bearer {secret}"}


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
    # Built-ins are catalog rows, not addable API-key vendors.
    built_in: bool = False
    # Bundled wire metadata, never supplied by a caller or a remote catalog.
    search: dict | None = None


def _catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "api_key_vendors.json"


def _validate_search_catalog(item: object, *, protocol: str, built_in: bool = False) -> dict[str, Any] | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise ValueError("api-key vendor search catalog is invalid")
    required = {"service_id", "endpoint", "auth", "request", "result", "limits", "price", "free_check", "service_refused_statuses", "outcomes", "billing", "tool_only"}
    if protocol != "search":
        required.add("auto_enroll")
    if set(item) != required:
        raise ValueError("api-key vendor search catalog is invalid")
    endpoint = normalize_model_hub_base_url(item.get("endpoint"))
    if endpoint is None or urlsplit(endpoint).scheme != "https":
        raise ValueError("api-key vendor search catalog endpoint is invalid")
    if not isinstance(item.get("service_id"), str) or not item["service_id"].strip():
        raise ValueError("api-key vendor search catalog service id is invalid")
    if item.get("auth") not in {"bearer", "x-api-key", "none"} or (item.get("auth") == "none" and not built_in) or not isinstance(item.get("request"), dict) or not isinstance(item.get("result"), dict) or not isinstance(item.get("limits"), dict):
        raise ValueError("api-key vendor search catalog mapping is invalid")
    if "reported_cost" in item["result"]:
        reported_cost = item["result"]["reported_cost"]
        if (
            not isinstance(reported_cost, dict)
            or set(reported_cost) != {"path", "estimate"}
            or not isinstance(reported_cost.get("path"), str)
            or not reported_cost["path"]
            or not isinstance(reported_cost.get("estimate"), bool)
        ):
            raise ValueError("api-key vendor search catalog reported cost is invalid")
    results_max = item["limits"].get("results_max")
    # Keep a fixed per-request results ceiling across catalog updates.
    if isinstance(results_max, bool) or not isinstance(results_max, int) or not 1 <= results_max <= 100:
        raise ValueError("api-key vendor search catalog results maximum is invalid")
    price = item.get("price")
    if not isinstance(price, dict) or set(price) != {"unit", "usd_per_1k", "extra", "as_of", "url"} or price.get("unit") != "search":
        raise ValueError("api-key vendor search catalog price is invalid")
    if not isinstance(price.get("usd_per_1k"), (int, float)) or isinstance(price.get("usd_per_1k"), bool) or price["usd_per_1k"] < 0:
        raise ValueError("api-key vendor search catalog price is invalid")
    if price.get("extra") != [] or not isinstance(price.get("as_of"), str) or not isinstance(price.get("url"), str):
        raise ValueError("api-key vendor search catalog price is invalid")
    if not isinstance(item.get("tool_only"), bool) or item["tool_only"] != (protocol == "search"):
        raise ValueError("api-key vendor search catalog protocol is invalid")
    if protocol != "search" and not isinstance(item.get("auto_enroll"), bool):
        raise ValueError("api-key vendor search catalog auto-enroll flag is invalid")
    if not isinstance(item.get("service_refused_statuses"), list) or any(
        isinstance(status, bool) or not isinstance(status, int) or status < 400 or status >= 500
        for status in item["service_refused_statuses"]
    ) or len(set(item["service_refused_statuses"])) != len(item["service_refused_statuses"]):
        raise ValueError("api-key vendor search catalog refusal statuses are invalid")
    outcomes, billing = item.get("outcomes"), item.get("billing")
    if not isinstance(outcomes, dict) or set(outcomes) - {"credential", "balance", "limit", "tag_overrides"} or any(
        not isinstance(statuses, list) or any(type(status) is not int or not 400 <= status < 500 for status in statuses)
        or len(set(statuses)) != len(statuses) for statuses in (outcomes.get(key) for key in ("credential", "balance", "limit"))
    ):
        raise ValueError("api-key vendor search outcome statuses are invalid")
    overrides = outcomes.get("tag_overrides", [])
    if not isinstance(overrides, list) or any(
        not isinstance(rule, dict) or set(rule) != {"status", "tag", "code"}
        or type(rule["status"]) is not int or not 400 <= rule["status"] < 500
        or not isinstance(rule["tag"], str) or not rule["tag"]
        or rule["code"] not in {"service_limit", "service_refused"} for rule in overrides
    ):
        raise ValueError("api-key vendor search outcome tag overrides are invalid")
    if (
        not isinstance(billing, dict) or set(billing) != {"nonempty_only", "unbilled_errors"}
        or not isinstance(billing["nonempty_only"], bool)
        or not isinstance(billing["unbilled_errors"], list)
        or any(error != "timeout" for error in billing["unbilled_errors"])
        or len(set(billing["unbilled_errors"])) != len(billing["unbilled_errors"])
    ):
        raise ValueError("api-key vendor search billing rule is invalid")
    free_check = item.get("free_check")
    if free_check is not None:
        if not isinstance(free_check, dict):
            raise ValueError("api-key vendor search catalog free check is invalid")
        limit_rule = free_check.get("limit")
        if limit_rule is not None:
            pairs = limit_rule.get("pairs") if isinstance(limit_rule, dict) else None
            if not isinstance(pairs, list) or any(
                not isinstance(pair, dict)
                or not isinstance(pair.get("usage"), str)
                or not isinstance(pair.get("limit"), str)
                for pair in pairs
            ):
                raise ValueError("api-key vendor search catalog free check is invalid")
    return dict(item)

@lru_cache(maxsize=1)
def api_key_vendor_catalog() -> tuple[APIKeyVendorCatalogEntry, ...]:
    payload = json.loads(_catalog_path().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("api-key vendor catalog is invalid")

    entries: list[APIKeyVendorCatalogEntry] = []
    seen_ids: set[str] = set()
    seen_service_ids: set[str] = set()
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
        built_in = item.get("built_in", False)
        if not isinstance(built_in, bool) or (built_in and protocol != "search"):
            raise ValueError("api-key vendor catalog is invalid")
        search = _validate_search_catalog(item.get("search"), protocol=protocol, built_in=built_in)
        if search is not None and search["service_id"] in seen_service_ids:
            raise ValueError("api-key vendor catalog is invalid")
        entries.append(
            APIKeyVendorCatalogEntry(
                id=vendor_id,
                label=label.strip(),
                official_base_url=official_base_url,
                protocol=protocol,
                models_dev_provider=models_dev_provider.strip() if models_dev_provider else None,
                search=search,
                built_in=built_in,
            )
        )
        seen_ids.add(vendor_id)
        if search is not None:
            seen_service_ids.add(search["service_id"])
    return tuple(entries)


@lru_cache(maxsize=1)
def _catalog_by_id() -> dict[str, APIKeyVendorCatalogEntry]:
    return {entry.id: entry for entry in api_key_vendor_catalog()}


def api_key_vendor_entry(vendor: str) -> APIKeyVendorCatalogEntry | None:
    return _catalog_by_id().get(vendor.strip().lower())


def search_service(vendor: str, protocol: str, base_url: str | None = None) -> dict | None:
    """The catalog search API this Source can use, never a relay or an OAuth grant.

    Callers also require kind=api_key. This pure predicate deliberately has no
    health or enabled checks: those belong to admission, not catalog identity.
    """
    entry = api_key_vendor_entry(vendor)
    if entry is None or entry.search is None:
        return None
    if entry.search["tool_only"]:
        return entry.search if protocol == "search" and base_url is None else None
    if protocol == "search" or (base_url is not None and not is_official_api_key_endpoint(vendor, base_url)):
        return None
    return entry.search


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


def is_official_api_key_endpoint(vendor: str, base_url: str) -> bool:
    """Whether a base URL is its vendor's official endpoint, by the engine's endpoint identity.

    Spellings that differ only by host case, a default port, or the implied ``/v1`` root are the same endpoint.
    """

    official = official_api_key_base_url(vendor)
    if official is None:
        return False
    identity = _endpoint_identity(base_url)
    return identity is not None and identity == _endpoint_identity(official)


def official_api_key_base_urls() -> dict[str, str]:
    return {
        **{entry.id: entry.official_base_url for entry in api_key_vendor_catalog()},
        **_LEGACY_OFFICIAL_BASE_URLS,
    }
