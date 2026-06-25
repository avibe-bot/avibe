"""OpenCode server lifecycle + HTTP API wrapper."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import quote as _url_quote
import urllib.error
import urllib.parse
import urllib.request
import threading
from asyncio.subprocess import Process
from typing import Any, Dict, List, Optional

import aiohttp

from config import paths
from core.process_isolation import isolated_subprocess_kwargs, terminate_process_tree
from modules.agents.opencode.config_reconciler import OpenCodeConfigReconciler
from vibe import runtime
from vibe.opencode_config import (
    get_opencode_custom_provider_adapter,
    load_first_opencode_user_config,
    read_opencode_provider_auth_entries,
)

logger = logging.getLogger(__name__)

DEFAULT_OPENCODE_PORT = 4096
DEFAULT_OPENCODE_HOST = "127.0.0.1"
SERVER_START_TIMEOUT = 15
OPENCODE_LOG_TAIL_BYTES = 2_000_000


def _percent_encode_path(path: str) -> str:
    """Percent-encode *path* so it is safe for an HTTP header value.

    RFC 7230 only allows visible US-ASCII characters (plus whitespace)
    in header field values.  Non-ASCII bytes (e.g. CJK characters in
    project paths) must be percent-encoded, otherwise they will be
    misinterpreted by the receiving end.
    """
    return _url_quote(path, safe="/")


class OpenCodeServerManager:
    """Manages a singleton OpenCode server process shared across all working directories."""

    _instance: Optional["OpenCodeServerManager"] = None
    _class_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        binary: str = "opencode",
        port: int = DEFAULT_OPENCODE_PORT,
        request_timeout_seconds: int = 60,
    ):
        self.binary = binary
        self.port = port
        self.request_timeout_seconds = request_timeout_seconds
        self.host = DEFAULT_OPENCODE_HOST
        self._process: Optional[Process] = None
        # The event loop ``_process`` was created on. Subprocess transports
        # bind their internal Future / wait helpers to the creating loop;
        # ``process.wait()`` or ``terminate_process_tree(process)`` from a
        # different loop raises ``RuntimeError: got Future attached to a
        # different loop``. The singleton outlives ``asyncio.run`` calls
        # (Flask UI server creates a new loop per request), so any code
        # that touches ``_process`` from a non-creating loop has to
        # detach it first. ``_process_loop`` is set alongside every
        # ``_process`` assignment.
        self._process_loop: Optional[asyncio.AbstractEventLoop] = None
        self._base_url: Optional[str] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_session_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._pid_file = paths.get_logs_dir() / "opencode_server.json"
        self._active_requests = 0
        self._active_run_sessions: set[str] = set()
        self._auth_refresh_pending = False
        self._auth_refresh_pending_port: Optional[int] = None
        self._pending_runtime_config: Optional[tuple[str, int, int]] = None
        self._last_prompt_started_at: dict[str, float] = {}

    def _get_lock(self) -> asyncio.Lock:
        """Get or create an asyncio.Lock bound to the current event loop."""
        current_loop = asyncio.get_event_loop()
        if self._lock is None or self._lock_loop is not current_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = current_loop
        return self._lock

    @classmethod
    async def get_instance(
        cls,
        binary: str = "opencode",
        port: int = DEFAULT_OPENCODE_PORT,
        request_timeout_seconds: int = 60,
    ) -> "OpenCodeServerManager":
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls(
                    binary=binary,
                    port=port,
                    request_timeout_seconds=request_timeout_seconds,
                )
            elif (
                cls._instance.binary != binary
                or cls._instance.port != port
                or cls._instance.request_timeout_seconds != request_timeout_seconds
            ):
                logger.warning(
                    "OpenCodeServerManager already initialized with "
                    f"binary={cls._instance.binary}, port={cls._instance.port}, "
                    f"request_timeout_seconds={cls._instance.request_timeout_seconds}; "
                    f"ignoring new params binary={binary}, port={port}, "
                    f"request_timeout_seconds={request_timeout_seconds}"
                )
            return cls._instance

    @classmethod
    async def get_instance_if_managed_server_exists(
        cls,
        binary: str = "opencode",
        port: int = DEFAULT_OPENCODE_PORT,
        request_timeout_seconds: int = 60,
    ) -> Optional["OpenCodeServerManager"]:
        with cls._class_lock:
            if cls._instance is not None:
                return cls._instance

            pid_file = paths.get_logs_dir() / "opencode_server.json"
            try:
                data = json.loads(pid_file.read_text())
            except Exception:
                return None
            if not isinstance(data, dict) or data.get("port") != port:
                return None
            pid = data.get("pid")
            if not isinstance(pid, int) or not runtime.pid_alive(pid):
                return None
            command = runtime.get_process_command(pid)
            if not command or not cls._is_opencode_serve_cmd(command, port):
                return None

            cls._instance = cls(
                binary=binary,
                port=port,
                request_timeout_seconds=request_timeout_seconds,
            )
            return cls._instance

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url
        return f"http://{self.host}:{self.port}"

    @staticmethod
    def _normalize_variant(reasoning_effort: Optional[str]) -> Optional[str]:
        normalized = (reasoning_effort or "").strip()
        if not normalized or normalized in {"default", "__default__"}:
            return None
        return normalized

    async def _get_http_session(self) -> aiohttp.ClientSession:
        current_loop = asyncio.get_running_loop()
        # Recreate session if it's closed or bound to a different event loop
        if self._http_session is None or self._http_session.closed or self._http_session_loop is not current_loop:
            # Close old session if it exists and is not closed
            if self._http_session is not None and not self._http_session.closed:
                try:
                    await self._http_session.close()
                except Exception:
                    pass
            total_timeout: Optional[int] = None if self.request_timeout_seconds <= 0 else self.request_timeout_seconds
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total_timeout))
            self._http_session_loop = current_loop
        return self._http_session

    async def _close_http_session_locked(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
            self._http_session_loop = None

    async def close_http_session(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Close the cached HTTP session explicitly.

        UI helper flows may run on short-lived event loops created per request.
        Closing the cached session at the end of those flows prevents aiohttp
        from reporting unclosed sessions/connectors when the loop exits.
        """

        async with self._get_lock():
            if loop is not None and self._http_session_loop is not loop:
                return
            await self._close_http_session_locked()

    async def _restart_for_auth_refresh_locked(self) -> None:
        await self._close_http_session_locked()

        cleanup_port = self._auth_refresh_pending_port or self.port
        targets: list[int] = []
        info = self._read_pid_file()
        pid = info.get("pid") if isinstance(info, dict) else None
        if isinstance(pid, int) and self._pid_exists(pid):
            cmd = self._get_pid_command(pid)
            if cmd and self._is_opencode_serve_cmd(cmd, cleanup_port):
                targets.append(pid)
            elif (
                isinstance(info, dict)
                and info.get("port") == cleanup_port
                and self._pid_owns_listening_port(pid, cleanup_port)
            ):
                logger.info(
                    "Trusting OpenCode pid file for pid=%s because it still owns port %s",
                    pid,
                    cleanup_port,
                )
                targets.append(pid)

        if not targets:
            for candidate in self._find_opencode_serve_pids(cleanup_port):
                cmd = self._get_pid_command(candidate)
                if cmd and self._is_opencode_serve_cmd(cmd, cleanup_port):
                    targets.append(candidate)

        for target_pid in dict.fromkeys(targets):
            await self._terminate_pid(target_pid, reason="auth refresh")

        self._clear_pid_file()
        self._process = None
        self._process_loop = None
        self._base_url = None
        self._auth_refresh_pending = False
        self._auth_refresh_pending_port = None
        self._apply_pending_runtime_config_locked()

    async def detach_after_deferred_refresh(self) -> None:
        """Drop cached client state when a refresh must wait for active runs."""
        async with self._get_lock():
            if self._active_requests > 0 or self._has_active_run_sessions():
                self._auth_refresh_pending = True
                self._auth_refresh_pending_port = self.port
                logger.info(
                    "Deferring OpenCode runtime detach until %s active request(s) and %s active run(s) finish",
                    self._active_requests,
                    len(self._active_run_sessions),
                )
                return
            await self._restart_for_auth_refresh_locked()

    async def refresh_global_config(self) -> bool:
        """Ask a live OpenCode server to reload global opencode.json config.

        OpenCode's HTTP API applies ``PATCH /global/config`` to the
        global config and disposes its cached instances, which refreshes
        provider options without terminating the shared ``opencode serve``
        process. Return ``False`` when the endpoint is unavailable so
        callers can fall back to a process restart.
        """

        config = self._load_opencode_user_config()
        if config is None:
            return False

        total_timeout: Optional[int] = None if self.request_timeout_seconds <= 0 else self.request_timeout_seconds
        async with self._get_lock():
            if self._active_requests > 0 or self._has_active_run_sessions():
                return False
            if not await self._is_healthy():
                return False
            current_config = await self._get_global_config_snapshot()
            if current_config is None:
                return False
            config = self._merge_global_config_snapshot(current_config, config)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total_timeout)) as session:
                async with session.patch(
                    f"{self.base_url}/global/config",
                    json=config,
                ) as resp:
                    if resp.status == 200:
                        await resp.read()
                        return True
                    if resp.status in (404, 405):
                        await resp.read()
                        return False
                    error_text = await resp.text()
                    raise RuntimeError(
                        f"Failed to refresh OpenCode global config: {resp.status} {error_text}"
                    )
        return False

    async def reload_runtime_config(
        self,
        *,
        binary: str,
        port: int,
        request_timeout_seconds: int,
    ) -> None:
        async with self._get_lock():
            if self._auth_refresh_pending:
                self._pending_runtime_config = (binary, port, request_timeout_seconds)
                return
            self._set_runtime_config(binary, port, request_timeout_seconds)

    def _set_runtime_config(self, binary: str, port: int, request_timeout_seconds: int) -> None:
        self.binary = binary
        self.port = port
        self.request_timeout_seconds = request_timeout_seconds

    def _apply_pending_runtime_config_locked(self) -> None:
        if self._pending_runtime_config is None:
            return
        self._set_runtime_config(*self._pending_runtime_config)
        self._pending_runtime_config = None

    def _has_active_run_sessions(self) -> bool:
        return bool(self._active_run_sessions)

    async def mark_run_active(self, session_id: str) -> None:
        async with self._get_lock():
            self._active_run_sessions.add(session_id)

    async def mark_run_inactive(self, session_id: str) -> None:
        async with self._get_lock():
            self._active_run_sessions.discard(session_id)

    @asynccontextmanager
    async def _request_scope(self):
        async with self._get_lock():
            if self._auth_refresh_pending and self._active_requests == 0 and not self._has_active_run_sessions():
                await self._restart_for_auth_refresh_locked()
            self._active_requests += 1
        try:
            yield
        finally:
            async with self._get_lock():
                self._active_requests = max(0, self._active_requests - 1)

    def _read_pid_file(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self._pid_file.read_text()
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.debug(f"Failed to read OpenCode pid file: {e}")
            return None

        try:
            data = json.loads(raw)
        except Exception as e:
            logger.debug(f"Failed to parse OpenCode pid file: {e}")
            return None

        return data if isinstance(data, dict) else None

    def _write_pid_file(self, pid: int) -> None:
        try:
            self._pid_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": pid,
                "port": self.port,
                "host": self.host,
                "started_at": time.time(),
            }
            self._pid_file.write_text(json.dumps(payload))
        except Exception as e:
            logger.debug(f"Failed to write OpenCode pid file: {e}")

    def _clear_pid_file(self) -> None:
        try:
            if self._pid_file.exists():
                self._pid_file.unlink()
        except Exception as e:
            logger.debug(f"Failed to clear OpenCode pid file: {e}")

    @staticmethod
    def _extract_json_object(text: str, start: int) -> Optional[str]:
        if start < 0 or start >= len(text) or text[start] != "{":
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    @staticmethod
    def _safe_url(raw: object) -> str:
        if not isinstance(raw, str) or not raw.strip():
            return ""
        value = raw.strip()
        parsed = urllib.parse.urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            # Relative provider paths may still carry query credentials, e.g.
            # /messages?api_key=...; keep only the path.
            return urllib.parse.urlunsplit(("", "", parsed.path or value.split("?", 1)[0].split("#", 1)[0], "", ""))[
                :160
            ]
        host = parsed.hostname or parsed.netloc
        port = f":{parsed.port}" if parsed.port else ""
        return urllib.parse.urlunsplit((parsed.scheme, f"{host}{port}", parsed.path or "", "", ""))[:160]

    @staticmethod
    def _redact_diagnostic_text(text: str) -> str:
        if not text:
            return ""
        def _redact_url_query(match: re.Match[str]) -> str:
            value = match.group(0)
            parsed = urllib.parse.urlsplit(value)
            if not parsed.scheme or not parsed.netloc:
                return value
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

        redacted = re.sub(r"https?://[^\s,)>\]}]+", _redact_url_query, text)
        redacted = re.sub(
            r"(?i)\b(authorization)(\s*[:=]\s*)Bearer\s+[A-Za-z0-9._~+/=-]+",
            r"\1\2Bearer [redacted]",
            redacted,
        )
        redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", redacted)
        redacted = re.sub(r"\bsk-[A-Za-z0-9._-]+", "[redacted]", redacted)
        return re.sub(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|x-api-key)"
            r"(\s*[:=]\s*)([^\s,;&]+)",
            r"\1\2[redacted]",
            redacted,
        )

    @staticmethod
    def _log_line_timestamp(line: str) -> Optional[float]:
        marker = "ERROR "
        index = line.find(marker)
        if index < 0:
            return None
        raw = line[index + len(marker) : index + len(marker) + 19]
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            return None

    @classmethod
    def _summarize_log_error_payload(cls, payload: object) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if not isinstance(error, dict):
            error = payload

        name = str(error.get("name") or "OpenCode provider error").strip()
        cause = error.get("cause") if isinstance(error.get("cause"), dict) else {}
        code = str(cause.get("code") or error.get("code") or "").strip()
        url = cls._safe_url(error.get("url") or cause.get("path"))

        message = ""
        data = error.get("data")
        if isinstance(data, dict):
            message = str(data.get("message") or "").strip()
        if not message:
            message = str(error.get("message") or "").strip()
        message = cls._redact_diagnostic_text(message)

        details = name
        if code:
            details += f" ({code})"
        if url:
            details += f" while calling {url}"
        if message and message not in details:
            details += f": {message[:200]}"
        return details[:500]

    @staticmethod
    def _opencode_log_dirs() -> list[Path]:
        candidates: list[Path] = []
        data_home = os.environ.get("XDG_DATA_HOME")
        if data_home:
            candidates.append(Path(data_home).expanduser() / "opencode" / "log")
        candidates.append(Path.home() / ".local" / "share" / "opencode" / "log")
        candidates.append(Path.home() / "Library" / "Application Support" / "opencode" / "log")
        return candidates

    @staticmethod
    def _read_text_tail(path: Path, max_bytes: int = OPENCODE_LOG_TAIL_BYTES) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                offset = max(0, size - max_bytes)
                handle.seek(offset)
                if offset > 0:
                    handle.readline()
                return handle.read(max_bytes).decode(errors="replace")
        except Exception:
            return ""

    def _recent_session_error_sync(self, session_id: str, since: Optional[float] = None) -> Optional[str]:
        if not session_id:
            return None
        log_files: list[Path] = []
        for directory in self._opencode_log_dirs():
            try:
                if directory.is_dir():
                    log_files.extend(path for path in directory.glob("*.log") if path.is_file())
            except Exception:
                continue
        for path in sorted(log_files, key=lambda item: item.stat().st_mtime, reverse=True)[:3]:
            text = self._read_text_tail(path)
            if not text:
                continue
            for line in reversed(text.splitlines()):
                if "ERROR" not in line or f"session.id={session_id}" not in line or "error=" not in line:
                    continue
                if since is not None:
                    log_ts = self._log_line_timestamp(line)
                    if log_ts is None or log_ts < int(since):
                        continue
                start = line.find("error={")
                if start < 0:
                    continue
                blob = self._extract_json_object(line, start + len("error="))
                if not blob:
                    continue
                try:
                    payload = json.loads(blob)
                except Exception:
                    continue
                summary = self._summarize_log_error_payload(payload)
                if summary:
                    return summary
        return None

    def get_last_prompt_started_at(self, session_id: str) -> Optional[float]:
        return self._last_prompt_started_at.get(session_id)

    async def get_recent_session_error(self, session_id: str, since: Optional[float] = None) -> Optional[str]:
        if since is None:
            since = self.get_last_prompt_started_at(session_id)
        return await asyncio.to_thread(self._recent_session_error_sync, session_id, since)

    @staticmethod
    def _diagnostic_payload_message(payload: object) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return OpenCodeServerManager._redact_diagnostic_text(message.strip())[:240]
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return OpenCodeServerManager._redact_diagnostic_text(message.strip())[:240]
        return ""

    @staticmethod
    def _auth_json_api_key(provider_id: str) -> Optional[str]:
        try:
            auth_entries = read_opencode_provider_auth_entries(logger_instance=logger)
        except Exception as exc:
            logger.debug("Could not read OpenCode auth entries for provider diagnostic: %s", exc)
            return None
        auth_entry = auth_entries.get(provider_id)
        if not isinstance(auth_entry, dict) or auth_entry.get("type") != "api":
            return None
        key = auth_entry.get("key")
        return key if isinstance(key, str) and key else None

    @staticmethod
    def _append_provider_endpoint(base_url: str, endpoint_path: str) -> str:
        return f"{base_url.rstrip('/')}/{endpoint_path.lstrip('/')}"

    @classmethod
    def _suggest_api_base_url(cls, base_url: str) -> str:
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.path.rstrip("/").endswith("/v1"):
            return cls._safe_url(base_url)
        path = (parsed.path.rstrip("/") + "/v1") if parsed.path else "/v1"
        return cls._safe_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")))

    @staticmethod
    def _diagnostic_adapter(provider_id: str, provider_config: Dict[str, Any]) -> Optional[str]:
        adapter = get_opencode_custom_provider_adapter(provider_id, provider_config)
        if adapter in {"anthropic-compatible", "openai-compatible"}:
            return adapter
        npm = provider_config.get("npm")
        if provider_id == "anthropic" or npm == "@ai-sdk/anthropic":
            return "anthropic-compatible"
        if provider_id == "openai" or npm == "@ai-sdk/openai-compatible":
            return "openai-compatible"
        return None

    def _provider_api_diagnostic_sync(self, provider_id: str, model_id: str) -> Optional[str]:
        probe = load_first_opencode_user_config(logger_instance=logger)
        config = probe.config
        if not isinstance(config, dict):
            return None
        provider_map = config.get("provider")
        if not isinstance(provider_map, dict):
            return None
        provider_config = provider_map.get(provider_id)
        if not isinstance(provider_config, dict):
            return None
        options = provider_config.get("options")
        if not isinstance(options, dict):
            return None
        base_url = options.get("baseURL")
        api_key = options.get("apiKey")
        if not isinstance(api_key, str) or not api_key:
            api_key = self._auth_json_api_key(provider_id)
        if not isinstance(base_url, str) or not base_url.strip() or not isinstance(api_key, str) or not api_key:
            return None

        base_url = base_url.rstrip("/")
        adapter = self._diagnostic_adapter(provider_id, provider_config)
        if adapter == "anthropic-compatible":
            endpoint_path = "/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": model_id,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "OK"}],
            }
        elif adapter == "openai-compatible":
            endpoint_path = "/chat/completions"
            headers = {
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            }
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": "OK"}],
                "stream": False,
            }
        else:
            return None

        try:
            request = urllib.request.Request(
                self._append_provider_endpoint(base_url, endpoint_path),
                data=json.dumps(body).encode("utf-8"),
                method="POST",
            )
            for key, value in headers.items():
                request.add_header(key, value)
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    content_type = response.headers.get("content-type", "")
                    raw = response.read(2048).decode(errors="replace")
                    if "text/html" in content_type.lower() or raw.lstrip().lower().startswith("<!doctype html"):
                        return (
                            f"Provider Base URL {self._safe_url(base_url)} returned an HTML page instead of an API "
                            f"response; use the API base path, usually {self._suggest_api_base_url(base_url)}."
                        )
                    return None
            except urllib.error.HTTPError as err:
                content_type = err.headers.get("content-type", "")
                raw = err.read(2048).decode(errors="replace")
                if "text/html" in content_type.lower() or raw.lstrip().lower().startswith("<!doctype html"):
                    return (
                        f"Provider Base URL {self._safe_url(base_url)} returned an HTML page instead of an API "
                        f"response; use the API base path, usually {self._suggest_api_base_url(base_url)}."
                    )
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                message = self._diagnostic_payload_message(payload)
                if message:
                    return f"Provider API returned HTTP {err.code}: {message}"
                return f"Provider API returned HTTP {err.code}."
            except urllib.error.URLError as err:
                reason = self._redact_diagnostic_text(str(err.reason or err))
                return f"Provider API request failed: {reason[:240]}"
            except (TimeoutError, OSError) as err:
                reason = self._redact_diagnostic_text(str(err))
                return f"Provider API request failed: {reason[:240] or type(err).__name__}"
        except Exception as err:
            logger.debug("OpenCode provider API diagnostic failed for %s/%s: %s", provider_id, model_id, err)
        return None

    async def get_provider_api_diagnostic(self, provider_id: str, model_id: str) -> Optional[str]:
        return await asyncio.to_thread(self._provider_api_diagnostic_sync, provider_id, model_id)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return runtime.pid_alive(pid)

    @staticmethod
    def _get_pid_command(pid: int) -> Optional[str]:
        return runtime.get_process_command(pid)

    @staticmethod
    def _is_opencode_serve_cmd(command: str, port: int) -> bool:
        if not command:
            return False
        return "opencode" in command and " serve" in command and f"--port={port}" in command

    @staticmethod
    def _pid_owns_listening_port(pid: int, port: int) -> bool:
        if os.name == "nt" or pid <= 0:
            return False

        proc_fd_dir = f"/proc/{pid}/fd"
        try:
            fd_entries = os.listdir(proc_fd_dir)
        except OSError:
            return False

        inodes: set[str] = set()
        for entry in fd_entries:
            try:
                target = os.readlink(f"{proc_fd_dir}/{entry}")
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])

        if not inodes:
            return False

        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, encoding="utf-8") as handle:
                    next(handle, None)
                    for raw_line in handle:
                        parts = raw_line.split()
                        if len(parts) < 10:
                            continue
                        local_address = parts[1]
                        state = parts[3]
                        inode = parts[9]
                        if state != "0A" or inode not in inodes:
                            continue
                        _, _, port_hex = local_address.rpartition(":")
                        try:
                            if int(port_hex, 16) == port:
                                return True
                        except ValueError:
                            continue
            except OSError:
                continue

        return False

    def _is_port_available(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
            return True
        except OSError:
            return False

    @staticmethod
    def _find_opencode_serve_pids(port: int) -> List[int]:
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception:
                return []

            pids: List[int] = []
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                local_addr = parts[1]
                state = parts[3].upper()
                pid_str = parts[4]
                if state != "LISTENING" or local_addr.rsplit(":", 1)[-1] != str(port):
                    continue
                try:
                    pid = int(pid_str)
                except ValueError:
                    continue
                command = runtime.get_process_command(pid)
                if command and OpenCodeServerManager._is_opencode_serve_cmd(command, port):
                    pids.append(pid)
            return pids

        try:
            result = subprocess.run(
                ["ps", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return []

        needle = f"--port={port}"
        pids: List[int] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_str, cmd = parts
            if "opencode" in cmd and " serve" in cmd and needle in cmd:
                try:
                    pids.append(int(pid_str))
                except ValueError:
                    continue
        return pids

    async def _terminate_pid(self, pid: int, reason: str) -> None:
        logger.info(f"Stopping OpenCode server pid={pid} ({reason})")
        if not runtime.stop_pid(pid, timeout=5) and self._pid_exists(pid):
            logger.debug("Failed to terminate OpenCode server pid=%s", pid)

    async def _cleanup_orphaned_managed_server(self) -> None:
        info = self._read_pid_file()
        if not info:
            return

        pid = info.get("pid")
        port = info.get("port")
        if not isinstance(pid, int) or port != self.port:
            self._clear_pid_file()
            return

        if self._process and self._process.returncode is None and self._process.pid == pid:
            return

        # Check if the server is healthy before deciding to kill it.
        # If it's healthy, we should adopt it rather than kill it.
        if await self._is_healthy():
            # Update PID file to reflect the actual running process.
            # The PID in the file may be stale if OpenCode was restarted externally.
            actual_pids = self._find_opencode_serve_pids(self.port)
            if actual_pids:
                actual_pid = actual_pids[0]
                if actual_pid != pid:
                    logger.info(f"Adopting healthy OpenCode server (updating stale PID {pid} -> {actual_pid})")
                    self._write_pid_file(actual_pid)
                else:
                    logger.info(f"Adopting healthy OpenCode server pid={pid} from previous run")
            else:
                # Server is healthy but we can't find its PID - clear stale file
                logger.info(f"Adopting healthy OpenCode server (clearing stale PID file, pid={pid} not found)")
                self._clear_pid_file()
            return

        cmd = self._get_pid_command(pid)
        if self._pid_exists(pid):
            if cmd and self._is_opencode_serve_cmd(cmd, self.port):
                await self._terminate_pid(pid, reason="orphaned and unhealthy")
            elif cmd is None and self._pid_owns_listening_port(pid, self.port):
                logger.info(
                    "Trusting OpenCode pid file for orphan cleanup pid=%s because it still owns port %s",
                    pid,
                    self.port,
                )
                await self._terminate_pid(pid, reason="orphaned and unhealthy")
        self._clear_pid_file()

    async def ensure_running(self) -> str:
        async with self._get_lock():
            if self._auth_refresh_pending and self._active_requests == 0 and not self._has_active_run_sessions():
                await self._restart_for_auth_refresh_locked()
            await self._cleanup_orphaned_managed_server()

            if await self._is_healthy():
                # If the server is already running (e.g., started by a previous run),
                # record its PID so shutdown can clean it up.
                if not self._read_pid_file():
                    pids = self._find_opencode_serve_pids(self.port)
                    if pids:
                        pid = pids[0]
                        cmd = self._get_pid_command(pid)
                        if cmd and self._is_opencode_serve_cmd(cmd, self.port):
                            self._write_pid_file(pid)

                self._base_url = f"http://{self.host}:{self.port}"
                return self.base_url

            if not self._is_port_available():
                for pid in self._find_opencode_serve_pids(self.port):
                    await self._terminate_pid(pid, reason="port occupied but unhealthy")
                await asyncio.sleep(0.5)

            if not self._is_port_available():
                raise RuntimeError(
                    f"OpenCode port {self.port} is already in use but the server is not responding. "
                    "Stop the process using this port or set OPENCODE_PORT to a free port."
                )

            await self._start_server()
            return self.base_url

    async def _is_healthy(self) -> bool:
        try:
            session = await self._get_http_session()
            async with session.get(f"{self.base_url}/global/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("healthy", False)
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
        return False

    async def _start_server(self) -> None:
        # ``self._process`` may be a stale subprocess from a previous
        # ``asyncio.run()`` call (the Flask UI server creates a new loop
        # per request, while ``OpenCodeServerManager`` is a singleton).
        # ``terminate_process_tree`` calls ``process.wait()`` which uses
        # the transport's internal Future — that Future is bound to the
        # loop that created the subprocess, and awaiting it from any
        # other loop raises "got Future attached to a different loop".
        # The OS-level pid signaling below (``_terminate_pid`` via
        # ``runtime.stop_pid``) is loop-agnostic and is the correct
        # cleanup path; just detach the dangling Python object first.
        current_loop = asyncio.get_running_loop()
        if (
            self._process
            and self._process.returncode is None
            and self._process_loop is current_loop
        ):
            await terminate_process_tree(self._process, logger, "OpenCode server", terminate_timeout=5)
        elif self._process and self._process_loop is not current_loop:
            # Foreign-loop subprocess. We can't trust ``returncode`` here
            # — the transport callbacks fire on the original (now-closed)
            # loop, so the cached ``returncode`` stays ``None`` even if
            # the OS process exited long ago. Worse, the PID may have
            # been reused by an unrelated process. Only OS-signal when
            # we can confirm the PID still owns an OpenCode serve
            # cmdline; otherwise just drop the dangling Python object.
            # ``_find_opencode_serve_pids`` + ``_cleanup_orphaned_
            # managed_server`` (called earlier in ``ensure_running``)
            # pick up any true orphan from a separate, pid-file-backed
            # path that doesn't rely on this dead reference.
            stale_pid = getattr(self._process, "pid", None)
            if isinstance(stale_pid, int) and self._pid_exists(stale_pid):
                cmd = self._get_pid_command(stale_pid)
                if cmd and self._is_opencode_serve_cmd(cmd, self.port):
                    await self._terminate_pid(stale_pid, reason="foreign-loop cleanup")
            self._process = None
            self._process_loop = None

        # Ensure any stale pid file is cleared before starting.
        self._clear_pid_file()

        cmd = [
            self.binary,
            "serve",
            f"--hostname={self.host}",
            f"--port={self.port}",
        ]

        logger.info(f"Starting OpenCode server: {' '.join(cmd)}")

        env = os.environ.copy()
        env["OPENCODE_ENABLE_EXA"] = "1"

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                **isolated_subprocess_kwargs(),
            )
            # Pair the subprocess object with the loop it was created on
            # so a future request from another loop can detach it
            # safely before issuing ``process.wait()``.
            self._process_loop = current_loop
            if self._process and self._process.pid:
                self._write_pid_file(self._process.pid)
        except FileNotFoundError:
            raise RuntimeError(
                f"OpenCode CLI not found at '{self.binary}'. Please install OpenCode or set OPENCODE_CLI_PATH."
            )

        start_time = time.monotonic()
        while time.monotonic() - start_time < SERVER_START_TIMEOUT:
            if await self._is_healthy():
                self._base_url = f"http://{self.host}:{self.port}"
                logger.info(f"OpenCode server started at {self._base_url}")
                return
            await asyncio.sleep(0.5)

        exit_code = self._process.returncode
        self._clear_pid_file()
        self._process = None
        self._process_loop = None
        raise RuntimeError(
            f"OpenCode server failed to start within {SERVER_START_TIMEOUT}s. Process exit code: {exit_code}"
        )

    async def stop(self) -> None:
        async with self._get_lock():
            await self._close_http_session_locked()

            # Don't terminate OpenCode server on vibe-remote shutdown.
            # Let it continue running so the next vibe-remote instance can adopt it.
            # This prevents interrupting tasks that are still in progress.
            logger.info("OpenCode server left running for next vibe-remote instance to adopt")

            # Keep pid_file so next instance knows about the running server.
            self._process = None
            self._process_loop = None

    def stop_sync(self) -> None:
        if self._http_session and self._http_session_loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self._http_session.close(), self._http_session_loop)
                future.result(timeout=5)
            except Exception as e:
                logger.debug(f"Failed to close OpenCode HTTP session: {e}")
            finally:
                self._http_session = None
                self._http_session_loop = None

        # Don't terminate OpenCode server on vibe-remote shutdown.
        # Let it continue running so the next vibe-remote instance can adopt it.
        # This prevents interrupting tasks that are still in progress.
        logger.info("OpenCode server left running for next vibe-remote instance to adopt")

        # Keep pid_file so next instance knows about the running server.
        # Don't clear _process reference - just let it be garbage collected.
        self._process = None
        self._process_loop = None

    async def restart_for_auth_refresh(self) -> None:
        """Terminate the shared server so the next request reloads refreshed auth."""
        async with self._get_lock():
            if self._active_requests > 0 or self._has_active_run_sessions():
                self._auth_refresh_pending = True
                logger.info(
                    "Deferring OpenCode auth refresh restart until %s active request(s) and %s active run(s) finish",
                    self._active_requests,
                    len(self._active_run_sessions),
                )
                return
            await self._restart_for_auth_refresh_locked()

    @classmethod
    def stop_instance_sync(cls) -> None:
        if cls._instance:
            cls._instance.stop_sync()
            return

        # Don't terminate OpenCode server on vibe-remote shutdown.
        # Let it continue running so the next vibe-remote instance can adopt it.
        logger.info("OpenCode server left running for next vibe-remote instance to adopt")

    async def create_session(self, directory: str, title: Optional[str] = None) -> Dict[str, Any]:
        async with self._request_scope():
            session = await self._get_http_session()
            body: Dict[str, Any] = {}
            if title:
                body["title"] = title

            async with session.post(
                f"{self.base_url}/session",
                json=body,
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Failed to create session: {resp.status} {text}")
                return await resp.json()

    async def fork_session(
        self,
        source_session_id: str,
        directory: str,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._request_scope():
            session = await self._get_http_session()
            body: Dict[str, Any] = {}
            if message_id:
                body["messageID"] = message_id
            async with session.post(
                f"{self.base_url}/session/{source_session_id}/fork",
                json=body,
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Failed to fork session: {resp.status} {text}")
                return await resp.json()

    async def send_message(
        self,
        session_id: str,
        directory: str,
        text: str,
        agent: Optional[str] = None,
        model: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._request_scope():
            session = await self._get_http_session()

            body: Dict[str, Any] = {
                "parts": [{"type": "text", "text": text}],
            }
            if agent:
                body["agent"] = agent
            if model:
                body["model"] = model
            variant = self._normalize_variant(reasoning_effort)
            if variant:
                body["variant"] = variant

            async with session.post(
                f"{self.base_url}/session/{session_id}/message",
                json=body,
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to send message: {resp.status} {error_text}")
                return await resp.json()

    async def prompt_async(
        self,
        session_id: str,
        directory: str,
        text: str,
        agent: Optional[str] = None,
        model: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
        system: Optional[str] = None,
        tools: Optional[Dict[str, bool]] = None,
    ) -> None:
        """Start a prompt asynchronously without holding the HTTP request open."""

        started_at = time.time()
        async with self._request_scope():
            session = await self._get_http_session()

            body: Dict[str, Any] = {
                "parts": [{"type": "text", "text": text}],
            }
            if agent:
                body["agent"] = agent
            if model:
                body["model"] = model
            variant = self._normalize_variant(reasoning_effort)
            if variant:
                body["variant"] = variant
            if system:
                body["system"] = system
            if tools:
                body["tools"] = tools

            async with session.post(
                f"{self.base_url}/session/{session_id}/prompt_async",
                json=body,
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                # OpenCode returns 204 when accepted.
                if resp.status not in (200, 204):
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to start async prompt: {resp.status} {error_text}")
            self._last_prompt_started_at[session_id] = started_at

    async def list_messages(self, session_id: str, directory: str) -> List[Dict[str, Any]]:
        async with self._request_scope():
            session = await self._get_http_session()
            async with session.get(
                f"{self.base_url}/session/{session_id}/message",
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to list messages: {resp.status} {error_text}")
                return await resp.json()

    async def get_message(self, session_id: str, message_id: str, directory: str) -> Dict[str, Any]:
        async with self._request_scope():
            session = await self._get_http_session()
            async with session.get(
                f"{self.base_url}/session/{session_id}/message/{message_id}",
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to get message: {resp.status} {error_text}")
                return await resp.json()

    async def abort_session(self, session_id: str, directory: str) -> bool:
        async with self._request_scope():
            session = await self._get_http_session()

            try:
                async with session.post(
                    f"{self.base_url}/session/{session_id}/abort",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    return resp.status == 200
            except Exception as e:
                logger.warning(f"Failed to abort session {session_id}: {e}")
                return False

    async def get_session(
        self, session_id: str, directory: str, *, raise_on_error: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch a session. ``None`` means the server reported it does not exist.

        ``raise_on_error``: when True, a transport/connection error is re-raised
        instead of being collapsed into ``None`` — so a caller validating an
        existing session can tell "genuinely gone" (None) from "couldn't reach the
        server" (raise) and not mislabel a transient blip as session expiry.
        """
        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/session/{session_id}",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    # Only a genuine "not found" means the session is gone. Other
                    # non-200s (transient 500/503, auth 401) are NOT expiry — when a
                    # caller is validating an existing session (raise_on_error), raise
                    # so it surfaces as a transient/auth failure rather than being
                    # mislabeled as session expiry / context loss (Codex P2).
                    if resp.status == 404:
                        return None
                    if raise_on_error:
                        error_text = await resp.text()
                        raise RuntimeError(
                            f"get session {session_id} failed: HTTP {resp.status} {error_text[:300]}"
                        )
                    return None
            except Exception as e:
                logger.debug(f"Failed to get session {session_id}: {e}")
                if raise_on_error:
                    raise
                return None

    async def get_available_agents(self, directory: str) -> List[Dict[str, Any]]:
        """Fetch available agents from OpenCode server.

        Returns:
            List of agent dicts with 'name', 'mode', 'native', etc.
        """

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/agent",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    if resp.status == 200:
                        agents = await resp.json()
                        # Filter to primary agents (build, plan), exclude hidden/subagent
                        return [a for a in agents if a.get("mode") == "primary" and not a.get("hidden", False)]
                    return []
            except Exception as e:
                logger.warning(f"Failed to get available agents: {e}")
                return []

    async def get_available_models(self, directory: str) -> Dict[str, Any]:
        """Fetch available models from OpenCode server.

        Returns:
            Dict with 'providers' list and 'default' dict mapping provider to default model.
        """

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/config/providers",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"providers": [], "default": {}}
            except Exception as e:
                logger.warning(f"Failed to get available models: {e}")
                return {"providers": [], "default": {}}

    async def get_default_config(self, directory: str) -> Dict[str, Any]:
        """Fetch current default config from OpenCode server.

        Returns:
            Config dict including 'model' (current default), 'agent' configs, etc.
        """

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/config",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {}
            except Exception as e:
                logger.warning(f"Failed to get default config: {e}")
                return {}

    async def set_api_key_auth(self, provider_id: str, api_key: str) -> None:
        """Persist provider API auth via OpenCode's own auth endpoint."""

        await self.ensure_running()

        async with self._request_scope():
            session = await self._get_http_session()
            async with session.put(
                f"{self.base_url}/auth/{provider_id}",
                json={"type": "api", "key": api_key},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to set OpenCode auth: {resp.status} {error_text}")

    async def remove_provider_auth(self, provider_id: str) -> None:
        """Drop a provider's stored credentials via OpenCode's auth endpoint.

        Used by the Settings UI's "Remove key" action. OpenCode treats 404
        as already-removed, which we silently accept so the UI can issue
        DELETE optimistically without first checking presence.
        """

        await self.ensure_running()

        async with self._request_scope():
            session = await self._get_http_session()
            async with session.delete(
                f"{self.base_url}/auth/{provider_id}",
            ) as resp:
                if resp.status in (200, 204, 404):
                    return
                error_text = await resp.text()
                raise RuntimeError(
                    f"Failed to remove OpenCode auth for {provider_id}: {resp.status} {error_text}"
                )

    async def get_providers(self) -> Dict[str, Any]:
        """Fetch the full provider catalog from the running OpenCode server.

        Returns the raw shape OpenCode reports: ``{all: {...}, default:
        {...}, connected: [...]}``. Callers (``vibe.api.get_opencode_providers``)
        merge this with the auth-method map from ``get_provider_auth`` to
        produce the per-card ``configured`` / ``oauth_available`` /
        ``local`` flags surfaced in the Settings UI.
        """

        await self.ensure_running()

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(f"{self.base_url}/provider") as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {}
            except Exception as e:
                logger.warning(f"Failed to get OpenCode providers: {e}")
                return {}

    async def start_provider_oauth(
        self,
        provider_id: str,
        *,
        method: int = 0,
        prompt_answers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Kick off a per-provider OAuth authorize via OpenCode's HTTP API.

        OpenCode 1.14 exposes ``POST /provider/<id>/oauth/authorize`` with
        ``{method, ...prompt_answers}`` (the method index is the position
        in ``/provider/auth[provider_id]``) and returns
        ``{url, method, instructions}``. ``instructions`` carries the
        user-facing device code for device-auth flows (e.g.
        ``"Enter code: AB1C-D2E3"``); browser-redirect flows omit it.

        For providers with prompts (e.g. github-copilot's deployment
        type), ``prompt_answers`` is merged into the body so the caller
        can pre-answer (e.g. ``{"deploymentType": "github.com"}``).
        """
        await self.ensure_running()
        payload = {"method": method}
        if prompt_answers:
            payload.update(prompt_answers)
        async with self._request_scope():
            session = await self._get_http_session()
            async with session.post(
                f"{self.base_url}/provider/{provider_id}/oauth/authorize",
                json=payload,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"OpenCode authorize failed for {provider_id}: {resp.status} {text}"
                    )
                try:
                    return await resp.json()
                except Exception:  # pragma: no cover - parse-defensive
                    return json.loads(text) if text else {}

    async def wait_provider_oauth(
        self,
        provider_id: str,
        *,
        method: int = 0,
        prompt_answers: Optional[Dict[str, Any]] = None,
        timeout: float = 900.0,
    ) -> Dict[str, Any]:
        """Block until ``POST /provider/<id>/oauth/callback`` resolves.

        OpenCode polls the provider's token endpoint (device flow) or
        catches the local HTTP callback (browser flow) and returns when
        the credentials have been minted and persisted into auth.json.
        We bound the wait at 900 s — same as the IM ``/setup`` timeout.

        Uses a dedicated short-lived ``ClientSession`` rather than the
        shared ``_get_http_session()`` cache. Any other code path that
        runs on a different event loop (e.g. a Flask request hitting
        ``_get_opencode_providers_async`` via ``asyncio.run``) would
        otherwise notice the cached session is bound to a stale loop,
        close it, and recreate — which would mid-flight disconnect the
        long-poll on the OAuth event loop with
        ``aiohttp.ServerDisconnectedError``.
        """
        await self.ensure_running()
        payload = {"method": method}
        if prompt_answers:
            payload.update(prompt_answers)
        # Skip ``_request_scope`` (the per-call semaphore) too — it
        # serialises all OpenCode HTTP calls behind a single lock, so
        # holding it for 15 minutes would block every other UI request
        # (provider list, save, restart, ...). The OAuth callback is
        # idempotent server-side and safe to run concurrently.
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/provider/{provider_id}/oauth/callback",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"OpenCode callback failed for {provider_id}: {resp.status} {text}"
                    )
                try:
                    return await resp.json()
                except Exception:  # pragma: no cover - parse-defensive
                    return json.loads(text) if text else {}

    async def forward_oauth_redirect(
        self,
        provider_id: str,
        callback_url: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        """Forward a manually-pasted callback URL to OpenCode's listener.

        Browser-redirect OAuth flows (poe, gitlab, openai-browser) end
        with the provider redirecting to ``http://127.0.0.1:<port>/callback?...``
        — a port OpenCode opens fresh per flow. From a *local* browser
        that's automatic; from a remote browser (Vibe Remote regression
        env, vibe_cloud tunnel, …) that URL is unreachable because the
        loopback address belongs to the daemon's host, not the user's
        machine.

        This helper takes the URL the user pastes, validates that it
        targets a 127.0.0.1 callback (don't blindly fetch external
        URLs), and replays it from inside the container so OpenCode's
        listener consumes it. ``wait_provider_oauth`` then returns
        success on its own thread.
        """
        parsed = urllib.parse.urlparse(callback_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError(
                f"callback_url must target 127.0.0.1, got host={parsed.hostname!r}"
            )
        # ``provider_id`` isn't part of the OpenCode-managed URL — keep
        # the parameter so the caller doesn't need to special-case
        # which provider matches the loopback port. Forwarding any
        # 127.0.0.1/<path> URL to OpenCode is safe because only the
        # listener bound by ``authorize`` will accept it.
        _ = provider_id  # noqa: F841 — kept for symmetry / future routing
        async with aiohttp.ClientSession() as session:
            async with session.get(
                callback_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                # OpenCode replies with 200 + HTML on success or 4xx on
                # bad state; we don't try to interpret the body — the
                # blocking ``wait_provider_oauth`` will surface the real
                # outcome via flow state.
                _ = await resp.read()

    async def get_provider_auth(self) -> Dict[str, Any]:
        """Fetch the per-provider auth-method index from OpenCode.

        Shape is ``{providerId: [{type, label?, ...}, ...]}`` — providers
        that support OAuth surface a ``{"type": "oauth", ...}`` entry,
        local providers (Ollama / LM Studio) report an empty list.
        """

        await self.ensure_running()

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(f"{self.base_url}/provider/auth") as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {}
            except Exception as e:
                logger.warning(f"Failed to get OpenCode provider/auth: {e}")
                return {}

    def _load_opencode_user_config(self) -> Optional[Dict[str, Any]]:
        """Load and cache opencode.json config file.

        Checks both ~/.config/opencode/opencode.json and ~/.opencode/opencode.json
        since OpenCode supports multiple config locations.

        Returns:
            Parsed config dict, or None if file doesn't exist or is invalid.
        """
        probe = load_first_opencode_user_config(logger_instance=logger)
        return probe.config

    def _merge_global_config_snapshot(self, current_config: Dict[str, Any], user_config: Dict[str, Any]) -> Dict[str, Any]:
        try:
            auth_entries = read_opencode_provider_auth_entries(logger_instance=logger)
        except Exception as exc:
            logger.debug("Could not read OpenCode auth entries during global config merge: %s", exc)
            auth_entries = {}
        return OpenCodeConfigReconciler().reconcile(
            user_config=user_config,
            live_config=current_config,
            auth_entries=auth_entries,
        )

    async def _get_global_config_snapshot(self) -> Optional[Dict[str, Any]]:
        total_timeout: Optional[int] = None if self.request_timeout_seconds <= 0 else self.request_timeout_seconds
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total_timeout)) as session:
            async with session.get(f"{self.base_url}/global/config") as resp:
                if resp.status != 200:
                    await resp.read()
                    return None
                data = await resp.json()
                return data if isinstance(data, dict) else None

    def _get_agent_config(self, config: Dict[str, Any], agent_name: Optional[str]) -> Dict[str, Any]:
        """Get agent-specific config from opencode.json with type safety."""

        if not agent_name:
            return {}
        agents = config.get("agent", {})
        if not isinstance(agents, dict):
            return {}
        agent_config = agents.get(agent_name, {})
        if not isinstance(agent_config, dict):
            return {}
        return agent_config

    def get_agent_model_from_config(self, agent_name: Optional[str]) -> Optional[str]:
        """Read agent's default model from user's opencode.json config file.

        This is a workaround for OpenCode server not using agent-specific models
        when only the agent parameter is passed to the message API.
        """

        config = self._load_opencode_user_config()
        if not config:
            return None

        # Try agent-specific model first
        agent_config = self._get_agent_config(config, agent_name)
        model = agent_config.get("model")
        if isinstance(model, str) and model:
            logger.debug(f"Found model '{model}' for agent '{agent_name}' in opencode.json")
            return model

        # Fall back to global default model
        model = config.get("model")
        if isinstance(model, str) and model:
            logger.debug(f"Using global default model '{model}' from opencode.json")
            return model
        return None

    def get_agent_reasoning_effort_from_config(self, agent_name: Optional[str]) -> Optional[str]:
        """Read agent's reasoningEffort from user's opencode.json config file."""

        config = self._load_opencode_user_config()
        if not config:
            return None

        # Valid reasoning effort values
        valid_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

        # Try agent-specific reasoningEffort first
        agent_config = self._get_agent_config(config, agent_name)
        reasoning_effort = agent_config.get("reasoningEffort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            if reasoning_effort in valid_efforts:
                logger.debug(f"Found reasoningEffort '{reasoning_effort}' for agent '{agent_name}' in opencode.json")
                return reasoning_effort
            else:
                logger.debug(f"Ignoring unknown reasoningEffort '{reasoning_effort}' for agent '{agent_name}'")

        # Fall back to global default reasoningEffort
        reasoning_effort = config.get("reasoningEffort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            if reasoning_effort in valid_efforts:
                logger.debug(f"Using global default reasoningEffort '{reasoning_effort}' from opencode.json")
                return reasoning_effort
            else:
                logger.debug(f"Ignoring unknown global reasoningEffort '{reasoning_effort}'")
        return None

    def get_default_agent_from_config(self) -> Optional[str]:
        """Read the default agent from user's opencode.json config file.

        OpenCode server doesn't automatically use its configured default agent
        when called via API, so we need to read and pass it explicitly.
        """

        # OpenCode doesn't have an explicit "default agent" config field.
        # Users can override via channel settings.
        # Default to "build" agent which uses the agent's configured model,
        # avoiding fallback to global model which may use restricted credentials.
        return "build"
