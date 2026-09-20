"""Interrupted delivery recovery through real reservation/claim and GitHub HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_feedback_intake import HELPER, WORKER, github, load_worker  # noqa: F401


def load_client():
    spec = importlib.util.spec_from_file_location("feedback_client", HELPER)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    return client


@pytest.fixture
def intake(github, monkeypatch):
    worker = load_worker()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            with self.server.lock:
                self.server.status_threads.append(threading.current_thread())
            self.server.status_calls.append(self.path)
            mode = self.server.status_mode
            if mode == "busy":
                self.send(429, {})
            elif mode == "disconnect":
                self.close_connection = True
            elif mode == "stall":
                self.server.status_entered.set()
                if not self.server.status_release.wait(30):
                    raise RuntimeError("Status barrier timed out")
                self.send(404, {})
            elif mode == "invalid_json":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'not JSON')
            elif mode == "oversize":
                self.send(404, "x" * 32769)
            elif mode == "unavailable":
                self.send(503, {})
            elif mode == "malformed404":
                self.send(404, {"not": "a fixed receipt response"})
            elif mode == "malformed":
                self.send(200, {"state":"created"})
            elif mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/api/feedback-status?request_id=x")
                self.end_headers()
            else:
                try:
                    receipt = worker.receipt(self.path.split("=",1)[1])
                    self.send(200, receipt)
                except worker.FeedbackError as exc:
                    self.send(exc.status,{})

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            with self.server.lock:
                self.server.uploads.append(raw)
                index = len(self.server.uploads)
                record = dict(thread=threading.current_thread(), done=threading.Event(),
                              worker_done=threading.Event(), worker_exit=None, owns_write=False)
                self.server.handlers[index] = record
            try:
                if self.server.rate_limit:
                    if self.server.post_status == "disconnect":
                        self.close_connection = True
                        return
                    mode = self.server.post_body
                    raw = {"normal": b"{}", "empty": b"", "nonjson": b"observed 429",
                           "oversize": b"x" * 32769, "stall": b"", "truncated": b"{}"}[mode]
                    self.send_response(self.server.post_status)
                    if self.server.post_status == 302:
                        self.send_header("Location", "/api/feedback-status?request_id=redirected")
                    self.send_header("Content-Length", str(100 if mode in ("stall", "truncated") else len(raw)))
                    self.end_headers()
                    self.wfile.flush()
                    if mode == "stall":
                        self.server.body_entered.set()
                        if not self.server.body_release.wait(30):
                            raise RuntimeError("POST body barrier timed out")
                    if mode == "truncated":
                        self.close_connection = True
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if index >= 3:
                    self.server.recovery_arrived.set()
                if self.server.delay_upload == index:
                    self.server.arrived.set()
                    if not self.server.release.wait(15):
                        raise RuntimeError("Original delivery barrier timed out")
                if self.server.reserve_winner and index != self.server.reserve_winner:
                    if not self.server.reserved.wait(10):
                        raise RuntimeError("Owning reservation barrier timed out")
                payload = worker._parse_payload(raw)
                token = worker.reserve(payload)
                if index == self.server.reserve_winner:
                    self.server.reserved.set()
                if token:
                    record["owns_write"] = True
                    with subprocess.Popen([sys.executable,str(WORKER),"execute",payload["request_id"],token],
                                          stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child:
                        try:
                            child.communicate(timeout=25)
                        finally:
                            if child.poll() is None:
                                child.kill()
                            child.communicate(timeout=3)
                            record["worker_exit"] = child.returncode
                            record["worker_done"].set()
                if self.server.disconnect:
                    self.server.status_mode = "unavailable"
                    self.close_connection = True
                else:
                    self.send(202,{"ok":True})
            except Exception as exc:
                self.server.errors.append(type(exc).__name__)
                raise
            finally:
                record["done"].set()

        def log_message(self,*args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    server.uploads = []
    server.handlers, server.errors = {}, []
    server.reserve_winner = None
    server.reserved = threading.Event()
    server.gate_readback = False
    server.readback_entered, server.readback_release = threading.Event(), threading.Event()
    server.lock = threading.Lock()
    server.rate_limit = True
    server.post_body, server.post_status = "normal", 429
    server.status_calls, server.status_threads = [], []
    server.status_entered, server.status_release = threading.Event(), threading.Event()
    server.body_entered, server.body_release = threading.Event(), threading.Event()
    server.status_mode = "normal"
    server.delay_upload = None
    server.disconnect = False
    server.arrived, server.release = threading.Event(), threading.Event()
    server.recovery_arrived = threading.Event()
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    monkeypatch.setenv("AVIBE_FEEDBACK_TEST_SHARE_BASE_URL",f"http://127.0.0.1:{server.server_port}")
    upstream_get = github.RequestHandlerClass.do_GET

    def readback(self):
        if server.gate_readback and self.path.startswith("/repos/avibe-bot/avibe/issues/"):
            server.readback_entered.set()
            if not server.readback_release.wait(10):
                raise RuntimeError("Readback barrier timed out")
        return upstream_get(self)

    monkeypatch.setattr(github.RequestHandlerClass, "do_GET", readback)
    try:
        yield server, github, worker
    finally:
        # Stop accepting work, release every barrier, then join owned handlers.
        # Their finally blocks reap worker subprocesses before signaling done.
        server.release.set()
        server.reserved.set()
        server.readback_release.set()
        server.status_release.set()
        server.body_release.set()
        server.shutdown()
        thread.join(2)
        with server.lock:
            handlers = list(server.handlers.values())
            status_threads = list(server.status_threads)
        for record in handlers:
            record["thread"].join(28)
            assert not record["thread"].is_alive(), "Intake handler did not terminate"
        for status_thread in status_threads:
            status_thread.join(3)
            assert not status_thread.is_alive(), "Status handler did not terminate"
        server.server_close()
        assert not server.errors, server.errors



COMMAND = [sys.executable,str(HELPER),"submit","bug","Recover delivery 中文", "--public-confirmed"]
BODY = "Only approved bytes & #\n中文".encode()


def run_submit():
    return subprocess.run(COMMAND,input=BODY,capture_output=True,timeout=15)


def outbox():
    return Path(os.environ["AVIBE_HOME"]) / "state/feedback-intake/outbox.sqlite"


def row():
    with sqlite3.connect(outbox()) as db:
        db.row_factory=sqlite3.Row
        return db.execute("SELECT * FROM attempts").fetchone()


def test_kill_after_durable_retry_intent_before_upload(intake):
    server, upstream, _ = intake
    first = json.loads(run_submit().stdout)
    request_id = first["request_id"]
    server.rate_limit = False
    # Real helper body, intercepted exactly at network entry. Kill the process
    # after its actual SQLite claim commit, without adding production hooks.
    script = '''
import importlib.util,sys,os,signal
spec=importlib.util.spec_from_file_location('client',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
original=c._request
def boundary(path,**kw):
    if kw.get('body') is not None: os.kill(os.getpid(),signal.SIGKILL)
    return original(path,**kw)
c._request=boundary
sys.argv=sys.argv[1:]
sys.exit(c.main())
'''
    killed = subprocess.run([sys.executable,"-c",script,*COMMAND[1:]],input=BODY,capture_output=True,timeout=10)
    assert killed.returncode == -signal.SIGKILL
    assert row()["retryable"] == 1 and row()["phase"] == "sending"
    assert len(server.uploads) == 1
    resume = subprocess.run([sys.executable,str(HELPER),"resume",request_id],capture_output=True)
    assert json.loads(resume.stdout)["state"] == "unknown" and len(server.uploads) == 1
    recovered = run_submit()
    assert recovered.returncode == 0, recovered.stderr.decode()
    assert json.loads(recovered.stdout)["request_id"] == request_id
    assert server.uploads[0] == server.uploads[1]
    assert len(upstream.issues) == 1 and row()["retryable"] == 0


@pytest.mark.parametrize("winner", [2, 3], ids=["original-wins", "recovery-wins"])
def test_original_delivery_races_explicit_recovery_one_github_write(intake, winner):
    server, upstream, worker = intake
    first=json.loads(run_submit().stdout)
    server.rate_limit=False
    server.delay_upload=2
    server.reserve_winner=winner
    server.gate_readback=True
    original=subprocess.Popen(COMMAND,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    recovery=None
    original.stdin.write(BODY);original.stdin.close()
    try:
        assert server.arrived.wait(5)
        original.kill();original.wait(3)
        assert worker._row(first["request_id"]) is None
        assert row()["phase"] == "sending" and row()["retryable"] == 1
        recovery=subprocess.Popen(COMMAND,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        recovery.stdin.write(BODY);recovery.stdin.close();recovery.stdin=None
        assert server.recovery_arrived.wait(5)
        server.release.set()
        # Exactly one actual worker owns the reservation. The other handler can
        # finish coalesced admission while the owner is still reading GitHub.
        assert server.readback_entered.wait(5)
        loser=3 if winner==2 else 2
        assert server.handlers[loser]["done"].wait(5)
        owner=server.handlers[winner]
        assert owner["owns_write"] and not owner["worker_done"].is_set()
        assert not owner["done"].is_set()
        assert not server.handlers[loser]["owns_write"]
        intermediate=subprocess.run([sys.executable,str(HELPER),"resume",first["request_id"]],capture_output=True,timeout=5)
        assert json.loads(intermediate.stdout)["state"]=="unknown"
        pending=worker._row(first["request_id"])
        assert pending["state"]=="unknown" and pending["issue_id"]==10100
        assert sum(call[0]=="POST" for call in upstream.calls)==1 and len(upstream.issues)==1
        server.readback_release.set()
        assert owner["worker_done"].wait(5) and owner["worker_exit"]==0
        for index in (2,3):
            assert server.handlers[index]["done"].wait(5)
            server.handlers[index]["thread"].join(2)
            assert not server.handlers[index]["thread"].is_alive()
        stdout,stderr=recovery.communicate(timeout=5)
        assert recovery.returncode in (0,2),stderr.decode()
        final=subprocess.run([sys.executable,str(HELPER),"resume",first["request_id"]],capture_output=True,timeout=5)
        expected=dict(schema_version=1,request_id=first["request_id"],state="created",issue_number=100,
                      issue_url="https://github.com/avibe-bot/avibe/issues/100")
        assert json.loads(final.stdout)==expected
        assert worker.receipt(first["request_id"])==expected
        assert sum(call[0]=="POST" for call in upstream.calls)==1
        assert len(upstream.issues)==1 and len(server.uploads)==3
        assert all(raw==server.uploads[0] for raw in server.uploads)
    finally:
        server.release.set();server.reserved.set();server.readback_release.set()
        if original.poll() is None:
            original.kill();original.wait(3)
        original.stdout.close();original.stderr.close()
        if recovery is not None:
            if recovery.poll() is None:
                recovery.kill()
            recovery.communicate(timeout=3)


@pytest.mark.parametrize("status_mode", [
    "busy", "unavailable", "disconnect", "stall", "invalid_json", "oversize", "malformed",
    "malformed404", "redirect",
])
def test_observed_429_survives_every_status_failure_until_fresh_absence(intake, status_mode):
    server, upstream, worker = intake
    server.status_mode = status_mode
    if status_mode == "stall":
        first_process = subprocess.Popen(
            COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            first_process.stdin.write(BODY)
            first_process.stdin.close()
            first_process.stdin = None
            assert server.status_entered.wait(5)
            assert row()["retryable"] == 1 and server.uploads
            # Preserve the real ten-second network timeout; the release is
            # cleanup, so a successful response cannot masquerade as timeout.
            stdout, stderr = first_process.communicate(timeout=13)
            assert first_process.returncode == 3, stderr.decode()
            first = subprocess.CompletedProcess(COMMAND, first_process.returncode, stdout, stderr)
        finally:
            server.status_release.set()
            if first_process.poll() is None:
                first_process.kill()
            first_process.communicate(timeout=3)
    else:
        first = run_submit()
    request_id = json.loads(first.stdout)["request_id"]
    if status_mode == "stall":
        server.status_mode = "busy"
    assert json.loads(first.stdout)["admission"] == "rate_limited"
    assert row()["request_id"] == request_id
    assert row()["retryable"] == 1 and row()["phase"] == "rate_limited"
    resumed = subprocess.run([sys.executable, str(HELPER), "resume", request_id], capture_output=True, timeout=15)
    assert json.loads(resumed.stdout)["admission"] == "rate_limited"
    suppressed = run_submit()
    assert json.loads(suppressed.stdout)["admission"] == "rate_limited"
    assert len(server.uploads) == 1
    server.status_mode = "normal"
    server.rate_limit = False
    resumed = subprocess.run([sys.executable, str(HELPER), "resume", request_id], capture_output=True, timeout=5)
    assert json.loads(resumed.stdout)["admission"] == "rate_limited" and len(server.uploads) == 1
    recovered = run_submit()
    assert recovered.returncode == 0, recovered.stderr.decode()
    assert json.loads(recovered.stdout)["request_id"] == request_id
    assert len(server.uploads) == 2 and server.uploads[0] == server.uploads[1]
    assert sum(call[0] == "POST" for call in upstream.calls) == len(upstream.issues) == 1
    assert worker.receipt(request_id)["state"] == "created" and row()["retryable"] == 0


@pytest.mark.parametrize("post_body", ["normal", "empty", "nonjson", "oversize", "truncated", "stall"])
def test_observed_429_is_saved_before_post_body_is_consumed(intake, post_body):
    server, upstream, worker = intake
    server.post_body = post_body
    if post_body == "stall":
        # Observe the committed callback with IPC, then leave the actual body
        # blocked until the normal network timeout returns. No shortened limit.
        script = '''
import importlib.util,socket,sys
spec=importlib.util.spec_from_file_location('c',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
channel=socket.socket(fileno=int(sys.argv[2]));channel.settimeout(5)
original=c._record_rejection
def recorded(*args):
 original(*args)
 channel.sendall(b'1')
c._record_rejection=recorded
sys.argv=[sys.argv[1],*sys.argv[3:]];sys.exit(c.main())
'''
        parent, child_socket = socket.socketpair()
        parent.settimeout(5)
        first_process = subprocess.Popen(
            [sys.executable, "-c", script, str(HELPER), str(child_socket.fileno()), *COMMAND[2:]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(child_socket.fileno(),),
        )
        child_socket.close()
        try:
            first_process.stdin.write(BODY)
            first_process.stdin.close()
            first_process.stdin = None
            assert parent.recv(1) == b"1" and server.body_entered.wait(5)
            assert row()["retryable"] == 1 and row()["phase"] == "rate_limited"
            assert first_process.poll() is None and server.status_calls == []
            stdout, stderr = first_process.communicate(timeout=13)
            assert first_process.returncode == 3, stderr.decode()
            first = subprocess.CompletedProcess(COMMAND, first_process.returncode, stdout, stderr)
        finally:
            server.body_release.set()
            parent.close()
            if first_process.poll() is None:
                first_process.kill()
            first_process.communicate(timeout=3)
    else:
        first = run_submit()
    request_id = json.loads(first.stdout)["request_id"]
    assert json.loads(first.stdout)["admission"] == "rate_limited"
    assert row()["request_id"] == request_id
    assert row()["retryable"] == 1 and row()["phase"] == "rate_limited"
    assert len(server.uploads) == 1
    server.rate_limit = False
    resumed = subprocess.run([sys.executable, str(HELPER), "resume", request_id], capture_output=True, timeout=5)
    assert json.loads(resumed.stdout)["admission"] == "rate_limited" and len(server.uploads) == 1
    recovered = run_submit()
    assert recovered.returncode == 0, recovered.stderr.decode()
    assert json.loads(recovered.stdout)["request_id"] == request_id
    assert server.uploads[0] == server.uploads[1]
    assert sum(call[0] == "POST" for call in upstream.calls) == len(upstream.issues) == 1
    assert worker.receipt(request_id)["state"] == "created"


@pytest.mark.parametrize("post_status", [202, 302, 400, 503, "disconnect"])
def test_no_rejection_is_inferred_from_non429_or_error_text(intake, post_status):
    server, upstream, _ = intake
    server.post_status, server.post_body = post_status, "nonjson"
    first = run_submit()
    request_id = json.loads(first.stdout)["request_id"]
    assert json.loads(first.stdout)["state"] == "unknown"
    assert row()["retryable"] == 0
    assert all("redirected" not in path for path in server.status_calls)
    server.status_mode = "busy"
    assert json.loads(run_submit().stdout)["state"] == "unknown"
    server.status_mode = "normal"
    resumed = subprocess.run([sys.executable, str(HELPER), "resume", request_id], capture_output=True, timeout=5)
    assert json.loads(resumed.stdout)["state"] == "unknown"
    assert json.loads(run_submit().stdout)["state"] == "unknown"
    assert len(server.uploads) == 1 and not upstream.issues and row()["retryable"] == 0


def test_observed_429_survives_a_kill_before_status_poll(intake):
    server, upstream, worker = intake
    # Kill immediately after the real transaction commits, before response body
    # consumption or the first GET. No timing assumption or file polling.
    script = """
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location('client', sys.argv[1])
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
original = client._record_rejection
def recorded(*args):
    original(*args)
    os.kill(os.getpid(), signal.SIGKILL)
client._record_rejection = recorded
sys.argv = sys.argv[1:]
client.main()
"""
    killed = subprocess.run([sys.executable, "-c", script, *COMMAND[1:]],
                            input=BODY, capture_output=True, timeout=5)
    assert killed.returncode == -signal.SIGKILL
    request_id = row()["request_id"]
    assert row()["retryable"] == 1 and row()["phase"] == "rate_limited"
    assert server.status_calls == [] and len(server.uploads) == 1
    assert worker._row(request_id) is None
    resumed = subprocess.run([sys.executable, str(HELPER), "resume", request_id],
                             capture_output=True, timeout=5)
    assert json.loads(resumed.stdout)["admission"] == "rate_limited"
    assert len(server.uploads) == 1
    server.rate_limit = False
    recovered = run_submit()
    assert recovered.returncode == 0, recovered.stderr.decode()
    assert json.loads(recovered.stdout)["request_id"] == request_id
    assert server.uploads[0] == server.uploads[1]
    assert sum(call[0] == "POST" for call in upstream.calls) == len(upstream.issues) == 1
    assert worker.receipt(request_id)["state"] == "created" and row()["retryable"] == 0


@pytest.mark.parametrize("status_mode", ["busy", "unavailable", "disconnect", "invalid_json",
                                         "malformed", "malformed404", "redirect"])
def test_interrupted_retry_recovery_requires_available_absent_receipt(intake, status_mode):
    server, _, _ = intake
    run_submit()
    client = load_client()
    with client._database() as db:
        assert client._claim_recovery(db, row()["request_id"]) is not None
    db.close()
    server.rate_limit = False
    server.status_mode = status_mode
    result = run_submit()
    assert json.loads(result.stdout)["state"] == "unknown"
    assert len(server.uploads) == 1 and row()["retryable"] == 1


@pytest.mark.parametrize("state",["pending","unknown","failed","created"])
def test_observed_reservation_permanently_suppresses_recovery(intake,state):
    server, upstream, worker=intake
    run_submit();server.rate_limit=False
    payload=worker._parse_payload(server.uploads[0]);token=worker.reserve(payload)
    if state=="created":
        subprocess.run([sys.executable,str(WORKER),"execute",payload["request_id"],token],check=True)
    elif state!="pending":
        worker._update(payload["request_id"],state=state)
    result=run_submit()
    assert json.loads(result.stdout)["state"]==state
    assert len(server.uploads)==1 and row()["retryable"]==0
    server.status_mode="unavailable"
    run_submit()
    assert len(server.uploads)==1
    assert len(upstream.issues)==(1 if state=="created" else 0)


def test_concurrent_late_results_cannot_regress_receipt_or_eligibility(intake):
    server, _, _=intake
    run_submit();request_id=row()["request_id"]
    client=load_client();a=client._database();b=client._database()
    old=client._claim_recovery(a,request_id)
    new=client._claim_recovery(b,request_id)
    client._record_rejection(a,request_id,old)
    assert row()["generation"]==new and row()["phase"]=="sending"
    unknown=dict(schema_version=1,request_id=request_id,state="unknown")
    client._observe(b,request_id,unknown)
    client._record_rejection(a,request_id,new)
    client._observe(a,request_id,dict(unknown,state="pending"))
    assert json.loads(row()["receipt"])["state"]=="unknown" and row()["retryable"]==0
    created=dict(unknown,state="created",issue_number=12,issue_url="https://github.com/avibe-bot/avibe/issues/12")
    client._observe(a,request_id,created)
    client._observe(b,request_id,unknown)
    assert json.loads(row()["receipt"])==created and row()["payload"] is None
    a.close();b.close()


def test_initial_unknown_and_legacy_rows_never_gain_eligibility_from_404(intake):
    server,_,_=intake
    server.rate_limit=False
    # Crash the initial helper after persistence but before HTTP. No historical
    # rejection exists, so even explicit identical submit plus404 cannot replay.
    script='''
import importlib.util,sys,os
spec=importlib.util.spec_from_file_location('c',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
c._request=lambda *a,**k: os._exit(91)
sys.argv=sys.argv[1:];c.main()
'''
    result=subprocess.run([sys.executable,"-c",script,*COMMAND[1:]],input=BODY,capture_output=True)
    assert result.returncode==91
    again=run_submit()
    assert json.loads(again.stdout)["state"]=="unknown" and not server.uploads
    assert row()["retryable"]==0


def test_retry_write_completed_but_response_and_receipt_unavailable(intake):
    server,upstream,worker=intake
    request_id=json.loads(run_submit().stdout)["request_id"]
    server.rate_limit=False;server.disconnect=True
    result=run_submit()
    assert json.loads(result.stdout)["state"]=="unknown"
    assert worker.receipt(request_id)["state"]=="created" and len(upstream.issues)==1
    assert row()["phase"]=="sending" and row()["retryable"]==1
    # Neither status-only resume nor explicit submit may upload while status is
    # unavailable. The prior429 cannot describe this possibly completed retry.
    run_submit()
    subprocess.run([sys.executable,str(HELPER),"resume",request_id],capture_output=True)
    assert len(server.uploads)==2
    server.status_mode="normal"
    settled=run_submit()
    assert json.loads(settled.stdout)["state"]=="created"
    assert len(server.uploads)==2 and len(upstream.issues)==1 and row()["retryable"]==0


def test_legacy_outbox_loads_conservatively(intake):
    server,_,_=intake
    run_submit()
    request_id=row()["request_id"]
    # Recreate the first unlaunched schema with an unknown attempt, preserving
    # its exact bytes. A GET404 must not invent the absent rejection evidence.
    with sqlite3.connect(outbox()) as db:
        db.execute("CREATE TABLE old AS SELECT request_id,digest,payload,receipt FROM attempts")
        db.execute("DROP TABLE attempts")
        db.execute("ALTER TABLE old RENAME TO attempts")
    server.rate_limit=False
    again=run_submit()
    assert json.loads(again.stdout)==dict(schema_version=1,request_id=request_id,state="unknown")
    assert len(server.uploads)==1 and row()["retryable"]==0


@pytest.mark.parametrize("state", ["pending", "unknown", "failed", "created"])
def test_concurrent_status_process_settles_before_late_rejection(intake, state):
    server, upstream, worker = intake
    request_id = json.loads(run_submit().stdout)["request_id"]
    # Pause at the actual 429 persistence boundary; a separate status process
    # observes a real receiver row before the late callback tries its CAS.
    script = '''
import importlib.util,sys,socket
spec=importlib.util.spec_from_file_location('c',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
channel=socket.socket(fileno=int(sys.argv[2]));channel.settimeout(8)
original=c._record_rejection
def late(*args):
 channel.sendall(b'ready')
 if channel.recv(1)!=b'1':raise RuntimeError('Missing release')
 return original(*args)
c._record_rejection=late
sys.argv=[sys.argv[1],*sys.argv[3:]];sys.exit(c.main())
'''
    parent, child_socket = socket.socketpair()
    parent.settimeout(5)
    late = subprocess.Popen([sys.executable, "-c", script, str(HELPER), str(child_socket.fileno()), *COMMAND[2:]],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            pass_fds=(child_socket.fileno(),))
    child_socket.close()
    late.stdin.write(BODY); late.stdin.close(); late.stdin = None
    try:
        assert parent.recv(5) == b"ready"
        payload = worker._parse_payload(server.uploads[0])
        token = worker.reserve(payload)
        if state == "created":
            subprocess.run([sys.executable, str(WORKER), "execute", request_id, token], check=True, timeout=5)
        elif state != "pending":
            worker._update(request_id, state=state)
        observed = subprocess.run([sys.executable, str(HELPER), "resume", request_id], capture_output=True, timeout=5)
        expected = worker.receipt(request_id)
        assert json.loads(observed.stdout) == expected
        server.status_mode = "busy"
        parent.sendall(b"1")
        stdout, stderr = late.communicate(timeout=5)
        assert json.loads(stdout) == expected, stderr.decode()
        assert row()["retryable"] == 0 and row()["phase"] == "observed"
        server.status_mode = "unavailable"
        assert json.loads(run_submit().stdout) == expected
        assert len(server.uploads) == 2
        assert len(upstream.issues) == int(state == "created")
    finally:
        parent.close()
        if late.poll() is None:
            late.kill()
        late.communicate(timeout=3)
