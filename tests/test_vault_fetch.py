"""CLI tests for ``vibe vault fetch`` (P0 commit 3b — brokered HTTP proxy).

A real loopback HTTP server records what it receives, so we can assert the secret is
attached at egress (never on stdout) and that domain binding refuses other hosts before
the secret is decrypted. Isolated VIBE_REMOTE_HOME via conftest.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading

import pytest

from vibe import cli


def _ns(**kw):
    base = dict(
        name=None, stdin=False, from_file=None, group=None, tag=None, description=None,
        allow_host=None, auth_header=None, auth_query=None,
        auth=None, url=None, method="GET", header=None, data=None, data_file=None, output=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def http_server():
    log: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _record_and_reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            log.append(
                {
                    "path": self.path,
                    "method": self.command,
                    "auth": self.headers.get("Authorization"),
                    "x_api_key": self.headers.get("X-Api-Key"),
                    "body": body,
                }
            )
            payload = b'{"result": "response-ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _record_and_reply
        do_POST = _record_and_reply

        def log_message(self, *args):  # silence the server's stderr logging
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", log
    finally:
        srv.shutdown()


def _set(name, value, tmp_path, **kw):
    vf = tmp_path / f"{name}.txt"
    vf.write_text(value)
    assert cli.cmd_vault_set(_ns(name=name, from_file=str(vf), **kw)) == 0


def test_fetch_attaches_bearer_and_passes_through_without_leak(http_server, tmp_path, capfd):
    base, log = http_server
    secret = "ghp-bearer-secret-1"
    _set("GH_PAT", secret, tmp_path, allow_host=["127.0.0.1"])
    capfd.readouterr()

    code = cli.cmd_vault_fetch(_ns(auth="GH_PAT", url=f"{base}/repos/o/r"))
    captured = capfd.readouterr()

    assert code == 0
    assert len(log) == 1
    assert log[0]["auth"] == f"Bearer {secret}"  # attached at egress
    assert "response-ok" in captured.out  # upstream body passed through
    assert secret not in captured.out  # never leaked to the CLI's stdout
    assert secret not in captured.err


def test_fetch_custom_header_auth(http_server, tmp_path, capfd):
    base, log = http_server
    secret = "apikey-XYZ-2"
    _set("SVC_KEY", secret, tmp_path, allow_host=["127.0.0.1"], auth_header="X-Api-Key")
    capfd.readouterr()

    assert cli.cmd_vault_fetch(_ns(auth="SVC_KEY", url=f"{base}/v1/thing")) == 0
    assert log[0]["x_api_key"] == secret
    assert log[0]["auth"] is None


def test_fetch_post_body(http_server, tmp_path, capfd):
    base, log = http_server
    _set("POST_KEY", "k", tmp_path, allow_host=["127.0.0.1"])
    capfd.readouterr()
    assert cli.cmd_vault_fetch(_ns(auth="POST_KEY", url=f"{base}/create", method="POST", data='{"x":1}')) == 0
    assert log[0]["method"] == "POST"
    assert log[0]["body"] == b'{"x":1}'


def test_fetch_denies_disallowed_host_without_hitting_it(http_server, tmp_path, capfd):
    base, log = http_server
    _set("BOUND_KEY", "secret", tmp_path, allow_host=["api.github.com"])
    capfd.readouterr()
    # base host is 127.0.0.1, not in allowed_hosts → must be refused, server untouched.
    code = cli.cmd_vault_fetch(_ns(auth="BOUND_KEY", url=f"{base}/x"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "host_not_allowed"
    assert log == []  # the request was never sent


def test_fetch_refuses_plaintext_http_to_real_host(http_server, tmp_path, capfd):
    # A real (non-loopback) host over plaintext http must be refused before the secret
    # is decrypted, even though the host is in allowed_hosts.
    _set("TLS_KEY", "secret", tmp_path, allow_host=["api.example.com"])
    capfd.readouterr()
    code = cli.cmd_vault_fetch(_ns(auth="TLS_KEY", url="http://api.example.com/x"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "insecure_transport"


def test_fetch_refuses_unbound_secret(http_server, tmp_path, capfd):
    base, log = http_server
    _set("UNBOUND_KEY", "secret", tmp_path)  # no --allow-host
    capfd.readouterr()
    code = cli.cmd_vault_fetch(_ns(auth="UNBOUND_KEY", url=f"{base}/x"))
    captured = capfd.readouterr()
    assert code == 1
    assert json.loads(captured.err)["code"] == "proxy_unbound"
    assert log == []


def test_fetch_subdomain_match(http_server, tmp_path, capfd):
    # '.example.com' style entry matches 127.0.0.1 only if listed; here verify exact +
    # that a leading-dot entry matches a subdomain via the helper.
    assert cli._host_allowed("api.github.com", [".github.com"]) is True
    assert cli._host_allowed("github.com", [".github.com"]) is True
    assert cli._host_allowed("evil.com", [".github.com"]) is False
    assert cli._host_allowed("api.github.com", ["api.github.com"]) is True
    assert cli._host_allowed("other.com", ["api.github.com"]) is False
