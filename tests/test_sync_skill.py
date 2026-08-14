from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skills/background-watch-hook/scripts/sync_skill.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "avibe"
    skill = repo / "skills/background-watch-hook"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: background-watch-hook\nversion: 0.15.0\n---\n",
        encoding="utf-8",
    )
    (skill / "scripts/wait_pr.py").write_text(
        """#!/usr/bin/env python3
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--sha')
parser.add_argument('--workflow')
parser.add_argument('--seed-state', action='store_true')
parser.add_argument('--actionable-only', action='store_true')
parser.add_argument('--ignore-author')
parser.parse_args()
""",
        encoding="utf-8",
    )
    (skill / "scripts/wait_pr.py").chmod(0o755)
    (skill / "README.txt").write_text("canonical skill payload\n", encoding="utf-8")

    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Skill Test")
    _git(repo, "config", "user.email", "skill-test@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "test skill")
    return repo, _git(repo, "rev-parse", "HEAD")


def _run(repo: Path, target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--target",
            str(target),
            *extra,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_install_records_commit_hash_and_check_detects_drift(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    target = tmp_path / "home/.agents/skills/background-watch-hook"

    installed = _run(repo, target, "--install")
    assert installed.returncode == 0, installed.stderr
    payload = json.loads(installed.stdout)
    assert payload["canonical_commit"] == commit
    assert payload["skill_version"] == "0.15.0"

    checked = _run(repo, target, "--check")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["ok"] is True

    (target / "scripts/wait_pr.py").write_text(
        (target / "scripts/wait_pr.py").read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    drift = _run(repo, target, "--check")
    assert drift.returncode == 1
    drift_payload = json.loads(drift.stdout)
    assert drift_payload["ok"] is False
    assert any("tree_sha256" in problem for problem in drift_payload["problems"])


def test_install_deduplicates_harness_symlink_targets(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    agents_target = tmp_path / "home/.agents/skills/background-watch-hook"
    claude_target = tmp_path / "home/.claude/skills/background-watch-hook"
    agents_target.parent.mkdir(parents=True)
    claude_target.parent.mkdir(parents=True)
    claude_target.symlink_to(agents_target)

    installed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--target",
            str(agents_target),
            "--target",
            str(claude_target),
            "--install",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert claude_target.is_symlink()
    assert (agents_target / ".avibe-skill-sync.json").is_file()
    assert len(json.loads(installed.stdout)["targets"]) == 1


def test_check_reports_missing_target_and_required_commit(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    target = tmp_path / "home/.opencode/skills/background-watch-hook"

    checked = _run(repo, target, "--check")
    assert checked.returncode == 1
    payload = json.loads(checked.stdout)
    assert payload["required_commit"] == commit
    assert str(target) in payload["problems"][0]
    assert "canonical_path" in payload


def test_default_commit_tracks_the_skill_tree_not_unrelated_changes(tmp_path: Path) -> None:
    repo, skill_commit = _make_repo(tmp_path)
    unrelated = repo / "README.md"
    unrelated.write_text("unrelated change\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "unrelated change")
    target = tmp_path / "home/.agents/skills/background-watch-hook"

    installed = _run(repo, target, "--install")
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["canonical_commit"] == skill_commit
