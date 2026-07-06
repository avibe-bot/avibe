"""Agent helper utilities (reasoning options, model lists, etc.).

Keep this module free of agent state. It should only contain pure helpers.
Shared by OpenCode, Claude, and Codex integrations for reasoning-effort option building.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


_REASONING_FALLBACK_OPTIONS = [
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
]

_REASONING_VARIANT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_REASONING_VARIANT_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
}

_UTILITY_MODEL_KEYWORDS = ["embedding", "tts", "whisper", "ada", "davinci", "turbo-instruct"]


def _parse_model_key(model_key: Optional[str]) -> tuple[str, str]:
    if not model_key:
        return "", ""
    parts = model_key.split("/", 1)
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _parse_provider_id(model_key: Optional[str]) -> Optional[str]:
    provider_id, _ = _parse_model_key(model_key)
    return provider_id or None


def get_opencode_provider_id(provider: dict | None) -> str:
    if not isinstance(provider, dict):
        return ""
    provider_id = provider.get("id") or provider.get("provider_id") or provider.get("name") or ""
    return provider_id if isinstance(provider_id, str) else ""


def _opencode_model_entry_id(model_entry: Any) -> str:
    if isinstance(model_entry, str):
        return model_entry
    if not isinstance(model_entry, dict):
        return ""
    model_id = model_entry.get("id") or model_entry.get("modelID") or model_entry.get("model_id") or ""
    return model_id if isinstance(model_id, str) else ""


def find_opencode_model_info(
    opencode_models: dict | None,
    provider_id: str | None,
    model_id: str | None,
) -> dict | None:
    if not provider_id or not model_id or not isinstance(opencode_models, dict):
        return None

    for provider in opencode_models.get("providers", []) or []:
        if get_opencode_provider_id(provider) != provider_id:
            continue

        models = provider.get("models", {}) if isinstance(provider, dict) else {}
        if isinstance(models, dict):
            model_info = models.get(model_id)
            if isinstance(model_info, dict):
                return model_info
            return {} if model_info is not None else None
        if isinstance(models, list):
            for entry in models:
                if _opencode_model_entry_id(entry) == model_id:
                    return entry if isinstance(entry, dict) else {}
        return None
    return None


def resolve_opencode_model_id(
    opencode_models: dict | None,
    provider_id: str | None,
    model_id: str | None,
) -> str | None:
    """Return the catalog-canonical model id for a provider/model pair.

    This does not lowercase user input. Exact catalog matches win; otherwise we
    only adopt the catalog casing when there is exactly one case-insensitive
    match. Ambiguous or unknown ids are preserved.
    """

    if not provider_id or not model_id or not isinstance(opencode_models, dict):
        return model_id

    for provider in opencode_models.get("providers", []) or []:
        if get_opencode_provider_id(provider) != provider_id:
            continue

        models = provider.get("models", {}) if isinstance(provider, dict) else {}
        if isinstance(models, dict):
            if model_id in models:
                return model_id
            matches = [key for key in models if isinstance(key, str) and key.casefold() == model_id.casefold()]
            return matches[0] if len(matches) == 1 else model_id
        if isinstance(models, list):
            ids = [_opencode_model_entry_id(entry) for entry in models]
            if model_id in ids:
                return model_id
            matches = [entry_id for entry_id in ids if entry_id and entry_id.casefold() == model_id.casefold()]
            return matches[0] if len(matches) == 1 else model_id
        return model_id
    return model_id


def _opencode_model_supports_variant(model_info: dict | None, variant: str | None) -> bool:
    if not isinstance(model_info, dict) or not isinstance(variant, str) or not variant.strip():
        return False
    variants = model_info.get("variants")
    if isinstance(variants, dict) and variant in variants:
        return True
    capabilities = model_info.get("capabilities")
    if variants is None and "variants" not in model_info and isinstance(capabilities, dict):
        return capabilities.get("reasoning") is True
    return False


def _opencode_model_has_no_variants(model_info: dict | None) -> bool:
    if not isinstance(model_info, dict):
        return False
    capabilities = model_info.get("capabilities")
    if isinstance(capabilities, dict):
        if capabilities.get("reasoning") is True:
            return False
        if capabilities.get("reasoning") is False:
            return True
    variants = model_info.get("variants")
    if variants is None and "variants" not in model_info:
        return True
    return isinstance(variants, dict) and not variants


def resolve_opencode_reasoning_effort(
    model_dict: dict[str, str] | None,
    requested_effort: str | None,
    model_catalog: dict | None,
) -> str | None:
    """Return the variant OpenCode should receive for this model."""

    normalized_effort = (requested_effort or "").strip() or None
    if normalized_effort in {"default", "__default__"}:
        normalized_effort = None

    if not model_dict:
        return normalized_effort
    provider_id = model_dict.get("providerID")
    model_id = model_dict.get("modelID")
    if not provider_id or not model_id:
        return normalized_effort
    if not isinstance(model_catalog, dict):
        return normalized_effort

    model_info = find_opencode_model_info(model_catalog, provider_id, model_id)
    if normalized_effort:
        if _opencode_model_supports_variant(model_info, normalized_effort):
            return normalized_effort
        if isinstance(model_info, dict):
            return None
        return normalized_effort
    if _opencode_model_has_no_variants(model_info):
        return None
    return normalized_effort


def _find_model_variants(opencode_models: dict, target_model: Optional[str]) -> Dict[str, Any]:
    target_provider, target_model_id = _parse_model_key(target_model)
    if not target_provider or not target_model_id or not isinstance(opencode_models, dict):
        return {}
    model_info = find_opencode_model_info(opencode_models, target_provider, target_model_id)
    if isinstance(model_info, dict):
        variants = model_info.get("variants", {})
        if isinstance(variants, dict):
            return variants
    return {}


def _append_unique(target: List[str], value: Optional[str]) -> None:
    if not value or value in target:
        return
    target.append(value)


def _extract_provider_ids_from_config(config: dict) -> List[str]:
    providers: List[str] = []
    if not isinstance(config, dict):
        return providers

    provider_value = config.get("provider")
    if isinstance(provider_value, str):
        _append_unique(providers, provider_value)

    default_provider = config.get("default_provider")
    if isinstance(default_provider, str):
        _append_unique(providers, default_provider)

    config_providers = config.get("providers")
    if isinstance(config_providers, dict):
        for key in config_providers.keys():
            _append_unique(providers, key)
    elif isinstance(config_providers, list):
        for entry in config_providers:
            if isinstance(entry, str):
                _append_unique(providers, entry)
                continue
            if isinstance(entry, dict):
                value = entry.get("id") or entry.get("provider") or entry.get("name")
                if isinstance(value, str):
                    _append_unique(providers, value)

    return providers


def resolve_opencode_default_model(
    opencode_default_config: dict,
    opencode_agents: list,
    selected_agent: Optional[str],
) -> Optional[str]:
    """Resolve default OpenCode model for an agent from config."""
    agent_names: List[str] = []
    for agent in opencode_agents or []:
        if isinstance(agent, dict):
            name = agent.get("name") or agent.get("id")
        elif isinstance(agent, str):
            name = agent
        else:
            name = None
        if isinstance(name, str) and name:
            agent_names.append(name)

    agent_name = selected_agent or ("build" if "build" in agent_names else (agent_names[0] if agent_names else None))

    if isinstance(opencode_default_config, dict):
        agents_config = opencode_default_config.get("agent", {})
        if isinstance(agents_config, dict) and agent_name:
            agent_config = agents_config.get(agent_name, {})
            if isinstance(agent_config, dict):
                model = agent_config.get("model")
                if isinstance(model, str) and model:
                    return model
        model = opencode_default_config.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def resolve_opencode_provider_preferences(
    opencode_default_config: dict,
    current_model: Optional[str] = None,
) -> List[str]:
    """Return provider IDs to prefer first when listing models."""
    providers: List[str] = []

    _append_unique(providers, _parse_provider_id(current_model))

    if isinstance(opencode_default_config, dict):
        _append_unique(providers, _parse_provider_id(opencode_default_config.get("model")))
        agents_config = opencode_default_config.get("agent", {})
        if isinstance(agents_config, dict):
            for agent_config in agents_config.values():
                if isinstance(agent_config, dict):
                    _append_unique(providers, _parse_provider_id(agent_config.get("model")))

        for provider_id in _extract_provider_ids_from_config(opencode_default_config):
            _append_unique(providers, provider_id)

    return providers


def resolve_opencode_allowed_providers(
    opencode_default_config: dict,
    opencode_models: Optional[dict] = None,
) -> List[str]:
    """Return provider IDs to include when listing models."""
    providers = _extract_provider_ids_from_config(opencode_default_config)
    if providers:
        return providers
    if isinstance(opencode_models, dict):
        defaults = opencode_models.get("default", {})
        if isinstance(defaults, dict) and defaults:
            return [key for key in defaults.keys() if isinstance(key, str) and key]
    return []


def _model_sort_key(model_item: Tuple[str, Any]) -> Tuple[int, int, str]:
    """Sort models by utility penalty, release date (DESC), then id."""
    model_id, model_info = model_item
    mid_lower = (model_id or "").lower()
    is_utility = any(keyword in mid_lower for keyword in _UTILITY_MODEL_KEYWORDS)
    utility_penalty = 1 if is_utility else 0
    release_date = "1970-01-01"
    if isinstance(model_info, dict):
        release_date = model_info.get("release_date", "1970-01-01") or "1970-01-01"
    try:
        date_int = -int(release_date.replace("-", ""))
    except (ValueError, AttributeError):
        date_int = 0
    return (utility_penalty, date_int, model_id or "")


def build_opencode_model_option_items(
    opencode_models: dict,
    max_total: int,
    preferred_providers: Optional[List[str]] = None,
    allowed_providers: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Build sorted model options for OpenCode providers."""
    if not isinstance(opencode_models, dict) or max_total <= 0:
        return []

    providers_data = opencode_models.get("providers", [])
    defaults = opencode_models.get("default", {})

    providers: List[Tuple[str, dict]] = []
    for provider in providers_data:
        provider_id = get_opencode_provider_id(provider)
        if not provider_id:
            continue
        providers.append((provider_id, provider))

    if allowed_providers:
        allowed_set = {p for p in allowed_providers if isinstance(p, str) and p}
        if allowed_set:
            providers = [entry for entry in providers if entry[0] in allowed_set]

    if preferred_providers:
        preferred_set = {p for p in preferred_providers if isinstance(p, str) and p}
        if preferred_set:
            provider_map = {pid: provider for pid, provider in providers}
            ordered: List[Tuple[str, dict]] = []
            for pid in preferred_providers:
                provider = provider_map.get(pid)
                if provider is not None:
                    ordered.append((pid, provider))
            for pid, provider in providers:
                if pid not in preferred_set:
                    ordered.append((pid, provider))
            providers = ordered

    num_providers = len(providers)
    max_per_provider = max(5, (max_total // num_providers)) if num_providers > 0 else max_total

    options: List[Dict[str, str]] = []
    for provider_id, provider in providers:
        provider_name = provider.get("name") or provider_id
        models = provider.get("models", {})

        if isinstance(models, dict):
            model_items = list(models.items())
        elif isinstance(models, list):
            model_items = [
                (entry, entry) if isinstance(entry, str) else (entry.get("id", ""), entry) for entry in models
            ]
        else:
            model_items = []

        model_items.sort(key=_model_sort_key)
        provider_model_count = 0
        for model_id, model_info in model_items:
            if provider_model_count >= max_per_provider:
                break
            if not model_id:
                continue

            if isinstance(model_info, dict):
                model_name = model_info.get("name", model_id) or model_id
            else:
                model_name = model_id

            full_model = f"{provider_id}/{model_id}" if provider_id else model_id
            is_default = defaults.get(provider_id) == model_id if provider_id else False
            display = f"{provider_name}: {model_name}" if provider_name else model_name
            if is_default:
                display += " (default)"

            options.append({"label": display, "value": full_model})
            provider_model_count += 1

    if len(options) > max_total:
        options = options[:max_total]
    return options


def _build_reasoning_options_from_variants(variants: Dict[str, Any]) -> List[Dict[str, str]]:
    sorted_variants = sorted(
        variants.keys(),
        key=lambda variant: (
            _REASONING_VARIANT_ORDER.index(variant)
            if variant in _REASONING_VARIANT_ORDER
            else len(_REASONING_VARIANT_ORDER),
            variant,
        ),
    )
    return [
        {
            "value": variant_key,
            "label": _REASONING_VARIANT_LABELS.get(variant_key, variant_key.capitalize()),
        }
        for variant_key in sorted_variants
    ]


def build_reasoning_effort_options(
    opencode_models: dict,
    target_model: Optional[str],
) -> List[Dict[str, str]]:
    """Build reasoning effort options from OpenCode model metadata."""

    options = [{"value": "__default__", "label": "(Default)"}]
    variants = _find_model_variants(opencode_models, target_model)
    if variants:
        options.extend(_build_reasoning_options_from_variants(variants))
        return options
    options.extend(_REASONING_FALLBACK_OPTIONS)
    return options


# ---------------------------------------------------------------------------
# Codex reasoning options
# ---------------------------------------------------------------------------
# Codex supports a fixed set of reasoning effort levels.  Unlike OpenCode
# (which derives options from live model metadata), these are static.
# Defined here so that every IM module shares a single source of truth.

_CODEX_REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
_CLAUDE_REASONING_EFFORTS = ["low", "medium", "high"]
_CLAUDE_1M_CONTEXT_LABEL = "[1M]"
_CLAUDE_OPUS_ALIASES = {"opus", "opus[1m]"}
_CLAUDE_SONNET_ALIASES = {"sonnet", "sonnet[1m]"}


def _supports_claude_xhigh_reasoning(target_model: Optional[str]) -> bool:
    normalized_model = (target_model or "").strip().lower()
    if not normalized_model:
        return False
    return (
        normalized_model in _CLAUDE_OPUS_ALIASES
        or normalized_model in _CLAUDE_SONNET_ALIASES
        or normalized_model.startswith("claude-opus-4-7")
        or normalized_model.startswith("claude-opus-4-8")
        or normalized_model.startswith("claude-sonnet-5")
        or normalized_model.startswith("claude-fable-5")
    )


def _supports_claude_max_reasoning(target_model: Optional[str]) -> bool:
    normalized_model = (target_model or "").strip().lower()
    if not normalized_model:
        return False
    return (
        normalized_model in _CLAUDE_OPUS_ALIASES
        or normalized_model in _CLAUDE_SONNET_ALIASES
        or normalized_model.startswith("claude-opus-4-6")
        or normalized_model.startswith("claude-opus-4-7")
        or normalized_model.startswith("claude-opus-4-8")
        or normalized_model.startswith("claude-sonnet-5")
        or normalized_model.startswith("claude-sonnet-4-6")
        or normalized_model.startswith("claude-fable-5")
    )


def supports_claude_1m_context(target_model: Optional[str]) -> bool:
    normalized_model = (target_model or "").strip().lower()
    if not normalized_model:
        return False
    return (
        normalized_model in _CLAUDE_OPUS_ALIASES
        or normalized_model in _CLAUDE_SONNET_ALIASES
        or normalized_model.startswith("claude-opus-4-6")
        or normalized_model.startswith("claude-opus-4-7")
        or normalized_model.startswith("claude-opus-4-8")
        or normalized_model.startswith("claude-sonnet-5")
        or normalized_model.startswith("claude-sonnet-4-6")
        or normalized_model.startswith("claude-fable-5")
    )


def format_claude_model_label(model: object) -> str:
    model_id = str(model or "").strip()
    if not model_id:
        return model_id
    if supports_claude_1m_context(model_id):
        return f"{model_id} {_CLAUDE_1M_CONTEXT_LABEL}"
    return model_id


def build_codex_reasoning_options() -> List[Dict[str, str]]:
    """Return the canonical Codex reasoning-effort option list.

    The returned format mirrors ``build_reasoning_effort_options`` so that
    IM modules can render both OpenCode and Codex dropdowns with the same
    helper logic.
    """
    options: List[Dict[str, str]] = [{"value": "__default__", "label": "(Default)"}]
    for effort in _CODEX_REASONING_EFFORTS:
        options.append(
            {
                "value": effort,
                "label": _REASONING_VARIANT_LABELS.get(effort, effort.capitalize()),
            }
        )
    return options


def build_claude_reasoning_options(target_model: Optional[str]) -> List[Dict[str, str]]:
    """Return the canonical Claude reasoning-effort option list for a model.

    Claude currently supports `low` / `medium` / `high` broadly. Newer
    Opus 4.7, Opus 4.8, and Sonnet 5 models add `xhigh`; Opus 4.8,
    4.7, 4.6, Sonnet 5, and Sonnet 4.6 also support `max`.
    """

    efforts = list(_CLAUDE_REASONING_EFFORTS)
    if _supports_claude_xhigh_reasoning(target_model):
        efforts.append("xhigh")
    if _supports_claude_max_reasoning(target_model):
        efforts.append("max")

    options: List[Dict[str, str]] = [{"value": "__default__", "label": "(Default)"}]
    for effort in efforts:
        options.append(
            {
                "value": effort,
                "label": _REASONING_VARIANT_LABELS.get(effort, effort.capitalize()),
            }
        )
    return options


def normalize_claude_reasoning_effort(target_model: Optional[str], effort: Optional[str]) -> Optional[str]:
    """Return a Claude effort only when it is valid for the target model."""

    if not effort:
        return None
    allowed = {item["value"] for item in build_claude_reasoning_options(target_model)}
    return effort if effort in allowed else None
