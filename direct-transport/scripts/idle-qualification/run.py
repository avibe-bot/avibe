#!/usr/bin/env python3
"""Run each idle qualification case in its own disposable network/PID namespace."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CASES = (
    "blackhole_both",
    "blackhole_client_to_server",
    "blackhole_server_to_client",
    "quiet_no_stream",
    "quiet_open_stream",
    "moderate_loss",
    "short_outage",
)
SETUP = """
set -euo pipefail
ip link set lo up
ip link add avdt-idle type dummy
ip addr add 192.168.244.1/24 dev avdt-idle
ip -6 addr add 2001:db8:244::1/64 dev avdt-idle nodad
ip link set avdt-idle up
exec "$@"
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--peer-exe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--max-close-ms", type=int)
    args = parser.parse_args()
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Actions Linux runner only; never run on the live desktop")
    args.sidecar = args.sidecar.resolve(strict=True)
    args.peer_exe = args.peer_exe.resolve(strict=True)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    hook = Path(__file__).resolve().with_name("fault.py")
    before = digest(args.peer_exe)
    host_netns = os.readlink("/proc/self/ns/net")

    def case(name):
        log = args.out / (name + ".log")
        faults = args.out / (name + ".faults.jsonl")
        env = [
            "GITHUB_ACTIONS=true", "AVDT_ISOLATED_IDLE=1",
            "AVDT_HOST_NETNS=" + host_netns,
            "AVDT_TEST_BINARY=" + str(args.sidecar),
            "AVDT_IDLE_FAULT_HOOK=" + str(hook),
            "AVDT_IDLE_FAULT_LOG=" + str(faults),
        ]
        if args.max_close_ms is not None:
            env.append("AVDT_IDLE_MAX_CLOSE_MS=" + str(args.max_close_ms))
        command = [
            "sudo", "env", *env, "unshare", "--net", "--pid", "--fork",
            "--mount-proc", "--kill-child=SIGKILL", "--", "bash", "-c", SETUP,
            "_", str(args.peer_exe), "--exact", "idle_qualification::" + name,
            "--ignored", "--nocapture", "--test-threads=1",
        ]
        with log.open("w") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=430)
            except subprocess.TimeoutExpired:
                # Only this recorded child group; the namespace PID1 exit also
                # kills any sidecar child, even if an assertion panicked.
                subprocess.run(["sudo", "kill", "-KILL", "--", "-" + str(process.pid)], check=False)
                process.wait(timeout=10)
                code = 124
        text = log.read_text()
        records = []
        for line in text.splitlines():
            if "AVDT_IDLE_RESULT " in line:
                records.append(json.loads(line.split("AVDT_IDLE_RESULT ", 1)[1]))
        fault_records = [json.loads(line) for line in faults.read_text().splitlines()] if faults.exists() else []
        dropped = sum(c["packets"] for record in fault_records for c in record["previous_drop_counters"])
        passed = code == 0 and "test result: ok. 1 passed; 0 failed" in text and bool(records)
        if name == "moderate_loss":
            passed = passed and dropped > 0
        result = {"case": name, "exit_code": code, "passed": passed, "records": records, "dropped_packets": dropped}
        print(json.dumps({"case": name, "passed": passed, "exit_code": code}), flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(case, CASES))
    report = {
        "variant": args.variant,
        "client_sha256_before": before,
        "client_sha256_after": digest(args.peer_exe),
        "sidecar_sha256": digest(args.sidecar),
        "all_passed": all(result["passed"] for result in results),
        "results": results,
        "limits": [
            "isolated low-RTT Linux namespace, not real LAN/mobile/firewall qualification",
            "loss is periodic one-in20 per direction, not a burst-loss distribution",
            "UDP counters are payload bytes; fault counters are IP bytes",
            "production300s no-open-stream cleanup is distinct from transport idle",
        ],
    }
    (args.out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["client_sha256_after"] != before or not report["all_passed"]:
        raise SystemExit("idle qualification failed; see per-case logs and report.json")


if __name__ == "__main__":
    main()
