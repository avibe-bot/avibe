from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ("release_ai.yml", "build-assets", "Checkout release verification", "${{ github.sha }}"),
    ("publish.yml", "build", "Checkout release automation", "${{ needs.resolve-tag.outputs.workflow_sha }}"),
)


@pytest.mark.parametrize("failure", [None, "missing-wheel", "missing-sdist", "mismatch", "unpublished"])
def test_public_companion_gate_checks_both_exact_assets_without_auth(tmp_path, failure):
    workspace = tmp_path / "public release 中文"
    dist = workspace / "dist"
    scripts = workspace / "scripts"
    binaries = tmp_path / "bin"
    runtime_tmp = tmp_path / "runner temp"
    for directory in (dist, scripts, binaries, runtime_tmp):
        directory.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/release_package_version.py", scripts)
    assets = {
        "avibe_memory-3.1.1-py3-none-any.whl": b"companion wheel",
        "avibe_memory-3.1.1.tar.gz": b"companion sdist",
    }
    for name, data in assets.items():
        (dist / name).write_bytes(data)
    if failure in {"missing-wheel", "missing-sdist"}:
        suffix = ".whl" if failure == "missing-wheel" else ".tar.gz"
        next(dist.glob(f"*{suffix}")).unlink()
    (binaries / "python").symlink_to(sys.executable)
    curl = binaries / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert not any('authorization' in arg.lower() for arg in args)\n"
        "url = args[-1]\n"
        "assert url.startswith('https://github.com/avibe-bot/avibe/releases/download/v3.1.1/')\n"
        "with pathlib.Path('fetches.jsonl').open('a') as log: log.write(json.dumps(url) + '\\n')\n"
        f"sys.exit(22) if {failure == 'unpublished'!r} else None\n"
        "asset = url.rsplit('/', 1)[1]\n"
        f"data = {assets!r}[asset]\n"
        f"data = b'wrong' if {failure == 'mismatch'!r} else data\n"
        "pathlib.Path(args[args.index('--output') + 1]).write_bytes(data)\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    command = _step(
        _job("publish.yml", "verify-avibe-memory-release"),
        "Verify public GitHub companion distributions",
    )["run"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runtime_tmp),
            "RELEASE_TAG": "v3.1.1",
            "GITHUB_REPOSITORY": "avibe-bot/avibe",
        },
        capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    assert not list(runtime_tmp.iterdir())
    if failure is None:
        fetches = [json.loads(line) for line in (workspace / "fetches.jsonl").read_text().splitlines()]
        assert {url.rsplit("/", 1)[1] for url in fetches} == set(assets)
    for path in dist.iterdir():
        assert path.read_bytes() == assets[path.name]


def _job(workflow_name: str, job_name: str) -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    return workflow["jobs"][job_name]


def _step(job: dict, name: str) -> dict:
    step, = [step for step in job["steps"] if step.get("name") == name]
    return step


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
def test_release_verification_is_workflow_owned_after_tagged_artifacts_are_built(
    workflow_name: str, job_name: str, checkout_name: str, workflow_ref: str,
) -> None:
    job = _job(workflow_name, job_name)
    steps = job["steps"]
    source = _step(job, "Checkout")
    checkout = _step(job, checkout_name)
    build = _step(job, "Build Python distributions")
    verify = _step(job, "Verify distribution package matrix")

    expected_source_ref = (
        "${{ github.event.inputs.tag || github.ref }}" if workflow_name == "release_ai.yml"
        else "${{ needs.resolve-tag.outputs.tag }}"
    )
    assert source["with"]["ref"] == expected_source_ref
    assert checkout["uses"] == source["uses"]
    assert checkout["with"]["ref"] == workflow_ref
    assert checkout["with"]["path"] == "release-automation"
    assert checkout["with"]["sparse-checkout-cone-mode"] is False
    expected_files = {"tests/test_memory_distribution.py"}
    if workflow_name == "publish.yml":
        expected_files.add("scripts/github_release.py")
    assert set(checkout["with"]["sparse-checkout"].splitlines()) == expected_files
    assert steps.index(source) < steps.index(build) < steps.index(checkout) < steps.index(verify)
    assert all(not step.get("if") and not step.get("continue-on-error") for step in (checkout, verify))
    commands = [
        line for line in verify["run"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands == [
        "cp release-automation/tests/test_memory_distribution.py tests/test_memory_distribution.py",
        'AVIBE_CORE_WHEEL="$(ls dist/avibe_os-*.whl)" \\',
        'AVIBE_MEMORY_WHEEL="$(ls dist/avibe_memory-*.whl)" \\',
        "  pytest tests/test_memory_distribution.py -v",
    ]
    for later in steps[steps.index(checkout) + 1:]:
        assert "python -m build" not in later.get("run", "")
    upload_name = "Upload release artifacts" if workflow_name == "release_ai.yml" else "Upload GitHub release assets"
    assert steps.index(verify) < steps.index(_step(job, upload_name))


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
@pytest.mark.parametrize("failure", [None, "missing-verifier", "test-failure"])
def test_exact_matrix_command_uses_new_verifier_without_changing_artifacts(
    tmp_path: Path, workflow_name: str, job_name: str, checkout_name: str, workflow_ref: str,
    failure: str | None,
) -> None:
    workspace = tmp_path / "tagged source 中文"
    tests = workspace / "tests"
    automation_tests = workspace / "release-automation" / "tests"
    dist = workspace / "dist"
    for directory in (tests, automation_tests, dist):
        directory.mkdir(parents=True, exist_ok=True)
    original = b"# original tagged verification\n"
    corrected = b"# corrected workflow-owned verification\n"
    (tests / "test_memory_distribution.py").write_bytes(original)
    if failure != "missing-verifier":
        (automation_tests / "test_memory_distribution.py").write_bytes(corrected)
    artifacts = {
        "avibe_os-3.1.0-py3-none-any.whl": b"core built from immutable source",
        "avibe_memory-3.1.0-py3-none-any.whl": b"companion built from immutable source",
        "avibe_os-3.1.0.tar.gz": b"core sdist",
        "avibe_memory-3.1.0.tar.gz": b"companion sdist",
    }
    for name, data in artifacts.items():
        (dist / name).write_bytes(data)
    source_file = workspace / "source.py"
    source_file.write_bytes(b"# unchanged runtime code\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    pytest_stub = binaries / "pytest"
    pytest_stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path('invocation.json').write_text(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'core': os.environ['AVIBE_CORE_WHEEL'],\n"
        "    'memory': os.environ['AVIBE_MEMORY_WHEEL'],\n"
        "    'test': pathlib.Path(sys.argv[1]).read_text(),\n"
        "}))\n"
        f"sys.exit({1 if failure == 'test-failure' else 0})\n",
        encoding="utf-8",
    )
    pytest_stub.chmod(0o755)
    command = _step(_job(workflow_name, job_name), "Verify distribution package matrix")["run"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace,
        env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in dist.iterdir()} == artifacts
    assert source_file.read_bytes() == b"# unchanged runtime code\n"
    receipt = workspace / "invocation.json"
    if failure == "missing-verifier":
        assert not receipt.exists()
        assert (tests / "test_memory_distribution.py").read_bytes() == original
    else:
        assert json.loads(receipt.read_text()) == {
            "args": ["tests/test_memory_distribution.py", "-v"],
            "core": "dist/avibe_os-3.1.0-py3-none-any.whl",
            "memory": "dist/avibe_memory-3.1.0-py3-none-any.whl",
            "test": corrected.decode(),
        }
