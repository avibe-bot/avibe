#!/usr/bin/env python3
"""Small Avibe Web UI API helper for agents.

This helper intentionally uses only the Python standard library so it can run
inside local machines and regression containers without installing packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_BASE = "http://127.0.0.1:5123"


class VibeApiClient:
    def __init__(self, base_url: str, cookie_jar_path: Path | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookie_jar_path = cookie_jar_path or self._default_cookie_jar_path(self.base_url)
        self.cookie_jar_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_jar = MozillaCookieJar(str(self.cookie_jar_path))
        if self.cookie_jar_path.exists():
            try:
                self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                # Stale or corrupt cookie jars should not block maintenance.
                pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))

    @staticmethod
    def _default_cookie_jar_path(base_url: str) -> Path:
        digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"vibe_api_{digest}.cookies"

    def call(self, method: str, path: str, payload: Any = None) -> Any:
        method = method.upper()
        url = urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        # Protected reads need the same-origin evidence mutations do.
        headers = {"Accept": "application/json", "Origin": self.base_url}
        if method in MUTATING_METHODS:
            headers["Content-Type"] = "application/json"
            headers["X-Vibe-CSRF-Token"] = self.get_csrf_token()

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = body or exc.reason
            raise SystemExit(f"HTTP {exc.code} {method} {path}: {message}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Request failed {method} {path}: {exc.reason}") from exc

    def get_csrf_token(self) -> str:
        payload = self.call("GET", "/api/csrf-token")
        token = payload.get("csrf_token") if isinstance(payload, dict) else None
        if not token:
            raise SystemExit("Missing csrf_token from /api/csrf-token")
        return str(token)


def _load_payload(value: str | None) -> Any:
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Call the local Avibe Web UI API with CSRF handling.",
    )
    parser.add_argument("method", help="HTTP method, for example GET, POST, DELETE")
    parser.add_argument("path", help="API path, for example /api/settings?platform=slack")
    parser.add_argument(
        "payload",
        nargs="?",
        help="JSON payload, @file.json, or - for stdin. Omit for GET/DELETE without a body.",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("VIBE_UI_BASE", DEFAULT_BASE),
        help=f"Web UI base URL. Defaults to VIBE_UI_BASE or {DEFAULT_BASE}.",
    )
    parser.add_argument(
        "--cookie-jar",
        default=os.environ.get("VIBE_API_COOKIE_JAR"),
        help="Cookie jar path. Defaults to a per-base temp file.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds.")
    args = parser.parse_args(argv)

    cookie_jar_path = Path(args.cookie_jar).expanduser() if args.cookie_jar else None
    client = VibeApiClient(args.base, cookie_jar_path=cookie_jar_path, timeout=args.timeout)
    payload = _load_payload(args.payload)
    result = client.call(args.method, args.path, payload)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
