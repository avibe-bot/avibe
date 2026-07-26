"""Desktop-shell endpoint and listener contracts.

The desktop shell always connects through a literal loopback origin, even when
the user's primary UI bind is a specific LAN or overlay-network address.  The
UI process owns both listeners so the shell never has to interpret Avibe
configuration or broaden its navigation policy.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Literal, TypedDict

DESKTOP_ENDPOINT_SCHEMA_VERSION: Literal[1] = 1


class DesktopEndpointPayload(TypedDict):
    schema_version: Literal[1]
    origin: str


def _normalized_bind_host(bind_host: str | None) -> str:
    host = (bind_host or "127.0.0.1").strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _bind_family(bind_host: str | None) -> socket.AddressFamily:
    host = _normalized_bind_host(bind_host)
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def desktop_loopback_host(bind_host: str | None) -> str:
    """Return the literal loopback matching the primary bind's IP family."""

    return "::1" if _bind_family(bind_host) == socket.AF_INET6 else "127.0.0.1"


def requires_desktop_loopback_listener(bind_host: str | None) -> bool:
    """Whether a specific primary bind needs a second loopback listener."""

    host = _normalized_bind_host(bind_host)
    if host == "*":
        return False
    if host.lower() == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Match the first address the same-family socket bind will select. A
        # loopback-only hostname already serves the desktop; an unresolved or
        # non-loopback name still needs the companion listener.
        try:
            resolved = socket.getaddrinfo(
                host,
                None,
                family=_bind_family(host),
                type=socket.SOCK_STREAM,
            )
            address = ipaddress.ip_address(resolved[0][4][0])
        except (IndexError, OSError, ValueError):
            return True
    return not address.is_loopback and not address.is_unspecified


def ui_listener_hosts(bind_host: str | None) -> tuple[str, ...]:
    """Return the primary listener plus any desktop-only loopback listener."""

    primary = "127.0.0.1" if bind_host is None else bind_host.strip()
    if requires_desktop_loopback_listener(primary):
        return primary, desktop_loopback_host(primary)
    return (primary,)


def desktop_origin(bind_host: str | None, port: int) -> str:
    """Build the desktop shell's exact loopback origin."""

    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("desktop endpoint port must be between 1 and 65535")
    loopback = desktop_loopback_host(bind_host)
    rendered_host = f"[{loopback}]" if ":" in loopback else loopback
    return f"http://{rendered_host}:{port}"


def desktop_endpoint_payload(bind_host: str | None, port: int) -> DesktopEndpointPayload:
    """Return the frozen schema-v1 descriptor consumed by the desktop shell."""

    return {
        "schema_version": DESKTOP_ENDPOINT_SCHEMA_VERSION,
        "origin": desktop_origin(bind_host, port),
    }
