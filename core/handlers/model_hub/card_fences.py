"""Durable single-controller claims. All operations run on a worker thread."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .state_file import write_state_document


KINDS = frozenset({"all", "fable", "session", "weekly", "account"})


class FenceUnavailable(Exception):
    pass


class CardFences:
    def __init__(self, path: Path):
        self.path = path
        self._mutex = threading.RLock()
        self._locks: dict[str, dict] = {}
        self._damaged = False
        self._loaded = False
        self._opened = datetime.now(timezone.utc).isoformat()

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            state = json.loads(self.path.read_bytes())
            if type(state) is not dict or state.get("version") != 1 or type(state.get("locks")) is not dict:
                raise ValueError
            for key, row in state["locks"].items():
                if (
                    not key
                    or not isinstance(row, dict)
                    or set(row) != {"request_id", "kind", "since", "reason"}
                    or row["kind"] not in KINDS
                    or row["reason"] not in ("in_flight", "unconfirmed")
                    or not re.fullmatch(r"[a-f0-9]{32}", row["request_id"])
                    or datetime.fromisoformat(row["since"]).tzinfo is None
                ):
                    raise ValueError
                row["reason"] = "unconfirmed"
            self._locks = state["locks"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self._damaged = True

    def _save(self, locks):
        try:
            write_state_document(self.path, {"version": 1, "locks": locks})
        except OSError:
            self._damaged = True
            raise FenceUnavailable from None
        self._locks = locks

    def claim(self, key: str, kind: str, now: datetime) -> str:
        with self._mutex:
            self._load()
            if self._damaged or key in self._locks or kind not in KINDS:
                raise FenceUnavailable
            request_id = uuid4().hex
            row = dict(
                request_id=request_id, kind=kind, since=now.astimezone(timezone.utc).isoformat(), reason="in_flight"
            )
            self._save({**self._locks, key: row})
            return request_id

    def has_locks(self):
        with self._mutex:
            self._load()
            return self._damaged or bool(self._locks)

    def unresolved_identity(self, kind):
        return {"kind": kind, "since": self._opened, "reason": "unconfirmed"}

    def public(self, key, fallback_kind="account"):
        with self._mutex:
            self._load()
            if self._damaged:
                return dict(kind=fallback_kind, since=self._opened, reason="unconfirmed")
            row = self._locks.get(key)
            return {k: row[k] for k in ("kind", "since", "reason")} if row else None

    def finish(self, key, request_id, *, unknown: bool):
        with self._mutex:
            self._load()
            old = self._locks.get(key)
            if self._damaged or not old or old["request_id"] != request_id:
                raise FenceUnavailable
            locks = self._locks.copy()
            if unknown:
                locks[key] = {**old, "reason": "unconfirmed"}
            else:
                del locks[key]
            self._save(locks)

    def refresh_token(self, key):
        with self._mutex:
            self._load()
            row = self._locks.get(key)
            return row["request_id"] if not self._damaged and row and row["reason"] == "unconfirmed" else None

    def confirm_read(self, key, token):
        with self._mutex:
            if token and token == self.refresh_token(key):
                self.finish(key, token, unknown=False)
