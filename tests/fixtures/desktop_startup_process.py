"""Hermetic cmd_start producer for the Rust startup admission integration test.

Only process creation/controller ownership and optional housekeeping are controlled.
cmd_start, both readiness paths, status writes, wire serialization, HTTP and stdout
are production code. No backend, keychain, browser, or user service is contacted.
The Rust parent supplies an empty environment with private homes before imports.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from vibe import cli, runtime  # noqa: E402
from vibe import install_generations  # noqa: E402

scratch = Path(sys.argv[1])
mode, delay, housekeeping = sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
case = sys.argv[5]
controller_ready = threading.Event()
trace = {"python_ready_probes": 0, "health_probes": 0, "service_starts": 0, "ui_starts": 0}
started = 0.0
service_pid = os.getpid()

# In the EOF case these children and their real log sinks outlive cmd_start.
# Their bounded lifetime and explicit stop file also cover assertion failures.
child_script = """
import os,sys,time
from pathlib import Path
root, role = Path(sys.argv[1]), sys.argv[2]
deadline = time.monotonic() + 30
while not (root / 'children-stop').exists() and time.monotonic() < deadline:
    if (root / 'prove-alive').exists():
        (root / (role + '-alive')).write_text(str(os.getpid()))
        print(role, flush=True)
    time.sleep(0.02)
(root / (role + '-done')).touch()
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ready = controller_ready.is_set()
        if self.path == "/health":
            trace["health_probes"] += 1
            body, status = b"{}", 200
        elif self.path == "/ready":
            if self.headers.get("User-Agent", "").lower().startswith("python"):
                trace["python_ready_probes"] += 1
            payload = {"schema_version": 1, "product": "avibe", "ready": ready}
            if ready:
                payload["desktop_runtime_id"] = "b" * 64
            else:
                payload["code"] = "controller_unavailable"
            body, status = json.dumps(payload).encode(), 200 if ready else 503
        else:
            body, status = b"", 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_address[1]
config = SimpleNamespace(
    has_configured_platform_credentials=lambda: True,
    ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=port, open_browser=False),
)
cli._ensure_config = lambda: config
cli._guard_cli_default_state_migration = lambda: None
cli.cache_running_vibe_path = lambda: None
runtime.resolve_service_owner_pid = lambda **kwargs: service_pid if controller_ready.is_set() else None
runtime.service_instance_started = lambda _pid: controller_ready.is_set()


def start_service(*, start_info, **kwargs):
    global service_pid
    trace["service_starts"] += 1
    if case == "eof":
        child = runtime.spawn_service_background_process(
            [sys.executable, "-c", child_script, str(scratch), "service"],
            "service-stdout.log", "service-stderr.log",
        )
        service_pid = child.pid
        start_info.capture(child.pid, reused=False)
    else:
        start_info.capture(service_pid, reused=True)

    def become_ready():
        time.sleep(delay)
        trace["controller_ms"] = (time.monotonic() - started) * 1000
        controller_ready.set()

    threading.Thread(target=become_ready, daemon=True).start()
    return service_pid


def start_ui(host, port, *, start_info, wait_for_ready=True):
    trace["ui_starts"] += 1
    if case == "eof":
        pid = runtime.spawn_background(
            [sys.executable, "-c", child_script, str(scratch), "ui"],
            scratch / "ui.pid", "ui-stdout.log", "ui-stderr.log",
            start_info=start_info,
        )
    else:
        pid = os.getpid()
        start_info.capture(pid, reused=True)
    if wait_for_ready:
        runtime.wait_for_ui_server(host, port)
    return pid


def wait_service(pid, timeout):
    return pid if controller_ready.wait(timeout) else None


def housekeeping_work(*_args):
    trace["housekeeping_start_ms"] = (time.monotonic() - started) * 1000
    (scratch / "housekeeping-started").touch()
    time.sleep(housekeeping)
    (scratch / "housekeeping-finished").touch()


runtime.start_service = start_service
runtime.start_ui = start_ui
runtime.wait_for_service_ready = wait_service
install_generations.collect_install_generations = housekeeping_work
(scratch / "endpoint.tmp").write_text(f"http://127.0.0.1:{port}", encoding="utf-8")
(scratch / "endpoint.tmp").replace(scratch / "endpoint")
try:
    assert sys.stdin.readline().strip() == "start"
    started = time.monotonic()
    args = cli.build_parser().parse_args(
        ["start", "--no-open-browser"] + (["--desktop-receipt"] if mode == "receipt" else [])
    )
    result = cli.cmd_start(open_browser=args.open_browser, desktop_receipt=args.desktop_receipt)
    trace["helper_work_ms"] = (time.monotonic() - started) * 1000
    (scratch / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    assert result == 0
    assert sys.stdin.readline().strip() == "done"
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
