"""WF-LOCAL-004: original SNI and real HTTP/2 on both numeric-pinned paths."""
from datetime import datetime, timedelta, timezone
import socket
import ssl
import threading

import pytest

from core.handlers.model_hub import fetch_transport as transport
from core.handlers.model_hub.fetch_policy import CheckedTarget, FetchURL


@pytest.fixture
def h2_server(tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import RequestReceived, StreamEnded

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "public.example")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("public.example")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setattr(transport.certifi, "where", lambda: str(cert_path))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_alpn_protocols(["h2"])
    sni, requests, errors = [], [], []
    context.set_servername_callback(lambda sock, hostname, ctx: sni.append(hostname))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]
    def serve():
        responded = False
        try:
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True) as connection:
                connection.settimeout(5)
                assert connection.selected_alpn_protocol() == "h2"
                h2 = H2Connection(config=H2Configuration(client_side=False))
                h2.initiate_connection()
                connection.sendall(h2.data_to_send())
                while data := connection.recv(65536):
                    for event in h2.receive_data(data):
                        if isinstance(event, RequestReceived):
                            requests.append(dict(event.headers))
                        if isinstance(event, StreamEnded):
                            h2.send_headers(event.stream_id, [(":status", "200"), ("content-type", "text/plain")])
                            h2.send_data(event.stream_id, b"HTTP/2 pinned document", end_stream=True)
                            connection.sendall(h2.data_to_send())
                            responded = True
                            # Drain the client's SETTINGS/window updates until
                            # it closes. Closing with unread TLS bytes can reset
                            # the socket and discard the response on Windows.
                    connection.sendall(h2.data_to_send())
        except (ConnectionResetError, ssl.SSLEOFError) as error:
            if not responded:
                errors.append(error)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield port, sni, requests
    finally:
        thread.join(6)
        listener.close()
        assert not thread.is_alive()
        assert not errors


@pytest.mark.parametrize("native", [True, False])
def test_tls_sni_and_http2_preserve_original_host(request, native):
    if native:
        pytest.importorskip("curl_cffi")
    port, sni, requests = request.getfixturevalue("h2_server")
    address = FetchURL(f"https://public.example:{port}/doc", "public.example", port, "https", False)
    # Only this fixture may create a loopback CheckedTarget.
    target = CheckedTarget(address, ("127.0.0.1",))
    headers = transport.request_headers(degraded=not native, locale="en", previous_origin=None, target=target)
    result = (transport.curl_hop if native else transport.httpx_hop)(target, headers, transport.TransferBudget(), 4, lambda: None)
    assert result.body == b"HTTP/2 pinned document"
    assert sni == ["public.example"]
    assert requests[0][b":authority"] == f"public.example:{port}".encode()
