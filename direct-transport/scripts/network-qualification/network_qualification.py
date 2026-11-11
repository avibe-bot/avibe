#!/usr/bin/env python3
"""Network qualification of the avibe-direct-transport sidecar.

Runs as root INSIDE a disposable network namespace that
run-network-qualification.sh creates and always deletes. Standard library only;
the external tools are `ip` and `tcpdump`.

What it proves, from unfiltered captures of every interface in the namespace:

* the sidecar's only UDP egress goes to the admitted peer's sockets;
* its only TCP goes to the loopback stream listener, and that connection
  carries the `AVDT1 <token> <admitted peer id>` header;
* nothing else leaves: no DNS, no multicast discovery, no public service. Both
  address families have a default route into a sink namespace, so an attempt to
  reach any destination becomes a captured frame instead of a silent
  ENETUNREACH;
* `ready` and `addresses` list concrete, kernel-assigned addresses on ports the
  sidecar process really owns, and they follow interface address changes.

Topology (addresses are RFC 1918 and the 2001:db8::/32 documentation prefix):

    qualification namespace                      sink namespace (no process)
      lo     sidecar <-> peer UDP, sidecar -> listener TCP
      dtq0   10.203.7.2/24, 2001:db8:7::2/64  <-> dtqs0 10.203.7.1, 2001:db8:7::1
             default routes for both families       (no forwarding: a black hole)
      dtq1   synthetic addresses come and go   <-> dtqs1 (no address)

Two kinds of phase run in sequence, separated in each capture by canary frames:

* `addresses`: this script owns a sidecar with no peer, changes interface
  addresses, and checks the `addresses` messages. That sidecar must send no
  UDP or TCP at all.
* one phase per scenario: the Rust integration-test executable owns the
  sidecar, the stream listener, and the admitted iroh peer, steered by the
  AVDT_TEST_* seams. Its context file is the port oracle for the capture, and
  /proc confirms which process owns which UDP port.

The sidecar and the peer share the namespace because the sidecar only forwards
to a loopback listener that the test owns. Their traffic is therefore told apart
by UDP port, per address family, within one phase window; a UDP frame from a
port neither process owns is a violation, never an assumption.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# --- experiment constants -------------------------------------------------

IFACES = ("lo", "dtq0", "dtq1")
SINK_IFACES = {"dtq0": "dtqs0", "dtq1": "dtqs1"}
BASE4, GATEWAY4, PREFIX4 = "10.203.7.2", "10.203.7.1", 24
BASE6, GATEWAY6, PREFIX6 = "2001:db8:7::2", "2001:db8:7::1", 64
SYNTHETIC4, SYNTHETIC6 = "10.203.8.2", "2001:db8:8::2"
DEAD4, DEAD6 = "10.203.99.9", "2001:db8:99::9"

# name, AVDT_TEST_DIAL, family the stream must use, address added by the pre-dial hook
SCENARIOS = (
    ("dial-v4", (BASE4,), 4, None),
    ("dial-v6", (BASE6,), 6, None),
    ("dead-v4-live-v6", (DEAD4, BASE6), 6, None),
    ("live-v4-dead-v6", (BASE4, DEAD6), 4, None),
    ("live-added-v4", (SYNTHETIC4,), 4, SYNTHETIC4),
    ("live-added-v6", (SYNTHETIC6,), 6, SYNTHETIC6),
)

TEST_NAME = "admitted_stream_forwards_bytes_and_half_closes"
CANARY_ETHERTYPE = 0x88B5  # IEEE 802 Local Experimental Ethertype 1
CANARY_MAGIC = b"AVDT-QUALIFICATION-CANARY"
SOLICITED_NODE = ipaddress.ip_network("ff02::1:ff00:0/104")
SYN, ACK = 0x02, 0x10


class QualificationError(Exception):
    """The experiment could not be carried out as designed."""


class CaptureError(QualificationError):
    """A capture file is not a complete, untruncated record."""


class Violation(Exception):
    """A captured frame that the policy does not allow."""


# --- capture file ----------------------------------------------------------

_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "<",
    b"\xa1\xb2\xc3\xd4": ">",
    b"\x4d\x3c\xb2\xa1": "<",
    b"\xa1\xb2\x3c\x4d": ">",
}


def read_pcap(path: Path, strict: bool = True) -> list[bytes]:
    """Return the Ethernet frames of a classic pcap file.

    A strict read rejects anything that could hide traffic: a partial trailing
    record or a frame cut short by the snapshot length. A non-strict read is for
    polling a file tcpdump is still writing.
    """
    data = Path(path).read_bytes()
    if len(data) < 24:
        if strict:
            raise CaptureError(f"{path}: no pcap header")
        return []
    endian = _PCAP_MAGIC.get(data[:4])
    if endian is None:
        raise CaptureError(f"{path}: not a classic pcap file")
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    if linktype != 1:
        raise CaptureError(f"{path}: link type {linktype}, expected Ethernet")
    frames: list[bytes] = []
    offset = 24
    while offset < len(data):
        if offset + 16 > len(data):
            if strict:
                raise CaptureError(f"{path}: file ends inside a record header")
            break
        included, original = struct.unpack(endian + "II", data[offset + 8 : offset + 16])
        end = offset + 16 + included
        if end > len(data):
            if strict:
                raise CaptureError(f"{path}: file ends inside frame {len(frames)}")
            break
        if included != original:
            raise CaptureError(f"{path}: frame {len(frames)} holds {included} of {original} bytes")
        frames.append(data[offset + 16 : end])
        offset = end
    return frames


def canary_frame(mac: bytes, nonce: bytes, label: str) -> bytes:
    payload = CANARY_MAGIC + nonce + label.encode()
    header = b"\xff" * 6 + mac + struct.pack("!H", CANARY_ETHERTYPE)
    return header + payload.ljust(46, b"\0")


def canary_label(frame: bytes, nonce: bytes) -> str | None:
    """The label of one of this run's canaries, or None for any other frame."""
    if len(frame) < 14 or struct.unpack("!H", frame[12:14])[0] != CANARY_ETHERTYPE:
        return None
    marker = CANARY_MAGIC + nonce
    if not frame[14:].startswith(marker):
        return None
    return frame[14 + len(marker) :].rstrip(b"\0").decode("ascii", "replace")


# --- packet decoding -------------------------------------------------------


@dataclass
class Datagram:
    version: int
    src: ipaddress.IPv4Address | ipaddress.IPv6Address
    dst: ipaddress.IPv4Address | ipaddress.IPv6Address
    proto: int
    payload: bytes


def decode_ip(data: bytes, quoted: bool = False) -> Datagram:
    """Decode an IPv4 or IPv6 packet down to its upper-layer payload.

    `quoted` is for the packet an ICMP error carries, which may be cut short.
    Fragments are rejected: a later fragment has no ports to attribute.
    """
    if not data:
        raise Violation("empty IP packet")
    version = data[0] >> 4
    if version == 4:
        if len(data) < 20:
            raise Violation("short IPv4 header")
        header = (data[0] & 0x0F) * 4
        total = struct.unpack("!H", data[2:4])[0]
        if header < 20 or len(data) < header:
            raise Violation("bad IPv4 header length")
        if struct.unpack("!H", data[6:8])[0] & 0x3FFF:
            raise Violation("IPv4 fragment")
        if total and total <= len(data):
            data = data[:total]  # drop Ethernet padding
        elif total and not quoted:
            raise Violation("IPv4 packet longer than its frame")
        return Datagram(
            4,
            ipaddress.IPv4Address(data[12:16]),
            ipaddress.IPv4Address(data[16:20]),
            data[9],
            data[header:],
        )
    if version == 6:
        if len(data) < 40:
            raise Violation("short IPv6 header")
        length = struct.unpack("!H", data[4:6])[0]
        src, dst = ipaddress.IPv6Address(data[8:24]), ipaddress.IPv6Address(data[24:40])
        rest = data[40:]
        if length and length <= len(rest):
            rest = rest[:length]
        elif length and not quoted:
            raise Violation("IPv6 packet longer than its frame")
        proto = data[6]
        while proto in (0, 43, 44, 51, 60):  # extension headers
            if proto == 44:
                raise Violation("IPv6 fragment")
            if len(rest) < 8:
                raise Violation("short IPv6 extension header")
            size = (rest[1] + 2) * 4 if proto == 51 else (rest[1] + 1) * 8
            if len(rest) < size:
                raise Violation("short IPv6 extension header")
            proto, rest = rest[0], rest[size:]
        return Datagram(6, src, dst, proto, rest)
    raise Violation(f"IP version {version}")


# --- policy ----------------------------------------------------------------


@dataclass
class Oracle:
    """What one phase window is allowed to contain."""

    sidecar_udp: dict[int, set[int]] = field(default_factory=lambda: {4: set(), 6: set()})
    peer_udp: dict[int, set[int]] = field(default_factory=lambda: {4: set(), 6: set()})
    stream_addr: tuple | None = None
    dial: set[tuple] = field(default_factory=set)
    local_ips: set = field(default_factory=set)

    def is_local(self, ip) -> bool:
        return ip.is_loopback or ip in self.local_ips


@dataclass
class Link:
    own_mac: bytes
    sink_mac: bytes


@dataclass
class Segment:
    client_port: int
    to_listener: bool
    seq: int
    flags: int
    payload: bytes


def classify_frame(iface: str, frame: bytes, oracle: Oracle, link: Link | None):
    """Return (class name, TCP segment or None) or raise Violation."""
    if len(frame) < 14:
        raise Violation("short Ethernet frame")
    ethertype = struct.unpack("!H", frame[12:14])[0]
    if iface == "lo":
        direction = "lo"
    elif link is not None and frame[6:12] == link.own_mac:
        direction = "out"
    elif link is not None and frame[6:12] == link.sink_mac:
        direction = "in"
    else:
        raise Violation(f"frame from unknown MAC {frame[6:12].hex(':')}")
    if ethertype == 0x0806 and direction != "lo":
        body = frame[14:]
        if len(body) < 28 or body[6:8] not in (b"\x00\x01", b"\x00\x02"):
            raise Violation("malformed ARP")
        return f"arp {direction}", None
    if ethertype in (0x0800, 0x86DD):
        datagram = decode_ip(frame[14:])
        if datagram.version != (4 if ethertype == 0x0800 else 6):
            raise Violation("IP version does not match the Ethernet type")
        return classify_datagram(iface, direction, datagram, oracle)
    raise Violation(f"Ethernet type 0x{ethertype:04x}")


def classify_datagram(iface, direction, datagram, oracle, quoted=False):
    if datagram.proto == 17:
        return classify_udp(iface, direction, datagram, oracle), None
    if datagram.proto == 6:
        return classify_tcp(iface, datagram, oracle, quoted)
    is_icmp4 = datagram.version == 4 and datagram.proto == 1
    is_icmp6 = datagram.version == 6 and datagram.proto == 58
    if (is_icmp4 or is_icmp6) and len(datagram.payload) >= 8 and not quoted:
        kind = datagram.payload[0]
        if kind in ((3, 11, 12) if is_icmp4 else (1, 2, 3, 4)):
            # A kernel-generated error is allowed only for a flow that is itself
            # allowed, judged in the direction the quoted packet travelled.
            reverse = {"lo": "lo", "in": "out", "out": "in"}[direction]
            inner = decode_ip(datagram.payload[8:], quoted=True)
            name, _ = classify_datagram(iface, reverse, inner, oracle, quoted=True)
            return f"icmp error for [{name}]", None
        if is_icmp6 and kind in (133, 135, 136):  # router solicitation, neighbour discovery
            return f"neighbour discovery {direction}", None
        if is_icmp6 and kind in (131, 132, 143):
            # The kernel reports the solicited-node group of each address. Any
            # other group means a process joined one, as mDNS or SSDP would.
            for group in mld_groups(kind, datagram.payload[4:]):
                if group not in SOLICITED_NODE:
                    raise Violation(f"multicast membership for {group}")
            return f"multicast listener report {direction}", None
        raise Violation(f"ICMPv{datagram.version} type {kind}: {datagram.src} -> {datagram.dst}")
    raise Violation(f"IP protocol {datagram.proto}: {datagram.src} -> {datagram.dst}")


def mld_groups(kind: int, body: bytes) -> list[ipaddress.IPv6Address]:
    if kind != 143:
        if len(body) < 20:
            raise Violation("short MLD message")
        return [ipaddress.IPv6Address(body[4:20])]
    if len(body) < 4:
        raise Violation("short MLDv2 report")
    groups, offset = [], 4
    for _ in range(struct.unpack("!H", body[2:4])[0]):
        if len(body) < offset + 20:
            raise Violation("short MLDv2 record")
        aux, sources = body[offset + 1], struct.unpack("!H", body[offset + 2 : offset + 4])[0]
        groups.append(ipaddress.IPv6Address(body[offset + 4 : offset + 20]))
        offset += 20 + 16 * sources + 4 * aux
    return groups


def classify_udp(iface, direction, datagram, oracle) -> str:
    if len(datagram.payload) < 4:
        raise Violation("short UDP header")
    sport, dport = struct.unpack("!HH", datagram.payload[:4])
    family = datagram.version
    flow = f"{datagram.src} port {sport} -> {datagram.dst} port {dport} on {iface}"
    from_sidecar = sport in oracle.sidecar_udp[family]
    from_peer = sport in oracle.peer_udp[family]
    if from_sidecar and from_peer:
        raise Violation(f"UDP source port owned by both processes: {flow}")
    if from_sidecar:
        if (
            iface == "lo"
            and dport in oracle.peer_udp[family]
            and oracle.is_local(datagram.src)
            and oracle.is_local(datagram.dst)
        ):
            return f"udp{family} sidecar -> peer"
        raise Violation(f"sidecar UDP to a destination that is not the admitted peer: {flow}")
    if from_peer:
        if iface == "lo" and dport in oracle.sidecar_udp[family] and oracle.is_local(datagram.dst):
            return f"udp{family} peer -> sidecar"
        if direction == "out" and (datagram.dst, dport) in oracle.dial and not oracle.is_local(datagram.dst):
            return f"udp{family} peer -> dead candidate"
        raise Violation(f"peer UDP to an unexpected destination: {flow}")
    raise Violation(f"UDP from a port neither the sidecar nor the peer owns: {flow}")


def classify_tcp(iface, datagram, oracle, quoted):
    data = datagram.payload
    if len(data) < 4 or (len(data) < 20 and not quoted):
        raise Violation("short TCP header")
    sport, dport = struct.unpack("!HH", data[:4])
    seq = flags = 0
    payload = b""
    if len(data) >= 20:
        seq = struct.unpack("!I", data[4:8])[0]
        flags = data[13]
        payload = data[(data[12] >> 4) * 4 :]
    flow = f"{datagram.src} port {sport} -> {datagram.dst} port {dport} on {iface}"
    listener = oracle.stream_addr
    if iface != "lo" or listener is None:
        raise Violation(f"TCP outside the loopback stream: {flow}")
    if (datagram.dst, dport) == listener and datagram.src.is_loopback:
        return "tcp sidecar -> listener", Segment(sport, True, seq, flags, payload)
    opens = flags & SYN and not flags & ACK
    if (datagram.src, sport) == listener and datagram.dst.is_loopback and not opens:
        return "tcp listener -> sidecar", Segment(dport, False, seq, flags, payload)
    raise Violation(f"TCP that is not a connection to the stream listener: {flow}")


def reassemble(segments: list[Segment]) -> bytes:
    """Rebuild one direction of a TCP flow; a hole means the capture lost data."""
    opening = [s for s in segments if s.flags & SYN]
    if not opening:
        raise Violation("TCP flow whose opening segment was not captured")
    base = (opening[0].seq + 1) & 0xFFFFFFFF
    stream = bytearray()
    for offset, payload in sorted(((s.seq - base) & 0xFFFFFFFF, s.payload) for s in segments):
        if not payload:
            continue
        if offset > len(stream):
            raise Violation(f"TCP stream has a {offset - len(stream)}-byte hole")
        stream += payload[len(stream) - offset :]
    return bytes(stream)


@dataclass
class InterfaceAnalysis:
    counts: Counter = field(default_factory=Counter)  # (phase, class) -> frames
    violations: list[str] = field(default_factory=list)
    segments: dict[str, list[Segment]] = field(default_factory=dict)  # phase -> TCP segments


def analyze_interface(iface, frames, nonce, labels, oracles, link) -> InterfaceAnalysis:
    """Classify every frame of one capture against the oracle of its phase.

    `labels` is the canary sequence the run emitted, "start" first and then one
    "end:<phase>" per phase. Frames before "start" and after the last canary are
    judged against an empty oracle, so only kernel housekeeping passes there.
    """
    result = InterfaceAnalysis()
    seen = 0
    for index, frame in enumerate(frames):
        if len(frame) >= 14 and struct.unpack("!H", frame[12:14])[0] == CANARY_ETHERTYPE:
            label = canary_label(frame, nonce)
            if seen and label == labels[seen - 1]:
                continue  # the same canary delivered twice
            if seen < len(labels) and label == labels[seen]:
                seen += 1
            else:
                result.violations.append(f"{iface} frame {index}: unexpected canary {label!r}")
            continue
        if seen == 0:
            phase, oracle = "before start", Oracle()
        elif seen == len(labels):
            phase, oracle = "after end", Oracle()
        else:
            phase = labels[seen].removeprefix("end:")
            oracle = oracles.get(phase, Oracle())
        try:
            name, segment = classify_frame(iface, frame, oracle, link)
        except Violation as violation:
            result.violations.append(f"{iface} frame {index} [{phase}]: {violation}")
            continue
        result.counts[(phase, name)] += 1
        if segment is not None:
            result.segments.setdefault(phase, []).append(segment)
    if seen != len(labels):
        result.violations.append(
            f"{iface}: capture holds {seen} of {len(labels)} canaries; it stopped before {labels[seen]!r}"
        )
    return result


def check_stream(segments: list[Segment], header: bytes, app: bytes | None, instance: bytes | None) -> list[str]:
    """Check that the phase's loopback TCP is exactly one AVDT1 stream.

    The header names the token and the admitted peer, so it is the proof that
    the connection is the sidecar's stream and not just any loopback TCP. The
    payloads are compared only when the caller knows what the test sends.
    """
    clients = sorted({s.client_port for s in segments})
    if len(clients) != 1:
        return [f"expected one TCP connection to the stream listener, captured {len(clients)}"]
    try:
        sent = reassemble([s for s in segments if s.to_listener])
        received = reassemble([s for s in segments if not s.to_listener])
    except Violation as violation:
        return [str(violation)]
    problems = []
    if not sent.startswith(header):
        problems.append(f"sidecar -> listener bytes start {sent[:160]!r}, expected {header!r}")
    elif app is not None and sent != header + app:
        problems.append(f"sidecar -> listener payload is {sent[len(header) :]!r}, expected {app!r}")
    if instance is not None and received != instance:
        problems.append(f"listener -> sidecar bytes are {received!r}, expected {instance!r}")
    return problems


# --- address lists ---------------------------------------------------------


def parse_socket_address(text: str) -> tuple:
    """Parse `1.2.3.4:5` or `[2001:db8::1]:5` into (ip_address, port)."""
    match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)(?:%\w+)?\]:(\d{1,5})", text) or re.fullmatch(
        r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", text
    )
    if not match:
        raise ValueError(f"not a socket address: {text!r}")
    return ipaddress.ip_address(match.group(1)), int(match.group(2))


def concrete_problems(what: str, addresses, kernel_ips, owned_ports) -> list[str]:
    """Problems that stop an address list from being concrete and true."""
    if not isinstance(addresses, list) or not addresses:
        return [f"{what}: address list is empty or not a list: {addresses!r}"]
    problems = []
    if len(set(addresses)) != len(addresses):
        problems.append(f"{what}: duplicate addresses in {addresses}")
    for text in addresses:
        try:
            ip, port = parse_socket_address(text)
        except (TypeError, ValueError):
            problems.append(f"{what}: {text!r} is not a socket address")
            continue
        if ip.is_unspecified or ip.is_loopback or ip.is_multicast or port == 0:
            problems.append(f"{what}: {text} is not a concrete interface address")
        elif ip not in kernel_ips:
            problems.append(f"{what}: {text} is not assigned to any interface")
        if port not in owned_ports[ip.version]:
            problems.append(f"{what}: {text} uses a UDP port the sidecar does not own")
    return problems


def address_ips(addresses) -> set:
    return {parse_socket_address(text)[0] for text in addresses}


# --- kernel facts ----------------------------------------------------------


def run_tool(*command: str) -> str:
    done = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise QualificationError(f"{' '.join(command)} failed: {done.stderr.strip()}")
    return done.stdout


def kernel_ips() -> set:
    """Every address currently assigned in this namespace."""
    return {
        ipaddress.ip_address(info["local"])
        for iface in json.loads(run_tool("ip", "-j", "addr", "show"))
        for info in iface.get("addr_info", [])
    }


def link_mac(iface: str, netns: str | None = None) -> bytes:
    scope = ("-n", netns) if netns else ()
    link = json.loads(run_tool("ip", "-j", *scope, "link", "show", "dev", iface))[0]
    return bytes.fromhex(link["address"].replace(":", ""))


def owned_udp_ports(pid: int, exe: str | None = None) -> dict[int, set[int]] | None:
    """UDP ports bound by sockets that `pid` holds, per family, from /proc.

    Returns None when the process is gone or is not (yet) running `exe`.
    """
    try:
        if exe is not None and os.readlink(f"/proc/{pid}/exe") != exe:
            return None
        inodes = set()
        for descriptor in os.listdir(f"/proc/{pid}/fd"):
            match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(f"/proc/{pid}/fd/{descriptor}"))
            if match:
                inodes.add(int(match.group(1)))
        ports: dict[int, set[int]] = {4: set(), 6: set()}
        for family, table in ((4, "udp"), (6, "udp6")):
            for line in Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 9 and int(fields[9]) in inodes:
                    ports[family].add(int(fields[1].rsplit(":", 1)[1], 16))
        return ports
    except (OSError, ValueError):
        return None


def merge_ports(total: dict[int, set[int]], sample: dict[int, set[int]] | None) -> None:
    if sample:
        for family in (4, 6):
            total[family] |= sample[family]


# --- experiment ------------------------------------------------------------


class ControlledSidecar:
    """A sidecar whose control channel this script owns (no peer, no admission)."""

    def __init__(self, binary: str, identity: dict, stderr_path: Path):
        self.stderr = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, **identity
        )
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buffer = b""
        self.transcript: list[dict] = []

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def wait_for(self, kind: str, accept, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                self.transcript.append(message)
                if message.get("type") == "fatal":
                    raise QualificationError(f"sidecar reported fatal: {message}")
                if message.get("type") == kind and accept(message):
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationError(f"no matching {kind!r} message within {timeout:.0f} s")
            if select.select([self.proc.stdout], [], [], remaining)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise QualificationError(f"sidecar closed stdout while waiting for {kind!r}")
                self.buffer += chunk

    def close(self) -> int | None:
        """Stop the sidecar; return its exit status, or None if it had to be killed."""
        status = None
        try:
            if self.proc.poll() is None:
                self.send({"type": "stop"})
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            status = self.proc.returncode
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()
        self.stderr.close()
        return status


class Experiment:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.out).resolve()
        self.work = self.out / "work"
        self.sidecar = os.path.realpath(args.sidecar)
        self.peer_exe = os.path.realpath(args.peer_exe)
        self.identity: dict = {}
        if args.run_as:
            uid, gid = (int(part) for part in args.run_as.split(":"))
            self.identity = {"user": uid, "group": gid, "extra_groups": [gid]}
        self.nonce = secrets.token_bytes(16)
        self.labels: list[str] = []
        self.oracles: dict[str, Oracle] = {}
        self.expectations: dict[str, dict] = {}
        self.failures: list[str] = []
        self.report: dict = {"phases": {}, "captures": {}}
        self.captures: dict[str, subprocess.Popen] = {}
        self.capture_errors: dict[str, bytes] = {iface: b"" for iface in IFACES}
        self.links: dict[str, Link] = {}
        self.macs: dict[str, bytes] = {"lo": bytes(6)}

    def require(self, condition, message: str) -> bool:
        if not condition:
            self.failures.append(message)
        return bool(condition)

    # -- topology and capture

    def setup_topology(self) -> None:
        sink = self.args.sink_netns
        for scope in ("all", "default", "lo"):
            Path(f"/proc/sys/net/ipv6/conf/{scope}/disable_ipv6").write_text("0")
        run_tool("ip", "link", "set", "lo", "up")
        for iface, peer in SINK_IFACES.items():
            run_tool("ip", "link", "add", iface, "type", "veth", "peer", "name", peer, "netns", sink)
            Path(f"/proc/sys/net/ipv6/conf/{iface}/accept_ra").write_text("0")
            run_tool("ip", "-n", sink, "link", "set", peer, "up")
            run_tool("ip", "link", "set", iface, "up")
            self.macs[iface] = link_mac(iface)
            self.links[iface] = Link(self.macs[iface], link_mac(peer, sink))
        run_tool("ip", "-n", sink, "addr", "add", f"{GATEWAY4}/{PREFIX4}", "dev", "dtqs0")
        run_tool("ip", "-n", sink, "addr", "add", f"{GATEWAY6}/{PREFIX6}", "dev", "dtqs0", "nodad")
        run_tool("ip", "addr", "add", f"{BASE4}/{PREFIX4}", "dev", "dtq0")
        run_tool("ip", "addr", "add", f"{BASE6}/{PREFIX6}", "dev", "dtq0", "nodad")
        # Default routes turn any stray destination into a captured frame.
        run_tool("ip", "route", "add", "default", "via", GATEWAY4, "dev", "dtq0")
        run_tool("ip", "-6", "route", "add", "default", "via", GATEWAY6, "dev", "dtq0")
        expected = {ipaddress.ip_address(BASE4), ipaddress.ip_address(BASE6)}
        if not expected <= kernel_ips():
            raise QualificationError("base addresses were not assigned")

    def set_synthetic(self, action: str, address: str) -> None:
        ip = ipaddress.ip_address(address)
        prefix = PREFIX4 if ip.version == 4 else PREFIX6
        extra = ("nodad",) if ip.version == 6 and action == "add" else ()
        run_tool("ip", "addr", action, f"{address}/{prefix}", "dev", "dtq1", *extra)

    def drain_capture_errors(self) -> None:
        for iface, proc in self.captures.items():
            try:
                while chunk := os.read(proc.stderr.fileno(), 65536):
                    self.capture_errors[iface] += chunk
            except BlockingIOError:
                pass

    def start_captures(self) -> None:
        for iface in IFACES:
            # No capture filter: classification happens here, on every frame.
            # stderr is a pipe because tcpdump's AppArmor profile only lets it
            # write *.pcap paths; -Z root keeps the file writable in any directory.
            self.captures[iface] = subprocess.Popen(
                ["tcpdump", "-i", iface, "-n", "-U", "--immediate-mode", "-s", "262144",
                 "-B", "8192", "-Z", "root", "-w", str(self.out / f"{iface}.pcap")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )  # fmt: skip
            os.set_blocking(self.captures[iface].stderr.fileno(), False)
        deadline = time.monotonic() + 15
        while True:
            self.drain_capture_errors()
            if all(b"listening on" in self.capture_errors[iface] for iface in IFACES):
                break
            dead = [iface for iface, proc in self.captures.items() if proc.poll() is not None]
            if dead or time.monotonic() > deadline:
                raise CaptureError(f"tcpdump did not start listening: {self.capture_errors}")
            time.sleep(0.05)
        self.canary("start")

    def canary(self, label: str) -> None:
        """Mark a phase boundary in every capture and wait until it is on disk.

        The wait proves each capture is live at this point of the run, not just
        that tcpdump was started.
        """
        time.sleep(0.1)  # let the kernel finish what the phase's processes left queued
        for iface in IFACES:
            with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as packet_socket:
                packet_socket.bind((iface, 0))
                packet_socket.send(canary_frame(self.macs[iface], self.nonce, label))
        self.labels.append(label)
        deadline = time.monotonic() + 10
        waiting = set(self.captures)
        while waiting:
            for iface in sorted(waiting):
                frames = read_pcap(self.out / f"{iface}.pcap", strict=False)
                if any(canary_label(frame, self.nonce) == label for frame in frames):
                    waiting.discard(iface)
            if waiting and time.monotonic() > deadline:
                raise CaptureError(f"canary {label!r} never reached the capture of {waiting}")
            time.sleep(0.02)

    def stop_captures(self) -> None:
        for iface, proc in self.captures.items():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.failures.append(f"tcpdump on {iface} did not stop on SIGINT")
            os.set_blocking(proc.stderr.fileno(), True)
            self.capture_errors[iface] += proc.stderr.read()

    # -- phase: address changes on a sidecar this script owns

    def sample_ports(self, pid: int) -> dict[int, set[int]]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ports = owned_udp_ports(pid, self.sidecar)
            if ports and (ports[4] or ports[6]):
                return ports
            time.sleep(0.01)
        raise QualificationError("the sidecar process owns no UDP socket")

    def phase_addresses(self) -> None:
        phase = "addresses"
        oracle = self.oracles.setdefault(phase, Oracle())
        record = self.report["phases"].setdefault(phase, {"steps": []})
        sidecar = ControlledSidecar(self.sidecar, self.identity, self.out / "addresses.stderr.log")
        try:
            start = {
                "type": "start", "protocol": 1, "alpn": "avibe-direct/1",
                # No peer connects in this phase, so nothing dials this address.
                "stream_addr": "127.0.0.1:9", "token": secrets.token_hex(32),
                "max_connections": 64, "max_streams_per_connection": 64,
            }  # fmt: skip
            if self.args.start_json:
                start = json.loads(Path(self.args.start_json).read_text())
            sidecar.send(start)

            def settle(step: str, addresses) -> set:
                ports = self.sample_ports(sidecar.proc.pid)
                merge_ports(oracle.sidecar_udp, ports)
                record["steps"].append({"step": step, "addresses": addresses})
                self.failures += concrete_problems(f"{phase}/{step}", addresses, kernel_ips(), ports)
                try:
                    return address_ips(addresses)
                except (TypeError, ValueError):
                    return set()

            ready = sidecar.wait_for("ready", lambda _: True, 15)
            self.require(ready.get("protocol") == 1, f"ready protocol is {ready.get('protocol')!r}")
            self.require(
                re.fullmatch(r"[0-9a-f]{64}", str(ready.get("endpoint_id"))),
                "ready endpoint_id is not 64 hex digits",
            )
            baseline = settle("ready", ready.get("addresses"))
            for address in (BASE4, BASE6):
                self.require(ipaddress.ip_address(address) in baseline, f"ready does not list {address}")
            for action, address in (
                ("add", SYNTHETIC4), ("add", SYNTHETIC6), ("del", SYNTHETIC4), ("del", SYNTHETIC6),
            ):  # fmt: skip
                ip = ipaddress.ip_address(address)
                present = action == "add"
                self.set_synthetic(action, address)
                message = sidecar.wait_for(
                    "addresses",
                    lambda m, ip=ip, present=present: (ip in address_ips(m["addresses"])) == present,
                    self.args.address_timeout,
                )
                current = settle(f"{action} {address}", message["addresses"])
            self.require(
                current == baseline,
                f"addresses after the changes are {sorted(map(str, current))}, "
                f"not the ready set {sorted(map(str, baseline))}",
            )
        except (QualificationError, KeyError, TypeError, ValueError) as error:
            # Keep going: the scenarios below do not depend on this phase.
            self.failures.append(f"{phase}: {type(error).__name__}: {error}")
        finally:
            status = sidecar.close()
            record["messages"] = sidecar.transcript
            self.require(status == 0, f"{phase}: sidecar exit status {status}")
            for address in (SYNTHETIC4, SYNTHETIC6):
                if ipaddress.ip_address(address) in kernel_ips():
                    self.set_synthetic("del", address)
            self.canary(f"end:{phase}")

    # -- phase: one scenario driven by the integration-test executable

    def scenario(self, name: str, dial: tuple, family: int, hook_address: str | None) -> None:
        oracle = self.oracles.setdefault(name, Oracle())
        record = self.report["phases"].setdefault(name, {"dial": list(dial)})
        directory = self.work / name
        directory.mkdir(parents=True)
        if self.identity:
            os.chown(directory, self.identity["user"], self.identity["group"])
        context_path, pid_path = directory / "context.json", directory / "sidecar.pid"
        wrapper = directory / "sidecar-wrapper.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"echo \"$$\" > '{pid_path}.tmp' && mv '{pid_path}.tmp' '{pid_path}'\n"
            f"exec \"$@\" 2>>'{directory}/sidecar.stderr.log'\n"
        )
        wrapper.chmod(0o755)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(directory),
            "RUST_BACKTRACE": "1", "AVDT_TEST_BINARY": self.sidecar,
            "AVDT_SIDECAR_WRAPPER": str(wrapper), "AVDT_TEST_CONTEXT": str(context_path),
            "AVDT_TEST_DIAL": ",".join(dial),
        }  # fmt: skip
        if hook_address:
            hook = directory / "pre-dial-hook.sh"
            hook.write_text(
                "#!/bin/sh\n"
                f": > '{directory}/hook.request'\n"
                "i=0\n"
                f"while [ ! -e '{directory}/hook.done' ]; do\n"
                '  i=$((i + 1)); [ "$i" -le 400 ] || exit 1\n'
                "  sleep 0.05\n"
                "done\n"
            )
            hook.chmod(0o755)
            env["AVDT_TEST_PRE_DIAL_HOOK"] = str(hook)

        sidecar_ports: dict[int, set[int]] = {4: set(), 6: set()}
        peer_ports: dict[int, set[int]] = {4: set(), 6: set()}
        oracle.local_ips |= kernel_ips()
        hook_done = False
        log_path = self.out / f"{name}.test.log"
        try:
            with open(log_path, "wb") as log:
                test = subprocess.Popen(
                    [self.peer_exe, "--exact", TEST_NAME, "--nocapture", "--test-threads=1"],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env=env, cwd=directory, **self.identity,
                )  # fmt: skip
                deadline = time.monotonic() + self.args.scenario_timeout
                while test.poll() is None:
                    merge_ports(peer_ports, owned_udp_ports(test.pid))
                    try:
                        sidecar_pid = int(pid_path.read_text())
                        merge_ports(sidecar_ports, owned_udp_ports(sidecar_pid, self.sidecar))
                    except (OSError, ValueError):
                        pass
                    if hook_address and not hook_done and (directory / "hook.request").exists():
                        self.set_synthetic("add", hook_address)
                        (directory / "hook.done").write_text("")
                        hook_done = True
                    if time.monotonic() > deadline:
                        test.kill()
                        test.wait()
                        self.failures.append(f"{name}: the test executable timed out")
                    time.sleep(0.002)
            oracle.local_ips |= kernel_ips()
            # `--exact` with a renamed test exits 0 having run nothing.
            self.require(
                test.returncode == 0 and "test result: ok. 1 passed" in log_path.read_text(errors="replace"),
                f"{name}: {TEST_NAME} did not pass (exit {test.returncode}); see {log_path}",
            )
            self.require(
                hook_done or not hook_address,
                f"{name}: the test never ran AVDT_TEST_PRE_DIAL_HOOK",
            )
            context = json.loads(context_path.read_text())
            record["context"] = context
            self.apply_context(name, context, oracle, sidecar_ports, peer_ports, dial, family, hook_address)
        except (QualificationError, OSError, KeyError, TypeError, ValueError) as error:
            # One broken scenario must not hide the results of the others.
            self.failures.append(f"{name}: {type(error).__name__}: {error}")
        finally:
            oracle.sidecar_udp, oracle.peer_udp = sidecar_ports, peer_ports
            record["sidecar_udp_ports"] = {f: sorted(p) for f, p in sidecar_ports.items()}
            record["peer_udp_ports"] = {f: sorted(p) for f, p in peer_ports.items()}
            if hook_done:
                self.set_synthetic("del", hook_address)
            self.canary(f"end:{name}")

    def apply_context(self, name, context, oracle, sidecar_ports, peer_ports, dial, family, hook_address):
        """Check the test's context file against the kernel and build the oracle."""
        self.require(
            sidecar_ports[4] or sidecar_ports[6],
            f"{name}: /proc never showed a UDP socket owned by the sidecar process",
        )
        for value in sidecar_ports[4] & peer_ports[4], sidecar_ports[6] & peer_ports[6]:
            self.require(not value, f"{name}: sidecar and peer both own UDP port {sorted(value)}")
        # The peer's ports: what the test reports must be what its process owns.
        for text in context["client_bound_sockets"]:
            ip, port = parse_socket_address(text)
            self.require(
                port in peer_ports[ip.version],
                f"{name}: /proc does not show the test process owning UDP port {port} ({text})",
            )
        stream_ip, stream_port = parse_socket_address(context["stream_addr"])
        self.require(stream_ip.is_loopback, f"{name}: stream_addr {stream_ip} is not loopback")
        oracle.stream_addr = (stream_ip, stream_port)

        reported = context["ready"]["addresses"]
        self.failures += concrete_problems(f"{name}/ready", reported, oracle.local_ips, sidecar_ports)
        if hook_address:
            added = ipaddress.ip_address(hook_address)
            self.require(
                added not in address_ips(reported),
                f"{name}: ready already listed {hook_address}, so the hook proved nothing",
            )
            reported = context.get("addresses")
            if isinstance(reported, dict):  # the whole `addresses` message
                reported = reported.get("addresses")
            self.failures += concrete_problems(f"{name}/addresses", reported, oracle.local_ips, sidecar_ports)
            self.require(
                reported and added in address_ips(reported),
                f"{name}: the addresses message after the hook does not list {hook_address}",
            )
        else:
            for address in (SYNTHETIC4, SYNTHETIC6):
                self.require(
                    ipaddress.ip_address(address) not in address_ips(reported),
                    f"{name}: ready lists the removed address {address}",
                )
        oracle.dial = {parse_socket_address(text) for text in context["dial"]}
        self.require(
            {ip for ip, _ in oracle.dial} == {ipaddress.ip_address(text) for text in dial},
            f"{name}: the test dialled {context['dial']}, not {list(dial)}",
        )
        for ip, port in oracle.dial:
            self.require(
                port in sidecar_ports[ip.version],
                f"{name}: dial target {ip} port {port} is not a sidecar UDP port",
            )
        header = f"AVDT1 {context['token']} {context['client_endpoint_id']}\n".encode()
        self.expectations[name] = {"family": family, "header": header}

    # -- analysis

    def analyze(self) -> None:
        segments: dict[str, list[Segment]] = {}
        counts: Counter = Counter()
        for iface in IFACES:
            errors = self.capture_errors[iface].decode(errors="replace")
            captured = re.search(r"(\d+) packets? captured", errors)
            dropped = re.search(r"(\d+) packets? dropped by kernel", errors)
            capture = self.report["captures"].setdefault(iface, {"tcpdump": errors.splitlines()})
            try:
                frames = read_pcap(self.out / f"{iface}.pcap")
            except (CaptureError, OSError) as error:
                self.failures.append(str(error))
                continue
            capture["frames"] = len(frames)
            self.require(
                captured and int(captured.group(1)) == len(frames),
                f"{iface}: the file holds {len(frames)} frames but tcpdump reports "
                f"{captured.group(1) if captured else 'no count'}",
            )
            self.require(
                dropped and int(dropped.group(1)) == 0,
                f"{iface}: kernel dropped {dropped.group(1) if dropped else 'an unknown number of'} packets",
            )
            analysis = analyze_interface(iface, frames, self.nonce, self.labels, self.oracles, self.links.get(iface))
            self.failures += analysis.violations
            for (phase, name), count in sorted(analysis.counts.items()):
                self.report["phases"].setdefault(phase, {}).setdefault("frames", {})[f"{iface}: {name}"] = count
                counts[(phase, name)] += count
            for phase, found in analysis.segments.items():
                segments.setdefault(phase, []).extend(found)
        for phase, expected in self.expectations.items():
            family = expected["family"]
            for direction in ("sidecar -> peer", "peer -> sidecar"):
                self.require(
                    counts[(phase, f"udp{family} {direction}")],
                    f"{phase}: no IPv{family} {direction} UDP frame was captured",
                )
            self.failures += [
                f"{phase}: {problem}"
                for problem in check_stream(
                    segments.get(phase, []), expected["header"],
                    self.args.app_payload and self.args.app_payload.encode(),
                    self.args.instance_payload and self.args.instance_payload.encode(),
                )
            ]  # fmt: skip

    def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        self.work.mkdir()  # a stale context file from an earlier run must never be read
        os.chmod(self.out, 0o755)
        os.chmod(self.work, 0o755)
        wanted = set(self.args.scenarios.split(",")) if self.args.scenarios else None
        try:
            versions = subprocess.run(["tcpdump", "--version"], capture_output=True, text=True)
            self.report["environment"] = {
                "kernel": os.uname().release,
                "machine": os.uname().machine,
                "tcpdump": (versions.stdout + versions.stderr).splitlines()[:2],
                "sidecar": self.sidecar,
                "peer_exe": self.peer_exe,
            }
            self.setup_topology()
            self.start_captures()
            self.phase_addresses()
            for scenario in SCENARIOS:
                if wanted is None or scenario[0] in wanted:
                    self.scenario(*scenario)
        except (QualificationError, OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
            self.failures.append(f"{type(error).__name__}: {error}")
        finally:
            self.stop_captures()
        if self.labels:
            self.analyze()
        self.report["failures"] = self.failures
        self.report["canaries"] = self.labels
        (self.out / "report.json").write_text(json.dumps(self.report, indent=2, default=str) + "\n")
        for phase, details in self.report["phases"].items():
            print(f"[{phase}]")
            for name, count in details.get("frames", {}).items():
                print(f"  {count:6d}  {name}")
        for failure in self.failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        print(f"network qualification {'FAILED' if self.failures else 'passed'}: {self.out}/report.json")
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sidecar", required=True, help="avibe-direct-transport binary to qualify")
    parser.add_argument("--peer-exe", required=True, help="tests/transport.rs test executable")
    parser.add_argument("--sink-netns", required=True, help="name of the empty sink namespace")
    parser.add_argument("--out", required=True, help="directory for captures, logs and report.json")
    parser.add_argument("--run-as", help="UID:GID for the sidecar and the test executable")
    parser.add_argument("--scenarios", help="comma-separated subset of scenario names")
    parser.add_argument("--start-json", help="file holding a replacement `start` command")
    parser.add_argument("--app-payload", help="exact bytes the test's peer sends, if known")
    parser.add_argument("--instance-payload", help="exact bytes the test's listener answers")
    parser.add_argument("--address-timeout", type=float, default=20)
    parser.add_argument("--scenario-timeout", type=float, default=90)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("run this through run-network-qualification.sh on a disposable Linux host")
    if os.environ.get("DTQ_INSIDE_NETNS") != "1":
        parser.error("refusing to change interfaces outside the wrapper's namespace")

    def interrupt(signum, _frame):
        raise QualificationError(f"interrupted by signal {signum}")

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupt)
    return Experiment(args).run()


if __name__ == "__main__":
    sys.exit(main())
