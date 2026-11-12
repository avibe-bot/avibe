"""Pinned released-callers, not reimplementations of v3.2.5 selection rules.

The native HTTP child is a fixture, not packaged CPA or real provider OAuth.
Run with the pinned commit available (CI fetches that exact object explicitly).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from tests.model_hub_engine_test_environment import child_environment
from tests.test_model_hub_engine_lifecycle import fixture_manifest, signed_candidate
from tests.test_model_hub_engine_processes import finish, real_owner

RELEASED_V325 = "ea402b8bc3ab5f7f29cc85b28f22aed17544cc2d"
pytestmark = pytest.mark.integration  # dedicated native CI fetches this pinned source


@pytest.fixture(scope="module")
def released_v325(tmp_path_factory):
    root = tmp_path_factory.mktemp("released-v325")
    archive = root / "source.tar"
    result = subprocess.run([
        "git", "archive", "--format=tar", "--output", str(archive), RELEASED_V325,
        "core", "vibe", "config", "storage", "modules", "pyproject.toml",
    ], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Pinned released-caller source is required; fetch {RELEASED_V325} before this test: {result.stderr}"
    )
    source = root / "source"
    source.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(source, filter="data")
    return source


def run_released(tmp_path, released, owner, supervisor, *, operation, startup):
    old_raw = fixture_manifest(tmp_path / "old-package", version="v8.0.63")
    manifest = tmp_path / "old-package-manifest.json"
    manifest.write_bytes(old_raw)
    # Only ordinary prepared demand gets its old bundle archive. OAuth/reauth
    # variants have neither B_old nor a successful prepare, by construction.
    if operation == "prepared_demand":
        asset = json.loads(old_raw)["assets"][0]
        name = "engine.zip" if asset["platform"].startswith("windows") else "engine.tar.gz"
        shutil.copyfile(tmp_path / "old-package" / name, owner.manager.runtime_dir / "downloads" / name)
    result = tmp_path / "released-result.json"
    spec = {
        "released_source": str(released), "runtime": str(owner.manager.runtime_dir),
        "state": str(supervisor.state_store.root), "service": str(tmp_path / "released-service"),
        "fixture_engine": str(tmp_path / "fixture-cpa.py"), "old_package_manifest": str(manifest),
        "operation": operation, "startup": startup, "result": str(result),
    }
    spec_path = tmp_path / "released-spec.json"
    spec_path.write_text(json.dumps(spec))
    environment = child_environment(tmp_path / "released-home", released)
    driver = Path(__file__).with_name("model_hub_released_caller_driver.py")
    process = subprocess.run(
        [sys.executable, str(driver), str(spec_path)], cwd=released, env=environment,
        capture_output=True, text=True, timeout=45,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(result.read_text())


@pytest.mark.parametrize("hint", ["absent", "actual_b_new"])
@pytest.mark.parametrize("startup", ["pending", "failed"])
@pytest.mark.parametrize("operation", ["oauth_start", "reauth_source"])
def test_released_oauth_before_prepare_never_starts_c_and_upgrade_back_keeps_c(
    tmp_path, monkeypatch, released_v325, hint, startup, operation,
):
    owner, supervisor, baseline, observations, processes = real_owner(
        tmp_path, monkeypatch, bundled_version="v8.0.64",
    )
    try:
        candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path, version="v8.0.65")
        owner.activate(candidate)
        assert owner.running_release is None  # preflight-only commit stays stopped
        if hint == "absent":
            (owner.manager.runtime_dir / "current.json").unlink()
            shutil.rmtree(baseline["install_dir"])
        result = run_released(
            tmp_path, released_v325, owner, supervisor, operation=operation, startup=startup,
        )
        assert result["prepare_succeeded"] is False and result["rpc_error"] is not None
        assert all(candidate.install_dir not in item for item in result["launches"])
        if hint == "absent":
            assert result["launches"] == [] and result["selected_path"] is None
        else:
            assert result["launches"] == [baseline["path"]]
            assert result["selected_path"] == baseline["path"]
        # The old process was reaped before returning. Upgrade-back uses the
        # actual new supervisor demand path, not its old compatibility hint.
        owner.recover()
        owner.ensure_running()
        assert owner.running_release == candidate.asset_release_tag
        assert owner.manager.required_generation() == candidate
        assert owner.journal.load()["active"] == candidate.document()
        real_starts = [item for item in observations if not item["isolated"]]
        assert real_starts[-1]["binary"] == Path(candidate.install_dir) / candidate.bin_path
    finally:
        finish(supervisor, processes)


def test_released_successfully_prepared_demand_can_run_b_old_without_repinning_upgrade_back(
    tmp_path, monkeypatch, released_v325,
):
    owner, supervisor, _baseline, _observations, processes = real_owner(
        tmp_path, monkeypatch, bundled_version="v8.0.64",
    )
    try:
        candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path, version="v8.0.65")
        owner.activate(candidate)
        result = run_released(tmp_path, released_v325, owner, supervisor, operation="prepared_demand", startup="none")
        assert result["prepare_succeeded"] and len(result["launches"]) == 1
        assert result["selected_path"] == result["launches"][0]
        assert "8.0.63" in result["selected_path"]
        assert candidate.install_dir not in result["selected_path"]
        owner.recover()
        owner.ensure_running()
        assert owner.running_release == candidate.asset_release_tag
        assert owner.journal.load()["active"] == candidate.document()
    finally:
        finish(supervisor, processes)
