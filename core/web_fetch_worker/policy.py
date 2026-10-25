"""Mandatory public-address policy shared by all web_fetch candidates.

DNS is synchronous only inside the disposable fetch worker, never on the
controller loop. A CheckedTarget is a complete answer-set snapshot, not a
permission to resolve the hostname again at connect time.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import idna


class FetchFailure(Exception):
    def __init__(self, code: str, *, terminal: bool = False, retry_after: str | None = None, cooldown_suppressed: bool = False):
        super().__init__(code)
        self.code = code
        self.terminal = terminal
        self.retry_after = retry_after
        self.cooldown_suppressed = cooldown_suppressed


# Deliberately explicit: ipaddress.is_global has changed across Python releases.
_V4_DENY = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "168.63.129.16/32",  # Azure platform virtual IP (metadata/WireServer).
))
_V6_GLOBAL = ipaddress.ip_network("2000::/3")
_V6_DENY = tuple(ipaddress.ip_network(net) for net in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20",
))
_DNS64 = ipaddress.ip_network("64:ff9b::/96")
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".home.arpa", ".invalid", ".test")


def checked_ip(value: str) -> str:
    """Return a canonical permitted numeric pin; mapped IPv6 becomes IPv4."""
    try:
        if "%" in value:
            raise ValueError
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise FetchFailure("fetch_dns_unverified", terminal=True) from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _FAKE_IP:
            raise FetchFailure("fetch_dns_unverified", terminal=True)
        allowed = not any(ip in net for net in _V4_DENY)
    elif ip in _DNS64:
        checked_ip(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        allowed = True
    else:
        # ISATAP's interface identifier embeds an IPv4 next hop under an
        # otherwise public IPv6 prefix; it must not bypass the transition ban.
        is_isatap = ((int(ip) >> 32) & 0xFFFFFFFF) in {0x00005EFE, 0x02005EFE}
        allowed = ip in _V6_GLOBAL and not is_isatap and not any(ip in net for net in _V6_DENY)
    if not allowed:
        raise FetchFailure("fetch_url_blocked", terminal=True)
    return str(ip)


@dataclass(frozen=True)
class FetchURL:
    url: str
    host: str
    port: int
    scheme: str
    literal: bool

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


@dataclass(frozen=True)
class CheckedTarget:
    address: FetchURL
    ips: tuple[str, ...]


def parse_url(value: str) -> FetchURL:
    """Strict UTS #46 host normalization; never reinterpret ambiguous numbers."""
    if (not isinstance(value, str) or not value or len(value) > 8192 or "\\" in value
            or any(c.isspace() or unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)):
        raise FetchFailure("fetch_url_blocked", terminal=True)
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname
        if not host or "%" in host or parts.netloc.endswith(":"):
            raise ValueError
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        if not 1 <= port <= 65535 or parts.port == 0:
            raise ValueError
        # One DNS root dot is canonical; repeated dots remain invalid.
        host = host[:-1] if host.endswith(".") else host
        if host.endswith("."):
            raise ValueError
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            host = checked_ip(str(literal))
        else:
            host = idna.encode(host, uts46=True, transitional=False, std3_rules=True).decode("ascii").lower()
            host = host[:-1] if host.endswith(".") else host
            if ("." not in host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES)
                    or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host)
                    or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1])):
                raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            authority += f":{port}"
        # Leave existing percent escapes and path/query separators untouched.
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        url = urlunsplit((parts.scheme.lower(), authority, path, query, ""))
        if len(url) > 8192:
            raise ValueError
        return FetchURL(url, host, port, parts.scheme.lower(), literal is not None)
    except (ValueError, UnicodeError, idna.IDNAError):
        raise FetchFailure("fetch_url_blocked", terminal=True) from None


def resolve_target(value: str) -> CheckedTarget:
    address = parse_url(value)
    if address.literal:
        return CheckedTarget(address, (checked_ip(address.host),))
    try:
        answers = socket.getaddrinfo(address.host, address.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise FetchFailure("fetch_dns_unverified", terminal=True) from None
    if not answers or len(answers) > 64:
        raise FetchFailure("fetch_dns_unverified", terminal=True)
    # Any bad answer rejects the entire set, even when a good address came first.
    ips = tuple(dict.fromkeys(checked_ip(answer[4][0]) for answer in answers))
    return CheckedTarget(address, ips)


def metadata_url(value: object, base: str | None = None) -> str | None:
    """Provider/page-claimed metadata is syntax-checked, never DNS-verified."""
    from urllib.parse import urljoin

    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_url(urljoin(base, value) if base else value).url
    except FetchFailure:
        return None
