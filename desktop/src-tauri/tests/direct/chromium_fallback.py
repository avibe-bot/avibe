"""Network-only Chromium proxy fallback probe; throwaway browser state and local sockets."""

import argparse
import base64
import contextlib
import os
from pathlib import Path
import socketserver
import ssl
import subprocess
import tempfile
import threading


def run(browser, root, mode):
    hits = {"connect": 0, "origin": 0}

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            first = self.rfile.readline(4096)
            if not first.startswith(b"CONNECT d1c.test:"):
                return
            hits["connect"] += 1
            while self.rfile.readline(4096) not in (b"\r\n", b""):
                pass
            if mode == "502":
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")

    class Origin(socketserver.StreamRequestHandler):
        def handle(self):
            if not self.rfile.readline(4096).startswith(b"GET / "):
                return
            while self.rfile.readline(4096) not in (b"\r\n", b""):
                pass
            hits["origin"] += 1
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Security-Policy: default-src 'none'\r\nContent-Length: 12\r\nConnection: close\r\n\r\nD1C_RECEIVED"
            )

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(root / "cert.pem", root / "key.pem")
    with contextlib.ExitStack() as stack:
        proxy = stack.enter_context(Server(("127.0.0.1", 0), Proxy))
        origin = stack.enter_context(Server(("127.0.0.1", 0), Origin))
        origin.socket = tls.wrap_socket(origin.socket, server_side=True)
        port = proxy.server_address[1]
        if mode == "unreachable":
            proxy.server_close()
        else:
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            stack.callback(proxy.shutdown)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        stack.callback(origin.shutdown)
        pac = (
            'function FindProxyForURL(u,h){return h==="d1c.test"?"PROXY 127.0.0.1:' + str(port) + '; DIRECT":"DIRECT";}'
        )

        class Pac(socketserver.StreamRequestHandler):
            def handle(self):
                if self.rfile.readline(4096).startswith(b"GET /rules.pac "):
                    hits["pac"] = hits.get("pac", 0) + 1
                    while self.rfile.readline(4096) not in (b"\r\n", b""):
                        pass
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ns-proxy-autoconfig\r\nConnection: close\r\nContent-Length: "
                        + str(len(pac)).encode()
                        + b"\r\n\r\n"
                        + pac.encode()
                    )

        pac_server = stack.enter_context(Server(("127.0.0.1", 0), Pac))
        threading.Thread(target=pac_server.serve_forever, daemon=True).start()
        stack.callback(pac_server.shutdown)
        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--use-mock-keychain",
            "--use-in-memory-pasteboard",
            "--password-store=basic",
            "--user-data-dir=" + str(root / ("profile-" + mode)),
            "--host-resolver-rules=MAP d1c.test 127.0.0.1, EXCLUDE 127.0.0.1, MAP * ~NOTFOUND",
            "--proxy-pac-url=http://127.0.0.1:" + str(pac_server.server_address[1]) + "/rules.pac",
            "--ignore-certificate-errors-spki-list=" + (root / "spki").read_text(),
            "--dump-dom",
            f"https://d1c.test:{origin.server_address[1]}/",
        ]
        if mode == "direct":
            cmd = [v for v in cmd if not v.startswith("--proxy-pac-url=")]
            cmd.insert(1, "--no-proxy-server")
        result = subprocess.run(cmd, cwd=root, env={**os.environ, "HOME": str(root)}, capture_output=True, timeout=20)
        assert result.returncode == 0
        if not hits["origin"]:
            import re

            print("browser result:", sorted(set(re.findall(rb"ERR_[A-Z0-9_]+", result.stdout)))[:4])
        return hits, b"D1C_RECEIVED" in result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("browser")
    browser = parser.parse_args().browser
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=d1c.test",
                "-keyout",
                str(root / "key.pem"),
                "-out",
                str(root / "cert.pem"),
            ],
            check=True,
            capture_output=True,
        )
        import hashlib

        pub = subprocess.check_output(["openssl", "x509", "-in", str(root / "cert.pem"), "-pubkey", "-noout"])
        der = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "der"], input=pub)
        (root / "spki").write_text(base64.b64encode(hashlib.sha256(der).digest()).decode())
        for mode in ["direct", "unreachable", "502", "eof"]:
            hits, reached = run(browser, root, mode)
            print(mode, hits, "reached=", reached, flush=True)
            assert mode == "direct" or hits.get("pac", 0) > 0, "browser did not load fixture PAC"
            assert reached if mode in ("direct", "unreachable") else hits["connect"] > 0
            assert reached == bool(hits["origin"])
            if mode in ("502", "eof"):
                assert not reached, "recheck Chromium fallback before changing the listener"


if __name__ == "__main__":
    main()
