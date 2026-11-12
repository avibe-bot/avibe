"""Native Windows console-launcher self-upgrade acceptance (never a module shortcut).

Installs this PR's wheel, then runs its real vibe.exe upgrade into a new immutable
installation of the same fixture wheel. The advertised future version forces the
upgrade; different interpreter/launcher bytes prove replacement actually happened.
Public 3.2.5 module-entry recovery remains a separate acceptance.
"""
from __future__ import annotations

import argparse
import ctypes
from email import message_from_bytes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import zipfile

from scripts.python315_install_regression import active_python, isolated_env, launcher, run, wait_for_activation


def short_path(path: Path) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(str(path), buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def windows_env(root: Path, uv: str) -> dict[str, str]:
    env = isolated_env(root, uv)
    # CI invokes this from the installer's uv environment. uv tool install
    # excludes virtualenv interpreters when resolving the ARM x64-Python policy;
    # expose the already-installed base Python without sharing managed state or
    # enabling downloads. Keep the test's own stable launcher first on PATH.
    entries = env["PATH"].split(os.pathsep)
    base = Path(getattr(sys, "_base_executable", sys.executable)).parent
    env["PATH"] = os.pathsep.join([entries[0], str(base), *entries[1:]])
    return env


def acceptance(wheel: Path, root: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("This acceptance requires native Windows")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed before running this acceptance")
    env = windows_env(root, uv)
    with zipfile.ZipFile(wheel) as archive:
        metadata_name, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        version = message_from_bytes(archive.read(metadata_name))["Version"]
        constraints = root / "constraints.txt"
        constraints.write_bytes(archive.read("vibe/dependency_constraints.txt"))
    run([uv, "tool", "install", "--python", sys.executable, "--constraints", str(constraints), str(wheel)], env, root)
    stable = launcher(root)
    previous = stable.read_bytes()
    old_python = active_python(root)
    # Exercise the native path APIs as well as the real argv[0] from uv below.
    alias = short_path(stable)
    print(f"Native short-path spelling differs: {alias.lower() != str(stable).lower()}", flush=True)
    probe = """
import sys
from vibe.upgrade import launcher_is_current_process
stable, alias = sys.argv[1:]
for spelling in (stable, stable.upper(), stable[:-4], alias, alias[:-4]):
    sys.argv = [spelling]
    assert launcher_is_current_process(stable), spelling
sys.argv = ['__main__.py']
assert not launcher_is_current_process(stable)
"""
    env["VIBE_CURRENT_EXECUTABLE"] = str(stable)
    run([str(old_python), "-I", "-c", probe, str(stable), alias], env, root)
    advertised = root / "latest.json"
    advertised.write_text(json.dumps({"info": {"version": "9999.0.0"}}), encoding="utf-8")
    env["AVIBE_UPDATE_METADATA_URL"] = advertised.as_uri()
    env["AVIBE_UPGRADE_PACKAGE_SPEC"] = str(wheel)
    print("Running the actual installed vibe.exe upgrade (no module-entry substitution)", flush=True)
    output = run([str(stable), "upgrade"], env, root)
    print(output, flush=True)
    if "Using uv:" not in output or "launcher activation will complete after this command exits" not in output:
        raise RuntimeError("The native console launcher did not choose deferred uv activation")
    # Do not execute or open the busy destination until its post-replace marker.
    wait_for_activation(root)
    if stable.read_bytes() == previous:
        raise RuntimeError("The stable launcher still selects the old interpreter")
    python = active_python(root)
    if python == old_python:
        raise RuntimeError("The upgrade did not create a new active environment")
    run([str(python), "-I", "-c", "import importlib.metadata as m,sys; assert m.version('avibe-os') == sys.argv[1]", version], env, root)
    output = run([str(stable), "version"], env, root)
    if version not in output:
        raise RuntimeError(f"The replaced launcher did not run the candidate: {output}")
    logs = list((root / "avibe" / "logs").glob("upgrade-activation-*.log"))
    if len(logs) != 1:
        raise RuntimeError("Expected exactly one detached activation helper log")
    print(f"Native vibe.exe deferred self-upgrade passed: {version}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="avibe windows launcher ")).resolve()
    try:
        acceptance(args.wheel.resolve(), root)
    finally:
        # The detached helper may still be exiting after publishing the marker.
        # Only retry cleanup of this test-owned tree, never the launcher replace.
        deadline = time.monotonic() + 30
        while True:
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)


if __name__ == "__main__":
    main()
