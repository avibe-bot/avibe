"""Production ledger and client tests; all state and HTTP egress are isolated."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples/feedback-intake"
WORKER = APP / "worker.py"
HELPER = ROOT / "skills/use-avibe/scripts/feedback_intake.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("feedback_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_payload(request_id=None, **overrides):
    value = dict(schema_version=1, request_id=request_id or str(uuid.uuid4()), kind="bug",
                 title="Workbench: 中文 ☃", body="Observed: 中文 ☃\n\nExpected: newlines & # $(data).", public_consent=True)
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False).encode()


class FakeGitHub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def do_GET(self):  # noqa: N802
        self.server.calls.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/user":
            self.send(200, self.server.actor)
        elif self.path == "/repos/avibe-bot/avibe":
            self.send(200, dict(id=1035030370, full_name="avibe-bot/avibe", private=False, has_issues=True))
        elif self.path.startswith("/repos/avibe-bot/avibe/issues/"):
            if self.server.mode == "readback-fail":
                self.send(502, {})
            else:
                issue = dict(self.server.issues[int(self.path.rsplit("/", 1)[1])])
                if self.server.mode == "wrong-author":
                    issue["user"] = dict(login="cs-agent-bot", id=7)
                self.send(200, issue)
        else:
            self.send(404, {})

    def do_POST(self):  # noqa: N802
        self.server.calls.append((self.command, self.path, self.headers.get("Authorization")))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/repos/avibe-bot/avibe/issues"
        if isinstance(self.server.mode, int):
            self.send(self.server.mode, {"message": "private upstream error"})
            return
        with self.server.lock:
            number = len(self.server.issues) + 100
            issue = dict(id=number + 10000, number=number, html_url=f"https://github.com/avibe-bot/avibe/issues/{number}",
                         repository_url="https://api.github.com/repos/avibe-bot/avibe", user=self.server.actor, **body)
            self.server.issues[number] = issue
        self.server.written.set()
        if self.server.mode == "disconnect":
            self.close_connection = True
        elif self.server.mode == "stall":
            self.server.release.wait(30)
        else:
            self.send(201, issue)


@pytest.fixture
def github(tmp_path, monkeypatch):
    # Strip inherited production/caller credentials from every subprocess.
    safe = {key: os.environ[key] for key in ("PATH", "HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")}
    for key in tuple(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in safe.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AVIBE_ALLOW_DEV_STATE_MIGRATION", "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    server.actor = dict(login="cs-agent-bot", id=272811739)
    server.calls, server.issues = [], {}
    server.lock = threading.Lock()
    server.written, server.release = threading.Event(), threading.Event()
    server.mode = "normal"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("AVIBE_FEEDBACK_DATABASE", str(private / "ledger.sqlite"))
    monkeypatch.setenv("AVIBE_FEEDBACK_TEST_MODE", "1")
    monkeypatch.setenv("AVIBE_FEEDBACK_TEST_API_BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AVIBE_FEEDBACK_GITHUB_TOKEN", "synthetic-feedback-pat")
    yield server
    server.release.set()
    server.shutdown()
    thread.join(2)


def execute(worker, payload):
    parsed = worker._parse_payload(payload)
    token = worker.reserve(parsed)
    if token:
        return subprocess.run([sys.executable, str(WORKER), "execute", parsed["request_id"], token], capture_output=True, timeout=25)
    return None


@pytest.mark.parametrize("change", [dict(kind=[]), dict(schema_version=True), dict(public_consent=1), dict(title="a\nb"),
                                    dict(title="a\u2028b"), dict(title="\ud800"), dict(body="\udfff"), dict(body="中"*6667),
                                    dict(title="a"*201), dict(body=""), dict(extra="x"), dict(request_id=str(uuid.uuid4()).upper())])
def test_strict_schema(change):
    worker = load_worker()
    value = json.loads(make_payload())
    value.update(change)
    with pytest.raises(worker.FeedbackError) as err:
        worker._parse_payload(json.dumps(value).encode())
    assert err.value.status in (400, 413)


@pytest.mark.parametrize("raw", [b'\xff', b'{"kind":"bug","kind":"feature"}', b'['*2000, b' '*32769])
def test_encoding_size_and_duplicate_keys(raw):
    worker = load_worker()
    with pytest.raises(worker.FeedbackError):
        worker._parse_payload(raw)


def test_create_binding_readback_repeat_conflict_and_privacy(github):
    worker = load_worker()
    payload = make_payload()
    request_id = json.loads(payload)["request_id"]
    assert execute(worker, payload).returncode == 0
    assert worker.receipt(request_id) == dict(schema_version=1, request_id=request_id, state="created", issue_number=100,
                                             issue_url="https://github.com/avibe-bot/avibe/issues/100")
    assert execute(worker, payload) is None
    with pytest.raises(worker.FeedbackError) as err:
        worker.reserve(worker._parse_payload(make_payload(request_id, body="changed")))
    assert err.value.status == 409
    assert [x[:2] for x in github.calls] == [("GET", "/user"), ("GET", "/repos/avibe-bot/avibe"),
                                           ("POST", "/repos/avibe-bot/avibe/issues"), ("GET", "/repos/avibe-bot/avibe/issues/100")]
    assert all(x[2] == "Bearer synthetic-feedback-pat" for x in github.calls)
    count = len(github.calls)
    worker.receipt(request_id)
    assert len(github.calls) == count


@pytest.mark.parametrize("mode,expected", [(401,"failed"),(403,"failed"),(422,"failed"),(429,"failed"),(502,"unknown"),
                                           ("disconnect","unknown"),("wrong-author","unknown"),("readback-fail","unknown")])
def test_upstream_uncertainty_never_reposts(github, mode, expected):
    worker = load_worker()
    github.mode = mode
    payload = make_payload()
    request_id = json.loads(payload)["request_id"]
    assert execute(worker, payload).returncode == 1
    assert worker.receipt(request_id) == dict(schema_version=1, request_id=request_id, state=expected)
    assert execute(worker, payload) is None
    assert sum(x[0] == "POST" for x in github.calls) == 1
    if mode == "readback-fail":
        assert worker._row(request_id)["issue_id"] == 10100
        github.mode = "normal"
        worker._verify("synthetic-feedback-pat", worker._row(request_id))
        assert worker.receipt(request_id)["state"] == "created"
        assert sum(x[0] == "POST" for x in github.calls) == 1


def test_actor_mismatch_cannot_write(github):
    github.actor = dict(login="cs-agent-bot", id=7)
    worker = load_worker()
    payload = make_payload()
    assert execute(worker, payload).returncode == 1
    assert not github.issues
    assert worker.receipt(json.loads(payload)["request_id"])["state"] == "failed"


def test_real_ledger_concurrent_repeat_and_limits(github, monkeypatch):
    worker = load_worker()
    payload = worker._parse_payload(make_payload())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: worker.reserve(payload), range(8)))
    assert sum(x is not None for x in results) == 1
    for _ in range(4):
        worker.reserve(worker._parse_payload(make_payload()))
    with pytest.raises(worker.FeedbackError) as err:
        worker.reserve(worker._parse_payload(make_payload()))
    assert err.value.status == 429
    # A new process/module shares the durable counters.
    fresh = load_worker()
    with pytest.raises(fresh.FeedbackError):
        fresh.reserve(worker._parse_payload(make_payload()))
    with worker._database() as db:
        db.execute("UPDATE limits SET window=0 WHERE name='minute'")
        db.execute("UPDATE limits SET count=30 WHERE name='day'")
    with pytest.raises(worker.FeedbackError) as err:
        worker.reserve(worker._parse_payload(make_payload()))
    assert err.value.status == 429
    monkeypatch.setattr(worker, "MAX_RECEIPTS", 5)
    with pytest.raises(worker.FeedbackError) as err:
        worker.reserve(worker._parse_payload(make_payload()))
    assert err.value.status == 503


def test_crash_after_write_preserves_unknown_and_releases_kernel_slot(github):
    worker = load_worker()
    github.mode = "stall"
    processes = []
    try:
        for _ in range(2):
            payload = worker._parse_payload(make_payload())
            token = worker.reserve(payload)
            processes.append((payload, subprocess.Popen([sys.executable, str(WORKER), "execute", payload["request_id"], token])))
        for _ in range(100):
            if len(github.issues) == 2:
                break
            time.sleep(.02)
        assert len(github.issues) == 2
        third = make_payload()
        assert execute(worker, third).returncode == 1
        assert worker.receipt(json.loads(third)["request_id"])["state"] == "failed"
        assert worker.reserve(worker._parse_payload(third)) is None
        assert len(github.issues) == 2
        for payload, process in processes:
            process.kill()
            process.wait(2)
            assert worker.receipt(payload["request_id"])["state"] == "unknown"
            assert worker.reserve(payload) is None
        github.mode = "normal"
        assert execute(worker, make_payload()).returncode == 0
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_safe_database_path(github, tmp_path, monkeypatch):
    worker = load_worker()
    for path in ("relative", str(WORKER.parent / "db.sqlite"), str(Path(os.environ["AVIBE_HOME"]) / "show/sibling/db.sqlite")):
        monkeypatch.setenv("AVIBE_FEEDBACK_DATABASE", path)
        with pytest.raises(worker.FeedbackError):
            worker._database_path()
    monkeypatch.delenv("AVIBE_FEEDBACK_DATABASE")
    with pytest.raises(worker.FeedbackError):
        worker._database_path()


@pytest.fixture
def real_vault(github, tmp_path, monkeypatch):
    from storage import vault_service
    from storage.vault_crypto import Sealed
    from vibe import cli
    binary = shutil.which("avault")
    if not binary:
        pytest.skip("Set PATH to a real avault custody binary for CLI integration")
    sealed = subprocess.run([binary, "--store", "file", "seal", "--name", "AVIBE_FEEDBACK_GITHUB_TOKEN"],
                            input=b"synthetic-feedback-pat", capture_output=True, check=True)
    value = json.loads(sealed.stdout)
    if isinstance(value["wrap_meta"], dict):
        value["wrap_meta"] = json.dumps(value["wrap_meta"])
    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        vault_service.create_secret(conn, name="AVIBE_FEEDBACK_GITHUB_TOKEN", sealed=Sealed(**value))
    engine.dispose()
    monkeypatch.setenv("AVIBE_FEEDBACK_FIXTURE_ROOT", str(tmp_path.resolve()))
    monkeypatch.setenv("AVIBE_FEEDBACK_SOURCE_ROOT", str(ROOT))
    monkeypatch.setenv("AVIBE_FEEDBACK_TEST_AVAULT", binary)
    # Use the test Python, not the fixture script's /usr/bin/env python3.
    launcher = tmp_path / "vibe-fixture"
    launcher.write_text(f"#!{sys.executable}\n" + (APP / "integration/vibe-fixture.py").read_text().split("\n",1)[1])
    launcher.chmod(0o700)
    monkeypatch.setenv("AVIBE_FEEDBACK_VAULT_BIN", str(launcher))
    return github


def test_real_named_vault_cli_stdin_and_detached_child(real_vault):
    worker = load_worker()
    payload = make_payload()
    assert worker.submit(payload) == 202
    assert worker.receipt(json.loads(payload)["request_id"])["state"] == "created"


def test_builtin_publication_preserves_helper(tmp_path):
    from core.managed_skills import publish_builtin_skills
    snapshot = publish_builtin_skills(source_root=ROOT / "skills", destination_root=tmp_path / "builtin")
    assert (tmp_path / "builtin" / snapshot / "use-avibe/scripts/feedback_intake.py").read_bytes() == HELPER.read_bytes()


def test_real_vault_timeout_terminates_detached_processes(real_vault, monkeypatch):
    import psutil
    worker = load_worker()
    real_vault.mode = "stall"
    payload = make_payload()
    # Shorten the absolute deadline in the parent ledger; the child must obey it.
    monkeypatch.setattr(worker, "EXECUTION_SECONDS", 2)
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    start = time.monotonic()
    with pytest.raises(worker.FeedbackError):
        worker.submit(payload)
    assert time.monotonic() - start < 6
    assert real_vault.written.is_set()
    request_id = json.loads(payload)["request_id"]
    assert worker.receipt(request_id)["state"] == "unknown"
    for _ in range(30):
        remaining = [p for p in psutil.Process().children(recursive=True) if p.pid not in before and p.status() != psutil.STATUS_ZOMBIE]
        if not remaining:
            break
        time.sleep(.05)
    assert remaining == []
    # The slot is released by actual child exit; no auto retry of this request.
    assert worker.reserve(worker._parse_payload(payload)) is None
    real_vault.mode = "normal"
    monkeypatch.setattr(worker, "EXECUTION_SECONDS", 20)
    assert worker.submit(make_payload()) == 202


def test_operator_reconciliation_is_bounded_read_only(real_vault):
    worker = load_worker()
    payload = make_payload()
    request_id = json.loads(payload)["request_id"]
    real_vault.mode = "readback-fail"
    with pytest.raises(worker.FeedbackError):
        worker.submit(payload)
    assert worker._row(request_id)["issue_id"] == 10100
    assert worker.receipt(request_id)["state"] == "unknown"
    with pytest.raises(worker.FeedbackError):
        worker.reconcile(request_id)  # Still within the original operation deadline.
    worker._update(request_id, deadline=0)
    real_vault.mode = "normal"
    assert worker.reconcile(request_id) == 200
    assert worker.receipt(request_id)["state"] == "created"
    assert sum(call[0] == "POST" for call in real_vault.calls) == 1
    with pytest.raises(worker.FeedbackError):
        worker.reconcile(request_id)


@pytest.mark.parametrize("state", ["created", "unknown", "malformed"])
def test_helper_uncertainty_resume_and_exact_payload_dedup(tmp_path, github, state):
    class Handler(BaseHTTPRequestHandler):
        posts = []
        ids = []

        def send(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            value = json.loads(raw)
            self.posts.append(raw)
            # The real outbox transaction committed before the upload.
            with sqlite3.connect(tmp_path / "outbox/outbox.sqlite") as db:
                saved = db.execute("SELECT request_id,payload FROM attempts").fetchone()
            assert saved == (value["request_id"], raw)
            self.send(504, {})

        def do_GET(self):  # noqa: N802
            request_id = self.path.split("=",1)[1]
            self.ids.append(request_id)
            receipt = dict(schema_version=1, request_id=request_id, state="created" if state == "malformed" else state)
            if receipt["state"] == "created":
                receipt.update(issue_number=123, issue_url="https://github.com/avibe-bot/avibe/issues/123")
            if state == "malformed":
                receipt["issue_url"] = "https://attacker.invalid/123"
            self.send(200, receipt)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, AVIBE_FEEDBACK_OUTBOX=str(tmp_path / "outbox"),
               AVIBE_FEEDBACK_TEST_SHARE_BASE_URL=f"http://127.0.0.1:{server.server_port}")
    command = [sys.executable,str(HELPER),"submit","bug","Unicode 中文", "--public-confirmed"]
    try:
        first = subprocess.run(command, input=b"body # $ &\nsecond line", capture_output=True, env=env, timeout=10)
        result = json.loads(first.stdout)
        assert result["state"] == ("unknown" if state == "malformed" else state)
        request_id = result["request_id"]
        again = subprocess.run(command, input=b"body # $ &\nsecond line", capture_output=True, env=env, timeout=10)
        resumed = subprocess.run([sys.executable,str(HELPER),"resume",request_id], capture_output=True, env=env, timeout=10)
        assert json.loads(again.stdout)["request_id"] == json.loads(resumed.stdout)["request_id"] == request_id
        assert len(Handler.posts) == 1 and Handler.ids == [request_id]*3
        assert "attacker" not in first.stdout.decode()
        with sqlite3.connect(tmp_path / "outbox/outbox.sqlite") as db:
            row = db.execute("SELECT payload FROM attempts").fetchone()
        assert (row[0] is None) == (state == "created")
    finally:
        server.shutdown()
        thread.join(2)


def test_crash_immediately_after_201_identity_is_read_recoverable(github):
    worker = load_worker()
    payload = worker._parse_payload(make_payload())
    token = worker.reserve(payload)
    script = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('worker', sys.argv[1])
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w._verify = lambda *args: os._exit(91)
w.execute(sys.argv[2],sys.argv[3])
"""
    child = subprocess.run([sys.executable,"-c",script,str(WORKER),payload["request_id"],token], capture_output=True)
    assert child.returncode == 91
    row = worker._row(payload["request_id"])
    assert row["issue_id"] == 10100 and row["issue_number"] == 100
    assert worker.receipt(payload["request_id"])["state"] == "unknown"
    assert worker.reserve(payload) is None
    worker._verify("synthetic-feedback-pat", row)
    assert worker.receipt(payload["request_id"])["state"] == "created"
    assert len(github.issues) == 1


def test_github_redirect_cannot_select_destination(github):
    worker = load_worker()
    github.mode = 302
    payload = make_payload()
    assert execute(worker, payload).returncode == 1
    assert worker.receipt(json.loads(payload)["request_id"])["state"] == "unknown"
    assert len(github.calls) == 3


def test_real_vault_child_output_is_bounded_and_killed(real_vault, tmp_path, monkeypatch):
    worker = load_worker()
    child_script = tmp_path / "noisy-child.py"
    child_script.write_text("import sys,time\nsys.stdout.write('x'*40000)\nsys.stdout.flush()\ntime.sleep(30)\n")
    monkeypatch.setattr(worker, "__file__", str(child_script))
    start = time.monotonic()
    with pytest.raises(worker.FeedbackError):
        worker._vault(str(uuid.uuid4()), uuid.uuid4().hex)
    assert time.monotonic() - start < 6
    assert not real_vault.calls


@pytest.mark.parametrize("terminal", ["created", "failed"])
@pytest.mark.parametrize("poll_mode", ["404", "malformed", "disconnect", "pending"])
def test_helper_preserves_verified_terminal_receipt(github, tmp_path, terminal, poll_mode):
    class Handler(BaseHTTPRequestHandler):
        mode = "initial"
        posts = 0
        def do_POST(self):  # noqa: N802
            type(self).posts += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def do_GET(self):  # noqa: N802
            request_id = self.path.split("=",1)[1]
            if self.mode == "disconnect":
                self.close_connection = True
                return
            value = dict(schema_version=1,request_id=request_id,state=terminal)
            if terminal == "created":
                value.update(issue_number=123, issue_url="https://github.com/avibe-bot/avibe/issues/123")
            if self.mode == "malformed":
                value["request_id"] = "wrong"
            if self.mode == "pending":
                value = dict(schema_version=1,request_id=request_id,state="pending")
            self.send_response(404 if self.mode == "404" else 200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    env = dict(os.environ, AVIBE_FEEDBACK_TEST_SHARE_BASE_URL=f"http://127.0.0.1:{server.server_port}")
    command = [sys.executable,str(HELPER),"submit","bug","Saved terminal", "--public-confirmed"]
    try:
        first = subprocess.run(command,input=b"body",env=env,capture_output=True,check=False)
        receipt = json.loads(first.stdout)
        assert receipt["state"] == terminal
        Handler.mode = poll_mode
        for command2 in (command, [sys.executable,str(HELPER),"resume",receipt["request_id"]]):
            result = subprocess.run(command2,input=b"body",env=env,capture_output=True,check=False)
            assert json.loads(result.stdout) == receipt
        assert Handler.posts == 1
        assert (Path(env["AVIBE_HOME"]) / "state/feedback-intake/outbox.sqlite").is_file()
        assert not (Path(env["HOME"]) / ".avibe-other/state/feedback-intake/outbox.sqlite").exists()
    finally:
        server.shutdown()
        thread.join(2)


def test_helper_429_retries_only_same_saved_attempt(github, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        posts = []
        accepted = False
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            self.posts.append(raw)
            self.send_response(202 if self.accepted else 429)
            self.end_headers()
            self.wfile.write(b'{}')
        def do_GET(self):  # noqa: N802
            request_id = self.path.split("=",1)[1]
            value = dict(schema_version=1,request_id=request_id,state="created",issue_number=123,
                         issue_url="https://github.com/avibe-bot/avibe/issues/123")
            self.send_response(200 if self.accepted and len(self.posts)>1 else 404)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    env = dict(os.environ, AVIBE_FEEDBACK_TEST_SHARE_BASE_URL=f"http://127.0.0.1:{server.server_port}",
               AVIBE_HOME=str(tmp_path / "custom-avibe-home"))
    command = [sys.executable,str(HELPER),"submit","bug","Rate limited", "--public-confirmed"]
    try:
        first = subprocess.run(command,input=b"report",env=env,capture_output=True)
        limited = json.loads(first.stdout)
        assert first.returncode == 3 and limited["retryable"] is True
        request_id = limited["request_id"]
        resumed = subprocess.run([sys.executable,str(HELPER),"resume",request_id],env=env,capture_output=True)
        assert resumed.returncode == 3 and len(Handler.posts) == 1
        Handler.accepted = True
        second = subprocess.run(command,input=b"report",env=env,capture_output=True)
        assert second.returncode == 0
        assert json.loads(second.stdout)["request_id"] == request_id
        assert Handler.posts[0] == Handler.posts[1]
        assert (tmp_path / "custom-avibe-home/state/feedback-intake/outbox.sqlite").is_file()
        assert not (Path(env["HOME"]) / ".avibe/state/feedback-intake/outbox.sqlite").exists()
    finally:
        server.shutdown()
        thread.join(2)


def test_runtime_manifest_validation_survives_optimized_python(tmp_path):
    manifest = tmp_path / "bad-manifest.json"
    archive = tmp_path / "vibe-show-runtime-node-linux-x64.tgz"
    manifest.write_text('{}')
    archive.write_bytes(b'not an archive')
    destination = tmp_path / "extract"
    result = subprocess.run([sys.executable,"-O",str(APP / "integration/prepare-runtime.py"),
                             str(manifest),str(archive),str(destination)],capture_output=True)
    assert result.returncode != 0 and b"Runtime manifest hash mismatch" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("receipt_state", ["failed", "created", "unavailable"])
def test_429_requires_absent_receipt_and_terminal_receipt_wins(github, tmp_path, receipt_state):
    class Handler(BaseHTTPRequestHandler):
        posts = 0
        def do_POST(self):  # noqa: N802
            type(self).posts += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{}')
        def do_GET(self):  # noqa: N802
            value = dict(schema_version=1,request_id=self.path.split("=",1)[1],state=receipt_state)
            if receipt_state == "created":
                value.update(issue_number=123, issue_url="https://github.com/avibe-bot/avibe/issues/123")
            self.send_response(503 if receipt_state == "unavailable" else 200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    env = dict(os.environ,AVIBE_FEEDBACK_TEST_SHARE_BASE_URL=f"http://127.0.0.1:{server.server_port}")
    command = [sys.executable,str(HELPER),"submit","bug","Receipt precedence", "--public-confirmed"]
    try:
        for _ in range(2):
            result = subprocess.run(command,input=b"body",env=env,capture_output=True)
            assert json.loads(result.stdout)["state"] == ("unknown" if receipt_state == "unavailable" else receipt_state)
        assert Handler.posts == 1
        with sqlite3.connect(Path(env["AVIBE_HOME"]) / "state/feedback-intake/outbox.sqlite") as db:
            assert db.execute("SELECT retryable FROM attempts").fetchone()[0] == 0
    finally:
        server.shutdown()
        thread.join(2)


def test_expired_pending_reservation_is_durable_no_write(github):
    worker = load_worker()
    payload = worker._parse_payload(make_payload())
    token = worker.reserve(payload)
    worker._update(payload["request_id"], deadline=0)
    assert worker.receipt(payload["request_id"])["state"] == "failed"
    assert worker._row(payload["request_id"])["state"] == "failed"
    child = subprocess.run([sys.executable,str(WORKER),"execute",payload["request_id"],token], capture_output=True)
    assert child.returncode == 1 and not github.calls
    assert worker.reserve(payload) is None
