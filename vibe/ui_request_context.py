"""Request-context presence without importing the Web server.

The compatibility adapter owns setting/resetting this context; shared services
only need to ask whether they are executing inside a request.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibe.ui_compat import CompatRequest

_request_var: ContextVar[CompatRequest] = ContextVar("ui_request")


def has_request_context() -> bool:
    try:
        _request_var.get()
    except LookupError:
        return False
    return True
