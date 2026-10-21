"""What a Vibey turn freezes about its hosted tools (``vibey-hosted-tools.md`` §3.4, §6).

A turn's first resolution fixes which tools the turn declares, each search
tool's executor order, and the ``image_generate`` enum. Later hops of the turn
recompute only their own facts, and the executor endpoint authorizes every
call against the frozen set, so a settings or Source change applies from the
next turn. ``hosted_tools`` computes it; the turn trace holds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Mapping

if TYPE_CHECKING:
    from .hosted_tools import ImageChoice

IMAGE_TOOL: Final = "image_generate"


@dataclass(frozen=True)
class ExecutorSlot:
    """One executor candidate as the chain was built: its Source identity is checked again at call time."""

    source_id: str
    model_id: str
    identity: str
    kind: str = "model"
    results: int | None = None
    enabled: bool = True
    label: str = ""


@dataclass(frozen=True)
class TurnHostedTools:
    tools: frozenset[str]
    # Per search tool, its effective executor order.
    executors: Mapping[str, tuple[ExecutorSlot, ...]]
    # The image_generate enum, default first; empty when the turn has no image tool.
    images: tuple[ImageChoice, ...] = ()
    # None denotes the released, activation-off turn shape.
    prefer_native: bool | None = None
