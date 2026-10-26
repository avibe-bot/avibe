"""Avibe's side of avault's one-time macOS Keychain authorization.

macOS lets a binary read a Keychain item only after the user approves that
binary's code identity once. Developer ID signed avault (0.1.8 and later) never
shows that dialog from a background call: when the item does not trust it yet,
the call fails with the ``keychain_authorization_required`` contract (one-shot
exit status 77, or ``code`` on a resident-agent error frame), and only
``avault key authorize`` may show the dialog. An existing user whose master key
an ad-hoc release created meets this once after upgrading to the signed
release; later signed releases read the key without asking again.

This module owns the state Avibe keeps for that:

- the "authorization required" record every process shares, written when an
  avault call reports the contract and removed once the master key is read;
- the single authorization a Web action runs at a time in this process.

Running avault stays in ``vibe.api`` with every other avault call. Nothing here
starts an authorization on its own: only an explicit user action may.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import paths

logger = logging.getLogger(__name__)

AUTHORIZATION_REQUIRED_CODE = "avault_authorization_required"
_RECORD_FILE = "avault_authorization.json"
_RECORD_SCHEMA_VERSION = 1

_ATTEMPT_LOCK = threading.Lock()
_ATTEMPT: dict[str, Any] | None = None


def _record_path() -> Path:
    return paths.get_state_dir() / _RECORD_FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_record() -> dict[str, Any] | None:
    """The recorded requirement, or None when avault is not known to need one.

    A record this version cannot read is ignored with a warning: it must never
    keep Vault behind an authorization nobody can clear.
    """
    path = _record_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("could not read the avault authorization record at %s", path, exc_info=True)
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        logger.warning("ignoring an unreadable avault authorization record at %s", path)
        return None
    if not isinstance(record, dict) or record.get("required") is not True:
        return None
    return record


def authorization_required() -> bool:
    return read_record() is not None


def record_authorization_required() -> None:
    """Remember that avault cannot read the master key until the user authorizes it."""
    if authorization_required():
        return
    path = _record_path()
    payload = {
        "schema_version": _RECORD_SCHEMA_VERSION,
        "required": True,
        "detected_at": _now(),
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        logger.warning("could not record that avault needs Keychain authorization", exc_info=True)
        return
    logger.warning("avault needs a one-time macOS Keychain authorization")


def clear_authorization_required() -> None:
    """Forget the requirement: avault has just read the master key."""
    path = _record_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("could not clear the avault authorization record at %s", path, exc_info=True)
        return
    logger.info("avault Keychain authorization is in place")


def current_attempt() -> dict[str, Any] | None:
    with _ATTEMPT_LOCK:
        return dict(_ATTEMPT) if _ATTEMPT is not None else None


def start_authorization(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Start one authorization in the background, or join the one in flight.

    ``run`` performs it and returns ``{"ok": bool, "reason": str, ...}``. Every
    caller that arrives while it runs gets the same attempt back instead of a
    second avault process, so a repeated click shows at most one dialog.
    """
    global _ATTEMPT
    with _ATTEMPT_LOCK:
        if _ATTEMPT is not None and _ATTEMPT.get("state") == "running":
            return dict(_ATTEMPT)
        attempt = {"id": uuid.uuid4().hex, "state": "running", "started_at": _now()}
        _ATTEMPT = attempt
        snapshot = dict(attempt)

    def _target() -> None:
        global _ATTEMPT
        try:
            outcome = run()
        except Exception as exc:  # noqa: BLE001 - the attempt must always finish
            logger.warning("avault authorization failed unexpectedly", exc_info=True)
            outcome = {"ok": False, "reason": "failed", "detail": str(exc)}
        finished = {
            **attempt,
            "state": "succeeded" if outcome.get("ok") else "failed",
            "reason": outcome.get("reason"),
            "finished_at": _now(),
        }
        if outcome.get("detail"):
            finished["detail"] = outcome["detail"]
        with _ATTEMPT_LOCK:
            if _ATTEMPT is attempt:
                _ATTEMPT = finished

    threading.Thread(target=_target, name="avault-authorize", daemon=True).start()
    return snapshot
