"""Run one CI unit-test shard as concurrent lanes on the same runner.

A hosted Linux runner has four vCPUs, while the one-interpreter-per-file suite
is single-threaded and CPU-bound: about 86% of each file's wall time is its own
process CPU (``CI_TEST_METRICS``). One serial lane per runner therefore leaves
most of every runner idle while the shards set the workflow's critical path.

Shard ``i`` of ``N`` with ``L`` lanes runs virtual shards ``i*L .. i*L+L-1`` of
``N*L``, each through ``scripts/ci_unit_tests.sh``. The same timing-balanced
planner assigns every discovered file to exactly one virtual shard, so
discovery, one process per file, per-file watchdogs and failure propagation are
unchanged; only the number of files in flight on a runner grows to ``L``.

Lanes are child processes of this foreground interpreter rather than shell
background jobs: a shell without job control starts ``&`` jobs with SIGINT
ignored, and every test process would inherit that, unlike the serial launcher.
Each output line is prefixed with its lane so interleaved lanes stay readable.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from pathlib import Path

LANE_RUNNER = Path(__file__).with_name("ci_unit_tests.sh")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 3 or any(not value.isdigit() for value in args):
        print("Usage: ci_unit_test_lanes.py SHARD_INDEX SHARD_TOTAL LANES", file=sys.stderr)
        return 2
    shard_index, shard_total, lanes = (int(value) for value in args)
    if shard_total < 1 or lanes < 1 or shard_index >= shard_total:
        print("Shard index must be less than a positive shard total, with at least one lane.", file=sys.stderr)
        return 2

    output = sys.stdout.buffer
    output_lock = threading.Lock()
    processes = [
        subprocess.Popen(
            ["bash", str(LANE_RUNNER), str(shard_index * lanes + lane), str(shard_total * lanes)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for lane in range(lanes)
    ]

    def stop_lanes(signum, _frame) -> None:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signum)

    signal.signal(signal.SIGTERM, stop_lanes)
    signal.signal(signal.SIGINT, stop_lanes)

    def relay(lane: int, process: subprocess.Popen) -> None:
        prefix = f"[lane {lane}] ".encode()
        assert process.stdout is not None
        for line in process.stdout:
            with output_lock:
                output.write(prefix + line)
                output.flush()

    relays = [
        threading.Thread(target=relay, args=(lane, process), daemon=True)
        for lane, process in enumerate(processes)
    ]
    for thread in relays:
        thread.start()
    codes = [process.wait() for process in processes]
    for thread in relays:
        thread.join()

    failed = [lane for lane, code in enumerate(codes) if code != 0]
    print(
        f"Shard {shard_index}/{shard_total}: {lanes} lane(s) finished with exit codes {codes}.",
        flush=True,
    )
    if failed:
        print(f"Failed lane(s): {failed}.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
