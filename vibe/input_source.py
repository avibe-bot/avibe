"""Resolve terminal presentation at the authenticated HTTP admission boundary."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.input_source import SURFACES, TERMINALS
from storage.models import remote_device_sessions

if TYPE_CHECKING:
    from vibe.authorization import AuthorizationContext


def resolve_source(
    conn: Connection, *, identity: Mapping[str, Any] | None,
    authorization: AuthorizationContext | None, client_hint: Any, surface_hint: Any,
) -> dict[str, str | bool]:
    """Prefer this authenticated session's registry row; client hints never confer authority.

    Only the type leaves the registry. Names, ids, tokens, addresses, and versions
    are neither copied to the message nor rendered in the Agent input.
    """
    terminal = None
    if (isinstance(identity, Mapping) and authorization is not None
            and authorization.is_remote and authorization.subject == identity.get("sub")
            and identity.get("_device_binding") and identity.get("browser_session_id")):
        kind = conn.scalar(select(remote_device_sessions.c.kind).where(
            remote_device_sessions.c.binding == identity["_device_binding"],
            remote_device_sessions.c.subject == authorization.subject,
            remote_device_sessions.c.session_id == identity["browser_session_id"],
            remote_device_sessions.c.expires_at > int(time.time()),
        ))
        terminal = {"iphone_app": "iphone_app", "desktop_app": "mac_app", "browser": "browser",
                    "home_screen_app": "browser"}.get(kind)
    hint = terminal is None
    if terminal is None:
        terminal = client_hint if isinstance(client_hint, str) and client_hint in TERMINALS else "unknown"
    result: dict[str, str | bool] = {"type": terminal}
    if terminal != "unknown":
        result["surface"] = surface_hint if isinstance(surface_hint, str) and surface_hint in SURFACES else "main"
        if hint:
            result["hint"] = True
    return result
