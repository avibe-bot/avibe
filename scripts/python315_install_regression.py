#!/usr/bin/env python3
"""Real CPython 3.15 wheel installation and unmodified public 3.2.5 upgrade.

Only PyYAML (no cp315 wheel) and http-ece (pure-Python, sdist-only) may build
from source. All state, launchers and caches belong to this invocation. No service
is started. Candidate constraints bootstrap the old client's dependencies only;
the old upgrade receives neither constraints nor new-client Python flags. On
Windows it uses the unchanged module entry point because 3.2.5's console launcher
has a pre-existing self-replacement bug; it does not prove that vibe.exe path.
"""
from __future__ import annotations

import argparse
from email import message_from_bytes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

SOURCE_EXCEPTIONS = {"pyyaml", "http-ece"}


def run(args, env, cwd, *, timeout=600):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args[0]} {args[1:3]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def isolated_env(root: Path, uv: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("AVIBE_", "VIBE_", "UV_", "PIP_", "PYTHON", "XDG_"))
        and k.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    for key, suffix in {
        "HOME": "home", "USERPROFILE": "home", "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache", "XDG_DATA_HOME": "data", "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata", "UV_CACHE_DIR": "cache/uv", "UV_TOOL_DIR": "uv/tools",
        "UV_TOOL_BIN_DIR": "bin", "UV_PYTHON_INSTALL_DIR": "python", "AVIBE_HOME": "avibe",
    }.items():
        directory = root / suffix
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory)
    # Old clients discover both their stable launcher and uv through PATH. Never
    # allow that lookup to choose the owner's launcher; the fixture bin is first.
    env["PATH"] = os.pathsep.join([str(root / "bin"), str(Path(sys.executable).parent), str(Path(uv).parent), env.get("PATH", "")])
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["VIBE_INSTALL_SKIP_SHOW_RUNTIME"] = "1"
    env["VIBE_INSTALL_SKIP_NODE"] = "1"
    return env


def launcher(root: Path) -> Path:
    return root / "bin" / ("vibe.exe" if os.name == "nt" else "vibe")


def active_python(root: Path) -> Path:
    """Find the environment encoded by the *current* launcher, not an old tool."""
    stable = launcher(root)
    payload = stable.read_bytes()
    # Windows may copy/hardlink rather than symlink the launcher's executable.
    # Matching its bytes matches its embedded interpreter path as well.
    for entry in root.rglob(stable.name):
        if entry == stable or not entry.is_file() or entry.read_bytes() != payload:
            continue
        for name in ("python.exe", "python", "python3"):
            candidate = entry.parent / name
            if candidate.is_file():
                return candidate
    raise RuntimeError(f"No environment matches the active fixture launcher {stable}")


VERIFY = r'''
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
from packaging.markers import default_environment
from packaging.requirements import Requirement
assert sys.version_info[:2] == (3, 15), sys.version
assert metadata.version('avibe-os') == sys.argv[2], metadata.version('avibe-os')
expected = {}
for raw in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    line = raw.strip().removesuffix('\\').strip()
    if not line or line.startswith(('#', '--hash=')):
        continue
    req = Requirement(line)
    if req.marker is not None and not req.marker.evaluate(default_environment()):
        continue
    pins = list(req.specifier)
    assert len(pins) == 1 and pins[0].operator == '==', line
    assert metadata.version(req.name) == pins[0].version, (line, metadata.version(req.name))
    expected[req.name] = pins[0].version
# Import the native modules whose old locks could not run on 3.15, not only
# their metadata. greenlet/uvloop/pywin32 are platform-conditional.
import pydantic_core, watchfiles, httptools, markupsafe, rpds, yaml
if 'greenlet' in expected:
    import greenlet
if 'uvloop' in expected:
    import uvloop
if 'pywin32' in expected:
    import win32api
print(json.dumps({'python': sys.version, 'version': metadata.version('avibe-os'), 'active_pins': len(expected)}))
'''


def wait_for_activation(root: Path, *, timeout: float = 60) -> None:
    # The owner atomically writes this marker *after* replacing the launcher.
    # Never execute (or repeatedly open) vibe.exe while its replacement is
    # pending: Windows would deny the helper's one-shot os.replace.
    stable = launcher(root)
    marker = stable.parent / f".{stable.name}.avibe-generation"
    deadline = time.monotonic() + timeout
    while True:
        try:
            generation = Path(marker.read_text(encoding="utf-8-sig").strip())
            if generation.is_relative_to(root) and generation.is_dir():
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("The old client's activation helper did not publish its generation marker")
        time.sleep(0.25)


def verify(root: Path, env: dict[str, str], constraints: Path, version: str) -> None:
    python = active_python(root)
    print(run([str(python), "-I", "-c", VERIFY, str(constraints), version], env, root), flush=True)
    output = run([str(launcher(root)), "version"], env, root)
    if version not in output:
        raise RuntimeError(f"The stable launcher did not activate {version}: {output}")


def legacy_upgrade_command(old_python: Path, old_launcher: Path) -> list[str]:
    # Public 3.2.5 misidentifies its Windows console launcher after uv strips
    # .exe from argv[0], then tries to replace the still-open executable.
    # Exercise the same unchanged old CLI without holding that executable.
    if os.name == "nt":
        return [str(old_python), "-m", "vibe.cli", "upgrade"]
    return [str(old_launcher), "upgrade"]


def acceptance(wheel: Path, root: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed before running this acceptance")
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        assert len(members) == 1
        metadata = message_from_bytes(archive.read(members[0]))
        assert metadata['Name'] == 'avibe-os'
        version = metadata['Version']
        constraints = root / "constraints.txt"
        constraints.write_bytes(archive.read("vibe/dependency_constraints.txt"))
    names = {match.group(1).lower().replace('_', '-') for match in re.finditer(r'^([\w.-]+)==', constraints.read_text(), re.M)}
    assert names
    guards = [arg for name in sorted(names - SOURCE_EXCEPTIONS) for arg in ("--no-build-package", name)]
    fresh = root / "fresh"
    env = isolated_env(fresh, uv)
    print("Installing the candidate with shipped constraints on CPython 3.15", flush=True)
    run([uv, "tool", "install", "--python", sys.executable, "--constraints", str(constraints), *guards, str(wheel)], env, fresh)
    verify(fresh, env, constraints, version)

    legacy = root / "legacy"
    env = isolated_env(legacy, uv)
    # Exact public release, unchanged code. Bootstrap with the candidate's
    # dependencies so Windows does not resolve an SDK release with no wheel.
    print("Bootstrapping public avibe-os 3.2.5 on CPython 3.15", flush=True)
    run([uv, "tool", "install", "--python", sys.executable, "--constraints", str(constraints), *guards, "avibe-os==3.2.5"], env, legacy)
    old_python = active_python(legacy)
    print(run([str(old_python), "-I", "-c", "import sys,importlib.metadata as m; assert sys.version_info[:2]==(3,15); assert m.version('avibe-os')=='3.2.5'; print(sys.version)"], env, legacy), flush=True)
    assert Path(shutil.which("vibe", path=env["PATH"])).resolve() == launcher(legacy).resolve()
    advertised = legacy / "latest.json"
    # CI builds carry a fixture version (3.0.99rc1), not the future release tag.
    # Advertise an update but install the actual unmodified candidate bytes.
    advertised.write_text(json.dumps({"info": {"version": "9999.0.0"}}))
    env["AVIBE_UPDATE_METADATA_URL"] = advertised.as_uri()
    env["AVIBE_UPGRADE_PACKAGE_SPEC"] = str(wheel)
    # No --python, constraints, or mocked installer are passed to the old CLI.
    # On Windows this is module-entry recovery coverage, NOT proof that the
    # already-released 3.2.5 vibe.exe can replace itself (a separate known bug).
    if os.name == "nt":
        print("Windows legacy coverage uses unchanged 3.2.5 python -m vibe.cli upgrade; normal vibe.exe self-upgrade is not validated", flush=True)
    output = run(legacy_upgrade_command(old_python, launcher(legacy)), env, legacy)
    print(output, flush=True)
    if "Using uv:" not in output or "Upgrade successful!" not in output:
        raise RuntimeError("Public 3.2.5 did not perform its real uv upgrade")
    command = next(line for line in output.splitlines() if line.startswith('Using uv:'))
    assert '--python' not in command and '--constraints' not in command, command
    if "launcher activation will complete after this command exits" in output:
        wait_for_activation(legacy)
    verify(legacy, env, constraints, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    args = parser.parse_args()
    assert sys.version_info[:2] == (3, 15), sys.version
    with tempfile.TemporaryDirectory(prefix='avibe-python315-') as directory:
        # TEMP may use an 8.3 alias on Windows. Use one canonical spelling
        # for fixture directories, PATH discovery and command invocations.
        acceptance(args.wheel.resolve(), Path(directory).resolve())
    print('CPython 3.15 constrained install and public 3.2.5 upgrade passed', flush=True)


if __name__ == '__main__':
    main()
