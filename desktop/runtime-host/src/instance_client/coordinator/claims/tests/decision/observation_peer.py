"""Test-owned peer for a native current-binding read. No Runtime, network or user state."""

import json
import sys
from pathlib import Path


def receive():
    return json.loads(sys.stdin.readline())


def send(event, **fields):
    print(json.dumps({"v": 1, "event": event, **fields}), flush=True)


Path("peer-started").touch()
case = sys.argv[1]
if case in {"old_cli", "exits"}:
    # As a CLI that does not know the claim pipe: it exits on the unknown command.
    sys.exit(2)
challenge = "e" * 64
generation = "a" * 64
assert receive() == {"v": 1, "op": "inspect", "origin": "http://127.0.0.1:40123"}
send("challenge", challenge=challenge)
Path("challenge").touch()
assert receive() == {"v": 1, "op": "begin", "challenge": challenge}
Path("begin").touch()
if case == "absent":
    send("no_binding")
elif case == "legacy":
    send("result", outcome="error", code="legacy_pairing_ended")
elif case == "helper_error":
    send("result", outcome="error", code="local_binding_changed")
else:
    send("binding", instance_id="fixture-local", generation=generation)
    Path("binding").touch()
    if case not in {"offline", "late_error", "during"}:
        command = receive()
        Path("recheck").touch()
        nonce = command["challenge"]
        assert command == {"v": 1, "op": "recheck", "generation": generation, "challenge": nonce}
        assert len(nonce) == 32 and int(nonce, 16) >= 0
        send("unchanged", challenge=nonce, generation=generation)
        Path("unchanged").touch()
assert not sys.stdin.readline()  # EOF, never an arm or an On request
Path("inspect-finished").touch()
