"""Official intake: strict input, durable receipts, fixed Vault/GitHub consumer.

Run `submit` outside Vault; it reserves before handing only the reservation ID to
`vibe vault run`. Vault's stdin belongs to sealed envelopes, not report content.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib import error, parse, request

SCHEMA_VERSION = 1
TARGET_FULL_NAME = "avibe-bot/avibe"
TARGET_API_BASE = "https://api.github.com"
TARGET_HTML_BASE = "https://github.com/avibe-bot/avibe/issues"
EXPECTED_ACTOR = "cs-agent-bot"
EXPECTED_ACTOR_ID = 272811739
EXPECTED_REPOSITORY_ID = 1035030370
TOKEN_ENV = "AVIBE_FEEDBACK_GITHUB_TOKEN"
DATABASE_ENV = "AVIBE_FEEDBACK_DATABASE"
MAX_REQUEST_BYTES = 32768
MAX_BODY_BYTES = 20000
MAX_RECEIPTS = 10000
MINUTE_LIMIT = 5
DAY_LIMIT = 30
MAX_UPSTREAM_WRITES = 2
EXECUTION_SECONDS = 20
MAX_OUTPUT_BYTES = 32768
MAX_UPSTREAM_RESPONSE_BYTES = 262144


class FeedbackError(Exception):
    def __init__(self, status=503):
        self.status = status


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _uuid_v4(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).version == 4
    except ValueError:
        return False


def _parse_payload(raw):
    if len(raw) > MAX_REQUEST_BYTES:
        raise FeedbackError(413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FeedbackError(400)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "request_id", "kind", "title", "body", "public_consent"
        }:
            raise ValueError()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        if not _uuid_v4(value["request_id"]) or value["public_consent"] is not True:
            raise ValueError()
        if type(value["kind"]) is not str or value["kind"] not in ("bug", "feature"):
            raise ValueError()
        title, body = value["title"], value["body"]
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError()
        if any(ord(c) < 32 or c in "\x7f\x85\u2028\u2029" for c in title):
            raise ValueError()
        title.encode("utf-8")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError()
        if "\0" in body:
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise FeedbackError(400) from None


def _database_path():
    configured = os.environ.get(DATABASE_ENV, "")
    path = Path(configured)
    avibe_home = Path(os.environ.get("AVIBE_HOME", str(Path.home() / ".avibe"))).resolve()
    forbidden = (avibe_home, Path.home() / ".vibe_remote", Path(__file__).resolve().parent)
    resolved = path.resolve()
    if not configured or not path.is_absolute() or path != resolved:
        raise FeedbackError()
    if any(resolved == root or root in resolved.parents for root in forbidden):
        raise FeedbackError()
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise FeedbackError()
    return path


@contextmanager
def _database():
    path = _database_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise FeedbackError()
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS receipts (
                request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, token TEXT NOT NULL, deadline REAL NOT NULL,
                issue_id INTEGER, issue_number INTEGER, issue_url TEXT,
                reconciliations INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS limits (
                name TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
            );
        """)
        yield connection
    finally:
        connection.close()


def reserve(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM receipts WHERE request_id=?", (payload["request_id"],)).fetchone()
        if row:
            if row["digest"] != digest:
                raise FeedbackError(409)
            return None
        if db.execute("SELECT count(*) FROM receipts").fetchone()[0] >= MAX_RECEIPTS:
            raise FeedbackError()
        now = time.time()
        for name, seconds, limit in (("minute", 60, MINUTE_LIMIT), ("day", 86400, DAY_LIMIT)):
            window = int(now) // seconds
            row = db.execute("SELECT * FROM limits WHERE name=?", (name,)).fetchone()
            count = row["count"] if row and row["window"] == window else 0
            if count >= limit:
                raise FeedbackError(429)
            db.execute("INSERT OR REPLACE INTO limits VALUES (?, ?, ?)", (name, window, count + 1))
        token = uuid.uuid4().hex
        db.execute("INSERT INTO receipts(request_id,digest,payload,state,token,deadline) VALUES(?,?,?,'pending',?,?)",
                   (payload["request_id"], digest, encoded, token, now + EXECUTION_SECONDS))
        db.execute("COMMIT")
        return token


def _row(request_id):
    with _database() as db:
        return db.execute("SELECT * FROM receipts WHERE request_id=?", (request_id,)).fetchone()


def _update(request_id, **fields):
    with _database() as db:
        db.execute("UPDATE receipts SET " + ",".join(f"{key}=?" for key in fields) + " WHERE request_id=?",
                   (*fields.values(), request_id))


def _fail_pending(request_id):
    with _database() as db:
        db.execute("UPDATE receipts SET state='failed' WHERE request_id=? AND state='pending'", (request_id,))


def receipt(request_id):
    row = _row(request_id)
    if row is None:
        raise FeedbackError(404)
    state = row["state"]
    if state == "pending" and time.time() >= row["deadline"]:
        _fail_pending(request_id)
        # An atomic write claim may have won concurrently. Never project over
        # that evidence; execute also checks the deadline in its claim.
        row = _row(request_id)
        state = row["state"]
    result = {"schema_version": 1, "request_id": request_id, "state": state}
    if state == "created":
        result.update(issue_number=row["issue_number"], issue_url=row["issue_url"])
    return result


@contextmanager
def _write_slot():
    # Kernel-owned locks last until actual worker exit, including SIGKILL. No
    # expiring lease can admit a third worker while a stalled writer is alive.
    handles = []
    try:
        for index in range(MAX_UPSTREAM_WRITES):
            fd = os.open(str(_database_path()) + f".slot{index}", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handles.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            yield
            return
        raise FeedbackError(429)
    finally:
        for fd in handles:
            os.close(fd)


def _api_base():
    if os.environ.get("AVIBE_FEEDBACK_TEST_MODE") == "1":
        candidate = os.environ.get("AVIBE_FEEDBACK_TEST_API_BASE", "")
        url = parse.urlsplit(candidate)
        if url.scheme == "http" and url.hostname in ("127.0.0.1", "::1") and url.port and not (
            url.username or url.password or url.path or url.query or url.fragment
        ):
            return candidate
        raise FeedbackError()
    return TARGET_API_BASE


def _github(token, method, path, body=None):
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = request.Request(_api_base() + path, data=raw, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "avibe-feedback-intake/1",
        "Content-Type": "application/json",
    })
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=4)
    except error.HTTPError as exc:
        response = exc
    except (OSError, error.URLError):
        raise FeedbackError() from None
    with response:
        raw = response.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
        if len(raw) > MAX_UPSTREAM_RESPONSE_BYTES:
            raise FeedbackError()
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            value = None
        return response.code, value if isinstance(value, dict) else {}


def _binding(token):
    status, actor = _github(token, "GET", "/user")
    if status != 200 or actor.get("login") != EXPECTED_ACTOR or actor.get("id") != EXPECTED_ACTOR_ID:
        raise FeedbackError()
    status, repo = _github(token, "GET", f"/repos/{TARGET_FULL_NAME}")
    if status != 200 or repo.get("id") != EXPECTED_REPOSITORY_ID or repo.get("full_name") != TARGET_FULL_NAME:
        raise FeedbackError()
    if repo.get("private") is not False or repo.get("has_issues") is not True:
        raise FeedbackError()


def _identity(issue):
    number, issue_id = issue.get("number"), issue.get("id")
    if type(number) is not int or number <= 0 or type(issue_id) is not int or issue_id <= 0:
        raise FeedbackError()
    url = f"{TARGET_HTML_BASE}/{number}"
    if issue.get("html_url") != url or issue.get("repository_url") != f"{TARGET_API_BASE}/repos/{TARGET_FULL_NAME}":
        raise FeedbackError()
    return issue_id, number, url


def _verify(token, row):
    status, issue = _github(token, "GET", f"/repos/{TARGET_FULL_NAME}/issues/{row['issue_number']}")
    payload = json.loads(row["payload"])
    if status != 200 or _identity(issue) != (row["issue_id"], row["issue_number"], row["issue_url"]):
        raise FeedbackError()
    user = issue.get("user", {})
    if not isinstance(user, dict) or user.get("login") != EXPECTED_ACTOR or user.get("id") != EXPECTED_ACTOR_ID:
        raise FeedbackError()
    if issue.get("title") != payload["title"] or issue.get("body") != payload["body"] or "pull_request" in issue:
        raise FeedbackError()
    _update(row["request_id"], state="created")


def execute(request_id, reservation_token, *, reconcile=False):
    row = _row(request_id)
    if not row or row["token"] != reservation_token:
        raise FeedbackError()
    remaining = row["deadline"] - time.time()
    if remaining <= 0:
        if not reconcile:
            _fail_pending(request_id)
        raise FeedbackError()
    # Independent hard bound survives CLI death and detached avault children.
    # The default signal terminates without releasing the slot early in Python.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        with _write_slot():
            token = os.environ.get(TOKEN_ENV, "")
            if not token:
                raise FeedbackError()
            if reconcile:
                _binding(token)
                _verify(token, row)
                return
            _binding(token)
            with _database() as db:
                changed = db.execute("UPDATE receipts SET state='unknown' WHERE request_id=? AND state='pending' AND deadline>?",
                                     (request_id, time.time())).rowcount
            if changed != 1:
                _fail_pending(request_id)
                return
            payload = json.loads(row["payload"])
            status, issue = _github(token, "POST", f"/repos/{TARGET_FULL_NAME}/issues",
                                    {"title": payload["title"], "body": payload["body"]})
            if status in (400, 401, 403, 404, 409, 422, 429):
                _update(request_id, state="failed")
                raise FeedbackError()
            if status != 201:
                raise FeedbackError()
            issue_id, number, url = _identity(issue)
            _update(request_id, issue_id=issue_id, issue_number=number, issue_url=url)
            _verify(token, _row(request_id))
    except FeedbackError:
        # Slot exhaustion or missing credentials happens before this worker
        # claims the pending row. Persist that known no-write outcome, without
        # ever downgrading unknown/created evidence from a claimed operation.
        if not reconcile:
            _fail_pending(request_id)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _vault(request_id, token, *, reconcile=False):
    # psutil is an existing avibe-os dependency. avault deliberately creates a
    # separate session, so killing only the CLI process group is insufficient.
    import psutil

    env = dict(os.environ)
    for name in (TOKEN_ENV, "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    command = [os.environ.get("AVIBE_FEEDBACK_VAULT_BIN", "vibe"), "vault", "run", "--no-approval-wait",
               "--env", TOKEN_ENV, "--", sys.executable, str(Path(__file__).resolve()),
               "execute", request_id, token]
    if reconcile:
        command.append("--reconcile")
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    parent = psutil.Process(child.pid)
    descendants = {}
    deadline = time.monotonic() + EXECUTION_SECONDS + 1
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while child.poll() is None or selector.get_map():
                try:
                    for process in parent.children(recursive=True):
                        descendants[process.pid] = process
                except psutil.NoSuchProcess:
                    pass
                if time.monotonic() >= deadline:
                    raise FeedbackError(504)
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                    output.extend(data)
                    if len(output) > MAX_OUTPUT_BYTES:
                        raise FeedbackError()
            if child.returncode != 0:
                raise FeedbackError()
    finally:
        try:
            for process in parent.children(recursive=True):
                descendants[process.pid] = process
        except psutil.NoSuchProcess:
            pass
        # Stop the spawner before descendants; psutil binds each PID's identity.
        for process in (parent, *descendants.values()):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        child.wait(timeout=2)
        child.stdout.close()


def submit(raw):
    payload = _parse_payload(raw)
    token = reserve(payload)
    if token:
        _vault(payload["request_id"], token)
    return 202


def reconcile(request_id):
    # Operator-only CLI; public status never initiates authenticated egress.
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM receipts WHERE request_id=?", (request_id,)).fetchone()
        if not row or row["state"] != "unknown" or row["issue_id"] is None or row["reconciliations"] >= 3:
            raise FeedbackError(409)
        if time.time() <= row["deadline"]:
            raise FeedbackError(409)
        token = uuid.uuid4().hex
        db.execute("UPDATE receipts SET token=?,deadline=?,reconciliations=reconciliations+1 WHERE request_id=?",
                   (token, time.time() + EXECUTION_SECONDS, request_id))
        db.execute("COMMIT")
    _vault(request_id, token, reconcile=True)
    return 200


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("submit")
    for command in ("status", "reconcile", "execute"):
        item = sub.add_parser(command)
        item.add_argument("request_id")
        if command == "execute":
            item.add_argument("token")
            item.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    result = {}
    try:
        if args.command != "submit" and not _uuid_v4(args.request_id):
            raise FeedbackError(404)
        if args.command == "submit":
            status = submit(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        elif args.command == "status":
            result["receipt"] = receipt(args.request_id)
            status = 200
        elif args.command == "reconcile":
            status = reconcile(args.request_id)
        else:
            execute(args.request_id, args.token, reconcile=args.reconcile)
            return 0
    except FeedbackError as exc:
        if args.command == "execute":
            return 1
        status = exc.status
    except Exception:
        if args.command == "execute":
            return 1
        status = 503
    print(json.dumps({"status": status, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
