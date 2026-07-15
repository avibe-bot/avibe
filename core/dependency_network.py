"""Shared network reliability and diagnostics for managed dependencies."""

from __future__ import annotations

import errno
import http.client
import logging
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_delay: float
    max_delay: float


DOWNLOAD_RETRY_POLICY = RetryPolicy(max_attempts=3, initial_delay=1.0, max_delay=4.0)
PROBE_RETRY_POLICY = RetryPolicy(max_attempts=2, initial_delay=0.5, max_delay=0.5)


class DependencyNetworkError(RuntimeError):
    """A dependency request that exhausted its retry policy."""

    def __init__(self, details: dict[str, Any]):
        self.details = details
        super().__init__(str(details.get("message") or "Dependency network request failed"))


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https", "file"}:
        return f"{parsed.scheme}:" if parsed.scheme else ""
    if parsed.scheme == "file":
        return urllib.parse.urlunparse((parsed.scheme, "", parsed.path, "", "", ""))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port else hostname
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def dependency_error_details(exc: BaseException, url: str, *, attempts: int = 1) -> dict[str, Any]:
    if isinstance(exc, DependencyNetworkError):
        return dict(exc.details)

    safe_url = redact_url(url)
    host = urllib.parse.urlparse(url).hostname
    error: dict[str, Any] = {
        "kind": "unknown",
        "message": "Unexpected dependency request failure",
        "url": safe_url,
        "host": host,
        "exception_type": type(exc).__name__,
        "retryable": False,
        "attempts": attempts,
    }
    if isinstance(exc, urllib.error.HTTPError):
        retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
        error.update(
            kind="http",
            message=f"HTTP {exc.code} {exc.reason or ''}".strip(),
            http_status=exc.code,
            retryable=retryable,
        )
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            error["retry_after_seconds"] = retry_after
        return error

    root = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    error["exception_type"] = type(root).__name__
    if isinstance(root, socket.gaierror):
        error.update(kind="dns", message=f"DNS lookup failed for {host or 'dependency host'}", retryable=True)
    elif isinstance(root, (ssl.SSLCertVerificationError, ssl.CertificateError)):
        error.update(kind="tls", message="TLS certificate verification failed")
    elif isinstance(root, ssl.SSLError):
        error.update(kind="tls", message="TLS negotiation failed")
    elif isinstance(root, (TimeoutError, socket.timeout)):
        error.update(kind="timeout", message="Connection timed out", retryable=True)
    elif isinstance(root, PermissionError):
        error.update(kind="permission", message="Permission denied while storing the dependency")
    elif isinstance(root, OSError) and root.errno == errno.ENOSPC:
        error.update(kind="disk", message="No space left while storing the dependency")
    elif isinstance(root, (ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected)):
        error.update(kind="network", message=f"Network request failed ({type(root).__name__})", retryable=True)
    elif isinstance(root, OSError) and root.errno in {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    }:
        error.update(kind="network", message=f"Network request failed ({type(root).__name__})", retryable=True)
    elif urllib.parse.urlparse(url).scheme == "file" and isinstance(root, OSError):
        error.update(kind="io", message=f"Local dependency file access failed ({type(root).__name__})")
    elif isinstance(exc, urllib.error.URLError):
        error.update(kind="network", message=f"Network request failed ({type(root).__name__})", retryable=True)
    elif isinstance(root, OSError):
        error.update(kind="io", message=f"Local or network I/O failed ({type(root).__name__})")
    return error


def dependency_error_message(error: dict[str, Any] | None, *, label: str) -> str:
    details = error or {}
    attempts = int(details.get("attempts") or 1)
    attempt_text = f" after {attempts} attempts" if attempts > 1 else ""
    message = str(details.get("message") or details.get("kind") or "request failed")
    url = str(details.get("url") or "")
    suffix = f" ({url})" if url else ""
    return f"{label} failed{attempt_text}: {message}{suffix}"


def fetch_bytes(
    request: str | urllib.request.Request,
    *,
    timeout: float,
    policy: RetryPolicy = DOWNLOAD_RETRY_POLICY,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    resolved_opener = opener or urllib.request.urlopen

    def operation() -> bytes:
        with resolved_opener(request, timeout=timeout) as response:
            return response.read()

    return _run_with_retry(request, operation, policy=policy)


def fetch_to_path(
    request: str | urllib.request.Request,
    target: Path,
    *,
    timeout: float,
    policy: RetryPolicy = DOWNLOAD_RETRY_POLICY,
    opener: Callable[..., Any] | None = None,
) -> None:
    resolved_opener = opener or urllib.request.urlopen

    def operation() -> None:
        _unlink_quietly(target)
        with resolved_opener(request, timeout=timeout) as response, target.open("wb") as destination:
            shutil.copyfileobj(response, destination)

    try:
        _run_with_retry(request, operation, policy=policy)
    except BaseException:
        _unlink_quietly(target)
        raise


def probe_url(
    url: str,
    *,
    timeout: float = 10.0,
    policy: RetryPolicy = PROBE_RETRY_POLICY,
    opener: Callable[..., Any] | None = None,
    user_agent: str = "avibe-dependency-doctor",
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
        if path.is_file():
            return {
                "ok": True,
                "checked": True,
                "kind": "local_file",
                "url": redact_url(url),
                "path": str(path),
                "reason": None,
            }
        error = dependency_error_details(FileNotFoundError(errno.ENOENT, "dependency file not found", path), url)
        return {
            "ok": False,
            "checked": True,
            "kind": "local_file",
            "url": redact_url(url),
            "path": str(path),
            "reason": "dependency_file_missing",
            "download_error": error,
        }

    request = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="HEAD")
    resolved_opener = opener or urllib.request.urlopen

    def operation() -> tuple[int, str]:
        with resolved_opener(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            final_url = response.geturl() if hasattr(response, "geturl") else url
        return int(status_code), str(final_url)

    try:
        status_code, final_url = _run_with_retry(request, operation, policy=policy)
    except DependencyNetworkError as exc:
        error = dict(exc.details)
        if error.get("kind") == "http" and error.get("http_status") in {405, 501}:
            return {
                "ok": False,
                "checked": False,
                "kind": "head_unsupported",
                "http_status": error.get("http_status"),
                "url": redact_url(url),
                "reason": "dependency_probe_unsupported",
                "download_error": error,
            }
        return {
            "ok": False,
            "checked": True,
            "reason": "dependency_download_failed",
            "download_error": error,
        }
    return {
        "ok": 200 <= status_code < 400,
        "checked": True,
        "kind": "reachable",
        "http_status": status_code,
        "url": redact_url(url),
        "final_host": urllib.parse.urlparse(final_url).hostname,
        "reason": None,
    }


T = TypeVar("T")


def _run_with_retry(
    request: str | urllib.request.Request,
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
) -> T:
    if policy.max_attempts < 1:
        raise ValueError("retry policy must allow at least one attempt")
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001
            details = dependency_error_details(exc, url, attempts=attempt)
            close = getattr(exc, "close", None)
            if callable(close):
                close()
            if not details.get("retryable") or attempt >= policy.max_attempts:
                raise DependencyNetworkError(details) from exc
            delay = details.get("retry_after_seconds")
            if not isinstance(delay, (int, float)):
                delay = min(policy.initial_delay * (2 ** (attempt - 1)), policy.max_delay)
            delay = min(max(0.0, float(delay)), policy.max_delay)
            logger.warning(
                "Dependency request attempt %d/%d failed for %s (%s); retrying in %.1fs",
                attempt,
                policy.max_attempts,
                redact_url(url),
                details.get("kind"),
                delay,
            )
            time.sleep(delay)
    raise AssertionError("retry loop exhausted without returning or raising")


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
