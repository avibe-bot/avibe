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
import urllib.parse
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
    state = SimpleNamespace(responses=[], requests=[], headers=[], routes={})
    real_urlopen = urllib.request.urlopen

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            state.headers.append(dict(self.headers))
            if state.routes:
                replies = state.routes[self.path]
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
            else:
                reply = state.responses.pop(0)
            status, body, *headers = reply
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers[0] if headers else {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    state.origin = f"http://127.0.0.1:{server.server_port}"
    state.url = state.origin + "/asset"

    def opener(request, *, timeout):
        parsed = urllib.parse.urlparse(request.full_url)
        prefix = "/api" if parsed.hostname == "api.github.com" else "/browser"
        local = urllib.request.Request(state.origin + prefix + parsed.path, headers=request.headers)
        for name, value in request.unredirected_hdrs.items():
            local.add_unredirected_header(name, value)
        return real_urlopen(local, timeout=timeout)

    state.opener = opener
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


@pytest.mark.parametrize("message", ["secondary rate limit", "abuse detection mechanism"])
def test_secondary_api_limit_retries_within_server_and_local_budget(asset_server, retry_clock, message):
    asset_server.responses = [(403, message.encode(), {"Retry-After": "2"}), (200, b"complete")]
    assert downloads.read_bytes(asset_server.url) == b"complete"
    assert len(asset_server.requests) == 2 and retry_clock.sleeps == [2.0]


def test_long_server_rate_limit_is_not_retried_early(asset_server, retry_clock):
    asset_server.responses = [(429, b"slow down", {"Retry-After": "60"})]
    with pytest.raises(RuntimeError, match="retry budget"):
        downloads.read_bytes(asset_server.url)
    assert len(asset_server.requests) == 1 and retry_clock.sleeps == []


def test_persistent_504_stops_after_three_attempts(asset_server, retry_clock):
    asset_server.responses = [(504, b"unavailable")] * 3
    with pytest.raises(urllib.error.HTTPError, match="504"):
        downloads.read_bytes(asset_server.url)
    assert len(asset_server.requests) == 3
    assert retry_clock.sleeps == [1.5, 2.5]


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
    assert len(timeouts) == 3
    assert retry_clock.sleeps == [1.5, 2.5]
    assert 0 < min(timeouts) <= max(timeouts) <= 10
    assert retry_clock.now <= 30


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


_OWN_URL = f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"
_API_LOOKUP = f"/api/repos/avibe-bot/avibe/releases/tags/{RELEASE_TAG}"
_API_ASSET = "/api/repos/avibe-bot/avibe/releases/assets/42"
_BROWSER = f"/browser/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"


def _api_routes(server, content=b"verified bytes", *, url=_OWN_URL):
    parts = urllib.parse.urlparse(url).path.split("/")
    repository = "/".join(parts[1:3])
    tag, name = (urllib.parse.unquote(part) for part in parts[5:])
    lookup = f"/api/repos/{repository}/releases/tags/{parts[5]}"
    api_asset = f"/api/repos/{repository}/releases/assets/42"
    browser = "/browser" + urllib.parse.urlparse(url).path
    asset = {"id": 42, "name": name, "browser_download_url": url,
             "digest": f"sha256:{hashlib.sha256(content).hexdigest()}"}
    release = json.dumps({"tag_name": tag, "assets": [asset]}).encode()
    server.routes = {
        lookup: [(200, release)],
        api_asset: [(200, content)],
        browser: [(504, b"browser route persistently unavailable")],
    }
    return asset


@pytest.mark.parametrize("url", [
    _OWN_URL,
    "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-pc-windows-msvc.zip",
    "https://github.com/another/project/releases/download/release%2Fv1/tool%2Bwindows.zip",
], ids=["owned", "ripgrep", "encoded"])
@pytest.mark.parametrize("to_file", [False, True])
@pytest.mark.parametrize("reuse_metadata", [False, True])
def test_github_release_resolves_api_asset_without_touching_broken_browser(
    asset_server, retry_clock, tmp_path, monkeypatch, to_file, reuse_metadata, url,
):
    # A hardcoded repository prefix used to leave third-party GitHub releases
    # on a persistently failing browser path despite a healthy asset API.
    asset = _api_routes(asset_server, url=url)
    lookup, api_asset, _ = asset_server.routes
    metadata = asset if reuse_metadata else None
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    if to_file:
        monkeypatch.setattr(downloads.urllib.request, "urlopen", asset_server.opener)
        target = tmp_path / "asset"
        downloads.download_to_path(url, target, release_asset=metadata)
        result = target.read_bytes()
    else:
        result = downloads.read_bytes(url, opener=asset_server.opener, release_asset=metadata)
    assert result == b"verified bytes"
    assert asset_server.requests == ([api_asset] if reuse_metadata else [lookup, api_asset])
    expected_accepts = ["application/octet-stream"] if reuse_metadata else [
        "application/vnd.github+json", "application/octet-stream",
    ]
    assert [headers["Accept"] for headers in asset_server.headers] == expected_accepts
    assert all(headers["Authorization"] == "Bearer test-github-token" for headers in asset_server.headers)
    assert retry_clock.sleeps == []


@pytest.mark.parametrize("mismatch", [False, True])
def test_manifest_reuses_release_api_metadata_and_still_checks_digest(asset_server, tmp_path, mismatch):
    content = (json.dumps(_manifest()) + "\n").encode()
    asset = _api_routes(asset_server, content)
    if mismatch:
        asset["digest"] = f"sha256:{'0' * 64}"
        asset_server.routes[_API_LOOKUP] = [(200, json.dumps({"tag_name": RELEASE_TAG, "assets": [asset]}).encode())]
    target = tmp_path / "manifest.json"
    target.write_bytes(b"previous verified manifest")
    if mismatch:
        with pytest.raises(RuntimeError, match="digest mismatch"):
            manifests.prepare_manifest(target, release_tag=RELEASE_TAG, opener=asset_server.opener)
        assert target.read_bytes() == b"previous verified manifest"
    else:
        assert manifests.prepare_manifest(target, release_tag=RELEASE_TAG, opener=asset_server.opener) == _manifest()
        assert target.read_bytes() == content
    # No duplicate metadata lookup and no browser fallback after valid API bytes,
    # even if the caller rejects those bytes on integrity grounds.
    assert asset_server.requests == [_API_LOOKUP, _API_ASSET]


@pytest.mark.parametrize("failure_path", [_API_LOOKUP, _API_ASSET])
@pytest.mark.parametrize("status", [403, 404, 429, 504])
def test_api_unavailable_falls_back_to_browser(asset_server, retry_clock, failure_path, status):
    _api_routes(asset_server)
    asset_server.routes[failure_path] = [(status, b"API unavailable")]
    asset_server.routes[_BROWSER] = [(200, b"verified bytes")]
    assert downloads.read_bytes(_OWN_URL, opener=asset_server.opener) == b"verified bytes"
    expected = [_API_LOOKUP] if failure_path == _API_LOOKUP else [_API_LOOKUP, _API_ASSET]
    assert asset_server.requests == expected + [_BROWSER]
    assert retry_clock.sleeps == []


@pytest.mark.parametrize("api_status", [404, 504])
def test_fallback_retries_do_not_repeat_permanent_api_errors(asset_server, retry_clock, api_status):
    _api_routes(asset_server)
    asset_server.routes[_API_ASSET] = [(api_status, b"API unavailable"), (200, b"verified bytes")]
    asset_server.routes[_BROWSER] = [(504, b"browser unavailable"), (200, b"verified bytes")]
    assert downloads.read_bytes(_OWN_URL, opener=asset_server.opener) == b"verified bytes"
    expected_last = _BROWSER if api_status == 404 else _API_ASSET
    assert asset_server.requests == [_API_LOOKUP, _API_ASSET, _BROWSER, expected_last]
    assert retry_clock.sleeps == [1.5]


@pytest.mark.parametrize("failure_path", [_API_LOOKUP, _API_ASSET])
def test_rate_limited_api_is_not_replayed_during_public_asset_retry(asset_server, retry_clock, failure_path):
    _api_routes(asset_server)
    asset_server.routes[failure_path] = [(403, b"secondary rate limit", {"Retry-After": "60"})]
    asset_server.routes[_BROWSER] = [(504, b"browser unavailable"), (200, b"verified bytes")]
    assert downloads.read_bytes(_OWN_URL, opener=asset_server.opener) == b"verified bytes"
    prefix = [_API_LOOKUP] if failure_path == _API_LOOKUP else [_API_LOOKUP, _API_ASSET]
    assert asset_server.requests == prefix + [_BROWSER, _BROWSER]
    assert retry_clock.sleeps == [1.5]


def test_api_and_browser_fallback_share_the_retry_time_budget(retry_clock):
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        retry_clock.now += timeout
        raise TimeoutError("network stalled")

    with pytest.raises(TimeoutError):
        downloads.read_bytes(_OWN_URL, opener=opener)
    assert any("api.github.com" in url for url in calls)
    assert _OWN_URL in calls
    assert retry_clock.now <= 30


def test_api_size_failure_does_not_fall_back(asset_server, retry_clock, monkeypatch, tmp_path):
    _api_routes(asset_server)
    monkeypatch.setattr(downloads.urllib.request, "urlopen", asset_server.opener)
    target = tmp_path / "asset"
    with pytest.raises(ValueError, match="size limit"):
        downloads.download_to_path(_OWN_URL, target, max_bytes=3)
    assert asset_server.requests == [_API_LOOKUP, _API_ASSET]
    assert not target.exists()
    assert retry_clock.sleeps == []


@pytest.mark.parametrize("url", [
    _OWN_URL,
    "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/tool.zip",
], ids=["owned", "ripgrep"])
def test_api_credentials_are_not_forwarded_to_asset_storage(asset_server, monkeypatch, url):
    _api_routes(asset_server, url=url)
    lookup, api_asset, _ = asset_server.routes
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    asset_server.routes[api_asset] = [(302, b"", {"Location": asset_server.origin + "/storage"})]
    asset_server.routes["/storage"] = [(200, b"verified bytes")]
    assert downloads.read_bytes(url, opener=asset_server.opener) == b"verified bytes"
    assert asset_server.requests == [lookup, api_asset, "/storage"]
    assert asset_server.headers[1]["Authorization"] == "Bearer test-github-token"
    assert "Authorization" not in asset_server.headers[2]


@pytest.mark.parametrize("url", [
    "https://downloads.example/other/repo/releases/download/v1/tool.zip",
    "https://github.com.example/other/repo/releases/download/v1/tool.zip",
    "https://github.com@downloads.example/other/repo/releases/download/v1/tool.zip",
    "https://github.com/other/repo/archive/refs/tags/v1.zip",
])
def test_other_urls_keep_plain_retry_without_api_credentials(asset_server, retry_clock, monkeypatch, url):
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    path = "/browser" + urllib.parse.urlparse(url).path
    asset_server.routes[path] = [(504, b"unavailable"), (200, b"verified bytes")]
    assert downloads.read_bytes(url, opener=asset_server.opener) == b"verified bytes"
    assert asset_server.requests == [path, path]
    assert all("Authorization" not in headers for headers in asset_server.headers)
    assert retry_clock.sleeps == [1.5]
