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
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)
# Publication reasons may originate in sidecar messages. Never log an arbitrary string.
_LOG_REFUSALS = frozenset({
    "owner_required", "no_reachable_address", "not_running", "starting", "stopped",
    "setting_off", "unpaired", "permission_denied", "permission_required", "permission_expired", "certificate_not_ready",
    "transport_installing", "transport_install_failed", "transport_not_installed", "transport_start_failed", "state_changed", "sidecar_exited",
})

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
    # Set only for a connected relay selected by this generation's supervisor.
    relay_origin: str | None = None
    relay_policy: str | None = None


PUBLIC_TRIAL = "public-trial-v1"
PUBLIC_TRIAL_RELAYS = frozenset(f"https://{region}-1.relay.n0.iroh.link./" for region in ("use1", "usw1", "euc1", "aps1"))

AdmissionListener = Callable[[str, int, int], None]
# V2 observers refresh unchanged paths at least every five seconds.
_PATH_MAX_AGE = 10.0


@dataclass
class _Admission:
    subject: str
    until: int
    session: tuple[str, str] | None  # (pairing binding, authenticated browser session)
    epoch: int  # zero disables attribution, not transport admission
    connections: dict[int, tuple[float, dict | None]] = field(default_factory=dict)


class DirectRendezvous:
    """The published endpoint and the client endpoints admitted to dial it.

    The transport calls :meth:`publish` whenever its endpoint or addresses
    change and :meth:`is_admitted` when a peer connects. The Web UI calls
    :meth:`offer` for an authenticated remote session.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # Key by authenticated caller, never by its freely replaceable endpoint key.
        self._log_callers: OrderedDict[str, tuple[tuple, float, int]] = OrderedDict()
        # Distinguishes this process's generations from any earlier process's.
        self._boot = secrets.token_hex(8)
        self._revision = 0
        self._next_epoch = 1
        self._endpoint: DirectEndpoint | None = None
        self._candidates: tuple[dict[str, str], ...] = ()
        self._unavailable_reason = "not_running"
        # client endpoint ID -> (subject, admitted_until), oldest first.
        self._admissions: dict[str, _Admission] = {}
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
            previous_policy = self._endpoint.relay_policy if self._endpoint else None
            current_policy = endpoint.relay_policy if endpoint else None
            if current_policy not in (None, PUBLIC_TRIAL) or (endpoint and endpoint.relay_origin is not None and (current_policy != PUBLIC_TRIAL or endpoint.relay_origin not in PUBLIC_TRIAL_RELAYS)):
                raise ValueError("invalid relay publication")
            if previous_id != current_id or previous_policy != current_policy:
                if self._admission_listener:
                    for key in self._admissions:
                        self._admission_listener(key, 0, 0)
                self._admissions.clear()
            previous_origin = self._endpoint.relay_origin if self._endpoint else None
            current_origin = endpoint.relay_origin if endpoint else None
            if previous_id != current_id or candidates != self._candidates or previous_origin != current_origin or previous_policy != current_policy:
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
        is_owner: bool = False,
        session: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Describe the endpoint to one authenticated session and admit its client."""

        now = int(self._clock())
        base = {"version": RENDEZVOUS_VERSION, "instance_id": instance_id, "hostname": hostname}
        with self._lock:
            generation = f"{self._boot}:{self._revision}"
            response = self._offer_locked(base, generation, now, subject, client_endpoint_id, is_owner, session)
            summary = self._log_summary(response, generation)
            tick = time.monotonic()
            previous = self._log_callers.pop(subject, None)
            suppressed = previous[2] if previous else 0
            emit = previous is None or previous[0] != summary or tick - previous[1] >= 60
            self._log_callers[subject] = (summary, tick, 0) if emit else (summary, previous[1], suppressed + 1)
            if len(self._log_callers) > _MAX_ADMISSIONS:
                self._log_callers.popitem(last=False)
        if emit:
            logger.info(
                "direct rendezvous generation=%s public_trial_v1=%s lan=%d ipv6=%d outcome=%s reason=%s suppressed=%d",
                *summary, suppressed,
            )
        return response

    @staticmethod
    def _log_summary(response, generation):
        offered = response["status"] == "ready"
        endpoint_info = response.get("endpoint", {})
        addresses = endpoint_info.get("addresses", ())
        reason = response.get("reason")
        return (
            generation, "yes" if endpoint_info.get("relay") else "no",
            sum(c["kind"] == "lan" for c in addresses), sum(c["kind"] == "ipv6" for c in addresses),
            "offered" if offered else "refused", "none" if offered else reason if reason in _LOG_REFUSALS else "unavailable",
        )

    def _offer_locked(self, base, generation, now, subject, client_endpoint_id, is_owner, session):
        endpoint = self._endpoint
        relay = None
        if endpoint and endpoint.relay_policy == PUBLIC_TRIAL:
            if not is_owner:
                return unavailable(instance_id=base["instance_id"], hostname=base["hostname"], reason="owner_required")
            if endpoint.relay_origin is not None:
                relay = {"policy": PUBLIC_TRIAL, "url": endpoint.relay_origin}
        if endpoint is None or (not self._candidates and relay is None):
            reason = self._unavailable_reason if endpoint is None else "no_reachable_address"
            return unavailable(instance_id=base["instance_id"], hostname=base["hostname"], reason=reason)
        expires_at = now + RENDEZVOUS_TTL_SECONDS
        notifications = self._admit(client_endpoint_id, subject, expires_at, now, session)
        if self._admission_listener is not None:
            # Schedule on the loop in mutation order while holding the lock.
            for endpoint_id, ttl_seconds in notifications:
                grant = self._admissions.get(endpoint_id)
                self._admission_listener(endpoint_id, ttl_seconds, grant.epoch if ttl_seconds and grant else 0)
        return {
            **base, "status": "ready", "generation": generation, "issued_at": now, "expires_at": expires_at,
            "alpn": DIRECT_ALPN,
            "endpoint": {"id": endpoint.endpoint_id, "addresses": [dict(c) for c in self._candidates], "relay": relay},
            "client": {"endpoint_id": client_endpoint_id, "admitted_until": expires_at},
        }

    def is_admitted(self, client_endpoint_id: str) -> bool:
        """Whether a transport peer with this endpoint ID may open streams now."""

        now = int(self._clock())
        with self._lock:
            admission = self._admissions.get(client_endpoint_id)
            return admission is not None and admission.until > now

    def clear_paths(self) -> None:
        with self._lock:
            for admission in self._admissions.values():
                admission.connections.clear()

    def observe_path(self, peer: str, epoch: int, ordinal: int, phase: str, path: dict | None) -> None:
        with self._lock:
            admission = self._admissions.get(peer)
            if not admission or not admission.session or not epoch or admission.epoch != epoch or admission.until <= self._clock():
                return
            connections = admission.connections
            if phase == "close":
                connections.pop(ordinal, None)
            elif (phase == "open" and len(connections) < 32) or ordinal in connections:
                connections[ordinal] = (time.monotonic(), path)

    def session_paths(self, binding: str) -> dict[tuple[str, str], dict]:
        """Snapshot for the registry's already-visible rows, never a list of devices."""
        latest = {}
        with self._lock:
            now = self._clock()
            cutoff = time.monotonic() - _PATH_MAX_AGE
            for admission in self._admissions.values():
                session = admission.session
                if not session or session[0] != binding or admission.until <= now:
                    continue
                key = (admission.subject, session[1])
                for stamp, path in admission.connections.values():
                    if path and stamp > cutoff and (key not in latest or stamp > latest[key][0]):
                        latest[key] = (stamp, path)
        return {key: dict(value[1]) for key, value in latest.items()}

    def _admit(
        self,
        client_endpoint_id: str,
        subject: str,
        admitted_until: int,
        now: int,
        session: tuple[str, str] | None,
    ) -> list[tuple[str, int]]:
        admissions = self._admissions
        notifications: list[tuple[str, int]] = []
        for endpoint_id in [key for key, grant in admissions.items() if grant.until <= now]:
            del admissions[endpoint_id]
            if endpoint_id != client_endpoint_id:
                notifications.append((endpoint_id, 0))
        previous = admissions.pop(client_endpoint_id, None)
        own = [key for key, grant in admissions.items() if grant.subject == subject]
        for endpoint_id in own[: max(0, len(own) - _MAX_ADMISSIONS_PER_SUBJECT + 1)]:
            del admissions[endpoint_id]
            notifications.append((endpoint_id, 0))
        while len(admissions) >= _MAX_ADMISSIONS:
            endpoint_id = next(iter(admissions))
            del admissions[endpoint_id]
            notifications.append((endpoint_id, 0))
        if previous and previous.subject == subject and previous.session == session:
            # A renewal preserves the epoch, including zero for a conflict.
            previous.until = admitted_until
            admissions[client_endpoint_id] = previous
        else:
            # Only a fresh admission can start attribution. Replacing a live
            # session is ambiguous for the rest of this continuous admission.
            epoch = 0
            if previous is None and session:
                epoch = self._next_epoch
                self._next_epoch += 1
            admissions[client_endpoint_id] = _Admission(subject, admitted_until, session, epoch)
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
