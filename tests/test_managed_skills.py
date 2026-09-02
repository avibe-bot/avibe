from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import managed_skills
from core.managed_skills import (
    BUILTIN_SKILLS_SNAPSHOT_ENV,
    CATALOG_DESCRIPTION_MAX_CHARS,
    CATALOG_PAGE_MAX_BYTES,
    CATALOG_PAGE_SIZE,
    SKILL_BODY_MAX_BYTES,
    SKILL_WORKING_DIR_ENV,
    load_skill,
    managed_skill_environment,
    parse_skill_file,
    publish_builtin_skills,
    render_skill_catalog_prompt,
    render_skill_content,
    render_skill_list,
    resolve_accessible_skills,
    resolve_skills,
    snapshot_tree_digest,
)
from core.system_prompt_injection import build_system_prompt_injection
from modules.im import MessageContext


def _write_skill(
    root: Path,
    directory: str,
    name: str,
    description: str,
    body: str = "Body\n",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return skill_file


def _isolated_resolve(cwd: Path, tmp_path: Path, **kwargs):
    return resolve_skills(
        cwd,
        home=kwargs.pop("home", tmp_path / "home"),
        avibe_home=kwargs.pop("avibe_home", tmp_path / "avibe-home"),
        codex_home=kwargs.pop("codex_home", tmp_path / "codex-home"),
        claude_home=kwargs.pop("claude_home", tmp_path / "claude-home"),
        xdg_config_home=kwargs.pop("xdg_config_home", tmp_path / "xdg-home"),
        builtin_snapshot_id=kwargs.pop("builtin_snapshot_id", ""),
        **kwargs,
    )


def _isolate_live_commands(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AVIBE_HOME", str(home / ".avibe"))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    return home, cwd


def test_loose_parser_requires_only_portable_name_and_description(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "unknown: [this is not valid yaml\n"
        "name: declared-name\n"
        "description: >\n"
        "  First line.\n"
        "  Second line.\n"
        "---\n"
        "# Instructions\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "declared-name"
    assert skill.description == "First line. Second line."
    assert skill.body == "# Instructions\n"


def test_loose_parser_ignores_nested_required_field_names(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "metadata:\n"
        "  name: nested-name\n"
        "  description: Nested description\n"
        "name: top-level-name\n"
        "description: Top-level description\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "top-level-name"
    assert skill.description == "Top-level description"


@pytest.mark.parametrize(
    "name",
    ["", "Uppercase", "two words", "-leading", "trailing-", "two--hyphens", "shell;word"],
)
def test_parser_omits_nonportable_names(tmp_path: Path, name: str) -> None:
    skill_file = _write_skill(tmp_path, "candidate", name, "Description")
    assert parse_skill_file(skill_file, priority=(1, 0, 1)) is None


def test_resolution_applies_all_precedence_dimensions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    avibe_home = home / ".avibe"
    project = tmp_path / "project"
    nested = project / "packages" / "app"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    snapshot_id = "a" * 64
    builtin_root = avibe_home / "builtin-skills" / snapshot_id

    _write_skill(home / ".agents" / "skills", "shared", "shared", "global agents")
    _write_skill(home / ".codex" / "skills", "global-family", "global-family", "codex")
    global_agents = _write_skill(home / ".agents" / "skills", "global-family", "global-family", "agents")
    _write_skill(project / ".agents" / "skills", "depth", "depth", "project root")
    nearest = _write_skill(nested / ".agents" / "skills", "depth", "depth", "nearest")
    _write_skill(nested / ".claude" / "skills", "family", "family", "claude")
    project_agents = _write_skill(nested / ".agents" / "skills", "family", "family", "agents")
    builtin = _write_skill(builtin_root, "shared", "shared", "builtin")

    skills = resolve_skills(
        nested,
        home=home,
        avibe_home=avibe_home,
        codex_home=home / ".codex",
        claude_home=home / ".claude",
        xdg_config_home=home / ".config",
        builtin_snapshot_id=snapshot_id,
    )
    by_name = {skill.name: skill for skill in skills}

    assert list(by_name) == sorted(by_name)
    assert by_name["shared"].directory == builtin.parent.resolve()
    assert by_name["depth"].directory == nearest.parent.resolve()
    assert by_name["family"].directory == project_agents.parent.resolve()
    assert by_name["global-family"].directory == global_agents.parent.resolve()


def test_global_roots_honor_backend_home_overrides(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    codex_home = tmp_path / "custom-codex"
    claude_home = tmp_path / "custom-claude"
    xdg_home = tmp_path / "custom-xdg"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")

    _write_skill(codex_home / "skills", "codex", "codex", "Codex")
    _write_skill(claude_home / "skills", "claude", "claude", "Claude")
    _write_skill(xdg_home / "opencode" / "skills", "opencode", "opencode", "OpenCode")

    assert [skill.name for skill in resolve_skills(cwd)] == ["claude", "codex", "opencode"]


def test_codex_system_skills_are_a_low_priority_global_compatibility_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    codex_home = home / ".codex"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")

    user = _write_skill(home / ".agents" / "skills", "shared", "shared", "User")
    _write_skill(codex_home / "skills" / ".system", "shared", "shared", "System")
    system = _write_skill(codex_home / "skills" / ".system", "imagegen", "imagegen", "System")

    by_name = {skill.name: skill for skill in resolve_skills(cwd)}

    assert by_name["shared"].directory == user.parent.resolve()
    assert by_name["imagegen"].directory == system.parent.resolve()


def test_codex_system_container_does_not_consume_the_user_root_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    codex_home = home / ".codex"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    monkeypatch.setattr(managed_skills, "DISCOVERY_ROOT_MAX_CHILDREN", 1)

    _write_skill(codex_home / "skills", "user-skill", "user-skill", "User")
    _write_skill(codex_home / "skills" / ".system", "system-skill", "system-skill", "System")

    assert [skill.name for skill in resolve_skills(cwd)] == ["system-skill", "user-skill"]


def test_empty_backend_home_overrides_fall_back_to_default_roots(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")

    _write_skill(home / ".codex" / "skills", "codex", "codex", "Codex")
    _write_skill(home / ".config" / "opencode" / "skills", "opencode", "opencode", "OpenCode")

    assert [skill.name for skill in resolve_skills(cwd)] == ["codex", "opencode"]


def test_final_path_tie_breaker_is_independent_of_enumeration_order(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = cwd / ".agents" / "skills"
    first = _write_skill(root, "a-dir", "same", "First")
    _write_skill(root, "z-dir", "same", "Last")
    original = managed_skills._root_children

    def reversed_children(path: Path, **kwargs):
        children = original(path, **kwargs)
        return list(reversed(children)) if children else children

    monkeypatch.setattr(managed_skills, "_root_children", reversed_children)
    resolved = _isolated_resolve(cwd, tmp_path)

    assert len(resolved) == 1
    assert resolved[0].directory == first.parent.resolve()


def test_reserved_root_and_non_git_ancestors_are_not_scanned(tmp_path: Path) -> None:
    cwd = tmp_path / "parent" / "child"
    cwd.mkdir(parents=True)
    _write_skill(tmp_path / "avibe-home" / "skills", "reserved", "reserved", "Reserved")
    _write_skill(tmp_path / "parent" / ".agents" / "skills", "parent", "parent", "Parent")
    _write_skill(cwd / ".agents" / "skills", "local", "local", "Local")

    assert [skill.name for skill in _isolated_resolve(cwd, tmp_path)] == ["local"]


def test_project_root_ascent_is_bounded(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    cwd = project / "a" / "b" / "c"
    cwd.mkdir(parents=True)
    (project / ".git").mkdir()
    _write_skill(project / ".agents" / "skills", "root", "root", "Root")
    _write_skill(cwd / ".agents" / "skills", "local", "local", "Local")
    monkeypatch.setattr(managed_skills, "PROJECT_ROOT_MAX_DIRECTORIES", 3)

    assert [skill.name for skill in _isolated_resolve(cwd, tmp_path)] == ["local"]


def test_catalog_paginates_stably_without_exposing_directories(tmp_path: Path) -> None:
    skills = []
    for index in reversed(range(CATALOG_PAGE_SIZE + 1)):
        skill_file = _write_skill(
            tmp_path / "skills",
            f"skill-{index:02d}",
            f"skill-{index:02d}",
            f"Description {index}",
        )
        skill = parse_skill_file(skill_file, priority=(1, 0, 1))
        assert skill is not None
        skills.append(skill)
    skills.sort(key=lambda item: item.name)

    prompt = render_skill_catalog_prompt(skills)

    assert prompt.count("\n- skill-") == CATALOG_PAGE_SIZE
    assert "`vibe skill list --page 2`" in prompt
    assert render_skill_list(skills, page=2) == "- skill-25: Description 25"
    assert str(tmp_path) not in prompt
    assert render_skill_catalog_prompt([]) == ""


def test_catalog_bounds_descriptions_and_row_bytes(tmp_path: Path) -> None:
    skills = []
    for index in range(CATALOG_PAGE_SIZE):
        skill_file = _write_skill(
            tmp_path,
            f"skill-{index:02d}",
            f"skill-{index:02d}",
            "界" * (CATALOG_DESCRIPTION_MAX_CHARS + 50),
        )
        skill = parse_skill_file(skill_file, priority=(1, 0, 1))
        assert skill is not None
        assert len(skill.description) == CATALOG_DESCRIPTION_MAX_CHARS
        assert skill.description.endswith("...")
        skills.append(skill)

    rows, notice = render_skill_list(skills).rsplit("\n", 1)
    assert len(rows.encode("utf-8")) <= CATALOG_PAGE_MAX_BYTES
    assert notice == "More skills are available. Run `vibe skill list --page 2` to view more."


def test_system_prompt_catalog_reflects_add_edit_and_delete_each_render(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)

    before = build_system_prompt_injection(skills_cwd=cwd)
    skill_file = _write_skill(cwd / ".agents" / "skills", "live", "live", "First")
    after_install = build_system_prompt_injection(skills_cwd=cwd)
    skill_file.write_text(
        "---\nname: live\ndescription: Updated\n---\nUpdated body\n",
        encoding="utf-8",
    )
    after_update = build_system_prompt_injection(skills_cwd=cwd)
    skill_file.unlink()
    after_delete = build_system_prompt_injection(skills_cwd=cwd)

    assert "## Skills" not in before
    assert "- live: First" in after_install
    assert "- live: Updated" in after_update
    assert "## Skills" not in after_delete


def test_system_prompt_catalog_forwards_remote_acl_context(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    resource_context = {
        "sub": "member-1",
        "instance_role": "editor",
        "is_remote": True,
    }
    context = MessageContext(
        user_id="remote:member-1",
        channel_id="workbench",
        platform="avibe",
        platform_specific={
            "agent_session_id": "sess-runtime",
            "message_metadata": {"resource_user_context": resource_context},
        },
    )
    captured = {}

    def resolve(catalog_cwd, *, backend, user_context):
        captured.update(cwd=catalog_cwd, backend=backend, user_context=user_context)
        return []

    monkeypatch.setattr(managed_skills, "resolve_accessible_skills", resolve)

    build_system_prompt_injection(
        context=context,
        fallback_platform="avibe",
        current_agent_backend="codex",
        skills_cwd=cwd,
    )

    assert captured == {
        "cwd": cwd,
        "backend": "codex",
        "user_context": resource_context,
    }


def test_remote_acl_identity_uses_the_registered_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    working_directory = repository / "registered-subdirectory"
    working_directory.mkdir(parents=True)
    (repository / ".git").mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AVIBE_HOME", str(home / ".avibe"))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    _write_skill(working_directory / ".agents" / "skills", "project", "project", "Project")
    captured = {}

    def filter_names(rows, *, backend, project_dir, user_context):
        captured.update(project_dir=project_dir, rows=rows)
        return {"project"}

    monkeypatch.setattr(
        "core.services.skills.filter_accessible_runtime_skill_names",
        filter_names,
    )

    skills = resolve_accessible_skills(
        working_directory,
        backend="codex",
        user_context={"sub": "member-1"},
    )

    assert [skill.name for skill in skills] == ["project"]
    assert captured["project_dir"] == str(working_directory.resolve())


def test_load_emits_body_only_and_agent_accessible_directory(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    skill_file = _write_skill(
        cwd / ".agents" / "skills",
        "docs",
        "docs",
        "Description",
        "Use references/guide.md",
    )
    reference = skill_file.parent / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text("Reference", encoding="utf-8")

    skill = load_skill("docs", cwd)

    assert skill is not None
    assert (skill.directory / "references" / "guide.md").read_text() == "Reference"
    assert render_skill_content(skill) == (
        f'<skill_content name="docs" directory="{skill_file.parent.resolve()}">\n'
        "Use references/guide.md</skill_content>"
    )


def test_load_escapes_directory_attribute_controls(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    working_directory = cwd / 'line\nbreak"&'
    working_directory.mkdir()
    _write_skill(working_directory / ".agents" / "skills", "demo", "demo", "Demo")

    skill = load_skill("demo", working_directory)

    assert skill is not None
    output = render_skill_content(skill)
    assert "line&#xA;break&quot;&amp;" in output
    assert output.count("\n") == 1 + skill.body.count("\n")


def test_load_reparses_the_selected_file_and_rejects_a_changed_name(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    skill_file = _write_skill(cwd / ".agents" / "skills", "docs", "docs", "Description")
    winner = resolve_skills(cwd)[0]
    skill_file.write_text(
        "---\nname: renamed\ndescription: Changed\n---\nNew body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_skills, "resolve_skills", lambda _cwd=None: [winner])

    assert load_skill("docs", cwd) is None


def test_load_does_not_fall_through_to_a_new_unadvertised_winner(tmp_path: Path, monkeypatch) -> None:
    home, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    project_file = _write_skill(cwd / ".agents" / "skills", "docs", "docs", "Project")
    _write_skill(home / ".agents" / "skills", "docs", "docs", "Global")
    advertised = resolve_skills(cwd)
    project_file.unlink()

    assert load_skill("docs", cwd, resolved_skills=advertised) is None


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot rename an open directory fixture")
def test_load_rejects_a_replaced_directory_before_output(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    skill_file = _write_skill(cwd / ".agents" / "skills", "docs", "docs", "Description")
    original_read = managed_skills._read_skill_path
    replaced = False

    def replace_after_read(*args, **kwargs):
        nonlocal replaced
        result = original_read(*args, **kwargs)
        if kwargs.get("include_body") and not replaced:
            replaced = True
            skill_file.parent.rename(skill_file.parent.with_name("docs-old"))
            _write_skill(skill_file.parent.parent, "docs", "docs", "Replacement")
        return result

    monkeypatch.setattr(managed_skills, "_read_skill_path", replace_after_read)

    assert load_skill("docs", cwd) is None


@pytest.mark.skipif(os.name == "nt", reason="FIFO fixtures are POSIX-only")
def test_fifo_candidate_cannot_block_discovery_or_load(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    fifo = cwd / ".agents" / "skills" / "fifo" / "SKILL.md"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    assert _isolated_resolve(cwd, tmp_path) == []


def test_verified_read_rejects_a_file_changed_during_read(tmp_path: Path, monkeypatch) -> None:
    skill_file = _write_skill(tmp_path, "docs", "docs", "Description")
    original = managed_skills._stat_token
    calls = 0

    def changed_token(value):
        nonlocal calls
        calls += 1
        token = original(value)
        return (*token[:-1], token[-1] + (1 if calls == 2 else 0))

    monkeypatch.setattr(managed_skills, "_stat_token", changed_token)
    assert parse_skill_file(skill_file, priority=(1, 0, 1)) is None


def test_frontmatter_body_and_root_limits_omit_candidates(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    too_much_frontmatter = cwd / ".agents" / "skills" / "frontmatter" / "SKILL.md"
    too_much_frontmatter.parent.mkdir(parents=True)
    too_much_frontmatter.write_text(
        "---\nname: frontmatter\ndescription: Description\nunknown: "
        + "x" * managed_skills.FRONTMATTER_MAX_BYTES
        + "\n---\nBody\n",
        encoding="utf-8",
    )
    _write_skill(
        cwd / ".agents" / "skills",
        "body",
        "body",
        "Description",
        "x" * (SKILL_BODY_MAX_BYTES + 1),
    )

    assert _isolated_resolve(cwd, tmp_path) == []

    oversized_root = tmp_path / "oversized" / ".agents" / "skills"
    for index in range(managed_skills.DISCOVERY_ROOT_MAX_CHILDREN + 1):
        (oversized_root / f"entry-{index:04d}").mkdir(parents=True)
    assert _isolated_resolve(tmp_path / "oversized", tmp_path) == []


def test_builtin_and_compatibility_inputs_have_independent_budgets(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    avibe_home = tmp_path / "avibe-home"
    snapshot_id = "b" * 64
    _write_skill(avibe_home / "builtin-skills" / snapshot_id, "builtin", "builtin", "Built-in")
    _write_skill(cwd / ".agents" / "skills", "project", "project", "Project")
    _write_skill(tmp_path / "home" / ".agents" / "skills", "global", "global", "Global")
    monkeypatch.setattr(managed_skills, "DISCOVERY_CLASS_MAX_CANDIDATES", 1)

    skills = _isolated_resolve(
        cwd,
        tmp_path,
        avibe_home=avibe_home,
        builtin_snapshot_id=snapshot_id,
    )
    assert [skill.name for skill in skills] == ["builtin", "project"]


def test_snapshot_v1_digest_fixture_is_stable(tmp_path: Path) -> None:
    root = tmp_path / "source"
    skill_file = root / "alpha" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes(b"---\nname: alpha\ndescription: Alpha\n---\nAlpha body\n")
    script = root / "alpha" / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_bytes(b"#!/bin/sh\nprintf alpha\n")
    skill_file.chmod(0o644)
    script.chmod(0o644)

    assert snapshot_tree_digest(root) == "00ee8932422279760a9cbe6a7b4e8ffb57c5a3e9ba3fee5a94c216ce2d72a848"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX executable mode")
def test_mode_only_change_produces_a_new_snapshot_id(tmp_path: Path) -> None:
    root = tmp_path / "source"
    script = _write_skill(root, "alpha", "alpha", "Alpha").parent / "run.sh"
    script.write_text("echo alpha\n", encoding="utf-8")
    script.chmod(0o644)
    first = snapshot_tree_digest(root)
    script.chmod(0o755)
    assert snapshot_tree_digest(root) != first


def test_publication_keeps_complete_versioned_snapshots_and_executable_modes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    old_file = _write_skill(source, "retired", "retired", "Retired", "Old\n")
    helper = old_file.parent / "run.sh"
    helper.write_text("echo old\n", encoding="utf-8")
    helper.chmod(0o755)

    old_id = publish_builtin_skills(source_root=source, destination_root=destination)
    old_snapshot = destination / old_id
    assert (old_snapshot / "retired" / "SKILL.md").is_file()
    if os.name != "nt":
        assert (old_snapshot / "retired" / "run.sh").stat().st_mode & 0o111

    for path in sorted((source / "retired").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (source / "retired").rmdir()
    _write_skill(source, "current", "current", "Current", "New\n")
    new_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert new_id != old_id
    assert (old_snapshot / "retired" / "SKILL.md").is_file()
    assert not (destination / new_id / "retired").exists()
    assert (destination / new_id / "current" / "SKILL.md").is_file()


def test_publication_ignores_install_generated_python_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "alpha", "alpha", "Alpha")
    cache = source / "alpha" / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "helper.cpython-312.pyc").write_bytes(b"generated")
    destination = tmp_path / "runtime"

    snapshot_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert not (destination / snapshot_id / "alpha" / "scripts" / "__pycache__").exists()


def test_concurrent_publication_reuses_one_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    _write_skill(source, "alpha", "alpha", "Alpha")

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(
            pool.map(
                lambda _: publish_builtin_skills(
                    source_root=source,
                    destination_root=destination,
                ),
                range(2),
            )
        )

    assert ids[0] == ids[1]
    assert (destination / ids[0] / "alpha" / "SKILL.md").is_file()


def test_publication_rejects_nonportable_and_wrong_type_paths(tmp_path: Path) -> None:
    destination = tmp_path / "builtin-skills"
    invalid_source = tmp_path / "invalid"
    _write_skill(invalid_source, "CON", "con-skill", "Invalid path")
    with pytest.raises(RuntimeError, match="Windows-reserved"):
        publish_builtin_skills(source_root=invalid_source, destination_root=destination)

    valid_source = tmp_path / "valid"
    _write_skill(valid_source, "alpha", "alpha", "Alpha")
    snapshot_id = snapshot_tree_digest(valid_source)
    destination.mkdir(parents=True)
    (destination / snapshot_id).write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a directory|unavailable"):
        publish_builtin_skills(source_root=valid_source, destination_root=destination)


def test_publication_rejects_builtins_outside_runtime_catalog_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "builtin-skills"

    too_many_entries = tmp_path / "too-many-entries"
    _write_skill(too_many_entries, "alpha", "alpha", "Alpha")
    (too_many_entries / "README.md").write_text("extra\n", encoding="utf-8")
    monkeypatch.setattr(managed_skills, "DISCOVERY_ROOT_MAX_CHILDREN", 1)
    with pytest.raises(RuntimeError, match="at most"):
        publish_builtin_skills(source_root=too_many_entries, destination_root=destination)

    oversized_frontmatter = tmp_path / "oversized-frontmatter" / "alpha" / "SKILL.md"
    oversized_frontmatter.parent.mkdir(parents=True)
    oversized_frontmatter.write_text(
        "---\nname: alpha\ndescription: Alpha\nunknown: "
        + "x" * managed_skills.FRONTMATTER_MAX_BYTES
        + "\n---\nBody\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="invalid"):
        publish_builtin_skills(
            source_root=oversized_frontmatter.parents[1],
            destination_root=destination,
        )

    oversized_body = tmp_path / "oversized-body"
    _write_skill(
        oversized_body,
        "alpha",
        "alpha",
        "Alpha",
        "x" * (SKILL_BODY_MAX_BYTES + 1),
    )
    with pytest.raises(RuntimeError, match="invalid"):
        publish_builtin_skills(source_root=oversized_body, destination_root=destination)


def test_builtin_source_ignores_an_unrelated_top_level_skills_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    site_packages = tmp_path / "site-packages"
    fake_core = site_packages / "core"
    fake_core.mkdir(parents=True)
    unrelated = site_packages / "skills"
    unrelated.mkdir()
    (site_packages / "pyproject.toml").write_text(
        '[tool.unrelated]\nname = "avibe-os"\n',
        encoding="utf-8",
    )
    package = site_packages / "vibe"
    packaged_source = package / "builtin_skills_source"
    packaged_source.mkdir(parents=True)
    init_file = package / "__init__.py"
    init_file.write_text("", encoding="utf-8")

    import vibe

    monkeypatch.setattr(managed_skills, "__file__", str(fake_core / "managed_skills.py"))
    monkeypatch.setattr(vibe, "__file__", str(init_file))

    assert managed_skills.builtin_skills_source() == packaged_source


def test_remote_acl_failure_hides_compatibility_skills_but_keeps_builtins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    avibe_home = home / ".avibe"
    snapshot_id = "d" * 64
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, snapshot_id)
    _write_skill(avibe_home / "builtin-skills" / snapshot_id, "builtin", "builtin", "Built-in")
    _write_skill(cwd / ".agents" / "skills", "project", "project", "Project")
    monkeypatch.setattr(
        "core.services.skills.filter_accessible_runtime_skill_names",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    skills = resolve_accessible_skills(
        cwd,
        backend="codex",
        user_context={},
    )

    assert [skill.name for skill in skills] == ["builtin"]


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture requires POSIX semantics")
def test_publication_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    (skill_file.parent / "linked").symlink_to(skill_file)

    with pytest.raises(RuntimeError, match="not a directory or regular file"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "out")


def test_bound_working_directory_and_snapshot_are_inherited(monkeypatch, tmp_path: Path) -> None:
    _, advertised_cwd = _isolate_live_commands(monkeypatch, tmp_path)
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    _write_skill(advertised_cwd / ".agents" / "skills", "advertised", "advertised", "Here")
    _write_skill(other_cwd / ".agents" / "skills", "other", "other", "There")
    snapshot_id = "c" * 64
    monkeypatch.setenv(SKILL_WORKING_DIR_ENV, str(advertised_cwd))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, snapshot_id)
    monkeypatch.chdir(other_cwd)

    assert [skill.name for skill in resolve_skills()] == ["advertised"]
    assert managed_skill_environment(advertised_cwd) == {
        SKILL_WORKING_DIR_ENV: str(advertised_cwd.resolve()),
        BUILTIN_SKILLS_SNAPSHOT_ENV: snapshot_id,
    }
