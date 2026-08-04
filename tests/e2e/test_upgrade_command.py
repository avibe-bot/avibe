"""Docker smoke test for release-style upgrade flow using a built wheel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.e2e.wheel_build import build_wheel


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = os.environ.get("VIBE_UPGRADE_TEST_IMAGE", "debian:trixie-slim")
INITIAL_RELEASE_VERSION = "9998.0.0"
TEST_RELEASE_VERSION = "9999.0.0"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _build_test_wheel(fixtures_dir: Path, version: str) -> Path:
    return build_wheel(fixtures_dir, version=version)


@pytest.mark.integration
def test_upgrade_command_uses_built_release_artifact():
    if not _docker_available():
        pytest.skip("Docker is not available")

    with tempfile.TemporaryDirectory(prefix="vibe-upgrade-fixtures-") as tmpdir:
        fixtures_dir = Path(tmpdir)
        initial_wheel_path = _build_test_wheel(fixtures_dir, INITIAL_RELEASE_VERSION)
        wheel_path = _build_test_wheel(fixtures_dir, TEST_RELEASE_VERSION)

        metadata_path = fixtures_dir / "metadata.json"
        metadata_path.write_text(json.dumps({"info": {"version": TEST_RELEASE_VERSION}}), encoding="utf-8")

        container_name = f"vibe-upgrade-smoke-{os.getpid()}"
        command = " && ".join(
            [
                "apt-get update >/dev/null",
                "apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null",
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
                'export PATH="$HOME/.local/bin:$PATH"',
                f"uv tool install /fixtures/{initial_wheel_path.name} --force",
                'ln -sf "$HOME/.local/bin/vibe" /usr/local/bin/vibe',
                'export PATH="/usr/local/bin:/usr/bin:/bin"',
                "vibe version",
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe check-update",
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe upgrade",
                "hash -r",
                'printf "launcher=%s\n" "$(command -v vibe)"',
                "vibe version",
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json vibe check-update",
                "vibe",
                "sleep 2",
                "vibe status",
            ]
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
                    f"{fixtures_dir}:/fixtures",
                    "-w",
                    "/work",
                    BASE_IMAGE,
                    "bash",
                    "-lc",
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=1200,
            )
        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"avibe-os {INITIAL_RELEASE_VERSION}" in result.stdout
    assert "New version available: 9999.0.0" in result.stdout
    assert "Upgrade successful!" in result.stdout
    assert "launcher=/usr/local/bin/vibe" in result.stdout
    assert f"avibe-os {TEST_RELEASE_VERSION}" in result.stdout
    assert "You are using the latest version." in result.stdout
    assert '"running": true' in result.stdout
