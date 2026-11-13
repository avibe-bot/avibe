"""Fixed-size, UI-loop-owned HTTP admission counts; never request records.

Counts cover the shared UI application, after DirectIngressGuard, not TLS,
CONNECT, parser failures, response delivery, committed effects or replay.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vibe.direct_ingress import DIRECT_INGRESS_SCOPE_KEY

ROUTE_CLASSES = ("direct_ingress", "main_listener")
METHOD_CLASSES = ("get_head_options", "post_put_patch_delete", "other")
# Saturation bounds integer storage as well as label cardinality.
MAX_COUNT = (1 << 63) - 1


class RequestCounters:
    """One six-cell table for a UI service lifetime, retained across OFF/ON."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Service lifecycle only; no request-facing clear/reset operation."""
        self._counts = [[0, 0, 0], [0, 0, 0]]

    def observe(self, scope: Any) -> None:
        route = 0 if scope.get(DIRECT_INGRESS_SCOPE_KEY) is not None else 1
        method = scope.get("method")
        if method in ("GET", "HEAD", "OPTIONS"):
            method_class = 0
        elif method in ("POST", "PUT", "PATCH", "DELETE"):
            method_class = 1
        else:
            method_class = 2
        count = self._counts[route][method_class]
        if count < MAX_COUNT:
            self._counts[route][method_class] = count + 1

    def snapshot(self, *, enabled: bool) -> dict[str, Any]:
        # Copies only fixed enums/integers; no scope or mutable table escapes.
        return {
            "enabled": enabled,
            "scope": "ui_service_lifetime",
            "counts": {
                route: dict(zip(METHOD_CLASSES, row, strict=True))
                for route, row in zip(ROUTE_CLASSES, self._counts, strict=True)
            },
            "saturated": any(count == MAX_COUNT for row in self._counts for count in row),
        }


class RequestCounterMiddleware:
    """Count once at admission; pass receive/send through completely unchanged."""

    def __init__(self, app: Any, *, counters: RequestCounters, enabled: Callable[[], bool]) -> None:
        self.app = app
        self.counters = counters
        self.enabled = enabled

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Disabled: no counter/label work, config I/O, clocks, locks or tasks.
        if scope["type"] == "http" and self.enabled():
            self.counters.observe(scope)
        await self.app(scope, receive, send)
