"""CI skip authority: real Git/event boundaries, never filename mocks.

A false docs verdict or route hides runtime regressions. Existing pipeline
tests cover job selection and real gate shell behavior; this file owns the Git
evidence: which complete synthetic-merge diffs may skip what.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from scripts.ci_docs_only import Scope, classify, platform_independent

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


def make_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    write(root, "AGENTS.md", "Initial guidance\n")
    write(root, "core.py", "print('runtime')\n")
    write(root, "core/existing.py", "print('runtime')\n")
    write(root, "ui/src/Existing.tsx", "export const existing = 1;\n")
    write(root, "ui/scripts/emojiCatalog.ts", "export function emojiCatalog() { return { name: 'catalog' }; }\n")
    commit(root)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def push(repo: Path, base: str, **overrides) -> Scope:
    after = git(repo, "rev-parse", "HEAD")
    event = {"before": base, "after": after, "created": False, "deleted": False, "forced": False}
    event.update(overrides)
    return classify("push", event, after, repo)


def routes(scope: Scope) -> tuple[str, str]:
    return scope.browser, scope.platform


FULL = ("run", "run")


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
    assert push(repo, before).docs_only is docs


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
    assert not push(repo, before, commits=[{"modified": ["AGENTS.md"]}]).docs_only


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
    assert push(repo, before).docs_only is docs


def irregular_change(repo: Path, kind: str, path: str) -> None:
    head = git(repo, "rev-parse", "HEAD")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "submodule":
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},{path}")
        git(repo, "-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid", "commit", "-qm", "gitlink")
        return
    if kind == "executable":
        if not target.exists():
            target.write_text("export {};\n", encoding="utf-8")
        target.chmod(0o755)
    elif kind == "symlink":
        target.symlink_to("../../core.py" if "/" in path else "core.py")
    else:  # type-change: an existing regular file becomes a symlink
        target.unlink()
        target.symlink_to("../../core.py" if "/" in path else "core.py")
    commit(repo)


@pytest.mark.parametrize(("kind", "path"), [
    ("executable", "AGENTS.md"), ("symlink", "docs/link.md"), ("submodule", "docs/module.md"),
    ("type-change", "AGENTS.md"),
])
def test_doc_suffix_never_hides_non_document_modes(repo: Path, kind: str, path: str) -> None:
    before = git(repo, "rev-parse", "HEAD")
    irregular_change(repo, kind, path)
    result = push(repo, before)
    assert result.docs_only is False and result.windows_control_ipc is False


def test_windows_control_ipc_survives_an_irregular_sibling_diff(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "core/managed_skills.py", "print('runtime')\n")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/link.md").symlink_to("../core.py")
    commit(repo)
    result = push(repo, before)
    assert result.docs_only is False and result.windows_control_ipc is True


def synthetic_pr(repo: Path, change=None, **pull_request) -> tuple[dict, str]:
    """A pull request merged onto a base that moved independently, as GitHub checks it out."""
    git(repo, "checkout", "-qb", "change")
    if change is None:
        write(repo, "AGENTS.md")
    else:
        change(repo)
    # A change may commit itself (a gitlink has no working-tree file to add).
    head = commit(repo) if git(repo, "status", "--porcelain") else git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    # Base advanced independently. Its code change is not this PR's change.
    write(repo, "core.py", "print('new base')\n")
    base = commit(repo)
    git(repo, "-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid",
        "merge", "--no-ff", "-qm", "PR merge", "change")
    event = {
        "pull_request": {"base": {"sha": base, "ref": "main"}, "head": {"sha": head}},
        "repository": {"default_branch": "main"},
    }
    event["pull_request"].update(pull_request)
    return event, git(repo, "rev-parse", "HEAD")


def synthetic_merge_group(repo: Path, change=None, **merge_group) -> tuple[dict, str]:
    """A queue merge group whose head is the exact tree GitHub will test."""
    pr_event, head = synthetic_pr(repo, change)
    group = {
        "base_sha": pr_event["pull_request"]["base"]["sha"],
        "base_ref": "refs/heads/main",
        "head_sha": head,
        "head_ref": "refs/heads/gh-readonly-queue/main/pr-1",
    }
    group.update(merge_group)
    return {
        "merge_group": group,
        "repository": pr_event["repository"],
    }, head


def test_merge_group_compares_base_and_head_from_the_event(repo: Path) -> None:
    event, head = synthetic_merge_group(repo, writes("core/feature.py", "docs/plans/feature.md"))
    scope = classify("merge_group", event, head, repo)
    assert scope.docs_only is False and routes(scope) == ("skip", "run")


def test_docs_only_merge_group_keeps_both_expensive_families_skipped(repo: Path) -> None:
    event, head = synthetic_merge_group(repo, writes("docs/queue.md"))
    scope = classify("merge_group", event, head, repo)
    assert scope.docs_only is True and routes(scope) == ("skip", "skip")


def test_merge_group_fetches_a_missing_base_object(repo: Path, tmp_path: Path) -> None:
    event, head = synthetic_merge_group(repo, writes("ui/src/Queue.tsx"))
    shallow = tmp_path / "merge-group-checkout"
    subprocess.run(["git", "clone", "-q", "--depth=1", repo.as_uri(), str(shallow)], check=True)
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert routes(classify("merge_group", event, head, shallow)) == ("run", "skip")


@pytest.mark.parametrize("mutate", [
    lambda event: event["merge_group"].update(base_sha="f" * 40),
    lambda event: event["merge_group"].update(head_sha="f" * 40),
])
def test_invalid_merge_group_sha_falls_back_to_full(repo: Path, mutate) -> None:
    event, head = synthetic_merge_group(repo, writes("docs/queue.md"))
    mutate(event)
    result = classify("merge_group", event, head, repo)
    assert result.docs_only is False and result.windows_control_ipc is True and routes(result) == FULL


@pytest.mark.parametrize("mutate", [
    lambda event: event["merge_group"].pop("base_ref"),
    lambda event: event.pop("repository"),
])
def test_merge_group_without_default_branch_metadata_keeps_docs_fast_path_but_not_routes(
    repo: Path, mutate,
) -> None:
    event, head = synthetic_merge_group(repo, writes("docs/queue.md"))
    mutate(event)
    result = classify("merge_group", event, head, repo)
    assert result.docs_only is True and routes(result) == ("skip", "skip")


def writes(*paths: str):
    def change(repo: Path) -> None:
        for path in paths:
            write(repo, path, f"changed {path}\n")
    return change


def test_pr_compares_actual_merge_tree_with_verified_parents_at_depth_two(repo: Path, tmp_path: Path) -> None:
    event, merge = synthetic_pr(repo, writes("core/feature.py", "docs/plans/feature.md"))
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(shallow)], check=True)
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    # The base's own core.py change is not the PR's: only its backend paths count.
    assert routes(classify("pull_request", event, merge, shallow)) == ("skip", "run")
    wrong = json.loads(json.dumps(event))
    wrong["pull_request"]["head"]["sha"] = event["pull_request"]["base"]["sha"]
    assert classify("pull_request", wrong, merge, shallow) == classify("workflow_dispatch", {}, merge, shallow)


UI_SOURCES = (
    "ui/src/App.tsx", "ui/src/i18n/en.json", "ui/e2e/pet-mini/mini.spec.ts",
    "ui/design/frames/home.design.tsx", "ui/playwright.pet-mini.config.ts", "ui/playwright.config.ts", "ui/tsconfig.tests.json",
    "ui/eslint-baseline.json", "ui/.oxlintrc.json", "ui/README.md", "ui/design.html",
    "ui/agentation.d.ts",
)
BACKEND = (
    "core/feature.py", "modules/im/slack.py", "vibe/api.py", "vibe/git_runtime_manifest.json",
    "storage/store.py", "config/v2_config.py", "tests/test_feature.py", "tests/fixtures/case.json",
    "skills/use-avibe/SKILL.md", "desktop/src/main.rs", "assets/logo.svg", "standards/AGENTS.md",
    "examples/demo/README.md", ".agents/skills/local/SKILL.md", "main.py",
)


@pytest.mark.parametrize(("paths", "expected"), [
    # UI sources reach the installed package only as vibe/ui/dist bytes.
    (UI_SOURCES, ("run", "skip")),
    (("ui/src/App.tsx", "docs/plans/ui.md", "tests/scenarios/model_hub/catalog.yaml"), ("run", "skip")),
    # Backend changes cannot reach the browser lanes' checkout.
    (BACKEND, ("skip", "run")),
    (("core/feature.py", "tests/scenarios/model_hub/test_rows.py", "AGENTS.md"), ("skip", "run")),
    # Read by both families' consumers, all of which still run.
    (("docs/plans/feature.md",), ("skip", "skip")),
    (("tests/scenarios/auth_setup/catalog.yaml", "docs/plans/model-hub-contracts/a.json"), ("skip", "skip")),
    # Inside the browser checkout, or shapes the package.
    (("vibe/data/api_key_vendors.json",), FULL),
    (("vibe/message_types.json",), FULL),
    (("assets/mascot/cloud-tuanzi-v2.png",), FULL),
    (("Dockerfile",), FULL),
    (("ui/package.json",), FULL),
    (("ui/package-lock.json",), FULL),
    (("ui/vite.config.ts",), FULL),
    (("ui/index.html",), FULL),
    (("ui/tsconfig.app.json",), FULL),
    (("ui/postcss.config.js",), FULL),
    (("ui/public/icon.png",), FULL),
    (("ui/.gitignore",), FULL),
    # Build code: a Vite plugin can emit a file under any name and path.
    (("ui/scripts/emojiCatalog.ts",), FULL),
    (("ui/scripts/validate-out-of-tree-imports.mjs",), FULL),
    (("ui/scripts/e2e-suite.mjs", "ui/src/App.tsx"), FULL),
    # CI, dependency, packaging and installer definitions.
    ((".github/workflows/lint.yml",), FULL),
    (("scripts/ci_docs_only.py",), FULL),
    (("scripts/incus_regression.py",), FULL),
    (("pyproject.toml",), FULL),
    (("uv.lock",), FULL),
    (("install.ps1",), FULL),
    (("npm/avibe/package.json",), FULL),
    (("patches/model-hub-cpa-1/a.patch",), FULL),
    # Mixed families and unrecognised paths.
    (("ui/src/App.tsx", "core/feature.py"), FULL),
    (("README.md",), FULL),
    (("newtop/module.py",), FULL),
    (("ui/new-root-config.ts",), FULL),
    (("tests/scenarios/model_hub/notes.yaml", "ui/src/App.tsx"), FULL),
])
def test_pull_request_routes_only_paths_outside_each_family_inputs(repo: Path, paths: tuple, expected: tuple) -> None:
    event, sha = synthetic_pr(repo, writes(*paths))
    scope = classify("pull_request", event, sha, repo)
    assert scope.docs_only is False and scope.windows_control_ipc is False
    assert routes(scope) == expected, (scope.browser_reason, scope.platform_reason)


@pytest.mark.parametrize("name", [
    "aux.ts", "LPT1.tsx", "con.test.tsx", "a b.tsx", "a:b.ts", "ünï.json", "trailing.", f"{'x' * 62}.tsx",
])
def test_added_ui_names_the_package_cannot_install_keep_platform_checks(repo: Path, name: str) -> None:
    event, sha = synthetic_pr(repo, writes(f"ui/src/lib/{name}"))
    assert routes(classify("pull_request", event, sha, repo)) == FULL


def test_portable_added_ui_name_and_modified_existing_name_skip_platform(repo: Path) -> None:
    event, sha = synthetic_pr(repo, writes(f"ui/src/lib/{'x' * 61}.ts", "ui/src/Existing.tsx"))
    assert routes(classify("pull_request", event, sha, repo)) == ("run", "skip")


def test_modifying_an_existing_build_plugin_keeps_platform_checks(repo: Path) -> None:
    """A plugin edit can add a generateBundle emit whose fixed names collide on Windows/macOS."""
    def edit(repo: Path) -> None:
        plugin = repo / "ui/scripts/emojiCatalog.ts"
        plugin.write_text(plugin.read_text() + "// generateBundle() { this.emitFile({ fileName: 'assets/EMOJI/en.json' }) }\n")
    event, sha = synthetic_pr(repo, edit)
    scope = classify("pull_request", event, sha, repo)
    assert routes(scope) == FULL and "ui/scripts/emojiCatalog.ts" in scope.platform_reason


def test_platform_allowlist_excludes_every_local_module_the_production_build_loads() -> None:
    """Build code, not UI source: everything vite.config.ts loads, transitively, must keep platform checks."""
    root = SCRIPT.parents[1]
    specifier = re.compile(r"""(?:\bfrom|\bimport)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")
    pending, loaded = [root / "ui/vite.config.ts"], set()
    while pending:
        module = pending.pop()
        if module in loaded:
            continue
        loaded.add(module)
        for relative in specifier.findall(module.read_text(encoding="utf-8")):
            target = (module.parent / relative).resolve()
            found = next((candidate for candidate in (
                target, *(target.with_name(target.name + suffix) for suffix in (".ts", ".mts", ".mjs", ".js")),
            ) if candidate.is_file()), None)
            assert found is not None, (module, relative)
            pending.append(found)
    local = sorted(module.relative_to(root).as_posix() for module in loaded)
    assert "ui/scripts/emojiCatalog.ts" in local  # The scan still sees the plugin it exists for.
    assert [module for module in local if platform_independent(module)] == []


def moves(source: str, target: str):
    def change(repo: Path) -> None:
        (repo / target).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", source, target)
    return change


@pytest.mark.parametrize(("source", "target", "expected"), [
    # No rename detection: both sides of a move count.
    ("core/existing.py", "ui/src/existing.ts", FULL),
    ("ui/src/Existing.tsx", "core/existing_ui.py", FULL),
    ("ui/src/Existing.tsx", "ui/src/Renamed.tsx", ("run", "skip")),
    ("core/existing.py", "core/renamed.py", ("skip", "run")),
    ("ui/src/Existing.tsx", "ui/src/aux.tsx", FULL),
    ("ui/src/Existing.tsx", "ui/scripts/existing.ts", FULL),
])
def test_pull_request_rename_routes_by_both_paths(repo: Path, source: str, target: str, expected: tuple) -> None:
    event, sha = synthetic_pr(repo, moves(source, target))
    assert routes(classify("pull_request", event, sha, repo)) == expected


@pytest.mark.parametrize(("extra", "expected"), [(None, ("run", "skip")), ("core/feature.py", FULL)])
def test_pull_request_routes_from_the_complete_diff_beyond_300_paths(repo: Path, extra, expected) -> None:
    paths = [f"ui/src/generated/c{i:03}.ts" for i in range(305)]
    if extra:
        paths.append(extra)
    event, sha = synthetic_pr(repo, writes(*paths))
    assert routes(classify("pull_request", event, sha, repo)) == expected


@pytest.mark.parametrize(("kind", "path"), [
    ("executable", "ui/scripts/run.mjs"), ("executable", "core/existing.py"), ("symlink", "ui/src/link.ts"),
    ("symlink", "core/link.py"), ("submodule", "ui/src/module"), ("type-change", "ui/src/Existing.tsx"),
])
def test_pull_request_irregular_modes_route_nothing(repo: Path, kind: str, path: str) -> None:
    event, sha = synthetic_pr(repo, lambda repo: irregular_change(repo, kind, path))
    scope = classify("pull_request", event, sha, repo)
    assert scope.docs_only is False and routes(scope) == FULL


@pytest.mark.parametrize("mutate", [
    lambda event: event["pull_request"]["base"].update(ref="release"),
    lambda event: event["pull_request"]["base"].pop("ref"),
    lambda event: event.pop("repository"),
    lambda event: event.update(repository="main"),
    lambda event: event["repository"].update(default_branch=None),
    lambda event: event["repository"].update(default_branch=""),
])
def test_routes_require_a_pull_request_into_the_default_branch(repo: Path, tmp_path: Path, mutate) -> None:
    event, sha = synthetic_pr(repo, writes("core/feature.py"))
    mutate(event)
    assert routes(classify("pull_request", event, sha, repo)) == FULL
    # The documentation fast path keeps its own, unchanged evidence.
    docs_repo = make_repo(tmp_path / "docs-repo")
    docs_event, docs_sha = synthetic_pr(docs_repo)
    mutate(docs_event)
    assert classify("pull_request", docs_event, docs_sha, docs_repo).docs_only is True


def test_multi_commit_push_fetches_missing_before_not_all_history(repo: Path, tmp_path: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    for i in range(3):
        write(repo, f"docs/{i}.md")
        commit(repo)
    shallow = tmp_path / "push-checkout"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(shallow)], check=True)
    assert push(shallow, before).docs_only


@pytest.mark.parametrize("change", [
    {"before": "0" * 40}, {"before": "f" * 40}, {"after": "f" * 40},
    {"before": "--output=/tmp/wrong"}, {"created": True}, {"deleted": True},
    {"forced": True}, {"forced": None},
])
def test_unknown_push_evidence_falls_back_to_full(repo: Path, change: dict) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "AGENTS.md")
    commit(repo)
    result = push(repo, before, **change)
    assert result.docs_only is False and result.windows_control_ipc is True and routes(result) == FULL


def test_push_never_routes_even_when_a_pull_request_would(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "ui/src/App.tsx", "export {};\n")
    commit(repo)
    assert routes(push(repo, before)) == FULL


@pytest.mark.parametrize(("name", "payload"), [
    ("workflow_dispatch", {}), ("merge_group", {}), ("pull_request_target", {}), ("pull_request", {}),
    ("push", []), ("pull_request", {"pull_request": None}), ("pull_request", {"pull_request": {"base": {}}}),
    ("pull_request", {"pull_request": {"base": {"sha": 7}, "head": {"sha": "x"}}}), ("", {}),
])
def test_empty_diff_and_unsupported_or_broken_events_route_nothing(repo: Path, name: str, payload) -> None:
    sha = git(repo, "rev-parse", "HEAD")
    result = push(repo, sha)
    assert result.docs_only is False and result.windows_control_ipc is True and routes(result) == FULL
    classified = classify(name, payload, sha, repo)
    assert classified.docs_only is False and classified.windows_control_ipc is True and routes(classified) == FULL


def run_cli(repo: Path, tmp_path: Path, event_name: str, payload, sha: str, *, event_file: bool = True):
    event = tmp_path / "event.json"
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    for path in (event, output, summary):
        path.unlink(missing_ok=True)
    if event_file:
        event.write_text(json.dumps(payload))
    env = {**os.environ, "GITHUB_EVENT_NAME": event_name, "GITHUB_SHA": sha,
           "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output),
           "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout, output.read_text(), summary.read_text()


def test_cli_pr_uses_checked_out_merge_head_when_github_sha_is_source_head(repo: Path, tmp_path: Path) -> None:
    payload, merge = synthetic_pr(repo)
    source_head = payload["pull_request"]["head"]["sha"]
    assert source_head != merge
    _, output, summary = run_cli(repo, tmp_path, "pull_request", payload, source_head)
    assert output == "docs_only=true\nwindows_control_ipc=false\nbrowser=skip\nplatform=skip\n"
    assert "## CI scope: documentation only" in summary


def test_cli_merge_group_uses_event_base_and_head_for_routing(repo: Path, tmp_path: Path) -> None:
    payload, head = synthetic_merge_group(repo, writes("ui/src/Queue.tsx"))
    _, output, summary = run_cli(repo, tmp_path, "merge_group", payload, head)
    assert output == "docs_only=false\nwindows_control_ipc=false\nbrowser=run\nplatform=skip\n"
    assert "## CI scope: routed" in summary


@pytest.mark.parametrize("event_name", ["pull_request", "merge_group", "push"])
def test_cli_keeps_routable_docs_fast_without_the_old_master_push_override(
    repo: Path, tmp_path: Path, event_name: str,
) -> None:
    if event_name == "pull_request":
        payload, sha = synthetic_pr(repo)
    elif event_name == "merge_group":
        payload, sha = synthetic_merge_group(repo)
    else:
        # Pushes are no longer the second full-suite pass. The classifier still
        # validates their exact diff when called by a standalone probe.
        before = git(repo, "rev-parse", "HEAD")
        write(repo, "AGENTS.md")
        sha = commit(repo)
        payload = {"before": before, "after": sha, "created": False, "deleted": False, "forced": False}
    expected = "docs_only=true\nwindows_control_ipc=false\nbrowser=skip\nplatform=skip\n"
    stdout, output, summary = run_cli(repo, tmp_path, event_name, payload, sha)
    assert output == expected
    assert "## CI scope: documentation only" in summary
    assert "superseded code changes" not in stdout
    stdout, output, summary = run_cli(repo, tmp_path, event_name, payload, sha, event_file=False)
    assert output == "docs_only=false\nwindows_control_ipc=true\nbrowser=run\nplatform=run\n"
    assert "## CI scope: full" in summary and "classification unavailable" in summary


@pytest.mark.parametrize(("paths", "expected", "title"), [
    (("ui/src/App.tsx",), "browser=run\nplatform=skip\n", "routed"),
    (("core/feature.py",), "browser=skip\nplatform=run\n", "routed"),
    (("ui/src/App.tsx", "core/feature.py"), "browser=run\nplatform=run\n", "full"),
])
def test_cli_writes_literal_routes_for_a_pull_request_and_full_for_its_push(
    repo: Path, tmp_path: Path, paths: tuple, expected: str, title: str,
) -> None:
    payload, sha = synthetic_pr(repo, writes(*paths))
    _, output, summary = run_cli(repo, tmp_path, "pull_request", payload, sha)
    assert output == "docs_only=false\nwindows_control_ipc=false\n" + expected
    assert f"## CI scope: {title}" in summary
    assert "- Browser lanes: " in summary and "- Installed-package platform checks: " in summary
    # The same tree pushed to master runs everything.
    before = git(repo, "rev-parse", "HEAD~1")
    _, output, summary = run_cli(repo, tmp_path, "push", {
        "before": before, "after": sha, "created": False, "deleted": False, "forced": False,
    }, sha)
    assert output == "docs_only=false\nwindows_control_ipc=false\nbrowser=run\nplatform=run\n"
    assert "## CI scope: full" in summary


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
    assert result.docs_only is False and result.windows_control_ipc is selected
    _, output, _ = run_cli(repo, tmp_path, "push", {
        "before": before, "after": sha, "created": False, "deleted": False, "forced": False,
    }, sha)
    assert output == f"docs_only=false\nwindows_control_ipc={str(selected).lower()}\nbrowser=run\nplatform=run\n"
    # A pull request keeps the selection while its backend route skips the browser lanes.
    pr_repo = make_repo(tmp_path / "pr-repo")
    event, merge = synthetic_pr(pr_repo, writes(path))
    scope = classify("pull_request", event, merge, pr_repo)
    assert scope.windows_control_ipc is selected and routes(scope) == ("skip", "run")


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
    assert push(repo, before).docs_only  # Scope remains docs; the independent check must reject its content.
    invalid = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert "test_pr_delivery_loop_delegates_waiters_to_background_watch_skill" in invalid.stdout
