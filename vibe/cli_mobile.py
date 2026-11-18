"""Local-only health commands. Values go solely to the invoking CLI's stdout."""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
import re
import secrets
import sys

from core.handlers.mobile_health import (
    current_identity, notify_phone, publish_request_card, wait_for_request,
)
from core.mobile_health_contract import METRICS, MobileError, metrics
from storage.lock import MigrationFileLock, remove_unheld_lock
from storage.mobile_store import MobileStore


def add_mobile_parser(subparsers) -> None:
    root = subparsers.add_parser("mobile", help="Read health data synced by your iPhone")
    groups = root.add_subparsers(dest="mobile_group", required=True)
    health = groups.add_parser("health", help="Connect, read and revoke health synchronization")
    commands = health.add_subparsers(dest="mobile_command", required=True)
    commands.add_parser("status", help="Show device connection and sync freshness")
    read = commands.add_parser("read", help="Read daily health data from this instance")
    read.add_argument("--metric", default=",".join(METRICS), help="Comma-separated steps,activity,workouts,sleep")
    window = read.add_mutually_exclusive_group()
    window.add_argument("--period", choices=["today", "yesterday", "last7d", "last30d"])
    window.add_argument("--from", dest="from_day", metavar="YYYY-MM-DD")
    read.add_argument("--to", dest="to_day", metavar="YYYY-MM-DD")
    read.add_argument("--max-age", help="Mark data stale after this age, e.g. 6h")
    read.add_argument("--refresh", action="store_true", help="Ask the paired iPhone to update")
    read.add_argument("--wait", type=float, default=None, metavar="S", help="Seconds to wait for refresh (default 20)")
    connect = commands.add_parser("connect", help="Request authorization on iPhone")
    connect.add_argument("--wait", type=float, default=0, metavar="S")
    for parser in (read, connect):
        callbacks = parser.add_mutually_exclusive_group()
        callbacks.add_argument("--callback-session-id", metavar="ID")
        callbacks.add_argument("--no-callback", action="store_true")
    commands.add_parser("revoke", help="Revoke devices and delete instance health data")
    requests = groups.add_parser("requests", help="Inspect value-free device request status")
    request_commands = requests.add_subparsers(dest="mobile_command", required=True)
    show = request_commands.add_parser("show", help="Show a device request")
    show.add_argument("request_id")
    wait = request_commands.add_parser("await", help="Wait for a device request")
    wait.add_argument("request_id")
    wait.add_argument("--timeout", type=float, default=20, metavar="S")


def _window(args, *, today: date) -> tuple[str, str]:
    if args.from_day or args.to_day:
        if not args.from_day or not args.to_day or args.period:
            raise MobileError("invalid_period")
        try:
            start, end = date.fromisoformat(args.from_day), date.fromisoformat(args.to_day)
        except ValueError:
            raise MobileError("invalid_period") from None
        if not 0 <= (end - start).days < 30:
            raise MobileError("invalid_period")
        return start.isoformat(), end.isoformat()
    period = args.period or "today"
    if period == "yesterday":
        today -= timedelta(days=1)
    days = {"today": 1, "yesterday": 1, "last7d": 7, "last30d": 30}[period]
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _max_age(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", value)
    if not match:
        raise MobileError("invalid_period")
    return float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def _seconds(value: float) -> float:
    import math
    if not math.isfinite(value) or not 0 <= value <= 24 * 60 * 60:
        raise MobileError("invalid_period")
    return value


def _callback(args) -> str | None:
    if getattr(args, "no_callback", False):
        return None
    session_id = getattr(args, "callback_session_id", None) or os.environ.get("AVIBE_SESSION_ID")
    if session_id:
        # Same target validator used by Vault callbacks; no device data enters it.
        from vibe.cli import TaskCliError, _validate_callback_session_id
        try:
            _validate_callback_session_id(session_id, help_command="vibe mobile health --help")
        except TaskCliError as error:
            raise MobileError(error.code, 422) from None
    return session_id


def _request(store, operation, identity, args, *, names=METRICS, caller="local"):
    seconds = _seconds(args.wait)
    callback = _callback(args)
    waiter_id = secrets.token_hex(16) if seconds else None
    lock = MigrationFileLock(store.waiter_path(waiter_id), timeout_seconds=0) if waiter_id else None
    if lock:
        lock.acquire()
    request = None
    created = False
    try:
        request, created = store.create_request(
            operation, identity, names=names, caller=caller, callback_session=callback,
            waiter_id=waiter_id, wait_seconds=seconds,
        )
        if created:
            publish_request_card(request, os.environ.get("AVIBE_SESSION_ID"))
            notify_phone(store, request, identity, caller=caller)
        if not seconds:
            return request
        if not created:
            # Coalescing does not steal the first caller's continuation. A later
            # reader may observe completion synchronously but arms no callback.
            waiter_id = None
        return wait_for_request(store, request["id"], seconds, waiter_id=waiter_id)
    finally:
        if request and created and waiter_id:
            store.finish_wait(request["id"], waiter_id, consumed=False)
        if lock:
            lock.release()
            remove_unheld_lock(lock.lock_path)


def execute(args) -> dict:
    # A remotely initiated Agent still runs this local binary. All instance
    # Agents may read; caller environment is audit identity, not an allowlist.
    caller = os.environ.get("AVIBE_AGENT_NAME") or os.environ.get("AVIBE_CALLER_BACKEND") or "local"
    session_id = os.environ.get("AVIBE_SESSION_ID")
    store = MobileStore()
    command = args.mobile_command
    if args.mobile_group == "requests":
        if command == "show":
            return {"kind": "mobile_request", "request": store.show_request(args.request_id)}
        seconds = _seconds(args.timeout)
        waiter_id = secrets.token_hex(16)
        lock = MigrationFileLock(store.waiter_path(waiter_id), timeout_seconds=0)
        try:
            with lock:
                armed = store.arm_wait(args.request_id, waiter_id, seconds)
                if not armed:
                    raise MobileError("continuation_owned", 409)
                return {"kind": "mobile_request", "request": wait_for_request(
                    store, args.request_id, seconds, waiter_id=waiter_id,
                )}
        finally:
            store.finish_wait(args.request_id, waiter_id, consumed=False)
            remove_unheld_lock(lock.lock_path)
    if command == "revoke":
        return {"kind": "mobile_health", **store.revoke(caller=caller, session_id=session_id)}
    identity = current_identity(store)
    if command == "status":
        return {"kind": "mobile_health", **store.status()}
    if command == "connect":
        if store.status()["connected"]:
            return {"kind": "mobile_health", **store.status()}
        return {"kind": "mobile_request", "request": _request(
            store, "health.connect", identity, args, caller=caller,
        )}
    names = metrics(args.metric.split(","))
    start, end = _window(args, today=store.calendar_today())
    max_age = _max_age(args.max_age)
    if args.wait is not None and not args.refresh:
        raise MobileError("invalid_period")
    existing = store.read(names, start, end, caller=caller, session_id=session_id, max_age=max_age)
    if args.refresh:
        args.wait = 20 if args.wait is None else args.wait
        try:
            request = _request(store, "health.refresh", identity, args, names=names, caller=caller)
            if request["state"] != "succeeded":
                raise MobileError("device_wait_timeout", 409, details={"request_id": request["id"]})
        except MobileError as error:
            if error.code == "device_wait_timeout":
                error.details = {**(error.details or {}), "data": existing}
            raise
        existing = store.read(names, start, end, caller=caller, session_id=session_id, max_age=max_age)
    return {"kind": "mobile_health", **existing}


def cmd_mobile(args) -> int:
    from vibe.cli import _json_text
    try:
        result = execute(args)
        payload = {"schema_version": 1, "ok": True, **result}
        code = 0
    except MobileError as error:
        payload = {"schema_version": 1, "ok": False, "kind": "error", "code": error.code, "error": error.code}
        if error.details is not None:
            payload["details"] = error.details
        code = 1
    print(_json_text(payload, stream=sys.stdout))
    return code
