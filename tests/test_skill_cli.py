import json
from pathlib import Path

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


def _skill_content(skill_file: Path, body: str) -> str:
    return f'<skill_content name="{skill_file.parent.name}" directory="{skill_file.parent.resolve()}">\n{body}</skill_content>\n'


def test_skill_list_prints_the_json_envelope(monkeypatch, tmp_path, capsys):
    cwd = _isolate_catalog(monkeypatch, tmp_path)
    _write_skill(cwd / ".agents" / "skills", "docs", "Read project docs.", "Use references/guide.md\n")

    assert cmd_skill(build_parser().parse_args(["skill", "list"])) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["kind"] == "skills"
    assert payload["skills"] == [{"name": "docs", "description": "Read project docs."}]
    assert payload["pagination"] == {"page": 1, "returned": 1, "has_more": False, "next_page": None}


def test_skill_load_prints_each_named_skill_in_order_and_fails_on_unknown_names(monkeypatch, tmp_path, capsys):
    cwd = _isolate_catalog(monkeypatch, tmp_path)
    docs = _write_skill(cwd / ".agents" / "skills", "docs", "Read project docs.", "Use references/guide.md\n")
    release = _write_skill(cwd / ".agents" / "skills", "release", "Cut a release.", "Tag it.\n")
    args = build_parser().parse_args(["skill", "load", "--", "release", "missing", "docs"])

    assert cmd_skill(args) == 1

    captured = capsys.readouterr()
    assert captured.out == _skill_content(release, "Tag it.\n") + _skill_content(docs, "Use references/guide.md\n")
    error = json.loads(captured.err)
    assert error["kind"] == "error"
    assert error["code"] == "skill_not_found"
    assert error["details"] == {"missing": ["missing"], "loaded": ["release", "docs"]}


def test_skill_load_of_known_names_succeeds_with_skill_text_only(monkeypatch, tmp_path, capsys):
    cwd = _isolate_catalog(monkeypatch, tmp_path)
    docs = _write_skill(cwd / ".agents" / "skills", "docs", "Read project docs.", "Use references/guide.md\n")

    assert cmd_skill(build_parser().parse_args(["skill", "load", "--", "docs"])) == 0

    captured = capsys.readouterr()
    assert captured.out == _skill_content(docs, "Use references/guide.md\n")
    assert captured.err == ""
