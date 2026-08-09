#!/usr/bin/env python3
"""Shared helpers for GitHub polling waiters."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any


RETRY_EXIT_CODE = 75
# Cycle completed, nothing worth waking the Agent for. `vibe watch` records the
# cycle and ends or re-arms without creating a follow-up run.
#
# The code alone is not enough: 64 is BSD `sysexits` EX_USAGE, so a watched command
# that rejects its own arguments exits with it and must keep failing loudly. `vibe
# watch` reads a quiet cycle only when the marker is on the waiter's output too, so
# always exit through `no_event()` rather than returning the bare code.
NO_EVENT_EXIT_CODE = 64
NO_EVENT_MARKER = "avibe-watch: no-event"
# `vibe watch` names the watch a waiter is a cycle of here. A waiter that keeps
# per-watch state on disk should own that state by this id, so two identically
# configured watches cannot silently share one file. Absent for a manual run.
WATCH_ID_ENV = "AVIBE_WATCH_ID"
# How many event reports from this watch `vibe watch` has durably queued as a
# follow-up, or empty for none. Flushing stdout proves nothing: the supervisor only
# sees the output once the process has exited, so a waiter that stages the cursors
# covering a reported event records this value with them and a later cycle promotes
# them only if the value has since CHANGED. Treat it as opaque and compare it, nothing
# more. The supervisor bumps it in the same transaction as the follow-up and keeps it
# out of the lifecycle fields a resume resets, so the answer is still there after a
# restart, or when a `once` watch is resumed long after its one report. Absent for a
# manual run, where printing IS the delivery.
LAST_DELIVERY_ENV = "AVIBE_WATCH_LAST_DELIVERY"
# Socket timeout for one GitHub request. Callers that have their own deadline need
# this to size a request budget: a waiter with 20s left cannot afford a fetch that
# is allowed to block for 30.
REQUEST_TIMEOUT_SECONDS = 30
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
NOT_MODIFIED_STATUS = 304
# GitHub compares timestamps at one-second resolution, and a bot that posts a
# review as a batch stamps several comments within the same second. Rewinding the
# `since` filter re-fetches the newest item or two, which the id cursors then
# drop, rather than risking a sibling that lands on the boundary second.
SINCE_REWIND_SECONDS = 2

_MISSING = object()


def no_event(summary: str = "") -> int:
    """End the cycle with nothing to report, and say so where the watch can see it.

    The summary goes to stderr so it stays in the watch log: it is the only record
    of what the waiter saw and chose not to forward, and no Agent turn carries it.
    """

    if summary:
        print(summary, file=sys.stderr)
    print(NO_EVENT_MARKER, file=sys.stderr)
    return NO_EVENT_EXIT_CODE


def get_token() -> str | None:
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            return value

    gh_path = shutil.which("gh")
    if not gh_path:
        return None

    try:
        result = subprocess.run(
            [gh_path, "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    token = result.stdout.strip()
    return token or None


def min_interval_for_unauthenticated(
    requests_per_poll: int,
    *,
    bootstrap_requests: int = 0,
) -> float:
    recurring_requests = max(requests_per_poll, 1)
    hourly_budget = max(1, 60 - max(bootstrap_requests, 0))
    return float(max(60, math.ceil((3600 * recurring_requests) / hourly_budget)))


class ResponseCache:
    """Per-URL ETag cache so an unchanged page costs nothing to re-check.

    GitHub answers a revalidated conditional request with 304 and does not charge
    it against the rate limit, so a waiter that sits on a quiet PR can keep
    polling indefinitely for free instead of re-downloading its whole history
    every interval.

    The cache lives for one waiter process. A 304 is only useful while the body it
    stands for is still in memory, so it is deliberately not persisted; the
    savings across cycles come from the cursor state file, which keeps the
    ``since`` filters narrow enough that a fresh fetch is small.
    """

    __slots__ = ("_entries", "revalidated", "downloaded")

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, Any]] = {}
        self.revalidated = 0
        self.downloaded = 0

    def etag_for(self, url: str) -> str | None:
        entry = self._entries.get(url)
        return entry[0] if entry is not None else None

    def store(self, url: str, etag: str, payload: Any) -> None:
        self._entries[url] = (etag, payload)

    def payload_for(self, url: str) -> Any:
        entry = self._entries.get(url)
        return _MISSING if entry is None else entry[1]

    def summary(self) -> str:
        total = self.revalidated + self.downloaded
        if not total:
            return "no GitHub requests issued"
        return (
            f"{total} GitHub request(s): {self.revalidated} revalidated as 304 "
            f"(free of rate limit), {self.downloaded} downloaded"
        )


def github_get(url: str, token: str | None, *, cache: ResponseCache | None = None) -> Any:
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "background-watch-hook/0.1.0")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    etag = cache.etag_for(url) if cache is not None else None
    if etag:
        request.add_header("If-None-Match", etag)

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if cache is not None:
                cache.downloaded += 1
                response_etag = response.headers.get("ETag")
                if response_etag:
                    cache.store(url, response_etag, payload)
            return payload
    except urllib.error.HTTPError as err:
        if err.code == NOT_MODIFIED_STATUS and cache is not None:
            payload = cache.payload_for(url)
            if payload is not _MISSING:
                cache.revalidated += 1
                return payload
        raise


def latest_timestamp(items: list[dict[str, Any]]) -> str | None:
    """Newest ``updated_at``/``created_at`` across items.

    GitHub emits a fixed-width UTC form (``2026-08-04T06:47:12Z``), so a string
    comparison orders these correctly without parsing every candidate.
    """

    newest = ""
    for item in items:
        for key in ("updated_at", "created_at"):
            value = item.get(key)
            if isinstance(value, str) and value > newest:
                newest = value
    return newest or None


def since_param(items: list[dict[str, Any]]) -> str | None:
    """A ``since`` value for the next poll that cannot skip an item."""

    newest = latest_timestamp(items)
    if not newest:
        return None
    try:
        parsed = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return None
    rewound = parsed - timedelta(seconds=SINCE_REWIND_SECONDS)
    return rewound.strftime("%Y-%m-%dT%H:%M:%SZ")


def later_since(previous: str | None, items: list[dict[str, Any]]) -> str | None:
    """Advance a ``since`` filter, never rewinding it."""

    candidate = since_param(items)
    if not candidate:
        return previous
    if previous and previous >= candidate:
        return previous
    return candidate


def is_retryable_http_error(err: urllib.error.HTTPError) -> bool:
    try:
        code = int(err.code)
    except Exception:
        return False
    return code in RETRYABLE_HTTP_STATUS_CODES


def get_authenticated_login(token: str | None) -> str | None:
    if not token:
        return None

    try:
        payload = github_get("https://api.github.com/user", token)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    login = payload.get("login")
    return str(login) if isinstance(login, str) and login else None


def list_paginated(
    base_url: str,
    token: str | None,
    *,
    cache: ResponseCache | None = None,
) -> list[dict[str, Any]]:
    items, _request_count = list_paginated_with_count(base_url, token, cache=cache)
    return items


def list_paginated_with_count(
    base_url: str,
    token: str | None,
    *,
    stop_after_id: int | None = None,
    max_pages: int | None = None,
    cache: ResponseCache | None = None,
) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    page = 1
    request_count = 0
    while True:
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}per_page=100&page={page}"
        payload = github_get(url, token, cache=cache)
        request_count += 1
        if not isinstance(payload, list):
            raise RuntimeError(f"Expected a JSON list from {url}")
        if not payload:
            break
        page_items = [item for item in payload if isinstance(item, dict)]
        items.extend(page_items)
        if stop_after_id is not None and stop_after_id > 0:
            if any(isinstance(item.get("id"), int) and int(item["id"]) <= stop_after_id for item in page_items):
                break
        if max_pages is not None and page >= max_pages:
            break
        if len(payload) < 100:
            break
        page += 1
    return items, request_count


def squash(text: str | None, *, limit: int = 140) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def max_id(items: list[dict[str, Any]]) -> int:
    values = [int(item["id"]) for item in items if isinstance(item.get("id"), int)]
    return max(values, default=0)


def filter_new(items: list[dict[str, Any]], since_id: int) -> list[dict[str, Any]]:
    return sorted(
        [item for item in items if isinstance(item.get("id"), int) and int(item["id"]) > since_id],
        key=lambda item: (str(item.get("created_at") or ""), int(item["id"])),
    )


def requests_per_poll(*collections: list[dict[str, Any]]) -> int:
    requests = 0
    for items in collections:
        requests += max(1, (len(items) // 100) + 1)
    return requests
