#!/usr/bin/env python3
"""DRAFT-ONLY CI PROBE (not for merge): a stand-in instance for the background
voice intent. It speaks just enough of the instance API, requires the session
cookie that /probe/login set, and logs every request as one JSON line."""

from __future__ import annotations

import json
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = open(sys.argv[2] if len(sys.argv) > 2 else "/dev/stdout", "a", buffering=1)
SESSION = "probe-session"
CSRF = "probe-csrf"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _cookies(self) -> dict[str, str]:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in jar.items()}

    def _send(self, status: int, body: dict | str, cookies: list[str] | None = None, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _log(self, status: int, extra: dict | None = None):
        cookies = self._cookies()
        LOG.write(json.dumps({
            "t": round(time.time(), 3),
            "method": self.command,
            "path": self.path,
            "status": status,
            "session_cookie": cookies.get("vibe_probe_session") == SESSION,
            "origin": self.headers.get("Origin"),
            "csrf_ok": self.headers.get("X-Vibe-CSRF-Token") == CSRF and cookies.get("vibe_csrf_token") == CSRF,
            "user_agent": self.headers.get("User-Agent", "")[:80],
            **(extra or {}),
        }) + "\n")

    def _authorized(self) -> bool:
        return self._cookies().get("vibe_probe_session") == SESSION

    def do_GET(self):
        if self.path == "/probe/login":
            self._log(200)
            return self._send(200, "<!doctype html><title>probe</title>signed in", [
                f"vibe_probe_session={SESSION}; Max-Age=86400; Path=/; HttpOnly; SameSite=Lax",
            ], ctype="text/html")
        if self.path == "/api/csrf-token":
            self._log(200)
            return self._send(200, {"ok": True, "csrf_token": CSRF}, [
                f"vibe_csrf_token={CSRF}; Path=/; SameSite=Strict",
            ])
        if not self._authorized():
            self._log(401)
            return self._send(401, {"ok": False, "error": "remote_access_login_required"})
        if self.path == "/api/asr/status":
            self._log(200)
            return self._send(200, {"available": True, "max_file_bytes": 26214400, "consent": "granted"})
        if self.path == "/api/sessions/main":
            self._log(200)
            return self._send(200, {"session": {"id": "ses_main"}, "capabilities": {"can_chat": True}})
        self._log(404)
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if not self._authorized():
            self._log(401)
            return self._send(401, {"ok": False, "error": "remote_access_login_required"})
        if self.headers.get("X-Vibe-CSRF-Token") != CSRF:
            self._log(403)
            return self._send(403, {"ok": False, "message": "Forbidden: invalid csrf token"})
        if self.path == "/api/asr/transcribe":
            audio = payload.get("data") or ""
            self._log(200, {"audio_base64_chars": len(audio), "mime": payload.get("mime")})
            return self._send(200, {"text": "probe transcript"})
        if self.path == "/api/sessions/ses_main/messages":
            self._log(200, {"text": payload.get("text"), "metadata": payload.get("metadata")})
            return self._send(200, {"ok": True})
        self._log(404)
        return self._send(404, {"error": "not_found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
