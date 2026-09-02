"""Hermetic real-HTTP Avibe runtime for Model Hub E2E scenarios."""

from __future__ import annotations

import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
)


_MISSING = object()
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAFE_INHERITED_ENV = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
)
_UI_PORT_START_ATTEMPTS = 3


@dataclass(frozen=True)
class HTTPResult:
    """One complete HTTP response, including non-2xx responses."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = self.body[:500].decode("utf-8", errors="replace")
            raise AssertionError(
                f"HTTP {self.status} did not return JSON: {preview!r}"
            ) from exc


class ModelHubHTTPClient:
    """Cookie- and CSRF-aware client for the browser-visible API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._cookies = http.cookiejar.CookieJar()
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(self._cookies),
        )
        self._csrf_token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: object = _MISSING,
        raw_body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20,
    ) -> HTTPResult:
        method = method.upper()
        if json_body is not _MISSING and raw_body is not None:
            raise ValueError("json_body and raw_body are mutually exclusive")
        request_headers = dict(headers or {})
        body = raw_body
        if json_body is not _MISSING:
            body = json.dumps(
                json_body,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if method in _MUTATION_METHODS:
            request_headers.setdefault("Origin", self.base_url)
            request_headers.setdefault(
                "X-Vibe-CSRF-Token", self._csrf()
            )
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            return HTTPResult(
                status=response.status,
                headers=dict(response.headers),
                body=response.read(),
            )

    def get(self, path: str, *, timeout: float = 20) -> HTTPResult:
        return self.request("GET", path, timeout=timeout)

    def post(
        self, path: str, json_body: object = _MISSING
    ) -> HTTPResult:
        return self.request("POST", path, json_body=json_body)

    def put(
        self, path: str, json_body: object = _MISSING
    ) -> HTTPResult:
        return self.request("PUT", path, json_body=json_body)

    def patch(
        self, path: str, json_body: object = _MISSING
    ) -> HTTPResult:
        return self.request("PATCH", path, json_body=json_body)

    def delete(
        self, path: str, json_body: object = _MISSING
    ) -> HTTPResult:
        return self.request("DELETE", path, json_body=json_body)

    def _csrf(self) -> str:
        if self._csrf_token is None:
            response = self.get("/api/csrf-token")
            if response.status != 200:
                raise RuntimeError(
                    f"CSRF bootstrap returned HTTP {response.status}"
                )
            payload = response.json()
            token = payload.get("csrf_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("CSRF bootstrap returned no token")
            self._csrf_token = token
        return self._csrf_token


class ModelHubTestApp:
    """Start isolated controller and UI processes against test-owned state."""

    def __init__(
        self,
        repo_root: Path,
        runtime_root: Path,
        *,
        enabled: bool = True,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.enabled = enabled
        self.port = _ephemeral_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.client = ModelHubHTTPClient(self.base_url)
        self.home = self.runtime_root / "home"
        self.avibe_home = self.runtime_root / "avibe-home"
        self.logs = self.runtime_root / "process-logs"
        self.env = self._environment(extra_env or {})
        self._controller: subprocess.Popen[bytes] | None = None
        self._ui: subprocess.Popen[bytes] | None = None
        self._controller_log = None
        self._ui_log = None
        self._ui_log_offset = 0

    def _environment(
        self, extra_env: Mapping[str, str]
    ) -> dict[str, str]:
        env = {
            name: os.environ[name]
            for name in _SAFE_INHERITED_ENV
            if os.environ.get(name)
        }
        env.update(
            {
                "PATH": os.pathsep.join(
                    dict.fromkeys(
                        (
                            str(Path(sys.executable).resolve().parent),
                            *os.defpath.split(os.pathsep),
                        )
                    )
                ),
                "HOME": str(self.home),
                "AVIBE_HOME": str(self.avibe_home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "TMPDIR": str(self.runtime_root / "tmp"),
                "TMP": str(self.runtime_root / "tmp"),
                "TEMP": str(self.runtime_root / "tmp"),
                "CODEX_HOME": str(self.home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "VIBE_MODEL_HUB_ENABLED": "1" if self.enabled else "0",
                "VIBE_MODEL_HUB_ENGINE_OFFLINE": "1",
                "VIBE_MEMORY_OFFLINE": "1",
                "VIBE_SHOW_RUNTIME_OFFLINE": "1",
                "VIBE_TMUX_OFFLINE": "1",
                "VIBE_GIT_OFFLINE": "1",
                "VIBE_STARTUP_DEPENDENCY_RECONCILE": "0",
                "VIBE_INSTALL_SKIP_ASKILL": "1",
                "VIBE_ASKILL_AUTO_UPDATE": "0",
                "VIBE_DISABLE_STDOUT_LOGGING": "1",
                "VIBE_INTERNAL_DISPATCH_SOCKET": str(
                    self.runtime_root / "dispatch.sock"
                ),
                "VIBE_SENTRY_DSN": "",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        env.update(extra_env)
        return env

    def start(self) -> "ModelHubTestApp":
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.avibe_home.mkdir(parents=True, exist_ok=True)
            self.logs.mkdir(parents=True, exist_ok=True)
            (self.runtime_root / "tmp").mkdir(
                parents=True, exist_ok=True
            )
            self._initialize_config()
            self._start_controller()
            self._start_ui_with_port_retry()
        except BaseException as exc:
            self.stop()
            if not isinstance(exc, Exception):
                raise
            details = self.diagnostics()
            raise RuntimeError(
                f"hermetic Model Hub app failed to start\n{details}"
            ) from exc
        return self

    def _initialize_config(self) -> None:
        config_path = self.avibe_home / "config" / "config.json"
        if config_path.exists():
            return
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config.v2_config import V2Config; "
                "config = V2Config.default(); "
                "config.update.auto_update = False; "
                "config.update.check_interval_minutes = 0; "
                "config.save()",
            ],
            cwd=self.repo_root,
            env=self.env,
            capture_output=True,
            timeout=20,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            raise RuntimeError(
                f"could not initialize hermetic config: {output}"
            )

    def _start_controller(self) -> None:
        if self._controller is not None:
            raise RuntimeError("controller is already running")
        self._controller_log = (self.logs / "controller.log").open("ab")
        self._controller = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=self.repo_root,
            env=self.env,
            stdout=self._controller_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _start_ui(self) -> None:
        if self._ui is not None:
            raise RuntimeError("UI is already running")
        log_path = self.logs / "ui.log"
        self._ui_log_offset = (
            log_path.stat().st_size if log_path.exists() else 0
        )
        self._ui_log = log_path.open("ab")
        expression = (
            "from vibe.ui_server import run_ui_server; "
            f"run_ui_server('127.0.0.1', {self.port})"
        )
        self._ui = subprocess.Popen(
            [sys.executable, "-c", expression],
            cwd=self.repo_root,
            env=self.env,
            stdout=self._ui_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _start_ui_with_port_retry(self) -> None:
        for attempt in range(_UI_PORT_START_ATTEMPTS):
            self._start_ui()
            try:
                self.wait_ready()
            except RuntimeError:
                exhausted = attempt + 1 >= _UI_PORT_START_ATTEMPTS
                if exhausted or not self._ui_lost_address_in_use_race():
                    raise
                self._stop_ui()
                self.port = _ephemeral_port()
                self.base_url = f"http://127.0.0.1:{self.port}"
                self.client = ModelHubHTTPClient(self.base_url)
            else:
                return
        raise AssertionError("UI port retry loop exhausted without a result")

    def _ui_lost_address_in_use_race(self) -> bool:
        if (
            self._controller is None
            or self._controller.poll() is not None
            or self._ui is None
            or self._ui.poll() is None
        ):
            return False
        try:
            output = (self.logs / "ui.log").read_bytes()[
                self._ui_log_offset :
            ].lower()
        except OSError:
            return False
        return any(
            marker in output
            for marker in (
                b"address already in use",
                b"errno 48",
                b"errno 98",
            )
        )

    def wait_ready(self, *, timeout: float = 40) -> None:
        deadline = time.monotonic() + timeout
        latest: str | None = None
        while time.monotonic() < deadline:
            self._assert_processes_alive()
            try:
                response = self.client.get(
                    "/api/models/sources", timeout=1
                )
                payload = response.json()
                if self.enabled:
                    if response.status == 200 and payload.get("ok") is True:
                        return
                elif (
                    response.status == 404
                    and payload.get("error") == "feature_disabled"
                ):
                    return
                latest = f"HTTP {response.status}: {payload!r}"
            except (AssertionError, OSError, URLError) as exc:
                latest = repr(exc)
            time.sleep(0.1)
        raise TimeoutError(
            "Model Hub HTTP/IPC readiness timed out"
            + (f": {latest}" if latest else "")
        )

    def restart_controller(self) -> None:
        self._stop_process(self._controller)
        self._controller = None
        if self._controller_log is not None:
            self._controller_log.close()
            self._controller_log = None
        self._start_controller()
        self.wait_ready()

    def _assert_processes_alive(self) -> None:
        for label, process in (
            ("controller", self._controller),
            ("UI", self._ui),
        ):
            if process is None or process.poll() is not None:
                code = None if process is None else process.returncode
                raise RuntimeError(f"{label} exited with status {code}")

    def diagnostics(self) -> str:
        sections = []
        for label, path in (
            ("controller", self.logs / "controller.log"),
            ("ui", self.logs / "ui.log"),
            ("service", self.avibe_home / "logs" / "vibe_remote.log"),
        ):
            try:
                content = path.read_text(
                    encoding="utf-8", errors="replace"
                )[-12_000:]
            except OSError:
                continue
            sections.append(f"--- {label} ---\n{content}")
        return "\n".join(sections)

    def stop(self) -> None:
        self._stop_ui()
        self._stop_process(self._controller)
        self._controller = None
        if self._controller_log is not None:
            self._controller_log.close()
            self._controller_log = None

    def _stop_ui(self) -> None:
        self._stop_process(self._ui)
        self._ui = None
        if self._ui_log is not None:
            self._ui_log.close()
            self._ui_log = None

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def __enter__(self) -> "ModelHubTestApp":
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
