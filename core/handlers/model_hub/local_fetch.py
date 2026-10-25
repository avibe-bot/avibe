"""Dormant local fetch: disposable workers, bounded admission and host health.

The mandatory gate has independent capacity. Fetch workers start only after
atomic host/worker admission; queued calls own neither a process nor a host.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psutil

from .async_owner import await_owned_task
from .fetch_policy import FetchFailure, parse_url
from .retry import retry_after_deadline


_WORKER = "import sys; sys.path.insert(0, sys.argv.pop(1)); from core.handlers.model_hub.fetch_worker import main; main()"
_MEMORY_LIMIT = 768 * 1024 * 1024
_IPC_LIMIT = 1200000
_WORKERS = 8


class LocalFetchEngine:
    def __init__(self):
        self._hosts: set[str] = set()
        self._workers = {"gate": 0, "fetch": 0}
        self._changed = asyncio.Event()
        self._cooldowns = OrderedDict()

    def _notify(self):
        previous, self._changed = self._changed, asyncio.Event()
        previous.set()

    async def _admit(self, operation, host, state):
        state["waiting"] = True
        while self._workers[operation] >= _WORKERS or (operation == "fetch" and host in self._hosts):
            await self._changed.wait()
        # No await between checking and claiming: no host is held by a call
        # waiting for capacity, so redirects cannot deadlock with that waiter.
        self._workers[operation] += 1
        state["slot"] = True
        if operation == "fetch":
            self._hosts.add(host)
            state["held_host"] = host
        state["waiting"] = False

    async def _change_host(self, host, state):
        if host != state["held_host"]:
            if state["held_host"]:
                self._hosts.discard(state["held_host"])
                state["held_host"] = None
                self._notify()
            state["waiting"] = True
            while host in self._hosts:
                await self._changed.wait()
            self._hosts.add(host)
            state["held_host"] = host
            state["waiting"] = False
        state["host"] = host

    def _release(self, operation, state):
        if state["held_host"]:
            self._hosts.discard(state["held_host"])
            state["held_host"] = None
        if state["slot"]:
            self._workers[operation] -= 1
            state["slot"] = False
        self._notify()

    async def check(self, url: str, *, deadline: float) -> str:
        """Mandatory gate, also when local fetching is disabled or reordered."""
        address = parse_url(url)
        result = await self._run("gate", address.url, deadline=min(deadline, time.monotonic() + 5))
        if result.get("error_code"):
            code = result["error_code"]
            raise FetchFailure(code if code in {"fetch_url_blocked", "service_busy"} else "fetch_dns_unverified", terminal=True)
        return result["url"]

    async def fetch(self, url: str, *, deadline: float, locale: str = "en", on_sent: Callable | None = None) -> dict:
        address = parse_url(url)
        return await self._run("fetch", address.url, deadline=min(deadline, time.monotonic() + 30),
                               locale=locale, on_sent=on_sent)

    @staticmethod
    def _failure(state, code):
        if state["operation"] == "gate":
            return {"error_code": "service_busy" if state["waiting"] else "fetch_dns_unverified", "terminal": True}
        if code == "fetch_timeout" and (state["waiting"] or state["phase"] in {"policy", "admission"}):
            # Budget spent on admission/revalidation is not a DNS failure and
            # cannot cool a host. The chain must gate the ORIGINAL URL again.
            return {"error_code": "fetch_timeout" if state["sent"] else "service_busy", "terminal": False,
                    "cooldown_suppressed": True}
        if state["phase"] == "policy":
            return {"error_code": "fetch_dns_unverified", "terminal": True}
        if state["phase"] == "extract":
            code = "fetch_target_unreadable"
        return {"error_code": code, "terminal": False}

    async def _run(self, operation, url, *, deadline, locale="en", on_sent=None):
        state = {"operation": operation, "sent": False, "host": None, "held_host": None, "slot": False,
                 "waiting": True, "phase": "policy" if operation == "gate" else "admission"}
        try:
            result = await asyncio.wait_for(self._invoke(operation, url, locale, state, on_sent, deadline),
                                            timeout=max(0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            result = self._failure(state, "fetch_timeout")
        if result.get("error_code") == "service_busy" and not state["sent"]:
            result["skipped"] = "unavailable"
        if state.get("fallback") and result.get("error_code") and not result.get("terminal"):
            result = {**state["fallback"], "cooldown_code": result["error_code"],
                      "cooldown_suppressed": result.get("cooldown_suppressed", False),
                      "retry_after": result.get("retry_after"), **({"skipped": result["skipped"]} if result.get("skipped") else {})}
        for field in ("follow_url", "follow_kind"):
            result.pop(field, None)
        result["sent"] = state["sent"]
        result.update(state.get("transport", {}))
        host = state["host"]
        cooldown_code = result.pop("cooldown_code", None) or result.get("error_code")
        suppressed = result.pop("cooldown_suppressed", False)
        retry_after = result.pop("retry_after", None)
        if host and not result.get("skipped") and not suppressed:
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
            result.pop("skipped", None)
        return result

    async def _invoke(self, operation, url, locale, state, on_sent, deadline):
        process = monitor = reader = None
        try:
            await self._admit(operation, parse_url(url).host, state)
            with tempfile.TemporaryDirectory(prefix="avibe-fetch-") as home:
                environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
                environment.update(HOME=home, USERPROFILE=home, TMPDIR=home, TMP=home, TEMP=home,
                                   XDG_CONFIG_HOME=home, XDG_CACHE_HOME=home)
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-B", "-c", _WORKER, str(Path(__file__).resolve().parents[3]),
                    cwd=home, env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=_IPC_LIMIT,
                ))
                try:
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        process = await await_owned_task(spawning)
                        raise
                    monitor = asyncio.create_task(self._monitor(process))
                    process.stdin.write((json.dumps({"operation": operation, "url": url, "locale": locale,
                                                    "remaining_seconds": max(0, deadline - time.monotonic())}) + "\n").encode())
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
                            await self._change_host(host, state)
                            state["phase"] = "policy"  # The worker re-resolves after this ACK.
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
                            state["phase"] = "fetch"
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
                    async def cleanup():
                        try:
                            if process is not None:
                                if process.returncode is None:
                                    try:
                                        process.kill()
                                    except ProcessLookupError:
                                        pass
                                await process.wait()
                        finally:
                            for task in (reader, monitor):
                                if task:
                                    task.cancel()
                            await asyncio.gather(*(task for task in (reader, monitor) if task), return_exceptions=True)
                    # Timeout followed by Stop must not interrupt kill/reap,
                    # strand a host, or leave a monitor reading a dead PID.
                    await await_owned_task(asyncio.create_task(cleanup()))
        finally:
            self._release(operation, state)

    @staticmethod
    async def _monitor(process):
        try:
            child = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            await asyncio.Future()
        while process.returncode is None:
            try:
                if (await asyncio.to_thread(child.memory_info)).rss > _MEMORY_LIMIT:
                    return
            except psutil.NoSuchProcess:
                await asyncio.sleep(0.1)
                continue
            await asyncio.sleep(0.05)
        await asyncio.Future()  # Bounded stdout, not exit timing, owns the result.
