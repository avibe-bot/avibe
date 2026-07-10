from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from config import paths


REMOTE_CATALOG_URL_ENV = "AVIBE_BACKEND_MODEL_CATALOG_URL"
DEFAULT_REMOTE_CATALOG_URL = (
    "https://raw.githubusercontent.com/avibe-bot/avibe/master/vibe/data/backend_models.json"
)
REMOTE_CATALOG_TTL_SECONDS = 6 * 60 * 60
REMOTE_CATALOG_FAILURE_TTL_SECONDS = 10 * 60
REMOTE_CATALOG_TIMEOUT_SECONDS = 3.0
REMOTE_CATALOG_USER_AGENT = "avibe/backend-model-catalog"

_REMOTE_LOCK = threading.Lock()
_REMOTE_REFRESH_IN_FLIGHT = False
_REMOTE_MEMORY_CACHE: dict[str, Any] = {}


def get_bundled_catalog_path(repo_root: Path | None = None) -> Path:
    base_dir = repo_root if repo_root is not None else Path(__file__).resolve().parent
    return base_dir / "data" / "backend_models.json"


def get_cached_catalog_path() -> Path:
    return paths.get_state_dir() / "backend_model_catalog.json"


def load_bundled_catalog(path: Path | None = None) -> dict[str, Any]:
    return _read_catalog(path or get_bundled_catalog_path()) or {}


def load_cached_remote_catalog(*, schedule_refresh: bool = True) -> dict[str, Any]:
    cached = _cached_remote_payload()
    if schedule_refresh and _remote_cache_stale(cached):
        schedule_remote_catalog_refresh()
    catalog = cached.get("catalog")
    return catalog if isinstance(catalog, dict) else {}


def remote_catalog_token() -> tuple[float | None, float | None]:
    payload = _cached_remote_payload()
    fetched_at = payload.get("fetched_at")
    failed_at = payload.get("failed_at")
    return (
        float(fetched_at) if isinstance(fetched_at, (int, float)) else None,
        float(failed_at) if isinstance(failed_at, (int, float)) else None,
    )


def remote_catalog_refresh_pending(since: tuple[float | None, float | None]) -> bool:
    with _REMOTE_LOCK:
        refresh_in_flight = _REMOTE_REFRESH_IN_FLIGHT
    return refresh_in_flight or remote_catalog_token() != since


def schedule_remote_catalog_refresh() -> bool:
    global _REMOTE_REFRESH_IN_FLIGHT

    with _REMOTE_LOCK:
        if _REMOTE_REFRESH_IN_FLIGHT:
            return False
        _REMOTE_REFRESH_IN_FLIGHT = True

    thread = threading.Thread(target=_refresh_remote_catalog_worker, name="avibe-model-catalog-refresh", daemon=True)
    thread.start()
    return True


def refresh_remote_catalog_now(url: str | None = None) -> dict[str, Any]:
    catalog = fetch_remote_catalog(url=url)
    payload = {"fetched_at": time.time(), "catalog": catalog, "error": None}
    _write_cached_remote_payload(payload)
    return catalog


def fetch_remote_catalog(url: str | None = None) -> dict[str, Any]:
    request_url = (url or os.environ.get(REMOTE_CATALOG_URL_ENV) or DEFAULT_REMOTE_CATALOG_URL).strip()
    req = urllib.request.Request(request_url, headers={"User-Agent": REMOTE_CATALOG_USER_AGENT})
    with urllib.request.urlopen(req, timeout=REMOTE_CATALOG_TIMEOUT_SECONDS) as response:  # noqa: S310 - public catalog
        return _normalize_catalog(json.loads(response.read().decode("utf-8")), strict=True)


def backend_model_entries(backend: str, catalog: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(catalog, dict):
        return []
    backend_key = (backend or "").strip().lower()
    backends = catalog.get("backends")
    raw_backend = backends.get(backend_key) if isinstance(backends, dict) else catalog.get(backend_key)
    if not isinstance(raw_backend, dict):
        return []
    raw_models = raw_backend.get("models")
    if not isinstance(raw_models, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw_models:
        entry = _normalize_model_entry(item)
        if entry:
            entries.append(entry)
    return entries


def model_id(entry: dict[str, Any]) -> str | None:
    value = entry.get("id")
    return value if isinstance(value, str) and value else None


def model_label(entry: dict[str, Any]) -> str | None:
    value = entry.get("label")
    return value if isinstance(value, str) and value else None


def reasoning_efforts(entry: dict[str, Any]) -> list[str]:
    values = entry.get("reasoning_efforts")
    return _dedupe_str_values(values) if isinstance(values, list) else []


def catalog_reasoning_efforts_for_model(backend: str, model: str | None) -> list[str] | None:
    if not model:
        return None
    for catalog in (
        load_cached_remote_catalog(schedule_refresh=False),
        load_bundled_catalog(),
    ):
        for entry in backend_model_entries(backend, catalog):
            if model_id(entry) != model:
                continue
            efforts = reasoning_efforts(entry)
            if efforts:
                return efforts
    return None


def _cached_remote_payload() -> dict[str, Any]:
    with _REMOTE_LOCK:
        if _REMOTE_MEMORY_CACHE:
            return dict(_REMOTE_MEMORY_CACHE)

    payload = _read_cached_remote_payload(get_cached_catalog_path())
    if isinstance(payload, dict):
        with _REMOTE_LOCK:
            _REMOTE_MEMORY_CACHE.clear()
            _REMOTE_MEMORY_CACHE.update(payload)
        return payload
    return {}


def _remote_cache_stale(payload: dict[str, Any]) -> bool:
    fetched_at = payload.get("fetched_at")
    failed_at = payload.get("failed_at")
    if isinstance(failed_at, (int, float)) and (
        not isinstance(fetched_at, (int, float)) or float(failed_at) >= float(fetched_at)
    ):
        return time.time() - float(failed_at) >= REMOTE_CATALOG_FAILURE_TTL_SECONDS
    if not isinstance(fetched_at, (int, float)):
        return True
    return time.time() - float(fetched_at) >= REMOTE_CATALOG_TTL_SECONDS


def _refresh_remote_catalog_worker() -> None:
    global _REMOTE_REFRESH_IN_FLIGHT
    try:
        refresh_remote_catalog_now()
    except Exception as exc:
        payload = {"failed_at": time.time(), "catalog": _cached_remote_payload().get("catalog"), "error": str(exc)}
        _write_cached_remote_payload(payload)
    finally:
        with _REMOTE_LOCK:
            _REMOTE_REFRESH_IN_FLIGHT = False


def _read_catalog(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return _normalize_catalog(payload)


def _read_cached_remote_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {}
    catalog_valid = False
    raw_catalog = payload.get("catalog")
    if isinstance(raw_catalog, dict):
        try:
            normalized["catalog"] = _normalize_catalog(raw_catalog, strict=True)
            catalog_valid = True
        except ValueError:
            pass
    for key in ("fetched_at", "failed_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and (key != "fetched_at" or catalog_valid):
            normalized[key] = value
    error = payload.get("error")
    if isinstance(error, str) or error is None:
        normalized["error"] = error
    return normalized


def _write_cached_remote_payload(payload: dict[str, Any]) -> None:
    cache_path = get_cached_catalog_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{cache_path.name}.", suffix=".tmp", dir=str(cache_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(tmp_name).replace(cache_path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    with _REMOTE_LOCK:
        _REMOTE_MEMORY_CACHE.clear()
        _REMOTE_MEMORY_CACHE.update(payload)


def _normalize_catalog(payload: object, *, strict: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        if strict:
            raise ValueError("Backend model catalog must be an object")
        return {}
    schema_version = payload.get("schema_version")
    if strict and schema_version != 1:
        raise ValueError(f"Unsupported backend model catalog schema version: {schema_version!r}")
    backends = payload.get("backends")
    if not isinstance(backends, dict):
        if strict:
            raise ValueError("Backend model catalog must contain a backends object")
        return {}
    normalized_backends: dict[str, Any] = {}
    for backend, raw_backend in backends.items():
        if not isinstance(backend, str) or not isinstance(raw_backend, dict):
            if strict:
                raise ValueError("Backend model catalog contains an invalid backend entry")
            continue
        backend_key = backend.strip().lower()
        if not backend_key:
            if strict:
                raise ValueError("Backend model catalog contains an empty backend name")
            continue
        models = raw_backend.get("models")
        if not isinstance(models, list):
            if strict:
                raise ValueError(f"Backend model catalog models must be a list: {backend_key}")
            continue
        entries = [_normalize_model_entry(item) for item in models]
        if strict and any(not entry for entry in entries):
            raise ValueError(f"Backend model catalog contains an invalid model entry: {backend_key}")
        normalized_backends[backend_key] = {"models": [entry for entry in entries if entry]}
    if strict and not normalized_backends:
        raise ValueError("Backend model catalog must contain at least one backend")
    return {"schema_version": schema_version or 1, "backends": normalized_backends}


def _normalize_model_entry(item: object) -> dict[str, Any]:
    if isinstance(item, str):
        model = item.strip()
        return {"id": model} if model else {}
    if not isinstance(item, dict):
        return {}
    raw_id = item.get("id") or item.get("slug") or item.get("model") or item.get("value")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return {}
    entry: dict[str, Any] = {"id": raw_id.strip()}
    raw_label = item.get("label") or item.get("display_name") or item.get("name")
    if isinstance(raw_label, str) and raw_label.strip():
        entry["label"] = raw_label.strip()
    raw_priority = item.get("priority")
    if isinstance(raw_priority, int):
        entry["priority"] = raw_priority
    raw_visibility = item.get("visibility")
    if isinstance(raw_visibility, str) and raw_visibility.strip():
        entry["visibility"] = raw_visibility.strip()
    efforts = _coerce_reasoning_efforts(item.get("reasoning_efforts") or item.get("supported_reasoning_levels"))
    if efforts:
        entry["reasoning_efforts"] = efforts
    return entry


def _coerce_reasoning_efforts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    efforts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            efforts.append(value.get("effort"))
        else:
            efforts.append(value)
    return _dedupe_str_values(efforts)


def _dedupe_str_values(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized
