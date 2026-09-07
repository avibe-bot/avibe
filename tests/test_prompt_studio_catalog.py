from __future__ import annotations

import json
import re
from pathlib import Path

from core.managed_skills import parse_skill_file, publish_builtin_skills
from core.prompt_registry import (
    PROMPT_MODULES,
    RenderedPromptBlock,
    export_prompt_catalog,
    order_prompt_blocks,
    prompt_module,
    render_prompt,
)
from core.prompt_studio_catalog import (
    PROMPT_STUDIO_CATALOG_SCHEMA,
    _markdown_blocks,
    _skill_documents,
    export_prompt_studio_catalog,
)
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]


def test_every_prompt_markdown_file_has_one_stably_ordered_registry_entry() -> None:
    registered = [module.filename for module in PROMPT_MODULES]
    present = sorted(path.name for path in (ROOT / "core" / "prompts").glob("*.md"))

    assert len(registered) == len(set(registered))
    assert sorted(registered) == present
    ids = [module.id for module in PROMPT_MODULES]
    assert ids[ids.index("base-capabilities-body") + 1] == "skills-prompt"
    assert ids.index("skills-catalog") > ids.index("memory-context-prompt")
    assert ids.index("skills-catalog-heading") < ids.index("skills-pagination-prompt") < ids.index("skills-catalog")


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


def test_production_order_follows_the_catalog_without_changing_block_bytes() -> None:
    blocks = [RenderedPromptBlock(module.id, f"\n{module.id} exact bytes\n") for module in PROMPT_MODULES]
    unordered = list(reversed(blocks))
    ordered = order_prompt_blocks(unordered)
    assert unordered == list(reversed(blocks))
    assert ordered == blocks
    assert all(actual is expected for actual, expected in zip(ordered, blocks))


def test_studio_catalog_contains_runtime_modules_and_builtin_skills() -> None:
    catalog = export_prompt_studio_catalog()

    assert catalog["schema"] == PROMPT_STUDIO_CATALOG_SCHEMA
    assert catalog == export_prompt_studio_catalog()
    runtime, *documents = catalog["documents"]
    skills = [document for document in documents if "parent_id" not in document]
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


def test_reference_documents_are_source_addressable_and_grouped_with_their_skill() -> None:
    documents = export_prompt_studio_catalog()["documents"]
    references = [document for document in documents if document.get("parent_id") == "skill-use-avibe"]
    files = sorted((ROOT / "skills/use-avibe/references").rglob("*.md"))
    assert [document["source_path"] for document in references] == [path.relative_to(ROOT).as_posix() for path in files]
    assert len(references) == 7
    start = next(index for index, document in enumerate(documents) if document["id"] == "skill-use-avibe")
    assert documents[start + 1:start + 1 + len(references)] == references
    ids = [document["id"] for document in documents]
    for document in documents:
        ids.extend(block["id"] for block in document["blocks"])
        if document.get("parent_id"):
            lines = (ROOT / document["source_path"]).read_text(encoding="utf-8").splitlines()
            for block in document["blocks"]:
                assert block["source_path"] == document["source_path"]
                assert "\n".join(lines[block["source_line"] - 1:]).startswith(block["source"])
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", identity) for identity in ids)


def test_reference_edits_additions_and_deletions_do_not_rekey_other_documents(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.prompt_studio_catalog.builtin_skills_source", lambda: tmp_path)
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: example\ndescription: Fixture\n---\n# Entry\nRead references.\n")
    references = directory / "references"
    references.mkdir()
    nested = references / "nested"
    nested.mkdir()
    file = nested / "detail.md"
    file.write_text("# Details\n\nFirst version.\n\n```sh\n# Not a heading\n```\n")
    (references / "ignored.txt").write_text("Not Markdown")
    (directory / "scripts").mkdir()
    (directory / "scripts/example.md").write_text("Not a reference")
    initial = _skill_documents()
    assert len(initial) == 2
    assert len(initial[1]["blocks"]) == 1
    file.write_text("# Details\n\nSecond version.\n")
    (references / "earlier.md").write_text("# Details\n\nAnother file.\n")
    edited = _skill_documents()
    assert edited[0] == initial[0]
    assert edited[2]["id"] == initial[1]["id"]
    assert edited[2]["blocks"][0]["id"] == initial[1]["blocks"][0]["id"]
    assert edited[2]["revision"] != initial[1]["revision"]
    assert edited[1]["blocks"][0]["id"] != edited[2]["blocks"][0]["id"]
    file.unlink()
    assert _skill_documents() == edited[:2]


def test_reference_export_does_not_follow_symbolic_links(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.prompt_studio_catalog.builtin_skills_source", lambda: tmp_path / "skills")
    directory = tmp_path / "skills/example"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: example\ndescription: Fixture\n---\n# Entry\n")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.md").write_text("Private file")
    references = directory / "references"
    references.symlink_to(outside, target_is_directory=True)
    assert len(_skill_documents()) == 1
    references.unlink()
    references.mkdir()
    (references / "directory-link").symlink_to(outside, target_is_directory=True)
    (references / "file-link.md").symlink_to(outside / "secret.md")
    assert len(_skill_documents()) == 1


def test_use_avibe_entry_routes_to_all_bundled_references() -> None:
    directory = ROOT / "skills/use-avibe"
    entry = (directory / "SKILL.md").read_text(encoding="utf-8")
    assert len(entry.splitlines()) < 150
    targets = re.findall(r"\]\((references/[^)]+\.md)\)", entry)
    assert set(targets) == {path.relative_to(directory).as_posix() for path in (directory / "references").glob("*.md")}
    assert all((directory / target).is_file() for target in targets)
    assert (directory / "scripts/vibe_api.py").is_file()
    for rule in ("preserve every existing channel", "missing user fields are not a patch", "Do not restart", "Treat secrets as opaque"):
        assert rule in entry


def test_published_skill_keeps_references_available_without_eagerly_loading_them(tmp_path) -> None:
    destination = tmp_path / "snapshots"
    snapshot = publish_builtin_skills(source_root=ROOT / "skills", destination_root=destination)
    directory = destination / snapshot / "use-avibe"
    skill = parse_skill_file(directory / "SKILL.md", priority=(0, 0, 0, str(directory)), include_body=True)
    assert skill is not None
    assert "references/global-config.md" in skill.body
    assert '"cloudflared_path"' not in skill.body
    for source in (ROOT / "skills/use-avibe/references").glob("*.md"):
        assert (directory / "references" / source.name).read_bytes() == source.read_bytes()


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


def test_show_history_is_source_addressable_only_in_its_skill() -> None:
    catalog = export_prompt_studio_catalog()
    runtime = next(document for document in catalog["documents"] if document["id"] == "runtime-core")
    assert not any("show-history" in block["id"] for block in runtime["blocks"])
    show = next(document for document in catalog["documents"] if document.get("name") == "use-show-pages")
    history = next(block for block in show["blocks"] if block["heading"] == "Show Page workspace history")
    assert history["source_path"] == "skills/use-show-pages/SKILL.md"
    assert "history.mode" in history["source"]
    assert "--git-dir=<shadow-git-dir>" in history["source"]


def test_debug_prompt_export_cli_contract(capsys) -> None:
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--format", "json"])

    assert args.command == "debug"
    assert args.debug_command == "prompt"
    assert args.prompt_debug_command == "export"
    assert cli.cmd_debug_prompt(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == PROMPT_STUDIO_CATALOG_SCHEMA
    assert payload["documents"][0]["id"] == "runtime-core"
