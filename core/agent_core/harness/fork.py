"""C-10 fork point (``agent-core-contracts/fork.md`` section 2).

A fork's model-visible messages begin with a source context's projection at a
fork point and then diverge. The point is a committed ``context_seq`` (0 is the
empty prefix) at which the context is settled: every tool call at or before it
has its result at or before it, so a fork never inherits an open call. These
are pure functions of the rows; nothing here reads a clock or a live job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from core.agent_core.harness.projection import context_view, open_tool_calls
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import Message


@dataclass(frozen=True)
class ForkPoint:
    """``as_of``: the source's view is its rows with ``context_seq <= as_of``; ``units``: keep only that many of
    the view's units (a side turn's prefix of its caller's current view), or all of them."""

    as_of: int
    units: Optional[int] = None


class ForkPointError(ValueError):
    """An internal fork point that is not legal; no product surface reaches it."""

    def __init__(self, code: Literal["unsettled", "out_of_range"], message: str) -> None:
        super().__init__(message)
        self.code = code


def settled(entries: Sequence[ContextEntry], as_of: int) -> bool:
    """No tool call in the rows up to ``as_of`` lacks its result in those rows."""
    return not open_tool_calls([entry for entry in entries if entry.context_seq <= as_of])


def latest_cut(entries: Sequence[ContextEntry], *, live_input_seq: Optional[int]) -> int:
    """A user's fork: the end of the previous ended Turn.

    ``live_input_seq`` is the ``context_seq`` of the live Turn's first consumed input, when a Turn is live and has
    consumed it. The live Turn writes every row of its own after that input (one writer per Session), so the largest
    settled point before it ends the previous Turn; an ended Turn whose calls recovery has not settled yet
    contributes only up to its last settled row.
    """
    seqs = sorted(
        {entry.context_seq for entry in entries if live_input_seq is None or entry.context_seq < live_input_seq},
        reverse=True,
    )
    return next((seq for seq in seqs if settled(entries, seq)), 0)


def fork_point(entries: Sequence[ContextEntry], as_of: int) -> ForkPoint:
    """An arbitrary point a system mechanism names: in range and settled, or ``ForkPointError``."""
    bound = max((entry.context_seq for entry in entries), default=0)
    if not 0 <= as_of <= bound:
        raise ForkPointError("out_of_range", f"fork point {as_of} is outside the source context (0 to {bound})")
    if not settled(entries, as_of):
        raise ForkPointError("unsettled", f"fork point {as_of} leaves a tool call without its result")
    return ForkPoint(as_of)


def fork_prefix(entries: Sequence[ContextEntry], point: ForkPoint) -> tuple[Message, ...]:
    """The fork's messages through its cut: the source's view at ``as_of``, whole or its first ``units`` units."""
    view = context_view(entries, fork_point=point.as_of)
    return view.prefix(len(view.units) if point.units is None else point.units)
