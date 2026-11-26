"""Avibe-owned model and reasoning-effort mapping for Antigravity ACP sessions."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import paths
from config.atomic_io import write_atomic
from modules.agents.acp import AcpClient, AcpRpcError
from modules.agents.base import AgentRequest

logger = logging.getLogger(__name__)

_LIVE_MODELS_CACHE_SCHEMA_VERSION = 1
_LIVE_MODELS_CACHE_FILENAME = "antigravity_models_cache.json"
_LIVE_MODELS_CACHE_LOCK = threading.Lock()
_CANONICAL_AUTH_MODES = ("oauth-personal", "oauth-business", "gemini-api-key", "agent-platform")
_AUTH_MODE_ALIASES = {
    "api_key": "gemini-api-key",
    "agent_platform": "agent-platform",
    "adc": "agent-platform",
}


class AntigravityModelSelectionError(RuntimeError):
    """Raised when Avibe's selected model or reasoning effort cannot be applied."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "model_selection_failed",
        config_options: tuple[dict[str, Any], ...] | None = None,
        model: str | None = None,
        default_effort: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.config_options = config_options
        self.model = model
        self.default_effort = default_effort


@dataclass(frozen=True)
class AppliedModelConfig:
    """Result of applying Avibe's model and dependent effort options to an ACP session."""

    model: str
    effort: str | None
    config_options: tuple[dict[str, Any], ...]
    default_effort: str | None = None


_EFFORT_CATEGORIES = ("thought_level",)
_EFFORT_IDS = ("thought_level", "reasoning_effort")


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_requested_model_and_effort(
    request: AgentRequest,
) -> tuple[str | None, str | None]:
    """Resolve the explicit Avibe-owned ``(model, effort)`` for one Antigravity Turn.

    Never reads or falls back to any backend-native CLI default.
    """
    subagent_model = _clean_optional(request.subagent_model)
    if subagent_model:
        model: str | None = subagent_model
    elif request.vibe_agent_model_explicit:
        model = _clean_optional(request.vibe_agent_model)
    else:
        model = _clean_optional(request.vibe_agent_model) or _clean_optional(
            request.vibe_agent_inherited_model
        )

    subagent_effort = _clean_optional(request.subagent_reasoning_effort)
    if subagent_effort:
        effort: str | None = subagent_effort
    elif request.vibe_agent_reasoning_effort_explicit:
        effort = _clean_optional(request.vibe_agent_reasoning_effort)
    else:
        effort = _clean_optional(request.vibe_agent_reasoning_effort) or _clean_optional(
            request.vibe_agent_inherited_reasoning_effort
        )

    return model, effort


def _find_config_option(
    config_options: Sequence[Mapping[str, Any]],
    *,
    categories: tuple[str, ...],
    ids: tuple[str, ...],
) -> Mapping[str, Any] | None:
    for opt in config_options:
        if opt.get("category") in categories:
            return opt
    for opt in config_options:
        if opt.get("id") in ids:
            return opt
    return None


def _extract_option_values(option: Mapping[str, Any]) -> set[str]:
    """Extract selectable string values from flat or grouped ACP config options."""
    values: set[str] = set()
    raw_options = option.get("options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        return values
    for entry in raw_options:
        if not isinstance(entry, Mapping):
            continue
        val = entry.get("value")
        if isinstance(val, str) and val:
            values.add(val)
        nested = entry.get("options")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            for sub in nested:
                if isinstance(sub, Mapping):
                    sub_val = sub.get("value")
                    if isinstance(sub_val, str) and sub_val:
                        values.add(sub_val)
    return values


def _normalize_config_options(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _option_current_value(option: Mapping[str, Any]) -> str | None:
    if "currentValue" in option:
        return _clean_optional(option.get("currentValue"))
    return _clean_optional(option.get("current_value"))


def _option_default_value(option: Mapping[str, Any]) -> str | None:
    for key in ("defaultValue", "default_value"):
        if key in option:
            cleaned = _clean_optional(option.get(key))
            if cleaned is not None:
                return cleaned
    return None


def _valid_option_value(value: str | None, allowed: set[str]) -> str | None:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    if allowed and cleaned not in allowed:
        return None
    return cleaned


def extract_initial_default_effort(
    config_options: Sequence[Mapping[str, Any]],
    *,
    allow_current_value: bool,
) -> tuple[str, str] | None:
    """Return ``(model, effort)`` baseline from initial session ``configOptions`` if safe."""
    model_option = _find_config_option(config_options, categories=("model",), ids=("model",))
    effort_option = _find_config_option(
        config_options,
        categories=_EFFORT_CATEGORIES,
        ids=_EFFORT_IDS,
    )
    if model_option is None or effort_option is None:
        return None
    init_model = _option_current_value(model_option)
    if init_model is None:
        return None
    allowed_efforts = _extract_option_values(effort_option)
    init_effort = _valid_option_value(_option_default_value(effort_option), allowed_efforts)
    if init_effort is None and allow_current_value:
        init_effort = _valid_option_value(_option_current_value(effort_option), allowed_efforts)
    if init_effort is None:
        return None
    return init_model, init_effort


def extract_current_model_and_effort(
    config_options: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """Return ``(model, effort)`` currently reported in ``config_options``."""
    model_option = _find_config_option(config_options, categories=("model",), ids=("model",))
    effort_option = _find_config_option(
        config_options,
        categories=_EFFORT_CATEGORIES,
        ids=_EFFORT_IDS,
    )
    current_model = _option_current_value(model_option) if model_option is not None else None
    current_effort = _option_current_value(effort_option) if effort_option is not None else None
    return current_model, current_effort


def has_model_config_option(config_options: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when ``config_options`` advertises a selectable ``model`` option."""
    model_option = _find_config_option(config_options, categories=("model",), ids=("model",))
    return (
        model_option is not None
        and isinstance(model_option.get("id"), str)
        and bool(model_option["id"].strip())
    )


def validate_model_selection_receipt(
    response: Mapping[str, Any] | None,
    expected_model: str,
    *,
    model_option_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Validate any model receipt returned by ACP model-selection RPCs.

    Checks ``configOptions``, ``currentModelId``/``current_model_id``,
    ``modelId``/``model_id``, and ``models.currentModelId`` against
    ``expected_model``, raising ``AntigravityModelSelectionError`` with
    ``reason="model_mismatch"`` if any returned receipt contradicts it.
    Returns normalized ``configOptions`` if present, otherwise ``()``.
    """
    if not isinstance(response, Mapping):
        return ()

    updated = _normalize_config_options(response.get("configOptions"))
    option_ids = (model_option_id, "model") if model_option_id else ("model",)
    if updated:
        returned_model_opt = _find_config_option(
            updated,
            categories=("model",),
            ids=option_ids,
        )
        if returned_model_opt is not None:
            confirmed_model = _option_current_value(returned_model_opt)
            if confirmed_model is not None and confirmed_model != expected_model:
                raise AntigravityModelSelectionError(
                    f"Antigravity applied model {confirmed_model!r} instead of requested {expected_model!r}.",
                    reason="model_mismatch",
                    config_options=updated,
                )

    candidates: list[Any] = [
        response.get("currentModelId"),
        response.get("current_model_id"),
        response.get("modelId"),
        response.get("model_id"),
    ]
    raw_models = response.get("models")
    if isinstance(raw_models, Mapping):
        candidates.append(raw_models.get("currentModelId"))
        candidates.append(raw_models.get("current_model_id"))

    for raw_confirmed in candidates:
        confirmed = _clean_optional(raw_confirmed)
        if confirmed is not None and confirmed != expected_model:
            raise AntigravityModelSelectionError(
                f"Antigravity applied model {confirmed!r} instead of requested {expected_model!r}.",
                reason="model_mismatch",
                config_options=updated or None,
            )

    return updated


def _with_option_current_value(
    options: tuple[dict[str, Any], ...],
    *,
    categories: tuple[str, ...],
    ids: tuple[str, ...],
    value: str,
) -> tuple[dict[str, Any], ...]:
    target = _find_config_option(options, categories=categories, ids=ids)
    if target is None:
        return options
    updated_items: list[dict[str, Any]] = []
    for item in options:
        copied = dict(item)
        if item is target:
            key = "current_value" if ("current_value" in copied and "currentValue" not in copied) else "currentValue"
            copied[key] = value
        updated_items.append(copied)
    return tuple(updated_items)


async def _set_model_option(
    client: AcpClient,
    session_id: str,
    model_option_id: str,
    selected_model: str,
    options: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], bool]:
    try:
        resp = await client.session_set_config_option(session_id, model_option_id, selected_model)
    except AcpRpcError as exc:
        raise AntigravityModelSelectionError(
            f"Antigravity rejected model {selected_model!r}: {exc.message}",
            reason="model_rejected",
        ) from exc
    updated = validate_model_selection_receipt(
        resp,
        selected_model,
        model_option_id=model_option_id,
    )
    if not updated:
        return (
            _with_option_current_value(
                options,
                categories=("model",),
                ids=(model_option_id, "model"),
                value=selected_model,
            ),
            False,
        )
    return updated, True


async def _set_effort_option(
    client: AcpClient,
    session_id: str,
    effort_option_id: str,
    target_effort: str,
    selected_model: str,
    options: tuple[dict[str, Any], ...],
    *,
    fallback_config_options: tuple[dict[str, Any], ...] | None = None,
    default_effort: str | None = None,
) -> tuple[dict[str, Any], ...]:
    try:
        resp = await client.session_set_config_option(session_id, effort_option_id, target_effort)
    except AcpRpcError as exc:
        raise AntigravityModelSelectionError(
            f"Antigravity rejected reasoning effort {target_effort!r} for model {selected_model!r}: "
            f"{exc.message}",
            reason="effort_rejected",
            config_options=fallback_config_options,
            model=selected_model,
            default_effort=default_effort,
        ) from exc
    try:
        updated = validate_model_selection_receipt(resp, selected_model)
    except AntigravityModelSelectionError as exc:
        raise AntigravityModelSelectionError(
            str(exc),
            reason=exc.reason,
            config_options=exc.config_options or fallback_config_options,
            model=selected_model,
            default_effort=default_effort,
        ) from exc
    if not updated:
        return _with_option_current_value(
            options,
            categories=_EFFORT_CATEGORIES,
            ids=(effort_option_id, *_EFFORT_IDS),
            value=target_effort,
        )
    returned_effort_opt = _find_config_option(
        updated,
        categories=_EFFORT_CATEGORIES,
        ids=(effort_option_id, *_EFFORT_IDS),
    )
    if returned_effort_opt is not None:
        confirmed_effort = _option_current_value(returned_effort_opt)
        if confirmed_effort is not None and confirmed_effort != target_effort:
            raise AntigravityModelSelectionError(
                f"Antigravity applied reasoning effort {confirmed_effort!r} instead of requested "
                f"{target_effort!r} for model {selected_model!r}.",
                reason="effort_mismatch",
                config_options=updated,
                model=selected_model,
                default_effort=default_effort,
            )
    return updated


async def apply_model_and_effort(
    client: AcpClient,
    session_id: str,
    config_options: Sequence[Mapping[str, Any]],
    *,
    model: str | None,
    effort: str | None,
    current_model: str | None = None,
    current_effort: str | None = None,
    default_effort: str | None = None,
    force_reapply: bool = False,
) -> AppliedModelConfig:
    """Explicitly apply Avibe's model, then dependent ``thought_level``, via ACP.

    Always applies ``model`` before ``thought_level`` because the valid
    ``thought_level`` values depend on the selected model and are returned in
    the updated ``configOptions`` from the model ``session/set_config_option``
    response. When clearing an active effort override (``effort=None`` while
    ``current_effort`` was set), restores the model's native baseline effort
    (or re-applies the model when no baseline is known) so stale overrides never
    persist on the ACP server. Fails explicitly on missing, unsupported, or
    rejected selections and never falls back to the CLI default.
    """
    selected_model = _clean_optional(model)
    if not selected_model:
        raise AntigravityModelSelectionError(
            "No Antigravity model selected; Avibe requires an explicit model and never falls back to the CLI default.",
            reason="model_selection_missing",
        )

    selected_effort = _clean_optional(effort)
    prior_model = _clean_optional(current_model)
    prior_effort = _clean_optional(current_effort)
    options = tuple(dict(item) for item in config_options)

    model_option = _find_config_option(options, categories=("model",), ids=("model",))
    if model_option is None or not isinstance(model_option.get("id"), str):
        raise AntigravityModelSelectionError(
            f"Antigravity ACP server did not advertise a selectable 'model' config option for {selected_model!r}.",
            reason="model_option_unavailable",
        )

    allowed_models = _extract_option_values(model_option)
    if allowed_models and selected_model not in allowed_models:
        raise AntigravityModelSelectionError(
            f"Antigravity model {selected_model!r} is not supported by this session "
            f"(available: {', '.join(sorted(allowed_models))}).",
            reason="model_unsupported",
        )

    model_option_id = str(model_option["id"])
    server_model = _option_current_value(model_option)
    model_needs_apply = force_reapply or prior_model != selected_model or server_model != selected_model
    model_options_refreshed = False

    if model_needs_apply:
        options, model_options_refreshed = await _set_model_option(
            client,
            session_id,
            model_option_id,
            selected_model,
            options,
        )

    latest_options = options if model_options_refreshed else None

    effort_option = _find_config_option(
        options,
        categories=_EFFORT_CATEGORIES,
        ids=_EFFORT_IDS,
    )
    baseline_effort = _clean_optional(default_effort)
    if effort_option is not None:
        allowed_efforts = _extract_option_values(effort_option)
        baseline_effort = _valid_option_value(baseline_effort, allowed_efforts)
        advertised_default = _valid_option_value(_option_default_value(effort_option), allowed_efforts)
        advertised_current = _valid_option_value(_option_current_value(effort_option), allowed_efforts)
        if advertised_default is not None:
            baseline_effort = advertised_default
        elif baseline_effort is None and (
            prior_effort is None or (model_options_refreshed and advertised_current != prior_effort)
        ):
            baseline_effort = advertised_current
    else:
        allowed_efforts = set()
        baseline_effort = None

    if effort_option is None or not allowed_efforts:
        selected_effort = None
    elif selected_effort is not None and selected_effort not in allowed_efforts:
        from core.session_route_succession import _nearest_effort

        offered = [value for value, _ in _extract_option_choices_ordered(effort_option)]
        if baseline_effort in offered:
            offered.remove(baseline_effort)
            offered.insert(0, baseline_effort)
        selected_effort = _nearest_effort(selected_effort, offered)

    if selected_effort is not None and effort_option is not None:
        effort_option_id = str(effort_option["id"])
        server_effort = _option_current_value(effort_option)
        # Re-apply effort whenever model changed or effort differs from server/tracked state.
        if model_needs_apply or prior_effort != selected_effort or server_effort != selected_effort:
            options = await _set_effort_option(
                client,
                session_id,
                effort_option_id,
                selected_effort,
                selected_model,
                options,
                fallback_config_options=latest_options,
                default_effort=baseline_effort,
            )
    elif effort_option is not None and isinstance(effort_option.get("id"), str):
        effort_option_id = str(effort_option["id"])
        server_effort = _option_current_value(effort_option)
        reset_target = baseline_effort
        if reset_target is not None and (
            server_effort != reset_target
            or (force_reapply and not model_options_refreshed)
        ):
            options = await _set_effort_option(
                client,
                session_id,
                effort_option_id,
                reset_target,
                selected_model,
                options,
                fallback_config_options=latest_options,
                default_effort=baseline_effort,
            )
        elif prior_effort is not None and reset_target is None:
            if not model_needs_apply:
                options, refreshed = await _set_model_option(
                    client,
                    session_id,
                    model_option_id,
                    selected_model,
                    options,
                )
                if refreshed:
                    model_options_refreshed = True
                    latest_options = options
                effort_option = _find_config_option(
                    options,
                    categories=_EFFORT_CATEGORIES,
                    ids=(effort_option_id, *_EFFORT_IDS),
                )
            if effort_option is not None and isinstance(effort_option.get("id"), str):
                effort_option_id = str(effort_option["id"])
                allowed_efforts = _extract_option_values(effort_option)
                server_effort = _option_current_value(effort_option)
                refreshed_default = _valid_option_value(_option_default_value(effort_option), allowed_efforts)
                if refreshed_default is not None:
                    baseline_effort = refreshed_default
                    if server_effort != refreshed_default:
                        options = await _set_effort_option(
                            client,
                            session_id,
                            effort_option_id,
                            refreshed_default,
                            selected_model,
                            options,
                            fallback_config_options=latest_options,
                            default_effort=baseline_effort,
                        )
                elif server_effort == prior_effort and bool(allowed_efforts - {prior_effort}):
                    raise AntigravityModelSelectionError(
                        f"Antigravity kept reasoning effort {server_effort!r} for model {selected_model!r} "
                        "after clearing the override and advertised no default value to restore.",
                        reason="effort_reset_unavailable",
                        config_options=latest_options,
                        model=selected_model,
                        default_effort=baseline_effort,
                    )
                else:
                    baseline_effort = _valid_option_value(server_effort, allowed_efforts)

    return AppliedModelConfig(
        model=selected_model,
        effort=selected_effort,
        config_options=options,
        default_effort=baseline_effort,
    )


def _config_str(config: Any, field_name: str) -> str:
    value = getattr(config, field_name, None)
    return str(value).strip() if value is not None else ""


def _extract_antigravity_config(config: Any) -> Any:
    if config is None:
        return None
    agents = getattr(config, "agents", None)
    if agents is not None and hasattr(agents, "antigravity"):
        return getattr(agents, "antigravity")
    if hasattr(config, "antigravity"):
        return getattr(config, "antigravity")
    return config


def _canonical_auth_mode(antigravity_config: Any) -> str:
    raw = (_config_str(antigravity_config, "auth_mode") or "oauth-personal").lower()
    mode = _AUTH_MODE_ALIASES.get(raw, raw)
    return mode if mode in _CANONICAL_AUTH_MODES else "oauth-personal"


def auth_config_fingerprint(antigravity_config: Any) -> str:
    """Hash auth-affecting Antigravity config fields without exposing secrets."""
    cfg = _extract_antigravity_config(antigravity_config)
    payload = {
        "auth_mode": _canonical_auth_mode(cfg),
        "api_key": _config_str(cfg, "api_key"),
        "base_url": _config_str(cfg, "base_url"),
        "gcp_project": _config_str(cfg, "gcp_project"),
        "gcp_location": _config_str(cfg, "gcp_location"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_option_choices_ordered(option: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Extract ordered, unique ``(value, display_name)`` pairs from flat or grouped ACP config options."""
    choices: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def _add(entry: Any) -> None:
        if isinstance(entry, str):
            val = entry.strip()
            label = None
        elif isinstance(entry, Mapping):
            raw_val = entry.get("value") or entry.get("id") or entry.get("modelId")
            val = raw_val.strip() if isinstance(raw_val, str) else ""
            raw_label = (
                entry.get("name")
                or entry.get("label")
                or entry.get("displayName")
                or entry.get("display_name")
            )
            label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None
        else:
            return
        if val and val not in seen:
            seen.add(val)
            choices.append((val, label))

    raw_options = option.get("options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        return choices
    for entry in raw_options:
        if isinstance(entry, Mapping):
            if entry.get("value") or entry.get("id") or entry.get("modelId"):
                _add(entry)
            nested = entry.get("options")
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                for sub in nested:
                    _add(sub)
        else:
            _add(entry)
    return choices


def extract_live_model_catalog_entries(
    session_or_config_options: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Extract live model catalog entries from ACP ``session/new`` or ``configOptions`` payloads."""
    entries_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _upsert(
        model_id: str,
        *,
        label: str | None = None,
        reasoning_efforts: Sequence[str] | None = None,
    ) -> None:
        cleaned_id = model_id.strip()
        if not cleaned_id:
            return
        existing = entries_by_id.get(cleaned_id)
        if existing is None:
            existing = {"id": cleaned_id}
            entries_by_id[cleaned_id] = existing
            order.append(cleaned_id)
        if label and label != cleaned_id and (not existing.get("label") or existing["label"] == cleaned_id):
            existing["label"] = label
        if reasoning_efforts is not None:
            existing["reasoning_efforts"] = list(reasoning_efforts)

    config_options: tuple[dict[str, Any], ...] = ()
    current_model: str | None = None

    if isinstance(session_or_config_options, Mapping):
        config_options = _normalize_config_options(session_or_config_options.get("configOptions"))
    elif isinstance(session_or_config_options, Sequence) and not isinstance(
        session_or_config_options, (str, bytes)
    ):
        config_options = tuple(
            dict(item) for item in session_or_config_options if isinstance(item, Mapping)
        )

    model_option = _find_config_option(config_options, categories=("model",), ids=("model",))
    if model_option is not None:
        for val, label in _extract_option_choices_ordered(model_option):
            _upsert(val, label=label)
        opt_current = _option_current_value(model_option)
        if opt_current:
            current_model = opt_current

    effort_option = _find_config_option(
        config_options,
        categories=_EFFORT_CATEGORIES,
        ids=("thought_level",),
    )
    if current_model in entries_by_id:
        efforts = [val for val, _ in _extract_option_choices_ordered(effort_option)] if effort_option else []
        _upsert(current_model, reasoning_efforts=efforts)

    return [entries_by_id[model_id] for model_id in order]


def _antigravity_models_cache_path() -> Path:
    return paths.get_state_dir() / _LIVE_MODELS_CACHE_FILENAME


def _normalize_cached_model_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    entry: dict[str, Any] = {"id": raw_id.strip()}
    raw_label = item.get("label")
    if isinstance(raw_label, str) and raw_label.strip() and raw_label.strip() != entry["id"]:
        entry["label"] = raw_label.strip()
    raw_efforts = item.get("reasoning_efforts")
    if isinstance(raw_efforts, Sequence) and not isinstance(raw_efforts, (str, bytes)):
        efforts: list[str] = []
        seen: set[str] = set()
        for val in raw_efforts:
            if isinstance(val, str):
                cleaned = val.strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    efforts.append(cleaned)
        entry["reasoning_efforts"] = efforts
    return entry


def _read_raw_live_models_cache(cache_path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return (
        loaded
        if isinstance(loaded, dict) and loaded.get("schema_version") == _LIVE_MODELS_CACHE_SCHEMA_VERSION
        else {}
    )


def invalidate_antigravity_live_models() -> None:
    """Discard account observations on OAuth cutover, whose config may be unchanged."""
    with _LIVE_MODELS_CACHE_LOCK:
        _antigravity_models_cache_path().unlink(missing_ok=True)


def record_antigravity_live_models(
    config: Any,
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    auth_fingerprint: str | None = None,
) -> None:
    """Persist account-scoped live Antigravity models and per-model effort options atomically."""
    extracted = extract_live_model_catalog_entries(payload)
    if not extracted:
        return

    active_fp = (auth_fingerprint or "").strip() or auth_config_fingerprint(config)
    replace_set = isinstance(payload, Mapping)
    cache_path = _antigravity_models_cache_path()

    try:
        with _LIVE_MODELS_CACHE_LOCK:
            existing = _read_raw_live_models_cache(cache_path)
            existing_models: list[dict[str, Any]] = []
            if existing.get("auth_fingerprint") == active_fp and isinstance(existing.get("models"), list):
                for item in existing["models"]:
                    normalized = _normalize_cached_model_entry(item)
                    if normalized is not None:
                        existing_models.append(normalized)

            if replace_set:
                prev_by_id = {m["id"]: m for m in existing_models}
                merged_models: list[dict[str, Any]] = []
                for entry in extracted:
                    merged = dict(entry)
                    prev = prev_by_id.get(entry["id"])
                    if prev is not None:
                        if not merged.get("label") and prev.get("label"):
                            merged["label"] = prev["label"]
                        if "reasoning_efforts" not in merged and "reasoning_efforts" in prev:
                            merged["reasoning_efforts"] = list(prev["reasoning_efforts"])
                    merged_models.append(merged)
            elif existing_models:
                merged_by_id = {m["id"]: dict(m) for m in existing_models}
                order = [m["id"] for m in existing_models]
                for entry in extracted:
                    model_id = entry["id"]
                    current = merged_by_id.get(model_id)
                    if current is None:
                        merged_by_id[model_id] = dict(entry)
                        order.append(model_id)
                        continue
                    if entry.get("label") and entry["label"] != model_id:
                        current["label"] = entry["label"]
                    if "reasoning_efforts" in entry:
                        current["reasoning_efforts"] = list(entry["reasoning_efforts"])
                merged_models = [merged_by_id[mid] for mid in order]
            else:
                merged_models = [dict(entry) for entry in extracted]

            document = {
                "schema_version": _LIVE_MODELS_CACHE_SCHEMA_VERSION,
                "auth_fingerprint": active_fp,
                "updated_at": time.time(),
                "models": merged_models,
            }
            write_atomic(cache_path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    except Exception:
        logger.debug("Best-effort Antigravity live model cache write skipped", exc_info=True)


def read_antigravity_live_models(
    config: Any | None = None,
    *,
    auth_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    """Return cached live Antigravity models when the cache matches the active auth fingerprint."""
    cache_path = _antigravity_models_cache_path()
    if not cache_path.is_file():
        return []

    cached = _read_raw_live_models_cache(cache_path)
    cached_fp = cached.get("auth_fingerprint")
    raw_models = cached.get("models")
    if not isinstance(cached_fp, str) or not cached_fp or not isinstance(raw_models, list) or not raw_models:
        return []

    active_fp = (auth_fingerprint or "").strip()
    if not active_fp:
        if config is not None:
            active_fp = auth_config_fingerprint(config)
        else:
            try:
                from config.v2_config import V2Config

                active_fp = auth_config_fingerprint(V2Config.load().agents.antigravity)
            except Exception:
                logger.debug("Could not load V2Config for Antigravity live model cache lookup", exc_info=True)
                return []

    if cached_fp != active_fp:
        return []

    entries: list[dict[str, Any]] = []
    for item in raw_models:
        normalized = _normalize_cached_model_entry(item)
        if normalized is not None:
            entries.append(normalized)
    return entries
