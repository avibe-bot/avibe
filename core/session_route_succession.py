"""A Session whose stored model or reasoning effort left its backend's model list carries on.

A Session keeps the model and effort it was given. When the list later drops that
model (a catalog replaces ``gpt-6-astra`` by ``gpt-6.2-astra``) or that model's
effort, the next Turn would be refused until someone switched the Session by hand.
Instead the Session moves to its Agent's current default model, and to the nearest
effort the model still offers, once: the row is rewritten, so the next Turn finds
nothing to replace.

Only a fixed list in Model Hub mode refuses an unlisted model (Model Hub's
``turn.no_candidate.unlisted``); an open menu, a Direct backend, and a model a manual
Route still serves are left as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from config.v2_config import normalized_model_hub_override
from core.handlers.model_hub.offers import NATIVE_OFFER_BACKENDS

# Lowest to highest. An effort the list drops moves to the nearest one below it, else above.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class RouteSuccession:
    """What a Session's stored route moves to: each pair is (stored, successor), or None when it stays."""

    model: Optional[tuple[str, str]] = None
    reasoning_effort: Optional[tuple[str, str]] = None


def _nearest_effort(stored: str, offered: list[str]) -> str:
    if stored in _EFFORT_ORDER:
        rank = _EFFORT_ORDER.index(stored)
        ranked = sorted(
            (effort for effort in offered if effort in _EFFORT_ORDER), key=_EFFORT_ORDER.index
        )
        below = [effort for effort in ranked if _EFFORT_ORDER.index(effort) < rank]
        if below:
            return below[-1]
        if ranked:
            return ranked[0]
    return offered[0]


def retired_route_succession(
    hub_config: Any,
    backend: str,
    *,
    stored_model: Optional[str],
    stored_reasoning_effort: Optional[str],
    agent_model: Optional[str],
    agent_reasoning_effort: Optional[str],
) -> Optional[RouteSuccession]:
    """The successor of a Session's stored model and effort its backend's list no longer holds, else None.

    The model's successor is the Agent's current default; with none, or one the list
    does not hold either, the model stays and Model Hub's own refusal still explains it.
    """

    agents = getattr(hub_config, "agents", None)
    agent = agents.get(backend) if isinstance(agents, Mapping) else None
    if agent is None or agent.mode != "hub" or agent.menu_kind != "fixed":
        return None
    if backend in NATIVE_OFFER_BACKENDS:
        # Their Hub menus follow the assigned Sources. Absence (including a
        # missing observation after restart) is not permission to rewrite a
        # saved model or effort. Dispatch checks its current binding instead.
        return None
    listed = {model.id: model for model in agent.models}

    def serves(model_id: Optional[str]) -> bool:
        return bool(model_id) and (
            model_id in listed or normalized_model_hub_override(agent.routes.get(model_id)) is not None
        )

    model_pair = None
    target = stored_model or agent_model
    if stored_model and not serves(stored_model) and agent_model and agent_model != stored_model and serves(agent_model):
        model_pair = (stored_model, agent_model)
        target = agent_model
    effort_pair = None
    offered = list(listed[target].reasoning_efforts) if target in listed else []
    if stored_reasoning_effort and offered and stored_reasoning_effort not in offered:
        successor = (
            agent_reasoning_effort
            if agent_reasoning_effort in offered
            else _nearest_effort(stored_reasoning_effort, offered)
        )
        effort_pair = (stored_reasoning_effort, successor)
    if model_pair is None and effort_pair is None:
        return None
    return RouteSuccession(model=model_pair, reasoning_effort=effort_pair)
