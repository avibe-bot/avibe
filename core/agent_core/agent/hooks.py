"""C-3 lifecycle hooks.

Subclass Hooks and override only the needed methods. Hooks run in registration
order; replacement values compose, while the first Deny or End stops that hook
chain. Mutable JSON state belongs to RunContext and is persisted at commit points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from core.agent_core.agent.events import RunEndReason
from core.agent_core.ai.provider import ModelRequest
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, ToolCallBlock, UserMessage
from core.agent_core.tools.base import Tool, ToolResult


@dataclass(frozen=True)
class AgentInput:
    """An existing Avibe input row plus its already rendered model content."""

    message_id: str
    message: UserMessage

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("an input needs its stored message row id")


@dataclass(frozen=True)
class Snapshot:
    session_id: str
    context_seq: int
    state: Mapping[str, Any]


@dataclass
class RunContext:
    session_id: str
    run_id: str
    cancel: CancelToken
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunSetup:
    system: Optional[str] = None
    tools: Optional[tuple[Tool, ...]] = None


@dataclass(frozen=True)
class End:
    """Finish after committing the current step."""


@dataclass(frozen=True)
class SkipTools:
    """Settle every call in the response with a policy error."""


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class AlterArgs:
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class AlterResult:
    result: ToolResult


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    reason: RunEndReason
    snapshot: Snapshot


class Hooks:
    async def before_run(self, input: AgentInput, ctx: RunContext) -> Optional[RunSetup]:
        return None

    async def before_model(self, request: ModelRequest, ctx: RunContext) -> ModelRequest | End | None:
        return None

    async def after_model(self, message: AssistantMessage, ctx: RunContext) -> SkipTools | End | None:
        return None

    async def before_tool(self, call: ToolCallBlock, ctx: RunContext) -> Deny | AlterArgs | End | None:
        return None

    async def after_tool(self, call: ToolCallBlock, result: ToolResult, ctx: RunContext) -> AlterResult | End | None:
        return None

    async def after_run(self, outcome: RunOutcome, ctx: RunContext) -> None:
        return None
