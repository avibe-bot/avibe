"""Deny-by-default CONNECT fixture: serves sealed bytes, never opens upstream sockets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.server
import json
from pathlib import Path
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from common import save


class Fixture:
    def __init__(self, root: Path, build: dict):
        self.root, self.build = root, build
        self.calls = []
        self.denied = []
        self.lock = threading.Lock()
        payload = Path(build["payload"])
        manifest = Path(build["manifest"])
        tag = build["tag"]
        index = {"schema_version": 1, "repository": "avibe-bot/avibe", "releases": [{
            "tag": tag, "prerelease": True, "commit": build["source_sha"],
            "assets": [{"name": name} for name in (payload.name, manifest.name, manifest.name + ".sig")],
        }]}
        files = {("dl.avibe.bot", "/index/releases.json"): json.dumps(index).encode()}
        for path in (payload, manifest, Path(str(manifest) + ".sig")):
            body = path.read_bytes()
            files[("dl.avibe.bot", f"/releases/{tag}/{path.name}")] = body
            files[("github.com", f"/avibe-bot/avibe/releases/download/{tag}/{path.name}")] = body
        self.payload_paths = {path for host, path in files if path.endswith(payload.name)}
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Avibe hosted acceptance only")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=6))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in ("dl.avibe.bot", "github.com")]), critical=False)
                .sign(key, hashes.SHA256()))
        certificate = root / "certificate.pem"
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private = root / "tls-key.pem"
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private.chmod(0o600)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certificate, private)
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reject(self, text):
                with fixture.lock:
                    fixture.denied.append(text)
                self.send_error(403)

            def do_CONNECT(self):
                if self.path not in {"dl.avibe.bot:443", "github.com:443"}:
                    self.reject("connect:" + self.path)
                    return
                self.fixture_host = self.path.removesuffix(":443")
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.connection = tls.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb", -1)
                self.wfile = self.connection.makefile("wb", 0)
                self.close_connection = False
                while not self.close_connection:
                    self.handle_one_request()

            def do_GET(self):
                host = getattr(self, "fixture_host", None)
                if self.headers.get("Host", "").removesuffix(":443") != host or (host, self.path) not in files:
                    self.reject("get:" + str(host) + self.path)
                    return
                with fixture.lock:
                    event = {"host": host, "path": self.path, "monotonic": time.monotonic(),
                             "payload": self.path in fixture.payload_paths}
                    fixture.calls.append(event)
                    with (root / "updater-http.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event) + "\n")
                body = files[(host, self.path)]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.proxy = f"http://127.0.0.1:{self.server.server_port}"
        self.certificate = certificate

    def payload_count(self) -> int:
        with self.lock:
            return sum(call["payload"] for call in self.calls)

    def indexes(self) -> list[dict]:
        with self.lock:
            return [call for call in self.calls if call["path"] == "/index/releases.json"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        save(self.root / "updater-denied.json", self.denied)
        (self.root / "tls-key.pem").unlink(missing_ok=True)
