#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BASE_IMAGE="${VIBE_INSTALL_AUDIT_IMAGE:-debian:trixie-slim}"
PREFIX="${VIBE_INSTALL_AUDIT_PREFIX:-vibe-install-audit}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" - "$REPO_ROOT" "$BASE_IMAGE" "$PREFIX" <<'PY'
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


repo_root = Path(sys.argv[1])
base_image = sys.argv[2]
prefix = sys.argv[3]
python_executable = sys.executable


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 2400) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def ensure_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise SystemExit(f"Required tool not found: {name}")


def build_wheel(dest: Path, version: str) -> Path:
    env = os.environ.copy()
    env["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
    result = run(
        [python_executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(dest)],
        cwd=repo_root,
        env=env,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)

    wheel = dest / f"avibe_os-{version}-py3-none-any.whl"
    if not wheel.exists():
        raise RuntimeError(f"Missing built wheel: {wheel}")
    return wheel


def prepare_show_runtime_manifest() -> None:
    result = run(
        [python_executable, str(repo_root / "scripts" / "prepare_local_show_runtime_manifest.py")],
        cwd=repo_root,
        env=os.environ.copy(),
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)


def build_scenarios(current_name: str, old_name: str, new_name: str) -> list[tuple[str, str]]:
    return [
        (
            "basic-version",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
test "$(command -v vibe)" = "/usr/local/bin/vibe"
vibe version | tee /tmp/vibe-version.log
grep -q '9997.0.0' /tmp/vibe-version.log
""",
        ),
        (
            "start-status",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
command -v vibe
vibe >/tmp/vibe-start.log 2>&1 &
sleep 3
vibe status | tee /tmp/vibe-status.log
grep -q '"running": true' /tmp/vibe-status.log
""",
        ),
        (
            "stale-local-bin",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/vibe" <<'EOF'
#!/usr/bin/env bash
echo "vibe-remote 0.1.0"
EOF
chmod +x "$HOME/.local/bin/vibe"
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
test "$(command -v vibe)" = "/usr/local/bin/vibe"
vibe version | tee /tmp/vibe-version.log
! grep -q '0.1.0' /tmp/vibe-version.log
grep -q '9997.0.0' /tmp/vibe-version.log
""",
        ),
        (
            "relative-path",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
mkdir -p /tmp/case/bin
cd /tmp/case
export PATH="bin:/usr/local/bin:/usr/bin:/bin"
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
test ! -e /tmp/case/bin/vibe
test "$(command -v vibe)" = "/usr/local/bin/vibe"
""",
        ),
        (
            "transient-bins",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
mkdir -p /tmp/.venv/bin /tmp/.pyenv/shims /tmp/.pyenv/versions/3.12.0/bin /tmp/.local/share/mise/installs/python/3.12.1/bin
export VIRTUAL_ENV=/tmp/.venv
export PYENV_ROOT=/tmp/.pyenv
export MISE_DATA_DIR=/tmp/.local/share/mise
export PATH="/tmp/.venv/bin:/tmp/.pyenv/shims:/tmp/.pyenv/versions/3.12.0/bin:/tmp/.local/share/mise/installs/python/3.12.1/bin:/usr/local/bin:/usr/bin:/bin"
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
test ! -e /tmp/.venv/bin/vibe
test ! -e /tmp/.pyenv/shims/vibe
test ! -e /tmp/.pyenv/versions/3.12.0/bin/vibe
test ! -e /tmp/.local/share/mise/installs/python/3.12.1/bin/vibe
test "$(command -v vibe)" = "/usr/local/bin/vibe"
""",
        ),
        (
            "sbin-before-bin",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
export PATH="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"
cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash
test "$(command -v vibe)" = "/usr/local/bin/vibe"
""",
        ),
        (
            "fallback-message",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps passwd >/dev/null
mkdir -p /opt/locked/bin
chmod 0555 /opt/locked /opt/locked/bin || true
useradd -m tester
install_output="$(su - tester -s /bin/bash -c 'export PATH="/opt/locked/bin:/usr/bin:/bin"; cat /repo/install.sh | env AVIBE_INSTALL_PACKAGE_SPEC=/fixtures/{current_name} bash' 2>&1)"
printf '%s\n' "$install_output"
printf '%s' "$install_output" | grep -F '/home/tester/.local/bin:$PATH'
test -x /home/tester/.local/bin/vibe
! su - tester -s /bin/bash -c 'export PATH="/opt/locked/bin:/usr/bin:/bin"; vibe version' >/tmp/fallback-vibe.log 2>&1
""",
        ),
        (
            "upgrade-symlink",
            f"""
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install /fixtures/{old_name} --force
ln -sf "$HOME/.local/bin/vibe" /usr/local/bin/vibe
export PATH="/usr/local/bin:/usr/bin:/bin"
test "$(command -v vibe)" = "/usr/local/bin/vibe"
vibe version | tee /tmp/old-version.log
grep -q '9998.0.0' /tmp/old-version.log
AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{new_name} vibe check-update
AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json AVIBE_UPGRADE_PACKAGE_SPEC=/fixtures/{new_name} vibe upgrade
hash -r
test "$(command -v vibe)" = "/usr/local/bin/vibe"
test "$(readlink /usr/local/bin/vibe)" = "/root/.local/bin/vibe"
vibe version | tee /tmp/new-version.log
grep -q '9999.0.0' /tmp/new-version.log
AVIBE_UPDATE_METADATA_URL=file:///fixtures/metadata.json vibe check-update | tee /tmp/check-update.log
grep -q 'latest version' /tmp/check-update.log
vibe >/tmp/vibe-upgrade-start.log 2>&1 &
sleep 3
vibe status | tee /tmp/vibe-upgrade-status.log
grep -q '"running": true' /tmp/vibe-upgrade-status.log
""",
        ),
        (
            "public-source-smoke",
            """
set -euo pipefail
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates bash procps >/dev/null
cat /repo/install.sh | bash
command -v vibe
vibe version
""",
        ),
    ]


def cleanup(containers: list[str]) -> None:
    for name in containers:
        run(["docker", "rm", "-f", name], timeout=120)


def main() -> int:
    ensure_tool("docker")
    docker_info = run(["docker", "info"], timeout=30)
    if docker_info.returncode != 0:
        print(docker_info.stdout + docker_info.stderr, file=sys.stderr)
        return 1

    created: list[str] = []
    results: list[dict[str, str | int]] = []

    try:
        prepare_show_runtime_manifest()
        with tempfile.TemporaryDirectory(prefix="vibe-install-audit-fixtures-") as tmpdir:
            fixtures = Path(tmpdir)
            current_wheel = build_wheel(fixtures, "9997.0.0")
            old_wheel = build_wheel(fixtures, "9998.0.0")
            new_wheel = build_wheel(fixtures, "9999.0.0")
            (fixtures / "metadata.json").write_text(json.dumps({"info": {"version": "9999.0.0"}}), encoding="utf-8")

            scenarios = build_scenarios(current_wheel.name, old_wheel.name, new_wheel.name)

            for name, script in scenarios:
                container_name = f"{prefix}-{name}"
                created.append(container_name)
                result = run(
                    [
                        "docker",
                        "run",
                        "--name",
                        container_name,
                        "--rm",
                        "-v",
                        f"{repo_root}:/repo",
                        "-v",
                        f"{fixtures}:/fixtures",
                        "-w",
                        "/repo",
                        base_image,
                        "bash",
                        "-lc",
                        script,
                    ],
                    timeout=3600,
                )
                results.append(
                    {
                        "name": name,
                        "returncode": result.returncode,
                        "stdout_tail": "\n".join(result.stdout.splitlines()[-20:]),
                        "stderr_tail": "\n".join(result.stderr.splitlines()[-20:]),
                    }
                )
    finally:
        cleanup(created)

    failures = [item["name"] for item in results if item["returncode"] != 0]
    print(json.dumps({"results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


raise SystemExit(main())
PY
