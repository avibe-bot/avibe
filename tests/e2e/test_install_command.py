"""Docker smoke test for the README one-command install flow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = os.environ.get("VIBE_INSTALL_TEST_IMAGE", "debian:trixie-slim")
PUBLIC_INSTALL_COMMAND = "curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch"


def _resolve_install_wheel(fixtures_dir: Path) -> Path:
    configured_wheel = os.environ.get("AVIBE_INSTALL_TEST_WHEEL") or os.environ.get("VIBE_INSTALL_TEST_WHEEL")
    if configured_wheel:
        wheel_path = Path(configured_wheel).resolve()
        assert wheel_path.exists(), f"Expected install test wheel at {wheel_path}"
        return wheel_path

    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(fixtures_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = sorted(fixtures_dir.glob("avibe_os-*.whl"))
    assert wheels, "Expected a built wheel for install test"
    return wheels[-1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.integration
def test_install_command_starts_vibe_for_new_user_without_local_bin_on_path():
    for readme in (REPO_ROOT / "README.md", REPO_ROOT / "README_ZH.md"):
        assert PUBLIC_INSTALL_COMMAND in readme.read_text(encoding="utf-8")

    if not _docker_available():
        pytest.skip("Docker is not available")

    with tempfile.TemporaryDirectory(prefix="vibe-install-fixtures-") as tmpdir:
        fixtures_dir = Path(tmpdir)
        wheel_path = _resolve_install_wheel(fixtures_dir)
        fixtures_dir.chmod(0o755)
        wheel_path.chmod(0o644)

        container_name = f"vibe-install-smoke-{os.getpid()}"
        local_install_command = PUBLIC_INSTALL_COMMAND.replace(
            "curl -fsSL https://avibe.bot/install.sh",
            "cat /work/install.sh",
        )
        command = (
            "apt-get update >/dev/null && "
            "apt-get install -y --no-install-recommends curl ca-certificates bash procps passwd >/dev/null && "
            "useradd -m -s /bin/bash installer && "
            "su - installer -s /bin/bash -c '"
            "set -euo pipefail; "
            "export PATH=/usr/bin:/bin; "
            "export VIBE_INSTALL_SKIP_NODE=1; "
            "export VIBE_INSTALL_SKIP_SHOW_RUNTIME=1; "
            f"export AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{wheel_path.name}; "
            f"{local_install_command}; "
            "test \"$PATH\" = /usr/bin:/bin; "
            "! command -v vibe; "
            "/home/installer/.local/bin/vibe version; "
            "sleep 2; "
            "/home/installer/.local/bin/vibe status'"
        )

        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--name",
                    container_name,
                    "--rm",
                    "-v",
                    f"{REPO_ROOT}:/work",
                    "-v",
                    f"{wheel_path.parent}:/fixtures",
                    "-w",
                    "/work",
                    BASE_IMAGE,
                    "bash",
                    "-lc",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installing vibe command into /home/installer/.local/bin" in result.stdout
    assert "avibe-os installed successfully (from custom package spec)" in result.stdout
    assert "Launching Avibe with /home/installer/.local/bin/vibe" in result.stdout
    assert "Avibe launched" in result.stdout
    assert "avibe-os " in result.stdout
    assert "Web UI:" in result.stdout
    assert '"running": true' in result.stdout
    assert '"service_pid":' in result.stdout
