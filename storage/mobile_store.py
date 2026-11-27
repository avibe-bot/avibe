"""Private mobile state, deliberately independent of the general state database."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from typing import Iterator, Literal

from config import paths
from core.mobile_health_contract import (
    METRICS, TERMINAL, MobileError, metrics, object_fields, text, timezone_name,
    validate_result, validate_upload,
)
from storage.lock import MigrationFileLock, probe_lock_holder

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE devices (
 id TEXT PRIMARY KEY, credential_hash TEXT UNIQUE NOT NULL, installation_hash TEXT,
 instance_id TEXT NOT NULL, owner TEXT NOT NULL, name TEXT NOT NULL,
 app_version TEXT NOT NULL, metrics TEXT NOT NULL, manifest TEXT,
 created_at TEXT NOT NULL, last_seen TEXT NOT NULL, revoked_at TEXT,
 device_seq INTEGER NOT NULL DEFAULT 0, polling_until TEXT
);
CREATE TABLE pairings (
 code_hash TEXT PRIMARY KEY, request_id TEXT NOT NULL, instance_id TEXT NOT NULL,
 owner TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE health_days (
 device_id TEXT NOT NULL REFERENCES devices(id), metric TEXT NOT NULL,
 day TEXT NOT NULL, item TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(device_id, metric, day)
);
CREATE TABLE metric_sync (
 device_id TEXT NOT NULL REFERENCES devices(id), metric TEXT NOT NULL,
 synced_at TEXT, last_failure TEXT, PRIMARY KEY(device_id, metric)
);
CREATE TABLE requests (
 id TEXT PRIMARY KEY, operation TEXT NOT NULL, device_id TEXT REFERENCES devices(id),
 instance_id TEXT NOT NULL, owner TEXT NOT NULL, metrics TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ended_at TEXT,
 claim_token TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 result TEXT, error TEXT, late_result INTEGER NOT NULL DEFAULT 0,
 callback_session TEXT, callback_state TEXT NOT NULL DEFAULT 'pending', callback_run TEXT,
 waiter_id TEXT, waiter_until TEXT, caller TEXT NOT NULL
);
CREATE INDEX requests_device_queue ON requests(device_id, state, created_at);
CREATE INDEX requests_callback ON requests(callback_state, state);
CREATE INDEX requests_expiry ON requests(state, expires_at);
CREATE TABLE accesses (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, caller TEXT NOT NULL, session_id TEXT,
 operation TEXT NOT NULL, metrics TEXT NOT NULL, from_day TEXT, to_day TEXT
);
CREATE INDEX accesses_retention ON accesses(at);
CREATE TABLE push_attempts (
 device_key TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX push_budget ON push_attempts(device_key, at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(now: str, **delta) -> str:
    return (datetime.fromisoformat(now) + timedelta(**delta)).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def mobile_store_path() -> Path:
    return paths.get_state_dir() / "mobile" / "mobile.sqlite3"


def mobile_job_mode() -> Literal["paired", "callbacks"] | None:
    """Never initialize, migrate or acquire a writer lock for a scheduling hint."""
    path = mobile_store_path()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.parent.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        if conn.execute("SELECT 1 FROM devices WHERE revoked_at IS NULL LIMIT 1").fetchone():
            return "paired"
        # Include queued connects: even if a phone never pairs, expiry owes a continuation.
        if conn.execute("SELECT 1 FROM requests WHERE callback_state IN ('pending','claimed') "
                        "AND callback_session IS NOT NULL LIMIT 1").fetchone():
            return "callbacks"
    return None


def publish_mobile_schedule_changed() -> None:
    from core.inbox_events import publish_definitions_updated

    publish_definitions_updated(definition_type="mobile-health")


class MobileStore:
    def __init__(self, path: Path | None = None, *, existing_only: bool = False):
        self.path = path or mobile_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise MobileError("private_storage_unavailable", 503)
        self.path.parent.chmod(0o700)
        with MigrationFileLock(self.path.with_suffix(".migration.lock")):
            try:
                if self.path.is_symlink():
                    raise MobileError("private_storage_unavailable", 503)
                fd = os.open(self.path, (0 if existing_only else os.O_CREAT) | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                        raise MobileError("private_storage_unavailable", 503)
                    if hasattr(os, "fchmod"):
                        os.fchmod(fd, 0o600)
                    else:
                        # Windows Python before 3.13 lacks descriptor chmod.
                        self.path.chmod(0o600)
                finally:
                    os.close(fd)
            except OSError:
                raise MobileError("private_storage_unavailable", 503) from None
            with self.transaction() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise MobileError("contract_unsupported", 426)
                if version == 0:
                    # executescript commits implicitly; execute each schema statement
                    # under the migration lock and the same explicit transaction.
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                # Compatible with released v1 writers, which name every audit column.
                if not any(col[1] == "device_id" for col in conn.execute("PRAGMA table_info(accesses)")):
                    conn.execute("ALTER TABLE accesses ADD COLUMN device_id TEXT")
                # Nullable additive column: released writers explicitly name request columns.
                if not any(col[1] == "consent_device_id" for col in conn.execute("PRAGMA table_info(requests)")):
                    conn.execute("ALTER TABLE requests ADD COLUMN consent_device_id TEXT")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path.absolute().as_uri() + '?mode=rw', uri=True, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def waiter_path(self, waiter_id: str) -> Path:
        if len(waiter_id) != 32 or any(char not in "0123456789abcdef" for char in waiter_id):
            raise MobileError("invalid_payload")
        return self.path.parent / "waiters" / f"{waiter_id}.lock"

    @staticmethod
    def _request(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "operation": row["operation"], "state": row["state"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "requires_user": row["operation"] == "health.connect",
            "metrics": json.loads(row["metrics"]),
            "minimum_health_version": 2 if row["consent_device_id"] else 1,
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": {"code": row["error"]} if row["error"] else None,
        }

    @staticmethod
    def _audit(conn, operation, *, caller, session_id=None, names=(), start=None, end=None, device_id=None):
        conn.execute(
            "INSERT INTO accesses(at,caller,session_id,operation,metrics,from_day,to_day,device_id) VALUES(?,?,?,?,?,?,?,?)",
            (_now(), caller, session_id, operation, _json(list(names)), start, end, device_id),
        )

    @staticmethod
    def _expire(conn, now: str):
        conn.execute(
            "UPDATE requests SET state='expired', ended_at=? "
            "WHERE state IN ('queued','claimed') AND expires_at<=?", (now, now),
        )
        conn.execute(
            "UPDATE requests SET state='failed', error='internal', ended_at=? "
            "WHERE state='claimed' AND lease_until<=? AND attempts>=3", (now, now),
        )

    def active_device(self, conn=None) -> dict | None:
        if conn is None:
            with self.transaction() as connection:
                return self.active_device(connection)
        row = conn.execute(
            "SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None

    def create_request(
        self, operation: str, identity: dict, *, names=METRICS, caller="local",
        callback_session=None, waiter_id=None, wait_seconds=0, expand=False,
    ) -> tuple[dict, bool]:
        metrics(list(names))
        if operation not in {"health.connect", "health.refresh"}:
            raise MobileError("invalid_payload")
        now = _now()
        with self.transaction() as conn:
            self._expire(conn, now)
            device = self.active_device(conn)
            if expand and (operation != "health.connect" or device is None):
                raise MobileError("not_connected", 409)
            if operation == "health.refresh" and not device:
                raise MobileError("not_connected", 409)
            device_id = device["id"] if operation == "health.refresh" else None
            selected = list(names) if expand else json.loads(device["metrics"]) if device else list(names)
            consent_device_id = device["id"] if expand else None
            candidates = conn.execute(
                "SELECT * FROM requests WHERE operation=? AND device_id IS ? AND instance_id=? "
                "AND owner=? AND state IN ('queued','claimed') ORDER BY created_at",
                (operation, device_id, identity["id"], identity["owner"]),
            ).fetchall()
            existing = next((row for row in candidates if row["consent_device_id"] == consent_device_id
                             and set(selected) <= set(json.loads(row["metrics"]))), None)
            if existing:
                # Refresh always queries all consented groups. A coalesced caller
                # cannot change the original request's continuation owner.
                return self._request(existing), False
            request_id = "mreq_" + secrets.token_hex(16)
            conn.execute(
                "INSERT INTO requests(id,operation,device_id,instance_id,owner,metrics,state,created_at,"
                "expires_at,callback_session,callback_run,waiter_id,waiter_until,caller) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                (request_id, operation, device_id, identity["id"], identity["owner"], _json(selected),
                 now, _after(now, hours=24) if operation == "health.connect" else _after(now, minutes=15),
                 callback_session, secrets.token_hex(16), waiter_id,
                 _after(now, seconds=wait_seconds) if waiter_id else None, caller),
            )
            if expand:
                conn.execute("UPDATE requests SET consent_device_id=? WHERE id=?", (device["id"], request_id))
            self._audit(conn, operation, caller=caller, session_id=callback_session, names=selected)
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if callback_session and not device:
            publish_mobile_schedule_changed()
        return self._request(row), True

    def issue_pairing(self, request_id: str, identity: dict) -> dict:
        now = _now()
        with self.transaction() as conn:
            self._expire(conn, now)
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise MobileError("request_not_found", 404)
            if row["operation"] != "health.connect" or row["state"] in TERMINAL or row["device_id"]:
                raise MobileError("request_terminal", 410)
            if (row["instance_id"], row["owner"]) != (identity["id"], identity["owner"]):
                raise MobileError("instance_not_eligible", 403)
            code = secrets.token_urlsafe(16)
            expiry = _after(now, minutes=5)
            conn.execute(
                "INSERT INTO pairings VALUES(?,?,?,?,?)",
                (_hash(code), request_id, identity["id"], identity["owner"], expiry),
            )
            return {"pairing_code": code, "expires_at": expiry,
                    "supported_metrics": list(METRICS), "metrics": json.loads(row["metrics"]),
                    "expansion": bool(row["consent_device_id"])}

    def precheck_pairing(self, payload: dict) -> None:
        """Reject unknown/expired codes locally; pair still consumes atomically."""
        code = text(payload.get("pairing_code"), limit=100)
        now = _now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT p.code_hash FROM pairings p JOIN requests r ON r.id=p.request_id "
                "WHERE p.code_hash=? AND p.expires_at>? AND r.expires_at>? "
                "AND r.state IN ('queued','claimed') AND r.device_id IS NULL",
                (_hash(code), now, now),
            ).fetchone()
            if row is None:
                raise MobileError("device_unauthorized", 401)

    def pair(self, payload: dict, identity: dict, *, reserved_device_id: str | None = None) -> dict:
        object_fields(payload, {"pairing_code", "device_name", "platform", "app_version", "metrics"},
                      {"installation_id"})
        code = text(payload["pairing_code"], limit=100)
        name = text(payload["device_name"], limit=100)
        text(payload["app_version"], limit=80)
        if payload["platform"] != "ios":
            raise MobileError("invalid_payload")
        names = metrics(payload["metrics"])
        installation = _hash(text(payload["installation_id"], limit=100)) if "installation_id" in payload else None
        now = _now()
        with self.transaction() as conn:
            self._expire(conn, now)
            pairing = conn.execute("SELECT * FROM pairings WHERE code_hash=?", (_hash(code),)).fetchone()
            if not pairing or pairing["expires_at"] <= now:
                raise MobileError("device_unauthorized", 401)
            if (pairing["instance_id"], pairing["owner"]) != (identity["id"], identity["owner"]):
                raise MobileError("instance_not_eligible", 403)
            request = conn.execute("SELECT * FROM requests WHERE id=?", (pairing["request_id"],)).fetchone()
            if not request or request["state"] in TERMINAL or request["device_id"]:
                raise MobileError("request_terminal", 410)
            if request["consent_device_id"]:
                raise MobileError("contract_unsupported", 426)
            if installation:
                for old in conn.execute(
                    "SELECT id FROM devices WHERE installation_hash=? AND revoked_at IS NULL",
                    (installation,),
                ).fetchall():
                    self._revoke_device(conn, old["id"], now)
            device_id = reserved_device_id or 'mdev_' + secrets.token_hex(16)
            credential = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO devices(id,credential_hash,installation_hash,instance_id,owner,name,"
                "app_version,metrics,created_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (device_id, _hash(credential), installation, identity["id"], identity["owner"],
                 name, payload["app_version"], _json(names), now, now),
            )
            conn.execute("UPDATE requests SET device_id=?,metrics=? WHERE id=?",
                         (device_id, _json(names), request["id"]))
            conn.execute("DELETE FROM pairings WHERE request_id=?", (request["id"],))
        publish_mobile_schedule_changed()
        return {"device_id": device_id, "credential": credential,
                "instance": {"id": identity["id"], "name": identity["name"]}}

    @staticmethod
    def _revoke_device(conn, device_id: str, now: str):
        conn.execute("UPDATE devices SET revoked_at=?,polling_until=NULL WHERE id=?", (now, device_id))
        conn.execute("DELETE FROM health_days WHERE device_id=?", (device_id,))
        conn.execute("DELETE FROM metric_sync WHERE device_id=?", (device_id,))
        conn.execute(
            "UPDATE requests SET state='canceled',ended_at=? WHERE (device_id=? OR consent_device_id=?) "
            "AND state IN ('queued','claimed')", (now, device_id, device_id),
        )

    def authenticate(self, credential: str, identity: dict) -> dict:
        now = _now()
        # Commit expiry / purge before returning the refusal.
        refusal = None
        device = None
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM devices WHERE credential_hash=?", (_hash(credential),)).fetchone()
            if row is None:
                refusal = MobileError("device_unauthorized", 401)
            elif row["revoked_at"]:
                refusal = MobileError("device_revoked", 403)
            elif row["last_seen"] <= _after(now, days=-60):
                self._revoke_device(conn, row["id"], now)
                refusal = MobileError("device_revoked", 403)
            elif (row["instance_id"], row["owner"]) != (identity["id"], identity["owner"]):
                self._revoke_device(conn, row["id"], now)
                refusal = MobileError("instance_not_eligible", 403)
            else:
                conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, row["id"]))
                device = dict(row)
        if refusal:
            if row is not None and not row["revoked_at"]:
                publish_mobile_schedule_changed()
            raise refusal
        return device

    def lookup_credential(self, credential: str) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM devices WHERE credential_hash=?", (_hash(credential),)).fetchone()
            if row is None:
                raise MobileError("device_unauthorized", 401)
            if row["revoked_at"]:
                raise MobileError("device_revoked", 403)
            return dict(row)

    def manifest(self, device: dict, payload: dict) -> dict:
        object_fields(payload, {"contract", "app_version", "os_version", "timezone", "metrics",
                                "background_delivery", "push_wake"}, {"pairing_code", "request_id"})
        if type(payload["contract"]) is not int or payload["contract"] != 1:
            raise MobileError("contract_unsupported", 426)
        text(payload["app_version"], limit=80)
        text(payload["os_version"], limit=80)
        timezone_name(payload["timezone"])
        names = metrics(payload["metrics"])
        if type(payload["background_delivery"]) is not bool or type(payload["push_wake"]) is not bool:
            raise MobileError("invalid_payload")
        now = _now()
        with self.transaction() as conn:
            self._ensure_active(conn, device["id"])
            self._expire(conn, now)
            current = conn.execute("SELECT * FROM devices WHERE id=?", (device["id"],)).fetchone()
            confirmed = json.loads(current["metrics"])
            if "pairing_code" in payload or "request_id" in payload:
                code = text(payload.get("pairing_code"), limit=100)
                request_id = text(payload.get("request_id"), limit=100)
                row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
                if (not row or row["consent_device_id"] != device["id"]
                        or (row["instance_id"], row["owner"]) != (device["instance_id"], device["owner"])):
                    raise MobileError("request_not_found", 404)
                if row["device_id"] is None:
                    pairing = conn.execute("SELECT * FROM pairings WHERE code_hash=? AND request_id=?",
                                           (_hash(code), request_id)).fetchone()
                    if row["state"] in TERMINAL:
                        raise MobileError("request_terminal", 410)
                    if not pairing or pairing["expires_at"] <= now:
                        raise MobileError("request_terminal", 410)
                    additions = json.loads(row["metrics"])
                    if set(names) != set(confirmed) | set(additions):
                        raise MobileError("unsupported_metric")
                    confirmed = [name for name in METRICS if name in names]
                    conn.execute("UPDATE devices SET metrics=? WHERE id=?", (_json(confirmed), device["id"]))
                    conn.execute("UPDATE requests SET device_id=? WHERE id=?", (device["id"], request_id))
                    conn.execute("DELETE FROM pairings WHERE request_id=?", (request_id,))
                # Bound requests already consumed consent; retries do not expand it further.
            if not set(names) <= set(confirmed):
                raise MobileError("unsupported_metric")
            stored_manifest = {key: value for key, value in payload.items()
                               if key not in {"pairing_code", "request_id"}}
            conn.execute("UPDATE devices SET manifest=?,app_version=? WHERE id=?",
                         (_json(stored_manifest), payload["app_version"], device["id"]))
        return {"accepted_at": now, "supported_metrics": list(METRICS), "confirmed_metrics": confirmed}

    @staticmethod
    def _ensure_active(conn, device_id):
        row = conn.execute("SELECT revoked_at FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or row["revoked_at"]:
            raise MobileError("device_revoked", 403)

    def upload(self, device: dict, payload: dict) -> dict:
        validate_upload(payload, allowed_metrics=json.loads(device["metrics"]))
        now = _now()
        with self.transaction() as conn:
            self._ensure_active(conn, device["id"])
            current = conn.execute("SELECT device_seq FROM devices WHERE id=?", (device["id"],)).fetchone()[0]
            if payload["device_seq"] <= current:
                raise MobileError("stale_seq", 409)
            for item in payload["items"]:
                conn.execute(
                    "INSERT INTO health_days VALUES(?,?,?,?,?) ON CONFLICT(device_id,metric,day) "
                    "DO UPDATE SET item=excluded.item,received_at=excluded.received_at",
                    (device["id"], item["metric"], item["day"], _json(item), now),
                )
            for name in payload["window"]["metrics"]:
                failure = next((value for value in payload["failures"] if value["metric"] == name), None)
                if failure:
                    conn.execute(
                        "INSERT INTO metric_sync(device_id,metric,last_failure) VALUES(?,?,?) "
                        "ON CONFLICT(device_id,metric) DO UPDATE SET last_failure=excluded.last_failure",
                        (device["id"], name, _json({"at": failure["at"], "code": failure["code"]})),
                    )
                else:
                    conn.execute(
                        "INSERT INTO metric_sync VALUES(?,?,?,NULL) ON CONFLICT(device_id,metric) "
                        "DO UPDATE SET synced_at=excluded.synced_at,last_failure=NULL",
                        (device["id"], name, now),
                    )
            conn.execute("UPDATE devices SET device_seq=? WHERE id=?", (payload["device_seq"], device["id"]))
            self._trim_days(conn, now)
        return {"accepted_seq": payload["device_seq"], "received_at": now}

    def set_polling(self, device_id: str, seconds: int) -> None:
        with self.transaction() as conn:
            self._ensure_active(conn, device_id)
            conn.execute("UPDATE devices SET polling_until=? WHERE id=?",
                         (_after(_now(), seconds=seconds) if seconds else None, device_id))

    def clear_polling(self, device_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE devices SET polling_until=NULL WHERE id=?", (device_id,))

    def claim(self, device_id: str, *, maximum: int, request_id: str | None = None) -> dict:
        now = _now()
        with self.transaction() as conn:
            self._ensure_active(conn, device_id)
            self._expire(conn, now)
            rows = conn.execute(
                "SELECT * FROM requests WHERE device_id=? AND (? IS NULL OR id=?) "
                "AND (state='queued' OR (state='claimed' AND lease_until<=? AND attempts<3)) "
                "ORDER BY created_at LIMIT ?",
                (device_id, request_id, request_id, now, maximum),
            ).fetchall()
            result = []
            for row in rows:
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "UPDATE requests SET state='claimed',claim_token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                    (token, _after(now, seconds=60), row["id"]),
                )
                claimed = self._request(row)
                claimed.pop("result")
                claimed.pop("error")
                claimed["state"], claimed["claim_token"] = "claimed", token
                result.append(claimed)
            return {"requests": result}

    def result(self, device: dict, request_id: str, payload: dict) -> dict:
        outcome = self._record_result(device, request_id, payload)
        if outcome.pop("recorded", False):
            publish_mobile_schedule_changed()  # A terminal may owe a continuation: deliver now.
        return outcome

    def _record_result(self, device: dict, request_id: str, payload: dict) -> dict:
        now = _now()
        with self.transaction() as conn:
            self._ensure_active(conn, device["id"])
            self._expire(conn, now)
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row["device_id"] != device["id"]:
                raise MobileError("request_not_found", 404)
            validate_result(payload, json.loads(row["metrics"]))
            if not row["claim_token"] or not secrets.compare_digest(row["claim_token"], payload["claim_token"]):
                raise MobileError("claim_mismatch", 409)
            if row["state"] == "canceled":
                conn.execute("UPDATE requests SET late_result=late_result+1 WHERE id=?", (request_id,))
                return {"state": "canceled"}
            if row["state"] in {"succeeded", "failed"}:
                return {"state": row["state"]}
            if row["state"] in TERMINAL:
                raise MobileError("request_terminal", 410)
            if row["lease_until"] <= now:
                raise MobileError("claim_mismatch", 409)
            if payload["outcome"] == "succeeded":
                # A receipt cannot assert completion without the preceding upload.
                synced = {
                    value["metric"] for value in conn.execute(
                        "SELECT metric FROM metric_sync WHERE device_id=? AND synced_at>=?",
                        (device["id"], row["created_at"]),
                    )
                }
                if not set(json.loads(row["metrics"])) <= synced:
                    raise MobileError("incomplete_window")
            conn.execute(
                "UPDATE requests SET state=?,result=?,error=?,ended_at=? WHERE id=?",
                (payload["outcome"], _json(payload["result"]) if "result" in payload else None,
                 payload.get("error", {}).get("code"), now, request_id),
            )
            return {"state": payload["outcome"], "recorded": True}

    def show_request(self, request_id: str) -> dict:
        with self.transaction() as conn:
            self._expire(conn, _now())
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise MobileError("request_not_found", 404)
            return self._request(row)

    def request_for_identity(self, request_id: str, identity: dict) -> dict:
        with self.transaction() as conn:
            self._expire(conn, _now())
            row = conn.execute(
                "SELECT * FROM requests WHERE id=? AND instance_id=? AND owner=?",
                (request_id, identity["id"], identity["owner"]),
            ).fetchone()
            if row is None:
                raise MobileError("request_not_found", 404)
            return self._request(row)

    def cancel_request(self, request_id: str, *, consume_callback: bool = False) -> None:
        with self.transaction() as conn:
            canceled = conn.execute(
                "UPDATE requests SET state='canceled',ended_at=? WHERE id=? AND state IN ('queued','claimed')",
                (_now(), request_id),
            ).rowcount
            if consume_callback:
                conn.execute(
                    "UPDATE requests SET callback_state='consumed',waiter_id=NULL,waiter_until=NULL "
                    "WHERE id=? AND state='canceled' AND callback_state='pending'",
                    (request_id,),
                )
            conn.execute("DELETE FROM pairings WHERE request_id=?", (request_id,))
        if canceled and not consume_callback:
            publish_mobile_schedule_changed()

    def finish_wait(self, request_id: str, waiter_id: str, *, consumed: bool) -> bool:
        with self.transaction() as conn:
            updated = conn.execute(
                "UPDATE requests SET callback_state=?,waiter_id=NULL,waiter_until=NULL "
                "WHERE id=? AND waiter_id=? AND callback_state='pending'",
                ("consumed" if consumed else "pending", request_id, waiter_id),
            ).rowcount == 1
        if updated and not consumed:
            publish_mobile_schedule_changed()  # The released continuation may already be owed.
        return updated

    def arm_wait(self, request_id: str, waiter_id: str, seconds: float) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise MobileError("request_not_found", 404)
            if row["callback_state"] != "pending":
                return False
            now = _now()
            if row["waiter_id"]:
                # A bounded reservation and an OS lock jointly establish
                # ownership. An expired deadline alone cannot evict a live
                # waiter; an unexpired reservation cannot be reclaimed either.
                if not row["waiter_until"] or row["waiter_until"] > now:
                    return False
                if probe_lock_holder(self.waiter_path(row["waiter_id"])) != "free":
                    return False
            # BEGIN IMMEDIATE serializes reclaimers with callbacks and
            # finish_wait; the old ID can no longer consume this continuation.
            conn.execute("UPDATE requests SET waiter_id=?,waiter_until=? WHERE id=?",
                         (waiter_id, _after(now, seconds=seconds), request_id))
            return True

    def pending_callbacks(self) -> list[dict]:
        now = _now()
        result = []
        with self.transaction() as conn:
            self._expire(conn, now)
            rows = conn.execute(
                "SELECT * FROM requests WHERE callback_state IN ('pending','claimed') "
                "AND state IN ('succeeded','failed','expired','canceled') AND callback_session IS NOT NULL LIMIT 100",
            ).fetchall()
            for row in rows:
                if row["waiter_id"] and row["waiter_until"] > now:
                    # A missing file cannot prove death before the declared bound.
                    if probe_lock_holder(self.waiter_path(row["waiter_id"])) != "free":
                        continue
                if row["callback_state"] == "pending":
                    conn.execute("UPDATE requests SET callback_state='claimed',waiter_id=NULL WHERE id=?",
                                 (row["id"],))
                result.append({**self._request(row), "callback_session": row["callback_session"],
                               "callback_run": row["callback_run"]})
        return result

    def callback_sent(self, request_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE requests SET callback_state='sent' WHERE id=? AND callback_state='claimed'",
                         (request_id,))

    def take_push_budget(self, device_key: str) -> bool:
        now = _now()
        with self.transaction() as conn:
            conn.execute("DELETE FROM push_attempts WHERE at<=?", (_after(now, hours=-1),))
            count = conn.execute("SELECT count(*) FROM push_attempts WHERE device_key=?", (device_key,)).fetchone()[0]
            if count >= 3:
                return False
            conn.execute("INSERT INTO push_attempts VALUES(?,?)", (device_key, now))
            return True

    def retire_other_identities(self, *, instance: str, owner: str, expected_device: str | None) -> None:
        """Apply a live owner proof, never a delayed answer over a new enrollment."""
        now = _now()
        with self.transaction() as conn:
            current = self.active_device(conn)
            if (current['id'] if current else None) != expected_device:
                raise MobileError('instance_eligibility_unavailable', 503)
            other = '(instance_id != ? OR owner != ?)'
            args = (instance, owner)
            devices = conn.execute(f'SELECT id FROM devices WHERE revoked_at IS NULL AND {other}', args).fetchall()
            for row in devices:
                self._revoke_device(conn, row['id'], now)
            codes = conn.execute(f'DELETE FROM pairings WHERE {other}', args).rowcount
            requests = conn.execute(f"UPDATE requests SET state='canceled',ended_at=? WHERE state IN ('queued','claimed')"
                                    f' AND {other}', (now, *args)).rowcount
            if not (devices or codes or requests):
                return
            self._audit(conn, 'health.revoke', caller='instance-policy')
        publish_mobile_schedule_changed()

    def revoke_confirmed_pairing(self, device_id: str, *, instance: str, owner: str) -> None:
        """Recheck the exact private enrollment in the same transaction that erases it."""
        with self.transaction() as conn:
            if not conn.execute('SELECT 1 FROM devices WHERE id=? AND instance_id=? AND owner=? AND revoked_at IS NULL',
                                (device_id, instance, owner)).fetchone():
                return
            self._revoke_device(conn, device_id, _now())
            self._audit(conn, 'health.revoke', caller='device_sign_out')
        publish_mobile_schedule_changed()

    def revoke(self, *, device_id: str | None = None, caller="local", session_id=None) -> dict:
        now = _now()
        with self.transaction() as conn:
            ids = ([device_id] if device_id else [
                row["id"] for row in conn.execute("SELECT id FROM devices WHERE revoked_at IS NULL")
            ])
            for identifier in ids:
                self._revoke_device(conn, identifier, now)
            if device_id is None:
                conn.execute("UPDATE requests SET state='canceled',ended_at=? WHERE state IN ('queued','claimed')",
                             (now,))
                conn.execute("DELETE FROM pairings")
            # A targeted disconnect must not cancel unused codes on other phones.
            self._audit(conn, "health.revoke", caller=caller, session_id=session_id)
        publish_mobile_schedule_changed()
        return {"purged": True}

    @staticmethod
    def _trim_days(conn, now: str):
        # Timestamp offsets in daily items preserve the phone's local calendar;
        # prune against that calendar instead of truncating a UTC date.
        rows = conn.execute("SELECT device_id,metric,day,item FROM health_days").fetchall()
        from zoneinfo import ZoneInfo
        for row in rows:
            zone = ZoneInfo(json.loads(row["item"])["timezone"])
            cutoff = (datetime.fromisoformat(now).astimezone(zone).date() - timedelta(days=29)).isoformat()
            if row["day"] < cutoff:
                conn.execute("DELETE FROM health_days WHERE device_id=? AND metric=? AND day=?",
                             (row["device_id"], row["metric"], row["day"]))

    def cleanup(self) -> None:
        now = _now()
        with self.transaction() as conn:
            self._expire(conn, now)
            expired = conn.execute("SELECT id FROM devices WHERE revoked_at IS NULL AND last_seen<=?",
                                   (_after(now, days=-60),)).fetchall()
            for row in expired:
                self._revoke_device(conn, row["id"], now)
            self._trim_days(conn, now)
            conn.execute("DELETE FROM pairings WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM pairings WHERE request_id IN "
                         "(SELECT id FROM requests WHERE ended_at<=?)", (_after(now, days=-7),))
            conn.execute("DELETE FROM requests WHERE ended_at<=?", (_after(now, days=-7),))
            conn.execute("DELETE FROM accesses WHERE at<=?", (_after(now, days=-90),))
            conn.execute("DELETE FROM push_attempts WHERE at<=?", (_after(now, hours=-1),))
        if expired:
            publish_mobile_schedule_changed()

    def status(self) -> dict:
        self.cleanup()
        with self.transaction() as conn:
            device = self.active_device(conn)
            if device is None:
                return {"connected": False, "metrics": {}}
            manifest = json.loads(device["manifest"]) if device["manifest"] else {}
            return {
                "connected": True,
                "confirmed_metrics": json.loads(device["metrics"]),
                "device": {"id": device["id"], "name": device["name"], "app_version": device["app_version"],
                           "last_contact_at": device["last_seen"]},
                "metrics": {
                    row["metric"]: {"synced_at": row["synced_at"],
                                    "last_failure": json.loads(row["last_failure"]) if row["last_failure"] else None}
                    for row in conn.execute("SELECT * FROM metric_sync WHERE device_id=?", (device["id"],))
                },
                "background_delivery": manifest.get("background_delivery", False),
                "live": bool(device["polling_until"] and device["polling_until"] > _now()),
            }

    def calendar_today(self) -> date:
        """Relative CLI windows use the phone's local daily calendar."""
        from zoneinfo import ZoneInfo
        with self.transaction() as conn:
            device = self.active_device(conn)
            manifest = json.loads(device["manifest"]) if device and device["manifest"] else {}
            zone = manifest.get("timezone")
            if zone is None and device:
                row = conn.execute(
                    "SELECT item FROM health_days WHERE device_id=? ORDER BY received_at DESC LIMIT 1",
                    (device["id"],),
                ).fetchone()
                zone = json.loads(row["item"])["timezone"] if row else None
        now = datetime.fromisoformat(_now())
        return now.astimezone(ZoneInfo(zone) if zone else None).date()

    def read(self, names: list[str], start: str, end: str, *, caller: str, session_id=None,
             max_age: float | None = None) -> dict:
        from core.mobile_health_contract import day
        metrics(names)
        first, last = day(start), day(end)
        if not 0 <= (last - first).days < 30:
            raise MobileError("invalid_period")
        self.cleanup()
        now = _now()
        with self.transaction() as conn:
            device = self.active_device(conn)
            if not device:
                raise MobileError("not_connected", 409)
            manifest = json.loads(device["manifest"]) if device["manifest"] else {}
            result = {}
            for name in names:
                indexed = {
                    row["day"]: {**json.loads(row["item"]), "received_at": row["received_at"]}
                    for row in conn.execute(
                        "SELECT * FROM health_days WHERE device_id=? AND metric=? AND day>=? AND day<=?",
                        (device["id"], name, start, end),
                    )
                }
                items = [
                    indexed.get(day_value, {
                        "day": day_value, "timezone": manifest.get("timezone"), "availability": "not_synced",
                        "closed": False, "value": None, "latest_sample_end_at": None,
                        "queried_at": None, "received_at": None,
                    })
                    for offset in range((last - first).days + 1)
                    for day_value in [(first + timedelta(days=offset)).isoformat()]
                ]
                queried = [item["queried_at"] for item in items if item["queried_at"]]
                freshness = {"queried_at": min(
                    queried, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
                ) if queried else None}
                if max_age is not None:
                    freshness["stale"] = len(queried) < len(items) or any(
                        datetime.fromisoformat(now) - datetime.fromisoformat(value.replace("Z", "+00:00"))
                        > timedelta(seconds=max_age) for value in queried
                    )
                result[name] = {"items": items, "freshness": freshness,
                                "confirmation_required": name not in json.loads(device["metrics"])}
            self._audit(conn, "health.read", caller=caller, session_id=session_id, names=names,
                        start=start, end=end, device_id=device["id"])
            return {"metrics": result}
