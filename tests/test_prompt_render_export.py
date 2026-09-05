from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from config import paths
from core import managed_skills, show_git
from core.managed_skills import ManagedSkill
from core.prompt_registry import PROMPT_MODULES, prompt_text
from core.prompt_studio_catalog import export_prompt_studio_catalog, render_prompt_context
from core.system_prompt_injection import build_system_prompt_injection
from modules.agents.codex.agent import CodexAgent
from modules.im import MessageContext
from vibe import cli


def _inputs(backend="codex", memory=True, history="managed", skill_mode="pages"):
    options = {
        "context": {
            "user_id": "reviewer",
            "channel_id": "review",
            "platform": "avibe",
            "platform_specific": {
                "agent_session_id": "sesreview",
                "agent_session_target": {"native_session_fork": {"source_session_id": "sessource"}},
            },
        },
        "memory_enabled": memory,
        "include_codex_generated_images": backend == "codex",
        "skills_cwd": "/fixture/project",
        "enabled_agents": [
            {"name": "zeta", "backend": "claude", "description": "Last"},
            {"name": "alpha", "backend": "codex", "description": "First"},
        ],
    }
    return {"backend": backend, "agent_instructions": "Custom {unchanged} instructions", "options": options}


def _environment(monkeypatch, history="managed", skill_mode="pages"):
    monkeypatch.setenv("CODEX_HOME", "/fixture/codex")
    monkeypatch.setattr(paths, "get_user_preferences_path", lambda: Path("/fixture/preferences.md"))
    monkeypatch.setattr(show_git, "show_git_checkpointing_active", lambda: history != "off")
    monkeypatch.setattr(show_git, "_workspace_is_self_managed", lambda _: history == "self-managed")
    names = [f"skill-{index:02}" for index in range(26)] + ["use-avibe-vault"]
    skills = [
        ManagedSkill(name, f"Description {name}", Path("/fixture") / name, (0,), disable_model_invocation=skill_mode == "manual")
        for name in names
    ] if skill_mode != "empty" else []
    monkeypatch.setattr(managed_skills, "resolve_skills", lambda *_args, **_kwargs: skills)


@pytest.mark.parametrize("backend,memory,history,skill_mode", itertools.product(
    ("claude", "codex", "opencode"), (False, True), ("off", "managed", "self-managed"), ("empty", "manual", "pages"),
))
def test_export_reconstructs_production_text_with_source_for_every_block(monkeypatch, backend, memory, history, skill_mode):
    _environment(monkeypatch, history, skill_mode)
    request = _inputs(backend, memory, history, skill_mode)
    result = render_prompt_context(request)
    options = dict(request["options"])
    options["context"] = MessageContext(**options["context"])
    production = request["agent_instructions"] + "\n\n" + build_system_prompt_injection(**options)
    if backend == "codex":
        production = CodexAgent._render_developer_prompt_snapshot(production)

    assert result["text"] == production
    assert "".join(block["text"] for block in result["blocks"]) == production
    principles = prompt_text("agent-working-principles")
    assert production.count(principles) == 1
    assert production.index(principles) < production.index(prompt_text("base-capabilities-body"))
    assert [block["text"] for block in result["blocks"] if block["id"] == "agent-working-principles"] == [principles]
    catalog = {module.id: module for module in PROMPT_MODULES}
    for block in result["blocks"]:
        assert block["source_path"] == catalog[block["id"]].source_path
    assert result == render_prompt_context(request)
    assert request["options"]["context"]["platform"] == "avibe"
    if skill_mode == "pages":
        assert "vibe skill load -- <name>" in production
        assert "vibe skill list --page 2" in production
        assert "- skill-00: Description skill-00" in production
    assert ("## Personal Memory" in production) == memory
    assert ("preferences.md" in production) != memory


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_working_principles_remain_without_session_skills_or_optional_capabilities(backend):
    request = {
        "backend": backend,
        "options": {
            "include_quick_replies": False,
            "include_show_pages": False,
            "include_codex_generated_images": False,
            "include_context_guidance": False,
        },
    }
    rendered = render_prompt_context(request)
    blocks = rendered["blocks"]
    if backend == "codex":
        blocks = blocks[1:-1]
    assert [block["id"] for block in blocks] == [
        "base-capabilities-intro", "agent-working-principles", "base-capabilities-body",
    ]
    assert blocks[1]["text"] == prompt_text("agent-working-principles")
    assert rendered == render_prompt_context(request)


def test_exported_sources_cover_all_rendered_branches(monkeypatch):
    visited = set()
    for backend, memory, history, skill_mode in itertools.product(
        ("claude", "codex", "opencode"), (False, True), ("off", "managed", "self-managed"), ("empty", "manual", "pages"),
    ):
        _environment(monkeypatch, history, skill_mode)
        result = render_prompt_context(_inputs(backend, memory, history, skill_mode))
        visited.update(block["id"] for block in result["blocks"])
    assert visited == {module.id for module in PROMPT_MODULES}


@pytest.mark.parametrize("history", ["managed", "self-managed"])
def test_history_and_pagination_keep_their_own_source_provenance(monkeypatch, history):
    _environment(monkeypatch, history=history)
    rendered = render_prompt_context(_inputs(history=history))
    blocks = {block["id"]: block for block in rendered["blocks"]}
    assert blocks["show-history-heading"]["text"] == prompt_text("show-history-heading")
    assert "History contract:" not in blocks[f"show-history-{history}"]["text"]
    rows = "\n".join(f"- skill-{index:02}: Description skill-{index:02}" for index in range(25))
    assert blocks["skills-catalog"]["text"] == prompt_text("skills-catalog").format(skill_rows=rows)
    assert blocks["skills-more-notice"] == {
        "id": "skills-more-notice",
        "source_path": "core/prompts/skills-more.md",
        "text": "\n" + prompt_text("skills-more-notice").format(next_page=2),
    }
    assert "| Agent Name | Backend | Agent Description |" in prompt_text("harness-agents-prompt")


def test_rendered_revision_changes_only_when_rendered_bytes_change(monkeypatch):
    _environment(monkeypatch)
    request = _inputs()
    before = render_prompt_context(request)
    request["options"]["enabled_agents"].reverse()
    assert render_prompt_context(request) == before
    request["options"]["context"]["message_id"] = "a-different-turn"
    assert render_prompt_context(request) == before
    request["agent_instructions"] = "Revised instructions"
    assert render_prompt_context(request)["revision"] != before["revision"]


def test_debug_cli_exports_both_sources_and_production_blocks(monkeypatch, tmp_path, capsys):
    _environment(monkeypatch)
    source = export_prompt_studio_catalog()
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(_inputs()), encoding="utf-8")
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["documents"] == source["documents"]
    assert exported["catalog_revision"] == source["catalog_revision"]
    assert exported["rendered"]["scope"] == "avibe-injection"
    assert "{skill_rows}" not in exported["rendered"]["text"]
    assert "{enabled_agents_rows}" not in exported["rendered"]["text"]


def test_real_cli_discovers_updated_skill_and_exports_its_rendered_row(monkeypatch, tmp_path):
    for name, value in vars(managed_skills).items():
        if name.endswith("_ENV") and isinstance(value, str):
            monkeypatch.delenv(value, raising=False)
    project = tmp_path / "project"
    skill = project / ".agents" / "skills" / "review-example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({
        "backend": "opencode",
        "options": {"skills_cwd": str(project), "include_context_guidance": False},
    }), encoding="utf-8")
    command = [sys.executable, "-c", "from vibe.cli import main; main()", "debug", "prompt", "export", "--context-file", str(context_file)]

    def exported_row():
        payload = json.loads(subprocess.check_output(command, text=True, cwd=Path(__file__).resolve().parents[1]))
        rendered = payload["rendered"]
        assert "".join(block["text"] for block in rendered["blocks"]) == rendered["text"]
        return rendered

    skill.write_text("---\nname: review-example\ndescription: First revision\n---\nBody only on load.\n", encoding="utf-8")
    first = exported_row()
    assert "- review-example: First revision" in first["text"]
    assert "Body only on load." not in first["text"]
    skill.write_text("---\nname: review-example\ndescription: Second revision\n---\nBody only on load.\n", encoding="utf-8")
    second = exported_row()
    assert "- review-example: Second revision" in second["text"]
    assert second["revision"] != first["revision"]


@pytest.mark.parametrize("payload", [[], None, {"backend": "unknown"}, {"backend": "codex", "options": {"unknown": True}}])
def test_debug_cli_rejects_invalid_context_without_partial_json(tmp_path, capsys, payload):
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(payload), encoding="utf-8")
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("options,field,kind", [
    ({"context": {"user_id": "u", "channel_id": "c", "platform_specific": [1]}}, "options.context.platform_specific", "invalidField"),
    ({"context": {"user_id": "u", "channel_id": "c", "platform_specific": "text"}}, "options.context.platform_specific", "invalidField"),
    ({"context": {"user_id": "u", "channel_id": "c", "unknown": 1}}, "options.context.unknown", "unknownField"),
    ({"context": {"user_id": "u"}}, "options.context.channel_id", "invalidField"),
    ({"memory_enabled": "false"}, "options.memory_enabled", "invalidField"),
    ({"include_show_pages": []}, "options.include_show_pages", "invalidField"),
    ({"enabled_agents": 123}, "options.enabled_agents", "invalidField"),
    ({"unknown": True}, "options.unknown", "unknownField"),
])
def test_cli_localizes_validation_before_rendering(monkeypatch, tmp_path, capsys, language, options, field, kind):
    def unexpected_render(*_args, **_kwargs):
        pytest.fail("Invalid input reached production rendering")

    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(managed_skills, "resolve_skills", unexpected_render)
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({
        "backend": "codex", "options": {"skills_cwd": "/fixture/project", **options},
    }), encoding="utf-8")
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    error = cli.i18n_t(f"debug.cli.error.{kind}", language, field=field)
    assert output.err == cli.i18n_t("debug.cli.error.promptExport", language, error=error) + "\n"


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("agents,location", [
    ("codex", ""), ("", ""), ({"name": "codex"}, ""), ({}, ""),
    (True, ""), (123, ""), (1.5, ""),
    (["codex"], ".0"), ([None], ".0"), ([42], ".0"), ([[]], ".0"),
    ([{}], ".0"), ([{"description": "Code review"}], ".0"),
    ([{"name": "   "}], ".0"), ([{"name": "!!!"}], ".0"),
    ([{"name": "codex"}, {}], ".1"),
])
def test_cli_rejects_invalid_json_agent_inputs(monkeypatch, tmp_path, capsys, language, agents, location):
    def unexpected_discovery(*_args, **_kwargs):
        pytest.fail("Invalid Agent input reached production rendering")

    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    monkeypatch.setattr(managed_skills, "resolve_skills", unexpected_discovery)
    request = _inputs()
    request["options"]["enabled_agents"] = agents
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(request), encoding="utf-8")
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    error = cli.i18n_t("debug.cli.error.invalidField", language, field=f"options.enabled_agents{location}")
    assert output.err == cli.i18n_t("debug.cli.error.promptExport", language, error=error) + "\n"


@pytest.mark.parametrize("agents,expected_rows", [
    (None, []), ([], []),
    ([{"name": "codex"}], ["| codex | unknown | (no description) |"]),
    ([{"normalized_name": "Custom_Agent", "description": "Review"}], ["| Custom_Agent | unknown | Review |"]),
    ([{"name": "zeta"}, {"name": "alpha", "backend": "codex"}], [
        "| alpha | codex | (no description) |", "| zeta | unknown | (no description) |",
    ]),
])
def test_cli_renders_valid_json_agent_arrays(monkeypatch, tmp_path, capsys, agents, expected_rows):
    _environment(monkeypatch)
    request = _inputs()
    request["options"]["enabled_agents"] = agents
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(request), encoding="utf-8")
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 0
    output = capsys.readouterr()
    assert output.err == ""
    rendered = json.loads(output.out)["rendered"]
    tables = [block for block in rendered["blocks"] if block["id"] == "harness-agents-prompt"]
    assert bool(tables) == bool(expected_rows)
    if tables:
        rows = [line for line in tables[0]["text"].splitlines() if line.startswith("| ")][2:]
        assert rows == expected_rows
    omitted = _inputs()
    del omitted["options"]["enabled_agents"]
    if agents is None:
        assert render_prompt_context(omitted) == rendered


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("content,kind", [
    (b"{", "invalidJson"), (b"\xff", "invalidJson"), (None, "unreadableContext"),
    (b"null", "invalidContext"), (b'{}', "invalidBackend"),
    (b'{"backend":"codex","extra":1}', "invalidContext"),
])
def test_cli_localizes_invalid_context_files(monkeypatch, tmp_path, capsys, language, content, kind):
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    context_file = tmp_path / "context.json"
    if content is not None:
        context_file.write_bytes(content)
    args = cli.build_parser().parse_args(["debug", "prompt", "export", "--context-file", str(context_file)])
    assert cli.cmd_debug_prompt(args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    error = cli.i18n_t(f"debug.cli.error.{kind}", language)
    assert output.err == cli.i18n_t("debug.cli.error.promptExport", language, error=error) + "\n"


def test_working_principles_addition_preserves_all_other_injection_bytes(monkeypatch):
    outputs = []
    for backend, memory, history, skill_mode in itertools.product(
        ("claude", "codex", "opencode"), (False, True), ("off", "managed", "self-managed"), ("empty", "manual", "pages"),
    ):
        _environment(monkeypatch, history, skill_mode)
        rendered = render_prompt_context(_inputs(backend, memory, history, skill_mode))["text"]
        principles = prompt_text("agent-working-principles")
        assert rendered.count(principles) == 1
        outputs.append(rendered.replace(principles, "", 1))
    digest = hashlib.sha256(json.dumps(outputs, ensure_ascii=False).encode()).hexdigest()
    # Captured from the pre-migration renderer at 928924dd82bb48c7eea67ff06ddd844d442cb2a1.
    # Only the approved working-principles block may differ from that baseline.
    assert digest == "adbf608248e0b0a03d6646f57d31816cdb520c86113225e281b2aa007b100bc5"
