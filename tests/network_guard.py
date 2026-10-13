"""No test reaches past this machine's loopback, however the request is made.

Two leaks turned CI shards red in one day: an update-checker test downloaded
askill from GitHub (#2531), and a Model Hub test that seeded Vibey with no
cached models.dev copy fetched the catalog, where an SSL read hung until the
file's watchdog killed it. Neither test meant to touch the network; each
reached it through product code nothing had stubbed.

``audit`` is a CPython audit hook (``tests/conftest.py`` installs it before
any product module is imported). It sees the socket module's own events,
raised in C, so it covers every client that ends in a Python socket --
``urllib``, ``http.client``, ``requests``/``urllib3``, ``httpx`` and
``aiohttp`` on the standard asyncio loop -- in every thread, whatever name the
caller imported, and between tests. It refuses:

- ``socket.connect`` (``connect`` and ``connect_ex``) and ``socket.sendto`` /
  ``socket.sendmsg`` to an IPv4 or IPv6 address that is not loopback or
  unspecified, or to a host name other than ``localhost``;
- ``socket.getaddrinfo`` and ``socket.gethostbyname`` for a host name other
  than ``localhost``, so a lookup fails before it queries DNS and the refusal
  names the host rather than the address it would have resolved to. IP
  literals resolve without a query and are left to the connect check;
- ``socket.gethostbyaddr``, a reverse lookup, of anything but loopback, an
  unspecified address included.

Unix sockets and every other address family pass. A refusal raises
``NetworkAccessRefused``, an ``OSError``, so the product takes its offline path
instead of hanging, and is recorded: a refusal the product swallows still
fails the test that made it (``take_refusals``), and one made outside any
test fails the run.

``allow_network(reason=...)`` opts out a test that is meant to be live. It is
honoured only on an ``integration`` test, which the unit shards exclude, because
the opt-out lifts the guard for the whole process while that test runs: every
thread, including one an earlier test left behind. The opt-out spans the test's
whole run protocol, including the session-scoped fixtures it sets up.

Outside the hook: child processes (each loads its own interpreter), a native
resolver or transport that bypasses CPython's socket module, such as uvloop's
or c-ares's, and ``socket.getnameinfo``, whose audit event omits the flags
that say whether it queries DNS.
"""

from __future__ import annotations

import errno
import ipaddress
import socket
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
_THIS_FILE = str(Path(__file__).resolve())
_INET_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})
_LOOKUP_EVENTS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"})
_SEND_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_EVENTS = _LOOKUP_EVENTS | _SEND_EVENTS
_STACK_FRAMES = 6


class NetworkAccessRefused(OSError):
    """A test tried to reach past this machine's loopback."""


@dataclass
class _Window:
    """One test's run protocol: whether it may reach the network, and what was refused."""

    nodeid: str
    allowed: bool
    refusals: list[str] = field(default_factory=list)


_current: _Window | None = None
# Refusals made while no test was running, in order: collection, module import,
# or a thread that outlived the test that started it.
refused_outside_tests: list[str] = []
# Recording a refusal and closing a window are atomic with respect to each other,
# so a refusal lands either in the window its test still reports or outside any test.
_lock = threading.Lock()


def begin_test(nodeid: str, *, allowed: bool) -> None:
    global _current
    with _lock:
        _current = _Window(nodeid, allowed)


def end_test() -> list[str]:
    """Close the running test's window; what it was refused."""

    global _current
    with _lock:
        window, _current = _current, None
    return window.refusals if window is not None else []


def take_refusals() -> list[str]:
    """Return and forget what the running test was refused, for a test that expects refusals."""

    with _lock:
        window = _current
        if window is None:
            return []
        taken, window.refusals[:] = list(window.refusals), []
    return taken


def _is_local_name(host: str) -> bool:
    return host.lower().rstrip(".") == "localhost"


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host.partition("%")[0])  # an IPv6 zone index names no host
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _host_text(host) -> str | None:
    if isinstance(host, bytes):
        return host.decode("ascii", "replace")
    return host if isinstance(host, str) else None


def _is_remote(host: str) -> bool:
    ip = _ip(host)
    if ip is None:
        return not _is_local_name(host)
    return not (ip.is_loopback or ip.is_unspecified)


def _refused_target(event: str, args: tuple) -> str | None:
    """What ``event`` would reach past loopback, as the caller named it, or None."""

    if event in _LOOKUP_EVENTS:
        host = _host_text(args[0])
        if not host:
            return None
        ip = _ip(host)
        if event == "socket.gethostbyaddr":
            # A reverse lookup asks DNS about any address but loopback, an unspecified one included.
            local = ip.is_loopback if ip is not None else _is_local_name(host)
        else:
            # A forward lookup of an IP literal sends no query.
            local = ip is not None or _is_local_name(host)
        if local:
            return None
        return f"{host}:{args[1]}" if event == "socket.getaddrinfo" and args[1] is not None else host
    sock, address = args[0], args[-1]
    if sock.family not in _INET_FAMILIES or not isinstance(address, tuple) or not address:
        return None
    host = _host_text(address[0])
    if host is None or not _is_remote(host):
        return None
    port = address[1] if len(address) > 1 else "?"
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _origin() -> str:
    """The innermost repository frames that led here, so the report names the call site."""

    frames = [
        frame
        for frame in traceback.extract_stack()
        if frame.filename.startswith(_REPO_ROOT)
        and frame.filename != _THIS_FILE
        and "site-packages" not in frame.filename  # a virtualenv inside the checkout
    ][-_STACK_FRAMES:]
    return " <- ".join(
        f"{frame.filename[len(_REPO_ROOT) + 1 :]}:{frame.lineno} {frame.name}" for frame in reversed(frames)
    )


def audit(event: str, args: tuple) -> None:
    if event not in _EVENTS:
        return
    window = _current
    if window is not None and window.allowed:
        return
    target = _refused_target(event, args)
    if target is None:
        return
    # A lookup the event loop hands to its executor carries no frame of the code that asked for it.
    origin = _origin() or "no repository frame on this thread"
    message = f"{event} to {target} (thread {threading.current_thread().name}) from {origin}"
    with _lock:
        # Read again: the window seen above may have closed while the stack was rendered.
        window = _current
        if window is not None and window.allowed:
            return
        (window.refusals if window is not None else refused_outside_tests).append(message)
    raise NetworkAccessRefused(
        errno.ENETUNREACH,
        f"tests may not reach past loopback: {event} to {target}; stub the fetch at the boundary that owns it, "
        "or mark a test that is meant to be live with allow_network(reason=...)",
    )
