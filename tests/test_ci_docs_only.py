"""CI skip authority: real Git/event boundaries, never filename mocks.

A false docs verdict hides runtime regressions. Existing pipeline tests cover
job selection and real gate shell behavior; this file owns the Git evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.ci_docs_only import classify

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci_docs_only.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def write(repo: Path, path: str, text: str = "Narrative documentation\n") -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid",
        "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    write(root, "AGENTS.md", "Initial guidance\n")
    write(root, "core.py", "print('runtime')\n")
    commit(root)
    return root


def push(repo: Path, base: str, **overrides) -> tuple[bool, str, str, bool]:
    after = git(repo, "rev-parse", "HEAD")
    event = {"before": base, "after": after, "created": False, "deleted": False, "forced": False}
    event.update(overrides)
    return classify("push", event, after, repo)


@pytest.mark.parametrize(("path", "docs"), [
    ("AGENTS.md", True),
    ("CONTRIBUTING.md", True),
    ("docs/普通 说明.md", True),
    ("docs/guide\twith\nseparators.md", True),
    (".github/PULL_REQUEST_TEMPLATE.md", True),
    ("README.md", False),
    ("README_ZH.md", False),
    ("core/prompts/agent-instructions.md", False),
    ("skills/use-avibe/SKILL.md", False),
    ("docs/plans/ordinary-plan.md", False),
    ("docs/plans/model-hub-implementation.md", False),
    ("docs/plans/model-hub-routing-modes.md", False),
    ("docs/plans/model-hub-contracts/api.md", False),
    ("docs/plans/agent-core-contracts/context.md", False),
    ("docs/plans/model-hub.md", False),
    ("docs/plans/model-hub-shell-core-coverage.md", False),
    ("docs/INSTALL_FOR_AI_ZH.md", False),
    ("docs/fixture.json", False),
    (".github/workflows/lint.yml", False),
    ("scripts/ci_docs_only.py", False),
    ("ui/package-lock.json", False),
    ("tests/fixture.md", False),
])
def test_only_narrative_paths_can_skip(repo: Path, path: str, docs: bool) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, path)
    commit(repo)
    assert push(repo, before)[0] is docs


def test_complete_diff_not_last_commit_or_truncated_event(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    for i in range(305):
        write(repo, f"docs/{i}.md")
    write(repo, "zzz_runtime.py", "print('changed')\n")
    commit(repo)
    write(repo, "AGENTS.md")
    commit(repo)
    # Neither a 300-file list limit nor an event listing only the last doc
    # commit may hide the earlier runtime change.
    assert not push(repo, before, commits=[{"modified": ["AGENTS.md"]}])[0]


@pytest.mark.parametrize(("source", "target", "docs"), [
    ("core.py", "docs/moved.md", False),
    ("AGENTS.md", "runtime.py", False),
    ("AGENTS.md", "docs/renamed.md", True),
])
def test_rename_checks_both_paths(repo: Path, source: str, target: str, docs: bool) -> None:
    before = git(repo, "rev-parse", "HEAD")
    (repo / target).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "mv", source, target)
    commit(repo)
    assert push(repo, before)[0] is docs


@pytest.mark.parametrize("kind", ["executable", "symlink", "submodule", "type-change"])
def test_doc_suffix_never_hides_non_document_modes(repo: Path, kind: str) -> None:
    before = git(repo, "rev-parse", "HEAD")
    if kind == "submodule":
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{before},docs/module.md")
        git(repo, "-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid", "commit", "-qm", "gitlink")
    else:
        path = repo / "AGENTS.md"
        if kind == "executable":
            path.chmod(0o755)
        elif kind == "symlink":
            (repo / "docs").mkdir()
            (repo / "docs/link.md").symlink_to("../core.py")
        else:
            path.unlink()
            path.symlink_to("core.py")
        commit(repo)
    assert not push(repo, before)[0]


def synthetic_pr(repo: Path) -> tuple[dict, str]:
    git(repo, "checkout", "-qb", "docs-change")
    write(repo, "AGENTS.md")
    head = commit(repo)
    git(repo, "checkout", "-q", "main")
    # Base advanced independently. Its code change is not this doc PR's change.
    write(repo, "core.py", "print('new base')\n")
    base = commit(repo)
    git(repo, "-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid",
        "merge", "--no-ff", "-qm", "PR merge", "docs-change")
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}, git(repo, "rev-parse", "HEAD")


def test_pr_compares_actual_merge_tree_with_verified_parents_at_depth_two(repo: Path, tmp_path: Path) -> None:
    event, sha = synthetic_pr(repo)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(shallow)], check=True)
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert classify("pull_request", event, sha, shallow)[0]
    wrong = json.loads(json.dumps(event))
    wrong["pull_request"]["head"]["sha"] = event["pull_request"]["base"]["sha"]
    assert not classify("pull_request", wrong, sha, shallow)[0]
    assert not classify("pull_request", event, event["pull_request"]["head"]["sha"], shallow)[0]


def test_multi_commit_push_fetches_missing_before_not_all_history(repo: Path, tmp_path: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    for i in range(3):
        write(repo, f"docs/{i}.md")
        commit(repo)
    shallow = tmp_path / "push-checkout"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(shallow)], check=True)
    assert push(shallow, before)[0]


@pytest.mark.parametrize("change", [
    {"before": "0" * 40}, {"before": "f" * 40}, {"after": "f" * 40},
    {"before": "--output=/tmp/wrong"}, {"created": True}, {"deleted": True},
    {"forced": True}, {"forced": None},
])
def test_unknown_push_evidence_falls_back_to_full(repo: Path, change: dict) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "AGENTS.md")
    commit(repo)
    assert not push(repo, before, **change)[0]


def test_empty_diff_and_unsupported_or_broken_events_are_not_docs(repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD")
    assert not push(repo, sha)[0]
    for name, payload in (("workflow_dispatch", {}), ("pull_request", {}), ("push", [])):
        assert not classify(name, payload, sha, repo)[0]


@pytest.mark.parametrize("event_name,expected", [("pull_request", "true"), ("push", "false")])
def test_cli_keeps_docs_pr_fast_but_master_replaces_full_coverage(
    repo: Path, tmp_path: Path, event_name: str, expected: str,
) -> None:
    if event_name == "pull_request":
        payload, sha = synthetic_pr(repo)
    else:
        # This code commit may still have a running (now superseded) full CI.
        write(repo, "core.py", "print('code awaiting CI')\n")
        before = commit(repo)
        write(repo, "AGENTS.md")
        sha = commit(repo)
        payload = {"before": before, "after": sha, "created": False, "deleted": False, "forced": False}
    event = tmp_path / "event.json"
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    event.write_text(json.dumps(payload))
    env = {**os.environ, "GITHUB_EVENT_NAME": event_name, "GITHUB_SHA": sha,
           "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output),
           "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == (
        f"docs_only={expected}\nwindows_control_ipc=false\n"
    )
    assert "Impact shadow: **docs-only**" in summary.read_text()
    if event_name == "push":
        assert "superseded code changes" in result.stdout
    event.unlink()
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        f"docs_only={expected}",
        "windows_control_ipc=false",
        "docs_only=false",
        "windows_control_ipc=false",
    ]
    assert "Impact shadow: **unknown**" in summary.read_text()
    assert "full" in summary.read_text()


@pytest.mark.parametrize("paths,category", [
    (["ui/src/pages/Page.tsx", "docs/guide.md"], "ui-source-only"),
    (["ui/src/App.tsx", "ui/src/lib/ApiContext.tsx"], "ui-source-only"),
    (["vibe/api.py", "storage/alembic/versions/new.py"], "python-source-only"),
    (["ui/src/Page.tsx", "ui/package-lock.json"], "shared-or-mixed"),
    (["core/controller.py", "ui/src/Page.tsx"], "shared-or-mixed"),
    (["docs/plans/contract.md"], "shared-or-mixed"),
])
def test_impact_shadow_never_authorizes_skipping_code_tests(repo: Path, tmp_path: Path, paths, category) -> None:
    before = git(repo, "rev-parse", "HEAD")
    for path in paths:
        write(repo, path)
    sha = commit(repo)
    result = push(repo, before)
    assert result[0] is False and result[2] == category
    event = tmp_path / "event.json"
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    event.write_text(json.dumps({"before": before, "after": sha,
                                 "created": False, "deleted": False, "forced": False}))
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True,
                            env={**os.environ, "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": sha,
                                 "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output),
                                 "GITHUB_STEP_SUMMARY": str(summary)})
    assert result.returncode == 0, result.stderr
    # Impact stays observational; windows_control_ipc is an explicit extra job, not a skip.
    assert output.read_text() == "docs_only=false\nwindows_control_ipc=false\n"
    assert f"Impact shadow: **{category}**" in summary.read_text()


@pytest.mark.parametrize(("path", "selected"), [
    ("core/managed_skills.py", True),
    ("tests/test_managed_skills.py", True),
    ("core/controller.py", False),
    ("tests/test_control_ipc.py", False),
])
def test_windows_control_ipc_selects_builtin_skill_publication_paths(
    repo: Path, tmp_path: Path, path: str, selected: bool,
) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, path, "print('runtime')\n")
    sha = commit(repo)
    result = push(repo, before)
    assert result[0] is False and result[3] is selected
    event = tmp_path / "event.json"
    output = tmp_path / "output"
    event.write_text(json.dumps({"before": before, "after": sha,
                                 "created": False, "deleted": False, "forced": False}))
    ran = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": sha,
             "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output)},
    )
    assert ran.returncode == 0, ran.stderr
    assert output.read_text() == (
        f"docs_only=false\nwindows_control_ipc={str(selected).lower()}\n"
    )


def test_guidance_remains_enforced_when_an_agents_only_diff_skips_application_tests(repo: Path) -> None:
    root = SCRIPT.parents[1]
    paths = (
        "AGENTS.md", "tests/test_repository_guidance.py", ".agents/skills/pr-delivery-loop/SKILL.md",
        "skills/background-watch-hook/scripts/wait_pr.py", "skills/background-watch-hook/scripts/wait_action.py",
    )
    for relative in paths:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    before = commit(repo)
    command = [sys.executable, "-m", "pytest", "--noconftest", "-p", "no:cacheprovider", "-c", "/dev/null", "-q",
               "tests/test_repository_guidance.py"]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    valid = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    agents = repo / "AGENTS.md"
    agents.write_text(agents.read_text().replace("use the `background-watch-hook` skill", "use unmanaged polling"))
    commit(repo)
    assert push(repo, before)[0]  # Scope remains docs; the independent check must reject its content.
    invalid = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert "test_pr_delivery_loop_delegates_waiters_to_background_watch_skill" in invalid.stdout
