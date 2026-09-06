"""Upgrade regression helpers and Docker coverage for the released 3.0.13 wheel."""

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

_UPGRADE_WAIT_HELPERS = r"""
report_upgrade_failure() {
    local exit_code="$1"
    local relative_path
    if [ "$exit_code" -eq 0 ]; then
        return 0
    fi

    printf 'Upgrade fixture failed (exit %s); isolated runtime diagnostics:\n' "$exit_code" >&2
    for relative_path in \
        state/memory-package-auto-repair.json \
        runtime/ui_stderr.log runtime/ui_stdout.log \
        runtime/service_stderr.log runtime/service_stdout.log \
        logs/vibe_remote.log; do
        printf '\n--- %s ---\n' "$relative_path" >&2
        if [ -f "$AVIBE_HOME/$relative_path" ]; then
            tail -c 32768 "$AVIBE_HOME/$relative_path" >&2 || true
        else
            printf 'not created\n' >&2
        fi
    done
    return "$exit_code"
}

resolve_vibe_runtime() {
    local launcher_var="$1"
    local resolved_var="$2"
    local python_var="$3"
    local mode="${4:-report}"
    local launcher=""
    local resolved=""
    local python_path=""

    printf -v "$launcher_var" '%s' ""
    printf -v "$resolved_var" '%s' ""
    printf -v "$python_var" '%s' ""

    launcher="$(command -v vibe 2>/dev/null || true)"
    if [ -z "$launcher" ]; then
        VIBE_RUNTIME_DIAGNOSTIC="vibe launcher is unavailable on PATH"
    else
        resolved="$(readlink -f "$launcher" 2>/dev/null || true)"
        if [ -z "$resolved" ]; then
            VIBE_RUNTIME_DIAGNOSTIC="vibe launcher $launcher could not be resolved"
        else
            python_path="$(dirname "$resolved")/python"
            if [ ! -x "$python_path" ]; then
                VIBE_RUNTIME_DIAGNOSTIC="resolved vibe launcher $resolved has no executable Python at $python_path"
            else
                printf -v "$launcher_var" '%s' "$launcher"
                printf -v "$resolved_var" '%s' "$resolved"
                printf -v "$python_var" '%s' "$python_path"
                VIBE_RUNTIME_DIAGNOSTIC=""
                return 0
            fi
        fi
    fi

    if [ "$mode" != "quiet" ]; then
        printf 'Unable to resolve the vibe runtime: %s\n' "$VIBE_RUNTIME_DIAGNOSTIC" >&2
    fi
    return 1
}

memory_runtime_ready() {
    local memory_probe_output=""

    if ! resolve_vibe_runtime current_launcher current_vibe current_python quiet; then
        WAIT_DIAGNOSTIC="$VIBE_RUNTIME_DIAGNOSTIC"
        return 1
    fi
    if memory_probe_output="$(
        "$current_python" -c \
            'import sys; from importlib.metadata import version; assert version("avibe-memory") == sys.argv[1]' \
            "$EXPECTED_AVIBE_VERSION" 2>&1
    )"; then
        WAIT_DIAGNOSTIC=""
        return 0
    fi

    WAIT_DIAGNOSTIC="resolved $current_launcher to $current_python, but the avibe-memory $EXPECTED_AVIBE_VERSION probe failed: ${memory_probe_output:-no output}"
    return 1
}

vibe_service_running() {
    status_output="$(vibe status 2>&1 || true)"
    if printf '%s' "$status_output" | grep -q '"running": true'; then
        WAIT_DIAGNOSTIC=""
        return 0
    fi

    WAIT_DIAGNOSTIC="vibe status did not report running=true: ${status_output:-no output}"
    return 1
}

wait_until() {
    local attempts="$1"
    local interval_seconds="$2"
    local description="$3"
    shift 3
    local attempt

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        WAIT_DIAGNOSTIC=""
        if "$@"; then
            return 0
        fi
        sleep "$interval_seconds"
    done

    printf 'Timed out after %s attempts waiting for %s. Last observation: %s\n' \
        "$attempts" "$description" "${WAIT_DIAGNOSTIC:-no diagnostic available}" >&2
    return 1
}
""".strip()


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


def _run_upgrade_wait_helper(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"{_UPGRADE_WAIT_HELPERS}\n{script}"],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_upgrade_wait_clears_missing_launcher_paths_before_failure() -> None:
    result = _run_upgrade_wait_helper(
        r"""
        sleep() { :; }
        export EXPECTED_AVIBE_VERSION=9999.0.0
        PATH=/path/that/does/not/exist
        current_launcher=stale-launcher
        current_vibe=stale-resolved-launcher
        current_python=./python

        wait_until 1 0 "avibe-memory 9999.0.0 to become available" memory_runtime_ready
        wait_result=$?
        printf 'launcher=<%s> resolved=<%s> python=<%s>\n' \
            "$current_launcher" "$current_vibe" "$current_python"
        exit "$wait_result"
        """
    )

    assert result.returncode == 1
    assert result.stdout == "launcher=<> resolved=<> python=<>\n"
    assert "vibe launcher is unavailable on PATH" in result.stderr
    assert "./python" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("setup", "description", "predicate", "last_observation"),
    [
        (
            """
            export EXPECTED_AVIBE_VERSION=9999.0.0
            memory_probe_fails() { return 1; }
            resolve_vibe_runtime() {
                printf -v "$1" '%s' /tmp/vibe
                printf -v "$2" '%s' /tmp/resolved-vibe
                printf -v "$3" '%s' memory_probe_fails
            }
            """,
            "avibe-memory 9999.0.0 to become available",
            "memory_runtime_ready",
            "the avibe-memory 9999.0.0 probe failed: no output",
        ),
        (
            """
            vibe() { printf '%s\\n' '{"running": false}'; }
            """,
            "vibe service to report running=true",
            "vibe_service_running",
            'vibe status did not report running=true: {"running": false}',
        ),
    ],
)
def test_upgrade_wait_reports_the_last_observation_on_exhaustion(
    setup: str,
    description: str,
    predicate: str,
    last_observation: str,
) -> None:
    result = _run_upgrade_wait_helper(
        f"""
        sleep() {{ :; }}
        {setup}
        wait_until 2 0 {shlex.quote(description)} {predicate}
        """
    )

    assert result.returncode == 1
    assert f"Timed out after 2 attempts waiting for {description}." in result.stderr
    assert last_observation in result.stderr


@pytest.mark.parametrize("exit_code", [0, 1, 7])
def test_upgrade_failure_trap_preserves_exit_and_bounded_runtime_evidence(tmp_path, exit_code):
    state = tmp_path / "state"
    state.mkdir()
    (state / "memory-package-auto-repair.json").write_text('{"reason":"memory_package_install_failed"}')
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "ui_stderr.log").write_text("discarded-prefix" + "x" * 32768 + "startup-failure")
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.json").write_text("do-not-print-config")
    result = _run_upgrade_wait_helper(
        f"export AVIBE_HOME={shlex.quote(str(tmp_path))}\n"
        "trap 'report_upgrade_failure $?' EXIT\n"
        f"(exit {exit_code}) && echo fixture-success"
    )
    assert result.returncode == exit_code
    if exit_code:
        assert "memory_package_install_failed" in result.stderr
        assert "startup-failure" in result.stderr
        assert "not created" in result.stderr
        assert "discarded-prefix" not in result.stderr
        assert "do-not-print-config" not in result.stderr
        assert len(result.stderr) < 34000
    else:
        assert result.stderr == ""


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
                _UPGRADE_WAIT_HELPERS,
                f"export EXPECTED_AVIBE_VERSION={shlex.quote(TEST_RELEASE_VERSION)}",
                "apt-get update >/dev/null",
                "apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null",
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
                'export PATH="$HOME/.local/bin:$PATH"',
                'export AVIBE_HOME="$HOME/.avibe-upgrade-test"',
                "trap 'report_upgrade_failure $?' EXIT",
                "VIBE_INSTALL_SKIP_NODE=1 VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 "
                f"AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{initial_wheel_path.name} bash /work/install.sh",
                'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"',
                "resolve_vibe_runtime initial_launcher initial_vibe initial_python",
                'printf "%s\\n" "$initial_vibe" | grep "/uv/tools/"',
                '"$initial_launcher" version',
                '"$initial_python" -c "from config.v2_config import V2Config; V2Config.default().save()"',
                f'"$initial_python" -c {shlex.quote(enable_memory)}',
                '"$initial_python" -c "from config.v2_config import V2Config; assert V2Config.load().memory.enabled"',
                'printf "preserved\\n" > "$AVIBE_HOME/upgrade-state-marker"',
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe check-update",
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json "
                f"VIBE_INSTALL_SKIP_SHOW_RUNTIME=1 AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{wheel_path.name} vibe upgrade",
                "hash -r",
                "resolve_vibe_runtime upgraded_launcher upgraded_vibe upgraded_python",
                'printf "launcher=%s\n" "$upgraded_launcher"',
                '"$upgraded_launcher" version',
                'test -x "$initial_vibe"',
                'test "$(cat "$AVIBE_HOME/upgrade-state-marker")" = "preserved"',
                "AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json vibe check-update",
                '"$upgraded_python" -c "import importlib.util; from config.v2_config import V2Config; '
                "assert V2Config.load().memory.enabled; assert importlib.util.find_spec('avibe_memory') is None\"",
                "export UV_FIND_LINKS=/fixtures",
                "export PIP_FIND_LINKS=/fixtures",
                '"$upgraded_launcher"',
                f'wait_until 120 1 "avibe-memory {TEST_RELEASE_VERSION} to become available" memory_runtime_ready',
                '"$current_python" -c "from config.v2_config import V2Config; '
                f"from importlib.metadata import version; assert V2Config.load().memory.enabled; assert version('avibe-os') == '{TEST_RELEASE_VERSION}'; "
                f"assert version('avibe-memory') == '{TEST_RELEASE_VERSION}'\"",
                'test "$current_python" != "$upgraded_python"',
                '"$upgraded_python" -c "import certifi, importlib.util; from pathlib import Path; '
                "assert Path(certifi.where()).is_file(); assert importlib.util.find_spec('vibe.api') is not None; "
                "assert importlib.util.find_spec('avibe_memory') is None\"",
                'wait_until 120 1 "vibe service to report running=true" vibe_service_running',
                "printf '%s\\n' \"$status_output\"",
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
                    "/tmp",
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
