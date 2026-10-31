"""Detached diagnostics for one committed Agent context cut.

A preview is a current-code reconstruction. Only transport artifacts establish
historical sent bytes. Sensitive inputs and wire data deliberately have no repr.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.agent_core.agent.hooks import Snapshot
from core.agent_core.ai.preview import WirePreview
from core.agent_core.ai.provider import ModelCapabilities, ModelRequest, ProviderError
from core.agent_core.harness.context import Budget

PreviewStatus = Literal[
    "ready", "needs_context_transition", "pending_tools", "unsupported_hooks", "preparation_error",
]
ContextTransition = Literal["clear_tool_results", "checkpoint", "context_relief"]


@dataclass(frozen=True)
class RequestPreview:
    snapshot: Snapshot = field(repr=False)
    status: PreviewStatus
    request: ModelRequest | None = field(default=None, repr=False)
    capabilities: ModelCapabilities | None = None
    budget: Budget | None = None
    transition: ContextTransition | None = None
    wire: WirePreview | None = field(default=None, repr=False)
    error: ProviderError | None = field(default=None, repr=False)
    turn_inputs: tuple[int, ...] = ()
    mode: Literal["reconstructed"] = "reconstructed"
