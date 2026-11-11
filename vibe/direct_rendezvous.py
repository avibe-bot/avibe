"""Rendezvous for remote direct paths.

An authorized remote client asks this instance, over the existing authenticated
origin, how to dial it directly: the instance's transport endpoint ID and the
addresses it can be reached on. Contract: ``docs/plans/remote-direct-rendezvous.md``.

The answer is public endpoint information for one authenticated session. It is
not a credential: a client that connects still passes the strict ingress and the
ordinary remote authentication. Nothing here holds or returns a key.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

RENDEZVOUS_VERSION = 1
# Application protocol the instance endpoint accepts and the client must offer.
DIRECT_ALPN = "avibe-direct/1"
RENDEZVOUS_TTL_SECONDS = 300
# How long a client waits before asking again when no endpoint is available.
UNAVAILABLE_RETRY_SECONDS = 60

_MAX_ADDRESSES = 8
_MAX_ADMISSIONS = 256
_MAX_ADMISSIONS_PER_SUBJECT = 8
# An endpoint ID is an Ed25519 public key in iroh's canonical lowercase hex.
_ENDPOINT_ID_RE = re.compile(r"[0-9a-f]{64}")
_SOCKET_ADDRESS_RE = re.compile(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})")

_LAN_NETWORKS_V4 = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_UNIQUE_LOCAL_V6 = ipaddress.ip_network("fc00::/7")
_KIND_ORDER = {"lan": 0, "ipv6": 1}


def is_endpoint_id(value: Any) -> bool:
    return isinstance(value, str) and _ENDPOINT_ID_RE.fullmatch(value) is not None


def classify_address(value: str) -> dict[str, str] | None:
    """Return the candidate a client may be told about, or None to withhold it.

    ``lan`` is a private IPv4 or unique-local IPv6 address: reachable only from
    the same network. ``ipv6`` is a global IPv6 address: reachable from anywhere
    the path allows. Loopback, link-local, unspecified, multicast, shared
    (CGNAT), documentation, and public IPv4 addresses are withheld; nothing in
    this phase can make a path to them work.
    """

    match = _SOCKET_ADDRESS_RE.fullmatch(value or "")
    if match is None:
        return None
    port = int(match["port"])
    if not 1 <= port <= 65535:
        return None
    try:
        address = ipaddress.ip_address(match["v6"] or match["v4"])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return None
        if address in _UNIQUE_LOCAL_V6:
            kind = "lan"
        elif address.is_global and not address.is_multicast:
            kind = "ipv6"
        else:
            return None
        return {"kind": kind, "address": f"[{address.compressed}]:{port}"}
    if any(address in network for network in _LAN_NETWORKS_V4):
        return {"kind": "lan", "address": f"{address}:{port}"}
    return None


@dataclass(frozen=True)
class DirectEndpoint:
    """What the transport publishes about its endpoint: public information only."""

    endpoint_id: str
    # Socket addresses the endpoint is bound to, as "ip:port" or "[ip]:port".
    addresses: tuple[str, ...]


AdmissionListener = Callable[[str, int], None]


class DirectRendezvous:
    """The published endpoint and the client endpoints admitted to dial it.

    The transport calls :meth:`publish` whenever its endpoint or addresses
    change and :meth:`is_admitted` when a peer connects. The Web UI calls
    :meth:`offer` for an authenticated remote session.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # Distinguishes this process's generations from any earlier process's.
        self._boot = secrets.token_hex(8)
        self._revision = 0
        self._endpoint: DirectEndpoint | None = None
        self._candidates: tuple[dict[str, str], ...] = ()
        self._unavailable_reason = "not_running"
        # client endpoint ID -> (subject, admitted_until), oldest first.
        self._admissions: dict[str, tuple[str, int]] = {}
        self._admission_listener: AdmissionListener | None = None

    def set_admission_listener(self, listener: AdmissionListener | None) -> None:
        """Set the optional non-blocking hook for admission notices.

        The hook is called after the in-memory admission change and before
        :meth:`offer` returns. It receives an endpoint ID and a TTL in seconds;
        a zero TTL means the admission was dropped early. The listener is
        intentionally not replayed when installed, so a new sidecar starts
        with an empty set.
        """

        with self._lock:
            self._admission_listener = listener

    def publish(self, endpoint: DirectEndpoint | None, *, reason: str = "not_running") -> None:
        """Replace the published endpoint; ``None`` withdraws it for ``reason``.

        A change of endpoint ID or of the address set starts a new generation.
        A change of endpoint ID also ends every admission: a new endpoint key
        means a new transport, with no peers.
        """

        candidates: tuple[dict[str, str], ...] = ()
        if endpoint is not None:
            if not is_endpoint_id(endpoint.endpoint_id):
                raise ValueError("endpoint_id must be 64 lowercase hex characters")
            seen: dict[str, dict[str, str]] = {}
            for address in endpoint.addresses:
                candidate = classify_address(address)
                if candidate is not None:
                    seen.setdefault(candidate["address"], candidate)
            # One canonical order, so the order a transport happens to list
            # its sockets in is not a change of address set.
            ordered = sorted(seen.values(), key=lambda item: (_KIND_ORDER[item["kind"]], item["address"]))
            candidates = tuple(ordered[:_MAX_ADDRESSES])
        with self._lock:
            previous_id = self._endpoint.endpoint_id if self._endpoint is not None else None
            current_id = endpoint.endpoint_id if endpoint is not None else None
            if previous_id != current_id:
                self._admissions.clear()
            if previous_id != current_id or candidates != self._candidates:
                self._revision += 1
            self._endpoint = endpoint
            self._candidates = candidates
            self._unavailable_reason = reason

    def offer(
        self,
        *,
        instance_id: str,
        hostname: str,
        subject: str,
        client_endpoint_id: str,
    ) -> dict[str, Any]:
        """Describe the endpoint to one authenticated session and admit its client."""

        now = int(self._clock())
        base = {"version": RENDEZVOUS_VERSION, "instance_id": instance_id, "hostname": hostname}
        with self._lock:
            endpoint = self._endpoint
            if endpoint is None or not self._candidates:
                reason = self._unavailable_reason if endpoint is None else "no_reachable_address"
                return unavailable(instance_id=instance_id, hostname=hostname, reason=reason)
            expires_at = now + RENDEZVOUS_TTL_SECONDS
            notifications = self._admit(client_endpoint_id, subject, expires_at, now)
            listener = self._admission_listener
            response = {
                **base,
                "status": "ready",
                "generation": f"{self._boot}:{self._revision}",
                "issued_at": now,
                "expires_at": expires_at,
                "alpn": DIRECT_ALPN,
                "endpoint": {
                    "id": endpoint.endpoint_id,
                    "addresses": [dict(candidate) for candidate in self._candidates],
                    "relay": None,
                },
                "client": {"endpoint_id": client_endpoint_id, "admitted_until": expires_at},
            }
        if listener is not None:
            for endpoint_id, ttl_seconds in notifications:
                listener(endpoint_id, ttl_seconds)
        return response

    def is_admitted(self, client_endpoint_id: str) -> bool:
        """Whether a transport peer with this endpoint ID may open streams now."""

        now = int(self._clock())
        with self._lock:
            admission = self._admissions.get(client_endpoint_id)
            return admission is not None and admission[1] > now

    def _admit(
        self,
        client_endpoint_id: str,
        subject: str,
        admitted_until: int,
        now: int,
    ) -> list[tuple[str, int]]:
        admissions = self._admissions
        notifications: list[tuple[str, int]] = []
        for endpoint_id in [key for key, (_subject, until) in admissions.items() if until <= now]:
            del admissions[endpoint_id]
            if endpoint_id != client_endpoint_id:
                notifications.append((endpoint_id, 0))
        admissions.pop(client_endpoint_id, None)
        own = [key for key, (owner, _until) in admissions.items() if owner == subject]
        for endpoint_id in own[: max(0, len(own) - _MAX_ADMISSIONS_PER_SUBJECT + 1)]:
            del admissions[endpoint_id]
            notifications.append((endpoint_id, 0))
        while len(admissions) >= _MAX_ADMISSIONS:
            endpoint_id = next(iter(admissions))
            del admissions[endpoint_id]
            notifications.append((endpoint_id, 0))
        admissions[client_endpoint_id] = (subject, admitted_until)
        notifications.append((client_endpoint_id, max(0, min(RENDEZVOUS_TTL_SECONDS, admitted_until - now))))
        return notifications

def unavailable(*, instance_id: str, hostname: str, reason: str) -> dict[str, Any]:
    """The answer when there is no endpoint to dial; the client stays on the tunnel."""

    return {
        "version": RENDEZVOUS_VERSION,
        "instance_id": instance_id,
        "hostname": hostname,
        "status": "unavailable",
        "reason": reason,
        "retry_after_seconds": UNAVAILABLE_RETRY_SECONDS,
    }


# The one rendezvous of this process. The Web UI server process owns both the
# transport and this state.
rendezvous = DirectRendezvous()
