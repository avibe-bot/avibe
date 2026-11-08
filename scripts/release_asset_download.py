"""Stdlib-only, API-first release downloads for build/CI scripts."""

from __future__ import annotations

import errno
import http.client
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

_T = TypeVar("_T")
_MAX_ATTEMPTS = 4
_REQUEST_TIMEOUT = 10.0
_RETRY_WINDOW = 45.0


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
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
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if not _retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = 2.0**attempt + random.uniform(0.0, 0.5)
            if time.monotonic() + delay >= deadline:
                raise
            detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            logging.getLogger(__name__).warning(
                "Release download attempt %d/%d failed (%s); retrying in %.1fs",
                attempt + 1, _MAX_ATTEMPTS, detail, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


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
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if not _retryable(exc):
                    api_allowed = False
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
