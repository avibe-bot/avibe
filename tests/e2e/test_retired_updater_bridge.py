"""Opt-in real v3.1.0 pip/uv upgrades in one disposable Linux container."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.e2e.test_install_command import _docker_available


@pytest.mark.integration
def test_released_updater_replaces_companion_without_touching_user_data(tmp_path):
    names = ("AVIBE_CORE_WHEEL", "AVIBE_BRIDGE_WHEEL", "AVIBE_OLD_CORE_WHEEL", "AVIBE_OLD_COMPANION_WHEEL")
    if not all(os.environ.get(name) for name in names):
        pytest.skip("Explicit new and released v3.1.0 artifact paths required")
    if not _docker_available():
        pytest.skip("Docker is required for isolated github.com routing")
    for name in names:
        source = Path(os.environ[name]).resolve()
        target = tmp_path / ("old" if "OLD" in name else "") / source.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
    for name in ("retired_updater_probe.py", "github_release_fixture.py"):
        shutil.copy2(Path(__file__).with_name(name), tmp_path / name)
    command = (
        "apt-get update -qq && apt-get install -y -qq python3-venv ca-certificates openssl >/dev/null && "
        "touch /avibe-bridge-container && python3 -m venv /opt/probe && "
        "/opt/probe/bin/pip -q install uv==0.12.10 && "
        "/opt/probe/bin/python /fixtures/retired_updater_probe.py"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{tmp_path}:/fixtures:ro", "debian:trixie-slim", "bash", "-ec", command],
        capture_output=True, text=True, timeout=1800,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count('"sentinels": "unchanged"') == 4, result.stdout
