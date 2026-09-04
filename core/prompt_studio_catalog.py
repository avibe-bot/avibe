"""Machine-readable Prompt Studio catalog built from authoritative sources."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from markdown_it import MarkdownIt

from core.managed_skills import builtin_skills_source, parse_skill_file
from core.prompt_registry import export_prompt_catalog


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


def export_prompt_studio_catalog() -> dict[str, Any]:
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
    return {
        "schema": PROMPT_STUDIO_CATALOG_SCHEMA,
        "catalog_revision": _stable_digest({"schema": PROMPT_STUDIO_CATALOG_SCHEMA, "documents": documents}),
        "documents": documents,
    }
