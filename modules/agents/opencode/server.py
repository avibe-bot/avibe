"""OpenCode runtime generations: process lifecycle and HTTP API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import time
from urllib.parse import quote as _url_quote
import urllib.error
import urllib.parse
import urllib.request
from asyncio.subprocess import Process
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import aiohttp
import psutil

from config import paths
from config.atomic_io import write_atomic
from core.handlers.model_hub.identifiers import OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL
from core.process_isolation import isolated_subprocess_kwargs
from modules.agents.opencode.caller_context import ensure_plugin_installed, server_environment
from vibe import runtime
from vibe.desktop_runtime import DESKTOP_OPENCODE_ROLE, DESKTOP_ROLE_ENV, desktop_caller_provenance
from vibe.opencode_config import (
    OPENCODE_REASONING_VARIANTS,
    OpenCodeRuntimeConfigInvalidError,
    get_opencode_custom_provider_adapter,
    load_first_opencode_user_config,
    managed_opencode_runtime_config_content as _managed_runtime_config_content,
    read_opencode_provider_auth_entries,
)

logger = logging.getLogger(__name__)

DEFAULT_OPENCODE_HOST = "127.0.0.1"
# A cold OpenCode process can take close to a minute to load on a busy or
# freshly provisioned host. This is only a ceiling: a healthy start returns as
# soon as ``/global/health`` answers, and it is not a request timeout.
SERVER_START_TIMEOUT = 120
# ``/global/health`` only proves the process is serving. OpenCode bootstraps a
# per-directory instance on that directory's first request, and with any plugin
# configured (Avibe always installs one) the bootstrap waits for OpenCode's own
# npm install into each config directory that has no ``node_modules`` yet: about
# a minute on a fresh host. The bootstrap keeps running inside OpenCode if the
# client gives up, so a later request joins it. This is a readiness ceiling, not
# a request timeout.
DIRECTORY_BOOTSTRAP_TIMEOUT = 300
OPENCODE_LOG_TAIL_BYTES = 2_000_000
# Ports Avibe chooses can be taken by another process before OpenCode binds.
_PORT_ATTEMPTS = 3
GENERATION_RECORD_SCHEMA = 1
# The spec of a server adopted from the single-server record of a release
# before generations. No new turn's spec ever equals it.
LEGACY_SPEC_DIGEST = "legacy"
# A record written before the desktop Runtime id was recorded.
_UNRECORDED_RUNTIME_ID = object()
# Generations a runtime of this process is starting or has attached, with the
# process each one runs. Adoption leaves them to that runtime, for example the
# one of an OpenCode backend disabled and enabled again while its turn still
# runs; it stops them once their work drains. A started process is owned from
# its spawn, before its record is written, until a stop proves it gone or its
# start proves it exited, so this controller's shutdown stops it, recorded or not.
_OWNED_HERE: dict[str, tuple[int, Optional[float]]] = {}
# A busy server after a controller crash can miss one health probe; adoption
# gives it this many before stopping it.
_ADOPTION_PROBES = 3
_ADOPTION_PROBE_INTERVAL_SECONDS = 2.0
_DURABLE_ATTEMPT_ID_RE = re.compile(r"^atm_([0-9a-f]{32})$")
# Bump whenever the process-level Avibe policy applied at launch changes. It is
# a launch spec input, so the next turn starts a generation under the new policy.
_MANAGED_RUNTIME_POLICY_REVISION = "disable-native-skill-v2"


def _percent_encode_path(path: str) -> str:
    """Percent-encode *path* so it is safe for an HTTP header value.

    RFC 7230 only allows visible US-ASCII characters (plus whitespace)
    in header field values.  Non-ASCII bytes (e.g. CJK characters in
    project paths) must be percent-encoded, otherwise they will be
    misinterpreted by the receiving end.
    """
    return _url_quote(path, safe="/")


def project_opencode_model_hub_models(
    providers: list[Any],
    runtime_models: dict[str, Any],
) -> list[Any]:
    """Expose runtime models under their public provider/model identities."""

    projected = [dict(entry) if isinstance(entry, dict) else entry for entry in providers]
    provider_index = {
        entry.get("id"): entry
        for entry in projected
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    for public_identifier, model_config in runtime_models.items():
        if not isinstance(public_identifier, str) or not public_identifier:
            continue
        native_protocol = (
            model_config.get("native_protocol")
            if isinstance(model_config, dict)
            else None
        )
        provider_id = OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL.get(native_protocol)
        if provider_id is None:
            continue
        model_id = public_identifier
        provider = provider_index.get(provider_id)
        if provider is None:
            provider = {"id": provider_id, "name": provider_id, "models": {}}
            projected.append(provider)
            provider_index[provider_id] = provider
        raw_models = provider.get("models")
        if isinstance(raw_models, dict):
            models = dict(raw_models)
        elif isinstance(raw_models, list):
            models = {}
            for entry in raw_models:
                if isinstance(entry, str) and entry:
                    models[entry] = {"id": entry}
                    continue
                if not isinstance(entry, dict):
                    continue
                entry_id = entry.get("id") or entry.get("modelID") or entry.get("model_id")
                if isinstance(entry_id, str) and entry_id:
                    models[entry_id] = dict(entry)
        else:
            models = {}
        existing_model = models.get(model_id)
        public_model = dict(existing_model) if isinstance(existing_model, dict) else {}
        if isinstance(model_config, dict):
            public_model.update(model_config)
        public_model.pop("native_protocol", None)
        public_model["id"] = model_id
        metadata = public_model.get("vibe_remote")
        public_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        public_metadata["model_hub_projected"] = True
        public_model["vibe_remote"] = public_metadata
        models[model_id] = public_model
        provider["models"] = models
    return projected


def _public_opencode_catalog(
    payload: Any,
    *,
    runtime_provider_ids: tuple[str, ...],
    model_hub_models: dict[str, Any] | None = None,
) -> Any:
    """Remove private transport state and merge the desired public projection."""

    if not isinstance(payload, dict):
        return payload
    projected = dict(payload)

    def _provider_id(entry: object) -> object:
        if isinstance(entry, dict):
            return entry.get("id") or entry.get("provider_id") or entry.get("name")
        return entry

    for key in ("providers", "all"):
        value = projected.get(key)
        if isinstance(value, list):
            projected[key] = [
                entry
                for entry in value
                if _provider_id(entry) not in runtime_provider_ids
            ]
        elif isinstance(value, dict):
            projected[key] = {
                provider_id: entry
                for provider_id, entry in value.items()
                if provider_id not in runtime_provider_ids
            }

    connected = projected.get("connected")
    if isinstance(connected, list):
        projected["connected"] = [
            provider_id
            for provider_id in connected
            if provider_id not in runtime_provider_ids
        ]

    for key in ("default", "provider"):
        value = projected.get(key)
        if isinstance(value, dict):
            projected[key] = {
                provider_id: entry
                for provider_id, entry in value.items()
                if provider_id not in runtime_provider_ids
            }

    providers = projected.get("providers")
    if model_hub_models and isinstance(providers, list):
        projected["providers"] = project_opencode_model_hub_models(
            providers,
            model_hub_models,
        )

    model = projected.get("model")
    if (
        isinstance(model, str)
        and model.split("/", 1)[0] in runtime_provider_ids
    ):
        projected.pop("model", None)
    return projected


def native_part_id_for_attempt(attempt_id: str) -> str:
    """Map one durable attempt into OpenCode's part namespace."""

    value = str(attempt_id or "").strip()
    match = _DURABLE_ATTEMPT_ID_RE.fullmatch(value)
    if match is None:
        raise ValueError("OpenCode prompt attempt identity is not canonical")
    return f"prt_{match.group(1)}"


class OpenCodePromptRejectedError(RuntimeError):
    """Definitive HTTP rejection from OpenCode's async prompt endpoint."""

    def __init__(self, status: int, response_text: str) -> None:
        self.status = status
        self.response_text = response_text
        super().__init__(f"Failed to start async prompt: {status} {response_text}")

    @property
    def is_permanent_input_rejection(self) -> bool:
        return self.status == 400



class OpenCodeDirectoryBootstrapTimeoutError(RuntimeError):
    """OpenCode is still bootstrapping a directory after the readiness ceiling."""


class StopOutcome(str, Enum):
    """What a confirmed stop found once nothing about it was still pending."""

    # The process is gone and its record removed.
    STOPPED = "stopped"
    # Work or a lease still binds it; it stops once that drains.
    DRAINING = "draining"
    # Its stop ran and the process survived; its record stays for a retry.
    FAILED = "failed"


class OpenCodeGenerationStartError(RuntimeError):
    """A generation's ``opencode serve`` process did not become ready."""

    def __init__(self, message: str, *, exited_pid: int | None = None) -> None:
        super().__init__(message)
        # Set only when the process exited on its own: the evidence a resource
        # pressure diagnosis may consume. A process Avibe stopped for timeout is
        # not that evidence.
        self.exited_pid = exited_pid


def generation_records_dir() -> Path:
    """Where each generation's process record lives."""

    return paths.get_runtime_dir() / "opencode" / "generations"


def legacy_pid_file() -> Path:
    """The single-server record every release before generations wrote."""

    return paths.get_logs_dir() / "opencode_server.json"


def _pid_exists(pid: int) -> bool:
    return runtime.pid_alive(pid)


def _get_pid_command(pid: int) -> Optional[str]:
    return runtime.get_process_command(pid)


def _is_opencode_serve_cmd(command: str, port: int) -> bool:
    if not command:
        return False
    return "opencode" in command and " serve" in command and f"--port={port}" in command


def _pid_listens_on(pid: int, port: int) -> bool:
    """Whether ``pid``, or a process it started, holds the listening socket on ``port``.

    A launcher such as an npm shim or a Windows ``.cmd`` wrapper starts the
    server as its child, so the listener can belong to a descendant.
    """

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return False
    for process in processes:
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.Error:
            continue
        if any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and connection.laddr.port == port
            for connection in connections
        ):
            return True
    return False


def _choose_port(host: str) -> int:
    # ``opencode serve --port=0`` listens on its default 4096 rather than an
    # ephemeral port, so the port is chosen here. Another process can take it
    # before OpenCode binds; the start then sees its own process exit and
    # retries with another port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class OpenCodeServerClient:
    """The HTTP API of one ``opencode serve`` process at ``base_url``."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout_seconds: int = 60,
        model_hub_provider_ids: tuple[str, ...] = (),
    ) -> None:
        self.base_url = base_url
        self.request_timeout_seconds = request_timeout_seconds
        # Private Hub transport providers of the process behind ``base_url``;
        # user-facing catalogs never show them.
        self.model_hub_provider_ids = tuple(model_hub_provider_ids)
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_session_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_requests = 0
        self._last_prompt_started_at: dict[str, float] = {}
        # The event-loop time a lease on this process expires, for a client in
        # another process than the controller. No request starts that its
        # timeout could carry past it, so the process never stops under one.
        self.lease_expires_at: Optional[float] = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create an asyncio.Lock bound to the current event loop."""
        current_loop = asyncio.get_event_loop()
        if self._lock is None or self._lock_loop is not current_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = current_loop
        return self._lock

    def _active_model_hub_provider_ids(self) -> tuple[str, ...]:
        return self.model_hub_provider_ids

    @asynccontextmanager
    async def _request_scope(self):
        if (
            self.lease_expires_at is not None
            and asyncio.get_running_loop().time() + self.request_timeout_seconds > self.lease_expires_at
        ):
            raise TimeoutError(f"The OpenCode lease on {self.base_url} ends before this request could")
        self._active_requests += 1
        try:
            yield
        finally:
            self._active_requests = max(0, self._active_requests - 1)

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

        response_payload: object = error.get("responseBody")
        if isinstance(response_payload, str):
            try:
                response_payload = json.loads(response_payload)
            except (TypeError, ValueError):
                response_payload = None
        response_error = (
            response_payload.get("error")
            if isinstance(response_payload, dict)
            else None
        )
        if not isinstance(response_error, dict):
            response_error = response_payload if isinstance(response_payload, dict) else {}
        response_code = str(response_error.get("code") or "").strip()
        response_type = str(response_error.get("type") or "").strip()
        status_code = str(error.get("statusCode") or "").strip()

        message = ""
        data = error.get("data")
        if isinstance(data, dict):
            message = str(data.get("message") or "").strip()
        if not message:
            message = str(error.get("message") or "").strip()
        if not message:
            message = str(response_error.get("message") or "").strip()
        message = cls._redact_diagnostic_text(message)

        details = name
        qualifiers = [value for value in (code, response_code, response_type) if value]
        if status_code:
            qualifiers.append(f"HTTP {status_code}")
        if qualifiers:
            details += f" ({'; '.join(dict.fromkeys(qualifiers))})"
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
                    return OpenCodeServerClient._redact_diagnostic_text(message.strip())[:240]
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return OpenCodeServerClient._redact_diagnostic_text(message.strip())[:240]
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

    async def ensure_directory_ready(self, directory: str) -> None:
        """Wait until OpenCode has bootstrapped its instance for ``directory``.

        Any directory-scoped request waits for that bootstrap; an idempotent read
        takes the wait so no request with side effects can time out halfway.
        """
        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/path",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                    timeout=aiohttp.ClientTimeout(total=DIRECTORY_BOOTSTRAP_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"OpenCode could not prepare {directory}: {resp.status} {text}")
            except asyncio.TimeoutError as exc:
                raise OpenCodeDirectoryBootstrapTimeoutError(
                    f"OpenCode did not finish preparing {directory} within {DIRECTORY_BOOTSTRAP_TIMEOUT}s"
                ) from exc

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
        attempt_id: Optional[str] = None,
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

            text_part: Dict[str, Any] = {"type": "text", "text": text}
            if attempt_id:
                text_part["id"] = native_part_id_for_attempt(attempt_id)
            body: Dict[str, Any] = {
                "parts": [text_part],
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
                    raise OpenCodePromptRejectedError(resp.status, error_text)
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

    async def get_version(self) -> Optional[str]:
        """Return the running OpenCode version advertised by its health endpoint."""

        try:
            async with self._request_scope():
                session = await self._get_http_session()
                async with session.get(
                    f"{self.base_url}/global/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    version = data.get("version") if isinstance(data, dict) else None
                    return str(version).strip() if version else None
        except Exception as err:
            logger.debug("Failed to read OpenCode runtime version: %s", err)
            return None

    async def get_session_status(
        self,
        session_id: str,
        directory: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the installed OpenCode runtime status for one native session."""

        async with self._request_scope():
            session = await self._get_http_session()
            async with session.get(
                f"{self.base_url}/session/status",
                headers={"x-opencode-directory": _percent_encode_path(directory)},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"Failed to get session status: {resp.status} {error_text}")
                statuses = await resp.json()
                status = statuses.get(session_id) if isinstance(statuses, dict) else None
                return status if isinstance(status, dict) else None

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

    async def _get_available_models(
        self,
        directory: str,
        *,
        model_hub_models: dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Fetch the OpenCode catalog with the requested public projection."""

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(
                    f"{self.base_url}/config/providers",
                    headers={"x-opencode-directory": _percent_encode_path(directory)},
                ) as resp:
                    if resp.status == 200:
                        return _public_opencode_catalog(
                            await resp.json(),
                            runtime_provider_ids=self._active_model_hub_provider_ids(),
                            model_hub_models=model_hub_models,
                        )
                    return {"providers": [], "default": {}}
            except Exception as e:
                logger.warning(f"Failed to get available models: {e}")
                return {"providers": [], "default": {}}

    async def get_available_models(
        self,
        directory: str,
        *,
        model_hub_models: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Fetch the user-facing catalog, including exact Hub projections.

        Returns:
            Dict with 'providers' list and 'default' dict mapping provider to default model.
        """

        return await self._get_available_models(
            directory,
            model_hub_models=model_hub_models,
        )

    async def get_native_available_models(self, directory: str) -> Dict[str, Any]:
        """Fetch models that native provider configuration can probe directly."""

        return await self._get_available_models(
            directory,
            model_hub_models=None,
        )

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
                        return _public_opencode_catalog(
                            await resp.json(),
                            runtime_provider_ids=self._active_model_hub_provider_ids(),
                        )
                    return {}
            except Exception as e:
                logger.warning(f"Failed to get default config: {e}")
                return {}

    async def set_api_key_auth(self, provider_id: str, api_key: str) -> None:
        """Persist provider API auth via OpenCode's own auth endpoint."""

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
        """Fetch the user-visible provider catalog from the OpenCode server.

        Preserves the shape OpenCode reports: ``{all: {...}, default:
        {...}, connected: [...]}``. Callers (``vibe.api.get_opencode_providers``)
        merge this with the auth-method map from ``get_provider_auth`` to
        produce the per-card ``configured`` / ``oauth_available`` /
        ``local`` flags surfaced in the Settings UI.
        """

        async with self._request_scope():
            session = await self._get_http_session()
            try:
                async with session.get(f"{self.base_url}/provider") as resp:
                    if resp.status == 200:
                        return _public_opencode_catalog(
                            await resp.json(),
                            runtime_provider_ids=self._active_model_hub_provider_ids(),
                        )
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
        code: str | None = None,
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
        payload = {key: value for key, value in (prompt_answers or {}).items() if key not in {"method", "code"}}
        payload["method"] = method
        if code is not None:
            payload["code"] = code
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

    def get_explicit_subagent_model(self, agent_name: str) -> Optional[str]:
        """Read only the selected subagent's own model, never a native default."""
        config = self._load_opencode_user_config() or {}
        model = self._get_agent_config(config, agent_name).get("model")
        return (model.strip() or None) if isinstance(model, str) else None

    def get_agent_reasoning_effort_from_config(self, agent_name: Optional[str]) -> Optional[str]:
        """Read agent's reasoningEffort from user's opencode.json config file."""

        config = self._load_opencode_user_config()
        if not config:
            return None

        # Accept exactly the tiers the save path can write, so a variant the
        # user just persisted is never dropped here as "unknown".

        # Try agent-specific reasoningEffort first
        agent_config = self._get_agent_config(config, agent_name)
        reasoning_effort = agent_config.get("reasoningEffort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            if reasoning_effort in OPENCODE_REASONING_VARIANTS:
                logger.debug(f"Found reasoningEffort '{reasoning_effort}' for agent '{agent_name}' in opencode.json")
                return reasoning_effort
            else:
                logger.debug(f"Ignoring unknown reasoningEffort '{reasoning_effort}' for agent '{agent_name}'")

        # Fall back to global default reasoningEffort
        reasoning_effort = config.get("reasoningEffort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            if reasoning_effort in OPENCODE_REASONING_VARIANTS:
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
        # Default to the native "build" agent; Avibe supplies its model explicitly.
        return "build"


def terminate_pid_tree_sync(pid: int, timeout: float = 5.0) -> bool:
    """Stop the OpenCode server and every process it started.

    OpenCode starts each tool command detached, as the leader of a new
    session and process group, so the server's own group never reaches it.
    Only a live parent ties a process to the server, so the tree is walked
    before anything is signalled and again before escalating. Every process
    is held as a ``psutil.Process``, which refuses a pid reused since.
    """
    if os.name == "nt":
        return runtime.stop_pid(pid, timeout=timeout)
    try:
        server = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return not runtime.pid_alive(pid)

    def running(processes: list[psutil.Process]) -> list[psutil.Process]:
        alive = []
        for process in processes:
            try:
                # A zombie has exited; reaping it belongs to its parent.
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                alive.append(process)
        return alive

    own_group = os.getpgrp()
    founded_groups: set[int] = set()
    tree = [server]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        found = dict.fromkeys(tree)
        for process in tree:
            try:
                found.update(dict.fromkeys(process.children(recursive=True)))
            except psutil.Error:
                continue
        tree = running(list(found))
        if not tree:
            return True
        groups = {}
        for process in tree:
            try:
                groups[process] = os.getpgid(process.pid)
            except OSError:
                continue
        # The caller's own group is the one carrying out this stop: ``vibe
        # stop`` run from an OpenCode tool sits inside the tree. It is never
        # signalled, nor waited for, so the stop can finish its own work.
        tree = [process for process in tree if groups.get(process) != own_group]
        if not tree:
            return True
        # A group is the tree's when a process of the tree founded it. It is
        # signalled only while a live process of the tree is still in it,
        # proof that its id was not recycled, and the group signal also
        # reaches whatever those processes started since the walk.
        founded_groups.update(process.pid for process, pgid in groups.items() if process.pid == pgid)
        owned_groups = (set(groups.values()) & founded_groups) - {own_group}
        if sig == signal.SIGKILL:
            logger.warning("Escalating to SIGKILL for %d process(es) of OpenCode server pid=%s", len(tree), pid)
        elif len(tree) > 1:
            logger.info("OpenCode server pid=%s has %d descendant process(es) to stop", pid, len(tree) - 1)
        signalled_groups = set()
        for pgid in owned_groups:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            except OSError:
                # Fall back to signalling its known members one by one.
                continue
            signalled_groups.add(pgid)
        for process in tree:
            if groups.get(process) in signalled_groups:
                continue
            try:
                process.send_signal(sig)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                logger.debug("Failed to signal OpenCode process pid=%s", process.pid, exc_info=True)
        deadline = time.monotonic() + timeout
        tree = running(tree)
        while tree and time.monotonic() < deadline:
            time.sleep(0.1)
            tree = running(tree)
        if not tree:
            return True
    return False


@dataclass(frozen=True)
class OpenCodeLaunchSpec:
    """Process-level inputs of one generation; equal specs have equal digests."""

    digest: str
    binary: str
    binary_version: Optional[str] = None
    overlay_hash: Optional[str] = None
    overlay_provider_ids: tuple[str, ...] = ()
    # The Hub overlay as Model Hub wrote it, and as OpenCode receives it inline.
    overlay_file_content: Optional[bytes] = field(default=None, repr=False)
    overlay_inline_content: Optional[str] = field(default=None, repr=False)


class OpenCodeGeneration(OpenCodeServerClient):
    """One ``opencode serve`` process, a runtime generation of the OpenCode instance.

    Its record lets a controller adopt it after a crash, so a failed write
    never leaves the record protecting less than the process runs. A run
    marker or a lease is persisted before its work starts. A turn clears its
    run marker together with its durable poll: when the record cannot be
    written, both stay and a later restore retries. Any other change that only
    releases protection takes effect at once; when its write fails, the record
    stays stale until a sweep's ``flush_record`` writes it, and a crash before
    then only keeps the process until the stale lease expires or adoption drops
    a marker no durable poll backs.
    """

    def __init__(
        self,
        *,
        generation_id: str,
        pid: int,
        port: int,
        spec_digest: str,
        process_created_at: float | None,
        host: str = DEFAULT_OPENCODE_HOST,
        binary: str = "",
        binary_version: Optional[str] = None,
        caller_context_path: Optional[str] = None,
        model_hub_overlay_hash: Optional[str] = None,
        model_hub_provider_ids: tuple[str, ...] = (),
        active_run_sessions: Iterable[str] = (),
        leases: Optional[Mapping[str, float]] = None,
        started_at: float | None = None,
        desktop_runtime_id: Any = None,
        process: Optional[Process] = None,
        request_timeout_seconds: int = 60,
    ) -> None:
        super().__init__(
            f"http://{host}:{port}",
            request_timeout_seconds=request_timeout_seconds,
            model_hub_provider_ids=model_hub_provider_ids,
        )
        self.generation_id = generation_id
        self.pid = pid
        self.port = port
        self.host = host
        self.spec_digest = spec_digest
        self.process_created_at = process_created_at
        self.binary = binary
        self.binary_version = binary_version
        self.caller_context_path = caller_context_path
        self.model_hub_overlay_hash = model_hub_overlay_hash
        self.active_run_sessions: set[str] = set(active_run_sessions)
        # Expiry of each lease held by a caller in another process.
        self.leases: dict[str, float] = dict(leases or {})
        self.started_at = time.time() if started_at is None else started_at
        # The desktop Runtime this process serves, or None for none; a record
        # written before Runtime ids were recorded keeps omitting it.
        self.desktop_runtime_id = desktop_runtime_id
        # Set when a change took effect but its record write failed.
        self.record_stale = False
        # Set once the process stopped and its record was removed.
        self._record_removed = False
        # A legacy record this record replaces once it is written.
        self._supersedes: Optional[Path] = None
        # The runtime activation identity the agent attached to this process.
        self.identity: Any = None
        # Sessions whose work a forced stop of this process already settled.
        self.interrupted_sessions: set[str] = set()
        self._process = process

    @property
    def record_path(self) -> Path:
        return generation_records_dir() / f"{self.generation_id}.json"

    @property
    def overlay_path(self) -> Path:
        return generation_records_dir() / f"{self.generation_id}.overlay.json"

    def caller_context_binding_path(self) -> Path:
        if self.caller_context_path and os.path.isabs(self.caller_context_path):
            return Path(self.caller_context_path)
        return Path(server_environment()["AVIBE_OPENCODE_CALLER_CONTEXT_PATH"])

    def record(self) -> Dict[str, Any]:
        return {
            "schema": GENERATION_RECORD_SCHEMA,
            "generation_id": self.generation_id,
            "pid": self.pid,
            "process_created_at": self.process_created_at,
            "host": self.host,
            "port": self.port,
            "started_at": self.started_at,
            "spec_digest": self.spec_digest,
            "binary": {"path": self.binary, "version": self.binary_version},
            "caller_context_path": self.caller_context_path,
            "model_hub_overlay_hash": self.model_hub_overlay_hash,
            "model_hub_overlay_provider_ids": list(self.model_hub_provider_ids),
            "active_run_sessions": sorted(self.active_run_sessions),
            "leases": dict(self.leases),
            **(
                {}
                if self.desktop_runtime_id is _UNRECORDED_RUNTIME_ID
                else {"desktop_runtime_id": self.desktop_runtime_id}
            ),
        }

    def write_record(self) -> None:
        """Persist this record now, raising when it cannot be written."""
        if self._record_removed:
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.record_path, json.dumps(self.record()))
        self.record_stale = False
        if self._supersedes is not None:
            _remove_quietly(self._supersedes)
            # A removal that failed is retried by the next flush.
            if not self._supersedes.exists():
                self._supersedes = None

    def write_record_or_defer(self, change: str) -> None:
        """Persist a change that has already taken effect, or leave it to a sweep."""
        try:
            self.write_record()
        except Exception:
            log = logger.debug if self.record_stale else logger.warning
            self.record_stale = True
            log(
                "Could not persist %s for OpenCode generation %s; a sweep retries",
                change,
                self.generation_id,
                exc_info=True,
            )

    def flush_record(self) -> None:
        if self.record_stale or self._supersedes is not None:
            self.write_record_or_defer("an earlier change")

    def _change_run_marker(self, session_id: str, *, active: bool) -> None:
        previous = set(self.active_run_sessions)
        if active:
            self.active_run_sessions.add(session_id)
        else:
            self.active_run_sessions.discard(session_id)
        if self.active_run_sessions == previous:
            return
        try:
            self.write_record()
        except Exception:
            self.active_run_sessions = previous
            raise

    async def mark_run_active(self, session_id: str) -> None:
        """Record a native run on this process before its first native write."""
        async with self._get_lock():
            self._change_run_marker(session_id, active=True)

    async def mark_run_inactive(self, session_id: str) -> None:
        async with self._get_lock():
            self._change_run_marker(session_id, active=False)

    def set_lease(self, lease_id: str, expires_at: float) -> None:
        previous = dict(self.leases)
        self.leases[lease_id] = expires_at
        try:
            self.write_record()
        except Exception:
            self.leases = previous
            raise

    def drop_lease(self, lease_id: str) -> None:
        if self.leases.pop(lease_id, None) is not None:
            self.write_record_or_defer(f"the release of lease {lease_id}")

    def is_drained(self) -> bool:
        """Whether no request or native run of this process is in flight."""
        return not self.has_requests_in_flight() and not self.active_run_sessions

    def has_requests_in_flight(self) -> bool:
        return self._active_requests > 0

    def process_alive(self) -> bool:
        process = self._process
        if process is not None and process.returncode is not None:
            return False
        created_at = runtime.process_create_time(self.pid)
        if self.process_created_at is None:
            return created_at is not None or _pid_exists(self.pid)
        return created_at == self.process_created_at

    def observed_exit_pid(self) -> int | None:
        """This process's pid once it has exited, never a pid reused since."""
        return None if self.process_alive() else self.pid

    async def is_healthy(self) -> bool:
        try:
            session = await self._get_http_session()
            async with session.get(f"{self.base_url}/global/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return bool(data.get("healthy", False))
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
        return False


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove %s", path, exc_info=True)


def apply_resource_governance(resource_governor: Any | None, pid: int | None) -> None:
    apply_to_pid = getattr(resource_governor, "apply_to_pid", None)
    if callable(apply_to_pid):
        apply_to_pid(pid, label="opencode serve")


def _launch_environment(spec: OpenCodeLaunchSpec, overlay_path: Optional[Path]) -> dict[str, str]:
    env = os.environ.copy()
    env[DESKTOP_ROLE_ENV] = DESKTOP_OPENCODE_ROLE
    env["OPENCODE_ENABLE_EXA"] = "1"
    env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
    env.update(server_environment())
    hub = spec.overlay_inline_content is not None
    env["AVIBE_OPENCODE_MODEL_HUB"] = "1" if hub else "0"
    if hub:
        env["OPENCODE_CONFIG"] = str(overlay_path)
        # Inline config is OpenCode's runtime-override tier, loaded after
        # project config. Reasserting the exact overlay here prevents a
        # checked-in opencode.json from replacing Hub provider transport.
        env["OPENCODE_CONFIG_CONTENT"] = spec.overlay_inline_content
    # Request-level ``tools.skill=false`` prevents native Skill calls. The
    # runtime override adds defense in depth, while the Avibe runtime plugin
    # removes OpenCode's independently assembled native Catalog.
    env["OPENCODE_CONFIG_CONTENT"] = _managed_runtime_config_content(env.get("OPENCODE_CONFIG_CONTENT"))
    return env


async def _wait_until_ready(generation: OpenCodeGeneration, process: Process) -> str:
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.returncode is not None:
            return "exited"
        # A healthy answer alone may come from another process that won the
        # port; only this process listening on it proves the generation.
        if await generation.is_healthy() and _pid_listens_on(process.pid, generation.port):
            return "ready"
        await asyncio.sleep(0.25)
    return "exited" if process.returncode is not None else "timeout"


async def _terminate_started_process(process: Process, reason: str) -> bool:
    """Stop a process this start spawned; return whether it has exited."""

    logger.info("Stopping OpenCode server pid=%s (%s)", process.pid, reason)
    await asyncio.to_thread(terminate_pid_tree_sync, process.pid)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("OpenCode server pid=%s did not exit after it was stopped", process.pid)
        return False
    return True


async def start_generation(
    spec: OpenCodeLaunchSpec,
    *,
    request_timeout_seconds: int = 60,
    resource_governor: Any | None = None,
    on_survivor: Optional[Callable[[OpenCodeGeneration], None]] = None,
) -> OpenCodeGeneration:
    """Start one generation on a port of its own and wait until it serves.

    A start that fails after spawning stops its process. When the process
    survives that stop, ``on_survivor`` receives it, still owned here, so its
    runtime keeps retrying the stop; its record and overlay stay meanwhile.
    The process is owned here from its spawn, so even one whose record was
    never written is stopped by this controller's shutdown.
    """

    generation_id = f"ocg_{secrets.token_hex(8)}"
    ensure_plugin_installed()
    caller_context_path = server_environment()["AVIBE_OPENCODE_CALLER_CONTEXT_PATH"]
    overlay_path: Optional[Path] = None
    if spec.overlay_inline_content is not None:
        # Model Hub rewrites its own overlay file for every new overlay while
        # older generations still run, so each generation keeps its own copy.
        overlay_path = generation_records_dir() / f"{generation_id}.overlay.json"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(overlay_path, spec.overlay_file_content or spec.overlay_inline_content.encode())
    env = _launch_environment(spec, overlay_path)
    exited_pid: int | None = None
    exit_code: int | None = None
    # A process that survives its stop keeps its record and overlay; its
    # runtime retries the stop, and ``vibe stop`` can still find it.
    survivor: Optional[OpenCodeGeneration] = None
    try:
        for _attempt in range(_PORT_ATTEMPTS):
            port = _choose_port(DEFAULT_OPENCODE_HOST)
            cmd = [spec.binary, "serve", f"--hostname={DEFAULT_OPENCODE_HOST}", f"--port={port}"]
            logger.info("Starting OpenCode generation %s: %s", generation_id, " ".join(cmd))
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    **isolated_subprocess_kwargs(),
                )
            except FileNotFoundError as exc:
                raise OpenCodeGenerationStartError(
                    f"OpenCode CLI not found at '{spec.binary}'. Please install OpenCode or set OPENCODE_CLI_PATH."
                ) from exc
            # Owned before anything else can fail, the record write included.
            created_at = runtime.process_create_time(process.pid)
            _OWNED_HERE[generation_id] = (process.pid, created_at)
            generation = OpenCodeGeneration(
                generation_id=generation_id,
                pid=process.pid,
                port=port,
                spec_digest=spec.digest,
                process_created_at=created_at,
                binary=spec.binary,
                binary_version=spec.binary_version,
                caller_context_path=caller_context_path,
                model_hub_overlay_hash=spec.overlay_hash,
                model_hub_provider_ids=spec.overlay_provider_ids,
                desktop_runtime_id=_this_desktop_runtime_id(),
                process=process,
                request_timeout_seconds=request_timeout_seconds,
            )
            try:
                # Written before readiness, so a controller that dies during
                # the start leaves a record its successor cleans up.
                generation.write_record()
                apply_resource_governance(resource_governor, process.pid)
                outcome = await _wait_until_ready(generation, process)
            except BaseException:
                await generation.close_http_session()
                if await _terminate_started_process(process, "start interrupted"):
                    _disown_ended_start(generation)
                else:
                    survivor = generation
                raise
            if outcome == "ready":
                logger.info("OpenCode generation %s serves at %s", generation_id, generation.base_url)
                return generation
            await generation.close_http_session()
            if outcome == "exited":
                # Most often another process took the port first.
                _disown_ended_start(generation)
                exited_pid, exit_code = process.pid, process.returncode
                continue
            # A late-starting process must not become a healthy server that
            # nothing records.
            if await _terminate_started_process(process, "startup timeout"):
                _disown_ended_start(generation)
            else:
                survivor = generation
            raise OpenCodeGenerationStartError(
                f"OpenCode server failed to start within {SERVER_START_TIMEOUT}s."
            )
        raise OpenCodeGenerationStartError(
            f"OpenCode server exited during startup. Process exit code: {exit_code}",
            exited_pid=exited_pid,
        )
    except BaseException:
        if survivor is not None and on_survivor is not None:
            # Still owned here: the runtime that started it retries its stop.
            on_survivor(survivor)
        if overlay_path is not None and survivor is None:
            _remove_quietly(overlay_path)
        raise


def _disown_ended_start(generation: OpenCodeGeneration) -> None:
    """A start's process exited: it is no longer owned here, and its record goes."""

    _OWNED_HERE.pop(generation.generation_id, None)
    _remove_quietly(generation.record_path)


async def stop_generation(generation: OpenCodeGeneration) -> None:
    """Stop one generation's process tree, then remove its record.

    The record stays when the process survives, so adoption or ``vibe stop``
    can try again.
    """

    await generation.close_http_session()
    process = generation._process
    if generation.process_alive():
        logger.info("Stopping OpenCode generation %s pid=%s", generation.generation_id, generation.pid)
        stopped = await asyncio.to_thread(terminate_pid_tree_sync, generation.pid)
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        if not stopped and generation.process_alive():
            raise RuntimeError(
                f"OpenCode generation {generation.generation_id} pid={generation.pid} did not exit"
            )
    generation._record_removed = True
    _OWNED_HERE.pop(generation.generation_id, None)
    _remove_quietly(generation.record_path)
    _remove_quietly(generation.overlay_path)
    if generation._supersedes is not None:
        _remove_quietly(generation._supersedes)


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """A record's content; ``None`` when it is missing or cannot be parsed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        data = None
    if not isinstance(data, dict):
        # Its process may still run on a port nothing else records, so the
        # record stays for a person to inspect rather than counting as dead.
        logger.warning("Keeping an unreadable OpenCode process record at %s", path)
        return None
    return data


# A record is read back from disk, where any field can be malformed. Readers
# take each field only through these, so a corrupt record degrades to one that
# lacks the field and never breaks the readers of every other record.


def _record_pid(info: Mapping[str, Any]) -> Optional[int]:
    pid = info.get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**32 else None


def _record_port(info: Mapping[str, Any]) -> Optional[int]:
    port = info.get("port")
    return port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None


def _record_number(value: object) -> Optional[float]:
    """A finite number a record holds, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _record_runtime_id(info: Mapping[str, Any]) -> Any:
    """The desktop Runtime a record names: an id, None, or unrecorded.

    A record from before the field existed, or one whose value is no id at
    all, counts as unrecorded and is judged by its live process.
    """
    value = info.get("desktop_runtime_id", _UNRECORDED_RUNTIME_ID)
    return value if value is None or isinstance(value, str) else _UNRECORDED_RUNTIME_ID


def _record_proves_process(info: Mapping[str, Any], *, require_port: bool = True) -> bool:
    """Whether a record still names the exact ``opencode serve`` it started.

    Stopping a server needs no port, so it accepts the bare legacy shape that
    ``vibe stop`` always accepted; adopting one does.
    """

    pid = _record_pid(info)
    port = _record_port(info)
    if pid is None or not _pid_exists(pid):
        return False
    has_port = port is not None
    if require_port and not has_port:
        return False
    created_at = _record_number(info.get("process_created_at"))
    if created_at is not None:
        # A command line alone can belong to a reused pid.
        if runtime.process_create_time(pid) != created_at:
            return False
    command = _get_pid_command(pid)
    if command:
        if has_port:
            return _is_opencode_serve_cmd(command, port)
        return "opencode" in command and "serve" in command
    return has_port and _pid_listens_on(pid, port)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _generation_from_record(
    info: Mapping[str, Any],
    *,
    generation_id: str,
    spec_digest: str,
    request_timeout_seconds: int,
) -> OpenCodeGeneration:
    binary = info.get("binary") if isinstance(info.get("binary"), dict) else {}
    pid = int(info["pid"])
    created_at = _record_number(info.get("process_created_at"))
    if created_at is None:
        created_at = runtime.process_create_time(pid)
    started_at = _record_number(info.get("started_at"))
    leases = info.get("leases") if isinstance(info.get("leases"), dict) else {}
    caller_context_path = info.get("caller_context_path")
    overlay_hash = info.get("model_hub_overlay_hash")
    host = info.get("host")
    binary_path = binary.get("path")
    return OpenCodeGeneration(
        generation_id=generation_id,
        pid=pid,
        port=int(info["port"]),
        host=host if isinstance(host, str) and host else DEFAULT_OPENCODE_HOST,
        spec_digest=spec_digest,
        process_created_at=created_at,
        binary=binary_path if isinstance(binary_path, str) else "",
        binary_version=binary.get("version") if isinstance(binary.get("version"), str) else None,
        caller_context_path=caller_context_path if isinstance(caller_context_path, str) else None,
        model_hub_overlay_hash=overlay_hash if isinstance(overlay_hash, str) else None,
        model_hub_provider_ids=_string_tuple(info.get("model_hub_overlay_provider_ids")),
        active_run_sessions=_string_tuple(info.get("active_run_sessions")),
        leases={
            str(key): expires_at
            for key, value in leases.items()
            if (expires_at := _record_number(value)) is not None
        },
        # The process's age: a record without a start time, such as the
        # legacy one, started when its process did, never at its adoption.
        started_at=started_at if started_at is not None else created_at,
        # Rewritten without a malformed id, so every later read judges it alike.
        desktop_runtime_id=_record_runtime_id(info),
        request_timeout_seconds=request_timeout_seconds,
    )


def _this_desktop_runtime_id() -> Optional[str]:
    """The one desktop Runtime this process acts for, or None."""

    runtime_ids = desktop_caller_provenance()
    return next(iter(runtime_ids)) if len(runtime_ids) == 1 else None


def _record_is_ours(info: Mapping[str, Any], runtime_ids: frozenset[str]) -> bool:
    """Whether a caller acting for ``runtime_ids`` may act on a record at all.

    It is the one ownership gate for every path that reads or acts on records.
    A caller with no desktop provenance acts on any record, as ``vibe stop``
    always has. A record names the desktop Runtime whose controller wrote it;
    a record from before Runtime ids were recorded, or whose id is malformed,
    is judged by its live process, exactly as ``refuse_foreign_desktop_process``
    judges one, and a record whose process is gone belongs to nobody.
    """

    if not runtime_ids:
        return True
    recorded = _record_runtime_id(info)
    if recorded is not _UNRECORDED_RUNTIME_ID:
        return runtime_ids == {recorded}
    pid = _record_pid(info)
    if pid is None:
        return True
    try:
        runtime.refuse_foreign_desktop_process(pid, "opencode", runtime_ids)
    except runtime.DesktopRuntimeClaimRefused:
        return False
    return True


def _recorded_processes(runtime_ids: Optional[frozenset[str]] = None) -> list[tuple[Path, Dict[str, Any]]]:
    """This caller's generation records, then the legacy single-server record.

    Records of another desktop Runtime sharing this state directory are left
    out, so nothing here adopts, stops, forgets, or cleans them.
    ``runtime_ids`` defaults to this process's desktop provenance.
    """

    ids = desktop_caller_provenance() if runtime_ids is None else runtime_ids
    found = _every_recorded_process()
    ours = [(path, info) for path, info in found if _record_is_ours(info, ids)]
    if len(ours) < len(found):
        logger.debug("Leaving %s OpenCode record(s) of another desktop Runtime alone", len(found) - len(ours))
    return ours


def other_runtimes_live_records() -> list[Dict[str, Any]]:
    """Records of another desktop Runtime whose process is proven running.

    Durable state naming such a process, such as the poll of a run it
    executes, is that Runtime's: nothing here resumes, rewrites, or settles it,
    and its controller resumes it when it runs here again. Once the process
    is gone, the state names nothing to protect and any controller here
    settles it, as after a restart.
    """

    runtime_ids = desktop_caller_provenance()
    return [
        info
        for _path, info in _every_recorded_process()
        if not _record_is_ours(info, runtime_ids) and _record_proves_process(info, require_port=False)
    ]


def _every_recorded_process() -> list[tuple[Path, Dict[str, Any]]]:
    found: list[tuple[Path, Dict[str, Any]]] = []
    records_dir = generation_records_dir()
    if records_dir.is_dir():
        for path in sorted(records_dir.glob("*.json")):
            if path.name.endswith(".overlay.json"):
                continue
            info = _read_json_object(path)
            if info is not None:
                # A generation's record is named for it, so its file name is
                # the id every reader uses, whatever the content claims.
                info["generation_id"] = path.stem
                found.append((path, info))
    legacy = _read_json_object(legacy_pid_file())
    if legacy is not None:
        found.append((legacy_pid_file(), legacy))
    return found


async def _serves_after_restart(generation: OpenCodeGeneration) -> bool:
    for attempt in range(_ADOPTION_PROBES):
        if await generation.is_healthy() and _pid_listens_on(generation.pid, generation.port):
            return True
        if attempt + 1 < _ADOPTION_PROBES:
            await asyncio.sleep(_ADOPTION_PROBE_INTERVAL_SECONDS)
    return False


async def adopt_recorded_generations(*, request_timeout_seconds: int = 60) -> list[OpenCodeGeneration]:
    """Adopt every recorded generation still serving after a controller restart.

    A record whose process is gone or unproven is removed without signalling.
    A proven process that no longer serves is stopped. A legacy single-server
    record becomes a generation whose spec never matches a new turn's.
    """

    adopted: list[OpenCodeGeneration] = []
    adopted_processes: set[tuple[int, Optional[float]]] = set()
    legacy_path = legacy_pid_file()
    for path, info in _recorded_processes():
        is_legacy = path == legacy_path
        if _owned_here(info, by_id=not is_legacy):
            # A legacy file a runtime here already converted goes once the
            # converted record is written.
            continue
        if not _record_proves_process(info):
            _remove_quietly(path)
            if not is_legacy and isinstance(info.get("generation_id"), str):
                _remove_quietly(generation_records_dir() / f"{info['generation_id']}.overlay.json")
            continue
        if is_legacy and (int(info["pid"]), runtime.process_create_time(int(info["pid"]))) in adopted_processes:
            # This pass adopted it from its converted record; a crash or a
            # failed removal after the conversion left this file behind.
            _remove_quietly(path)
            continue
        recorded_id = info.get("generation_id")
        generation = _generation_from_record(
            info,
            generation_id=(
                recorded_id
                if isinstance(recorded_id, str) and recorded_id and not is_legacy
                else f"ocg_{secrets.token_hex(8)}"
            ),
            spec_digest=(
                LEGACY_SPEC_DIGEST
                if is_legacy or not isinstance(info.get("spec_digest"), str)
                else str(info["spec_digest"])
            ),
            request_timeout_seconds=request_timeout_seconds,
        )
        if not await _serves_after_restart(generation):
            logger.info("Stopping recorded OpenCode server pid=%s that no longer serves", generation.pid)
            await generation.close_http_session()
            await asyncio.to_thread(stop_recorded_server_sync, path, info)
            continue
        if is_legacy:
            # The legacy record goes once the generation record replacing it exists.
            generation._supersedes = path
            generation.write_record_or_defer("the record of an adopted pre-generations server")
        logger.info("Adopted OpenCode generation %s pid=%s", generation.generation_id, generation.pid)
        adopted.append(generation)
        adopted_processes.add((generation.pid, generation.process_created_at))
    # No sweep of overlays without a record: another desktop Runtime writes
    # its overlay before it spawns and records the process.
    return adopted


def _owned_here(info: Mapping[str, Any], *, by_id: bool = True) -> bool:
    """Whether a runtime of this process started or attached the recorded process."""

    if not _OWNED_HERE:
        # A CLI process, or a controller whose OpenCode never ran, owns nothing.
        return False
    generation_id = info.get("generation_id")
    if by_id and isinstance(generation_id, str) and generation_id in _OWNED_HERE:
        return True
    pid = _record_pid(info)
    if pid is None:
        return False
    return (pid, runtime.process_create_time(pid)) in _OWNED_HERE.values()


def own_generation(generation: OpenCodeGeneration) -> None:
    """Mark an adopted generation as attached to a runtime of this process."""
    _OWNED_HERE[generation.generation_id] = (generation.pid, generation.process_created_at)


def forget_record(path: Path) -> None:
    """Remove a generation record and the overlay copy beside it.

    The overlay carries the Model Hub gateway credential, so it never outlives
    its record.
    """

    _remove_quietly(path)
    if path.name.endswith(".json") and path.parent == generation_records_dir():
        _remove_quietly(path.with_name(path.name.removesuffix(".json") + ".overlay.json"))


def forget_dead_records(runtime_ids: Optional[frozenset[str]] = None) -> None:
    """Forget every record of this caller no live process backs, with its overlay."""

    for path, info in _recorded_processes(runtime_ids):
        if not _record_proves_process(info, require_port=False):
            forget_record(path)


def recorded_servers(runtime_ids: Optional[frozenset[str]] = None) -> list[tuple[int, Path, Dict[str, Any]]]:
    """This caller's live, proven OpenCode servers, for status and ``vibe stop``."""

    return [
        (int(info["pid"]), path, info)
        for path, info in _recorded_processes(runtime_ids)
        if _record_proves_process(info, require_port=False)
    ]


def stop_recorded_server_sync(path: Path, info: Mapping[str, Any]) -> StopOutcome:
    """Stop one recorded server outside any runtime, confirming the outcome.

    A record no live process of ours backs is simply forgotten. A process
    that survives its stop keeps its record, so ``vibe stop`` or the next
    adoption retries it.
    """

    if _record_proves_process(info, require_port=False) and not terminate_pid_tree_sync(int(info["pid"])):
        logger.warning("Recorded OpenCode server pid=%s survived its stop", info["pid"])
        return StopOutcome.FAILED
    forget_record(path)
    return StopOutcome.STOPPED


def stop_recorded_servers_sync(runtime_ids: frozenset[str] = frozenset()) -> list[StopOutcome]:
    """Stop every recorded server of ``runtime_ids`` no runtime of this process owns.

    ``vibe stop`` runs it, as does a controller that starts with OpenCode
    disabled: no agent there would adopt what a crashed controller left. A
    record whose process already ended is forgotten with its overlay. A record
    of another desktop Runtime is left alone. OpenCode starts each tool
    command in its own session, so each stop takes the whole process tree.

    The result holds one outcome per stop it ran; ``StopOutcome.FAILED`` means
    a process survived, its record stays, and the caller should retry.
    """

    forget_dead_records(runtime_ids)
    outcomes: list[StopOutcome] = []
    for _pid, path, info in recorded_servers(runtime_ids):
        if _owned_here(info):
            continue
        outcomes.append(stop_recorded_server_sync(path, info))
    return outcomes


def stop_owned_generations_sync() -> None:
    """Stop every process a runtime of this controller started or adopted.

    An explicit Avibe shutdown runs it. A recorded process stops through its
    record. One owned here whose record is missing, such as a start whose
    record write failed, stops by its process identity. A record no runtime
    here owns, such as one of another desktop Runtime sharing this state
    directory, is left alone.
    """

    recorded: set[str] = set()
    for path, info in _recorded_processes():
        if _owned_here(info):
            stop_recorded_server_sync(path, info)
            if isinstance(info.get("generation_id"), str):
                recorded.add(info["generation_id"])
    for generation_id, (pid, created_at) in list(_OWNED_HERE.items()):
        if generation_id not in recorded:
            _stop_unrecorded_process_sync(generation_id, pid, created_at)


def _stop_unrecorded_process_sync(generation_id: str, pid: int, created_at: Optional[float]) -> None:
    """Stop an owned process no record names, proven by its pid and create time.

    A process whose create time is unknown or cannot be read now is never
    signalled: its pid may have been reused. One whose pid is gone, or now
    names another process, has ended, and is no longer owned here.
    """

    if _pid_exists(pid):
        current = runtime.process_create_time(pid)
        if created_at is None or current is None:
            logger.warning("Leaving OpenCode generation %s pid=%s: its process cannot be proven", generation_id, pid)
            return
        if current == created_at and not terminate_pid_tree_sync(pid):
            logger.warning("OpenCode generation %s pid=%s survived its stop", generation_id, pid)
            return
    _OWNED_HERE.pop(generation_id, None)
    _remove_quietly(generation_records_dir() / f"{generation_id}.overlay.json")


