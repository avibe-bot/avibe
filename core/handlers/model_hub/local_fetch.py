"""Dormant local-fetch engine and mandatory gate, not wired to the gateway yet.

One worker owns a fetch's disposable cookies, DNS, pinned connections, decoders
and parsers. Cancellation kills and reaps it, rather than abandoning a thread.
The controller owns only admission, a host lock, bounded cooldowns and IPC.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
import weakref
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psutil

from .fetch_policy import FetchFailure, parse_url
from .retry import retry_after_deadline


_WORKER = "import sys; sys.path.insert(0, sys.argv.pop(1)); from core.handlers.model_hub.fetch_worker import main; main()"
_MEMORY_LIMIT = 768 * 1024 * 1024
_IPC_LIMIT = 1200000  # Bounded normalized prefix, never the raw response body.


class LocalFetchEngine:
    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._cooldowns = OrderedDict()

    async def check(self, url: str, *, deadline: float) -> str:
        """Mandatory gate, also when local fetching is disabled or reordered."""
        address = parse_url(url)
        result = await self._run("gate", address.url, deadline=min(deadline, time.monotonic() + 5))
        if result.get("error_code"):
            code = result["error_code"]
            raise FetchFailure(code if code == "fetch_url_blocked" else "fetch_dns_unverified", terminal=True)
        return result["url"]

    async def fetch(self, url: str, *, deadline: float, locale: str = "en", on_sent: Callable | None = None) -> dict:
        address = parse_url(url)
        return await self._run("fetch", address.url, deadline=min(deadline, time.monotonic() + 30),
                               locale=locale, on_sent=on_sent)

    @staticmethod
    def _failure(state, code):
        if state["phase"] == "policy":
            return {"error_code": "fetch_dns_unverified", "terminal": True}
        if state["waiting"]:
            return {"error_code": "service_busy", "terminal": False,
                    **({"skipped": "unavailable"} if not state["sent"] else {})}
        if state["phase"] == "extract":
            code = "fetch_target_unreadable"
        return {"error_code": code, "terminal": False}

    async def _run(self, operation, url, *, deadline, locale="en", on_sent=None):
        state = {"sent": False, "host": None, "waiting": False, "phase": "policy" if operation == "gate" else "fetch"}
        try:
            result = await asyncio.wait_for(self._invoke(operation, url, locale, state, on_sent, deadline),
                                            timeout=max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            result = self._failure(state, "fetch_timeout")
        if state.get("fallback") and result.get("error_code") and not result.get("terminal"):
            result = {**state["fallback"], "cooldown_code": result["error_code"],
                      "retry_after": result.get("retry_after"), **({"skipped": result["skipped"]} if result.get("skipped") else {})}
        for field in ("follow_url", "follow_kind"):
            result.pop(field, None)
        result["sent"] = state["sent"]
        result.update(state.get("transport", {}))
        host = state["host"]
        cooldown_code = result.pop("cooldown_code", None) or result.get("error_code")
        retry_after = result.pop("retry_after", None)
        if host and not result.get("skipped"):
            now = datetime.now(timezone.utc)
            if cooldown_code in {"fetch_timeout", "fetch_unavailable", "service_rate_limited"}:
                deadline_at = retry_after_deadline(retry_after, now)
                previous = self._cooldowns.get(host, (0, 0))
                backoff = min(300, max(30, previous[1] * 2))
                until = max(now.timestamp() + backoff, deadline_at.timestamp() if deadline_at else 0)
                self._cooldowns[host] = (until, backoff)
                self._cooldowns.move_to_end(host)
                while len(self._cooldowns) > 1024:
                    self._cooldowns.popitem(last=False)
                result["retry_at"] = datetime.fromtimestamp(until, timezone.utc).isoformat()
            elif not result.get("error_code"):
                self._cooldowns.pop(host, None)
        if not result.get("error_code"):
            result.pop("skipped", None)  # Retained content is a completed fetch, not an admission skip.
        return result

    async def _invoke(self, operation, url, locale, state, on_sent, deadline):
        # No global permit is held while a host is queued. In particular, a slow
        # site's lock must never exhaust the mandatory policy gate's capacity.
        with tempfile.TemporaryDirectory(prefix="avibe-fetch-") as home:
            environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
            environment.update(HOME=home, USERPROFILE=home, TMPDIR=home, TMP=home, TEMP=home,
                               XDG_CONFIG_HOME=home, XDG_CACHE_HOME=home)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", "-c", _WORKER, str(Path(__file__).resolve().parents[3]),
                cwd=home, env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_IPC_LIMIT,
            )
            lock, locked_host = None, None
            monitor = asyncio.create_task(self._monitor(process))
            reader = None
            try:
                process.stdin.write((json.dumps({"operation": operation, "url": url, "locale": locale, "deadline": deadline}) + "\n").encode())
                await process.stdin.drain()
                while True:
                    reader = asyncio.create_task(process.stdout.readline())
                    completed, _ = await asyncio.wait({reader, monitor}, return_when=asyncio.FIRST_COMPLETED)
                    if monitor in completed:
                        return self._failure(state, "fetch_unavailable")
                    raw = reader.result()
                    if not raw or len(raw) > _IPC_LIMIT:
                        return self._failure(state, "fetch_unavailable")
                    event = await asyncio.to_thread(json.loads, raw)
                    kind = event.get("event")
                    if kind in {"result", "failure"}:
                        return event["result"] if kind == "result" else {key: value for key, value in event.items() if key != "event"}
                    if kind == "host":
                        host = event["host"]
                        if host != locked_host:
                            if lock:
                                lock.release()
                                lock, locked_host = None, None
                            # Never attribute queue expiry to the host's health.
                            state["waiting"] = True
                            candidate_lock = self._locks.setdefault(host, asyncio.Lock())
                            await candidate_lock.acquire()
                            lock, locked_host = candidate_lock, host
                            state["waiting"] = False
                        state["host"] = host
                        until = self._cooldowns.get(host, (0, 0))[0]
                        if until > time.time():
                            return {"error_code": "fetch_unavailable", "skipped": "cooling", "terminal": False,
                                    "retry_at": datetime.fromtimestamp(until, timezone.utc).isoformat()}
                    elif kind == "phase":
                        state["phase"] = event["phase"]
                    elif kind == "fallback":
                        state["fallback"] = event["result"]
                    elif kind == "transport":
                        state["transport"] = {key: event[key] for key in ("transport_degraded", "transport_degraded_reason")}
                    elif kind == "sent":
                        if not state["sent"]:
                            if on_sent is not None:
                                result = on_sent()
                                if inspect.isawaitable(result):
                                    await result
                            state["sent"] = True
                    else:
                        return self._failure(state, "fetch_unavailable")
                    process.stdin.write(b"ok\n")
                    await process.stdin.drain()
            finally:
                # This is our exact child PID. Killing it also closes its
                # sockets and ends a stuck DNS/parser/native callback.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                for task in (reader, monitor):
                    if task:
                        task.cancel()
                await asyncio.gather(*(task for task in (reader, monitor) if task), return_exceptions=True)
                if lock:
                    lock.release()

    @staticmethod
    async def _monitor(process):
        try:
            child = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            await asyncio.Future()  # Its bounded stdout, not exit timing, owns the result.
        while process.returncode is None:
            try:
                if (await asyncio.to_thread(child.memory_info)).rss > _MEMORY_LIMIT:
                    return
            except psutil.NoSuchProcess:
                # stdout may still contain the result after normal child exit.
                await asyncio.sleep(0.1)
                continue
            await asyncio.sleep(0.05)
        # Keep the stdout reader authoritative for an exited worker.
        await asyncio.Future()
