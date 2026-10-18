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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Awaitable, Callable, Literal, Mapping, Optional, Sequence

from core.agent_core.agent.scratch import ScratchRoot, valid_name
from core.agent_core.ai.provider import ModelCapabilities
from core.agent_core.harness.context import MEMO_FILE, MIND_FILE
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
    #: Scratch names only ``edit`` may change, and the denial a ``write`` to one gets (``{name}``: the name).
    edit_only: frozenset[str] = frozenset()
    rewrite_denied: str = ""
    #: The floor a scratch ``write`` or ``edit`` runs above instead of ``room_floor``: its result is one short line,
    #: never cut, so it needs no room for a large result (0: the same floor as any call).
    scratch_floor: int = 0


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
        "This is a checkpoint turn: tools that act outside your own scratch directory are unavailable. Revise mind.md "
        "and memo.md there with edit, at the paths the request gives, and write your now as your reply text."
    ),
    budget_used=(
        "This is a checkpoint turn and its tool budget is used up. Write your now as your reply text; mind.md and "
        "memo.md stay as they are."
    ),
    max_rounds=5,
    room_floor=4_000,
    room_slack=1_000,
    # The harness's own files (C-9 section 6): revised in place, never rewritten whole.
    edit_only=frozenset({MIND_FILE, MEMO_FILE}),
    scratch_floor=500,
    rewrite_denied=(
        "This is a checkpoint turn: {name} is revised only with edit, one change at a time; write cannot replace it. "
        "Change only what changed."
    ),
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
        if name is None:
            return Decision(denial=self.policy.denied)
        if name in self.policy.edit_only and call.name != "edit":
            return Decision(denial=self.policy.rewrite_denied.format(name=name))
        return Decision(scratch=name)

    def scratch_call(self, call: ToolCallBlock) -> bool:
        """Whether ``call`` addresses a single name in the scratch root under a ``scratch`` rule: a write or an edit
        whose result is one short line (the budget's floor for it is ``scratch_floor``)."""
        return self.policy.tools.get(call.name) == "scratch" and self._scratch_name(call) is not None

    def seed(self, files: Mapping[str, str]) -> bool:
        """Write the purpose's ``files`` into the scratch root before the turn's first call; False when there is no
        root (the platform cannot open one, or no scratch directory) or a write failed."""
        if self._root is None:
            return False
        try:
            for name, content in files.items():
                self._root.put(name, content)
        except OSError:
            return False
        return True

    def read(self, names: Sequence[str]) -> dict[str, Optional[str]]:
        """Each of ``names`` as the turn has left it so far; None for one that is not a readable regular file."""
        if self._root is None:
            return {name: None for name in names}
        found: dict[str, Optional[str]] = {}
        for name in names:
            try:
                found[name] = self._root.get(name)
            except OSError:
                found[name] = None
        return found

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


#: The purpose's scratch files as the turn left them (None: one that could not be read), or None when the turn had no
#: files, or no scratch root to keep them in.
Files = Optional[Mapping[str, Optional[str]]]


@dataclass(frozen=True)
class SideReply:
    """A side turn's reply: a response that stopped with ``stop`` and called no tool, and its purpose's files."""

    message: AssistantMessage
    rounds: int
    usage: Optional[Usage]
    files: Files = None
    #: A call the turn made got the used-up budget's text instead of running.
    closed: bool = False


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
    #: The purpose's check of a reply and its files (section 7 step 4): None accepts it; otherwise the problem, which
    #: ``repair`` names in one corrective follow-up. ``final`` says no follow-up is left, so only a problem that fails
    #: the turn counts; a second rejected reply fails it.
    check: Optional[Callable[[AssistantMessage, Files, bool], Optional[str]]] = None
    repair: Optional[Callable[[str], UserMessage]] = None
    #: Files the turn starts with in the scratch root, written before its first call (C-9 section 6).
    files: Mapping[str, str] = field(default_factory=dict)
    #: Why the purpose could not prepare the turn: it fails at once, with no model call.
    refused: Optional[str] = None

    def __post_init__(self) -> None:
        expected = _DETAILS.get(self.purpose)
        if expected is None:
            raise ValueError(f"not a side-turn purpose: {self.purpose!r}")
        if not isinstance(self.detail, expected):
            raise TypeError(f"a {self.purpose} side turn needs a {expected.__name__} detail, not {self.detail!r}")
        if (self.check is None) != (self.repair is None):
            raise ValueError("a side turn's check and its repair come together")


@dataclass(frozen=True)
class SideTurnResult:
    outcome: Literal["completed", "failed"]
    folded: Optional[ContextEntry]
    error: Optional[str]
    #: Its request could not fit, or the provider refused it as overflow.
    overflow: bool = False
