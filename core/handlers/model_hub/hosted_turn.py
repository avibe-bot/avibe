"""What a Vibey turn freezes about its hosted tools (``vibey-hosted-tools.md`` §3.4, §6).

A turn's first resolution fixes which tools the turn declares, each search
tool's executor order, and the ``image_generate`` enum. Later hops of the turn
recompute only their own facts, and the executor endpoint authorizes every
call against the frozen set, so a settings or Source change applies from the
next turn. The facts themselves are Model Hub's (``hosted_tools``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from config.v2_config import ModelHubConfig, ModelHubSourceConfig

from .capability_evidence import identity_digest
from .hosted_tools import (
    HostedEvidence,
    ImageChoice,
    enabled_tools,
    executor_candidates,
    hosted_tool_facts,
    image_choices,
)

IMAGE_TOOL: Final = "image_generate"


@dataclass(frozen=True)
class ExecutorSlot:
    """One executor candidate as the chain was built: its Source identity is checked again at call time."""

    source_id: str
    model_id: str
    identity: str


@dataclass(frozen=True)
class TurnHostedTools:
    tools: frozenset[str]
    # Per search tool, its effective executor order.
    executors: Mapping[str, tuple[ExecutorSlot, ...]]
    # The image_generate enum, default first; empty when the turn has no image tool.
    images: tuple[ImageChoice, ...] = ()


def freeze_turn_tools(config: ModelHubConfig, *, evidence: HostedEvidence) -> TurnHostedTools:
    """What a turn's first resolution freezes. With no image model, ``image_generate`` is not declared at all."""

    enabled = enabled_tools(config)
    images = image_choices(config, evidence=evidence).models if IMAGE_TOOL in enabled else ()
    tools = frozenset(tool for tool in enabled if tool != IMAGE_TOOL or images)
    executors = {
        tool: tuple(
            ExecutorSlot(candidate.source.id, candidate.model_id, identity_digest(candidate.source))
            for candidate in executor_candidates(config, tool, evidence=evidence)
        )
        for tool in tools
        if tool != IMAGE_TOOL
    }
    return TurnHostedTools(tools, executors, images)


def hop_hosted_tools(
    turn: TurnHostedTools, source: ModelHubSourceConfig, model_id: str, *, evidence: HostedEvidence,
) -> dict[str, dict[str, Any]]:
    """``HopResolution.hosted_tools`` for one hop of the turn (C-6 item 7).

    A search tool's entry is the hop's own facts; the image entry is the
    turn's frozen enum, with ``params`` and ``size_forms`` as sorted lists so
    the tool schema Vibey renders from them is stable across turns.
    """

    entries: dict[str, dict[str, Any]] = {}
    for tool in sorted(turn.tools - {IMAGE_TOOL}):
        facts = hosted_tool_facts(source, model_id, tool, evidence=evidence)
        entry: dict[str, Any] = {"native": facts.native, "replay": facts.replay}
        if facts.native == "assumed":
            entry["declaration"] = dict(facts.declaration or {})
            entry["include"] = list(facts.include)
        entries[tool] = entry
    if turn.images:
        entries[IMAGE_TOOL] = {
            "models": [
                {
                    "source_id": choice.source_id,
                    "model_id": choice.model_id,
                    "description": choice.description,
                    "params": sorted(choice.params),
                    "size_forms": sorted(choice.size_forms),
                }
                for choice in turn.images
            ],
            "default": {"source_id": turn.images[0].source_id, "model_id": turn.images[0].model_id},
        }
    return entries
