"""The checkpoint turn's tool policy (C-9 ``context.md`` sections 6 and 10).

"Dreaming": a checkpoint turn may think with the tools that only look, and
write only into its own scratch space; anything that acts on the world is
denied and never executed. The loop's checkpoint tool pipeline asks it once
per call, in a fixed order: the turn's budget first (``open``), then the
user's ``before_tool`` hooks, then this declarative table on the arguments
they leave. An allowed scratch write is pinned to the real path the table
authorized, which the ``write`` and ``edit`` mutation boundary enforces.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Optional

from core.agent_core.messages import ToolCallBlock
from core.agent_core.tools.args import ToolInputError, str_arg
from core.agent_core.tools.edit import prepare_edit_arguments
from core.agent_core.tools.paths import resolve_to_cwd

Rule = Literal["allow", "scratch"]

#: Tools a checkpoint turn may call; every other tool is denied.
CHECKPOINT_TOOL_POLICY: Mapping[str, Rule] = MappingProxyType(
    {
        "read": "allow",
        "write": "scratch",
        "edit": "scratch",
        # Memory-read tools join here as "allow" once they exist.
    }
)

DENIED = (
    "This is a checkpoint turn: tools that act outside your own scratch space are unavailable. "
    "Write the checkpoint now."
)
BUDGET_USED = "This is a checkpoint turn and its tool budget is used up. Write the checkpoint now."


@dataclass(frozen=True)
class Decision:
    """The table's answer for one call: a denial text, or allowed with the real path a scratch write is pinned to."""

    denial: Optional[str] = None
    pinned: Optional[str] = None


class CheckpointPolicy:
    """The table for one checkpoint turn; ``open`` is the turn's budget, which the loop closes."""

    def __init__(
        self, *, cwd: str, scratch_dir: Optional[str], table: Mapping[str, Rule] = CHECKPOINT_TOOL_POLICY
    ) -> None:
        self._cwd = cwd
        self._scratch = os.path.realpath(scratch_dir) if scratch_dir else None
        self._table = table
        self.open = True

    def decide(self, call: ToolCallBlock) -> Decision:
        """The table on ``call``'s final arguments (the budget is the pipeline's first check, not this)."""
        rule = self._table.get(call.name)
        if rule == "allow":
            return Decision()
        target = self._scratch_target(call) if rule == "scratch" else None
        return Decision(pinned=target) if target is not None else Decision(denial=DENIED)

    def _scratch_target(self, call: ToolCallBlock) -> Optional[str]:
        """The target's real path, resolved as the tool resolves it, when it lies inside the scratch directory."""
        if self._scratch is None:
            return None
        try:
            arguments = prepare_edit_arguments(call.arguments) if call.name == "edit" else call.arguments
            target = os.path.realpath(resolve_to_cwd(str_arg(arguments, "path"), self._cwd))
            # commonpath raises ValueError for paths on different Windows drives: outside, so denied.
            inside = target != self._scratch and os.path.commonpath((target, self._scratch)) == self._scratch
        except (ToolInputError, OSError, ValueError):
            return None
        return target if inside else None
