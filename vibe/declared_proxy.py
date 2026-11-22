"""Two independent operator declarations. No header, DNS, or alleged client IP here.

Only the serving UI may use its environment to attest installation policy. A
notification sender must consume that attestation, not call listener_policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import ipaddress
import os
import re
from urllib.parse import urlsplit

from config.v2_config import config_recovery_refusal
from vibe import instance_address
from vibe.network_position import BindingState, binding_state

PEERS_ENV = 'VIBE_UI_TRUSTED_PROXY_IPS'
ORIGINS_ENV = 'VIBE_UI_TRUSTED_PUBLIC_ORIGINS'
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_ORIGIN_LABEL = re.compile(r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?')
_MAPPED = ipaddress.ip_network('::ffff:0:0/96')
_LOOPBACK = (ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'))


def origin_entries(config, environment):
    entries = getattr(getattr(config, 'ui', None), 'trusted_public_origins', ()) or ()
    if isinstance(entries, str):
        entries = entries.split(',')
    return tuple(s.strip() for s in (*entries, *environment.split(',')) if isinstance(s, str) and s.strip())


@lru_cache(maxsize=128)
def normalize_origin(value: str) -> str | None:
    """Canonical HTTP origin, not a URL prefix. Reject ambiguous authorities."""
    if not value or any(ord(ch) < 33 or ord(ch) == 127 or ch in '\\?#@' for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in ('http', 'https') or not host or parsed.path not in ('', '/'):
            return None
        if parsed.netloc.startswith('['):
            match = re.fullmatch(r'\[([^\]%]+)\](?::[0-9]+)?', parsed.netloc)
            if not match:
                return None
            host = f'[{ipaddress.IPv6Address(match[1])}]'
        else:
            if parsed.netloc.endswith(':'):
                return None
            host = host.removesuffix('.')
            if len(host) > 253 or not all(_ORIGIN_LABEL.fullmatch(label) for label in host.split('.')):
                return None
        if port == 0:
            return None
        default = 443 if parsed.scheme == 'https' else 80
        suffix = '' if port in (None, default) else f':{port}'
        return f'{parsed.scheme}://{host}{suffix}'
    except ValueError:
        return None


@lru_cache(maxsize=16)
def declared_peers(value: str) -> tuple[tuple[Network, bool], ...]:
    entries = set()
    for item in value.split(','):
        item = item.strip()
        try:
            if '%' in item:
                continue  # A zone is not a network declaration.
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        entries.add((network, '/' in item))
    return tuple(sorted(entries, key=lambda entry: (entry[0].version, entry[0], entry[1])))


def _subtract(networks, blocked):
    for network in networks:
        if network.version != blocked.version or not network.overlaps(blocked):
            yield network
        elif not network.subnet_of(blocked):
            yield from network.address_exclude(blocked)


@lru_cache(maxsize=32)
def _eligible_peers(entries, remote_on):
    # Classify the declarations that still contribute an address, before
    # collapsing their networks. A wholly excluded CIDR contributes nothing.
    blocked = (_MAPPED, *_LOOPBACK) if remote_on else (_MAPPED,)
    networks = []
    declarations = []
    for network, is_cidr in entries:
        parts = (network,)
        for block in blocked:
            parts = tuple(_subtract(parts, block))
        if parts:
            networks.extend(parts)
            declarations.append(is_cidr)
    effective = tuple(n for version in (4, 6) for n in ipaddress.collapse_addresses(x for x in networks if x.version == version))
    scope = 'range' if len(declarations) > 1 or any(declarations) else 'single_address'
    return effective, scope


@dataclass(frozen=True)
class Policy:
    origins: tuple[str, ...] = ()
    networks: tuple[Network, ...] = ()
    peer_scope: str = 'single_address'

    def projection(self):
        if not self.origins or not self.networks:
            return {}
        return {'unauthenticated_proxy_access': {'origins': list(self.origins), 'peer_scope': self.peer_scope}}

    def admits(self, peer, origin):
        if peer is None or origin not in self.origins:
            return False
        peer = getattr(peer, 'ipv4_mapped', None) or peer
        return any(peer in network for network in self.networks)


def forwarded_origin(headers, *, websocket=False) -> str | None:
    """One unambiguous forwarded authority. The transport peer is checked separately."""
    def single(name):
        getlist = getattr(headers, 'getlist', None)
        values = getlist(name) if getlist else ([headers[name]] if name in headers else [])
        if len(values) > 1 or (values and ',' in values[0]):
            raise ValueError('ambiguous authority')
        return values[0].strip() if values else None

    try:
        scheme = (single('x-forwarded-proto') or '').lower()
        if websocket:
            scheme = {'ws': 'http', 'wss': 'https'}.get(scheme, scheme)
        authority = single('x-forwarded-host')
        port = single('x-forwarded-port')
        if not authority or any(c in authority for c in '/\\?#@') or scheme not in ('http', 'https'):
            return None
        if headers.get('forwarded'):
            # We implement X-Forwarded-* authority only, not a second competing chain.
            return None
        if port is not None:
            if not re.fullmatch('[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
                return None
            explicit = urlsplit('//' + authority).port
            if explicit is not None and explicit != int(port):
                return None
            if explicit is None:
                authority += ':' + port
        return normalize_origin(f'{scheme}://{authority}')
    except ValueError:
        return None


def listener_policy(config) -> Policy | None:
    """None means unavailable (not an attestation of an empty policy)."""
    if binding_state(config) is BindingState.UNAVAILABLE or config_recovery_refusal(config, 'ui', 'remote_access'):
        return None
    entries = declared_peers(os.environ.get(PEERS_ENV, ''))
    cloud = config.remote_access.vibe_cloud
    networks, peer_scope = _eligible_peers(entries, cloud.enabled)
    if not networks:
        return Policy()
    origins = {origin for value in origin_entries(config, os.environ.get(ORIGINS_ENV, '')) if (origin := normalize_origin(value))}
    if not origins:
        return Policy()
    from vibe.remote_access import active_hostnames

    reserved = set(active_hostnames(config))
    if cloud.public_url:
        try:
            name = urlsplit(cloud.public_url).hostname
        except ValueError:
            return None
        if not name:
            return None
        reserved.add(instance_address.normalize_host(name))
    origins = tuple(sorted(origin for origin in origins if (host := urlsplit(origin).hostname) not in reserved
                           and not instance_address.is_managed_name(host)))
    if not origins:
        return Policy()
    return Policy(origins, networks, peer_scope)
