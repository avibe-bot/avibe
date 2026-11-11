#!/usr/bin/env python3
"""Offline checks of the capture reader and policy in network_qualification.py.

Unprivileged and network-free: it builds pcap bytes in memory. Run with
`python3 test_capture_policy.py` (also collectable by pytest).

Contract protected: a capture that contains anything other than sidecar <-> peer
UDP, the AVDT1 loopback stream, and kernel neighbour housekeeping is rejected,
and so is a capture that is cut short. The live run on CI cannot show that the
policy rejects bad traffic, because a healthy sidecar produces none.
"""

from __future__ import annotations

import ipaddress
import struct
import tempfile
import unittest
from pathlib import Path

import network_qualification as nq

NONCE = bytes(range(16))
OWN, SINK = bytes.fromhex("02aa00000001"), bytes.fromhex("02aa00000002")
LINK = nq.Link(OWN, SINK)
LABELS = ["start", "end:dial"]
SIDECAR4, SIDECAR6, PEER4, PEER6, LISTENER = 40004, 40006, 50004, 50006, 45000
SCRATCH = tempfile.TemporaryDirectory()  # removed when the interpreter exits
HEADER = b"AVDT1 " + b"ab" * 32 + b" " + b"cd" * 32 + b"\n"


def ip(text):
    return ipaddress.ip_address(text)


def oracle() -> nq.Oracle:
    return nq.Oracle(
        sidecar_udp={4: {SIDECAR4}, 6: {SIDECAR6}},
        peer_udp={4: {PEER4}, 6: {PEER6}},
        stream_addr=(ip("127.0.0.1"), LISTENER),
        dial={(ip(nq.DEAD4), SIDECAR4), (ip(nq.BASE6), SIDECAR6)},
        local_ips={ip(nq.BASE4), ip(nq.BASE6)},
    )


def ether(src, dst, ethertype, body):
    return dst + src + struct.pack("!H", ethertype) + body


def ipv4(src, dst, proto, body, flags=0):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 0, flags, 64, proto, 0,
                         ip(src).packed, ip(dst).packed)  # fmt: skip
    return header + body


def ipv6(src, dst, proto, body, hop_by_hop=False):
    if hop_by_hop:  # router alert option, as MLD reports carry
        body = bytes([proto, 0, 5, 2, 0, 0, 1, 0]) + body
        proto = 0
    return struct.pack("!IHBB16s16s", 6 << 28, len(body), proto, 64, ip(src).packed, ip(dst).packed) + body


def udp(sport, dport, body=b"quic"):
    return struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body


def tcp(sport, dport, seq, flags, body=b""):
    return struct.pack("!HHIIBBHHH", sport, dport, seq, 0, 5 << 4, flags, 65535, 0, 0) + body


def lo4(body, proto=17, src=nq.BASE4, dst=nq.BASE4):
    return ether(bytes(6), bytes(6), 0x0800, ipv4(src, dst, proto, body))


def loopback_stream(port=LISTENER, payload=HEADER + b"from-app", lose_data=False):
    client = 33333
    frames = [
        lo4(tcp(client, port, 1000, nq.SYN), 6, "127.0.0.1", "127.0.0.1"),
        lo4(tcp(port, client, 5000, nq.SYN | nq.ACK), 6, "127.0.0.1", "127.0.0.1"),
        lo4(tcp(client, port, 1001, nq.ACK, payload[:10]), 6, "127.0.0.1", "127.0.0.1"),
        lo4(tcp(client, port, 1011, nq.ACK, payload[10:]), 6, "127.0.0.1", "127.0.0.1"),
        lo4(tcp(client, port, 1011, nq.ACK, payload[10:]), 6, "127.0.0.1", "127.0.0.1"),  # retransmit
        lo4(tcp(port, client, 5001, nq.ACK, b"from-instance"), 6, "127.0.0.1", "127.0.0.1"),
    ]
    if lose_data:
        del frames[2]
    return frames


def canary(label, mac=bytes(6)):
    return nq.canary_frame(mac, NONCE, label)


def good_lo():
    return [
        canary("start"),
        lo4(udp(PEER4, SIDECAR4)),
        lo4(udp(SIDECAR4, PEER4)),
        ether(bytes(6), bytes(6), 0x86DD, ipv6(nq.BASE6, nq.BASE6, 17, udp(SIDECAR6, PEER6))),
        *loopback_stream(),
        canary("end:dial"),
    ]


def good_veth():
    mld = bytes([143, 0, 0, 0, 0, 0, 0, 1, 4, 0, 0, 0]) + ip("ff02::1:ff00:2").packed
    return [
        ether(OWN, b"\xff" * 6, 0x0806, bytes([0, 1, 8, 0, 6, 4, 0, 1]) + bytes(20)),
        canary("start", OWN),
        ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, nq.DEAD4, 17, udp(PEER4, SIDECAR4))),
        ether(OWN, SINK, 0x86DD, ipv6("fe80::1", "ff02::16", 58, mld, hop_by_hop=True)),
        ether(SINK, OWN, 0x86DD, ipv6("fe80::2", nq.BASE6, 58, bytes([136, 0, 0, 0]) + bytes(20))),
        canary("end:dial", OWN),
    ]


def analyze(iface, frames):
    link = None if iface == "lo" else LINK
    return nq.analyze_interface(iface, frames, NONCE, LABELS, {"dial": oracle()}, link)


def write_pcap(frames, snaplen_cut=None, tail=b""):
    data = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
    for index, frame in enumerate(frames):
        stored = frame[:snaplen_cut] if snaplen_cut and index == 1 else frame
        data += struct.pack("<IIII", 1, index, len(stored), len(frame)) + stored
    path = Path(SCRATCH.name) / f"{len(list(Path(SCRATCH.name).iterdir()))}.pcap"
    path.write_bytes(data + tail)
    return path


class CaptureFileTest(unittest.TestCase):
    def test_complete_file_round_trips(self):
        self.assertEqual(nq.read_pcap(write_pcap(good_lo())), good_lo())

    def test_partial_trailing_record_is_rejected_only_when_strict(self):
        path = write_pcap(good_lo(), tail=b"\x01\x00\x00")
        with self.assertRaisesRegex(nq.CaptureError, "record header"):
            nq.read_pcap(path)
        self.assertEqual(len(nq.read_pcap(path, strict=False)), len(good_lo()))

    def test_frame_cut_by_snapshot_length_is_rejected(self):
        with self.assertRaisesRegex(nq.CaptureError, "frame 1 holds 20 of"):
            nq.read_pcap(write_pcap(good_lo(), snaplen_cut=20))


class PolicyTest(unittest.TestCase):
    def assert_violation(self, iface, frames, pattern):
        violations = analyze(iface, frames).violations
        self.assertEqual(len(violations), 1, violations)
        self.assertRegex(violations[0], pattern)

    def test_allowed_traffic_passes_and_is_attributed(self):
        lo, veth = analyze("lo", good_lo()), analyze("dtq0", good_veth())
        self.assertEqual(lo.violations + veth.violations, [])
        self.assertEqual(lo.counts[("dial", "udp4 sidecar -> peer")], 1)
        self.assertEqual(lo.counts[("dial", "udp6 sidecar -> peer")], 1)
        self.assertEqual(veth.counts[("dial", "udp4 peer -> dead candidate")], 1)
        self.assertEqual(veth.counts[("before start", "arp out")], 1)
        self.assertEqual(nq.check_stream(lo.segments["dial"], HEADER, b"from-app", b"from-instance"), [])

    def test_stream_proof_rejects_wrong_header_and_lost_data(self):
        wrong = analyze("lo", [canary("start"), *loopback_stream(payload=b"GET / HTTP/1.1\r\n"), canary("end:dial")])
        self.assertRegex(nq.check_stream(wrong.segments["dial"], HEADER, None, None)[0], "expected b'AVDT1")
        holed = analyze("lo", [canary("start"), *loopback_stream(lose_data=True), canary("end:dial")])
        self.assertRegex(nq.check_stream(holed.segments["dial"], HEADER, None, None)[0], "10-byte hole")
        self.assertRegex(nq.check_stream([], HEADER, None, None)[0], "captured 0")

    def test_violations(self):
        dns = ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, "8.8.8.8", 17, udp(41000, 53)))
        mdns = bytes([143, 0, 0, 0, 0, 0, 0, 1, 4, 0, 0, 0]) + ip("ff02::fb").packed
        icmp_about_dns = ether(SINK, OWN, 0x0800, ipv4(nq.GATEWAY4, nq.BASE4, 1, bytes([3, 3, 0, 0, 0, 0, 0, 0])
                               + ipv4(nq.BASE4, "8.8.8.8", 17, udp(41000, 53))))  # fmt: skip
        cases = [
            ("dtq0", dns, "neither the sidecar nor the peer"),
            ("dtq0", ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, "8.8.8.8", 17, udp(SIDECAR4, 443))),
             "sidecar UDP to a destination that is not the admitted peer"),
            ("dtq0", ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, "8.8.8.8", 17, udp(PEER4, SIDECAR4))),
             "peer UDP to an unexpected destination"),
            ("dtq0", ether(OWN, SINK, 0x86DD, ipv6("fe80::1", "ff02::16", 58, mdns, hop_by_hop=True)),
             "multicast membership for ff02::fb"),
            ("dtq0", ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, "224.0.0.22", 2, bytes(8))), "IP protocol 2"),
            ("dtq0", ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, "1.1.1.1", 6, tcp(40000, 443, 1, nq.SYN))),
             "TCP outside the loopback stream"),
            ("dtq0", ether(OWN, SINK, 0x0800, ipv4(nq.BASE4, nq.DEAD4, 17, udp(PEER4, SIDECAR4), flags=0x2000)),
             "IPv4 fragment"),
            ("dtq0", ether(bytes.fromhex("02bb00000009"), OWN, 0x0806, bytes(28)), "unknown MAC"),
            ("dtq0", icmp_about_dns, "neither the sidecar nor the peer"),
            ("lo", lo4(tcp(33334, 8080, 1, nq.SYN), 6, "127.0.0.1", "127.0.0.1"),
             "not a connection to the stream listener"),
            ("lo", lo4(udp(SIDECAR4, 53), src="127.0.0.1", dst="127.0.0.53"),
             "sidecar UDP to a destination that is not the admitted peer"),
            ("lo", lo4(udp(SIDECAR4, PEER4), dst="10.9.9.9"), "not the admitted peer"),
            ("lo", canary("end:other"), "unexpected canary 'end:other'"),
        ]  # fmt: skip
        for iface, frame, pattern in cases:
            with self.subTest(pattern=pattern, iface=iface):
                frames = good_lo() if iface == "lo" else good_veth()
                frames.insert(2, frame)
                self.assert_violation(iface, frames, pattern)

    def test_traffic_outside_its_phase_window_is_rejected(self):
        frames = good_lo()
        frames.append(lo4(udp(SIDECAR4, PEER4)))  # after the phase's closing canary
        self.assert_violation("lo", frames, r"\[after end\].*neither the sidecar nor the peer")

    def test_capture_that_stops_early_is_rejected(self):
        self.assert_violation("lo", good_lo()[:-1], "holds 1 of 2 canaries; it stopped before 'end:dial'")

    def test_duplicated_canary_is_tolerated(self):
        frames = good_lo()
        frames.insert(1, canary("start"))
        self.assertEqual(analyze("lo", frames).violations, [])

    def test_kernel_error_for_an_allowed_flow_passes(self):
        quoted = ipv4(nq.BASE4, nq.BASE4, 17, udp(SIDECAR4, PEER4))
        frames = good_lo()
        frames.insert(2, lo4(bytes([3, 3, 0, 0, 0, 0, 0, 0]) + quoted, proto=1))
        result = analyze("lo", frames)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.counts[("dial", "icmp error for [udp4 sidecar -> peer]")], 1)


class AddressListTest(unittest.TestCase):
    PORTS = {4: {SIDECAR4}, 6: {SIDECAR6}}
    KERNEL = {ip(nq.BASE4), ip(nq.BASE6)}

    def test_concrete_list_passes(self):
        addresses = [f"{nq.BASE4}:{SIDECAR4}", f"[{nq.BASE6}]:{SIDECAR6}"]
        self.assertEqual(nq.concrete_problems("ready", addresses, self.KERNEL, self.PORTS), [])
        self.assertEqual(nq.address_ips(addresses), self.KERNEL)

    def test_non_concrete_entries_are_reported(self):
        cases = [
            ([], "empty"),
            ([f"0.0.0.0:{SIDECAR4}"], "not a concrete interface address"),
            ([f"[::]:{SIDECAR6}"], "not a concrete interface address"),
            ([f"127.0.0.1:{SIDECAR4}"], "not a concrete interface address"),
            ([f"{nq.SYNTHETIC4}:{SIDECAR4}"], "not assigned to any interface"),
            ([f"{nq.BASE4}:{PEER4}"], "port the sidecar does not own"),
            ([f"{nq.BASE4}:0"], "not a concrete interface address"),
            ([f"{nq.BASE6}:{SIDECAR6}"], "is not a socket address"),
            ([f"{nq.BASE4}:{SIDECAR4}"] * 2, "duplicate"),
        ]
        for addresses, pattern in cases:
            with self.subTest(addresses=addresses):
                problems = nq.concrete_problems("ready", addresses, self.KERNEL, self.PORTS)
                self.assertRegex(" | ".join(problems), pattern)


if __name__ == "__main__":
    unittest.main()
