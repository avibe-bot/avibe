from typing import TYPE_CHECKING, Optional

from .catalog import display_name_for_backend

if TYPE_CHECKING:
    from .base import BaseAgent, AgentRequest, AgentMessage
    from .service import AgentService


def __getattr__(name: str):
    # The backend catalog is also used by config and static CLI parsing.
    if name in {"BaseAgent", "AgentRequest", "AgentMessage"}:
        from . import base

        return getattr(base, name)
    if name == "AgentService":
        from .service import AgentService

        return AgentService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_agent_display_name(agent_name: Optional[str], fallback: Optional[str] = None) -> str:
    candidate = (agent_name or fallback or "Agent").strip()
    if not candidate:
        candidate = "Agent"
    return display_name_for_backend(candidate.lower())


__all__ = [
    "AgentMessage",
    "AgentRequest",
    "BaseAgent",
    "AgentService",
    "get_agent_display_name",
]
