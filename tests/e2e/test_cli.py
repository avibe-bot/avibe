"""E2E tests: CLI commands executed inside the Docker container."""

import os
import subprocess

COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.e2e.yml")


def _docker_exec(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command inside the running vibe container."""
    compose_file = os.path.abspath(COMPOSE_FILE)
    env = os.environ.copy()
    env["VIBE_E2E_PORT"] = os.environ.get("VIBE_E2E_PORT", "15123")
    return subprocess.run(
        ["docker", "compose", "-f", compose_file, "exec", "-T", "vibe", "bash", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


class TestCLIVersion:
    """vibe version command."""

    def test_version_output(self, vibe_container):
        result = _docker_exec("vibe version")
        assert result.returncode == 0
        assert "vibe-remote" in result.stdout


class TestCLIDoctor:
    """vibe doctor command."""

    def test_doctor_runs(self, vibe_container):
        result = _docker_exec("vibe doctor")
        # Doctor may exit 0 (all ok) or 1 (some failures) - both are valid
        assert result.returncode in (0, 1)
        assert "Diagnostics" in result.stdout


class TestCLIStatus:
    """vibe status command."""

    def test_status_runs(self, vibe_container):
        result = _docker_exec("vibe status")
        assert result.returncode == 0
        # Should output JSON status
        assert "{" in result.stdout


class TestCLIPackageInstalled:
    """Verify the package is properly installed."""

    def test_vibe_importable(self, vibe_container):
        result = _docker_exec("python -c 'import vibe; print(vibe.__version__)'")
        assert result.returncode == 0
        assert result.stdout.strip()  # Should print a version string

    def test_config_paths_uses_env(self, vibe_container):
        """AVIBE_HOME env var should control the base directory."""
        result = _docker_exec("python -c 'from config.paths import get_vibe_remote_dir; print(get_vibe_remote_dir())'")
        assert result.returncode == 0
        assert "/data/avibe" in result.stdout
