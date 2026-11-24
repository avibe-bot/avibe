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


def matching_reload_target(listeners, config, requested_host, port):
    """Pin a no-change reload to the address actually being served, never to DNS."""
    from vibe.desktop_runtime import ui_listener_hosts
    from vibe.runtime import effective_ui_bind_host

    def spelling(host):
        host = host.strip().lower()
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return host

    if not listeners or config.ui.setup_port != port or spelling(config.ui.setup_host) != spelling(requested_host):
        return None
    target = effective_ui_bind_host(config, requested_host=requested_host)
    if target == '*':
        target = '0.0.0.0'
    try:
        primary = str(ipaddress.ip_address(target))
    except ValueError:
        # run_ui_server uses AF_INET for hostnames. Resolve once and launch with
        # that literal only if its complete address set is already serving.
        import socket

        try:
            primary = str(ipaddress.IPv4Address(socket.getaddrinfo(target, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]))
        except (IndexError, OSError, ValueError, UnicodeError):
            return None
    observed = {(str(item.address), item.port) for item in listeners}
    expected = {(host, port) for host in ui_listener_hosts(primary)}
    return primary if observed == expected else None


def close_redundant(listeners, target):
    # A wildcard may be the only path serving loopback. Never drop that path
    # to make the readback look closed. Server.close() stops accepting, it
    # does not abort established connections (including the invoking POST).
    keep = {item.server for item in listeners if str(item.address) == target}
    if keep:
        for server in {item.server for item in listeners if not item.address.is_loopback} - keep:
            server.close()
