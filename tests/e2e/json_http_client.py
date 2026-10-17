"""HTTP helpers for UI E2E tests with CSRF support."""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request


class JsonHttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self._csrf_token: str | None = None

    def _request(self, path: str, *, method: str = "GET", body: dict | None = None, timeout: int = 5):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        headers["Origin"] = self.base_url
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["X-Vibe-CSRF-Token"] = self.csrf_token(timeout=timeout)

        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        return self.opener.open(request, timeout=timeout)

    def csrf_token(self, timeout: int = 5) -> str:
        if self._csrf_token:
            return self._csrf_token

        response = self.opener.open(f"{self.base_url}/api/csrf-token", timeout=timeout)
        payload = json.loads(response.read())
        self._csrf_token = payload["csrf_token"]
        return self._csrf_token

    def get_json(self, path: str, timeout: int = 5):
        response = self._request(path, timeout=timeout)
        return response.status, json.loads(response.read())

    def post_json(self, path: str, body: dict | None = None, timeout: int = 5):
        response = self._request(path, method="POST", body=body or {}, timeout=timeout)
        return response.status, json.loads(response.read())

    def delete_json(self, path: str, timeout: int = 5):
        response = self._request(path, method="DELETE", timeout=timeout)
        return response.status, json.loads(response.read())

    def expect_error(self, path: str, *, method: str = "DELETE", timeout: int = 5):
        try:
            self._request(path, method=method, timeout=timeout)
            raise AssertionError(f"Expected HTTP error for {method} {path}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
