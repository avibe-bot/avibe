"""Bounded, cached metadata lookup for the public models.dev catalog."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import (
    normalize_storable_backend_model_text,
)
from core.handlers.model_hub.catalog_admission import admissible_backend_model


MODELS_DEV_URL_ENV = "AVIBE_MODELS_DEV_URL"
DEFAULT_MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_TIMEOUT_SECONDS = 8.0
MODELS_DEV_CACHE_TTL_SECONDS = 24 * 60 * 60
MODELS_DEV_MAX_BYTES = 16 * 1024 * 1024
MODELS_DEV_MAX_MATCHES = 8
_CACHE_LOCK = threading.Lock()


def _vendor_map_path() -> Path:
    return Path(__file__).with_name("data") / "model_vendors.json"


def load_model_vendor_map() -> dict[str, Any]:
    payload = json.loads(_vendor_map_path().read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("families"), list)
        or not isinstance(payload.get("aggregators"), list)
    ):
        raise ValueError("model vendor map is invalid")
    return payload


def _first_party_vendor(
    model_id: str,
    vendor_map: dict[str, Any],
) -> str | None:
    matches = [
        item
        for item in vendor_map["families"]
        if isinstance(item, dict)
        and isinstance(item.get("prefix"), str)
        and isinstance(item.get("vendor_id"), str)
        and model_id.lower().startswith(item["prefix"])
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item["prefix"]))["vendor_id"]


def _vendor_rank(
    vendor_id: str,
    *,
    first_party_vendor: str | None,
    aggregators: list[str],
) -> tuple[int, int, str]:
    if vendor_id == first_party_vendor:
        return (0, 0, vendor_id)
    if vendor_id in aggregators:
        return (1, aggregators.index(vendor_id), vendor_id)
    return (2, 0, vendor_id)


def _cache_path():
    return paths.get_state_dir() / "models_dev_catalog.json"


def _models_dev_url() -> str:
    return (
        os.environ.get(MODELS_DEV_URL_ENV)
        or DEFAULT_MODELS_DEV_URL
    ).strip()


def _read_cache() -> dict[str, Any]:
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(payload: dict[str, Any]) -> None:
    write_atomic(
        _cache_path(),
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode(),
    )


def _catalog_from_cache(payload: dict[str, Any]) -> dict[str, Any] | None:
    catalog = payload.get("catalog")
    return catalog if isinstance(catalog, dict) else None


def _fetch_catalog(previous: dict[str, Any]) -> dict[str, Any]:
    url = _models_dev_url()
    headers = {
        "User-Agent": "avibe/models-dev-catalog",
        "Cache-Control": "no-cache",
    }
    if previous.get("url") == url:
        if isinstance(previous.get("etag"), str):
            headers["If-None-Match"] = previous["etag"]
        if isinstance(previous.get("last_modified"), str):
            headers["If-Modified-Since"] = previous["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed public endpoint or explicit operator override
            request,
            timeout=MODELS_DEV_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(MODELS_DEV_MAX_BYTES + 1)
            if len(raw) > MODELS_DEV_MAX_BYTES:
                raise ValueError("models.dev catalog exceeded the safety limit")
            catalog = json.loads(raw.decode("utf-8"))
            if not isinstance(catalog, dict):
                raise ValueError("models.dev catalog must be an object")
            payload = {
                "fetched_at": time.time(),
                "url": url,
                "catalog": catalog,
            }
            for header, key in (
                ("ETag", "etag"),
                ("Last-Modified", "last_modified"),
            ):
                value = response.headers.get(header)
                if isinstance(value, str) and value:
                    payload[key] = value
            _write_cache(payload)
            return catalog
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            catalog = _catalog_from_cache(previous)
            if catalog is not None:
                previous["fetched_at"] = time.time()
                _write_cache(previous)
                return catalog
        raise


def load_models_dev_catalog() -> dict[str, Any]:
    with _CACHE_LOCK:
        cached = _read_cache()
        catalog = (
            _catalog_from_cache(cached)
            if cached.get("url") == _models_dev_url()
            else None
        )
        fetched_at = cached.get("fetched_at")
        cache_age = (
            time.time() - float(fetched_at)
            if isinstance(fetched_at, (int, float))
            else None
        )
        fresh = (
            cache_age is not None
            and 0 <= cache_age < MODELS_DEV_CACHE_TTL_SECONDS
        )
        if catalog is not None and fresh:
            return catalog
        try:
            return _fetch_catalog(cached)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            if catalog is not None:
                return catalog
            raise RuntimeError("models.dev catalog is unavailable") from None


def _search_tokens(query: str) -> tuple[str, ...]:
    lowered = query.strip().lower()
    tokens = [lowered]
    if "/" in lowered:
        tokens.append(lowered.rsplit("/", 1)[-1])
    for prefix in ("claude-avibe-", "anthropic-avibe-", "avibe-"):
        if lowered.startswith(prefix):
            tokens.append(lowered[len(prefix) :])
    return tuple(dict.fromkeys(token for token in tokens if token))


def _fold(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _match_score(
    tokens: tuple[str, ...],
    provider_id: str,
    model_id: str,
    name: str,
) -> int | None:
    identity = f"{provider_id}/{model_id}".lower()
    model_lower = model_id.lower()
    name_lower = name.lower()
    for token in tokens:
        if token == identity:
            return 0
        if token == model_lower:
            return 1
        if _fold(token) == _fold(model_lower):
            return 2
        if token == name_lower:
            return 3
    if any(token in identity or token in name_lower for token in tokens):
        return 4
    return None


def _reasoning_efforts(model: dict[str, Any]) -> list[str] | None:
    options = model.get("reasoning_options")
    if not isinstance(options, list):
        return []
    for option in options:
        if not isinstance(option, dict) or option.get("type") != "effort":
            continue
        values = option.get("values")
        if isinstance(values, list):
            normalized: list[str] = []
            for value in values:
                if not isinstance(value, str) or len(value) > 64:
                    continue
                effort = normalize_storable_backend_model_text(
                    value,
                    field_name="reasoning_efforts",
                )
                if effort is None:
                    return None
                if effort not in normalized:
                    normalized.append(effort)
            return normalized
    return []


def _modalities(model: dict[str, Any], direction: str) -> list[str]:
    modalities = model.get("modalities")
    values = modalities.get(direction) if isinstance(modalities, dict) else None
    if not isinstance(values, list):
        return []
    allowed = (
        {"text", "image", "audio", "video", "pdf"}
        if direction == "input"
        else {"text", "image", "audio", "video"}
    )
    return list(
        dict.fromkeys(
            value
            for value in values
            if isinstance(value, str) and value in allowed
        )
    )


def search_models_dev(query: str) -> list[dict[str, Any]]:
    tokens = _search_tokens(query)
    catalog = load_models_dev_catalog()
    vendor_map = load_model_vendor_map()
    aggregators = vendor_map["aggregators"]
    candidates: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for provider_key, provider in catalog.items():
        if not isinstance(provider_key, str) or not isinstance(provider, dict):
            continue
        provider_id = (
            provider.get("id")
            if isinstance(provider.get("id"), str)
            else provider_key
        )
        provider_name = (
            provider.get("name")
            if isinstance(provider.get("name"), str)
            else provider_id
        )
        raw_models = provider.get("models")
        if not isinstance(raw_models, dict):
            continue
        for model_key, model in raw_models.items():
            if not isinstance(model_key, str) or not isinstance(model, dict):
                continue
            model_id = (
                model.get("id")
                if isinstance(model.get("id"), str)
                else model_key
            )
            display_name = (
                model.get("name")
                if isinstance(model.get("name"), str)
                else model_id
            )
            display_name = normalize_storable_backend_model_text(
                display_name,
                field_name="display_name",
            )
            reasoning_efforts = _reasoning_efforts(model)
            if display_name is None or reasoning_efforts is None:
                continue
            score = _match_score(tokens, provider_id, model_id, display_name)
            if score is None:
                continue
            limit = model.get("limit")
            limit = limit if isinstance(limit, dict) else {}
            row = {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "model_id": model_id,
                "models_dev_id": f"{provider_id}/{model_id}",
                "display_name": display_name,
                "context_window": (
                    limit.get("context")
                    if isinstance(limit.get("context"), int)
                    and not isinstance(limit.get("context"), bool)
                    and limit["context"] > 0
                    else None
                ),
                "max_output_tokens": (
                    limit.get("output")
                    if isinstance(limit.get("output"), int)
                    and not isinstance(limit.get("output"), bool)
                    and limit["output"] > 0
                    else None
                ),
                "input_modalities": _modalities(model, "input"),
                "output_modalities": _modalities(model, "output"),
                "supports_tools": (
                    model.get("tool_call")
                    if isinstance(model.get("tool_call"), bool)
                    else None
                ),
                "supports_reasoning": (
                    model.get("reasoning")
                    if isinstance(model.get("reasoning"), bool)
                    else None
                ),
                "reasoning_efforts": reasoning_efforts,
            }
            admitted = admissible_backend_model(
                None,
                row["model_id"],
                {
                    "origin": "models_dev",
                    "models_dev_id": row["models_dev_id"],
                    "display_name": row["display_name"],
                    "context_window": row["context_window"],
                    "max_output_tokens": row["max_output_tokens"],
                    "input_modalities": row["input_modalities"],
                    "output_modalities": row["output_modalities"],
                    "supports_tools": row["supports_tools"],
                    "supports_reasoning": row["supports_reasoning"],
                    "reasoning_efforts": row["reasoning_efforts"],
                },
            )
            if admitted is None:
                continue
            row.update(
                {
                    "model_id": admitted.id,
                    "models_dev_id": admitted.models_dev_id,
                    "display_name": admitted.display_name,
                    "context_window": admitted.context_window,
                    "max_output_tokens": admitted.max_output_tokens,
                    "input_modalities": admitted.input_modalities,
                    "output_modalities": admitted.output_modalities,
                    "supports_tools": admitted.supports_tools,
                    "supports_reasoning": admitted.supports_reasoning,
                    "reasoning_efforts": admitted.reasoning_efforts,
                }
            )
            candidates.setdefault(admitted.id, []).append((score, provider_id, row))

    matches: list[tuple[bool, int, str, str, dict[str, Any]]] = []
    for model_id, copies in candidates.items():
        first_party_vendor = _first_party_vendor(model_id, vendor_map)
        _score, _vendor_id, row = min(
            copies,
            key=lambda item: _vendor_rank(
                item[1],
                first_party_vendor=first_party_vendor,
                aggregators=aggregators,
            ),
        )
        first_party = row["provider_id"] == first_party_vendor
        row["first_party"] = first_party
        matches.append(
            (
                not first_party,
                min(item[0] for item in copies),
                row["display_name"].lower(),
                model_id,
                row,
            )
        )
    matches.sort(key=lambda item: item[:-1])
    return [row for *_, row in matches[:MODELS_DEV_MAX_MATCHES]]
