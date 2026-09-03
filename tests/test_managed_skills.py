from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import managed_skills
from core.managed_skills import (
    BUILTIN_SKILLS_ROOT_ENV,
    BUILTIN_SKILLS_SNAPSHOT_ENV,
    CATALOG_DESCRIPTION_MAX_CHARS,
    CATALOG_PAGE_MAX_BYTES,
    CATALOG_PAGE_SIZE,
    SKILL_BODY_MAX_BYTES,
    SKILL_CLAUDE_CLI_PATH_ENV,
    SKILL_CLAUDE_HOME_ENV,
    SKILL_CODEX_HOME_ENV,
    SKILL_HOME_ENV,
    SKILL_PROJECT_BASE_ENV,
    SKILL_XDG_CONFIG_HOME_ENV,
    SKILL_WORKING_DIR_ENV,
    load_skill,
    managed_skill_environment,
    parse_skill_file,
    publish_builtin_skills,
    render_skill_catalog_prompt,
    render_skill_content,
    render_skill_list,
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
    monkeypatch.setenv(BUILTIN_SKILLS_ROOT_ENV, "")
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


def test_loose_parser_decodes_quoted_name_escapes_and_plain_description_folding(
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        'name: "format\\u002dcode"\n'
        "description: Formats source files and\n"
        "  applies repository conventions.\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "format-code"
    assert skill.description == "Formats source files and applies repository conventions."


def test_invalid_optional_yaml_type_falls_back_to_required_fields(tmp_path: Path) -> None:
    skill_file = tmp_path / "typed" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "name: typed\n"
        "description: Still portable\n"
        "expires: 2022-99-99\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(0,))

    assert skill is not None
    assert skill.name == "typed"
    assert skill.description == "Still portable"


def test_loose_parser_never_constructs_ignored_yaml_values(tmp_path: Path, monkeypatch) -> None:
    skill_file = tmp_path / "typed" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "name: typed\n"
        "description: Still portable\n"
        "metadata: &recursive [*recursive]\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        managed_skills.yaml,
        "load",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("optional YAML must not be constructed")
        ),
    )

    skill = parse_skill_file(skill_file, priority=(0,))

    assert skill is not None
    assert skill.name == "typed"
    assert skill.description == "Still portable"


def test_loose_parser_accepts_quoted_required_keys(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "\"name\": format-code\n"
        "'description': Formats source files.\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "format-code"
    assert skill.description == "Formats source files."


def test_loose_parser_decodes_escaped_required_keys(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        '"na\\u006de": format-code\n'
        '"descr\\u0069ption": Formats source files.\n'
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "format-code"
    assert skill.description == "Formats source files."


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


def test_loose_parser_accepts_yaml_comments_on_required_scalars(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        '---\nname: formatter # local tools\ndescription: "Format # headings" # shown in catalog\n---\nBody\n',
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "formatter"
    assert skill.description == "Format # headings"


def test_loose_parser_ignores_comments_before_continued_required_scalars(tmp_path: Path) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "unknown: [invalid yaml\n"
        "name:\n"
        "# local name\n"
        "  formatter # callable name\n"
        "description:\n"
        "  # catalog copy\n"
        "  Formats source files. # display text\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.name == "formatter"
    assert skill.description == "Formats source files."


@pytest.mark.parametrize("quote", ["'", '"'])
def test_loose_parser_consumes_multiline_quoted_description(tmp_path: Path, quote: str) -> None:
    skill_file = tmp_path / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "name: formatter\n"
        f"description: {quote}Formats source files and\n"
        f"  applies repository conventions.{quote} # catalog copy\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.description == "Formats source files and applies repository conventions."


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


def test_bound_global_roots_do_not_follow_later_relative_overrides(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    other = tmp_path / "other"
    cwd.mkdir()
    other.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "bound-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bound-claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "bound-xdg"))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    bound = managed_skill_environment(cwd)
    monkeypatch.setenv("CODEX_HOME", "relative-codex")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-claude")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-xdg")
    monkeypatch.chdir(other)
    for key, value in bound.items():
        monkeypatch.setenv(key, value)

    _write_skill(tmp_path / "bound-codex" / "skills", "codex", "codex", "Codex")
    _write_skill(tmp_path / "bound-claude" / "skills", "claude", "claude", "Claude")
    _write_skill(tmp_path / "bound-xdg" / "opencode" / "skills", "opencode", "opencode", "OpenCode")

    assert [skill.name for skill in resolve_skills()] == ["claude", "codex", "opencode"]


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


def test_codex_system_container_counts_toward_the_user_root_enumeration_limit(
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

    assert [skill.name for skill in resolve_skills(cwd)] == ["system-skill"]


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


@pytest.mark.skipif(os.name == "nt", reason="directory symlink fixture requires POSIX semantics")
def test_compatibility_skill_directory_symlink_is_loaded_and_revalidated(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    root = cwd / ".agents" / "skills"
    root.mkdir(parents=True)
    target_file = _write_skill(tmp_path / "shared", "formatter", "formatter", "Format", "Linked\n")
    linked = root / "formatter"
    linked.symlink_to(target_file.parent, target_is_directory=True)

    resolved = _isolated_resolve(cwd, tmp_path)

    assert [skill.name for skill in resolved] == ["formatter"]
    assert resolved[0].directory == target_file.parent.resolve()
    loaded = load_skill("formatter", resolved_skills=resolved)
    assert loaded is not None
    assert loaded.body == "Linked\n"

    target_file.parent.rename(tmp_path / "old-shared")
    _write_skill(tmp_path / "shared", "formatter", "formatter", "Format", "Replacement\n")
    assert load_skill("formatter", resolved_skills=resolved) is None

    linked.unlink()
    assert load_skill("formatter", resolved_skills=resolved) is None


def test_project_root_ascent_is_bounded(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    cwd = project / "a" / "b" / "c"
    cwd.mkdir(parents=True)
    (project / ".git").mkdir()
    _write_skill(project / ".agents" / "skills", "root", "root", "Root")
    _write_skill(cwd / ".agents" / "skills", "local", "local", "Local")
    monkeypatch.setattr(managed_skills, "PROJECT_ROOT_MAX_DIRECTORIES", 3)

    assert [skill.name for skill in _isolated_resolve(cwd, tmp_path)] == ["local"]


def test_bound_project_base_outside_ascent_limit_is_ignored(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    cwd = project / "a" / "b" / "c"
    cwd.mkdir(parents=True)
    _write_skill(project / ".agents" / "skills", "root", "root", "Root")
    _write_skill(cwd / ".agents" / "skills", "local", "local", "Local")
    monkeypatch.setattr(managed_skills, "PROJECT_ROOT_MAX_DIRECTORIES", 3)

    assert [
        skill.name
        for skill in _isolated_resolve(cwd, tmp_path, project_base=project)
    ] == ["local"]


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
    assert "ordinary tasks do not require scanning every page" in prompt
    assert "load that name directly" in prompt
    assert render_skill_list(skills, page=2) == "- skill-25: Description 25"
    assert str(tmp_path) not in prompt
    assert render_skill_catalog_prompt([]) == ""


def test_catalog_rendering_is_byte_stable_for_any_input_order(tmp_path: Path) -> None:
    skills = []
    for name in ("zeta", "alpha", "middle"):
        skill_file = _write_skill(
            tmp_path / "skills",
            name,
            name,
            f"Description for {name}",
        )
        skill = parse_skill_file(skill_file, priority=(1, 0, 1))
        assert skill is not None
        skills.append(skill)

    forward = render_skill_catalog_prompt(skills)
    reverse = render_skill_catalog_prompt(list(reversed(skills)))

    assert forward == reverse
    assert forward.index("- alpha:") < forward.index("- middle:")
    assert forward.index("- middle:") < forward.index("- zeta:")


def test_manual_only_skill_is_loadable_but_not_advertised(tmp_path: Path) -> None:
    skill_file = tmp_path / "manual" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\n"
        "name: manual\n"
        "description: Run only when explicitly requested.\n"
        "disable-model-invocation: true\n"
        "---\n"
        "Manual body\n",
        encoding="utf-8",
    )
    manual = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert manual is not None
    assert manual.disable_model_invocation is True
    assert render_skill_list([manual]) == ""
    manual_prompt = render_skill_catalog_prompt([manual])
    assert "`vibe skill load -- <name>`" in manual_prompt
    assert "- manual:" not in manual_prompt
    assert "Run only when explicitly requested." not in manual_prompt

    loaded = load_skill("manual", resolved_skills=[manual])
    assert loaded is not None
    assert loaded.body == "Manual body\n"
    assert loaded.disable_model_invocation is True


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

    first_page_names = [line.split(":", 1)[0][2:] for line in rows.splitlines()]
    second_page = render_skill_list(skills, page=2)
    assert second_page.splitlines()[0].startswith(
        f"- skill-{len(first_page_names):02d}:"
    )


def test_catalog_neutralizes_yaml_decoded_terminal_controls(tmp_path: Path) -> None:
    skill_file = tmp_path / "controlled" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        '---\nname: controlled\ndescription: "\\u001b[31mred\\u009b0m"\n---\nBody\n',
        encoding="utf-8",
    )

    skill = parse_skill_file(skill_file, priority=(1, 0, 1))

    assert skill is not None
    assert skill.description == "[31mred 0m"
    assert "\x1b" not in render_skill_list([skill])
    assert "\x9b" not in render_skill_list([skill])


def test_system_prompt_catalog_reflects_add_edit_and_delete_each_render(tmp_path: Path, monkeypatch) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)

    before = build_system_prompt_injection(skills_cwd=cwd)
    skill_file = _write_skill(cwd / ".agents" / "skills", "live", "live", "First")
    after_install = build_system_prompt_injection(skills_cwd=cwd)
    skill_file.write_text(
        "---\nname: live\ndescription: First\n---\nBody changed without catalog metadata\n",
        encoding="utf-8",
    )
    after_body_update = build_system_prompt_injection(skills_cwd=cwd)
    skill_file.write_text(
        "---\nname: live\ndescription: Updated\n---\nUpdated body\n",
        encoding="utf-8",
    )
    after_update = build_system_prompt_injection(skills_cwd=cwd)
    skill_file.unlink()
    after_delete = build_system_prompt_injection(skills_cwd=cwd)

    assert "## Skills" not in before
    assert "- live: First" in after_install
    assert after_body_update == after_install
    assert "- live: Updated" in after_update
    assert after_update != after_body_update
    assert "## Skills" not in after_delete


def test_system_prompt_catalog_is_backend_neutral_for_remote_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AVIBE_HOME", str(home / ".avibe"))
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    _write_skill(cwd / ".agents" / "skills", "shared", "shared", "Shared")
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
    prompts = {
        backend: build_system_prompt_injection(
            context=context,
            fallback_platform="avibe",
            current_agent_backend=backend,
            skills_cwd=cwd,
        )
        for backend in ("claude", "codex", "opencode")
    }

    assert all("- shared: Shared" in prompt for prompt in prompts.values())


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


def test_invalid_utf8_body_is_advertised_but_cannot_be_loaded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    skill_file = cwd / ".agents" / "skills" / "binary" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes(
        b"---\nname: binary\ndescription: Invalid text body\n---\n\xff\n"
    )

    assert [skill.name for skill in resolve_skills(cwd)] == ["binary"]
    assert load_skill("binary", cwd) is None


def test_terminal_controls_in_body_are_advertised_but_cannot_be_loaded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    skill_file = cwd / ".agents" / "skills" / "controlled" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes(
        b"---\nname: controlled\ndescription: Controlled body\n---\n"
        b"Allowed tabs\tand newlines\nRejected escape: \x1b[31m\n"
    )

    assert [skill.name for skill in resolve_skills(cwd)] == ["controlled"]
    assert load_skill("controlled", cwd) is None


@pytest.mark.skipif(
    os.name == "nt" or sys.platform == "darwin",
    reason="fixture requires raw-byte filenames supported by the host filesystem",
)
def test_non_utf8_resolved_directory_is_not_advertised(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    root = cwd / ".agents" / "skills"
    root.mkdir(parents=True)
    raw_directory = os.path.join(os.fsencode(root), b"raw-\xff")
    os.mkdir(raw_directory)
    skill_fd = os.open(
        os.path.join(raw_directory, b"SKILL.md"),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(skill_fd, b"---\nname: raw\ndescription: Raw path\n---\nBody\n")
    finally:
        os.close(skill_fd)

    assert resolve_skills(cwd) == []
    assert load_skill("raw", cwd) is None


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


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot replace an open file fixture")
@pytest.mark.parametrize("include_body", [False, True])
def test_verified_read_rejects_an_atomic_path_replacement(
    tmp_path: Path,
    monkeypatch,
    include_body: bool,
) -> None:
    skill_file = _write_skill(tmp_path, "docs", "docs", "Description", "Original\n")
    read_name = "_read_all" if include_body else "_read_prefix"
    original_read = getattr(managed_skills, read_name)

    def replace_after_read(*args, **kwargs):
        data = original_read(*args, **kwargs)
        replacement = skill_file.with_suffix(".replacement")
        replacement.write_text(
            "---\nname: docs\ndescription: Replacement\n---\nReplacement\n",
            encoding="utf-8",
        )
        os.replace(replacement, skill_file)
        return data

    monkeypatch.setattr(managed_skills, read_name, replace_after_read)

    assert parse_skill_file(
        skill_file,
        priority=(1, 0, 1),
        include_body=include_body,
    ) is None


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


def test_aggregate_direct_child_budget_bounds_all_roots(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    root = cwd / ".agents" / "skills"
    root.mkdir(parents=True)
    (root / "README.md").write_text("not a Skill\n", encoding="utf-8")
    (root / "notes.txt").write_text("not a Skill\n", encoding="utf-8")
    _write_skill(tmp_path / "home" / ".agents" / "skills", "global", "global", "Global")
    monkeypatch.setattr(managed_skills, "DISCOVERY_CLASS_MAX_CHILDREN", 2)

    assert _isolated_resolve(cwd, tmp_path) == []


def test_aggregate_direct_child_budget_keeps_the_root_that_exactly_fills_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cwd = tmp_path / "project"
    _write_skill(cwd / ".agents" / "skills", "local", "local", "Local")
    _write_skill(tmp_path / "home" / ".agents" / "skills", "global", "global", "Global")
    monkeypatch.setattr(managed_skills, "DISCOVERY_CLASS_MAX_CHILDREN", 1)

    assert [skill.name for skill in _isolated_resolve(cwd, tmp_path)] == ["local"]


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


@pytest.mark.skipif(os.name == "nt", reason="directory symlink fixture requires POSIX semantics")
def test_compatibility_aliases_share_one_candidate_budget_slot(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    canonical = _write_skill(home / ".agents" / "skills", "shared", "shared", "Shared")
    for root in (
        tmp_path / "codex-home" / "skills",
        tmp_path / "claude-home" / "skills",
        tmp_path / "xdg-home" / "opencode" / "skills",
    ):
        root.mkdir(parents=True)
        (root / "shared").symlink_to(canonical.parent, target_is_directory=True)
    _write_skill(tmp_path / "xdg-home" / "opencode" / "skills", "unique", "unique", "Unique")
    monkeypatch.setattr(managed_skills, "DISCOVERY_CLASS_MAX_CANDIDATES", 2)

    skills = _isolated_resolve(cwd, tmp_path)

    assert [skill.name for skill in skills] == ["shared", "unique"]
    assert skills[0].directory == canonical.parent.resolve()


def test_bound_non_git_project_base_is_discovered_from_descendant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    working_directory = project / "packages" / "app"
    working_directory.mkdir(parents=True)
    _write_skill(project / ".agents" / "skills", "root-skill", "root-skill", "Root")

    assert _isolated_resolve(working_directory, tmp_path) == []
    assert [
        skill.name
        for skill in _isolated_resolve(
            working_directory,
            tmp_path,
            project_base=project,
        )
    ] == ["root-skill"]

    monkeypatch.setenv(SKILL_WORKING_DIR_ENV, str(working_directory))
    monkeypatch.setenv(SKILL_PROJECT_BASE_ENV, str(project))
    assert [
        skill.name
        for skill in _isolated_resolve(
            working_directory,
            tmp_path,
            project_base=None,
        )
    ] == ["root-skill"]


def test_bound_project_base_wins_over_nested_git_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested_checkout = project / "packages" / "app"
    working_directory = nested_checkout / "src"
    working_directory.mkdir(parents=True)
    (nested_checkout / ".git").mkdir()
    _write_skill(project / ".agents" / "skills", "outer", "outer", "Outer")
    _write_skill(nested_checkout / ".agents" / "skills", "inner", "inner", "Inner")

    assert [
        skill.name
        for skill in _isolated_resolve(
            working_directory,
            tmp_path,
            project_base=project,
        )
    ] == ["inner", "outer"]

def test_project_base_outside_working_directory_is_ignored(tmp_path: Path) -> None:
    working_directory = tmp_path / "project" / "child"
    other = tmp_path / "other"
    working_directory.mkdir(parents=True)
    _write_skill(other / ".agents" / "skills", "other", "other", "Other")

    assert _isolated_resolve(
        working_directory,
        tmp_path,
        project_base=other,
    ) == []


def test_distinct_same_name_candidates_keep_precedence_winner(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    _write_skill(cwd / ".codex" / "skills", "shared", "shared", "Codex")
    _write_skill(cwd / ".claude" / "skills", "shared", "shared", "Claude")

    skills = _isolated_resolve(cwd, tmp_path)

    assert len(skills) == 1
    assert skills[0].description == "Codex"


def test_enabled_claude_plugin_skills_join_the_managed_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    claude_home = home / ".claude"
    registry = claude_home / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"version": 2, "plugins": {}}', encoding="utf-8")
    plugin = tmp_path / "plugin-cache" / "formatter"
    _write_skill(plugin / "skills", "format-code", "format-code", "Format code")
    captured = {}

    def plugin_list(command, **kwargs):
        captured.update(command=command, **kwargs)
        return json.dumps(
            [
                {
                    "id": "formatter@example",
                    "enabled": True,
                    "installPath": str(plugin),
                },
                {
                    "id": "disabled@example",
                    "enabled": False,
                    "installPath": str(tmp_path / "disabled"),
                },
            ]
        ).encode()

    monkeypatch.setattr(managed_skills, "_bounded_subprocess_stdout", plugin_list)

    skills = resolve_skills(
        cwd,
        home=home,
        avibe_home=tmp_path / "avibe",
        codex_home=home / ".codex",
        claude_home=claude_home,
        claude_cli_path="/opt/claude-custom",
        xdg_config_home=home / ".config",
        builtin_snapshot_id="",
    )

    assert [skill.name for skill in skills] == ["format-code"]
    assert captured["command"] == [
        "/opt/claude-custom",
        "plugin",
        "list",
        "--json",
    ]
    assert captured["cwd"] == cwd
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(claude_home)
    assert captured["timeout"] == managed_skills.CLAUDE_PLUGIN_LIST_TIMEOUT_SECONDS
    assert captured["max_bytes"] == managed_skills.CLAUDE_PLUGIN_LIST_MAX_BYTES


@pytest.mark.parametrize(
    ("stdout_bytes", "stderr_bytes"),
    [(4097, 0), (0, 4097), (2049, 2049)],
)
def test_bounded_subprocess_capture_rejects_oversized_combined_output(
    tmp_path: Path,
    stdout_bytes: int,
    stderr_bytes: int,
) -> None:
    limit = 4096

    assert managed_skills._bounded_subprocess_stdout(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"os.write(1, b'x' * {stdout_bytes}); "
                f"os.write(2, b'x' * {stderr_bytes})"
            ),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=1,
        max_bytes=limit,
    ) is None


def test_bounded_subprocess_capture_rejects_timeout(tmp_path: Path) -> None:
    assert managed_skills._bounded_subprocess_stdout(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=0.01,
        max_bytes=4096,
    ) is None


def test_claude_plugin_discovery_fails_closed_when_capture_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    claude_home = home / ".claude"
    registry = claude_home / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(managed_skills.shutil, "which", lambda _: "/usr/bin/claude")

    monkeypatch.setattr(managed_skills, "_bounded_subprocess_stdout", lambda *args, **kwargs: None)

    assert resolve_skills(
        cwd,
        home=home,
        avibe_home=tmp_path / "avibe",
        codex_home=home / ".codex",
        claude_home=claude_home,
        xdg_config_home=home / ".config",
        builtin_snapshot_id="",
    ) == []


def test_enabled_claude_plugin_skill_overrides_codex_system_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    claude_home = home / ".claude"
    registry = claude_home / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}", encoding="utf-8")
    plugin = tmp_path / "plugin-cache" / "formatter"
    plugin_skill = _write_skill(plugin / "skills", "shared", "shared", "Plugin")
    _write_skill(home / ".codex" / "skills" / ".system", "shared", "shared", "System")
    payload = json.dumps(
        [{"id": "formatter@example", "enabled": True, "installPath": str(plugin)}]
    ).encode()
    monkeypatch.setattr(managed_skills, "_bounded_subprocess_stdout", lambda *args, **kwargs: payload)

    skills = resolve_skills(
        cwd,
        home=home,
        avibe_home=tmp_path / "avibe",
        codex_home=home / ".codex",
        claude_home=claude_home,
        claude_cli_path="/opt/claude-custom",
        xdg_config_home=home / ".config",
        builtin_snapshot_id="",
    )

    assert len(skills) == 1
    assert skills[0].directory == plugin_skill.parent.resolve()


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


def test_snapshot_digest_charges_file_growth_after_enumeration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_BYTES", 128)
    original_snapshot_entries = managed_skills._snapshot_entries

    def enumerate_then_grow_file(root: Path):
        entries = original_snapshot_entries(root)
        skill_file.write_bytes(b"x" * 129)
        return entries

    monkeypatch.setattr(managed_skills, "_snapshot_entries", enumerate_then_grow_file)

    with pytest.raises(RuntimeError, match="128 bytes"):
        snapshot_tree_digest(source)


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


def test_publication_opens_snapshot_files_in_binary_mode_when_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    _write_skill(source, "alpha", "alpha", "Alpha", "Line one\r\nLine two\n")
    binary_flag = 1 << 29
    original_open = os.open
    destination_flags: list[int] = []

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_CREAT:
            destination_flags.append(flags)
        flags &= ~binary_flag
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(managed_skills.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(managed_skills.os, "open", tracking_open)

    snapshot_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert destination_flags
    assert all(flags & binary_flag for flags in destination_flags)
    assert (destination / snapshot_id / "alpha" / "SKILL.md").read_bytes().endswith(
        b"Line one\r\nLine two\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX executable mode")
def test_publication_preserves_each_executable_bit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    helper = _write_skill(source, "alpha", "alpha", "Alpha").parent / "run.sh"
    helper.write_text("echo alpha\n", encoding="utf-8")
    helper.chmod(0o744)

    snapshot_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert (destination / snapshot_id / "alpha" / "run.sh").stat().st_mode & 0o111 == 0o100


def test_publication_ignores_install_generated_python_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "alpha", "alpha", "Alpha")
    cache = source / "alpha" / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "helper.cpython-312.pyc").write_bytes(b"generated")
    (cache.parent / "helper.py").write_text("print('source')\n", encoding="utf-8")
    destination = tmp_path / "runtime"

    snapshot_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert not (destination / snapshot_id / "alpha" / "scripts" / "__pycache__").exists()


def test_publication_rejects_empty_companion_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    (skill_file.parent / "assets").mkdir()

    with pytest.raises(RuntimeError, match="must not be empty"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "runtime")


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


def test_publication_reclaims_interrupted_staging_and_retries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    _write_skill(source, "alpha", "alpha", "Alpha")
    snapshot_id = snapshot_tree_digest(source)
    staging = destination / f".snapshot-{snapshot_id}.staging"
    staging.mkdir(parents=True)
    (staging / "partial").write_text("interrupted", encoding="utf-8")

    assert publish_builtin_skills(source_root=source, destination_root=destination) == snapshot_id
    assert not staging.exists()
    assert (destination / snapshot_id / "alpha" / "SKILL.md").is_file()


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


def test_publication_rejects_duplicate_declared_builtin_names(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "first", "shared-name", "First")
    _write_skill(source, "second", "shared-name", "Second")

    with pytest.raises(RuntimeError, match="unique declared names"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "out")


def test_publication_rejects_invalid_utf8_builtin_body(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    skill_file.write_bytes(
        b"---\nname: alpha\ndescription: Alpha\n---\ninvalid:\xff\n"
    )

    with pytest.raises(RuntimeError, match="Built-in Skill is invalid"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "out")


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create forbidden fixture names")
@pytest.mark.parametrize("component", ["bad?name", "bad*name", 'bad"name', "bad\x01name"])
def test_publication_rejects_all_windows_forbidden_path_classes(
    tmp_path: Path,
    component: str,
) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    (skill_file.parent / component).write_text("invalid", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not portable"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "out")


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


def test_publication_bounds_the_complete_builtin_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "builtin-skills"
    too_many_entries = tmp_path / "too-many-tree-entries"
    skill_file = _write_skill(too_many_entries, "alpha", "alpha", "Alpha")
    references = skill_file.parent / "references"
    references.mkdir()
    (references / "one.md").write_text("one\n", encoding="utf-8")
    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_ENTRIES", 3)
    with pytest.raises(RuntimeError, match="3 entries"):
        publish_builtin_skills(source_root=too_many_entries, destination_root=destination)

    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_ENTRIES", 4096)
    too_many_bytes = tmp_path / "too-many-tree-bytes"
    byte_skill = _write_skill(too_many_bytes, "alpha", "alpha", "Alpha")
    (byte_skill.parent / "reference.md").write_bytes(b"x" * 32)
    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_BYTES", 64)
    with pytest.raises(RuntimeError, match="64 bytes"):
        publish_builtin_skills(source_root=too_many_bytes, destination_root=destination)


def test_publication_copies_only_the_bounded_enumeration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    _write_skill(source, "alpha", "alpha", "Alpha")
    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_BYTES", 128)
    original_snapshot_entries = managed_skills._snapshot_entries
    source_enumerations = 0

    def enumerate_then_add_late_entry(root: Path):
        nonlocal source_enumerations
        entries = original_snapshot_entries(root)
        if Path(root) == source:
            source_enumerations += 1
            if source_enumerations == 3:
                (source / "late.bin").write_bytes(b"x" * 129)
        return entries

    monkeypatch.setattr(managed_skills, "_snapshot_entries", enumerate_then_add_late_entry)

    snapshot_id = publish_builtin_skills(source_root=source, destination_root=destination)

    assert source_enumerations == 3
    assert not (destination / snapshot_id / "late.bin").exists()


def test_publication_charges_file_growth_at_copy_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "builtin-skills"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    monkeypatch.setattr(managed_skills, "BUILTIN_TREE_MAX_BYTES", 128)
    original_snapshot_entries = managed_skills._snapshot_entries
    source_enumerations = 0

    def enumerate_then_grow_file(root: Path):
        nonlocal source_enumerations
        entries = original_snapshot_entries(root)
        if Path(root) == source:
            source_enumerations += 1
            if source_enumerations == 3:
                skill_file.write_bytes(b"x" * 129)
        return entries

    monkeypatch.setattr(managed_skills, "_snapshot_entries", enumerate_then_grow_file)

    with pytest.raises(RuntimeError, match="128 bytes"):
        publish_builtin_skills(source_root=source, destination_root=destination)


def test_authoritative_builtin_source_contains_prompt_modules() -> None:
    source = managed_skills.builtin_skills_source()

    expected = {
        "use-avibe-harness": "Avibe Harness turns user intent into durable Agent work",
        "use-avibe-vault": "the child process receives static secrets as environment variables",
        "use-show-pages": "Every Show Page URL is agent-readable without page-specific code",
    }
    for name, migrated_text in expected.items():
        skill = parse_skill_file(
            source / name / "SKILL.md",
            priority=(0, 0, 0),
            include_body=True,
        )
        assert skill is not None
        assert skill.name == name
        assert skill.body is not None
        assert migrated_text in skill.body


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


def test_workbench_management_acl_does_not_narrow_runtime_catalog(
    tmp_path: Path, monkeypatch
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
    skills = resolve_skills(cwd)

    assert [skill.name for skill in skills] == ["builtin", "project"]


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture requires POSIX semantics")
def test_publication_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    skill_file = _write_skill(source, "alpha", "alpha", "Alpha")
    (skill_file.parent / "linked").symlink_to(skill_file)

    with pytest.raises(RuntimeError, match="not a directory or regular file"):
        publish_builtin_skills(source_root=source, destination_root=tmp_path / "out")


def test_bound_working_directory_and_snapshot_are_inherited(monkeypatch, tmp_path: Path) -> None:
    home, advertised_cwd = _isolate_live_commands(monkeypatch, tmp_path)
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
        BUILTIN_SKILLS_ROOT_ENV: str(
            (Path(os.environ["AVIBE_HOME"]) / "builtin-skills" / snapshot_id).resolve()
        ),
        SKILL_HOME_ENV: str(home.resolve()),
        SKILL_CODEX_HOME_ENV: str((home / ".codex").resolve()),
        SKILL_CLAUDE_HOME_ENV: str((home / ".claude").resolve()),
        SKILL_XDG_CONFIG_HOME_ENV: str((home / ".config").resolve()),
    }


def test_managed_skill_environment_binds_valid_project_base(tmp_path: Path) -> None:
    project = tmp_path / "project"
    working_directory = project / "child"
    working_directory.mkdir(parents=True)

    env = managed_skill_environment(working_directory, project_base=project)

    assert env[SKILL_WORKING_DIR_ENV] == str(working_directory.resolve())
    assert env[SKILL_PROJECT_BASE_ENV] == str(project.resolve())
    assert SKILL_PROJECT_BASE_ENV not in managed_skill_environment(
        working_directory,
        project_base=tmp_path / "other",
    )


def test_managed_skill_environment_binds_configured_claude_cli_path(
    tmp_path: Path,
) -> None:
    env = managed_skill_environment(
        tmp_path,
        claude_cli_path="~/tools/claude-custom",
    )

    assert env[SKILL_CLAUDE_CLI_PATH_ENV] == str(
        Path("~/tools/claude-custom").expanduser()
    )


def test_managed_skill_environment_can_retain_an_explicit_builtin_snapshot(
    tmp_path: Path,
) -> None:
    working_directory = tmp_path / "project"
    working_directory.mkdir()
    snapshot_id = "d" * 64
    snapshot_root = tmp_path / "old-home" / "builtin-skills" / snapshot_id

    env = managed_skill_environment(
        working_directory,
        builtin_snapshot_id=snapshot_id,
        builtin_snapshot_root=snapshot_root,
    )

    assert env[BUILTIN_SKILLS_SNAPSHOT_ENV] == snapshot_id
    assert env[BUILTIN_SKILLS_ROOT_ENV] == str(snapshot_root.resolve())
    invalid = managed_skill_environment(
        working_directory,
        builtin_snapshot_id="invalid",
        builtin_snapshot_root=snapshot_root,
    )
    assert BUILTIN_SKILLS_SNAPSHOT_ENV not in invalid
    assert BUILTIN_SKILLS_ROOT_ENV not in invalid


def test_bound_builtin_root_survives_avibe_home_change(monkeypatch, tmp_path: Path) -> None:
    home, cwd = _isolate_live_commands(monkeypatch, tmp_path)
    snapshot_id = "e" * 64
    original_root = home / "first-avibe" / "builtin-skills" / snapshot_id
    _write_skill(original_root, "builtin", "builtin", "Built-in")
    monkeypatch.setenv(BUILTIN_SKILLS_SNAPSHOT_ENV, snapshot_id)
    monkeypatch.setenv(BUILTIN_SKILLS_ROOT_ENV, str(original_root.resolve()))
    monkeypatch.setenv("AVIBE_HOME", str(home / "second-avibe"))

    assert [skill.name for skill in resolve_skills(cwd)] == ["builtin"]
