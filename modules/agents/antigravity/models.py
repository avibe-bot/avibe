"""Avibe-owned model and reasoning-effort mapping for Antigravity ACP sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from modules.agents.acp import AcpClient, AcpRpcError
from modules.agents.base import AgentRequest


class AntigravityModelSelectionError(RuntimeError):
    """Raised when Avibe's selected model or reasoning effort cannot be applied."""

    def __init__(self, message: str, *, reason: str = "model_selection_failed") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AppliedModelConfig:
    """Result of applying Avibe's model and dependent effort options to an ACP session."""

    model: str
    effort: str | None
    config_options: tuple[dict[str, Any], ...]


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


async def apply_model_and_effort(
    client: AcpClient,
    session_id: str,
    config_options: Sequence[Mapping[str, Any]],
    *,
    model: str | None,
    effort: str | None,
    current_model: str | None = None,
    current_effort: str | None = None,
) -> AppliedModelConfig:
    """Explicitly apply Avibe's model, then dependent ``thought_level``, via ACP.

    Always applies ``model`` before ``thought_level`` because the valid
    ``thought_level`` values depend on the selected model and are returned in
    the updated ``configOptions`` from the model ``session/set_config_option``
    response. Fails explicitly on missing, unsupported, or rejected selections
    and never falls back to the CLI default.
    """
    selected_model = _clean_optional(model)
    if not selected_model:
        raise AntigravityModelSelectionError(
            "No Antigravity model selected; Avibe requires an explicit model and never falls back to the CLI default.",
            reason="model_selection_missing",
        )

    selected_effort = _clean_optional(effort)
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
    server_model = _clean_optional(model_option.get("currentValue"))
    model_needs_apply = current_model != selected_model or server_model != selected_model

    if model_needs_apply:
        try:
            resp = await client.session_set_config_option(session_id, model_option_id, selected_model)
        except AcpRpcError as exc:
            raise AntigravityModelSelectionError(
                f"Antigravity rejected model {selected_model!r}: {exc.message}",
                reason="model_rejected",
            ) from exc
        updated = _normalize_config_options(resp.get("configOptions"))
        if updated:
            options = updated

    if selected_effort is not None:
        effort_option = _find_config_option(
            options,
            categories=("thought_level",),
            ids=("thought_level", "reasoning_effort"),
        )
        if effort_option is None or not isinstance(effort_option.get("id"), str):
            raise AntigravityModelSelectionError(
                f"Antigravity model {selected_model!r} does not advertise a 'thought_level' config option "
                f"for requested effort {selected_effort!r}.",
                reason="effort_option_unavailable",
            )
        allowed_efforts = _extract_option_values(effort_option)
        if allowed_efforts and selected_effort not in allowed_efforts:
            raise AntigravityModelSelectionError(
                f"Reasoning effort {selected_effort!r} is not supported for Antigravity model {selected_model!r} "
                f"(available: {', '.join(sorted(allowed_efforts))}).",
                reason="effort_unsupported",
            )

        effort_option_id = str(effort_option["id"])
        server_effort = _clean_optional(effort_option.get("currentValue"))
        # Re-apply effort whenever model changed or effort differs from server/tracked state.
        if model_needs_apply or current_effort != selected_effort or server_effort != selected_effort:
            try:
                resp = await client.session_set_config_option(session_id, effort_option_id, selected_effort)
            except AcpRpcError as exc:
                raise AntigravityModelSelectionError(
                    f"Antigravity rejected reasoning effort {selected_effort!r} for model {selected_model!r}: "
                    f"{exc.message}",
                    reason="effort_rejected",
                ) from exc
            updated = _normalize_config_options(resp.get("configOptions"))
            if updated:
                options = updated

    return AppliedModelConfig(
        model=selected_model,
        effort=selected_effort,
        config_options=options,
    )
