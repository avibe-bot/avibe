"""Consuming namespace checks before any Go suite or actual engine starts."""

from __future__ import annotations

import http.client
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

from isolation import namespace_receipt


def probe_blocked_connection(network: str, host: str, port: int, kind: str) -> dict:
    """Accept only an observed connection error, never a successful connection."""
    if kind == "subprocess":
        child = subprocess.run(
            [sys.executable, "-I", "-B", "-c",
             "import socket,sys\ntry:\n s=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=1)\nexcept OSError as exc:\n print(exc.errno)\nelse:\n s.sendall(b'forbidden'); s.close(); sys.exit(3)",
             host, str(port)], capture_output=True, close_fds=True, timeout=3,
        )
        assert child.returncode == 0, child.stderr
        observed_errno = int(child.stdout)
    else:
        try:
            if kind == "direct-ip":
                with socket.create_connection((host, port), timeout=1) as connection:
                    connection.sendall(b"forbidden")
                raise AssertionError("Direct socket reached outside sentinel.")
            connection = http.client.HTTPConnection(host, port, timeout=1)
            try:
                method = "CONNECT" if kind == "connect-proxy" else "GET"
                target = "example.invalid:443" if kind == "connect-proxy" else (
                    "http://example.invalid/" if kind == "http-proxy" else "/"
                )
                headers = {"Connection": "Upgrade", "Upgrade": "websocket"} if kind == "websocket" else {}
                connection.request(method, target, headers=headers)
                raise AssertionError(f"{kind} reached outside sentinel.")
            finally:
                connection.close()
        except OSError as exc:
            observed_errno = exc.errno
    allowed = (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM)
    # With loopback down, IPv6 has no usable local address in the empty netns.
    if network == "none" and host == "::1":
        allowed += (errno.EADDRNOTAVAIL,)
    assert observed_errno in allowed, (network, host, kind, observed_errno)
    return {"host": host, "kind": kind, "blocked": True, "errno": observed_errno}


def main() -> None:
    proof = namespace_receipt()
    attempts = [
        probe_blocked_connection(proof["network"], host, port, kind)
        for host, port in zip(("127.0.0.1", "::1"), proof["sentinel_ports"])
        for kind in ("http", "websocket", "http-proxy", "connect-proxy", "direct-ip", "subprocess")
    ]
    for name in ("source", "fixture", "recipe"):
        target = Path(proof[name]) / ".isolation-write-probe"
        try:
            target.write_bytes(b"must-not-write")
        except OSError:
            pass
        else:
            raise AssertionError(f"{name} is writable.")
    for path in ("/Users", "/home", "/root", "/sys", "/run/netns"):
        assert not Path(path).exists(), f"Guest/user namespace surface exposed: {path}"
    assert not any("shared:" in line for line in Path("/proc/self/mountinfo").read_text().splitlines())
    assert os.getuid() == proof["uid"] != 0 and os.getgid() == proof["gid"] != 0
    state = Path(proof["state"])
    representative_write = state / "writable-state-probe"
    representative_write.write_bytes(b"test-owned")
    representative_write.unlink()

    positive = []
    if proof["network"] == "loopback":
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"private-listener")

            def log_message(self, *_args):
                pass

        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            class Server(ThreadingHTTPServer):
                address_family = family

            with Server((host, 0), Handler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                connection = http.client.HTTPConnection(host, server.server_address[1], timeout=2)
                try:
                    connection.request("GET", "/")
                    assert connection.getresponse().read() == b"private-listener"
                    positive.append(host)
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=2)
                assert not thread.is_alive()
    result = {**proof, "isolation_probe": "pass", "blocked_attempts": attempts, "positive_listeners": positive,
              "readonly_source_fixture_recipe": True, "task_state_write": True}
    # The privileged supervisor captures this dedicated preflight stdout before
    # candidate execution. No proof is published into mutable candidate state.
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
