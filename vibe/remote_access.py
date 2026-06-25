"""Avibe Cloud remote-access runtime and auth helpers."""

from __future__ import annotations

import atexit
import hashlib
import hmac
import ipaddress
import json
import logging
import ntpath
import os
import platform
import re
import shlex
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import jwt
import requests
from jwt import PyJWKClient

from config import paths
from config.v2_config import V2Config
from vibe import api, runtime

logger = logging.getLogger(__name__)

CLOUDFLARED_BASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"
SESSION_COOKIE_NAME = "__Host-vibe_remote_session"
SESSION_TTL_SECONDS = 24 * 60 * 60
_CONNECTOR_LOCK = threading.RLock()
_STATUS_HEARTBEAT_LOCK = threading.Lock()
_STATUS_HEARTBEAT_STARTED = False
_STATUS_REPORT_LOCK = threading.Lock()
_STATUS_REPORT_THREADS: set[threading.Thread] = set()
_STATUS_REPORT_ATEXIT_REGISTERED = False
STATUS_HEARTBEAT_SECONDS = 5 * 60
STATUS_LOG_TAIL_BYTES = 64 * 1024
STATUS_REPORT_DRAIN_SECONDS = 1.0


class BackendRequestError(Exception):
    def __init__(self, status: int, payload: dict[str, Any]):
        super().__init__(payload.get("detail") or payload.get("error") or f"HTTP {status}")
        self.status = status
        self.payload = payload


class OAuthCodeExchangeError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or ""


def _bin_dir() -> Path:
    return paths.get_vibe_remote_dir() / "bin"


def _managed_cloudflared_path() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _bin_dir() / f"cloudflared{suffix}"


def _pid_path() -> Path:
    return paths.get_runtime_remote_access_pid_path()


def _state_path() -> Path:
    return paths.get_runtime_dir() / "remote-access-cloudflared.json"


def _cloudflared_stderr_path() -> Path:
    return paths.get_runtime_dir() / "remote_access_cloudflared_stderr.log"


def _cloudflared_stdout_path() -> Path:
    return paths.get_runtime_dir() / "remote_access_cloudflared_stdout.log"


def _clear_cloudflared_logs() -> None:
    for path in (_cloudflared_stdout_path(), _cloudflared_stderr_path()):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "amd64" if machine in {"x86_64", "amd64"} else "arm64" if machine in {"aarch64", "arm64"} else ""
    if not arch:
        raise ValueError(f"Unsupported architecture for cloudflared: {machine}")
    if system == "linux":
        return f"cloudflared-linux-{arch}"
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    if system == "windows" and arch == "amd64":
        return "cloudflared-windows-amd64.exe"
    raise ValueError(f"Unsupported OS for cloudflared: {system}")


def _version(path: str) -> str | None:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8, check=False)
    except Exception:
        return None
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return output[0] if output else None


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_stream_atomically(source: Any, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f".{target.name}.tmp")
    temp_target.unlink(missing_ok=True)
    try:
        with temp_target.open("wb") as output:
            shutil.copyfileobj(source, output)
        os.replace(temp_target, target)
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise


def _safe_extract_cloudflared(archive: tarfile.TarFile, target: Path) -> None:
    for member in archive.getmembers():
        member_name = Path(member.name)
        if member_name.name != "cloudflared":
            continue
        if member_name.is_absolute() or ".." in member_name.parts or not member.isfile():
            raise RuntimeError("Downloaded cloudflared archive contained an unsafe entry")
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError("Downloaded cloudflared archive did not contain a readable binary")
        _copy_stream_atomically(source, target)
        return
    raise RuntimeError("Downloaded cloudflared archive did not contain cloudflared")


def install_cloudflared() -> dict[str, Any]:
    try:
        paths.ensure_data_dirs()
        asset = _asset_name()
        url = f"{CLOUDFLARED_BASE_URL}/{asset}"
        target = _managed_cloudflared_path()
        with tempfile.TemporaryDirectory() as tmp:
            download_path = Path(tmp) / asset
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with download_path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if asset.endswith(".tgz"):
                with tarfile.open(download_path, "r:gz") as archive:
                    _safe_extract_cloudflared(archive, target)
            else:
                with download_path.open("rb") as source:
                    _copy_stream_atomically(source, target)
        _make_executable(target)
        return {"ok": True, "path": str(target), "version": _version(str(target)), "source_url": url}
    except Exception as exc:
        return {"ok": False, "error": "cloudflared_install_failed", "detail": str(exc)}


def _resolve_binary(config: V2Config | None = None) -> str | None:
    configured = ""
    if config is not None:
        configured = getattr(config.remote_access.vibe_cloud, "cloudflared_path", "") or ""
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.exists() and os.access(expanded, os.X_OK):
            return str(expanded)
    managed = _managed_cloudflared_path()
    if managed.exists() and os.access(managed, os.X_OK):
        return str(managed)
    return shutil.which("cloudflared")


def _read_pid() -> int | None:
    try:
        return int(_pid_path().read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _is_cloudflared_pid(pid: int | None) -> bool:
    return _cloudflared_pid_state(pid) == "cloudflared"


def _is_cloudflared_executable(value: str) -> bool:
    executable = value.strip().strip("\"'")
    executable_name = Path(executable).name.lower()
    windows_name = ntpath.basename(executable).lower()
    return executable_name in {"cloudflared", "cloudflared.exe"} or windows_name in {"cloudflared", "cloudflared.exe"}


def _command_starts_with_cloudflared(command: str) -> bool:
    command = command.strip()
    for posix in (True, False):
        try:
            parts = shlex.split(command, posix=posix)
        except ValueError:
            continue
        if parts and _is_cloudflared_executable(parts[0]):
            return True

    lower_command = command.lower()
    for marker in (" tunnel ", " tunnel", " --", " access ", " service ", " update ", " version"):
        marker_index = lower_command.find(marker)
        if marker_index > 0 and _is_cloudflared_executable(command[:marker_index]):
            return True
    return _is_cloudflared_executable(command)


def _cloudflared_pid_state(pid: int | None) -> str:
    if not pid or not runtime.pid_alive(pid):
        return "dead"
    try:
        command = runtime.get_process_command(pid)
    except Exception:
        return "unknown"
    if not command:
        return "unknown"
    if _command_starts_with_cloudflared(command):
        return "cloudflared"
    return "other"


def _write_state(pid: int, config: V2Config, binary: str) -> None:
    _state_path().write_text(json.dumps({"pid": pid, **_runtime_signature(config, binary)}, indent=2), encoding="utf-8")


def _runtime_signature(config: V2Config, binary: str) -> dict[str, str]:
    cloud = config.remote_access.vibe_cloud
    return {
        "provider": "vibe_cloud",
        "binary_path": binary,
        "public_url": cloud.public_url,
        "tunnel_token_sha256": hashlib.sha256((cloud.tunnel_token or "").encode("utf-8")).hexdigest(),
    }


def _read_state() -> dict[str, Any] | None:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_text_tail(path: Path, byte_limit: int) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - byte_limit))
            return handle.read(byte_limit).decode("utf-8", errors="replace")
    except Exception:
        return None


def _observed_cloudflared_origin_service() -> str | None:
    content = _read_text_tail(_cloudflared_stderr_path(), STATUS_LOG_TAIL_BYTES)
    if content is None:
        return None
    pattern = re.compile(
        r'originService=([^\s"]+)'
        r'|\\"service\\":\\"(http://[^"\\]+)\\"'
        r'|(?<!\\)"service":"(http://[^"]+)"'
    )
    last_origin = None
    for match in pattern.finditer(content):
        last_origin = next((group for group in match.groups() if group), None)
    return str(last_origin) if last_origin else None


def _running_signature(pid: int | None) -> dict[str, str] | None:
    if not _is_cloudflared_pid(pid):
        return None
    state = _read_state()
    if state is None or state.get("pid") != pid:
        return None
    return {
        "provider": str(state.get("provider") or ""),
        "binary_path": str(state.get("binary_path") or ""),
        "public_url": str(state.get("public_url") or ""),
        "tunnel_token_sha256": str(state.get("tunnel_token_sha256") or ""),
    }


def status(config: V2Config | None = None) -> dict[str, Any]:
    try:
        config = config or V2Config.load()
    except Exception:
        config = None
    pid = _read_pid()
    pid_state = _cloudflared_pid_state(pid)
    running = pid_state == "cloudflared"
    if pid and pid_state in {"dead", "other"}:
        _pid_path().unlink(missing_ok=True)
        _state_path().unlink(missing_ok=True)
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None) if config else None
    binary = _resolve_binary(config)
    return {
        "ok": True,
        "provider": "vibe_cloud",
        "enabled": bool(getattr(cloud, "enabled", False)),
        "public_url": getattr(cloud, "public_url", "") if cloud else "",
        "paired": bool(getattr(cloud, "instance_id", "") and getattr(cloud, "tunnel_token", "")) if cloud else False,
        "running": running,
        "pid": pid if running or pid_state == "unknown" else None,
        "pid_state": pid_state,
        "binary_found": bool(binary),
        "binary_path": binary,
        "binary_version": _version(binary) if binary else None,
    }


def _local_ui_healthy(config: V2Config) -> bool:
    try:
        response = requests.get(f"{origin_service_for_pairing(config)}/health", timeout=1.0)
        return response.ok
    except Exception:
        return False


def runtime_status_payload(config: V2Config | None = None, event: str = "heartbeat", last_error: str | None = None) -> dict[str, Any]:
    config = config or V2Config.load()
    current = status(config)
    payload = {
        "event": event,
        "local_version": "dev",
        "ui_healthy": _local_ui_healthy(config),
        "tunnel_running": bool(current.get("running")),
        "cloudflared_found": bool(current.get("binary_found")),
        "expected_origin_service": origin_service_for_pairing(config),
        "observed_origin_service": _observed_cloudflared_origin_service(),
    }
    error = last_error or current.get("error")
    if error:
        payload["last_error"] = str(error)
    return payload


def report_runtime_status(config: V2Config | None = None, event: str = "heartbeat", last_error: str | None = None) -> dict[str, Any]:
    try:
        config = config or V2Config.load()
        cloud = config.remote_access.vibe_cloud
        if not (cloud.instance_id and cloud.instance_secret and cloud.backend_url):
            return {"ok": False, "error": "remote_status_not_configured"}
        payload = {
            "instance_secret": cloud.instance_secret,
            **runtime_status_payload(config, event=event, last_error=last_error),
        }
        result = _json_request(
            f"{cloud.backend_url.rstrip('/')}/api/v1/instances/{cloud.instance_id}/runtime-status",
            payload,
            timeout=5.0,
        )
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": "remote_status_report_failed", "detail": str(exc)}


def mint_cloud_token(
    config: V2Config | None = None,
    *,
    sub: str,
    email: str,
    scope: str = "asr",
    timeout: float = 8.0,
) -> dict[str, Any] | None:
    """Exchange this instance's device secret for a short-lived user token the
    workbench frontend uses to call avibe.bot's ``/api/cloud/*`` surface directly.

    Returns the backend payload (``access_token`` / ``expires_in``), or ``None``
    when the cloud isn't configured or the mint fails — the caller then falls
    back to the local relay.
    """
    config = config or V2Config.load()
    cloud = config.remote_access.vibe_cloud
    if not (cloud.enabled and cloud.instance_id and cloud.instance_secret and cloud.backend_url):
        return None
    try:
        return _json_request(
            f"{cloud.backend_url.rstrip('/')}/api/v1/instances/{cloud.instance_id}/user-token",
            {"sub": sub, "email": email, "scope": scope},
            timeout=timeout,
            headers={"X-Vibe-Device-Secret": cloud.instance_secret},
        )
    except (BackendRequestError, RuntimeError):
        return None


def cloud_token_for_request(
    config: V2Config | None,
    cookie_value: str | None,
    scope: str = "asr",
) -> dict[str, Any] | None:
    """Resolve the logged-in user from the remote-access session cookie and mint a
    short-lived cloud token for them.

    Returns ``{base_url, token, expires_at, scope}`` for the frontend, or ``None``
    when there is no authenticated user or the mint fails.
    """
    config = config or V2Config.load()
    payload = parse_session_cookie(config, cookie_value)
    if payload is None:
        return None
    email = str(payload.get("email", "")).strip()
    sub = str(payload.get("sub", "")).strip()
    if not email or not sub:
        return None
    minted = mint_cloud_token(config, sub=sub, email=email, scope=scope)
    if not minted or not minted.get("access_token"):
        return None
    cloud = config.remote_access.vibe_cloud
    return {
        "base_url": cloud.backend_url.rstrip("/"),
        "token": str(minted["access_token"]),
        "expires_at": int(time.time()) + int(minted.get("expires_in", 0) or 0),
        "scope": scope,
    }


def drain_runtime_status_reports(timeout_seconds: float = STATUS_REPORT_DRAIN_SECONDS) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        with _STATUS_REPORT_LOCK:
            threads = [thread for thread in _STATUS_REPORT_THREADS if thread.is_alive()]
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)


def _register_status_report_thread(thread: threading.Thread) -> None:
    global _STATUS_REPORT_ATEXIT_REGISTERED
    with _STATUS_REPORT_LOCK:
        _STATUS_REPORT_THREADS.add(thread)
        if not _STATUS_REPORT_ATEXIT_REGISTERED:
            atexit.register(drain_runtime_status_reports)
            _STATUS_REPORT_ATEXIT_REGISTERED = True


def _report_runtime_status_async(config: V2Config | None = None, event: str = "heartbeat", last_error: str | None = None) -> None:
    holder: dict[str, threading.Thread] = {}

    def worker() -> None:
        try:
            report_runtime_status(config, event=event, last_error=last_error)
        finally:
            thread = holder.get("thread")
            if thread is not None:
                with _STATUS_REPORT_LOCK:
                    _STATUS_REPORT_THREADS.discard(thread)

    try:
        thread = threading.Thread(target=worker, name="vibe-remote-status-report", daemon=True)
    except Exception:
        return
    holder["thread"] = thread
    try:
        _register_status_report_thread(thread)
        thread.start()
    except Exception:
        with _STATUS_REPORT_LOCK:
            _STATUS_REPORT_THREADS.discard(thread)


def start_status_heartbeat(config: V2Config | None = None, interval_seconds: int = STATUS_HEARTBEAT_SECONDS) -> None:
    global _STATUS_HEARTBEAT_STARTED
    def loop() -> None:
        while True:
            report_runtime_status(None, event="heartbeat")
            time.sleep(interval_seconds)

    with _STATUS_HEARTBEAT_LOCK:
        if _STATUS_HEARTBEAT_STARTED:
            return
        try:
            thread = threading.Thread(target=loop, name="vibe-remote-status-heartbeat", daemon=True)
            thread.start()
        except Exception:
            return
        _STATUS_HEARTBEAT_STARTED = True


def stop(config: V2Config | None = None) -> dict[str, Any]:
    try:
        config = config or V2Config.load()
    except Exception:
        config = None
    with _CONNECTOR_LOCK:
        pid = _read_pid()
        pid_state = _cloudflared_pid_state(pid)
        if pid is not None and pid_state == "unknown":
            result = {**status(config), "ok": False, "error": "cloudflared_process_unknown", "stopped": False}
            _report_runtime_status_async(config, event="stop_failed", last_error="cloudflared_process_unknown")
            return result
        if pid is not None and pid_state in {"dead", "other"}:
            _pid_path().unlink(missing_ok=True)
            _state_path().unlink(missing_ok=True)
            return {**status(config), "ok": True, "stopped": False, "stale_pid": True}
        stopped = runtime.stop_pid(pid, timeout=8) if pid is not None else False
        if stopped:
            post_stop_state = _cloudflared_pid_state(pid)
            if post_stop_state in {"cloudflared", "unknown"}:
                result = {**status(config), "ok": False, "error": "cloudflared_stop_failed", "stopped": False}
                _report_runtime_status_async(config, event="stop_failed", last_error="cloudflared_stop_failed")
                return result
            _pid_path().unlink(missing_ok=True)
            _state_path().unlink(missing_ok=True)
        if pid is not None and not stopped and _is_cloudflared_pid(pid):
            result = {**status(config), "ok": False, "error": "cloudflared_stop_failed", "stopped": False}
            _report_runtime_status_async(config, event="stop_failed", last_error="cloudflared_stop_failed")
            return result
        result = {**status(config), "ok": True, "stopped": stopped}
        _report_runtime_status_async(config, event="stop")
        return result


def rotate_session_secret(config: V2Config) -> None:
    config.remote_access.vibe_cloud.session_secret = secrets.token_urlsafe(32)
    config.save()


def start(config: V2Config | None = None) -> dict[str, Any]:
    fallback_config = config
    if fallback_config is None:
        try:
            fallback_config = V2Config.load()
        except Exception as exc:
            return {"ok": False, "error": "remote_access_config_load_failed", "detail": str(exc), "started": False}
    with _CONNECTOR_LOCK:
        try:
            config = V2Config.load()
        except Exception as exc:
            stop_result = stop(fallback_config)
            return {**stop_result, "ok": False, "error": "remote_access_config_load_failed", "detail": str(exc)}
        cloud = config.remote_access.vibe_cloud
        if not cloud.enabled:
            stop_result = stop(config)
            return {**stop_result, "ok": False, "error": "remote_access_disabled"}
        if not cloud.tunnel_token:
            stop_result = stop(config)
            return {**stop_result, "ok": False, "error": "missing_tunnel_token"}
        binary = _resolve_binary(config)
        if not binary:
            install_result = install_cloudflared()
            if install_result.get("ok") is False:
                _report_runtime_status_async(config, event="start_failed", last_error=str(install_result.get("error") or "cloudflared_install_failed"))
                return {**status(config), **install_result}
            binary = str(install_result["path"])
        current = status(config)
        if current.get("pid_state") == "unknown":
            _report_runtime_status_async(config, event="start_failed", last_error="cloudflared_process_unknown")
            return {**current, "ok": False, "error": "cloudflared_process_unknown", "started": False}
        if current.get("running"):
            running_sig = _running_signature(current.get("pid"))
            desired_sig = _runtime_signature(config, binary)
            if running_sig == desired_sig:
                _report_runtime_status_async(config, event="start")
                return {**current, "ok": True, "started": False}
            stop_result = stop(config)
            if stop_result.get("ok") is False or stop_result.get("running"):
                return {
                    **stop_result,
                    "ok": False,
                    "error": stop_result.get("error") or "cloudflared_stop_failed",
                    "restarted": False,
                }
        env = {**os.environ, "TUNNEL_TOKEN": cloud.tunnel_token}
        try:
            _clear_cloudflared_logs()
            pid = runtime.spawn_background(
                [binary, "tunnel", "--no-autoupdate", "run"],
                _pid_path(),
                "remote_access_cloudflared_stdout.log",
                "remote_access_cloudflared_stderr.log",
                env=env,
            )
            _write_state(pid, config, binary)
        except Exception as exc:
            _report_runtime_status_async(config, event="start_failed", last_error="cloudflared_spawn_failed")
            return {**status(config), "ok": False, "error": "cloudflared_spawn_failed", "detail": str(exc)}
        time.sleep(0.2)
        current = status(config)
        if not current.get("running"):
            _report_runtime_status_async(config, event="start_failed", last_error="cloudflared_exited")
            return {**current, "ok": False, "error": "cloudflared_exited"}
        result = {**current, "ok": True, "started": True, "pid": pid}
        _report_runtime_status_async(config, event="start")
        return result


def reconcile(config: V2Config | None = None) -> dict[str, Any]:
    config = config or V2Config.load()
    if config.remote_access.provider == "vibe_cloud" and config.remote_access.vibe_cloud.enabled:
        return start(config)
    return stop(config)


def _json_request(
    url: str,
    payload: dict[str, Any],
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "avibe/dev",
        }
        if headers:
            request_headers.update(headers)
        response = requests.post(
            url,
            json=payload,
            headers=request_headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        response = exc.response
        try:
            parsed = response.json() if response is not None else {}
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("error", "backend_http_error")
        if response is not None and response.text and "detail" not in parsed:
            parsed["detail"] = response.text
        status = response.status_code if response is not None else 0
        raise BackendRequestError(status, parsed) from exc
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc


def _effective_ui_port(config: V2Config) -> int:
    raw_port = os.environ.get("VIBE_UI_PORT") or config.ui.setup_port
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        port = int(config.ui.setup_port)
    if port < 1 or port > 65535:
        return int(config.ui.setup_port)
    return port


def _origin_host_for_pairing(config: V2Config) -> str:
    host = (config.ui.setup_host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    # "localhost" is ambiguous: cloudflared and the UI server resolve it
    # independently, so on a dual-stack host they can land on different
    # families (::1 vs 127.0.0.1) and surface as a 502. Hand cloudflared
    # a literal loopback IP whose family matches the bind host that
    # ``effective_ui_bind_host`` chooses, so the two sides never disagree
    # and IPv6-only hosts still work.
    if host.lower() == "localhost":
        return "[::1]" if runtime.resolve_localhost_family() == "inet6" else "127.0.0.1"

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "127.0.0.1"

    if address.is_loopback:
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    if address.is_unspecified and address.version == 6:
        return "[::1]"
    return "127.0.0.1"


def origin_service_for_pairing(config: V2Config | None = None) -> str:
    config = config or V2Config.load()
    return f"http://{_origin_host_for_pairing(config)}:{_effective_ui_port(config)}"


def pair(pairing_key: str, backend_url: str, device_name: str = "avibe") -> dict[str, Any]:
    pairing_key = (pairing_key or "").strip()
    backend_url = (backend_url or "https://avibe.bot").strip().rstrip("/")
    if not pairing_key:
        return {"ok": False, "error": "missing_pairing_key"}
    try:
        origin_service = origin_service_for_pairing()
    except Exception:
        origin_service = "http://127.0.0.1:5123"
    try:
        result = _json_request(
            f"{backend_url}/api/v1/pairing/redeem",
            {
                "pairing_key": pairing_key,
                "device_name": device_name,
                "local_version": "dev",
                "origin_service": origin_service,
            },
        )
    except BackendRequestError as exc:
        return {"ok": False, **exc.payload, "status": exc.status}
    except Exception as exc:
        return {"ok": False, "error": "pairing_request_failed", "detail": str(exc)}
    required = ("instance_id", "client_id", "issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "public_url", "redirect_uri", "tunnel_token", "instance_secret")
    missing = [field for field in required if not result.get(field)]
    if missing:
        return {"ok": False, "error": "invalid_pairing_response", "missing": missing}
    origin_update = result.get("tunnel_origin_update")
    if isinstance(origin_update, dict) and origin_update.get("ok") is False:
        return {
            "ok": False,
            "error": str(origin_update.get("error") or "tunnel_origin_update_failed"),
            "pairing": {"ok": False, "origin_service": origin_service},
        }
    config = api.save_config(
        {
            "remote_access": {
                "provider": "vibe_cloud",
                "vibe_cloud": {
                    "enabled": True,
                    "backend_url": backend_url,
                    "instance_id": result["instance_id"],
                    "client_id": result["client_id"],
                    "issuer": result["issuer"],
                    "authorization_endpoint": result["authorization_endpoint"],
                    "token_endpoint": result["token_endpoint"],
                    "jwks_uri": result["jwks_uri"],
                    "public_url": result["public_url"],
                    "redirect_uri": result["redirect_uri"],
                    "tunnel_token": result["tunnel_token"],
                    "instance_secret": result["instance_secret"],
                    "session_secret": secrets.token_urlsafe(32),
                },
            }
        }
    )
    start_result = start(config)
    _report_runtime_status_async(config, event="pair", last_error=start_result.get("error"))
    return {**status(config), "ok": True, "pairing": {"ok": True}, "start": start_result}


def _session_signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_cookie(config: V2Config, email: str, subject: str) -> str:
    cloud = config.remote_access.vibe_cloud
    if not cloud.session_secret:
        raise ValueError("Remote access session secret is not configured")
    issued_at = int(time.time())
    payload = {
        "email": email,
        "sub": subject,
        "instance_id": cloud.instance_id,
        "iat": issued_at,
        "exp": issued_at + SESSION_TTL_SECONDS,
    }
    payload_text = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = _session_signature(cloud.session_secret, payload_text)
    return f"{payload_text}.{signature}"


def parse_session_cookie(config: V2Config, cookie_value: str | None) -> dict[str, Any] | None:
    if not cookie_value or "." not in cookie_value:
        return None
    cloud = config.remote_access.vibe_cloud
    if not cloud.session_secret:
        return None
    payload_text, signature = cookie_value.rsplit(".", 1)
    expected = _session_signature(cloud.session_secret, payload_text)
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(urllib.parse.unquote(payload_text))
    except Exception:
        return None
    if payload.get("instance_id") != cloud.instance_id:
        return None
    if int(payload.get("exp", 0)) <= int(time.time()):
        return None
    return payload


def validate_session_cookie(config: V2Config, cookie_value: str | None) -> bool:
    return parse_session_cookie(config, cookie_value) is not None


def session_needs_renewal(payload: dict[str, Any], now: int | None = None) -> bool:
    """Return True when the session has spent more than half of SESSION_TTL_SECONDS.

    Mirrors the avibe-bot-backend control-plane sliding-session policy
    (middleware.ts): re-sign the cookie once the remaining lifetime drops
    below half so users don't get bounced through OAuth on every visit.
    """
    current = now if now is not None else int(time.time())
    return int(payload.get("exp", 0)) - current < SESSION_TTL_SECONDS // 2


def authorization_url(config: V2Config, state: str, nonce: str, code_challenge: str) -> str:
    cloud = config.remote_access.vibe_cloud
    params = {
        "client_id": cloud.client_id,
        "redirect_uri": cloud.redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if cloud.dev_login_hint:
        params["login_hint"] = cloud.dev_login_hint
    return f"{cloud.authorization_endpoint}?{urllib.parse.urlencode(params)}"


def exchange_oauth_code(config: V2Config, code: str, code_verifier: str) -> dict[str, Any]:
    cloud = config.remote_access.vibe_cloud
    response = requests.post(
        cloud.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "client_id": cloud.client_id,
            "redirect_uri": cloud.redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        },
        headers={"Accept": "application/json", "User-Agent": "avibe/dev"},
        timeout=20,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("error") or payload.get("detail") or "")
        except Exception:
            detail = response.text[:120]
        raise OAuthCodeExchangeError("token_endpoint_rejected", detail or str(exc)) from exc
    try:
        token_payload = response.json()
    except ValueError as exc:
        raise OAuthCodeExchangeError("invalid_token_response", str(exc)) from exc
    id_token = token_payload.get("id_token")
    if not id_token:
        raise OAuthCodeExchangeError("missing_id_token")
    try:
        jwk_client = PyJWKClient(cloud.jwks_uri)
        signing_key = jwk_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=cloud.client_id,
            issuer=cloud.issuer,
        )
    except jwt.InvalidIssuerError as exc:
        raise OAuthCodeExchangeError("invalid_issuer", str(exc)) from exc
    except jwt.InvalidAudienceError as exc:
        raise OAuthCodeExchangeError("invalid_audience", str(exc)) from exc
    except jwt.ExpiredSignatureError as exc:
        raise OAuthCodeExchangeError("expired_id_token", str(exc)) from exc
    except jwt.ImmatureSignatureError as exc:
        raise OAuthCodeExchangeError("immature_id_token", str(exc)) from exc
    except jwt.InvalidTokenError as exc:
        raise OAuthCodeExchangeError("invalid_id_token", str(exc)) from exc
    except Exception as exc:
        raise OAuthCodeExchangeError("jwks_or_token_validation_failed", str(exc)) from exc
    if claims.get("vibe_instance_id") != cloud.instance_id:
        raise OAuthCodeExchangeError("invalid_instance_id")
    if not claims.get("email_verified"):
        raise OAuthCodeExchangeError("email_not_verified")
    return {"claims": claims, "token": token_payload}


# --- OAuth handshake store -------------------------------------------------
#
# The login handshake (PKCE ``code_verifier`` + ``nonce`` + device binding) lives
# only between the login-start redirect and the ``/auth/callback`` of one flow
# (~5 min). iOS standalone PWAs run the cross-origin authorize step in a separate
# in-app-browser context, so the handshake *cookie* the callback reads can belong
# to a different ``GET /`` generation than the consent the user approved; we
# therefore also keep the handshake here, keyed by the signed state's random id, so
# the callback can recover it by the (signature-verified) state in the callback URL.
#
# Kept in process memory, not on disk: the UI server is a single process that
# handles both the redirect and the callback, the data is short-lived, and a
# mid-flow restart just means the user logs in again. This avoids a disk/inode DoS
# surface entirely and makes single-use + the cap trivially atomic under one lock.

OAUTH_HANDSHAKE_TTL_SECONDS = 300
# Cap on live handshakes — bounds memory under a burst of unauthenticated login
# starts; sheds new entries when full (preserving in-flight logins). Far above any
# realistic concurrent-login count. (The unauthenticated /auth rate limiter is the
# primary bound; this is a backstop.)
OAUTH_HANDSHAKE_MAX_ENTRIES = 2048
_OAUTH_HANDSHAKE_RID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
# Guards prune + capacity-check + insert/pop so admission and single-use are atomic.
_OAUTH_STORE_LOCK = threading.Lock()
_oauth_handshakes: dict[str, dict[str, Any]] = {}
# Throttle the "at capacity" warning so it can't itself grow the log under the flood
# it reports.
_OAUTH_STORE_CAPACITY_WARN_INTERVAL_SECONDS = 60.0
_oauth_store_capacity_warned_at = 0.0


def _prune_oauth_handshakes(now: int) -> None:
    """Drop expired handshakes. Caller must hold ``_OAUTH_STORE_LOCK``."""
    for rid in [rid for rid, record in _oauth_handshakes.items() if record.get("exp", 0) <= now]:
        _oauth_handshakes.pop(rid, None)


def _warn_oauth_store_at_capacity() -> None:
    """Log the capacity-shed warning at most once per interval (call under the lock)."""
    global _oauth_store_capacity_warned_at
    now = time.monotonic()
    if now - _oauth_store_capacity_warned_at >= _OAUTH_STORE_CAPACITY_WARN_INTERVAL_SECONDS:
        _oauth_store_capacity_warned_at = now
        logger.warning("oauth handshake store at capacity (>= %d); shedding writes", OAUTH_HANDSHAKE_MAX_ENTRIES)


def store_oauth_handshake(
    rid: str, *, nonce: str, code_verifier: str, next_target: str, device_hash: str | None = None
) -> None:
    """Persist a login handshake in memory, keyed by the signed state's random id.

    Single-use, ``OAUTH_HANDSHAKE_TTL_SECONDS`` TTL. Invalid ids are ignored.
    ``device_hash`` binds a later store-fallback recovery to the originating browser.
    """
    if not _OAUTH_HANDSHAKE_RID_RE.match(rid or ""):
        return
    now = int(time.time())
    record = {
        "nonce": nonce,
        "code_verifier": code_verifier,
        "next": next_target,
        "device_hash": device_hash,
        "exp": now + OAUTH_HANDSHAKE_TTL_SECONDS,
    }
    with _OAUTH_STORE_LOCK:
        _prune_oauth_handshakes(now)
        # At capacity (a flood of fresh entries): shed this one rather than grow
        # unbounded, but keep in-flight logins. Re-storing an existing id is allowed
        # (it doesn't grow the store).
        if len(_oauth_handshakes) >= OAUTH_HANDSHAKE_MAX_ENTRIES and rid not in _oauth_handshakes:
            _warn_oauth_store_at_capacity()
            return
        _oauth_handshakes[rid] = record


def pop_oauth_handshake(rid: str | None) -> dict[str, Any] | None:
    """Return and remove (single-use) the handshake for ``rid``; None if absent/expired.

    ``dict.pop`` under the lock is atomic, so concurrent callbacks for the same
    ``rid`` get exactly one winner.
    """
    if not rid or not _OAUTH_HANDSHAKE_RID_RE.match(rid):
        return None
    with _OAUTH_STORE_LOCK:
        record = _oauth_handshakes.pop(rid, None)
    if record is None or record.get("exp", 0) <= int(time.time()):
        return None
    return record
