"""Shared fixture safety; no mutation of installed Avibe or production state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time


def hosted(root: Path) -> Path:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("AVIBE_NATIVE_ACCEPTANCE_HOSTED") != "1":
        raise RuntimeError("Native fixture execution requires its GitHub-hosted job")
    if platform.system() not in {"Darwin", "Windows"}:
        raise RuntimeError("A native macOS or Windows runner is required")
    runner = Path(os.environ["RUNNER_TEMP"]).resolve()
    root = root.resolve()
    if root == runner or not root.is_relative_to(runner):
        raise RuntimeError("Acceptance root must be a dedicated runner-temp child")
    root.mkdir(parents=True, exist_ok=True)
    return root


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def command(argv, cwd: Path, env: dict | None = None, log: Path | None = None) -> None:
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps([str(part) for part in argv]) + "\n")
            stream.flush()
            subprocess.run([str(part) for part in argv], cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    else:
        subprocess.run([str(part) for part in argv], cwd=cwd, env=env, check=True)


def runtime_environment(root: Path) -> dict[str, str]:
    # Windows SystemRoot is required by the real installer; never inherit tokens,
    # browser flags, proxy settings, cloud credentials or backend configuration.
    keep = ("PATH", "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "LANG",
            "__CF_USER_TEXT_ENCODING", "GITHUB_ACTIONS", "RUNNER_TEMP", "AVIBE_NATIVE_ACCEPTANCE_HOSTED")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR", "AVIBE_HOME"):
        path = root / "environment" / key.lower()
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env["AVIBE_DESKTOP_MANAGED_RUNTIME"] = "1"
    env["AVIBE_COMPUTER_USE_STATE_DIR"] = str(root / "native-state")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


class Processes:
    """Track only created processes/descendants with PID reuse protection."""
    def __init__(self):
        import psutil
        self.psutil = psutil
        self.owned = {}

    def add(self, pid: int) -> None:
        try:
            process = self.psutil.Process(pid)
            self.owned[pid] = process.create_time()
        except self.psutil.NoSuchProcess:
            pass

    def collect(self) -> None:
        for pid, created in list(self.owned.items()):
            try:
                process = self.psutil.Process(pid)
                if process.create_time() != created:
                    continue
                for child in process.children(recursive=True):
                    self.owned[child.pid] = child.create_time()
            except self.psutil.Error:
                continue

    def close(self) -> None:
        self.collect()
        children = []
        for pid, created in self.owned.items():
            try:
                process = self.psutil.Process(pid)
                if process.create_time() == created:
                    process.terminate()
                    children.append(process)
            except self.psutil.Error:
                pass
        _, alive = self.psutil.wait_procs(children, timeout=10)
        for process in alive:
            try:
                process.kill()
            except self.psutil.Error:
                pass
        self.psutil.wait_procs(alive, timeout=5)


def wait(predicate, timeout: float, description: str, processes: Processes | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if processes:
            processes.collect()
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(description)
