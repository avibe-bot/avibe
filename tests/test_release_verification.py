from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import shlex
import subprocess
import sys
import shutil
import threading

import pytest
import yaml

from tests.e2e.github_release_fixture import create_certificate, make_server


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ("release_ai.yml", "build-assets", "Checkout release verification", "${{ github.sha }}"),
    ("publish.yml", "build", "Checkout release automation", "${{ needs.resolve-tag.outputs.workflow_sha }}"),
)


@pytest.mark.parametrize(
    ("annotation", "existing_body", "silent"),
    [
        ("说明\n<!-- avibe:update-notification=none -->", "", True),
        ("vibe-remote:update-notification=none", "", True),
        ("Ordinary annotated release", "", False),
        (None, "", False),
        (None, "<!-- vibe-remote:update-notification=none -->", True),
        (None, "Document `avibe:update-notification=none` in prose.", False),
    ],
)
@pytest.mark.parametrize("rewritten", [False, True])
def test_notes_shell_reads_remote_annotation_not_checkout_ref(
    tmp_path, annotation, existing_body, silent, rewritten,
):
    remote, workspace, env, tag, source, command = _notes_git_fixture(
        tmp_path, annotation=annotation, existing_body=existing_body,
    )
    if rewritten:
        _fixture_git(workspace, "update-ref", f"refs/tags/{tag}", source)
    local_object = _fixture_git(workspace, "rev-parse", f"refs/tags/{tag}")
    remote_object = _fixture_git(remote, "rev-parse", f"refs/tags/{tag}")
    notes = workspace / "release.md"
    notes.write_text("# Release 发布说明\n\nChanges and changes in Chinese.\n", encoding="utf-8")
    for _ in range(2):
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    text = notes.read_text(encoding="utf-8")
    for brand in ("avibe", "vibe-remote"):
        assert text.count(f"<!-- {brand}:update-notification=none -->") == int(silent)
    assert text.count(f"<!-- avibe:release-notes=ready source={source} run=42 -->") == 1
    assert "# Release 发布说明" in text
    assert _fixture_git(workspace, "rev-parse", f"refs/tags/{tag}") == local_object
    assert _fixture_git(remote, "rev-parse", f"refs/tags/{tag}") == remote_object


@pytest.mark.parametrize("failure", ["missing-tag", "source-mismatch"])
def test_notes_shell_does_not_mutate_notes_when_remote_tag_cannot_be_bound(tmp_path, failure):
    remote, workspace, env, tag, source, command = _notes_git_fixture(
        tmp_path, annotation="<!-- avibe:update-notification=none -->",
        existing_body="<!-- vibe-remote:update-notification=none -->",
    )
    _fixture_git(remote, "update-ref", "-d", f"refs/tags/{tag}")
    if failure == "source-mismatch":
        _fixture_git(remote, "commit", "--allow-empty", "-m", "Different release source")
        _fixture_git(remote, "tag", "-a", tag, "-m", "Replacement annotation")
    notes = workspace / "release.md"
    before = b"# Untouched release notes\n"
    notes.write_bytes(before)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert notes.read_bytes() == before
    assert _fixture_git(workspace, "rev-parse", f"refs/tags/{tag}^{{commit}}") == source


def _fixture_git(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=Release test",
         "-c", "user.email=release-test@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()


def _notes_git_fixture(tmp_path, *, annotation, existing_body):
    remote = tmp_path / "remote 中文"
    remote.mkdir()
    _fixture_git(remote, "init")
    # Lightweight commit-message prose is not an annotation. Annotated cases
    # use an ordinary commit, reproducing checkout losing actual silent intent.
    message = (
        "Document avibe:update-notification=none without selecting that policy"
        if annotation is None else "Release source"
    )
    _fixture_git(remote, "commit", "--allow-empty", "-m", message)
    source = _fixture_git(remote, "rev-parse", "HEAD")
    tag = "gh-v3.1.0rc1"
    if annotation is None:
        _fixture_git(remote, "tag", tag)
    else:
        _fixture_git(remote, "tag", "-a", tag, "-m", annotation)
    workspace = tmp_path / "checkout 中文"
    _fixture_git(tmp_path, "clone", "--no-hardlinks", str(remote), str(workspace))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"assert sys.argv[1:] == {['release', 'view', tag, '--repo', 'avibe-bot/avibe', '--json', 'body', '--jq', '.body']!r}\n"
        f"print({existing_body!r})\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {
        **os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "TAG": tag, "GITHUB_RUN_ID": "42", "GITHUB_REPOSITORY": "avibe-bot/avibe",
    }
    command = _step(
        _job("release_ai.yml", "release"),
        "Preserve silent-update marker (tag annotation or existing release body)",
    )["run"]
    return remote, workspace, env, tag, source, command






@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
@pytest.mark.parametrize("tag", ["v3.2.0-rc1", "v03.02.00"])
def test_noncanonical_official_tags_fail_before_artifact_construction(
    tmp_path, workflow_name, job_name, checkout_name, workflow_ref, tag,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/release_package_version.py", scripts)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    command = _step(_job(workflow_name, job_name), "Pin package version to release tag")["run"]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command + "\ntouch artifact-construction-reached"],
        cwd=tmp_path, env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                          "RELEASE_TAG": tag, "GITHUB_ENV": str(tmp_path / "github-env")},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "canonical spelling" in result.stderr
    assert not (tmp_path / "artifact-construction-reached").exists()
    assert not (tmp_path / "github-env").exists()


def _job(workflow_name: str, job_name: str) -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    return workflow["jobs"][job_name]


def _step(job: dict, name: str) -> dict:
    step, = [step for step in job["steps"] if step.get("name") == name]
    return step


def test_official_finalization_serializes_cross_version_decision_and_publication():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    job = workflow["jobs"]["finalize-github-release"]
    # A literal group is shared by every tag, branch and event. Keep only the
    # critical finalization job serialized; builds must remain independent.
    assert job["concurrency"] == {
        "group": "avibe-official-release-finalization",
        "cancel-in-progress": False,
        "queue": "max",
    }
    assert "concurrency" not in workflow
    assert "concurrency" not in workflow["jobs"]["build"]
    assert "wait-notes" in _step(job, "Wait for exact-source release notes")["run"]
    finalize = _step(job, "Publish GitHub Release")["run"]
    assert 'LATEST_MODE="auto"' in finalize
    assert "python scripts/github_release.py finalize" in finalize
    assert not job.get("continue-on-error")


def test_release_installer_job_provisions_the_same_uv_as_its_ci_consumer():
    job = _job("publish.yml", "build")
    lint = _job("lint.yml", "install-upgrade-shards")
    setup = _step(job, "Install pinned uv")
    ci_setup = _step(lint, "Install pinned uv")
    consumer = _step(job, "Run release install and upgrade regressions")
    assert setup["run"] == ci_setup["run"]
    assert "--only-binary=:all: --no-deps uv==0.12.10" in setup["run"]
    assert job["steps"].index(setup) < job["steps"].index(consumer)
    assert job["steps"].index(consumer) < job["steps"].index(_step(job, "Upload GitHub release assets"))
    assert "tests/e2e/test_install_command.py" in consumer["run"]
    assert not setup.get("if") and not setup.get("continue-on-error")
    assert not consumer.get("if") and not consumer.get("continue-on-error")





