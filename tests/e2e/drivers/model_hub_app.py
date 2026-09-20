"""Hermetic real-HTTP Avibe runtime for Model Hub E2E scenarios."""

from __future__ import annotations

import http.cookiejar
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
    url2pathname,
)

import psutil


_MISSING = object()
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ENGINE_MANIFEST_PATH_ENV = "VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"
_SAFE_INHERITED_ENV = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
    _ENGINE_MANIFEST_PATH_ENV,
)
_UI_PORT_START_ATTEMPTS = 3
_OWNED_PROCESS_GRACE_SECONDS = 3.0
_OWNED_PROCESS_KILL_SECONDS = 2.0
_OWNED_PROCESS_POLL_SECONDS = 0.05
_DEAD_PROCESS_STATUSES = frozenset(
    status
    for status in (
        psutil.STATUS_ZOMBIE,
        getattr(psutil, "STATUS_DEAD", None),
    )
    if status is not None
)
logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _OwnedProcess:
    """Stable identity for one running process owned by the harness."""

    pid: int
    process_group: int
    create_time: float
    argv: str


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
    """Start isolated controller and UI processes on macOS or Linux."""

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

    def _seed_local_engine_archive(self) -> None:
        """Put a verified local engine asset in the isolated offline cache."""

        raw_manifest_path = self.env.get(_ENGINE_MANIFEST_PATH_ENV, "").strip()
        if not raw_manifest_path:
            return
        manifest_path = Path(raw_manifest_path).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = self.repo_root / manifest_path

        from vibe.model_hub_runtime.installer import EngineRuntimeManager

        runtime_dir = (
            self.avibe_home / "runtime" / "model-hub" / "engine"
        )
        manager = EngineRuntimeManager(
            runtime_dir=runtime_dir,
            manifest_path=manifest_path,
            offline=True,
        )
        contract = manager.contract_manifest()
        if contract.get("resolution") != "resolved":
            return
        selected = next(
            (
                asset
                for asset in contract.get("assets", ())
                if isinstance(asset, Mapping)
                and asset.get("platform") == manager.host_platform()
            ),
            None,
        )
        if selected is None:
            return
        archive_url = selected.get("url")
        if not isinstance(archive_url, str):
            return
        parsed_url = urlparse(archive_url)
        if parsed_url.scheme != "file":
            return
        if (
            parsed_url.netloc not in {"", "localhost"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise RuntimeError(
                "local Model Hub engine archive must use a plain local file URL"
            )
        source = Path(url2pathname(parsed_url.path))
        if not source.is_absolute():
            raise RuntimeError(
                "local Model Hub engine archive URL must be absolute"
            )

        expected_size = selected.get("size_bytes")
        expected_sha256 = selected.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or not isinstance(expected_sha256, str)
        ):
            raise RuntimeError(
                "local Model Hub engine archive requires size and sha256"
            )
        self._verify_local_engine_archive(
            source,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

        archive_status = manager.status().get("archive")
        if not isinstance(archive_status, Mapping):
            return
        archive_name = archive_status.get("name")
        if (
            not isinstance(archive_name, str)
            or not archive_name
            or Path(archive_name).name != archive_name
        ):
            raise RuntimeError("local Model Hub engine archive name is unsafe")

        downloads = runtime_dir / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        cached = downloads / archive_name
        temporary = downloads / f".{archive_name}.seed.tmp"
        try:
            shutil.copyfile(source, temporary)
            self._verify_local_engine_archive(
                temporary,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_local_engine_archive(
        path: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(
                f"local Model Hub engine archive is unavailable: {path}"
            ) from exc
        if actual_size != expected_size:
            raise RuntimeError(
                "local Model Hub engine archive size mismatch: "
                f"expected {expected_size}, got {actual_size}"
            )
        digest = hashlib.sha256()
        try:
            with path.open("rb") as archive:
                for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise RuntimeError(
                f"local Model Hub engine archive is unavailable: {path}"
            ) from exc
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256.lower():
            raise RuntimeError(
                "local Model Hub engine archive sha256 mismatch: "
                f"expected {expected_sha256.lower()}, got {actual_sha256}"
            )

    def start(self) -> "ModelHubTestApp":
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self.avibe_home.mkdir(parents=True, exist_ok=True)
            self.logs.mkdir(parents=True, exist_ok=True)
            (self.runtime_root / "tmp").mkdir(
                parents=True, exist_ok=True
            )
            self._seed_local_engine_archive()
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
            [sys.executable, "main.py", str(self.avibe_home)],
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
            [sys.executable, "-c", expression, str(self.avibe_home)],
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
        self._terminate_owned_process_groups()

    def _stop_ui(self) -> None:
        self._stop_process(self._ui)
        self._ui = None
        if self._ui_log is not None:
            self._ui_log.close()
            self._ui_log = None

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        if process.poll() is not None:
            process.wait(timeout=0)
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def _terminate_owned_process_groups(self) -> None:
        """Prove no running process still references the runtime marker."""

        owned = self._owned_process_groups()
        if not owned:
            return
        tracked = {
            process.pid: process
            for processes in owned.values()
            for process in processes
        }
        self._signal_process_groups(owned, signal.SIGTERM)
        running, zombies = self._wait_for_owned_processes(
            tracked,
            _OWNED_PROCESS_GRACE_SECONDS
        )
        if running:
            self._signal_process_groups(
                self._group_processes(running.values()),
                signal.SIGKILL,
            )
            running, zombies = self._wait_for_owned_processes(
                tracked,
                _OWNED_PROCESS_KILL_SECONDS
            )
        self._log_persistent_zombies(zombies)
        if running:
            details = "; ".join(
                f"pid={process.pid} pgid={process.process_group} "
                f"argv={process.argv!r}"
                for process in sorted(
                    running.values(), key=lambda item: item.pid
                )
            )
            raise RuntimeError(
                "hermetic Model Hub cleanup left running owned processes: "
                f"{details}"
            )

    def _owned_process_groups(
        self,
    ) -> dict[int, list[_OwnedProcess]]:
        marker = str(self.avibe_home)
        try:
            candidates = psutil.process_iter(
                attrs=["pid", "cmdline", "create_time", "status"],
                ad_value=None,
            )
        except psutil.Error as exc:
            raise RuntimeError(
                "could not scan the process table for hermetic children"
            ) from exc

        current_group = os.getpgrp()
        owned: dict[int, list[_OwnedProcess]] = {}
        for candidate in candidates:
            info = candidate.info
            status = info.get("status")
            if status in _DEAD_PROCESS_STATUSES:
                continue
            cmdline = info.get("cmdline")
            if not isinstance(cmdline, list):
                continue
            argv = " ".join(str(part) for part in cmdline)
            if marker not in argv:
                continue
            try:
                pid = int(info["pid"])
                process_group = os.getpgid(pid)
                create_time = float(info["create_time"])
            except (KeyError, TypeError, ValueError, ProcessLookupError):
                continue
            if process_group == current_group:
                raise RuntimeError(
                    "hermetic child shares the pytest process group; "
                    f"refusing to signal pgid {current_group}"
                )
            owned.setdefault(process_group, []).append(
                _OwnedProcess(
                    pid=pid,
                    process_group=process_group,
                    create_time=create_time,
                    argv=argv,
                )
            )
        return owned

    @staticmethod
    def _group_processes(
        processes: Iterable[_OwnedProcess],
    ) -> dict[int, list[_OwnedProcess]]:
        grouped: dict[int, list[_OwnedProcess]] = {}
        for process in processes:
            grouped.setdefault(process.process_group, []).append(process)
        return grouped

    @staticmethod
    def _signal_process_groups(
        owned: Mapping[int, list[_OwnedProcess]],
        signum: signal.Signals,
    ) -> None:
        for process_group in owned:
            try:
                os.killpg(process_group, signum)
            except ProcessLookupError:
                pass

    def _wait_for_owned_processes(
        self,
        tracked: dict[int, _OwnedProcess],
        timeout: float,
    ) -> tuple[dict[int, _OwnedProcess], dict[int, _OwnedProcess]]:
        deadline = time.monotonic() + timeout
        while True:
            for processes in self._owned_process_groups().values():
                for process in processes:
                    previous = tracked.get(process.pid)
                    if (
                        previous is None
                        or previous.create_time != process.create_time
                    ):
                        tracked[process.pid] = process
            running: dict[int, _OwnedProcess] = {}
            zombies: dict[int, _OwnedProcess] = {}
            for pid, process in tracked.items():
                state = self._tracked_process_state(process)
                if state == "running":
                    running[pid] = process
                elif state == "zombie":
                    zombies[pid] = process
            if not running or time.monotonic() >= deadline:
                return running, zombies
            time.sleep(_OWNED_PROCESS_POLL_SECONDS)

    @staticmethod
    def _tracked_process_state(process: _OwnedProcess) -> str:
        try:
            candidate = psutil.Process(process.pid)
            if candidate.create_time() != process.create_time:
                return "gone"
            status = candidate.status()
        except psutil.ZombieProcess:
            return "zombie"
        except psutil.NoSuchProcess:
            return "gone"
        except psutil.Error as exc:
            raise RuntimeError(
                "could not read the status of an owned process: "
                f"pid={process.pid}"
            ) from exc
        if status in _DEAD_PROCESS_STATUSES:
            return "zombie"
        return "running"

    @staticmethod
    def _log_persistent_zombies(
        zombies: Mapping[int, _OwnedProcess],
    ) -> None:
        for process in zombies.values():
            if ModelHubTestApp._tracked_process_state(process) != "zombie":
                continue
            logger.warning(
                "hermetic Model Hub child is a harmless zombie awaiting its "
                "parent and holds no resources: pid=%s pgid=%s argv=%r",
                process.pid,
                process.process_group,
                process.argv,
            )

    def __enter__(self) -> "ModelHubTestApp":
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
