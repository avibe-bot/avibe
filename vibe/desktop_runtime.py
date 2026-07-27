"""Desktop-shell endpoint and listener contracts.

The desktop shell always connects through a literal loopback origin, even when
the user's primary UI bind is a specific LAN or overlay-network address.  The
UI process owns both listeners so the shell never has to interpret Avibe
configuration or broaden its navigation policy.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict

DESKTOP_ENDPOINT_SCHEMA_VERSION: Literal[1] = 1
DESKTOP_RUNTIME_ID_ENV = "AVIBE_DESKTOP_RUNTIME_ID"
DESKTOP_RUNTIME_ROOT_ENV = "AVIBE_DESKTOP_RUNTIME_ROOT"


class DesktopEndpointPayload(TypedDict):
    schema_version: Literal[1]
    origin: str


def desktop_runtime_id(base_env: Mapping[str, str] | None = None) -> str | None:
    """Return the validated identity of a desktop-managed Runtime."""

    env = os.environ if base_env is None else base_env
    value = env.get(DESKTOP_RUNTIME_ID_ENV, "")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def private_desktop_runtime_root(base_env: Mapping[str, str] | None = None) -> Path | None:
    """Return the app-private Runtime root supplied by the desktop launcher."""

    env = os.environ if base_env is None else base_env
    value = env.get(DESKTOP_RUNTIME_ROOT_ENV, "")
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        return None
    return root.resolve(strict=False)


def is_private_desktop_runtime_path(
    path: str | os.PathLike[str] | None,
    base_env: Mapping[str, str] | None = None,
) -> bool:
    """Whether *path* belongs to the verified app-private Runtime tree."""

    if not path:
        return False
    root = private_desktop_runtime_root(base_env)
    if root is None:
        return False
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


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
    advertised = ipaddress.ip_address(desktop_loopback_host(host))
    return not address.is_unspecified and address != advertised


def ui_listener_hosts(bind_host: str | None) -> tuple[str, ...]:
    """Return the primary listener plus any desktop-only loopback listener."""

    primary = "127.0.0.1" if bind_host is None else bind_host.strip()
    if requires_desktop_loopback_listener(primary):
        return primary, desktop_loopback_host(primary)
    return (primary,)


def normalize_desktop_port(port: int | str) -> int:
    if isinstance(port, str):
        if not port.isascii() or not port.isdecimal():
            raise ValueError("desktop endpoint port must be between 1 and 65535")
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("desktop endpoint port must be between 1 and 65535")
    return port


def desktop_origin(bind_host: str | None, port: int | str) -> str:
    """Build the desktop shell's exact loopback origin."""

    normalized_port = normalize_desktop_port(port)
    loopback = desktop_loopback_host(bind_host)
    rendered_host = f"[{loopback}]" if ":" in loopback else loopback
    return f"http://{rendered_host}:{normalized_port}"


def desktop_endpoint_payload(
    bind_host: str | None,
    port: int | str,
) -> DesktopEndpointPayload:
    """Return the frozen schema-v1 descriptor consumed by the desktop shell."""

    return {
        "schema_version": DESKTOP_ENDPOINT_SCHEMA_VERSION,
        "origin": desktop_origin(bind_host, port),
    }
