"""Publish an approved public report; resume saved attempts without another POST."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

OFFICIAL_SHARE_BASE_URL = "https://avibe-feedback-app.avibe.bot/p/132vCvND49U"
OFFICIAL_POSTING_ACTOR = "cs-agent-bot"
OFFICIAL_REPOSITORY = "avibe-bot/avibe"
MAX_REQUEST_BYTES = 32768
MAX_BODY_BYTES = 20000


class TotalDeadline(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _share_base():
    if os.environ.get("AVIBE_FEEDBACK_TEST_MODE") == "1":
        value = os.environ.get("AVIBE_FEEDBACK_TEST_SHARE_BASE_URL", "")
        url = urllib.parse.urlsplit(value)
        if url.scheme == "http" and url.hostname == "127.0.0.1" and url.port and not (
            url.username or url.password or url.path or url.query or url.fragment
        ):
            return value
        raise SystemExit("invalid isolated test destination")
    return OFFICIAL_SHARE_BASE_URL


def _request(path, *, body=None):
    req = urllib.request.Request(_share_base() + path, data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError()
        return response.code, json.loads(raw)


def _valid_receipt(receipt, request_id):
    if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        return False
    if receipt.get("request_id") != request_id or receipt.get("state") not in ("pending", "created", "unknown", "failed"):
        return False
    fields = {"schema_version", "request_id", "state"}
    if receipt["state"] == "created":
        fields |= {"issue_number", "issue_url"}
        number = receipt.get("issue_number")
        if type(number) is not int or number <= 0 or receipt.get("issue_url") != f"https://github.com/{OFFICIAL_REPOSITORY}/issues/{number}":
            return False
    return set(receipt) == fields


def _database():
    avibe_home = Path(os.environ.get("AVIBE_HOME", str(Path.home() / ".avibe"))).expanduser()
    root = Path(os.environ.get("AVIBE_FEEDBACK_OUTBOX", str(avibe_home / "state/feedback-intake"))).expanduser()
    if not root.is_absolute():
        raise SystemExit("feedback outbox must be an absolute path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "outbox.sqlite", timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS attempts (request_id TEXT PRIMARY KEY, digest TEXT UNIQUE, payload BLOB, receipt TEXT)")
    db.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
    if "retryable" not in columns:
        db.execute("ALTER TABLE attempts ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0")
    if "phase" not in columns:
        db.execute("ALTER TABLE attempts ADD COLUMN phase TEXT NOT NULL DEFAULT 'unknown'")
        # The old flag proves only a previous definitive 429+404. A missing flag
        # or saved receipt cannot be promoted by a later 404 into replay rights.
        db.execute("UPDATE attempts SET phase='rate_limited' WHERE retryable=1 AND receipt IS NULL")
        db.execute("UPDATE attempts SET retryable=0,phase='observed' WHERE receipt IS NOT NULL")
    if "generation" not in columns:
        db.execute("ALTER TABLE attempts ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    db.commit()
    return db


def _attempt(db, request_id):
    return db.execute("SELECT * FROM attempts WHERE request_id=?", (request_id,)).fetchone()


def _saved_receipt(row):
    try:
        value = json.loads(row["receipt"]) if row["receipt"] else None
    except (ValueError, TypeError, RecursionError):
        return None
    return value if _valid_receipt(value, row["request_id"]) else None


def _observe(db, request_id, found):
    """Merge server evidence monotonically, even across concurrent processes."""
    if not _valid_receipt(found, request_id):
        return False
    with db:
        db.execute("BEGIN IMMEDIATE")
        saved = _saved_receipt(_attempt(db, request_id))
        # Terminal evidence is immutable locally; unknown cannot regress to
        # pending. Every valid receipt permanently revokes delivery recovery.
        if saved and (saved["state"] in ("created", "failed") or
                      saved["state"] == "unknown" and found["state"] == "pending"):
            found = saved
        db.execute("UPDATE attempts SET receipt=?,retryable=0,phase='observed',"
                   "payload=CASE WHEN ? IN ('created','failed') THEN NULL ELSE payload END WHERE request_id=?",
                   (json.dumps(found), found["state"], request_id))
    return True


def _claim_recovery(db, request_id):
    """Called only after a fresh 404 during an explicit identical submit."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = _attempt(db, request_id)
        if not row["retryable"] or row["receipt"] is not None or row["payload"] is None:
            return None
        generation = row["generation"] + 1
        # Retain historical rejection separately. A crash or lost response now
        # means unknown delivery, with explicit recovery still subject to GET.
        db.execute("UPDATE attempts SET phase='sending',generation=? WHERE request_id=?", (generation, request_id))
    return generation


def _record_rejection(db, request_id, generation):
    # A late response cannot overwrite newer sending intent or restore rights
    # over another process's receipt. Only this exact send's 429+404 qualifies.
    with db:
        db.execute("UPDATE attempts SET retryable=1,phase='rate_limited' WHERE request_id=? "
                   "AND generation=? AND phase='sending' AND receipt IS NULL", (request_id, generation))


def _poll(request_id):
    return _request("/api/feedback-status?" + urllib.parse.urlencode({"request_id": request_id}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, epilog="Only explicit identical submit can recover a previously rate-limited delivery after a fresh absent receipt. Resume never uploads.")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("kind", choices=("bug", "feature"))
    submit.add_argument("title")
    submit.add_argument("--body-file", default="-")
    submit.add_argument("--public-confirmed", action="store_true", required=True,
                        help="Only after the user approves this exact public title/body, destination and posting identity")
    resume = sub.add_parser("resume")
    resume.add_argument("request_id")
    args = parser.parse_args()
    db = _database()
    generation = None
    existing = False
    if args.command == "submit":
        if args.body_file == "-":
            raw = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
        else:
            with open(args.body_file, "rb") as source:
                raw = source.read(MAX_BODY_BYTES + 1)
        try:
            body = raw.decode("utf-8")
            args.title.encode("utf-8")
        except UnicodeError:
            raise SystemExit("title and body must be valid UTF-8") from None
        if len(raw) > MAX_BODY_BYTES or not body.strip() or "\0" in body:
            raise SystemExit("body must be nonempty and at most 20000 UTF-8 bytes")
        if not args.title.strip() or len(args.title) > 200 or any(ord(c) < 32 or c in "\x7f\x85\u2028\u2029" for c in args.title):
            raise SystemExit("title must be one line and at most 200 characters")
        content = dict(kind=args.kind, title=args.title, body=body)
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM attempts WHERE digest=?", (digest,)).fetchone()
        if row:
            request_id, payload = row["request_id"], row["payload"]
            existing = True
        else:
            request_id = str(uuid.uuid4())
            payload = json.dumps(dict(schema_version=1, request_id=request_id, public_consent=True, **content),
                                 ensure_ascii=False, separators=(",", ":")).encode()
            if len(payload) > MAX_REQUEST_BYTES:
                raise SystemExit("encoded request exceeds 32768 bytes")
            db.execute("INSERT INTO attempts(request_id,digest,payload,phase,generation) VALUES(?,?,?,'sending',1)",
                       (request_id, digest, payload))
            generation = 1
        db.commit()  # Exact bytes and ID are durable before any network operation.
    else:
        request_id = args.request_id
        try:
            if str(uuid.UUID(request_id)) != request_id or uuid.UUID(request_id).version != 4:
                raise ValueError()
        except ValueError:
            raise SystemExit("request_id must be a canonical UUIDv4") from None
        if not db.execute("SELECT 1 FROM attempts WHERE request_id=?", (request_id,)).fetchone():
            raise SystemExit("saved attempt not found")
    print(f"Public destination: {OFFICIAL_REPOSITORY}; posting identity: {OFFICIAL_POSTING_ACTOR}; attempt: {request_id}", file=sys.stderr)
    # A bounded helper can be interrupted at any point: resume always does GET.
    def timeout(*_):
        raise TotalDeadline()
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(35)
    try:
        if existing:
            row = _attempt(db, request_id)
            if row["retryable"] and row["receipt"] is None:
                # No upload if the receipt route is unavailable, malformed,
                # redirected or non-404. GET404 alone never grants eligibility.
                status, found = _poll(request_id)
                if status == 200:
                    _observe(db, request_id, found)
                elif status == 404 and found == {}:
                    generation = _claim_recovery(db, request_id)
        post_status = 0
        if generation is not None:
            try:
                post_status, _ = _request("/api/feedback", body=payload)
            except (OSError, ValueError, urllib.error.URLError):
                pass  # A possible write remains unknown, including retries.
        status, found = _poll(request_id)
        if status == 200:
            _observe(db, request_id, found)
        elif generation is not None and post_status == 429 and status == 404 and found == {}:
            _record_rejection(db, request_id, generation)
    except (OSError, ValueError, urllib.error.URLError, RecursionError, TotalDeadline):
        pass
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        row = _attempt(db, request_id)
        result = _saved_receipt(row) or {"schema_version": 1, "request_id": request_id, "state": "unknown"}
        rate_limited = row["retryable"] and row["phase"] == "rate_limited" and row["receipt"] is None
        db.close()
    if rate_limited:
        print(json.dumps({"request_id": request_id, "admission": "rate_limited", "retryable": True}))
        print("Last delivery was rate limited (429+404). Explicit identical submit may recover this same ID after a fresh receipt check; resume is GET-only.", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] != "created":
        print(f"Keep this attempt. Resume status with: resume {request_id}. Do not create a replacement submission.", file=sys.stderr)
    return 0 if result["state"] == "created" else 2


if __name__ == "__main__":
    raise SystemExit(main())
