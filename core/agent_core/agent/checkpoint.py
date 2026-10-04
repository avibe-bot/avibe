"""The checkpoint turn's tool policy (C-9 ``context.md`` sections 6 and 10).

"Dreaming": a checkpoint turn may think with the tools that only look, and
write only into its own scratch space; anything that acts on the world is
denied and never executed. The loop's checkpoint tool pipeline asks it once
per call, after the turn's budget (``open``). Scratch is flat and is reached
only through the root's directory descriptor, which the policy opens when the
turn starts (``agent.scratch``): a write or edit is authorized only for a
single name directly inside the root, and runs relative to that descriptor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Optional

from core.agent_core.agent.scratch import ScratchRoot, valid_name
from core.agent_core.messages import ToolCallBlock
from core.agent_core.tools.args import ToolInputError, str_arg
from core.agent_core.tools.base import ToolResult
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
    """The table's answer for one call: a denial text, or allowed (``scratch``: the name in the scratch root)."""

    denial: Optional[str] = None
    scratch: Optional[str] = None


class CheckpointPolicy:
    """The table for one checkpoint turn; ``open`` is the turn's budget, which the loop closes.

    Opening the policy opens the scratch root; ``close`` releases it when the turn ends.
    """

    def __init__(
        self, *, cwd: str, scratch_dir: Optional[str], table: Mapping[str, Rule] = CHECKPOINT_TOOL_POLICY
    ) -> None:
        self._cwd = cwd
        self._table = table
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
        rule = self._table.get(call.name)
        if rule == "allow":
            return Decision()
        name = self._scratch_name(call) if rule == "scratch" else None
        return Decision(scratch=name) if name is not None else Decision(denial=DENIED)

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
