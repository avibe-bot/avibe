"""Interrupted delivery recovery through real reservation/claim and GitHub HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import signal
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
            except BrokenPipeError:
                pass

        def do_GET(self):  # noqa: N802
            mode = self.server.status_mode
            if mode == "unavailable":
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
                    self.send(429,{})
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
        server.shutdown()
        thread.join(2)
        with server.lock:
            handlers = list(server.handlers.values())
        for record in handlers:
            record["thread"].join(28)
            assert not record["thread"].is_alive(), "Intake handler did not terminate"
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


@pytest.mark.parametrize("status_mode",["unavailable","malformed","malformed404","redirect"])
def test_interrupted_retry_recovery_requires_available_absent_receipt(intake,status_mode):
    server, _, _=intake
    run_submit()
    client=load_client();db=client._database();request_id=row()["request_id"]
    assert client._claim_recovery(db,request_id) is not None
    db.close()
    server.rate_limit=False;server.status_mode=status_mode
    result=run_submit()
    assert json.loads(result.stdout)["state"] == "unknown"
    assert len(server.uploads)==1 and row()["retryable"]==1


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


def test_concurrent_status_process_settles_before_late_rejection(intake,tmp_path):
    server,_,worker=intake
    request_id=json.loads(run_submit().stdout)["request_id"]
    # Pause one actual helper after it has read the old404 response but before
    # saving its late429. Another process observes the real reserved receipt.
    script='''
import importlib.util,sys,pathlib,time
spec=importlib.util.spec_from_file_location('c',sys.argv[1]);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
marker=pathlib.Path(sys.argv[2]);release=pathlib.Path(sys.argv[3]);original=c._record_rejection
def late(*a):
 marker.write_text('ready')
 while not release.exists():time.sleep(.01)
 return original(*a)
c._record_rejection=late
sys.argv=[sys.argv[1],*sys.argv[4:]];sys.exit(c.main())
'''
    marker,release=tmp_path/'ready',tmp_path/'release'
    late=subprocess.Popen([sys.executable,"-c",script,str(HELPER),str(marker),str(release),*COMMAND[2:]],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    late.stdin.write(BODY);late.stdin.close();late.stdin=None
    try:
        import time
        for _ in range(500):
            if marker.exists():break
            time.sleep(.01)
        assert marker.exists()
        payload=worker._parse_payload(server.uploads[0]);worker.reserve(payload)
        worker._update(request_id,state='unknown')
        observed=subprocess.run([sys.executable,str(HELPER),'resume',request_id],capture_output=True,timeout=10)
        assert json.loads(observed.stdout)['state']=='unknown'
        release.write_text('go')
        late.communicate(timeout=5)
        assert row()['retryable']==0 and row()['phase']=='observed'
        assert json.loads(row()['receipt'])['state']=='unknown'
        server.status_mode='unavailable';run_submit()
        assert len(server.uploads)==2
    finally:
        release.write_text('go')
        if late.poll() is None:late.kill();late.wait()
