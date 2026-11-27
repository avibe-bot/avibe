"""Frozen v3.2.8 feed validator, copied from vibe/backend_model_catalog.py
at 643075b3fa441939e4aa476e53cff747703e22a3. Keep independent of current readers.
"""

from __future__ import annotations

from typing import Any, Iterable

_HIDDEN_VISIBILITIES = {"hide", "hidden"}
_VISIBLE_VISIBILITIES = {"visible", "list"}
_SUPPORTED_BACKENDS = {"claude", "codex"}
_SUPPORTED_VISIBILITIES = {*_VISIBLE_VISIBILITIES, *_HIDDEN_VISIBILITIES}


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

    presets = item.get("client_presets")
    if presets is not None and (
        not isinstance(presets, list)
        or any(not isinstance(value, str) or not value.strip() for value in presets)
        or not {value.strip() for value in presets} <= {value.strip() for value in efforts or ()}
    ):
        raise ValueError(f"Backend model catalog contains invalid client presets: {backend}/{raw_id}")


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
    raw_efforts = item.get("reasoning_efforts")
    if raw_efforts is None:
        raw_efforts = item.get("supported_reasoning_levels")
    efforts = _coerce_reasoning_efforts(raw_efforts)
    if raw_efforts is not None:
        entry["reasoning_efforts"] = efforts
    # A preset is a menu effort the client maps before sending, so one the row
    # does not offer is invalid; the strict reader rejects it.
    raw_presets = item.get("client_presets")
    presets = [
        preset
        for preset in _dedupe_str_values(raw_presets if isinstance(raw_presets, list) else ())
        if preset in efforts
    ]
    if presets:
        entry["client_presets"] = presets
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
