"""Docker regression for upgrading the released 3.0.13 wheel to a built wheel."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = os.environ.get("VIBE_UPGRADE_TEST_IMAGE", "debian:trixie-slim")
INITIAL_RELEASE_VERSION = "3.0.13"
INITIAL_RELEASE_WHEEL_URL = (
    "https://github.com/avibe-bot/avibe/releases/download/v3.0.13/avibe_os-3.0.13-py3-none-any.whl"
)
INITIAL_RELEASE_WHEEL_SHA256 = "994adbfd23228ea387f0479db8a4efe0ef121847bd04efa550486203a6b03542"
TEST_RELEASE_VERSION = "9999.0.0"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _build_test_wheel(
    fixtures_dir: Path,
    version: str,
    *,
    project: Path = REPO_ROOT,
    distribution: str = "avibe_os",
) -> Path:
    env = os.environ.copy()
    env["SETUPTOOLS_SCM_PRETEND_VERSION"] = version

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(project),
            "--no-deps",
            "--wheel-dir",
            str(fixtures_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel_path = fixtures_dir / f"{distribution}-{version}-py3-none-any.whl"
    assert wheel_path.exists(), f"Expected built wheel at {wheel_path}"
    return wheel_path


def _released_initial_wheel(fixtures_dir: Path) -> Path:
    wheel_path = fixtures_dir / f"avibe_os-{INITIAL_RELEASE_VERSION}-py3-none-any.whl"
    local_wheel = os.environ.get("VIBE_UPGRADE_INITIAL_WHEEL")
    if local_wheel:
        shutil.copy2(local_wheel, wheel_path)
    else:
        request = urllib.request.Request(INITIAL_RELEASE_WHEEL_URL, headers={"User-Agent": "avibe-upgrade-test"})
        with urllib.request.urlopen(request, timeout=120) as response, wheel_path.open("wb") as destination:
            shutil.copyfileobj(response, destination)

    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    assert digest == INITIAL_RELEASE_WHEEL_SHA256, f"Unexpected {INITIAL_RELEASE_VERSION} wheel digest: {digest}"
    return wheel_path


@pytest.mark.integration
def test_memory_indep_026_upgrade_command_bridges_released_3_0_13_generation():
    """The released bundled-Memory upgrader converges onto the split package pair."""

    if not _docker_available():
        pytest.skip("Docker is not available")

    with tempfile.TemporaryDirectory(prefix="vibe-upgrade-fixtures-") as tmpdir:
        fixtures_dir = Path(tmpdir)
        initial_wheel_path = _released_initial_wheel(fixtures_dir)
        wheel_path = _build_test_wheel(fixtures_dir, TEST_RELEASE_VERSION)
        memory_wheel_path = _build_test_wheel(
            fixtures_dir,
            TEST_RELEASE_VERSION,
            project=REPO_ROOT / "packaging" / "avibe-memory",
            distribution="avibe_memory",
        )
        assert memory_wheel_path.exists()

        memory_payload = {
            "enabled": True,
            "mode": "custom",
            "processing": {
                "llm": {
                    "base_url": "https://llm.example.test/v1",
                    "model": "chat",
                    "api_key": "test-key",
                },
                "embedding": {
                    "base_url": "https://embedding.example.test/v1",
                    "model": "embedding",
                    "api_key": "test-key",
                },
            },
        }
        enable_memory = "from vibe import api; api.save_memory_config(" + repr(memory_payload) + ")"

        metadata_path = fixtures_dir / "metadata.json"
        metadata_path.write_text(json.dumps({"info": {"version": TEST_RELEASE_VERSION}}), encoding="utf-8")

        container_name = f"vibe-upgrade-smoke-{os.getpid()}"
        command = " && ".join(
            [
                "apt-get update >/dev/null",
                "apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null",
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
                'export PATH="$HOME/.local/bin:$PATH"',
                "VIBE_INSTALL_SKIP_NODE=1 VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 "
                f"AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{initial_wheel_path.name} bash /work/install.sh",
                'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"',
                'initial_vibe="$(readlink -f "$HOME/.local/bin/vibe")"',
                'initial_python="$(dirname "$initial_vibe")/python"',
                'printf "%s\\n" "$initial_vibe" | grep "/uv/tools/"',
                "vibe version",
                f'"$initial_python" -c {shlex.quote(enable_memory)}',
                '"$initial_python" -c "from config.v2_config import V2Config; assert V2Config.load().memory.enabled"',
                'printf "preserved\\n" > "$HOME/.avibe/upgrade-state-marker"',
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe check-update",
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe upgrade",
                "hash -r",
                'printf "launcher=%s\n" "$(command -v vibe)"',
                "vibe version",
                'test -x "$initial_vibe"',
                'test "$(cat "$HOME/.avibe/upgrade-state-marker")" = "preserved"',
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json vibe check-update",
                'upgraded_vibe="$(readlink -f "$(command -v vibe)")"',
                'upgraded_python="$(dirname "$upgraded_vibe")/python"',
                '"$upgraded_python" -c "import importlib.util; from config.v2_config import V2Config; '
                "assert V2Config.load().memory.enabled; assert importlib.util.find_spec('avibe_memory') is None\"",
                "export UV_FIND_LINKS=/fixtures",
                "export PIP_FIND_LINKS=/fixtures",
                "vibe",
                "for attempt in $(seq 1 120); do "
                'current_vibe="$(readlink -f "$(command -v vibe)")"; '
                'current_python="$(dirname "$current_vibe")/python"; '
                f'if "$current_python" -c "from importlib.metadata import version; assert version(\'avibe-memory\') == \'{TEST_RELEASE_VERSION}\'" 2>/dev/null; '
                "then break; fi; sleep 1; done",
                '"$current_python" -c "from config.v2_config import V2Config; '
                f"from importlib.metadata import version; assert V2Config.load().memory.enabled; assert version('avibe-os') == '{TEST_RELEASE_VERSION}'; "
                f"assert version('avibe-memory') == '{TEST_RELEASE_VERSION}'\"",
                "sleep 4",
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
    assert "launcher=/root/.local/bin/vibe" in result.stdout
    assert f"avibe-os {TEST_RELEASE_VERSION}" in result.stdout
    assert "You are using the latest version." in result.stdout
    assert '"running": true' in result.stdout
