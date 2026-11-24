"""Avibe-owned model and reasoning-effort mapping for Antigravity ACP sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from modules.agents.acp import AcpClient, AcpRpcError
from modules.agents.base import AgentRequest


class AntigravityModelSelectionError(RuntimeError):
    """Raised when Avibe's selected model or reasoning effort cannot be applied."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "model_selection_failed",
        config_options: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.config_options = config_options


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


def has_model_config_option(config_options: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when ``config_options`` advertises a selectable ``model`` option."""
    model_option = _find_config_option(config_options, categories=("model",), ids=("model",))
    return model_option is not None and isinstance(model_option.get("id"), str)


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
    updated = _normalize_config_options(resp.get("configOptions"))
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
    returned_model_opt = _find_config_option(
        updated,
        categories=("model",),
        ids=(model_option_id, "model"),
    )
    if returned_model_opt is not None:
        confirmed_model = _option_current_value(returned_model_opt)
        if confirmed_model is not None and confirmed_model != selected_model:
            raise AntigravityModelSelectionError(
                f"Antigravity applied model {confirmed_model!r} instead of requested {selected_model!r}.",
                reason="model_mismatch",
                config_options=updated,
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
) -> tuple[dict[str, Any], ...]:
    try:
        resp = await client.session_set_config_option(session_id, effort_option_id, target_effort)
    except AcpRpcError as exc:
        raise AntigravityModelSelectionError(
            f"Antigravity rejected reasoning effort {target_effort!r} for model {selected_model!r}: "
            f"{exc.message}",
            reason="effort_rejected",
            config_options=fallback_config_options,
        ) from exc
    updated = _normalize_config_options(resp.get("configOptions"))
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

    if selected_effort is not None:
        if effort_option is None or not isinstance(effort_option.get("id"), str):
            raise AntigravityModelSelectionError(
                f"Antigravity model {selected_model!r} does not advertise a 'thought_level' config option "
                f"for requested effort {selected_effort!r}.",
                reason="effort_option_unavailable",
                config_options=latest_options,
            )
        if allowed_efforts and selected_effort not in allowed_efforts:
            raise AntigravityModelSelectionError(
                f"Reasoning effort {selected_effort!r} is not supported for Antigravity model {selected_model!r} "
                f"(available: {', '.join(sorted(allowed_efforts))}).",
                reason="effort_unsupported",
                config_options=latest_options,
            )

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
                        )
                elif server_effort == prior_effort and bool(allowed_efforts - {prior_effort}):
                    raise AntigravityModelSelectionError(
                        f"Antigravity kept reasoning effort {server_effort!r} for model {selected_model!r} "
                        "after clearing the override and advertised no default value to restore.",
                        reason="effort_reset_unavailable",
                        config_options=latest_options,
                    )
                else:
                    baseline_effort = _valid_option_value(server_effort, allowed_efforts)

    return AppliedModelConfig(
        model=selected_model,
        effort=selected_effort,
        config_options=options,
        default_effort=baseline_effort,
    )
