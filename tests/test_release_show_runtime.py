from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("release_ai.yml", "publish.yml")


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow_name", WORKFLOWS)
def test_release_bundle_keeps_native_shell_separate_from_router_export(workflow_name: str) -> None:
    workflow = _workflow(workflow_name)
    job = workflow["jobs"]["show-runtime-bundles"]
    steps = job["steps"]
    build, = [step for step in steps if step.get("name") == "Build Show Runtime bundle"]
    export, = [step for step in steps if step.get("name") == "Export Show Runtime router"]
    upload, = [step for step in steps if step.get("name") == "Upload Show Runtime bundle"]

    # Bash prepends Git for Windows' GNU tar, which reads C:/D: as remote hosts.
    # Windows must keep its default pwsh environment for the existing bundler.
    for owner in (workflow, job):
        assert "shell" not in owner.get("defaults", {}).get("run", {})
    assert "shell" not in build
    assert build["run"].splitlines() == [
        "npm ci",
        "npm run build",
        "npm run bundle:vibe-remote",
        "cp dist/vibe-show-runtime-node-*.tgz .",
        "git rev-parse HEAD > show-runtime-ref-${{ matrix.artifact }}.txt",
    ]
    assert export["shell"] == "bash"
    assert "npm" not in export["run"] and "tar " not in export["run"]
    assert steps.index(build) < steps.index(export) < steps.index(upload)
    assert all(not step.get("if") and not step.get("continue-on-error") for step in (build, export, upload))
    assert set(upload["with"]["path"].splitlines()) == {
        "vibe-show-runtime-node-*.tgz",
        "show-runtime-ref-${{ matrix.artifact }}.txt",
        "show-router-${{ matrix.artifact }}.tsx",
    }
    assert {entry["artifact"] for entry in job["strategy"]["matrix"]["include"]} == {
        "linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64",
    }


@pytest.mark.parametrize("workflow_name", WORKFLOWS)
@pytest.mark.parametrize("template_failure", [False, True])
def test_release_router_export_preserves_bytes_cleanup_and_failure(
    tmp_path: Path, workflow_name: str, template_failure: bool,
) -> None:
    export, = [
        step for step in _workflow(workflow_name)["jobs"]["show-runtime-bundles"]["steps"]
        if step.get("name") == "Export Show Runtime router"
    ]
    assert shutil.which("node"), "Node is required to exercise the release export command"
    temporary = tmp_path / "temporary space 中文"
    temporary.mkdir()
    runtime = tmp_path / "runtime space 中文"
    templates = runtime / "packages/runtime/dist/templates.js"
    templates.parent.mkdir(parents=True)
    (runtime / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    router = "// router 中文\nexport const path = '/nested';\n"
    (runtime / "router-source.tsx").write_text(router, encoding="utf-8")
    templates.write_text(
        'import { mkdir, copyFile } from "node:fs/promises";\n'
        'import { join } from "node:path";\n'
        'export async function ensureSessionTemplate(workspace) {\n'
        '  await mkdir(join(workspace, "src"));\n'
        + ('  throw new Error("fixture template failure");\n' if template_failure else
           '  await copyFile("router-source.tsx", join(workspace, "src/router.tsx"));\n')
        + '}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
         export["run"].replace("${{ matrix.artifact }}", "win32-x64")],
        cwd=runtime,
        env={**os.environ, "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = runtime / "show-router-win32-x64.tsx"
    if template_failure:
        assert result.returncode != 0
        assert "fixture template failure" in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_bytes() == router.encode()
    assert list(temporary.iterdir()) == []
