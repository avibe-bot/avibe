#!/usr/bin/env python3
"""Fault only test-owned UDP ports inside a disposable Actions network namespace."""

import json
import os
import subprocess
import sys
from pathlib import Path

CHAIN = "AVDT_IDLE"
MODES = {"clear", "both", "client-to-server", "server-to-client", "loss5"}


def run(*args, check=True):
    result = subprocess.run(args, check=False, text=True, capture_output=True)
    if check and result.returncode:
        sys.stderr.write(result.stderr)
        result.check_returncode()
    return result


def main():
    if (
        sys.platform != "linux"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("AVDT_ISOLATED_IDLE") != "1"
        or os.geteuid() != 0
        or not os.environ.get("AVDT_HOST_NETNS")
        or os.readlink("/proc/self/ns/net") == os.environ["AVDT_HOST_NETNS"]
    ):
        raise SystemExit("refusing fault outside a disposable Actions network namespace")
    run("ip", "link", "show", "dev", "avdt-idle")
    if len(sys.argv) != 6 or sys.argv[1] not in MODES:
        raise SystemExit("usage: fault.py MODE CLIENT_V4 CLIENT_V6 SERVER_V4 SERVER_V6")
    mode = sys.argv[1]
    ports = [int(value) for value in sys.argv[2:]]
    if any(port < 0 or port > 65535 for port in ports) or not any(ports[:2]) or not any(ports[2:]):
        raise SystemExit("invalid endpoint UDP ports")
    counters = []
    for tool, client, server in (("iptables", ports[0], ports[2]), ("ip6tables", ports[1], ports[3])):
        if client and client == server:
            raise SystemExit("test endpoint ports must differ within an address family")
        exists = run(tool, "-w", "-L", CHAIN, "-n", check=False).returncode == 0
        if not exists:
            run(tool, "-w", "-N", CHAIN)
            # Local peers share the namespace. INPUT drops every delivered path
            # silently; unlike OUTPUT DROP it cannot return EPERM to sendmsg.
            run(tool, "-w", "-I", "INPUT", "-j", CHAIN)
        else:
            listing = run(tool, "-w", "-L", CHAIN, "-n", "-v", "-x", "--line-numbers").stdout
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) > 3 and fields[0].isdigit() and fields[3] == "DROP":
                    direction = "client-to-server" if "client-to-server" in line else "server-to-client"
                    counters.append({"family": tool, "direction": direction, "rule": int(fields[0]), "packets": int(fields[1]), "ip_bytes": int(fields[2])})
        run(tool, "-w", "-F", CHAIN)
        for direction, port in (("client-to-server", client), ("server-to-client", server)):
            if not port or mode not in {"both", "loss5", direction}:
                continue
            rule = [tool, "-w", "-A", CHAIN, "-p", "udp", "--sport", str(port)]
            if mode == "loss5":
                # Deterministic periodic loss, not an assertion about bursty or
                # independent random loss. Covers exactly one of every20 packets.
                rule += ["-m", "statistic", "--mode", "nth", "--every", "20", "--packet", "0"]
            run(*rule, "-m", "comment", "--comment", direction, "-j", "DROP")
    result = {"mode": mode, "previous_drop_counters": counters, "counter_bytes": "IP bytes including headers", "chain": "INPUT"}
    for owner, direction in (("client", "client-to-server"), ("server", "server-to-client")):
        result[owner + "_dropped_packets"] = sum(row["packets"] for row in counters if row["direction"] == direction)
        result[owner + "_dropped_bytes"] = sum(row["ip_bytes"] for row in counters if row["direction"] == direction)
    line = json.dumps(result, sort_keys=True)
    print(line, flush=True)
    with Path(os.environ["AVDT_IDLE_FAULT_LOG"]).open("a") as stream:
        stream.write(line + "\n")


if __name__ == "__main__":
    main()
