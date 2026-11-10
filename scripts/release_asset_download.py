"""Stdlib-only, API-first release downloads for build/CI scripts."""

from __future__ import annotations

import errno
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import json
import logging
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

_T = TypeVar("_T")
_MAX_ATTEMPTS = 3
_REQUEST_TIMEOUT = 10.0
_RETRY_WINDOW = 30.0


class _GhReadError(RuntimeError):
    def __init__(self, message: str, *, transient: bool, retry_after: float = 0.0):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


def _secondary_limit(exc: urllib.error.HTTPError) -> bool:
    # Classification is used by both API fallback and retry; consume a 403's
    # error body at most once, before closing it. Never classify release data.
    if not hasattr(exc, "_secondary_limit"):
        try:
            body = exc.read(8192).decode("utf-8", errors="replace").lower()
        except (OSError, http.client.HTTPException):
            body = ""
        exc._secondary_limit = "secondary rate limit" in body or "abuse detection mechanism" in body
    return exc._secondary_limit


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _server_delay(exc: Exception) -> float:
    if isinstance(exc, _GhReadError):
        return exc.retry_after
    if isinstance(exc, urllib.error.HTTPError):
        hint = _retry_after(exc.headers.get("Retry-After")) if exc.headers else None
        if hint is not None:
            return hint
        # GitHub asks for at least a minute without an explicit server delay.
        # Fail clearly when that cannot fit, rather than hammering a rate limit.
        if exc.code == 403 and _secondary_limit(exc):
            return 60.0
        if exc.code == 429 and urllib.parse.urlparse(exc.url).hostname == "api.github.com":
            return 60.0
    return 0.0


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, _GhReadError):
        return exc.transient
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600 or (exc.code == 403 and _secondary_limit(exc))
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (ConnectionResetError, TimeoutError, http.client.RemoteDisconnected,
                               http.client.IncompleteRead)) or (
        isinstance(reason, OSError) and reason.errno in {errno.ECONNRESET, errno.ETIMEDOUT}
    )


def _with_retry(operation: Callable[[], _T], *, deadline: float) -> _T:
    # The window bounds retries, not healthy large transfers; urllib's timeout
    # is a socket inactivity limit, not a transfer deadline.
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            transient = _retryable(exc)
            server_delay = _server_delay(exc) if transient else 0.0
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if not transient or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = max(server_delay, 2.0**attempt + random.uniform(0.0, 0.5))
            if time.monotonic() + delay >= deadline:
                if server_delay:
                    raise RuntimeError(f"GitHub rate limit exceeds the {_RETRY_WINDOW:g}s retry budget; retry later: {exc}") from exc
                raise
            detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            logging.getLogger(__name__).warning(
                "GitHub/release read attempt %d/%d failed (%s); retrying in %.1fs",
                attempt + 1, _MAX_ATTEMPTS, detail, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def run_gh_read(*args: str, **kwargs) -> str:
    """Run the queue's read-only gh operations within one hard wall budget.

    A failed paginated response is discarded, not concatenated with a retry.
    Callers must validate successful JSON/digests outside this transport loop.
    """
    api_read = args and args[0] == "api"
    asset_read = args[:2] == ("release", "download")
    # gh changes to POST when fields are present. Do not let a future caller
    # accidentally replay a mutation through this read-only helper.
    if (not (api_read or asset_read) or (api_read and any(
            arg.startswith(("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"))
            for arg in args[1:]))):
        raise ValueError("GitHub retries support read-only API and release download commands only")
    if asset_read and "--clobber" not in args:
        raise ValueError("Retried release downloads require --clobber in a test-owned directory")
    deadline = time.monotonic() + _RETRY_WINDOW
    attempt = 0

    def operation() -> str:
        nonlocal attempt
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"GitHub read exhausted its {_RETRY_WINDOW:g}s retry budget")
        try:
            result = subprocess.run(["gh", *args], capture_output=True, text=True,
                                    timeout=remaining, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise _GhReadError(f"GitHub read exceeded its {_RETRY_WINDOW:g}s retry budget",
                               transient=True) from exc
        if result.returncode == 0:
            return result.stdout
        detail = result.stderr.strip() or f"gh exited {result.returncode} without an error message"
        status = re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+(\d{3})\b", detail, re.IGNORECASE)
        code = int(status[1]) if status else None
        secondary = "secondary rate limit" in detail.lower() or "abuse detection mechanism" in detail.lower()
        limited = code == 429 or (secondary and code in (None, 403))
        network = re.search(
            r"unexpected EOF|\bEOF$|connection (?:reset|refused)|i/o timeout|"
            r"TLS handshake timeout|context deadline exceeded|temporary failure in name resolution|"
            r"network is unreachable|error connecting to api\.github\.com", detail, re.IGNORECASE,
        )
        transient = limited or (code is not None and 500 <= code < 600) or (code is None and bool(network))
        hint = re.search(r"(?im)^Retry-After:\s*(.+)$", detail)
        delay = _retry_after(hint[1]) if hint else None
        if limited and delay is None:
            delay = 60.0
        raise _GhReadError(
            f"GitHub read failed (attempt {attempt}/{_MAX_ATTEMPTS}, exit {result.returncode}): {detail[:1000]}",
            transient=transient, retry_after=delay or 0.0,
        )

    return _with_retry(operation, deadline=deadline)


def _request(url: str, *, accept: str = "application/octet-stream") -> urllib.request.Request:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "avibe-release-downloader/1"})
    _authorize_api(request)
    return request


def _authorize_api(request: urllib.request.Request) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    parsed = urllib.parse.urlparse(request.full_url)
    if token and parsed.scheme == "https" and parsed.netloc == "api.github.com":
        # GitHub redirects asset API downloads to a signed storage URL. Never
        # forward the API credential to that host (or any other redirect).
        request.add_unredirected_header("Authorization", f"Bearer {token}")


def _github_release(url: str) -> tuple[str, str, str] | None:
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.split("/")
    if (parsed.scheme not in {"http", "https"} or parsed.netloc != "github.com"
            or len(parts) != 7 or parts[3:5] != ["releases", "download"]
            or not all(parts[1:]) or any(part in {".", ".."} for part in parts[1:3])):
        return None
    _, owner, repository, _, _, tag, name = parts
    api_root = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repository, safe='')}/releases"
    )
    return api_root, urllib.parse.unquote(tag), urllib.parse.unquote(name)


def _asset_api_request(
    url: str,
    asset: Mapping[str, Any] | None,
    open_response: Callable[..., Any],
) -> urllib.request.Request | None:
    release_path = _github_release(url)
    if release_path is None:
        return None
    api_root, tag, name = release_path
    if asset is None:
        lookup = _request(f"{api_root}/tags/{urllib.parse.quote(tag, safe='')}", accept="application/vnd.github+json")
        with open_response(lookup) as response:
            try:
                release = json.loads(response.read())
            except (ValueError, UnicodeDecodeError):
                return None
        if not isinstance(release, dict) or release.get("tag_name") != tag:
            return None
        assets = release.get("assets")
        if not isinstance(assets, list):
            return None
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
        if len(matches) != 1:
            return None
        asset = matches[0]
    asset_id = asset.get("id")
    if (asset.get("name") != name or asset.get("browser_download_url") != url
            or not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0):
        return None
    # Construct the API URL from a validated id, never trust a metadata-supplied
    # URL with a bearer token. All digest/size validation remains with callers.
    return _request(f"{api_root}/assets/{asset_id}")


def _with_response(
    request: str | urllib.request.Request,
    consume: Callable[[Any], _T],
    *,
    opener: Callable[..., Any] | None = None,
    release_asset: Mapping[str, Any] | None = None,
) -> _T:
    request = _request(request) if isinstance(request, str) else request
    _authorize_api(request)
    resolved_opener = opener or urllib.request.urlopen
    deadline = time.monotonic() + _RETRY_WINDOW
    api_allowed = _github_release(request.full_url) is not None
    api_request = None

    def open_response(candidate: urllib.request.Request):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("release download retry window exhausted")
        return resolved_opener(candidate, timeout=min(_REQUEST_TIMEOUT, remaining))

    def transfer() -> _T:
        nonlocal api_allowed, api_request
        if api_allowed:
            try:
                if api_request is None:
                    api_request = _asset_api_request(request.full_url, release_asset, open_response)
                    api_allowed = api_request is not None
                if api_request is not None:
                    with open_response(api_request) as response:
                        return consume(response)
            except Exception as exc:
                # A transport failure makes the alternate browser route useful;
                # a size/digest/local-write failure does not. Permanent API 4xx
                # is never requested again during retries of the browser route.
                if not (_retryable(exc) or isinstance(exc, urllib.error.URLError)):
                    raise
                # A throttled API must not be replayed early if the public
                # asset route also needs a retry. The public route remains a
                # separate, unauthenticated fallback with the same byte checks.
                if _server_delay(exc) or not _retryable(exc):
                    api_allowed = False
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                logging.getLogger(__name__).warning("Release asset API unavailable; trying browser download URL")
        with open_response(request) as response:
            return consume(response)

    # API discovery, asset transfer and fallback share one elapsed retry window,
    # not nested independent retry loops. A healthy API never hits browser URLs.
    return _with_retry(transfer, deadline=deadline)


def read_bytes(
    request: str | urllib.request.Request,
    *,
    opener: Callable[..., Any] | None = None,
    release_asset: Mapping[str, Any] | None = None,
) -> bytes:
    """Read fresh bytes; reuse release metadata when the caller already has it."""
    return _with_response(request, lambda response: response.read(), opener=opener, release_asset=release_asset)


def download_to_path(
    request: str | urllib.request.Request,
    destination: Path,
    *,
    max_bytes: int | None = None,
    release_asset: Mapping[str, Any] | None = None,
) -> None:
    """Stream each attempt from byte zero, never retaining an incomplete file."""
    def download(response: Any) -> None:
        started = False
        try:
            with destination.open("wb") as output:
                started = True
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if max_bytes is not None and downloaded > max_bytes:
                        raise ValueError("release asset exceeds its size limit")
                    output.write(chunk)
        except Exception:
            if started:
                destination.unlink(missing_ok=True)
            raise

    _with_response(request, download, release_asset=release_asset)
