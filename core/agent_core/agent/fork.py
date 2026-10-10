"""The side turn (C-10 ``agent-core-contracts/fork.md`` sections 6, 7, and 15).

A side turn is a fork that lives inside its caller's run: the caller's request
through a cut of its current view, then a prompt, under a ``ForkPolicy`` that
names the tools it may run. Nothing it produces is context or shown; its
caller's ``fold`` decides what of its reply enters the caller's context, and
it leaves one ``fork_turn`` audit. C-9's checkpoint turn is the first one.

The policy is data. ``PolicyGate`` is one turn's policy in force: it asks the
table once per call, after the turn's budget (``open``). Scratch is flat and is
reached only through the root's directory descriptor, which the gate opens
when the turn starts (``agent.scratch``): a write or edit is authorized only
for a single name directly inside the root, and runs relative to that
descriptor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Awaitable, Callable, Literal, Mapping, Optional

from core.agent_core.agent.scratch import ScratchRoot, valid_name
from core.agent_core.ai.provider import ModelCapabilities
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import AssistantMessage, ToolCallBlock, Usage, UserMessage
from core.agent_core.tools.args import ToolInputError, str_arg
from core.agent_core.tools.base import ToolResult
from core.agent_core.tools.edit import prepare_edit_arguments
from core.agent_core.tools.paths import resolve_to_cwd

Rule = Literal["allow", "scratch"]


@dataclass(frozen=True)
class ForkPolicy:
    """What a side turn may run (section 6): a tool ``tools`` does not name is denied with ``denied`` and never runs.

    Its budget: at most ``max_rounds`` tool rounds, each call only while the window leaves ``room_floor``; a result is
    cut to the room less ``room_slack``. Past the budget every call is answered with ``budget_used``.
    """

    name: str
    tools: Mapping[str, Rule]
    denied: str
    budget_used: str
    max_rounds: int
    room_floor: int
    room_slack: int


#: C-9's checkpoint turn (context.md section 6): it may think with the tools that only look, and write only into its
#: own scratch space. Each text ends by saying where the checkpoint belongs: a model denied a file write may otherwise
#: keep trying one.
DREAMING = ForkPolicy(
    name="dreaming",
    tools=MappingProxyType(
        {
            "read": "allow",
            "write": "scratch",
            "edit": "scratch",
            # Memory-read tools join here as "allow" once they exist.
        }
    ),
    denied=(
        "This is a checkpoint turn: tools that act outside your own scratch space are unavailable. "
        "Write the checkpoint as your reply text, not into a file."
    ),
    budget_used=(
        "This is a checkpoint turn and its tool budget is used up. Write the checkpoint as your reply text, not into a file."
    ),
    max_rounds=5,
    room_floor=4_000,
    room_slack=1_000,
)


@dataclass(frozen=True)
class Decision:
    """The table's answer for one call: a denial text, or allowed (``scratch``: the name in the scratch root)."""

    denial: Optional[str] = None
    scratch: Optional[str] = None


class PolicyGate:
    """One side turn's policy in force; ``open`` is the turn's budget, which the loop closes.

    Opening the gate opens the scratch root; ``close`` releases it when the turn ends.
    """

    def __init__(self, policy: ForkPolicy, *, cwd: str, scratch_dir: Optional[str]) -> None:
        self.policy = policy
        self._cwd = cwd
        self._root = ScratchRoot.open(scratch_dir) if scratch_dir else None
        # How a call may address the root: the configured path, or the real path it named when it was opened.
        self._addresses = (
            {os.path.normpath(os.path.abspath(scratch_dir)), os.path.realpath(scratch_dir)}
            if self._root is not None
            else set()
        )
        self.open = True

    def decide(self, call: ToolCallBlock) -> Decision:
        """The table on ``call``'s arguments (the budget is the pipeline's first check, not this)."""
        rule = self.policy.tools.get(call.name)
        if rule == "allow":
            return Decision()
        name = self._scratch_name(call) if rule == "scratch" else None
        return Decision(scratch=name) if name is not None else Decision(denial=self.policy.denied)

    def run(self, call: ToolCallBlock, name: str) -> ToolResult:
        """An authorized scratch ``write`` or ``edit``, relative to the root's descriptor."""
        assert self._root is not None
        return self._root.write(name, call.arguments) if call.name == "write" else self._root.edit(name, call.arguments)

    def close(self) -> None:
        if self._root is not None:
            self._root.close()
            self._root = None

    def _scratch_name(self, call: ToolCallBlock) -> Optional[str]:
        """The single file name a call addresses directly inside the scratch root, or None."""
        if self._root is None:
            return None
        try:
            arguments = prepare_edit_arguments(call.arguments) if call.name == "edit" else call.arguments
            raw = str_arg(arguments, "path")
            target = resolve_to_cwd(raw, self._cwd)
        except (ToolInputError, ValueError):
            return None
        name = os.path.basename(target)
        # Lexical only: nothing is resolved, because the write never goes through this pathname.
        if os.path.dirname(target) not in self._addresses or not valid_name(name):
            return None
        last = raw.replace(os.altsep or os.sep, os.sep).rstrip(os.sep).rsplit(os.sep, 1)[-1]
        return name if last == name else None


@dataclass(frozen=True)
class SideReply:
    """A side turn's reply: a response that stopped with ``stop`` and called no tool."""

    message: AssistantMessage
    rounds: int
    usage: Optional[Usage]


@dataclass(frozen=True)
class Folded:
    """What the caller's fold committed, if anything; a fold that failed (``error``) fails the turn."""

    row: Optional[ContextEntry]
    error: Optional[str] = None


@dataclass(frozen=True)
class CheckpointDetail:
    """A checkpoint side turn's purpose fields (C-9 section 6)."""

    reason: Literal["threshold", "overflow"]
    mode: Literal["normal", "rolling"]


#: Each purpose's ``detail`` type; every ``fork_turn`` audit records one.
_DETAILS: Mapping[str, type] = MappingProxyType({"checkpoint": CheckpointDetail})


@dataclass(frozen=True)
class SideTurn:
    """One side turn (section 7). ``units``: the caller's current view, all of it (None) or its first ``units``."""

    purpose: Literal["checkpoint"]
    detail: CheckpointDetail
    units: Optional[int]
    prompt: UserMessage
    policy: ForkPolicy
    max_tokens: Callable[[ModelCapabilities], int]
    fold: Callable[[SideReply], Awaitable[Folded]]

    def __post_init__(self) -> None:
        expected = _DETAILS.get(self.purpose)
        if expected is None:
            raise ValueError(f"not a side-turn purpose: {self.purpose!r}")
        if not isinstance(self.detail, expected):
            raise TypeError(f"a {self.purpose} side turn needs a {expected.__name__} detail, not {self.detail!r}")


@dataclass(frozen=True)
class SideTurnResult:
    outcome: Literal["completed", "failed"]
    folded: Optional[ContextEntry]
    error: Optional[str]
    #: Its request could not fit, or the provider refused it as overflow.
    overflow: bool = False
