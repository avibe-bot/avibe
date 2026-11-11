# Idle and keepalive qualification

This harness runs only on disposable Linux Actions runners. It never runs network faults on the owner's desktop and uploads evidence, not binaries.

Each case gets its own network and PID namespace. A dummy interface supplies private IPv4 and documentation-prefix IPv6 addresses; there is no external route. The sidecar and default stock client share that namespace so the stand-in TCP stream listener stays on loopback. The fault hook drops packets in `INPUT`, matching the sending endpoint's UDP port in both families. This silently affects **all** candidate paths; a proxy alone would not, because stock iroh can learn alternate addresses. The Rust fixture checks that the namespace has no uncovered UDP socket.

`run.py --sidecar <binary> --peer-exe <prebuilt transport test executable> --out <new directory> --variant <label>` runs the ignored test cases, keeps each log, and writes `report.json`. `--max-close-ms` enables a failure bound after a candidate has been measured. The client executable is hashed before and after: dependency patching or server compilation must not rebuild it.

Cases cover three quiet blackhole directions with fresh connections and varied onset phases; the five-minute no-open-stream cleanup; an open but application-data-idle stream for 305 seconds; deterministic periodic one-in-20 packet loss in each direction for 120 seconds; and repeated 1.5-second two-way outages. Admission is refreshed through stdin during long cases. It does not generate application traffic on the QUIC connection.

The healthy no-stream connection intentionally closes at the existing 300-second **application** deadline. The separate open-idle-stream case proves transport keepalives do not close a working quiet path. Neither test extends the admission TTL or changes the control protocol.

Connection UDP statistics report datagrams and UDP payload bytes, including transport probes/control packets, not IP/UDP headers or link-layer overhead. Fault counters separately report dropped IP bytes. Quiet windows provide per-minute costs in both directions without application data. This is not a measurement of battery use.

The measurements cover a low-latency isolated runner. They are not real-network/mobile/firewall qualification or a universal timer guarantee: QUIC's three-PTO floor and task scheduling still apply. Loss is periodic, not a claim about arbitrary burst-loss distributions. The scratch `experiment.sh` freezes a stock client before changing a disposable server copy. The first matrix compared 8 s / 2.5 s with 4 s / 1.25 s; the follow-up sweeps fifteen onset phases at 4 s / 1.25 s and 3 s / 0.75 s, with and without the experimental NAT-zero patch. The broader phase sweep covers the stock client's independent NAT-traversal round timer, not just keepalives. Nothing from that script is a packaged runtime.
