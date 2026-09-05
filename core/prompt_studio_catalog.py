"""Machine-readable Prompt Studio catalog built from authoritative sources."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from typing import Any, get_type_hints

from markdown_it import MarkdownIt
from pydantic import TypeAdapter, ValidationError

from core.managed_skills import builtin_skills_source, parse_skill_file
from core.prompt_registry import export_prompt_catalog, join_prompt_blocks, render_prompt_block, runtime_snapshot_blocks


PROMPT_STUDIO_CATALOG_SCHEMA = "avibe-prompt-studio-catalog/1"
_MARKDOWN = MarkdownIt("commonmark")
_LEGACY_HEADING_PATTERN = re.compile(r"(?m)^(#{1,3})[ \t]+(.+?)[ \t]*$")


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _split_frontmatter(raw: str) -> tuple[str, str, int]:
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return "", raw, 1
    offset = len(lines[0])
    for index, line in enumerate(lines[1:], start=1):
        offset += len(line)
        if line.rstrip("\r\n") == "---":
            return raw[:offset].rstrip("\r\n"), raw[offset:], index + 2
    return "", raw, 1


def _block_id(prefix: str, slug: str, index: int) -> str:
    suffix = f"-{index}"
    available = max(1, 200 - len(prefix) - len(suffix) - 1)
    return f"{prefix}-{slug[:available]}{suffix}"


def _heading_spans(body: str) -> list[tuple[int, str, int]]:
    line_offsets = [0]
    for line in body.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))
    legacy_order = {match.start(): index for index, match in enumerate(_LEGACY_HEADING_PATTERN.finditer(body), start=1)}
    headings: list[tuple[int, str, int]] = []
    tokens = _MARKDOWN.parse(body)
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag not in {"h1", "h2", "h3"} or token.map is None:
            continue
        inline = tokens[index + 1] if index + 1 < len(tokens) else None
        title = inline.content.strip() if inline is not None and inline.type == "inline" else token.tag.upper()
        start = line_offsets[token.map[0]]
        legacy_index = legacy_order.get(start, len(legacy_order) + len(headings) + 1)
        headings.append((start, title, legacy_index))
    return headings


def _markdown_blocks(
    body: str,
    *,
    id_prefix: str,
    first_line: int,
    source_path: str,
) -> list[dict[str, Any]]:
    headings = _heading_spans(body)
    blocks: list[dict[str, Any]] = []
    preface = body[: headings[0][0]] if headings else body
    if preface.strip():
        blocks.append(
            {
                "id": f"{id_prefix}-preface" if headings else f"{id_prefix}-body",
                "heading": "Preface" if headings else "Body",
                "source": preface.strip(),
                "source_line": first_line,
                "source_path": source_path,
            }
        )
    for position, (start, title, legacy_index) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(body)
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or str(legacy_index)
        blocks.append(
            {
                "id": _block_id(id_prefix, slug, legacy_index),
                "heading": title,
                "source": body[start:end].strip(),
                "source_line": first_line + body[:start].count("\n"),
                "source_path": source_path,
            }
        )
    return blocks


def _skill_documents() -> list[dict[str, Any]]:
    root = builtin_skills_source()
    documents: list[dict[str, Any]] = []
    for skill_file in sorted(root.glob("*/SKILL.md"), key=lambda path: (path.parent.name.casefold(), path.parent.name)):
        skill = parse_skill_file(skill_file, priority=(0, 0, 0, str(skill_file.parent)), include_body=True)
        if skill is None:
            raise RuntimeError(f"Invalid built-in Skill: {skill_file.parent.name}")
        raw = skill_file.read_text(encoding="utf-8")
        logical_path = f"skills/{skill_file.parent.name}/SKILL.md"
        frontmatter, body, first_body_line = _split_frontmatter(raw)
        id_prefix = skill.name
        blocks: list[dict[str, Any]] = []
        if frontmatter:
            blocks.append(
                {
                    "id": f"{id_prefix}-frontmatter",
                    "heading": "Frontmatter",
                    "source": frontmatter,
                    "source_line": 1,
                    "source_path": logical_path,
                }
            )
        blocks.extend(
            _markdown_blocks(
                body,
                id_prefix=id_prefix,
                first_line=first_body_line,
                source_path=logical_path,
            )
        )
        documents.append(
            {
                "id": f"skill-{skill.name}",
                "name": skill.name,
                "kind": "skill",
                "source_path": logical_path,
                "description": skill.description,
                "revision": _digest_text(raw),
                "blocks": blocks,
            }
        )
    return documents


class PromptRenderInputError(ValueError):
    """Localized at the CLI boundary; never expose validator exception prose."""

    def __init__(self, key: str, *, field: str = ""):
        super().__init__(key)
        self.key = key
        self.field = field


def _render_options(raw: object) -> dict[str, Any]:
    from core.system_prompt_injection import _coerce_agent_prompt_info, build_system_prompt_blocks
    from modules.im import MessageContext

    if not isinstance(raw, dict):
        raise PromptRenderInputError("invalidField", field="options")
    parameters = inspect.signature(build_system_prompt_blocks).parameters
    types = get_type_hints(build_system_prompt_blocks)
    # Python callers can supply domain objects and arbitrary iterables. JSON
    # callers supply an array of named objects, never strings or object keys.
    types["enabled_agents"] = list[dict[str, Any]] | None
    options: dict[str, Any] = {}
    for name, value in raw.items():
        field = f"options.{name}"
        if name not in parameters:
            raise PromptRenderInputError("unknownField", field=field)
        if name == "context" and isinstance(value, dict):
            unknown = set(value) - set(MessageContext.__dataclass_fields__)
            if unknown:
                raise PromptRenderInputError("unknownField", field=f"{field}.{sorted(unknown)[0]}")
        try:
            # Validate from JSON so dataclasses and paths accept their JSON
            # representation while scalars retain strict bool/string types.
            options[name] = TypeAdapter(types[name]).validate_json(json.dumps(value), strict=True)
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.errors()[0]["loc"])
            raise PromptRenderInputError("invalidField", field=f"{field}.{location}" if location else field) from exc
        if name == "enabled_agents":
            for index, agent in enumerate(options[name] or []):
                try:
                    _coerce_agent_prompt_info(agent)
                except ValueError as exc:
                    raise PromptRenderInputError("invalidField", field=f"{field}.{index}") from exc
    return options


def render_prompt_context(request: dict[str, Any]) -> dict[str, Any]:
    """Render explicit builder inputs, not a guessed or recorded native session.

    No backend is started and no Memory admission/configuration is performed.
    Skill discovery, when requested through skills_cwd, uses the production
    resolver and its ordinary built-in snapshot maintenance.
    """
    from core.system_prompt_injection import build_system_prompt_blocks

    if not isinstance(request, dict) or set(request) - {"backend", "agent_instructions", "options"}:
        raise PromptRenderInputError("invalidContext")
    backend = request.get("backend")
    if backend not in ("claude", "codex", "opencode"):
        raise PromptRenderInputError("invalidBackend")
    agent_instructions = request.get("agent_instructions", "")
    if not isinstance(agent_instructions, str):
        raise PromptRenderInputError("invalidField", field="agent_instructions")
    options = _render_options(request.get("options", {}))
    options.setdefault("include_codex_generated_images", backend == "codex")
    blocks = build_system_prompt_blocks(**options)
    if agent_instructions:
        blocks.insert(0, render_prompt_block("agent-instructions", agent_instructions=agent_instructions))
    if backend == "codex":
        blocks = runtime_snapshot_blocks(blocks)
    text = join_prompt_blocks(blocks)
    return {
        "backend": backend,
        "scope": "avibe-injection",
        "context_source": "supplied",
        "blocks": [block.export() for block in blocks],
        "text": text,
        "revision": _digest_text(text),
    }


def export_prompt_studio_catalog(*, render_context: dict[str, Any] | None = None) -> dict[str, Any]:
    prompt_catalog = export_prompt_catalog()
    prompt_blocks = []
    for module in prompt_catalog["modules"]:
        prompt_blocks.append(
            {
                "id": f"runtime-{module['id']}",
                "heading": module["title"],
                "source": module["source"],
                "source_line": 1,
                "source_path": module["source_path"],
                "revision": module["revision"],
                "order": module["order"],
                "placeholders": module["placeholders"],
                "leading_newlines": module["leading_newlines"],
                "trailing_newlines": module["trailing_newlines"],
            }
        )
    documents = [
        {
            "id": "runtime-core",
            "name": "Avibe Runtime",
            "kind": "prompt",
            "source_path": "core/prompts",
            "description": "Authoritative Markdown modules composed into Avibe's runtime System Prompt.",
            "revision": prompt_catalog["catalog_revision"],
            "blocks": prompt_blocks,
        },
        *_skill_documents(),
    ]
    result = {
        "schema": PROMPT_STUDIO_CATALOG_SCHEMA,
        "catalog_revision": _stable_digest({"schema": PROMPT_STUDIO_CATALOG_SCHEMA, "documents": documents}),
        "documents": documents,
    }
    if render_context is not None:
        result["rendered"] = render_prompt_context(render_context)
    return result
