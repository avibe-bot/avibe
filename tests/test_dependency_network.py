from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error

import pytest

from core import dependency_network


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
