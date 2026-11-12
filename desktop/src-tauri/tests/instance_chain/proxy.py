"""Exact fixture host CONNECT proxy; only its generated certificate is accepted."""

import datetime
import hashlib
import http.server
import json
from pathlib import Path
import ssl
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import requests


def start(root: Path, mappings: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Desktop fixture only")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.avibe.bot")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = root / "certificate.pem"
    key_path = root / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_path.chmod(0o600)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_CONNECT(self):
            host, port = self.path.rsplit(":", 1)
            targets = json.loads(mappings.read_text())
            authority = host if port == "443" else self.path
            with (root / "proxy.log").open("a") as log:
                log.write(json.dumps({"connect": host, "port": port, "mapped": host in targets}) + "\n")
            if authority not in targets or (port != "443" and host != "127.0.0.1"):
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.flush()
            if port == "443":
                self.connection = tls.wrap_socket(self.connection, server_side=True)
            self.rfile = self.connection.makefile("rb", -1)
            self.wfile = self.connection.makefile("wb", 0)
            self.fixture_host = authority
            self.fixture_scheme = "https" if port == "443" else "http"
            self.close_connection = False
            while not self.close_connection:
                self.handle_one_request()

        def forward(self):
            host = getattr(self, "fixture_host", None)
            targets = json.loads(mappings.read_text())
            path = self.path
            scheme = getattr(self, "fixture_scheme", "http")
            if host is None:
                parsed = urlsplit(path)
                if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
                    self.send_error(403)
                    return
                host = parsed.netloc
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
            if host not in targets or self.headers.get("Host", "").removesuffix(":443") != host:
                self.send_error(403)
                return
            base = targets[host]
            assert base.startswith("http://127.0.0.1:")
            length = int(self.headers.get("Content-Length", 0))
            assert length < 2_000_000
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in ("connection", "accept-encoding", "proxy-connection")
            }
            headers["X-Fixture-Scheme"] = scheme
            with requests.Session() as http:
                http.trust_env = False
                result = http.request(
                    self.command,
                    base.rstrip("/") + path,
                    headers=headers,
                    data=self.rfile.read(length) if length else None,
                    timeout=20,
                    allow_redirects=False,
                )
            with (root / "proxy.log").open("a") as log:
                log.write(
                    json.dumps({"host": host, "path": self.path.split("?")[0], "status": result.status_code}) + "\n"
                )
            self.send_response(result.status_code)
            for key in result.raw.headers:
                if key.lower() not in ("connection", "content-length", "transfer-encoding", "content-encoding"):
                    for value in result.raw.headers.getlist(key):
                        self.send_header(key, value)
            self.send_header("Content-Length", str(len(result.content)))
            self.end_headers()
            self.wfile.write(result.content)

        do_GET = forward
        do_POST = forward
        do_DELETE = forward
        do_PUT = forward
        do_OPTIONS = forward

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return server, f"http://127.0.0.1:{server.server_port}", fingerprint
