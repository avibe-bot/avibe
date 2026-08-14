from __future__ import annotations

import json
import os
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


def _run(
    repo: Path,
    target: Path | None,
    *extra: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo-root",
        str(repo),
    ]
    if target is not None:
        command.extend(["--target", str(target)])
    command.extend([*extra, "--json"])
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=process_env,
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

    checked_again = _run(repo, target, "--check")
    assert checked_again.returncode == 0, checked_again.stderr
    assert json.loads(checked_again.stdout)["ok"] is True

    marker = tmp_path / "untrusted-waiter-executed"
    (target / "scripts/wait_pr.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    drift = _run(repo, target, "--check")
    assert drift.returncode == 1
    assert not marker.exists()
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


def test_default_install_honors_configured_harness_homes(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    homes = tmp_path / "harnesses"
    env = {
        "CODEX_HOME": str(homes / "codex"),
        "AGENTS_HOME": str(homes / "agents"),
        "CLAUDE_HOME": str(homes / "claude"),
        "OPENCODE_HOME": str(homes / "opencode"),
        "XDG_CONFIG_HOME": str(homes / "xdg"),
    }

    installed = _run(repo, None, "--install", env=env)
    assert installed.returncode == 0, installed.stderr
    expected = {
        homes / "codex/skills/background-watch-hook",
        homes / "agents/skills/background-watch-hook",
        homes / "claude/skills/background-watch-hook",
        homes / "opencode/skills/background-watch-hook",
        homes / "xdg/opencode/skills/background-watch-hook",
    }
    assert {Path(target) for target in json.loads(installed.stdout)["targets"]} == expected
    assert all((target / ".avibe-skill-sync.json").is_file() for target in expected)


def test_check_reports_missing_target_and_required_commit(tmp_path: Path) -> None:
    repo, commit = _make_repo(tmp_path)
    target = tmp_path / "home/.opencode/skills/background-watch-hook"

    checked = _run(repo, target, "--check")
    assert checked.returncode == 1
    payload = json.loads(checked.stdout)
    assert payload["required_commit"] == commit
    assert str(target) in payload["problems"][0]
    assert "canonical_path" in payload


def test_check_follows_the_active_skill_resolution(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    target = tmp_path / "home/.codex/skills/background-watch-hook"

    installed = _run(repo, target, "--install")
    assert installed.returncode == 0, installed.stderr

    installed_check = _run(
        repo,
        None,
        "--check",
        env={"BACKGROUND_WATCH_HOOK_SKILL_FILE": str(target / "SKILL.md")},
    )
    assert installed_check.returncode == 0, installed_check.stderr

    canonical_check = _run(
        repo,
        None,
        "--check",
        env={
            "BACKGROUND_WATCH_HOOK_SKILL_FILE": str(
                repo / "skills/background-watch-hook/SKILL.md"
            )
        },
    )
    assert canonical_check.returncode == 0, canonical_check.stderr


def test_check_resolves_the_active_skill_from_a_companion_repo(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    companion = tmp_path / "avibe-docs"
    target = companion / ".agents/skills/background-watch-hook"

    installed = _run(repo, target, "--install")
    assert installed.returncode == 0, installed.stderr

    checked = _run(
        repo,
        None,
        "--caller-root",
        str(companion),
        "--check",
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["targets"] == [str(target.resolve())]


def test_install_refuses_the_canonical_checkout_and_symlink(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    canonical = repo / "skills/background-watch-hook"

    direct = _run(repo, canonical, "--install")
    assert direct.returncode == 1
    assert "canonical checkout" in json.loads(direct.stdout)["error"]
    assert not (canonical / ".avibe-skill-sync.json").exists()

    symlink = tmp_path / "home/.agents/skills/background-watch-hook"
    symlink.parent.mkdir(parents=True)
    symlink.symlink_to(canonical, target_is_directory=True)
    linked = _run(repo, symlink, "--install")
    assert linked.returncode == 1
    assert "canonical checkout" in json.loads(linked.stdout)["error"]

    parent_alias = tmp_path / "avibe-alias"
    parent_alias.symlink_to(repo, target_is_directory=True)
    parent_linked = _run(
        repo,
        parent_alias / "skills/background-watch-hook",
        "--install",
    )
    assert parent_linked.returncode == 1
    assert "canonical checkout" in json.loads(parent_linked.stdout)["error"]


def test_install_rejects_a_regular_file_without_mutating_it(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    target = tmp_path / "home/.agents/skills/background-watch-hook"
    target.parent.mkdir(parents=True)
    target.write_text("corrupt installation\n", encoding="utf-8")

    installed = _run(repo, target, "--install")
    assert installed.returncode == 1
    assert "not a directory" in json.loads(installed.stdout)["error"]
    assert target.read_text(encoding="utf-8") == "corrupt installation\n"
    assert not list(target.parent.glob(".background-watch-hook.old-*"))


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
