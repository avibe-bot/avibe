"""Run the dependency owner against an offline wheel index, never a live target."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import incus_regression as regression


def _wheel(directory: Path, name: str, version: str) -> None:
    """A real, dependency-free wheel for observing installer behavior offline."""
    module = name.replace("-", "_")
    info = f"{module}-{version}.dist-info"
    files = {
        f"{module}.py": f"VERSION = {version!r}\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: regression-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{info}/RECORD"])
    with zipfile.ZipFile(directory / f"{module}-{version}-py3-none-any.whl", "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


@pytest.fixture
def locked_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    uv = shutil.which("uv")
    if os.name == "nt":
        pytest.skip("Incus host tooling requires POSIX")
    assert uv is not None, "Install scripts/requirements-regression.txt to run offline dependency synchronization tests"
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
        "UV_CACHE_DIR": str(tmp_path / "cache"), "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "1", "PIP_NO_INDEX": "1",
    }
    Path(env["HOME"]).mkdir()
    project = tmp_path / "source"
    project.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    app = tmp_path / "app"
    untouched = tmp_path / "unrelated-environment"
    untouched.mkdir()
    (untouched / "keep").write_text("not the application environment")
    # Explicit command configuration must win over ambient interpreter/env choices.
    env.update(UV_PROJECT_ENVIRONMENT=str(untouched), VIRTUAL_ENV=str(untouched), UV_PYTHON="missing-python")

    def run(args, *, check=True):
        return subprocess.run(args, cwd=project, env=env, text=True, capture_output=True, check=check)

    class LocalRunner:
        def run(self, command, *, check=True, **kwargs):
            return run(command, check=check)

    requirements = Path(regression.__file__).with_name("requirements-regression.txt").read_text()
    pin = next(line.removeprefix("uv==") for line in requirements.splitlines() if line.startswith("uv=="))
    assert run([uv, "--version"]).stdout.split()[1] == pin, "Install scripts/requirements-regression.txt"

    # Keep the installer real. Replace only the Incus transport and independent
    # tool provisioning, so this test cannot address any daemon or product home.
    monkeypatch.setattr(regression, "VENV_DIR", str(app))
    monkeypatch.setattr(regression, "SOURCE_DIR", str(project))
    monkeypatch.setattr(regression, "ensure_regression_uv", lambda *_args, **_kwargs: uv)
    monkeypatch.setattr(regression, "tenant_exec", lambda _target, command, *args, **kwargs: ["bash", "-c", command])
    (project / "ui/dist").mkdir(parents=True)
    (project / "ui/dist/index.html").write_text("test fixture")
    _wheel(wheels, "fixture-dev", "1.0")
    _wheel(wheels, "fixture-orphan", "1.0")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "fixture-app"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["fixture-runtime>=1"]\n'
        '[dependency-groups]\ndev = ["fixture-dev==1.0"]\n'
        f'[[tool.uv.index]]\nurl = {json.dumps(wheels.as_uri())}\nformat = "flat"\ndefault = true\n'
    )
    target = regression.RegressionTarget("worktree", "offline", "unused", "unused", 0, "127.0.0.1", 0)

    def prepare(initial: str, locked: str):
        _wheel(wheels, "fixture-runtime", locked)
        run([uv, "lock", "--python", sys.executable])
        _wheel(wheels, "fixture-runtime", initial)
        # The legacy persistent environment has pip; an unseeded uv venv would
        # instead exercise the old missing-pip bootstrap, not equal fingerprints.
        run([sys.executable, "-m", "venv", str(app)])
        run([uv, "pip", "install", "--python", str(app / "bin/python"), "--no-index", "--find-links", str(wheels),
             f"fixture-runtime=={initial}", "fixture-dev==1.0", "fixture-orphan==1.0"])
        (app / "keep-marker").write_text("existing interpreter environment")
        return (project / "uv.lock").read_bytes()

    def versions():
        code = "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))"
        return json.loads(run([str(app / "bin/python"), "-I", "-c", code]).stdout)

    def sync(*, force_deps=False):
        same = {"python": "legacy-same-lock-fingerprint", "ui_deps": "d", "ui_source": "s"}
        return regression.update_dependencies_and_build(
            LocalRunner(), target, previous_fingerprints=same, next_fingerprints=same,
            force_deps=force_deps, build_ui=False, force_ui=False, remote=None,
        )

    return prepare, sync, versions, project, untouched


@pytest.mark.parametrize(("initial", "locked"), [(None, "2.0"), ("1.0", "2.0"), ("2.0", "1.0"), ("2.0", "2.0")])
def test_existing_environment_converges_to_lock_despite_equal_fingerprints(locked_environment, initial, locked):
    """The old pip path either keeps a within-range version or skips it entirely."""
    prepare, sync, versions, project, untouched = locked_environment
    lock = prepare(initial or locked, locked)
    if initial is None:
        shutil.rmtree(project.parent / "app")
        expected = {"fixture-runtime": locked}
    else:
        assert versions()["fixture-runtime"] == initial
        expected = {"fixture-runtime": locked, "pip": versions()["pip"]}
    assert "python" in sync()

    # uv deliberately retains the venv's pip bootstrap; unselected application
    # and development distributions must still disappear.
    assert versions() == expected
    assert (project / "uv.lock").read_bytes() == lock
    assert not (project / ".venv").exists()
    assert (untouched / "keep").read_text() == "not the application environment"
    # A repeated clean pass must keep the same selected state, not resolve latest.
    sync()
    assert versions() == expected


def test_force_deps_repairs_damaged_files_without_upgrading_the_lock(locked_environment):
    prepare, sync, _versions, project, _untouched = locked_environment
    lock = prepare("1.0", "1.0")
    sync()
    module = next((project.parent / "app").glob("lib/python*/site-packages/fixture_runtime.py"))
    original = module.read_bytes()
    # Break any installer hardlink first: damage the application, not its cache.
    module.unlink()
    module.write_text("damaged")
    sync()
    assert module.read_text() == "damaged"
    sync(force_deps=True)
    assert module.read_bytes() == original
    assert (project / "uv.lock").read_bytes() == lock


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("problem", ["missing", "stale"])
def test_missing_or_stale_lock_fails_without_changing_the_existing_environment(locked_environment, problem, present):
    prepare, sync, versions, project, _untouched = locked_environment
    lock = prepare("1.0", "2.0")
    before = versions()
    app = project.parent / "app"
    if not present:
        shutil.rmtree(app)
    if problem == "missing":
        (project / "uv.lock").unlink()
    else:
        project_file = project / "pyproject.toml"
        project_file.write_text(project_file.read_text().replace("fixture-runtime>=1", "fixture-runtime<2"))

    with pytest.raises(subprocess.CalledProcessError) as failure:
        sync()
    assert "lock --check" in " ".join(failure.value.cmd)
    assert "lockfile" in failure.value.stderr.lower()

    if present:
        assert versions() == before
        assert (app / "keep-marker").read_text() == "existing interpreter environment"
    else:
        assert not app.exists()
    if problem == "missing":
        assert not (project / "uv.lock").exists()
    else:
        assert (project / "uv.lock").read_bytes() == lock
