from __future__ import annotations

import hashlib
import importlib.util
import json
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import shlex
import subprocess
import sys
import shutil
import threading
import re

import pytest
import yaml

from tests.e2e.github_release_fixture import create_certificate, make_server


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ("release_ai.yml", "build-assets", "Checkout release verification", "${{ github.sha }}"),
    ("publish.yml", "build", "Checkout release automation", "${{ needs.resolve-tag.outputs.workflow_sha }}"),
)


def test_historical_guard_is_release_only_and_verifies_inert_bridge_shape():
    workflow = yaml.safe_load((ROOT / ".github/workflows/memory-runtime-release-guard.yml").read_text())
    assert workflow.get("on", workflow.get(True))["schedule"]
    assert workflow["permissions"]["contents"] == "write"
    text = (ROOT / ".github/workflows/memory-runtime-release-guard.yml").read_text()
    assert "verified inert compatibility bridge" in text
    assert "has no self-pinned historical Runtime manifest" in text
    assert "gh release upload" in text and "All asset names exist" in text
    assert "memory-runtime-release-backup-" in text


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


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
def test_workflow_owned_checkout_supplies_the_executed_contract(tmp_path, monkeypatch, workflow_name, job_name, checkout_name, workflow_ref):
    job = _job(workflow_name, job_name)
    checkout = _step(job, checkout_name)
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == workflow_ref
    assert checkout["with"]["path"] == "release-automation"
    assert checkout["with"]["sparse-checkout-cone-mode"] is False
    files = checkout["with"]["sparse-checkout"].splitlines()
    assert "tests/test_distribution_artifacts.py" in files
    verification = _step(job, "Verify built distribution contracts")
    assert "release-automation/tests/test_distribution_artifacts.py" in verification["run"]
    assert "AVIBE_CORE_WHEEL=" in verification["run"] and "AVIBE_CORE_SDIST=" in verification["run"]
    assert "working-directory" not in verification
    assert job["steps"].index(checkout) < job["steps"].index(verification)
    # Replay the path/ref contract: release source lacks the automation, while
    # the workflow commit supplies it via the declared sparse checkout.
    remote = tmp_path / "remote"
    remote.mkdir()
    _fixture_git(remote, "init")
    source_skill = remote / "skills" / "use-avibe" / "SKILL.md"
    source_skill.parent.mkdir(parents=True)
    source_skill.write_text("Released skill content\n", encoding="utf-8")
    (remote / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    _fixture_git(remote, "add", ".")
    _fixture_git(remote, "commit", "-m", "release source")
    release = _fixture_git(remote, "rev-parse", "HEAD")
    source_skill.write_text("Workflow-only revision\n", encoding="utf-8")
    for name in files:
        target = remote / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "tests/test_distribution_artifacts.py" or (
            workflow_name == "publish.yml"
            and (name.startswith("tests/e2e/") or name == "tests/__init__.py")
        ):
            shutil.copy2(ROOT / name, target)
        else:
            target.write_text("print('workflow-owned automation')\n", encoding="utf-8")
    _fixture_git(remote, "add", ".")
    _fixture_git(remote, "commit", "-m", "workflow automation")
    workflow = _fixture_git(remote, "rev-parse", "HEAD")
    destination = tmp_path / checkout["with"]["path"]
    _fixture_git(tmp_path, "clone", "--no-checkout", str(remote), str(destination))
    _fixture_git(destination, "sparse-checkout", "set", "--no-cone", *files)
    _fixture_git(destination, "checkout", workflow)
    assert _fixture_git(destination, "rev-parse", "HEAD") != release
    for name in files:
        assert (destination / name).is_file()
    assert not (destination / "skills").exists()
    source_checkout = tmp_path / "build-source"
    _fixture_git(tmp_path, "clone", "--no-checkout", str(remote), str(source_checkout))
    _fixture_git(source_checkout, "checkout", release)
    contract = destination / "tests/test_distribution_artifacts.py"
    spec = importlib.util.spec_from_file_location("sparse_distribution_contract", contract)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with monkeypatch.context() as scoped:
        scoped.chdir(source_checkout)
        expected = module._expected_builtin_skills_snapshot()
    assert expected["use-avibe/SKILL.md"]["sha256"] == hashlib.sha256(
        b"Released skill content\n"
    ).hexdigest()
    if workflow_name == "publish.yml":
        upload = _step(job, "Upload GitHub release assets")
        script = "release-automation/scripts/github_release.py"
        assert f"python {script} ensure-draft" in upload["run"]
        result = subprocess.run([sys.executable, str(tmp_path / script), "ensure-draft"],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0 and "workflow-owned automation" in result.stdout
        assert not (tmp_path / "tests/e2e/test_retired_updater_bridge.py").exists()
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/e2e/test_retired_updater_bridge.py"],
            cwd=destination, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=15,
        )
        assert collected.returncode == 0, collected.stdout + collected.stderr
        assert "1 test collected" in collected.stdout


def test_preview_python_assets_build_from_the_desktop_source_sha():
    # The desktop packages are built from the resolved TEST source; the Python
    # assets published beside them must come from that same commit.
    checkout = _step(_job("release_ai.yml", "build-assets"), "Checkout")
    assert checkout["with"]["ref"] == "${{ needs.resolve-desktop-release.outputs.source_sha }}"


@pytest.mark.parametrize("workflow", ["publish.yml", "release_ai.yml"])
def test_release_shell_steps_parse(workflow):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            if "run" not in step or step.get("shell", "bash") not in {"bash", "sh"}:
                continue
            command = re.sub(r"\$\{\{.*?\}\}", "fixture", step["run"])
            result = subprocess.run(["bash", "-n"], input=command, capture_output=True, text=True)
            assert result.returncode == 0, f"{step.get('name')}: {result.stderr}"


@pytest.mark.parametrize("existing", ["none", "same", "different"])
def test_preview_upload_validates_all_existing_bytes_before_runtime_then_packages(tmp_path, existing):
    dist = tmp_path / "dist"
    dist.mkdir()
    names = ["vibe-show-runtime-node-linux-arm64.tgz", "show-runtime-manifest.json",
             "avibe_os-1.0.0-py3-none-any.whl", "avibe_os-1.0.0.tar.gz",
             "avibe_memory-1.0.0-py3-none-any.whl"]
    for name in names:
        (dist / name).write_bytes(b"immutable asset")
    desktop_asset = "desktop-dist/Avibe-1.0.0rc1-macos-aarch64.dmg"
    (tmp_path / "desktop-dist").mkdir()
    (tmp_path / desktop_asset).write_bytes(b"desktop asset")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "calls.jsonl"
    fake = f'''#!{sys.executable}
import json, pathlib, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as stream: stream.write(json.dumps([pathlib.Path(sys.argv[0]).name, *args]) + "\\n")
if args[:2] == ["release", "view"]:
    print({names[-1] if existing != 'none' else ''!r})
elif args[:2] == ["release", "download"]:
    target = pathlib.Path(args[args.index("--dir") + 1]) / args[args.index("--pattern") + 1]
    target.write_bytes({b'immutable asset' if existing == 'same' else b'changed bytes'!r})
'''
    for name in ("gh", "python"):
        path = binaries / name
        path.write_text(fake)
        path.chmod(0o755)
    command = _step(_job("release_ai.yml", "release"), "Create GitHub-only Release")["run"]
    command = command.replace("${{ steps.tag.outputs.tag }}", "gh-v1.0.0rc1").replace("${{ steps.release_type.outputs.prerelease }}", "true")
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path,
                            env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                                 "GITHUB_REPOSITORY": "fixture/repo", "SOURCE_SHA": "0" * 40},
                            capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    uploads = [call for call in calls if call[1:3] == ["release", "upload"]]
    if existing == "different":
        assert result.returncode != 0 and "differs" in result.stderr
        assert uploads == []
        assert not any("finalize" in call for call in calls)
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(uploads) == 2
        assert uploads[0][-2:] == [f"dist/{name}" for name in names[:2]]
        assert uploads[1][6:] == [
            *(f"dist/{name}" for name in names[2:] if existing == "none" or name != names[-1]),
            desktop_asset,
        ]
        assert "finalize" in calls[-1]


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


def test_muc_004_official_publish_gates_real_retired_updater_before_assets_and_pypi():
    """MUC-004: released old wheels, built new wheels, Docker and pytest all gate publication."""
    job = _job("publish.yml", "build")
    bridge = _step(job, "Build and verify inert old-updater bridge")
    consumer = _step(job, "Run release install and upgrade regressions")
    upload = _step(job, "Upload GitHub release assets")
    assert job["steps"].index(bridge) < job["steps"].index(consumer) < job["steps"].index(upload)
    checkout = _step(job, "Checkout release automation")
    assert checkout["with"]["ref"] == "${{ needs.resolve-tag.outputs.workflow_sha }}"
    assert _step(job, "Checkout")["with"]["ref"] == "${{ needs.resolve-tag.outputs.tag }}"
    resolve = _step(_job("publish.yml", "resolve-tag"), "Resolve tag")["run"]
    assert 'if [ "${{ github.event_name }}" = "push" ]; then' in resolve
    assert 'WORKFLOW_SHA="$SOURCE_SHA"' in resolve
    assert set((
        "tests/__init__.py",
        "tests/e2e/test_retired_updater_bridge.py",
        "tests/e2e/test_install_command.py",
        "tests/e2e/retired_updater_probe.py",
        "tests/e2e/github_release_fixture.py",
    )) <= set(checkout["with"]["sparse-checkout"].splitlines())
    assert "build" in _job("publish.yml", "publish-avibe-os")["needs"]
    assert "finalize-github-release" in _job("publish.yml", "publish-avibe-os")["needs"]
    assert consumer["env"]["GH_TOKEN"] == "${{ github.token }}"
    commands = consumer["run"]
    for contract in (
        "tests/e2e/test_retired_updater_bridge.py",
        "AVIBE_RELEASE_GATE=1",
        "AVIBE_CORE_WHEEL=",
        "AVIBE_BRIDGE_WHEEL=",
        "AVIBE_OLD_CORE_WHEEL=",
        "AVIBE_OLD_COMPANION_WHEEL=",
        "gh release download v3.1.0",
        "7c2321ae32174c0fcf0b659c7d7a5b90ae53740c223ae41452702adbc393b924",
        "ce5cfb1473442642d7d19863b6f9131c0a17a1fb6e828d5e0aec04f2deea06b0",
        "sha256sum -c -",
        "docker info",
    ):
        assert contract in commands
    assert "dist/avibe_memory-*-py3-none-any.whl" in upload["run"]
    assert "cd release-automation && pytest tests/e2e/test_retired_updater_bridge.py -v" in commands
    assert "TARGET_VERSION=\"$(python release-automation/scripts/release_package_version.py \"$RELEASE_TAG\")\"" in commands
    assert 'realpath "$VIBE_INSTALL_TEST_WHEEL"' in commands
    assert "realpath old-release/avibe_memory-3.1.0-py3-none-any.whl" in commands


@pytest.mark.parametrize(
    ("tag", "forward"),
    [
        ("v3.0.14", False),
        ("v3.1.0rc1", False),
        ("v3.1.0", False),
        ("v3.1.0.post1", True),
        ("v3.1.1rc1", True),
        ("v3.2.0", True),
        ("v4.0.0", True),
    ],
)
def test_muc_004_retirement_gate_replays_from_workflow_checkout(tmp_path, tag, forward):
    """MUC-004: old tag has no gate; forward tag executes the complete workflow checkout."""
    step = _step(_job("publish.yml", "build"), "Run release install and upgrade regressions")
    gate = "TARGET_VERSION=" + step["run"].split("TARGET_VERSION=", 1)[1]
    automation = tmp_path / "release-automation"
    script = automation / "scripts/release_package_version.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/release_package_version.py", script)
    test = automation / "tests/e2e/test_retired_updater_bridge.py"
    test.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "tests/e2e/test_retired_updater_bridge.py", test)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    (binaries / "python").chmod(0o755)
    mock_pytest = binaries / "pytest"
    mock_pytest.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "assert pathlib.Path(sys.argv[1]).is_file()\n"
        "pathlib.Path(os.environ['GATE_LOG']).write_text(str(pathlib.Path.cwd()))\n"
    )
    mock_pytest.chmod(0o755)
    log = tmp_path / "gate.log"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", gate], cwd=tmp_path,
        env={**os.environ, "RELEASE_TAG": tag, "GATE_LOG": str(log),
             "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.exists() is forward
    if forward:
        assert log.read_text() == str(automation)
    else:
        assert "No forward Memory retirement upgrade" in result.stdout


def test_muc_004_release_gate_cannot_skip_missing_artifacts_or_docker(monkeypatch):
    from tests.e2e import test_retired_updater_bridge as upgrade_bridge

    monkeypatch.setenv("AVIBE_RELEASE_GATE", "1")
    for name in ("AVIBE_CORE_WHEEL", "AVIBE_BRIDGE_WHEEL", "AVIBE_OLD_CORE_WHEEL", "AVIBE_OLD_COMPANION_WHEEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pytest.fail.Exception, match="requires all four"):
        upgrade_bridge.test_released_updater_replaces_companion_without_touching_user_data(Path("/unused"))
    for name in ("AVIBE_CORE_WHEEL", "AVIBE_BRIDGE_WHEEL", "AVIBE_OLD_CORE_WHEEL", "AVIBE_OLD_COMPANION_WHEEL"):
        monkeypatch.setenv(name, "/unused")
    monkeypatch.setattr(upgrade_bridge, "_docker_available", lambda: False)
    with pytest.raises(pytest.fail.Exception, match="requires Docker"):
        upgrade_bridge.test_released_updater_replaces_companion_without_touching_user_data(Path("/unused"))


@pytest.mark.parametrize("workflow_name", ["publish.yml", "release_ai.yml"])
@pytest.mark.parametrize(
    "state",
    ["empty", "identical", "partial", "core-mismatch", "core-sdist-mismatch",
     "runtime-mismatch", "bridge-mismatch", "missing-bridge", "missing-wheel", "empty-sdist", "read-failure"],
)
def test_upload_protects_all_existing_bytes_before_any_write(tmp_path, workflow_name, state):
    workspace = tmp_path / "release source 中文"
    dist = workspace / "dist"
    runtime = workspace / ("runtime-artifacts" if workflow_name == "publish.yml" else "dist")
    binaries = tmp_path / "bin"
    temporary = tmp_path / "temp"
    for path in {workspace, dist, runtime, binaries, temporary}:
        path.mkdir(parents=True, exist_ok=True)
    packages = {
        f"{package}-3.1.0{suffix}": f"{package}{suffix}".encode()
        for package in ("avibe_os",)
        for suffix in ("-py3-none-any.whl", ".tar.gz")
    }
    if workflow_name == "publish.yml":
        # Preserve the optional legacy package upload path too.
        packages["vibe_remote-3.0.14-py3-none-any.whl"] = b"legacy shim"
    packages["avibe_memory-3.1.0-py3-none-any.whl"] = b"inert bridge"
    runtimes = {
        **{f"vibe-show-runtime-node-{platform}.tgz": platform.encode()
           for platform in ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64")},
        "show-runtime-manifest.json": b"show manifest",
    }
    for directory, assets in ((dist, packages), (runtime, runtimes)):
        for name, data in assets.items():
            (directory / name).write_bytes(data)
    desktop = {}
    if workflow_name == "release_ai.yml":
        desktop_dir = workspace / "desktop-dist"
        desktop_dir.mkdir()
        desktop = {"Avibe_3.1.0-rc.1_aarch64-apple-darwin.dmg": b"desktop installer"}
        for name, data in desktop.items():
            (desktop_dir / name).write_bytes(data)
    all_assets = {**packages, **runtimes, **desktop}
    existing = dict(all_assets) if state == "identical" else {}
    if state not in {"empty", "identical"}:
        existing = {name: all_assets[name] for name in (
            "avibe_os-3.1.0-py3-none-any.whl", "avibe_os-3.1.0.tar.gz", "show-runtime-manifest.json",
            "avibe_memory-3.1.0-py3-none-any.whl",
        )}
    mismatch = {
        "core-mismatch": "avibe_os-3.1.0-py3-none-any.whl",
        "core-sdist-mismatch": "avibe_os-3.1.0.tar.gz",
        "runtime-mismatch": "show-runtime-manifest.json",
        "bridge-mismatch": "avibe_memory-3.1.0-py3-none-any.whl",
    }.get(state)
    if mismatch:
        existing[mismatch] = b"already published different bytes"
    if state == "missing-wheel":
        (dist / "avibe_os-3.1.0-py3-none-any.whl").unlink()
    if state == "missing-bridge":
        (dist / "avibe_memory-3.1.0-py3-none-any.whl").unlink()
    if state == "empty-sdist":
        (dist / "avibe_os-3.1.0.tar.gz").write_bytes(b"")

    # Only gh and the metadata helper are simulated; run the complete upload
    # shell, including real file comparisons, globs, temp cleanup and ordering.
    gh = binaries / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        f"existing = {existing!r}\n"
        "with pathlib.Path('events.jsonl').open('a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "if args[1] == 'view':\n"
        f"    sys.exit(1) if {state == 'read-failure'!r} else None\n"
        "    print('\\n'.join(existing))\n"
        "elif args[1] == 'download':\n"
        "    name = args[args.index('--pattern') + 1]\n"
        "    (pathlib.Path(args[args.index('--dir') + 1]) / name).write_bytes(existing[name])\n"
        "elif args[1] == 'upload':\n"
        "    assert '--clobber' not in args\n"
        "    for path in args[args.index('--repo') + 2:]:\n"
        "        assert pathlib.Path(path).name not in existing\n"
        "        assert pathlib.Path(path).is_file()\n"
        "else: raise AssertionError(args)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    python = binaries / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    if workflow_name == "publish.yml":
        command = _step(_job(workflow_name, "build"), "Upload GitHub release assets")["run"]
        command = command.replace("${{ needs.resolve-tag.outputs.tag }}", "v3.1.0")
    else:
        command = _step(_job(workflow_name, "release"), "Create GitHub-only Release")["run"]
        command = command.replace("${{ steps.tag.outputs.tag }}", "gh-v3.1.0")
        command = command.replace("${{ steps.release_type.outputs.prerelease }}", "true")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace, env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                            "TMPDIR": str(temporary), "GITHUB_REPOSITORY": "avibe-bot/avibe", "SOURCE_SHA": "a" * 40},
        capture_output=True, text=True, timeout=20,
    )
    success = state in {"empty", "identical", "partial"}
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    events = [json.loads(line) for line in (workspace / "events.jsonl").read_text().splitlines()]
    uploads = [event for event in events if event[1] == "upload"]
    if not success:
        assert not uploads
    else:
        uploaded = [Path(path).name for event in uploads for path in event[event.index("--repo") + 2:]]
        assert len(uploaded) == len(set(uploaded))
        assert set(uploaded) == set(all_assets) - set(existing)
        if uploads:
            first_upload = events.index(uploads[0])
            assert all(event[1] != "download" for event in events[first_upload:])
        kinds = [name in packages or name in desktop for name in uploaded]
        assert kinds == sorted(kinds), "Runtime uploads must complete before package uploads"
    assert not list(temporary.iterdir())


@pytest.mark.parametrize("tag", ["v3.1.0", "v3.2.0rc1", "gh-v3.2.0rc1"])
@pytest.mark.parametrize("build_result", ["success", "skipped", "failure", "cancelled"])
@pytest.mark.parametrize("desktop_result", ["success", "skipped", "failure", "cancelled"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_notes_skip_unused_official_build_but_never_publish_a_failed_preview(
    tag, build_result, desktop_result, cancelled, event,
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release_ai.yml").read_text())
    jobs = workflow["jobs"]
    preview_condition = "startsWith(github.event.inputs.tag || github.ref_name, 'gh-v')"
    for name in ("resolve-show-runtime-ref",):
        assert jobs[name]["if"] == preview_condition
    assert jobs["show-runtime-bundles"]["needs"] == "resolve-show-runtime-ref"
    assert jobs["build-assets"]["needs"] == ["resolve-desktop-release", "show-runtime-bundles"]
    assert jobs["release"]["needs"] == ["build-assets", "resolve-desktop-release", "desktop-packages"]
    # Evaluate the actual bounded job expression for both event kinds. This
    # catches skipped-needs propagation without replacing the condition itself.
    expression = jobs["release"]["if"].strip().removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("needs.build-assets.result", "build_result")
    expression = expression.replace("needs.desktop-packages.result", "desktop_result")
    expression = expression.replace("needs.resolve-desktop-release.result", "desktop_result")
    expression = expression.replace("github.event.inputs.tag", "input_tag").replace("github.ref_name", "ref")
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    expression = " ".join(expression.split())
    result = eval(expression, {"__builtins__": {}}, {
        "build_result": build_result, "desktop_result": desktop_result, "input_tag": tag if event == "workflow_dispatch" else "",
        "ref": "master" if event == "workflow_dispatch" else tag,
        "cancelled": lambda: cancelled,
        "startsWith": lambda value, prefix: value.startswith(prefix),
    })
    expected = not cancelled and (
        build_result == "success" or (build_result == "skipped" and not tag.startswith("gh-v"))
    ) and (not tag.startswith("gh-v") or desktop_result == "success")
    assert result == expected
