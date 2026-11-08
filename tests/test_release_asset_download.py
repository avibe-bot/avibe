"""Release transport retries must not turn integrity or permanent failures into success.

The previous manifest reader failed on the first 504; the other script loops
had independent policies. Loopback HTTP covers real urllib status handling,
while a fake clock / interrupted stream makes timeout budgets deterministic.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts import release_asset_download as downloads
from scripts import show_runtime_manifest_asset as manifests
from tests.test_show_runtime_manifest_packaging import RELEASE_TAG, _manifest


@pytest.fixture
def retry_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay

    monkeypatch.setattr(downloads, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(downloads.random, "uniform", lambda low, high: high)
    return clock


@pytest.fixture
def asset_server():
    state = SimpleNamespace(responses=[], requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            status, body = state.responses.pop(0)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/asset"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("to_file", [False, True])
@pytest.mark.parametrize("status", [429, 500, 504, 599, 400, 401, 403, 404, 408, 425])
def test_http_retries_only_transient_statuses(asset_server, retry_clock, tmp_path, to_file, status):
    asset_server.responses = [(status, b"unavailable"), (200, b"verified bytes")]
    target = tmp_path / "asset"

    def fetch():
        if to_file:
            downloads.download_to_path(asset_server.url, target)
            return target.read_bytes()
        return downloads.read_bytes(asset_server.url)

    if status == 429 or status >= 500:
        assert fetch() == b"verified bytes"
        assert len(asset_server.requests) == 2
        assert retry_clock.sleeps == [1.5]
    else:
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch()
        assert error.value.code == status
        assert len(asset_server.requests) == 1
        assert retry_clock.sleeps == []
        assert not target.exists()


def test_persistent_504_stops_after_four_attempts(asset_server, retry_clock):
    asset_server.responses = [(504, b"unavailable")] * 4
    with pytest.raises(urllib.error.HTTPError, match="504"):
        downloads.read_bytes(asset_server.url)
    assert len(asset_server.requests) == 4
    assert retry_clock.sleeps == [1.5, 2.5, 4.5]


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("error_type", [ConnectionResetError, TimeoutError])
def test_connection_failures_retry_with_a_subminute_budget(retry_clock, wrapped, error_type):
    timeouts = []

    def opener(request, *, timeout):
        timeouts.append(timeout)
        retry_clock.now += timeout
        error = error_type("interrupted")
        raise urllib.error.URLError(error) if wrapped else error

    with pytest.raises((urllib.error.URLError, error_type)):
        downloads.read_bytes("https://example.invalid/asset", opener=opener)
    assert len(timeouts) == 4
    assert retry_clock.sleeps == [1.5, 2.5, 4.5]
    assert 0 < min(timeouts) <= max(timeouts) <= 10
    assert retry_clock.now <= 45


def test_elapsed_window_prevents_another_attempt(retry_clock):
    calls = []

    def opener(request, *, timeout):
        calls.append(timeout)
        retry_clock.now += 45
        raise ConnectionResetError("failed after a long transfer")

    with pytest.raises(ConnectionResetError):
        downloads.read_bytes("https://example.invalid/asset", opener=opener)
    assert calls == [10]
    assert retry_clock.sleeps == []


@pytest.mark.parametrize("error", [PermissionError("denied"), urllib.error.URLError("bad certificate")])
def test_other_io_failures_do_not_retry(retry_clock, error):
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        raise error

    with pytest.raises(type(error)):
        downloads.read_bytes("https://example.invalid/asset", opener=opener)
    assert len(calls) == 1
    assert retry_clock.sleeps == []


def test_interrupted_stream_restarts_without_partial_bytes(monkeypatch, retry_clock, tmp_path):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise ConnectionResetError("reset during body")
            return super().read(size)

    responses = [Interrupted(b"partial"), io.BytesIO(b"complete")]
    monkeypatch.setattr(downloads.urllib.request, "urlopen", lambda *args, **kwargs: responses.pop(0))
    target = tmp_path / "asset"
    downloads.download_to_path("https://example.invalid/asset", target)
    assert target.read_bytes() == b"complete"
    assert retry_clock.sleeps == [1.5]


def test_size_limit_is_not_retried_and_partial_file_is_removed(asset_server, retry_clock, tmp_path):
    asset_server.responses = [(200, b"oversized")]
    target = tmp_path / "asset"
    with pytest.raises(ValueError, match="size limit"):
        downloads.download_to_path(asset_server.url, target, max_bytes=3)
    assert len(asset_server.requests) == 1
    assert retry_clock.sleeps == []
    assert not target.exists()


@pytest.mark.parametrize("case", ["transient", "missing", "digest-mismatch"])
def test_manifest_consumer_retries_transport_but_never_integrity(asset_server, tmp_path, case):
    content = (json.dumps(_manifest()) + "\n").encode()
    url = f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"
    digest = "0" * 64 if case == "digest-mismatch" else hashlib.sha256(content).hexdigest()
    release = json.dumps({
        "tag_name": RELEASE_TAG,
        "assets": [{"name": "show-runtime-manifest.json", "browser_download_url": url,
                    "digest": f"sha256:{digest}"}],
    }).encode()
    # The injected opener routes real requests to loopback without weakening the
    # production GitHub release identity or digest validation.
    asset_server.responses = [(200, release)]
    if case == "missing":
        asset_server.responses += [(404, b"missing")]
    else:
        asset_server.responses += [(504, b"unavailable"), (200, content)]

    def opener(request, *, timeout):
        return urllib.request.urlopen(asset_server.url, timeout=timeout)

    target = tmp_path / "manifest.json"
    target.write_bytes(b"previous verified manifest")
    if case == "missing":
        with pytest.raises(urllib.error.HTTPError, match="404"):
            manifests.prepare_manifest(target, release_tag=RELEASE_TAG, opener=opener)
        assert len(asset_server.requests) == 2
    elif case == "digest-mismatch":
        with pytest.raises(RuntimeError, match="digest mismatch"):
            manifests.prepare_manifest(target, release_tag=RELEASE_TAG, opener=opener)
        assert len(asset_server.requests) == 3
    else:
        assert manifests.prepare_manifest(target, release_tag=RELEASE_TAG, opener=opener) == _manifest()
        assert target.read_bytes() == content
        assert len(asset_server.requests) == 3
    if case != "transient":
        assert target.read_bytes() == b"previous verified manifest"
