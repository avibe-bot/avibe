"""HTTPS asset supplier for the disposable released-client upgrade test.

No product URL override is used. Only the disposable container maps github.com
to this server and trusts its test CA; installed package code remains intact.
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import subprocess


def create_certificate(directory: Path) -> tuple[Path, Path, Path]:
    """Create ephemeral test credentials, never a developer-machine trust entry."""
    directory.mkdir(parents=True, exist_ok=True)
    ca = directory / "ca.pem"
    cert = directory / "server.pem"
    key = directory / "server.key"
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=DNS:github.com\nbasicConstraints=critical,CA:FALSE\n"
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
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=github.com",
         "-keyout", str(key), "-out", str(directory / "server.csr")],
        ["x509", "-req", "-days", "2", "-in", str(directory / "server.csr"),
         "-CA", str(ca), "-CAkey", str(directory / "ca.key"), "-CAcreateserial",
         "-extfile", str(extensions), "-out", str(cert)],
    ]
    for command in commands:
        subprocess.run(["openssl", *command], check=True, capture_output=True, timeout=30)
    return ca, cert, key


def verify_archive_origin(record: dict, url: str, sha256: str) -> None:
    """Require exact provenance across both specified PEP610 hash encodings."""
    assert record["url"] == url
    archive = record["archive_info"]
    hashes = archive.get("hashes")
    legacy = archive.get("hash")
    if hashes is not None:
        assert hashes.get("sha256") == sha256
        if legacy is not None:
            algorithm, separator, digest = legacy.partition("=")
            assert separator and hashes.get(algorithm) == digest
    else:
        assert legacy == f"sha256={sha256}"


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
