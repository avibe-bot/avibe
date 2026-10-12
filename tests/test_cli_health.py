"""Executable evidence, including real supervisor/child boundaries (#2257)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from vibe.cli_health import cli_version, probe_cli


@pytest.mark.parametrize(
    "backend,output,version",
    [
        ("claude", "2.1.34 (Claude Code)\n", "2.1.34"),
        ("claude", "Claude Code 2.1.34", "2.1.34"),
        ("codex", "codex-cli 0.162.1", "0.162.1"),
        ("codex", "codex-cli 0.162.1-alpha.2+build.4", "0.162.1-alpha.2+build.4"),
        ("codex", "codex-cli 0.77.1 (build test)", "0.77.1"),
        ("opencode", "1.2.15\n", "1.2.15"),
        ("opencode", "opencode 1.2.15", "1.2.15"),
        ("opencode", "0.0.0-dev-20261011", "0.0.0-dev-20261011"),
        ("opencode", "local", "local"),
    ],
)
def test_cli_version_shapes(backend, output, version):
    assert cli_version(backend, output, "a harmless stderr warning") == version
    assert cli_version(backend, "", output) == version


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize(
    "output",
    [
        "Error: Missing optional dependency @openai/codex-darwin-arm64\nNode.js v22.18.0",
        "Node.js v22.18.0",
        "v22.18.0",
        "",
        "Python 3.12.0",
        "arbitrary diagnostic 1.2.3",
    ],
)
def test_version_does_not_extract_an_unrelated_semver(backend, output):
    assert cli_version(backend, output, "") is None


def make_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "CLI 空格.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "CLI 空格.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "CLI 空格"
        launcher.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def fixture_env(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PYTHONPATH",
            "TMPDIR",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
        }
        or key.startswith(("AVIBE_TEST_", "VIBE_TEST_"))
    }
    for key in (
        "HOME",
        "AVIBE_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        path = tmp_path / key.lower()
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    env.pop("AVIBE_DESKTOP_RUNTIME_ROOT", None)
    env.pop("AVIBE_DESKTOP_MANAGED_RUNTIME", None)
    return env


@pytest.mark.parametrize(
    "code,stdout,stderr,status,version",
    [
        (0, "codex-cli 0.162.1", "", "ok", "0.162.1"),
        (1, "", "Error: missing platform package\nNode.js v22.18.0 SECRET_SENTINEL", "broken", None),
        (1, "codex-cli 0.162.1", "SECRET_SENTINEL", "broken", None),
        (0, "Node.js v22.18.0", "", "broken", None),
        (0, "", "", "broken", None),
    ],
)
def test_real_cli_probe_exit_status_and_safe_result(tmp_path, code, stdout, stderr, status, version, caplog):
    executable = make_cli(
        tmp_path, f"import sys\nprint({stdout!r})\nprint({stderr!r}, file=sys.stderr)\nsys.exit({code})\n"
    )
    health = asyncio.run(probe_cli("codex", str(executable), env=fixture_env(tmp_path)))
    assert health.status == status and health.version == version
    assert health.found and health.path == str(executable)
    assert "SECRET_SENTINEL" not in json.dumps(health.payload()) + caplog.text


def test_probe_does_not_cache_unchanged_launcher_metadata(tmp_path):
    dependency = tmp_path / "dependency"
    executable = make_cli(
        tmp_path,
        f'import pathlib\nprint("codex-cli 0.162.1" if pathlib.Path({str(dependency)!r}).exists() else "Node.js v22.18.0")\n',
    )
    before = executable.stat()
    env = fixture_env(tmp_path)
    assert asyncio.run(probe_cli("codex", str(executable), env=env)).status == "broken"
    dependency.touch()
    assert asyncio.run(probe_cli("codex", str(executable), env=env)).status == "ok"
    assert (executable.stat().st_mtime_ns, executable.stat().st_size) == (before.st_mtime_ns, before.st_size)


def test_probe_timeout_and_output_cap(tmp_path):
    executable = make_cli(tmp_path, 'import time\nprint("codex-cli 0.162.1", flush=True)\ntime.sleep(60)\n')
    health = asyncio.run(probe_cli("codex", str(executable), env=fixture_env(tmp_path), timeout_seconds=0.5))
    assert health.status == "broken" and health.reason == "probe_timeout" and health.version is None
    executable = make_cli(tmp_path, 'print("codex-cli 0.162.1" + " " * 100_000)\n')
    health = asyncio.run(probe_cli("codex", str(executable), env=fixture_env(tmp_path)))
    assert health.status == "broken" and health.reason == "invalid_version"


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute permission")
def test_non_executable_present_cli_is_not_healthy(tmp_path):
    executable = make_cli(tmp_path, 'print("codex-cli 0.162.1")\n')
    executable.chmod(0o644)
    health = asyncio.run(probe_cli("codex", str(executable), env=fixture_env(tmp_path)))
    assert health.found and health.status == "broken" and health.version is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX descendant liveness; Windows Job Object covered by runner suite")
@pytest.mark.parametrize("cancel", [False, True])
def test_probe_owns_descendants_on_timeout_and_cancellation(tmp_path, cancel):
    import psutil

    pid_file = tmp_path / "child.pid"
    child_code = "import time; time.sleep(60)"
    executable = make_cli(
        tmp_path,
        f'import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,"-c",{child_code!r}])\nPath({str(pid_file)!r}).write_text(str(p.pid))\nprint("codex-cli 0.162.1",flush=True)\n',
    )

    async def run():
        task = asyncio.create_task(
            probe_cli("codex", str(executable), env=fixture_env(tmp_path), timeout_seconds=60 if cancel else 2)
        )
        if cancel:
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.03)
            assert pid_file.exists(), "owned CLI never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "broken" and result.reason == "probe_timeout"

    asyncio.run(run())
    assert pid_file.exists()
    try:
        child = psutil.Process(int(pid_file.read_text()))
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_probe_uses_private_backend_environment_supplier(tmp_path, monkeypatch):
    from vibe import desktop_backends

    executable = make_cli(
        tmp_path,
        'import os\nprint("codex-cli 0.162.1" if os.environ.get("OWNED_PRIVATE_HELPER") == "yes" else "missing helper")\n',
    )
    supplied = []

    def environment(backend, path, base):
        supplied.append((backend, path))
        return {**base, "OWNED_PRIVATE_HELPER": "yes"}

    monkeypatch.setattr(desktop_backends, "desktop_backend_subprocess_environment", environment)
    health = asyncio.run(probe_cli("codex", str(executable), env=fixture_env(tmp_path)))
    assert health.status == "ok" and supplied == [("codex", str(executable))]
