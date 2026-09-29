"""The native helper must preserve a selected instance and stop only its receipt.

The existing Desktop tests cover already-managed handover. These use an old
process with the existing lock and running-agents protocol, without requiring
the predecessor to implement the new takeover command.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading

import psutil
from pathlib import Path

import pytest

from config import paths
from config.v2_config import V2Config
from vibe import desktop_takeover, runtime

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX predecessor IPC fixture")


@pytest.fixture
def old_instance(tmp_path, monkeypatch):
    home = tmp_path / "已有数据 空格"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    (home / "config").mkdir(parents=True)
    (home / "runtime").mkdir()
    (home / "state").mkdir()
    V2Config.default().save(paths.get_config_path())
    (home / "state" / "history.txt").write_text("已有会话与项目", encoding="utf-8")
    source = Path(__file__).resolve().parents[1]
    predecessor = tmp_path / "old-install"
    (predecessor / "vibe").mkdir(parents=True)
    (predecessor / "core").mkdir()
    (predecessor / "vibe" / "runtime.py").touch()
    (predecessor / "core" / "controller.py").touch()
    script = predecessor / "main.py"
    script.write_text('''import json, os, signal, socketserver
from pathlib import Path
from vibe import runtime
home = Path(os.environ['AVIBE_HOME'])
lock = (home / 'runtime/service.lock').open('a+')
assert runtime._try_lock_file(lock)
lock.write(json.dumps({'pid': os.getpid(), 'phase': 'running'})); lock.flush()
(home / 'runtime/vibe.pid').write_text(str(os.getpid()))
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()
        while self.rfile.readline().strip(): pass
        busy = (home / 'runtime/test-busy').exists()
        body = json.dumps({'ok': True, 'agents': [{'state': 'active'}] if busy else [], 'ownership_available': True}).encode()
        self.wfile.write(b'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: ' + str(len(body)).encode() + b'\\r\\n\\r\\n' + body)
server = socketserver.UnixStreamServer(os.environ['VIBE_INTERNAL_DISPATCH_SOCKET'], Handler)
os.chmod(os.environ['VIBE_INTERNAL_DISPATCH_SOCKET'], 0o600)
signal.signal(signal.SIGTERM, lambda *_: None if (home / 'runtime/test-refuse-stop').exists() else exit(0))
print('ready', flush=True)
server.serve_forever()
''', encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="avibe-takeover-") as socket_dir:
        monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(Path(socket_dir) / "ipc"))
        environment = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
        for key in ("AVIBE_DESKTOP_RUNTIME_ID", "INVOCATION_ID", "NOTIFY_SOCKET", "LAUNCH_JOBKEY_LABEL"):
            environment.pop(key, None)
        service = subprocess.Popen([sys.executable, str(script)], env=environment, stdout=subprocess.PIPE, text=True)
        assert service.stdout.readline().strip() == "ready"
        ui_root = tmp_path / "old-ui"
        (ui_root / "vibe").mkdir(parents=True)
        (ui_root / "vibe/__init__.py").touch()
        (ui_root / "vibe/ui_server.py").write_text("def run_ui_server():\n    import time; print('ready', flush=True); time.sleep(120)\n")
        ui = subprocess.Popen([sys.executable, "-c", "from vibe.ui_server import run_ui_server; run_ui_server()"],
                              cwd=ui_root, env=environment, stdout=subprocess.PIPE, text=True)
        assert ui.stdout.readline().strip() == "ready"
        paths.get_runtime_ui_pid_path().write_text(str(ui.pid))
        reapers = [threading.Thread(target=process.wait, daemon=True) for process in (service, ui)]
        for reaper in reapers:
            reaper.start()
        try:
            yield home, environment, service, ui
        finally:
            (home / "runtime/test-refuse-stop").unlink(missing_ok=True)
            for process in (service, ui):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            for reaper in reapers:
                reaper.join(timeout=5)


def _helper(environment, *arguments):
    return subprocess.run([sys.executable, "-m", "vibe.desktop_takeover", *arguments],
                          env=environment, capture_output=True, text=True, timeout=15)


def test_native_helper_consumes_old_service_and_preserves_existing_data(old_instance):
    home, environment, service, ui = old_instance
    original = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
    original_dirs = {path.relative_to(home) for path in home.rglob("*") if path.is_dir()}
    inspected = _helper(environment, "inspect")
    assert inspected.returncode == 0, inspected.stderr + inspected.stdout
    discovery = json.loads(inspected.stdout)
    assert discovery["home"] == str(home)
    assert discovery["external"]["reason"] is None
    assert discovery["external"]["service"]["pid"] == service.pid
    assert {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()} == original
    assert {path.relative_to(home) for path in home.rglob("*") if path.is_dir()} == original_dirs

    switched = _helper(environment, "takeover", "--home", str(home), "--receipt", json.dumps(discovery["external"]))
    assert switched.returncode == 0, switched.stderr + switched.stdout
    service.wait(timeout=5)
    ui.wait(timeout=5)
    assert (home / "state/history.txt").read_text() == "已有会话与项目"
    assert paths.get_config_path().read_bytes() == original[Path("config/config.json")]
    assert not (home / "state/vibe.sqlite").exists(), "takeover must not run migrations before normal startup"


@pytest.mark.parametrize("change", ["busy", "identity", "home"])
def test_a_changed_confirmation_never_stops_the_current_service(old_instance, change):
    home, environment, service, ui = old_instance
    receipt = json.loads(_helper(environment, "inspect").stdout)["external"]
    if change == "busy":
        (home / "runtime/test-busy").touch()
    elif change == "identity":
        receipt["service"]["created"] += 1
    else:
        receipt["home"] = str(home.parent)
    result = _helper(environment, "takeover", "--home", str(home), "--receipt", json.dumps(receipt))
    assert result.returncode == 3
    assert json.loads(result.stdout)["error"] == "identity_changed"
    assert service.poll() is None and ui.poll() is None


def test_custom_home_is_discovered_without_a_finder_environment(old_instance, monkeypatch):
    home, _, service, _ = old_instance
    process = psutil.Process(service.pid)
    process.info = {"pid": service.pid, "cmdline": process.cmdline()}
    monkeypatch.delenv("AVIBE_HOME")
    monkeypatch.setattr(desktop_takeover.psutil, "process_iter", lambda *_: iter([process]))
    assert desktop_takeover.discover_home(None) == home


def test_unknown_selected_home_is_not_created(tmp_path):
    home = tmp_path / "not-an-instance"
    with pytest.raises(desktop_takeover.TakeoverRefused, match="invalid_home"):
        desktop_takeover.discover_home(str(home))
    assert not home.exists()


def test_external_supervisor_blocks_takeover(old_instance, monkeypatch):
    _, _, service, _ = old_instance
    monkeypatch.setattr(desktop_takeover, "_supervised", lambda _pid: True)
    snapshot = desktop_takeover.inspect_runtime()["external"]
    assert snapshot["reason"] == "supervised"
    with pytest.raises(desktop_takeover.TakeoverRefused, match="supervised"):
        desktop_takeover.take_over(snapshot)
    assert service.poll() is None


def test_a_predecessor_that_does_not_exit_is_not_force_killed(old_instance, monkeypatch):
    home, _, service, ui = old_instance
    receipt = desktop_takeover.inspect_runtime()["external"]
    (home / "runtime/test-refuse-stop").touch()
    stop = runtime._stop_desktop_processes
    monkeypatch.setattr(runtime, "_stop_desktop_processes", lambda processes, **kwargs: stop(processes, timeout=0.1, force=kwargs["force"]))
    with pytest.raises(desktop_takeover.TakeoverRefused, match="stop_failed"):
        desktop_takeover.take_over(receipt)
    assert service.poll() is None and ui.poll() is None


def test_inspection_of_a_broken_config_does_not_write_recovery_files(old_instance):
    home, environment, service, ui = old_instance
    paths.get_config_path().write_text("{broken")
    before = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
    result = _helper(environment, "inspect")
    assert result.returncode == 3
    assert {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()} == before
    assert service.poll() is None and ui.poll() is None
