from __future__ import annotations

import http.client
import io
import socket
import ssl
import urllib.error
import urllib.request

import pytest

from core import dependency_network
from scripts import release_mirror


RELEASE_URL = "https://github.com/avibe-bot/avibe/releases/download/git-runtime-v2.55.0-1/git-runtime.tar.gz"
MIRROR_URL = "https://dl.avibe.bot/releases/git-runtime-v2.55.0-1/git-runtime.tar.gz"


class _Stream:
    """A streamed HTTP response that can end by raising ``cut``."""

    def __init__(self, body: bytes, *, status: int = 200, headers=None, cut: BaseException | None = None) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self._cut = cut

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        chunk = self._body.read(size)
        if not chunk and self._cut is not None:
            raise self._cut
        return chunk


def _scripted_opener(steps: list):
    calls: list[tuple[str, str | None]] = []

    def opener(request, timeout):
        calls.append((request.full_url, request.get_header("Range")))
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    return opener, calls


class _Response:
    status = 200

    def __init__(self, body: bytes = b"ok", url: str = "https://example.test/final") -> None:
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url


def test_fetch_bytes_retries_transient_http_failure(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                "https://example.test/archive.tgz",
                503,
                "Unavailable",
                hdrs={},
                fp=None,
            )
        return _Response(b"archive")

    monkeypatch.setattr(dependency_network.time, "sleep", sleeps.append)

    result = dependency_network.fetch_bytes(
        "https://example.test/archive.tgz",
        timeout=5,
        opener=opener,
    )

    assert result == b"archive"
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_fetch_bytes_does_not_retry_missing_asset(monkeypatch) -> None:
    attempts = 0

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "https://example.test/missing.tgz",
            404,
            "Not Found",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(dependency_network.time, "sleep", lambda _delay: pytest.fail("404 must not retry"))

    with pytest.raises(dependency_network.DependencyNetworkError) as raised:
        dependency_network.fetch_bytes("https://example.test/missing.tgz", timeout=5, opener=opener)

    assert attempts == 1
    assert raised.value.details["http_status"] == 404
    assert raised.value.details["retryable"] is False
    assert raised.value.details["attempts"] == 1


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (urllib.error.URLError(socket.gaierror(-2, "not found")), "dns", True),
        (urllib.error.URLError(TimeoutError("timed out")), "timeout", True),
        (urllib.error.URLError(ssl.SSLCertVerificationError(1, "bad cert")), "tls", False),
        (ConnectionResetError("reset"), "network", True),
        (http.client.IncompleteRead(b"partial", 10), "network", True),
    ],
)
def test_dependency_error_details_classifies_retryability(exc, kind, retryable) -> None:
    details = dependency_network.dependency_error_details(exc, "https://user:secret@example.test/file?token=secret")

    assert details["kind"] == kind
    assert details["retryable"] is retryable
    assert details["url"] == "https://example.test/file"


def test_probe_uses_bounded_retry_and_reports_attempts(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr(dependency_network.time, "sleep", lambda _delay: None)

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError(TimeoutError("timed out"))

    result = dependency_network.probe_url("https://example.test/archive.tgz", opener=opener)

    assert result["ok"] is False
    assert result["checked"] is True
    assert result["download_error"]["kind"] == "timeout"
    assert result["download_error"]["attempts"] == 2


def test_probe_file_url_checks_local_file_without_http_opener(monkeypatch, tmp_path) -> None:
    archive = tmp_path / "runtime.tgz"
    archive.write_bytes(b"archive")
    monkeypatch.setattr(
        dependency_network.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("file probe must not use urlopen"),
    )

    result = dependency_network.probe_url(archive.as_uri())

    assert result["ok"] is True
    assert result["checked"] is True
    assert result["kind"] == "local_file"
    assert result["path"] == str(archive)


def test_probe_missing_file_url_reports_local_io_failure(monkeypatch, tmp_path) -> None:
    archive = tmp_path / "missing.tgz"
    monkeypatch.setattr(
        dependency_network.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("file probe must not use urlopen"),
    )

    result = dependency_network.probe_url(archive.as_uri())

    assert result["ok"] is False
    assert result["checked"] is True
    assert result["reason"] == "dependency_file_missing"
    assert result["download_error"]["kind"] == "io"
    assert result["download_error"]["retryable"] is False


def test_retry_after_is_capped_by_policy(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = 0

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                "https://example.test/archive.tgz",
                503,
                "Unavailable",
                hdrs={"Retry-After": "3600"},
                fp=None,
            )
        return _Response(b"archive")

    monkeypatch.setattr(dependency_network.time, "sleep", sleeps.append)

    result = dependency_network.fetch_bytes(
        "https://example.test/archive.tgz",
        timeout=5,
        opener=opener,
    )

    assert result == b"archive"
    assert sleeps == [4.0]


def test_missing_local_file_is_not_retried() -> None:
    details = dependency_network.dependency_error_details(
        urllib.error.URLError(FileNotFoundError("missing")),
        "file:///tmp/missing.tgz",
    )

    assert details["kind"] == "io"
    assert details["retryable"] is False


def test_dependency_error_message_reports_exhausted_attempts() -> None:
    message = dependency_network.dependency_error_message(
        {
            "kind": "timeout",
            "message": "Connection timed out",
            "url": "https://example.test/archive.tgz",
            "attempts": 3,
        },
        label="Runtime download",
    )

    assert message == "Runtime download failed after 3 attempts: Connection timed out (https://example.test/archive.tgz)"


@pytest.mark.parametrize(("repository", "root"), sorted(release_mirror.REPOSITORIES.items()))
def test_release_assets_of_every_mirrored_repository_try_the_mirror_first(repository, root) -> None:
    url = f"https://github.com/{repository}/releases/download/v1.0.0/tool_1.0.0%2Bbuild.tar.gz"

    assert dependency_network.download_sources(url) == [
        f"https://dl.avibe.bot/{root}releases/v1.0.0/tool_1.0.0%2Bbuild.tar.gz",
        url,
    ]


@pytest.mark.parametrize(
    "url",
    [
        f"{RELEASE_URL}?token=secret",
        "https://github.com/avibe-bot/avibe-docs/releases/download/v1/a.tgz",
    ],
)
def test_other_urls_keep_their_single_source(url) -> None:
    assert dependency_network.download_sources(url) == [url]


def test_mirror_miss_falls_back_to_github_and_later_downloads_start_there(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dependency_network, "_preferred_source", 0)
    monkeypatch.setattr(dependency_network.time, "sleep", lambda _delay: pytest.fail("fallback must not wait"))
    target = tmp_path / "asset.tmp"
    opener, calls = _scripted_opener(
        [
            urllib.error.HTTPError(MIRROR_URL, 404, "Not Found", hdrs={}, fp=None),
            _Stream(b"asset"),
            _Stream(b"again"),
        ]
    )

    dependency_network.fetch_to_path(RELEASE_URL, target, timeout=5, opener=opener)
    assert target.read_bytes() == b"asset"
    dependency_network.fetch_to_path(RELEASE_URL, target, timeout=5, opener=opener)

    assert target.read_bytes() == b"again"
    assert calls == [(MIRROR_URL, None), (RELEASE_URL, None), (RELEASE_URL, None)]


def test_downloads_name_an_agent_because_the_mirror_rejects_the_urllib_default(tmp_path) -> None:
    agents: list[str | None] = []

    def opener(request, timeout):
        agents.append(request.get_header("User-agent"))
        return _Stream(b"asset")

    dependency_network.fetch_to_path(RELEASE_URL, tmp_path / "asset.tmp", timeout=5, opener=opener)

    assert agents == ["avibe-dependency-download"]


def test_exhausted_sources_report_the_last_failure_and_remove_the_partial_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dependency_network, "_preferred_source", 0)
    sleeps: list[float] = []
    monkeypatch.setattr(dependency_network.time, "sleep", sleeps.append)
    target = tmp_path / "asset.tmp"
    opener, calls = _scripted_opener(
        [
            urllib.error.HTTPError(MIRROR_URL, 404, "Not Found", hdrs={}, fp=None),
            _Stream(b"ab", cut=TimeoutError("stalled")),
            TimeoutError("stalled"),
            TimeoutError("stalled"),
        ]
    )

    with pytest.raises(dependency_network.DependencyNetworkError) as raised:
        dependency_network.fetch_to_path(RELEASE_URL, target, timeout=5, opener=opener)

    assert calls == [(MIRROR_URL, None), (RELEASE_URL, None), (RELEASE_URL, "bytes=2-"), (RELEASE_URL, "bytes=2-")]
    assert sleeps == [1.0, 2.0]
    assert raised.value.details["url"] == RELEASE_URL
    assert raised.value.details["kind"] == "timeout"
    assert raised.value.details["attempts"] == 4
    assert not target.exists()


def test_next_round_waits_out_every_remaining_source_retry_window(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dependency_network, "_preferred_source", 0)
    sleeps: list[float] = []
    monkeypatch.setattr(dependency_network.time, "sleep", sleeps.append)
    opener, calls = _scripted_opener(
        [
            urllib.error.HTTPError(MIRROR_URL, 503, "Unavailable", hdrs={"Retry-After": "3"}, fp=None),
            TimeoutError("stalled"),
            _Stream(b"asset"),
        ]
    )

    dependency_network.fetch_to_path(RELEASE_URL, tmp_path / "asset.tmp", timeout=5, opener=opener)

    assert sleeps == [3.0]
    assert [url for url, _range in calls] == [MIRROR_URL, RELEASE_URL, MIRROR_URL]


class _Endless:
    """A source that never stops sending, and fails the test if read without bound."""

    status = 200
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.reads > 3:
            pytest.fail("download kept reading past its pinned size")
        return b"x" * size


def test_a_source_sending_more_than_the_pinned_size_is_cut_off_and_discarded(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dependency_network, "_preferred_source", 0)
    target = tmp_path / "asset.tmp"
    opener, calls = _scripted_opener([_Endless(), _Stream(b"abcdefghij")])

    dependency_network.fetch_to_path(RELEASE_URL, target, timeout=5, size=10, opener=opener)

    assert target.read_bytes() == b"abcdefghij"
    assert calls == [(MIRROR_URL, None), (RELEASE_URL, None)]


def _cut_after_abcd():
    return _Stream(b"abcd", cut=TimeoutError("stalled"))


def _resumed(content_range: str = "bytes 4-9/10", body: bytes = b"efghij"):
    return _Stream(body, status=206, headers={"Content-Range": content_range, "Content-Length": str(len(body))})


@pytest.mark.parametrize(
    ("steps", "expected_calls"),
    [
        pytest.param(
            [_cut_after_abcd(), _resumed()],
            [(MIRROR_URL, None), (RELEASE_URL, "bytes=4-")],
            id="stall-resumes-on-next-source",
        ),
        pytest.param(
            [_Stream(b"abcd", headers={"Content-Length": "10"}), _resumed()],
            [(MIRROR_URL, None), (RELEASE_URL, "bytes=4-")],
            id="early-close-is-not-a-complete-file",
        ),
        pytest.param(
            [_cut_after_abcd(), _Stream(b"abcdefghij")],
            [(MIRROR_URL, None), (RELEASE_URL, "bytes=4-")],
            id="range-ignored-restarts",
        ),
        pytest.param(
            [_cut_after_abcd(), _resumed("bytes 0-9/10", b"abcdefghij"), _resumed()],
            [(MIRROR_URL, None), (RELEASE_URL, "bytes=4-"), (MIRROR_URL, "bytes=4-")],
            id="wrong-offset-is-not-spliced",
        ),
        pytest.param(
            [_cut_after_abcd(), _resumed("bytes 4-5/10", b"ef"), _resumed("bytes 6-9/10", b"ghij")],
            [(MIRROR_URL, None), (RELEASE_URL, "bytes=4-"), (MIRROR_URL, "bytes=6-")],
            id="partial-range-continues-to-the-file-end",
        ),
    ],
)
def test_interrupted_download_resumes_without_corrupting_the_file(monkeypatch, tmp_path, steps, expected_calls) -> None:
    monkeypatch.setattr(dependency_network, "_preferred_source", 0)
    monkeypatch.setattr(dependency_network.time, "sleep", lambda _delay: None)
    target = tmp_path / "asset.tmp"
    opener, calls = _scripted_opener(list(steps))

    dependency_network.fetch_to_path(RELEASE_URL, target, timeout=5, opener=opener)

    assert target.read_bytes() == b"abcdefghij"
    assert calls == expected_calls


def test_probe_reports_a_release_asset_reachable_through_github_when_the_mirror_is_not(monkeypatch) -> None:
    monkeypatch.setattr(dependency_network.time, "sleep", lambda _delay: None)
    opener, calls = _scripted_opener(
        [
            urllib.error.URLError(socket.gaierror(-2, "not found")),
            urllib.error.URLError(socket.gaierror(-2, "not found")),
            _Response(url=RELEASE_URL),
        ]
    )

    result = dependency_network.probe_url(RELEASE_URL, opener=opener)

    assert result["ok"] is True
    assert result["url"] == RELEASE_URL
    assert [url for url, _range in calls] == [MIRROR_URL, MIRROR_URL, RELEASE_URL]
