from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import paths
from vibe.claude_model_catalog import DEFAULT_CLAUDE_MODEL_ALIASES, load_catalog_models
from vibe.codex_config import get_codex_home


REMOTE_CATALOG_URL_ENV = "AVIBE_BACKEND_MODEL_CATALOG_URL"
DEFAULT_REMOTE_CATALOG_URL = (
    "https://raw.githubusercontent.com/avibe-bot/avibe/master/vibe/data/backend_models.json"
)
REMOTE_CATALOG_TTL_SECONDS = 6 * 60 * 60
REMOTE_CATALOG_FAILURE_TTL_SECONDS = 10 * 60
REMOTE_CATALOG_TIMEOUT_SECONDS = 3.0
REMOTE_CATALOG_USER_AGENT = "avibe/backend-model-catalog"

_HIDDEN_VISIBILITIES = {"hide", "hidden"}
_SUPPORTED_BACKENDS = {"claude", "codex"}
_SUPPORTED_VISIBILITIES = {"visible", "list", *_HIDDEN_VISIBILITIES}
_DEFAULT_REASONING_EFFORTS = {
    "claude": ["low", "medium", "high"],
    "codex": ["minimal", "low", "medium", "high", "xhigh"],
}
_CODEX_BUILT_IN_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2-codex",
    "gpt-5.2",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.1",
    "gpt-5",
]
_REASONING_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultra": "Ultra",
}

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

    thread = threading.Thread(
        target=_refresh_remote_catalog_worker,
        name="avibe-model-catalog-refresh",
        daemon=True,
    )
    thread.start()
    return True


def refresh_remote_catalog_now(url: str | None = None) -> dict[str, Any]:
    catalog = fetch_remote_catalog(url=url)
    payload = {"fetched_at": time.time(), "catalog": catalog, "error": None}
    _write_cached_remote_payload(payload)
    return catalog


def fetch_remote_catalog(url: str | None = None) -> dict[str, Any]:
    request_url = (url or os.environ.get(REMOTE_CATALOG_URL_ENV) or DEFAULT_REMOTE_CATALOG_URL).strip()
    request = urllib.request.Request(request_url, headers={"User-Agent": REMOTE_CATALOG_USER_AGENT})
    with urllib.request.urlopen(  # noqa: S310 - fixed public catalog or explicit operator override
        request,
        timeout=REMOTE_CATALOG_TIMEOUT_SECONDS,
    ) as response:
        return _normalize_catalog(json.loads(response.read().decode("utf-8")), strict=True)


def backend_model_entries(backend: str, catalog: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(catalog, dict):
        return []
    backend_key = (backend or "").strip().lower()
    backends = catalog.get("backends")
    raw_backend = backends.get(backend_key) if isinstance(backends, dict) else None
    if not isinstance(raw_backend, dict):
        return []
    raw_models = raw_backend.get("models")
    if not isinstance(raw_models, list):
        return []
    entries = [_normalize_model_entry(item) for item in raw_models]
    return [entry for entry in entries if entry]


def backend_model_snapshot(backend: str, *, schedule_refresh: bool = True) -> dict[str, Any]:
    backend_key = (backend or "").strip().lower()
    if backend_key not in _SUPPORTED_BACKENDS:
        return {"ok": False, "backend": backend_key, "error": f"unsupported backend '{backend}'"}

    refresh_token = remote_catalog_token()
    remote_catalog = load_cached_remote_catalog(schedule_refresh=schedule_refresh)
    bundled_catalog = load_bundled_catalog()

    if backend_key == "claude":
        sources = _claude_sources(remote_catalog, bundled_catalog)
        blocked: set[str] = set()
    else:
        local_catalog = _read_codex_models_cache()
        remote_entries = backend_model_entries("codex", remote_catalog)
        blocked = {
            entry["id"]
            for entry in [*local_catalog, *remote_entries]
            if _model_hidden(entry)
        }
        sources = _codex_sources(remote_entries, local_catalog, bundled_catalog)

    merged = merge_model_sources(sources, blocked_model_ids=blocked)
    default_efforts = _DEFAULT_REASONING_EFFORTS[backend_key]
    models = [entry["id"] for entry in merged]
    model_labels = {
        entry["id"]: entry["label"]
        for entry in merged
        if isinstance(entry.get("label"), str) and entry["label"] != entry["id"]
    }
    reasoning_options = {"": _reasoning_option_items(default_efforts)}
    for entry in merged:
        efforts = entry.get("reasoning_efforts") or default_efforts
        reasoning_options[entry["id"]] = _reasoning_option_items(efforts)

    cached_payload = _cached_remote_payload()
    notes = []
    error = cached_payload.get("error")
    if isinstance(error, str) and error:
        notes.append(f"remote catalog refresh failed: {error}")

    return {
        "ok": True,
        "backend": backend_key,
        "models": models,
        "model_labels": model_labels,
        "reasoning_options": reasoning_options,
        "sources": [name for name, entries in sources if entries],
        "source": " + ".join(name for name, entries in sources if entries),
        "live": False,
        "notes": notes or None,
        "catalog_refresh_pending": remote_catalog_refresh_pending(refresh_token),
    }


def catalog_reasoning_efforts_for_model(backend: str, model: str | None) -> list[str] | None:
    if not model:
        return None
    snapshot = backend_model_snapshot(backend, schedule_refresh=False)
    if not snapshot.get("ok"):
        return None
    entries = snapshot.get("reasoning_options", {}).get(model)
    if not isinstance(entries, list):
        return None
    efforts = []
    for entry in entries:
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(value, str) and value and value != "__default__":
            efforts.append(value)
    return efforts or None


def merge_model_sources(
    sources: Sequence[tuple[str, Sequence[dict[str, Any]]]],
    *,
    blocked_model_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    blocked = {model.strip() for model in blocked_model_ids if isinstance(model, str) and model.strip()}
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for source_name, raw_entries in sources:
        for entry in _ordered_entries(raw_entries):
            model = entry["id"]
            if model in blocked:
                continue
            if _model_hidden(entry):
                if model not in merged:
                    blocked.add(model)
                continue

            current = merged.get(model)
            if current is None:
                current = {
                    "id": model,
                    "label": entry.get("label") or model,
                    "reasoning_efforts": list(entry.get("reasoning_efforts") or []),
                    "source": source_name,
                }
                merged[model] = current
                order.append(model)
                continue

            if current.get("label") == model and entry.get("label"):
                current["label"] = entry["label"]
            if not current.get("reasoning_efforts") and entry.get("reasoning_efforts"):
                current["reasoning_efforts"] = list(entry["reasoning_efforts"])

    return [merged[model] for model in order]


def _claude_sources(
    remote_catalog: dict[str, Any],
    bundled_catalog: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    from modules.agents.opencode.utils import format_claude_model_label

    legacy_entries = []
    for model in [*load_catalog_models(), *DEFAULT_CLAUDE_MODEL_ALIASES]:
        entry = {
            "id": model,
            "reasoning_efforts": _legacy_claude_reasoning_efforts(model),
        }
        label = format_claude_model_label(model)
        if label != model:
            entry["label"] = label
        legacy_entries.append(entry)
    return [
        ("remote", backend_model_entries("claude", remote_catalog)),
        ("bundled", backend_model_entries("claude", bundled_catalog)),
        ("legacy", legacy_entries),
        ("config", _read_claude_settings_models()),
    ]


def _codex_sources(
    remote_entries: list[dict[str, Any]],
    local_catalog: list[dict[str, Any]],
    bundled_catalog: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    visible_local = [entry for entry in local_catalog if not _model_hidden(entry)]
    local_by_id = {entry["id"]: entry for entry in visible_local}
    bundled_entries = backend_model_entries("codex", bundled_catalog)
    built_in = [
        {"id": model, "reasoning_efforts": _DEFAULT_REASONING_EFFORTS["codex"]}
        for model in _CODEX_BUILT_IN_MODELS
    ]
    return [
        (
            "remote",
            _overlay_local_reasoning_efforts(
                [entry for entry in remote_entries if not _model_hidden(entry)],
                local_by_id,
            ),
        ),
        ("bundled", _overlay_local_reasoning_efforts(bundled_entries, local_by_id)),
        ("local", visible_local),
        ("legacy", built_in),
        ("config", _read_codex_config_models()),
    ]


def _overlay_local_reasoning_efforts(
    catalog_entries: list[dict[str, Any]],
    local_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    overlaid = []
    for entry in catalog_entries:
        merged = dict(entry)
        local_efforts = local_by_id.get(entry["id"], {}).get("reasoning_efforts")
        if local_efforts:
            merged["reasoning_efforts"] = list(local_efforts)
        overlaid.append(merged)
    return overlaid


def _read_claude_settings_models() -> list[dict[str, Any]]:
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    values = [payload.get("model")]
    env = payload.get("env")
    if isinstance(env, dict):
        values.extend((env.get("ANTHROPIC_MODEL"), env.get("ANTHROPIC_SMALL_FAST_MODEL")))
    return [{"id": value.strip()} for value in values if isinstance(value, str) and value.strip()]


def _read_codex_models_cache() -> list[dict[str, Any]]:
    cache_path = get_codex_home() / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return []
    entries = [_normalize_model_entry(item) for item in raw_models]
    return [entry for entry in entries if entry]


def _read_codex_config_models() -> list[dict[str, Any]]:
    config_path = get_codex_home() / "config.toml"
    try:
        payload = _parse_toml(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    values = [payload.get("model")]
    notice = payload.get("notice")
    migrations = notice.get("model_migrations") if isinstance(notice, dict) else None
    if isinstance(migrations, dict):
        for source, target in migrations.items():
            values.extend((source, target))
    return [
        {"id": value.strip(), "reasoning_efforts": _DEFAULT_REASONING_EFFORTS["codex"]}
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _parse_toml(raw: str) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        import tomli as tomllib

    payload = tomllib.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _legacy_claude_reasoning_efforts(model: str) -> list[str]:
    from modules.agents.opencode.utils import build_claude_reasoning_options

    return [
        entry["value"]
        for entry in build_claude_reasoning_options(model)
        if entry.get("value") != "__default__"
    ]


def _reasoning_option_items(efforts: Iterable[object]) -> list[dict[str, str]]:
    options = [{"value": "__default__", "label": "(Default)"}]
    for effort in _dedupe_str_values(efforts):
        options.append({"value": effort, "label": _REASONING_LABELS.get(effort, effort.capitalize())})
    return options


def _model_hidden(entry: dict[str, Any]) -> bool:
    visibility = entry.get("visibility")
    return isinstance(visibility, str) and visibility.strip().lower() in _HIDDEN_VISIBILITIES


def _ordered_entries(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = []
    for index, entry in enumerate(entries):
        normalized = _normalize_model_entry(entry)
        if not normalized:
            continue
        priority = normalized.get("priority")
        indexed.append((priority if isinstance(priority, int) else 10**9, index, normalized))
    return [entry for _, _, entry in sorted(indexed, key=lambda value: (value[0], value[1]))]


def _cached_remote_payload() -> dict[str, Any]:
    with _REMOTE_LOCK:
        if _REMOTE_MEMORY_CACHE:
            return dict(_REMOTE_MEMORY_CACHE)

    payload = _read_cached_remote_payload(get_cached_catalog_path())
    with _REMOTE_LOCK:
        _REMOTE_MEMORY_CACHE.clear()
        _REMOTE_MEMORY_CACHE.update(payload)
    return payload


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
        previous = _cached_remote_payload()
        payload = {
            "failed_at": time.time(),
            "catalog": previous.get("catalog"),
            "error": str(exc),
        }
        if isinstance(previous.get("fetched_at"), (int, float)):
            payload["fetched_at"] = previous["fetched_at"]
        _write_cached_remote_payload(payload)
    finally:
        with _REMOTE_LOCK:
            _REMOTE_REFRESH_IN_FLIGHT = False


def _read_catalog(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
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
        Path(tmp_name).unlink(missing_ok=True)
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
        if strict and backend_key not in _SUPPORTED_BACKENDS:
            raise ValueError(f"Backend model catalog contains an unsupported backend: {backend_key}")
        models = raw_backend.get("models")
        if not backend_key or not isinstance(models, list):
            if strict:
                raise ValueError(f"Backend model catalog models must be a list: {backend_key}")
            continue
        if strict:
            for item in models:
                _validate_catalog_model_entry(item, backend_key)
        entries = [_normalize_model_entry(item) for item in models]
        if strict and any(not entry for entry in entries):
            raise ValueError(f"Backend model catalog contains an invalid model entry: {backend_key}")
        normalized_backends[backend_key] = {"models": [entry for entry in entries if entry]}
    if strict and not normalized_backends:
        raise ValueError("Backend model catalog must contain at least one backend")
    return {"schema_version": schema_version or 1, "backends": normalized_backends}


def _validate_catalog_model_entry(item: object, backend: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"Backend model catalog contains an invalid model entry: {backend}")
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(f"Backend model catalog contains an invalid model entry: {backend}")

    for key in ("label", "visibility"):
        value = item.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"Backend model catalog contains an invalid {key}: {backend}/{raw_id}")
    visibility = item.get("visibility")
    if isinstance(visibility, str) and visibility.strip().lower() not in _SUPPORTED_VISIBILITIES:
        raise ValueError(f"Backend model catalog contains an invalid visibility: {backend}/{raw_id}")

    priority = item.get("priority")
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
        raise ValueError(f"Backend model catalog contains an invalid priority: {backend}/{raw_id}")

    efforts = item.get("reasoning_efforts")
    if efforts is not None:
        if not isinstance(efforts, list) or any(not isinstance(value, str) or not value.strip() for value in efforts):
            raise ValueError(f"Backend model catalog contains invalid reasoning efforts: {backend}/{raw_id}")


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
    label = item.get("label") or item.get("display_name") or item.get("name")
    if isinstance(label, str) and label.strip():
        entry["label"] = label.strip()
    if isinstance(item.get("priority"), int):
        entry["priority"] = item["priority"]
    visibility = item.get("visibility")
    if isinstance(visibility, str) and visibility.strip():
        entry["visibility"] = visibility.strip().lower()
    efforts = _coerce_reasoning_efforts(
        item.get("reasoning_efforts") or item.get("supported_reasoning_levels")
    )
    if efforts:
        entry["reasoning_efforts"] = efforts
    return entry


def _coerce_reasoning_efforts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    efforts = []
    for value in values:
        efforts.append(value.get("effort") if isinstance(value, dict) else value)
    return _dedupe_str_values(efforts)


def _dedupe_str_values(values: Iterable[object]) -> list[str]:
    seen: set[str] = set()
    normalized = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized
