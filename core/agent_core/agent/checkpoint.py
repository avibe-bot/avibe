"""The checkpoint turn's tool policy (C-9 ``context.md`` section 6).

"Dreaming": a checkpoint turn may think with the tools that only look, and
write only into its own scratch space; anything that acts on the world is
denied and never executed. The policy is a declarative table installed as the
last ``before_tool`` hook of the turn, so it judges the arguments every other
hook has already rewritten.
"""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import Literal, Mapping, Optional

from core.agent_core.agent.hooks import Deny, Hooks, RunContext
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


class CheckpointPolicy(Hooks):
    """``before_tool`` for one checkpoint turn; the loop closes it when the turn's tool budget is used up."""

    def __init__(
        self, *, cwd: str, scratch_dir: Optional[str], table: Mapping[str, Rule] = CHECKPOINT_TOOL_POLICY
    ) -> None:
        self._cwd = cwd
        self._scratch = os.path.realpath(scratch_dir) if scratch_dir else None
        self._table = table
        self.open = True

    async def before_tool(self, call: ToolCallBlock, ctx: RunContext) -> Optional[Deny]:
        rule = self._table.get(call.name)
        if rule is None or (rule == "scratch" and not self._in_scratch(call)):
            return Deny(DENIED)
        if not self.open:
            return Deny(BUDGET_USED)
        return None

    def _in_scratch(self, call: ToolCallBlock) -> bool:
        """The target's real path, resolved as the tool resolves it, lies inside the scratch directory."""
        if self._scratch is None:
            return False
        try:
            arguments = prepare_edit_arguments(call.arguments) if call.name == "edit" else call.arguments
            target = os.path.realpath(resolve_to_cwd(str_arg(arguments, "path"), self._cwd))
        except (ToolInputError, OSError, ValueError):
            return False
        return target != self._scratch and os.path.commonpath((target, self._scratch)) == self._scratch
