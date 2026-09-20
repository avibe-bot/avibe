from pathlib import Path
from types import SimpleNamespace

from core import managed_skills
from vibe.cli import build_parser, cmd_skill


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return skill_file


def _isolate_catalog(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    empty_builtins = tmp_path / "builtins"
    empty_builtins.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AVIBE_HOME", str(home / ".avibe"))
    monkeypatch.setattr(managed_skills, "builtin_skills_source", lambda: empty_builtins)
    return cwd


def test_skill_list_and_load_follow_the_text_protocol(monkeypatch, tmp_path, capsys):
    cwd = _isolate_catalog(monkeypatch, tmp_path)
    skill_file = _write_skill(
        cwd / ".agents" / "skills",
        "docs",
        "Read project docs.",
        "Use references/guide.md\n",
    )

    assert cmd_skill(SimpleNamespace(skill_command="list", page=1)) == 0
    assert capsys.readouterr().out == "- docs: Read project docs.\n"

    assert cmd_skill(SimpleNamespace(skill_command="load", name="docs")) == 0
    output = capsys.readouterr().out
    assert output == (
        f'<skill_content name="docs" directory="{skill_file.parent.resolve()}">\n'
        "Use references/guide.md\n"
        "</skill_content>\n"
    )


def test_skill_load_failure_has_empty_stdout(monkeypatch, tmp_path, capsys):
    _isolate_catalog(monkeypatch, tmp_path)

    assert cmd_skill(SimpleNamespace(skill_command="load", name="missing")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Skill not found: missing\n"


def test_skill_load_parser_accepts_the_canonical_separator_form():
    args = build_parser().parse_args(["skill", "load", "--", "pdf-processing"])

    assert args.skill_command == "load"
    assert args.name == "pdf-processing"


def test_skill_cli_localizes_errors(monkeypatch, tmp_path, capsys):
    _isolate_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr("vibe.cli._configured_cli_language", lambda: "zh")

    assert cmd_skill(SimpleNamespace(skill_command="load", name="missing")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "未找到 Skill：missing\n"
