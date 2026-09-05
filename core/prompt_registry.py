"""Stable registry and export contract for Avibe-authored prompt modules."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


PROMPT_CATALOG_SCHEMA = "avibe-prompt-catalog/1"
_PROMPT_ROOT = Path(__file__).with_name("prompts")
_NAMED_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class PromptModule:
    """One author-maintained Markdown module and its composition boundary."""

    id: str
    title: str
    filename: str
    leading_newlines: int = 0
    trailing_newlines: int = 0
    placeholders: tuple[str, ...] = ()

    @property
    def source_path(self) -> str:
        return f"core/prompts/{self.filename}"

    def source(self) -> str:
        return _read_prompt_source(self.filename)

    def text(self) -> str:
        return "\n" * self.leading_newlines + self.source() + "\n" * self.trailing_newlines

    def render(self, values: Mapping[str, object]) -> str:
        missing = [name for name in self.placeholders if name not in values]
        unexpected = sorted(set(values) - set(self.placeholders))
        if missing or unexpected:
            raise ValueError(
                f"Prompt module {self.id!r} values do not match its placeholders: "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
        template = self.text()
        authored = set(_NAMED_PLACEHOLDER_PATTERN.findall(template))
        if authored != set(self.placeholders):
            raise RuntimeError(
                f"Prompt module {self.id!r} source does not match its declared placeholders: "
                f"authored={sorted(authored)!r}, declared={sorted(self.placeholders)!r}"
            )
        return _NAMED_PLACEHOLDER_PATTERN.sub(lambda match: str(values[match.group(1)]), template)

    def export(self, *, order: int) -> dict[str, Any]:
        source = self.source()
        stable = {
            "id": self.id,
            "title": self.title,
            "source_path": self.source_path,
            "source": source,
            "leading_newlines": self.leading_newlines,
            "trailing_newlines": self.trailing_newlines,
            "placeholders": list(self.placeholders),
        }
        revision = _stable_digest(stable)
        return {**stable, "order": order, "revision": revision}


# Tuple order is the public catalog order. It changes only when the authored
# prompt structure changes, never because filesystem enumeration changed.
PROMPT_MODULES: tuple[PromptModule, ...] = (
    PromptModule("base-capabilities-intro", "Base Capabilities Intro", "base-capabilities-intro.md", trailing_newlines=2),
    PromptModule("agent-working-principles", "Agent Working Principles", "agent-working-principles.md", trailing_newlines=2),
    PromptModule("base-capabilities-body", "Base Capabilities Body", "base-capabilities.md", trailing_newlines=1),
    PromptModule("codex-generated-images", "Codex-generated images", "codex-generated-images.md", leading_newlines=1, trailing_newlines=1, placeholders=("example_uri",)),
    PromptModule("show-pages-prompt", "Show Pages Prompt", "show-pages.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("vault-routing-prompt", "Vault Routing Prompt", "vault.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("harness-routing-prompt", "Harness Routing Prompt", "harness.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("harness-agents-prompt", "Harness Agents Prompt", "harness-agents.md", placeholders=("enabled_agents_rows",)),
    PromptModule("session-start-prompt", "Session Start Prompt", "session-start.md", trailing_newlines=2, placeholders=("default_session_id",)),
    PromptModule("forked-session-prompt", "Forked Session Prompt", "forked-session.md", trailing_newlines=2, placeholders=("source_session_id", "default_session_id")),
    PromptModule("quick-replies-prompt", "Quick Replies Prompt", "quick-replies.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("preferences-context-prompt", "Preferences and Project Context Prompt", "preferences-context.md", leading_newlines=1, trailing_newlines=1, placeholders=("preferences_path", "platform")),
    PromptModule("memory-context-prompt", "Memory and Project Context Prompt", "memory-context.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("session-title-prompt", "Session Title Prompt", "session-title.md", leading_newlines=1, trailing_newlines=1),
    PromptModule("skills-prompt", "Skills Usage", "skills.md", leading_newlines=2, trailing_newlines=2),
    PromptModule("skills-manual-prompt", "Explicit Skill Requests", "skills-manual.md", leading_newlines=2),
    PromptModule("skills-pagination-prompt", "Skills Discovery Pagination", "skills-pagination.md", trailing_newlines=1),
    PromptModule("skills-more-notice", "Skills Next Page", "skills-more.md", placeholders=("next_page",)),
    PromptModule("skills-catalog", "Available Skills", "skills-catalog.md", placeholders=("skill_rows",)),
    PromptModule("show-history-managed", "Show History: Managed Workspace", "show-history-managed.md"),
    PromptModule("show-history-self-managed", "Show History: User Repository", "show-history-self-managed.md"),
    PromptModule("runtime-snapshot-open", "Codex Runtime Snapshot Start", "runtime-snapshot-open.md", trailing_newlines=2),
    PromptModule("runtime-snapshot-close", "Codex Runtime Snapshot End", "runtime-snapshot-close.md", leading_newlines=1),
    PromptModule("agent-instructions", "Agent Custom Instructions", "agent-instructions.md", trailing_newlines=2, placeholders=("agent_instructions",)),
    PromptModule("show-history-heading", "Show History Heading", "show-history-heading.md", leading_newlines=1, trailing_newlines=1),
)

_MODULES_BY_ID = {module.id: module for module in PROMPT_MODULES}
if len(_MODULES_BY_ID) != len(PROMPT_MODULES):  # pragma: no cover - import-time invariant
    raise RuntimeError("Avibe prompt module ids must be unique")
if len({module.filename for module in PROMPT_MODULES}) != len(PROMPT_MODULES):  # pragma: no cover
    raise RuntimeError("Avibe prompt module files must be unique")


@lru_cache(maxsize=None)
def _read_prompt_source(filename: str) -> str:
    path = _PROMPT_ROOT / filename
    source = path.read_text(encoding="utf-8")
    if "\x00" in source:
        raise RuntimeError(f"Prompt module contains a NUL byte: {path}")
    if not source.endswith("\n"):
        raise RuntimeError(f"Prompt module must end with one file newline: {path}")
    return source[:-1]


def prompt_module(module_id: str) -> PromptModule:
    try:
        return _MODULES_BY_ID[module_id]
    except KeyError as exc:
        raise KeyError(f"Unknown Avibe prompt module: {module_id}") from exc


def prompt_text(module_id: str) -> str:
    return prompt_module(module_id).text()


def render_prompt(module_id: str, **values: object) -> str:
    return prompt_module(module_id).render(values)


@dataclass(frozen=True)
class RenderedPromptBlock:
    """One production-rendered block, addressable by the source catalog."""

    module_id: str
    text: str

    def export(self) -> dict[str, str]:
        module = prompt_module(self.module_id)
        return {"id": self.module_id, "source_path": module.source_path, "text": self.text}


def render_prompt_block(module_id: str, **values: object) -> RenderedPromptBlock:
    return RenderedPromptBlock(module_id, render_prompt(module_id, **values))


def join_prompt_blocks(blocks: list[RenderedPromptBlock]) -> str:
    return "".join(block.text for block in blocks)


def runtime_snapshot_blocks(blocks: list[RenderedPromptBlock]) -> list[RenderedPromptBlock]:
    return [render_prompt_block("runtime-snapshot-open"), *blocks, render_prompt_block("runtime-snapshot-close")]


def export_prompt_catalog() -> dict[str, Any]:
    modules = [module.export(order=index) for index, module in enumerate(PROMPT_MODULES)]
    catalog_revision = _stable_digest({"schema": PROMPT_CATALOG_SCHEMA, "modules": modules})
    return {
        "schema": PROMPT_CATALOG_SCHEMA,
        "catalog_revision": catalog_revision,
        "modules": modules,
    }


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
