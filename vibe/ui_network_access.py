"""Observe the UI's accepting sockets on its owning event loop, not its launch flags."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress


@dataclass(frozen=True)
class Listener:
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    server: object


def active_listeners(ui_server) -> tuple[Listener, ...]:
    if not getattr(ui_server, 'started', False) or getattr(ui_server, 'should_exit', False):
        return ()
    listeners = []
    try:
        for server in ui_server.servers:
            if server.is_serving():
                # asyncio owns acceptance on this event loop. SO_ACCEPTCONN is
                # not a portable getsockopt (Darwin may return ENOPROTOOPT).
                for sock in server.sockets:
                    host, port = sock.getsockname()[:2]
                    listeners.append(Listener(ipaddress.ip_address(host), port, server))
    except (AttributeError, OSError, ValueError):
        return ()
    return tuple(listeners)


def loopback_target(listeners, port):
    if not listeners or {item.port for item in listeners} != {port}:
        return None
    versions = {item.address.version for item in listeners}
    if len(versions) != 1:
        return None
    target = '::1' if 6 in versions else '127.0.0.1'
    if not any(item.address.is_unspecified or str(item.address) == target for item in listeners):
        return None  # cannot demonstrate continued access at this loopback literal
    return target


def close_redundant(listeners, target):
    # A wildcard may be the only path serving loopback. Never drop that path
    # to make the readback look closed. Server.close() stops accepting, it
    # does not abort established connections (including the invoking POST).
    keep = {item.server for item in listeners if str(item.address) == target}
    if keep:
        for server in {item.server for item in listeners if not item.address.is_loopback} - keep:
            server.close()
