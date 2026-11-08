"""Vibey's durable cached prefix and Turn-start freshness policy.

This optional state is deliberately outside the transcript. All disk operations
are synchronous and called on the adapter's worker threads (or the fork service's
existing synchronous path). Corruption or write failure only loses the optimization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.agent_core.ai.provider import HostedToolRoute, ModelEndpoint
from core.agent_core.messages import TextBlock
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)
DEADLINE_S = 30 * 60
VERSION = 1

# IDs are the extension point for future modules (including Bots). Unknown = 2.
# Presence changes of optional static modules describe configuration, not upgrades.
COMPONENT_TIERS = {
    "agent-identity": 1,
    "coding-rules": 1,
    "instruction-contract": 1,
    "prefix-contract": 1,
    "local-tools": 1,
    "tool-environment": 3,
    "base-capabilities-intro": 1,
    "agent-working-principles": 1,
    "gpt-reporting-prompt": 1,
    "base-capabilities-body": 1,
    "show-pages-prompt": 1,
    "quick-replies-prompt": 1,
    "vault-routing-prompt": 1,
    "harness-routing-prompt": 1,
    "project-context-prompt": 1,
    "skills-prompt": 1,
    "skills-manual-prompt": 1,
    "skills-catalog-heading": 1,
    "skills-pagination-prompt": 1,
    "mcp-capabilities-prompt": 1,
    "mcp-pagination-prompt": 1,
    "session-title-prompt": 1,
    "agent-instructions": 1,
    # A Bot's sections are who it is (docs/plans/bots.md "Prompt drafts"); the roster is someone else's change.
    "bot-identity-prompt": 1,
    "bot-memory-prompt": 1,
    "bot-main-prompt": 1,
    "bot-harness-prompt": 1,
    "harness-agents-prompt": 2,
    "skills-catalog": 2,
    "skills-more-notice": 2,
    "mcp-catalog": 2,
    "mcp-more-notice": 2,
    "hosted-tools": 2,
}
OPTIONAL_MODULES = frozenset({
    "quick-replies-prompt", "session-title-prompt", "vault-routing-prompt",
    "skills-prompt", "skills-manual-prompt", "skills-catalog-heading", "skills-pagination-prompt",
    "mcp-capabilities-prompt", "mcp-pagination-prompt",
})
CATALOG_NAMES = {
    "harness-agents-prompt": "agents",
    "skills-catalog": "skills",
    "skills-more-notice": "skills",
    "mcp-catalog": "mcp",
    "mcp-more-notice": "mcp",
    "hosted-tools": "hosted-tools",
}


def revision(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Component:
    id: str
    content: str
    # Instruction path identity stays separate from the human-readable notice name.
    path: str | None = None
    global_file: bool = False
    source_revision: str | None = None
    # The authored template, distinguished from its live catalog values.
    contract_revision: str | None = None

    @property
    def revision(self) -> str:
        return revision(self.content)

    @property
    def name(self) -> str:
        return self.path or CATALOG_NAMES.get(self.id, self.id)


@dataclass(frozen=True)
class Prefix:
    system: str
    components: tuple[Component, ...]
    tools: tuple[ToolSpec, ...]
    # Only rendered declaration facts, never auth, URLs, or credential-bearing headers.
    hosted: Mapping[str, Any] = field(default_factory=dict)
    # Observation of the catalog actually offered, not the deferred candidate.
    catalog: Mapping[str, Any] | None = None

    @property
    def revision(self) -> str:
        return revision([self.system, [asdict(tool) for tool in self.tools], self.hosted])


@dataclass(frozen=True)
class Snapshot:
    prefix: Prefix
    applied_at: float
    route: tuple[str, ...]
    checkpoint_seq: int


@dataclass(frozen=True)
class Pending:
    component: str
    name: str
    revision: str
    tier: int
    pending_since: float
    own: bool = False


@dataclass(frozen=True)
class State:
    snapshot: Snapshot
    pending: tuple[Pending, ...] = ()
    written: Mapping[str, str] = field(default_factory=dict)
    # A checkpoint is a one-Turn opportunity, even when the prefix stayed identical.
    observed_checkpoint_seq: int = 0

    @property
    def notice(self) -> str:
        names: dict[str, list[str]] = {}
        for item in self.pending:
            if not item.own:
                names.setdefault(item.name, []).append(item.revision)
        if not names:
            return "current"
        return "pending " + "; ".join(
            f"{name}@{(revs[0] if len(revs) == 1 else revision(revs))[:6]}"
            for name, revs in sorted(names.items())
        )


@dataclass(frozen=True)
class Decision:
    state: State
    reason: str | None
    # Changes first observed or revised this Turn; future metrics consume these facts.
    detected: tuple[Pending, ...] = ()
    applied: tuple[Pending, ...] = ()


def _file_tier(path: str, *, global_file: bool) -> int:
    """The user's global file is 1; a Bot's own memory file, ``~/.avibe/bots/<handle>/AGENTS.md``, is the Bot's own
    writing, 3 (bots.md D11); every other file, the shared ``~/.avibe/bots/AGENTS.md`` included, is 2."""
    if global_file:
        return 1
    from config import paths

    bots = os.path.realpath(paths.get_bots_dir())
    return 3 if os.path.dirname(os.path.dirname(os.path.realpath(path))) == bots else 2


def component_tier(before: Component | None, after: Component | None) -> int:
    component = after or before
    if component.path:
        return COMPONENT_TIERS.get(component.id, _file_tier(component.path, global_file=component.global_file))
    if (before is not None and after is not None and component.id in COMPONENT_TIERS
            and before.contract_revision != after.contract_revision):
        return 1
    if component.id in OPTIONAL_MODULES and (before is None or after is None):
        return 2
    return COMPONENT_TIERS.get(component.id, 2)


def decide(
    previous: State | None, candidate: Prefix, *, now: float, route: tuple[str, ...],
    checkpoint_seq: int, cold: bool,
) -> Decision:
    """Replace the whole prefix, never individual components; continuous edits keep their deadline."""
    if previous is None:
        return Decision(State(Snapshot(candidate, now, route, checkpoint_seq), observed_checkpoint_seq=checkpoint_seq), "session_start")
    if previous.snapshot.route == route and previous.snapshot.prefix.revision == candidate.revision:
        return Decision(replace(previous, pending=(), observed_checkpoint_seq=checkpoint_seq), None)
    old = {item.id: item for item in previous.snapshot.prefix.components}
    fresh = {item.id: item for item in candidate.components}
    prior = {item.component: item for item in previous.pending}
    pending = []
    for key in dict.fromkeys([*old, *fresh]):
        before, after = old.get(key), fresh.get(key)
        if before is not None and after is not None and before.content == after.content:
            continue
        component = after or before
        rev = after.revision if after is not None else revision(None)
        own = bool(after and after.path and after.source_revision
                   and previous.written.get(after.path) == after.source_revision)
        tier = component_tier(before, after)
        earlier = prior.get(key)
        # Authorship controls the notice only; it cannot restart a component's deadline.
        since = earlier.pending_since if earlier else now
        pending.append(Pending(key, component.name, rev, tier, since, own))
    shared = old.keys() & fresh.keys()
    if [key for key in old if key in shared] != [key for key in fresh if key in shared]:
        pending.append(Pending("prefix-order", "prompt-order", revision(list(fresh)), 1, now))
    pending = tuple(pending)
    detected = tuple(item for item in pending if prior.get(item.component) != item)
    reason = None
    if previous.snapshot.route != route or native_revoked(previous.snapshot.prefix.hosted, candidate.hosted):
        reason = "route_change"
    elif any(item.tier == 1 for item in pending):
        reason = "tier1"
    elif pending and cold:
        reason = "cold"
    elif pending and checkpoint_seq > previous.observed_checkpoint_seq:
        reason = "checkpoint"
    elif any(item.tier == 2 and now - item.pending_since >= DEADLINE_S for item in pending):
        reason = "deadline"
    if reason is not None:
        return Decision(State(Snapshot(candidate, now, route, checkpoint_seq), observed_checkpoint_seq=checkpoint_seq), reason, detected, pending)
    return Decision(replace(previous, pending=pending, observed_checkpoint_seq=checkpoint_seq), None, detected)


def route_identity(endpoint: ModelEndpoint, *, agent: str = "", model: str = "", effort: str = "") -> tuple[str, ...]:
    return (endpoint.provider, endpoint.protocol, endpoint.model_id, endpoint.source_id or "",
            endpoint.source_identity or "", agent, model, effort)


def _number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid prompt timestamp")
    return float(value)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid prompt text")
    return value


def _strings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("invalid prompt mapping")
    return {_text(key): _text(item) for key, item in value.items()}


def candidate_prefix(
    system: str, tools: Sequence[Tool], endpoint: ModelEndpoint, blocks: Sequence[Any],
    files: Sequence[Any], *, builtin_agent: bool, rg: bool, catalog: Mapping[str, Any] | None = None,
) -> Prefix:
    from core.prompt_registry import prompt_module
    from modules.agents.vibey import instructions
    from modules.agents.vibey.prompt import coding_prompt, preamble, system_prompt

    rendered: dict[str, str] = {}
    instructions.project_context(files, components=rendered)
    names = [tool.spec.name for tool in tools]
    hosted_names = {*endpoint.hosted_tools, "image_generate"}
    specs = tuple(tool.spec for tool in tools)
    hosted = hosted_declarations(endpoint)
    components = [
        Component("agent-identity", preamble(builtin_agent)),
        Component("coding-rules", coding_prompt(names, builtin_agent=False, rg=False)),
        Component("prefix-contract", system_prompt((), "<modules>", builtin_agent=False, rg=False, project_context="<instructions>")),
        Component("tool-environment", str(rg)),
        Component("instruction-contract", "\n".join((instructions.HEADER, instructions.PRECEDENCE, instructions.SUBTREE,
                                                       instructions.TRUNCATED, instructions.NOT_LOADED,
                                                       str(instructions.LIMIT), str(instructions.OMITTED_ROOM)))),
        Component("local-tools", json.dumps([asdict(tool) for tool in specs if tool.name not in hosted_names])),
        Component("hosted-tools", json.dumps([hosted, [asdict(tool) for tool in specs if tool.name in hosted_names]])),
        *(Component("file:" + file.path, rendered.get(file.path, ""), file.path, file.is_global, file.source_revision) for file in files),
        *(Component(block.module_id, block.text, contract_revision=revision(prompt_module(block.module_id).source())) for block in blocks),
    ]
    return Prefix(system, tuple(components), specs, hosted, catalog)


def hosted_declarations(endpoint: ModelEndpoint) -> dict[str, Any]:
    return {name: {"mode": tool.mode, "declaration": tool.declaration, "include": list(tool.include)}
            for name, tool in endpoint.hosted_tools.items()}


def native_revoked(saved: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    """A cached native tool executes upstream: losing its live route cannot be deferred."""
    return any(raw["mode"] == "native" and live.get(name, {}).get("mode") != "native" for name, raw in saved.items())


def frozen_hosted(endpoint: ModelEndpoint, saved: Mapping[str, Any]) -> dict[str, HostedToolRoute]:
    """Keep the declared prefix, but never undo a live provider refusal or recovery decision."""
    routes = {}
    for name, raw in saved.items():
        live = endpoint.hosted_tools.get(name)
        if raw["mode"] == "native" and (live is None or live.mode != "native"):
            # A declaration is not an execution grant. This also covers a fresh
            # resolution inside the Turn, where the system snapshot cannot change.
            if live is not None:
                routes[name] = live
            continue
        if live is not None and (live.refused or live.route_switch):
            routes[name] = live
        else:
            routes[name] = HostedToolRoute(
                mode=raw["mode"], declaration=raw["declaration"], include=tuple(raw["include"]),
                replay=live.replay if live is not None else False,
            )
    return routes


class FrozenTool:
    """A saved declaration with the live executor; the snapshot grants no execution authority."""

    def __init__(self, spec: ToolSpec, tool: Tool | None) -> None:
        self.spec, self._tool = spec, tool
        self.concurrent = getattr(tool, "concurrent", False)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        if self._tool is None:
            return ToolResult((TextBlock(f"Tool {self.spec.name} is no longer available; check the current configuration."),), True)
        return await self._tool.execute(arguments, ctx)


def frozen_tools(saved: Sequence[ToolSpec], live: Sequence[Tool]) -> tuple[Tool, ...]:
    executors = {tool.spec.name: tool for tool in live}
    return tuple(FrozenTool(spec, executors.get(spec.name)) for spec in saved)


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def directory(self, session_id: str) -> Path:
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise ValueError("invalid snapshot Session id")
        return self.root / "prompts" / session_id

    def load(self, session_id: str) -> State | None:
        try:
            directory = self.directory(session_id)
            raw = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
            waiting = json.loads((directory / "pending.json").read_text(encoding="utf-8"))
            if raw["version"] != VERSION or waiting["version"] != VERSION:
                raise ValueError("unknown prompt state version")
            data = raw["prefix"]
            components = []
            for item in data["components"]:
                component = Component(**item)
                _text(component.id)
                _text(component.content)
                if component.path is not None:
                    _text(component.path)
                if component.source_revision is not None:
                    _text(component.source_revision)
                if component.contract_revision is not None:
                    _text(component.contract_revision)
                if type(component.global_file) is not bool:
                    raise ValueError("invalid global instruction flag")
                components.append(component)
            if len({item.id for item in components}) != len(components):
                raise ValueError("duplicate prompt component")
            tools = []
            for item in data["tools"]:
                tool = ToolSpec(**item)
                _text(tool.name)
                _text(tool.description)
                if not isinstance(tool.input_schema, dict):
                    raise ValueError("invalid tool schema")
                tools.append(tool)
            if len({item.name for item in tools}) != len(tools):
                raise ValueError("duplicate tool")
            hosted = data["hosted"]
            if not isinstance(hosted, dict):
                raise ValueError("invalid hosted declarations")
            for name, item in hosted.items():
                _text(name)
                if set(item) != {"mode", "declaration", "include"} or not isinstance(item["include"], list):
                    raise ValueError("invalid hosted declaration fields")
                HostedToolRoute(mode=item["mode"], replay=False, declaration=item["declaration"], include=tuple(item["include"]))
            catalog = data.get("catalog")
            if catalog is not None and not isinstance(catalog, dict):
                raise ValueError("invalid prompt catalog observation")
            prefix = Prefix(_text(data["system"]), tuple(components), tuple(tools), hosted, catalog)
            if raw["revision"] != prefix.revision or waiting["snapshot_revision"] != prefix.revision:
                raise ValueError("inconsistent prompt state")
            if not isinstance(raw["route"], list):
                raise ValueError("invalid prompt route")
            route = tuple(_text(item) for item in raw["route"])
            seq = raw["checkpoint_seq"]
            observed_seq = waiting["observed_checkpoint_seq"]
            if any(type(value) is not int or value < 0 for value in (seq, observed_seq)):
                raise ValueError("invalid checkpoint seq")
            pending = []
            for item in waiting["components"]:
                change = Pending(**item)
                for value in (change.component, change.name, change.revision):
                    _text(value)
                if type(change.tier) is not int or change.tier not in (1, 2, 3) or type(change.own) is not bool:
                    raise ValueError("invalid prompt tier")
                _number(change.pending_since)
                pending.append(change)
            return State(Snapshot(prefix, _number(raw["applied_at"]), route, seq), tuple(pending),
                         _strings(waiting["written"]), observed_seq)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            return None

    def save(self, session_id: str, state: State, *, snapshot: bool) -> bool:
        try:
            directory = self.directory(session_id)
            if snapshot:
                self._write(directory / "snapshot.json", {
                    "version": VERSION, **asdict(state.snapshot), "revision": state.snapshot.prefix.revision,
                })
            self._write(directory / "pending.json", {
                "version": VERSION, "snapshot_revision": state.snapshot.prefix.revision,
                "components": [asdict(item) for item in state.pending], "written": dict(state.written),
                "observed_checkpoint_seq": state.observed_checkpoint_seq,
            })
            return True
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            logger.warning("Could not persist optional prompt state for Session %s", session_id)
            return False

    def record_write(self, session_id: str, path: str, revision: str) -> None:
        from modules.agents.vibey.instructions import CANDIDATES

        if Path(path).name not in CANDIDATES:
            return
        state = self.load(session_id)
        if state is not None:
            written = {**state.written, path: revision}
            self.save(session_id, replace(state, written=written), snapshot=False)

    def inherit(self, source: str, target: str) -> None:
        """Copy at reservation, not first use (the source may advance before then)."""
        state = self.load(source)
        if state is None:
            return
        try:
            # Read once more as bytes to preserve the source file exactly; atomic replacement
            # plus the revision link makes a concurrent source update degrade to fresh.
            data = (self.directory(source) / "snapshot.json").read_bytes()
            self._write_bytes(self.directory(target) / "snapshot.json", data)
            self.save(target, replace(state, written={}), snapshot=False)
        except (OSError, ValueError):
            logger.warning("Could not inherit optional prompt snapshot for Session %s", target)

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        SnapshotStore._write_bytes(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".prompt-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
