"""HTTPS asset supplier for the disposable released-client upgrade test.

No product URL override is used. Only the disposable container maps github.com
to this server and trusts its test CA; installed package code remains intact.
"""

from __future__ import annotations

import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import Distribution
import json
from pathlib import Path
import ssl
import subprocess
import zipfile


def create_certificate(directory: Path, *, hostname: str = "github.com") -> tuple[Path, Path, Path]:
    """Create ephemeral test credentials, never a developer-machine trust entry."""
    directory.mkdir(parents=True, exist_ok=True)
    ca = directory / "ca.pem"
    cert = directory / "server.pem"
    key = directory / "server.key"
    extensions = directory / "server.ext"
    extensions.write_text(
        f"subjectAltName=DNS:{hostname}\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="utf-8",
    )
    commands = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=Avibe disposable upgrade fixture CA",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-keyout", str(directory / "ca.key"), "-out", str(ca)],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={hostname}",
         "-keyout", str(key), "-out", str(directory / "server.csr")],
        ["x509", "-req", "-days", "2", "-in", str(directory / "server.csr"),
         "-CA", str(ca), "-CAkey", str(directory / "ca.key"), "-CAcreateserial",
         "-extfile", str(extensions), "-out", str(cert)],
    ]
    for command in commands:
        subprocess.run(["openssl", *command], check=True, capture_output=True, timeout=30)
    return ca, cert, key


def verify_installed_companion(distribution: Distribution, wheel: Path, url: str) -> None:
    """Verify exact origin AND installed bytes without requiring optional hashes."""
    record = json.loads(distribution.read_text("direct_url.json"))
    assert record["url"] == url, "Installed companion has a different origin"
    archive = record.get("archive_info")
    assert isinstance(archive, dict), "Wheel origin requires archive_info"
    hashes = archive.get("hashes", {})
    assert isinstance(hashes, dict), "archive_info.hashes must be a dictionary"
    reported = dict(hashes)
    if "hash" in archive:
        assert isinstance(archive["hash"], str), "archive_info.hash must be a string"
        algorithm, separator, digest = archive["hash"].partition("=")
        assert separator and algorithm and digest, "Malformed legacy archive hash"
        if "hashes" in archive:
            assert hashes.get(algorithm) == digest, "Conflicting archive hashes"
        reported[algorithm] = digest
    contents = wheel.read_bytes()
    for algorithm, digest in reported.items():
        assert hashlib.new(algorithm, contents).hexdigest() == digest, "Wrong archive digest"

    # PEP610 permits empty archive_info (uv emits it for an unhashed URL).
    # Independently inspect what was installed, including code and metadata.
    # RECORD is rewritten by installers to add their own generated files.
    with zipfile.ZipFile(wheel) as source:
        payload = [
            entry.filename for entry in source.infolist()
            if not entry.is_dir() and not entry.filename.endswith(".dist-info/RECORD")
        ]
        assert any(name.startswith("avibe_memory/") for name in payload), "Missing companion payload"
        for name in payload:
            installed = Path(distribution.locate_file(name))
            assert installed.is_file(), f"Missing installed companion file: {name}"
            assert installed.read_bytes() == source.read(name), f"Changed installed companion file: {name}"


def make_server(root: Path, cert: Path, key: Path, *, port: int = 0, handler=None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), handler or partial(SimpleHTTPRequestHandler, directory=str(root)),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--port", type=int, default=443)
    args = parser.parse_args()
    with make_server(args.root, args.cert, args.key, port=args.port) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
