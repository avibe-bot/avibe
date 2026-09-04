from __future__ import annotations

import json
from pathlib import Path

from core.prompt_registry import PROMPT_MODULES, export_prompt_catalog, prompt_module, render_prompt
from core.prompt_studio_catalog import (
    PROMPT_STUDIO_CATALOG_SCHEMA,
    _markdown_blocks,
    export_prompt_studio_catalog,
)
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]


def test_every_prompt_markdown_file_has_one_stably_ordered_registry_entry() -> None:
    registered = [module.filename for module in PROMPT_MODULES]
    present = sorted(path.name for path in (ROOT / "core" / "prompts").glob("*.md"))

    assert len(registered) == len(set(registered))
    assert sorted(registered) == present
    assert [module.id for module in PROMPT_MODULES[:3]] == [
        "base-capabilities-intro",
        "base-capabilities-body",
        "codex-generated-images",
    ]


def test_prompt_rendering_replaces_only_declared_placeholders() -> None:
    rendered = render_prompt(
        "preferences-context-prompt",
        preferences_path="`/tmp/preferences.md`",
        platform="avibe",
    )

    assert "`/tmp/preferences.md`" in rendered
    assert "`avibe/<user_id>`" in rendered
    assert prompt_module("memory-context-prompt").source().find("{0,62}") >= 0


def test_prompt_catalog_export_is_deterministic_and_source_addressable() -> None:
    first = export_prompt_catalog()
    second = export_prompt_catalog()

    assert first == second
    assert len(first["modules"]) == len(PROMPT_MODULES)
    assert [module["order"] for module in first["modules"]] == list(range(len(PROMPT_MODULES)))
    assert "tool-policy-relaxed-section" not in {
        module["id"] for module in first["modules"]
    }
    for module in first["modules"]:
        assert (ROOT / module["source_path"]).read_text(encoding="utf-8") == module["source"] + "\n"


def test_studio_catalog_contains_runtime_modules_and_builtin_skills() -> None:
    catalog = export_prompt_studio_catalog()

    assert catalog["schema"] == PROMPT_STUDIO_CATALOG_SCHEMA
    assert catalog == export_prompt_studio_catalog()
    runtime, *skills = catalog["documents"]
    assert runtime["id"] == "runtime-core"
    assert [block["id"] for block in runtime["blocks"]] == [
        f"runtime-{module.id}" for module in PROMPT_MODULES
    ]
    assert [document["name"] for document in skills] == sorted(
        (document["name"] for document in skills), key=lambda name: (name.casefold(), name)
    )
    assert {document["name"] for document in skills} == {
        "background-watch-hook",
        "use-avibe",
        "use-avibe-harness",
        "use-avibe-vault",
        "use-show-pages",
    }


def test_skill_sections_ignore_heading_like_lines_inside_code_fences() -> None:
    blocks = _markdown_blocks(
        "# Real\n\n```sh\n# shell comment\n```\n\n## Also real\n",
        id_prefix="example",
        first_line=8,
        source_path="skills/example/SKILL.md",
    )

    assert [block["heading"] for block in blocks] == ["Real", "Also real"]
    assert [block["id"] for block in blocks] == ["example-real-1", "example-also-real-3"]
    assert blocks[0]["source_line"] == 8
    assert blocks[1]["source_line"] == 14


def test_debug_prompt_export_cli_contract(capsys) -> None:
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--format", "json"])

    assert args.command == "debug"
    assert args.debug_command == "prompt"
    assert args.prompt_debug_command == "export"
    assert cli.cmd_debug_prompt(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == PROMPT_STUDIO_CATALOG_SCHEMA
    assert payload["documents"][0]["id"] == "runtime-core"
