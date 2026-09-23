"""Independent archive and release-consumer checks for the inert bridge."""

import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from tests.test_release_verification import ROOT, WORKFLOWS, _job, _step


@pytest.mark.parametrize("tag,version", [("v3.2.0", "3.2.0"), ("gh-v3.2.1rc1", "3.2.1rc1")])
def test_bridge_is_only_valid_metadata_and_rebuilds_identically(tmp_path, tag, version):
    command = [sys.executable, str(ROOT / "scripts/build_retired_companion.py"),
               "--tag", tag, "--output-dir", str(tmp_path)]
    subprocess.run(command, check=True, capture_output=True)
    wheel = tmp_path / f"avibe_memory-{version}-py3-none-any.whl"
    before = wheel.read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        directory = f"avibe_memory-{version}.dist-info"
        assert set(archive.namelist()) == {f"{directory}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
        metadata = BytesParser().parsebytes(archive.read(f"{directory}/METADATA"))
        assert metadata["Name"] == "avibe-memory" and metadata["Version"] == version
        assert metadata.get_all("Requires-Dist", []) == []
        assert metadata.get_all("Provides-Extra", []) == []
        wheel_metadata = BytesParser().parsebytes(archive.read(f"{directory}/WHEEL"))
        assert wheel_metadata["Tag"] == "py3-none-any"
        assert wheel_metadata["Root-Is-Purelib"] == "true"
        records = list(csv.reader(io.StringIO(archive.read(f"{directory}/RECORD").decode())))
        assert {row[0] for row in records} == set(archive.namelist())
        for name, digest, size in records:
            if name.endswith("/RECORD"):
                assert (digest, size) == ("", "")
            else:
                data = archive.read(name)
                assert int(size) == len(data)
                assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    subprocess.run(command, check=True, capture_output=True)
    subprocess.run([*command, "--verify"], check=True, capture_output=True)
    assert wheel.read_bytes() == before
    wheel.write_bytes(before + b"tampered")
    for suffix in ([], ["--verify"]):
        result = subprocess.run([*command, *suffix], capture_output=True, text=True)
        assert result.returncode != 0
        assert wheel.read_bytes() == before + b"tampered"


@pytest.mark.parametrize("workflow,job_name,checkout_name,workflow_ref", WORKFLOWS)
def test_release_builds_exact_bridge_before_upload_from_workflow_owned_tooling(
    tmp_path, workflow, job_name, checkout_name, workflow_ref,
):
    job = _job(workflow, job_name)
    checkout = _step(job, checkout_name)
    build = _step(job, "Build and verify inert old-updater bridge")
    upload = _step(job, "Upload artifacts" if workflow == "publish.yml" else "Upload release artifacts")
    assert job["steps"].index(checkout) < job["steps"].index(build) < job["steps"].index(upload)
    assert not build.get("if") and not build.get("continue-on-error")
    destination = tmp_path / "release-automation/scripts"
    destination.mkdir(parents=True)
    for script in ("build_retired_companion.py", "release_package_version.py"):
        assert f"scripts/{script}" in checkout["with"]["sparse-checkout"].splitlines()
        shutil.copy2(ROOT / "scripts" / script, destination)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    tag = "v3.2.0" if workflow == "publish.yml" else "gh-v3.2.0rc1"
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", build["run"]], cwd=tmp_path,
                   env={**os.environ, "RELEASE_TAG": tag, "PATH": f"{binaries}:{os.environ['PATH']}"}, check=True)
    assert [path.name for path in (tmp_path / "dist").iterdir()] == [
        f"avibe_memory-{tag.removeprefix('gh-').removeprefix('v')}-py3-none-any.whl",
    ]
