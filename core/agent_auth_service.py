"""Agent OAuth setup orchestration for remote IM-driven login recovery."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from modules.claude_sdk_compat import (
    CLAUDE_SDK_AVAILABLE,
    CLAUDE_SDK_MAX_BUFFER_SIZE,
    ClaudeAgentOptions,
    ClaudeSDKClient,
)
from modules.agents.claude_process_reaper import (
    AVIBE_CLAUDE_AUTH_OWNER,
    AVIBE_CLAUDE_PROCESS_OWNER_ENV,
    get_claude_client_pid,
    register_claude_owned_process,
)
from modules.agents.catalog import WEB_OAUTH_BACKENDS
from modules.agents.opencode.message_processor import (
    extract_opencode_response_text,
    is_empty_terminal_opencode_message,
)
from modules.agents.opencode.utils import (
    resolve_opencode_configured_default_model,
    resolve_opencode_model_id,
    resolve_opencode_reasoning_effort,
)
from modules.im import InlineButton, InlineKeyboard, MessageContext
from core.resource_governance import governor_from_controller
from core.message_output import MessageOutput, terminal_turn_output
from vibe.i18n import t as i18n_t
from vibe.opencode_config import remove_opencode_provider_api_key

logger = logging.getLogger(__name__)

ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
CODEX_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device")
URL_RE = re.compile(r"https?://\S+")
CODEX_DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4,})+\b")
OPENCODE_API_KEY_PROMPT_RE = re.compile(r"enteryourapikey", re.IGNORECASE)
OPENCODE_CREDENTIAL_COUNT_RE = re.compile(r"\b(\d+)\s+credential(?:s)?\b", re.IGNORECASE)
CLAUDE_LOGIN_METHODS = {"claudeai", "console"}
OPENCODE_DIRECT_SETUP_URLS = {"opencode": "https://opencode.ai/auth"}


def _pick_probe_response_excerpt(stdout_text: str) -> str:
    """Return the first content-bearing line from Codex/Claude probe stdout.

    The CLIs emit decorations the user doesn't care about: warnings
    (``warning:``, ``ERROR:``, bubblewrap notices), session
    bookkeeping (``codex``, ``tokens used``, ``Reconnecting...``),
    and timestamped headers. Skip those and take the first real
    response line so the "Last run" status shows what the model
    actually said.
    """
    skip_prefixes = (
        "warning:",
        "error:",
        "info:",
        "debug:",
        "trace:",
        "codex",
        "claude",
        "reconnecting",
        "tokens used",
        "thinking",
        "session id",
        "user instructions",
        "system",
        "[",  # rich-tty escape sequence remnants like "[1m..."
    )
    candidate = ""
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(p) for p in skip_prefixes):
            continue
        if stripped.startswith(">") or stripped.startswith("#"):
            continue
        candidate = stripped[:240]
        break
    if not candidate:
        # Fall back to the first non-blank line even if it looks
        # decorative — better something than empty.
        for line in stdout_text.splitlines():
            stripped = line.strip()
            if stripped:
                candidate = stripped[:240]
                break
    return candidate


def _classify_test_failure(stdout: str, stderr: str) -> str:
    """Map a backend CLI's failure output to a specific UI error code.

    The Settings → Backends "Test connection" panel routes the returned
    ``error`` string into an i18n key (``settings.backends.testFailure*``),
    so the user gets a sentence they can act on instead of the opaque
    ``cli_failed``. Order matters: more specific patterns (HTTP status
    codes, named error strings) are checked before generic substrings.

    The classifier is conservative — anything it doesn't recognise stays
    as ``cli_failed`` and the raw stderr is preserved in ``detail`` for
    inspection. Adding a new class is a one-line append below.
    """
    text = f"{stderr}\n{stdout}".lower()
    if not text.strip():
        return "cli_failed"

    if "not logged in" in text:
        return "not_logged_in"

    # Auth-side rejections (key wrong / revoked / rejected).
    auth_needles = (
        "401",
        "unauthorized",
        "invalid api key",
        "invalid_api_key",
        "api key not valid",
        "authentication",
        "auth failed",
        "missing api key",
        "no api key",
    )
    if any(needle in text for needle in auth_needles):
        return "invalid_credentials"

    # Quota / rate limiting.
    if any(needle in text for needle in ("429", "rate limit", "quota", "usage limit", "too many requests")):
        return "rate_limited"

    # Authorization (key valid but not allowed).
    if any(needle in text for needle in ("403", "forbidden", "permission denied", "access denied")):
        return "forbidden"

    # Model / endpoint mismatches.
    if any(
        needle in text
        for needle in (
            "model not found",
            "model_not_found",
            "unknown model",
            "model does not exist",
            "no such model",
        )
    ):
        return "model_not_found"

    # Network-side failures: the relay / endpoint isn't reachable at all.
    if any(
        needle in text
        for needle in (
            "connection refused",
            "could not resolve",
            "getaddrinfo",
            "name or service not known",
            "name resolution failed",
            "eai_again",
            "enotfound",
            "econnrefused",
            "network is unreachable",
            "no route to host",
            "ssl",
            "certificate",
            "timed out",
            "request timeout",
            "deadline exceeded",
        )
    ):
        return "endpoint_unreachable"

    # 5xx from the upstream.
    if any(needle in text for needle in ("502", "503", "504", "bad gateway", "service unavailable", "internal server error")):
        return "server_error"

    # Codex's own trust-gate / sandbox blockers (shouldn't fire now that
    # we pass --skip-git-repo-check, but kept for observability if a
    # future Codex release changes the flag).
    if "trusted directory" in text or "--skip-git-repo-check" in text:
        return "trust_check_failed"

    return "cli_failed"


def classify_auth_error(backend: str, error_text: str) -> bool:
    """Return True when the error likely requires an OAuth reset."""
    text = (error_text or "").strip().lower()
    if not text:
        return False

    if backend == "codex":
        needles = (
            "401",
            "unauthorized",
            "not logged in",
            "login required",
            "authentication",
            "oauth",
            "token data is not available",
        )
        return any(needle in text for needle in needles)

    if backend == "claude":
        needles = (
            "401",
            "unauthorized",
            "oauth",
            "re-auth",
            "re-authenticate",
            "auth login",
            "login",
            "logged out",
        )
        return any(needle in text for needle in needles)

    if backend == "opencode":
        needles = (
            "401",
            "unauthorized",
            "authentication",
            "credential",
            "api key",
            "provider",
            "failed to send message: 401",
            "failed to start async prompt: 401",
        )
        return any(needle in text for needle in needles)

    return False


def sanitize_process_output(text: str) -> str:
    """Strip ANSI/control sequences so parsing works across TTY and non-TTY flows."""
    cleaned = ANSI_ESCAPE_RE.sub("", text)
    cleaned = CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned.strip()


def verify_opencode_auth_list_output(text: str, provider: str | None = None) -> bool:
    """Return True when `opencode auth list` shows credentials for the target provider."""
    normalized_lines = []
    for line in sanitize_process_output(text).splitlines():
        stripped = re.sub(r"^[^\w]+", "", line).strip()
        if stripped:
            normalized_lines.append(stripped.lower())

    if provider:
        provider_pattern = re.compile(rf"\b{re.escape(provider.lower())}\b")
        negative_markers = (
            "0 credential",
            "no credential",
            "not configured",
            "logged out",
            "unauthenticated",
            "missing",
        )
        for line in normalized_lines:
            if line.startswith("credentials "):
                continue
            if not provider_pattern.search(line):
                continue
            count_match = OPENCODE_CREDENTIAL_COUNT_RE.search(line)
            if count_match:
                return int(count_match.group(1)) > 0
            return not any(marker in line for marker in negative_markers)
        return False

    normalized = "\n".join(line for line in normalized_lines if not line.startswith("credentials "))
    count_matches = [int(match.group(1)) for match in OPENCODE_CREDENTIAL_COUNT_RE.finditer(normalized)]
    if count_matches:
        return any(count > 0 for count in count_matches)
    return "credential" in normalized and "0 credentials" not in normalized and "no credentials" not in normalized


@dataclass
class AgentAuthFlow:
    flow_id: str
    backend: str
    settings_key: str
    initiator_user_id: str
    context: MessageContext
    process: asyncio.subprocess.Process | None
    reader_task: asyncio.Task[None]
    waiter_task: asyncio.Task[None]
    claude_client: ClaudeSDKClient | None = None
    pty_master_fd: int | None = None
    awaiting_code: bool = False
    login_prompt_sent: bool = False
    code_prompt_sent: bool = False
    url: str | None = None
    device_code: str | None = None
    provider: str | None = None
    last_status_text: str | None = None
    force_oauth: bool = False
    claude_oauth_attempt: "ClaudeOAuthAttempt | None" = None

    @property
    def flow_key(self) -> str:
        return f"{self.settings_key}:{self.backend}"


@dataclass
class ClaudeOAuthBatch:
    backup: dict[str, str] | None
    attempts: dict[int, "ClaudeOAuthAttempt"] = field(default_factory=dict)
    committed: bool = False
    durable_backup: bool = False


@dataclass
class ClaudeOAuthAttempt:
    attempt_id: int
    batch: ClaudeOAuthBatch
    active: bool = True
    succeeded: bool = False

    @property
    def settings_backup(self) -> dict[str, str] | None:
        return self.batch.backup


# Web Settings → Backends OAuth flows live alongside the IM ``AgentAuthFlow``
# but share none of its IM coupling: no MessageContext, no ``_send_message``,
# no settings_key. State is exposed to the browser via a polling endpoint, so
# every transition the UI needs to render lives in plain fields here.
WebFlowState = str  # "starting" | "awaiting_code" | "verifying" | "success" | "failed" | "cancelled"


@dataclass
class WebAuthFlow:
    flow_id: str
    backend: str  # "claude" | "codex" | "opencode"
    state: WebFlowState = "starting"
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    claude_client: ClaudeSDKClient | None = None
    url: str | None = None
    device_code: str | None = None
    awaiting_code: bool = False
    error: str | None = None
    last_status_text: str | None = None
    # Per-provider context for OpenCode flows; ``None`` for Claude / Codex
    # which authenticate at the backend level. Declared on the dataclass
    # so ``api.py::start_oauth_web`` can serialise it unconditionally
    # — accessing a runtime-assigned attribute on Claude/Codex flows
    # would AttributeError out and 500 the start endpoint.
    provider: str | None = None
    claude_oauth_attempt: ClaudeOAuthAttempt | None = None
    created_at: float = field(default_factory=time.time)
    # Timestamp the flow first entered a terminal state (success /
    # failed / cancelled). ``None`` while the flow is still in
    # ``starting`` / ``awaiting_code`` / ``verifying``. Used by
    # ``get_web_flow_status`` to evict stale terminal flows after a
    # TTL so the dict doesn't grow unboundedly across sign-in
    # attempts on a long-lived UI server.
    terminal_at: float | None = None


class AgentAuthService:
    """Manage backend-specific login flows triggered through IM."""

    def __init__(self, controller):
        self.controller = controller
        self._flows: dict[str, AgentAuthFlow] = {}
        self._flows_by_id: dict[str, AgentAuthFlow] = {}
        self._flow_lock = asyncio.Lock()
        self.setup_timeout_seconds = 900.0
        # Web-initiated OAuth flows are keyed by ``flow_id`` only; no IM
        # context exists for them, so they cannot live in ``_flows``.
        self._web_flows: dict[str, WebAuthFlow] = {}
        self._web_flow_lock = asyncio.Lock()
        self._claude_oauth_attempt_counter = 0
        self._claude_oauth_batch: ClaudeOAuthBatch | None = None
        self._claude_oauth_lock = asyncio.Lock()
        self._claude_control_flow_starts_in_flight = 0
        self._recover_interrupted_claude_oauth_settings_backup()
        # Optional callable invoked after a successful *web* auth flow so
        # the UI-server process can ask the long-running controller to
        # reload V2Config-backed credentials. The hook receives ``(backend,)``
        # and runs in a worker thread to avoid blocking the auth event loop.
        self._post_web_success_hook: Optional[Any] = None

    def _active_claude_auth_clients(self) -> list[Any]:
        clients: list[Any] = []
        for flow in list(self._flows.values()):
            if flow.backend != "claude" or flow.claude_client is None:
                continue
            clients.append(flow.claude_client)
        for flow in list(self._web_flows.values()):
            if flow.backend != "claude" or flow.claude_client is None:
                continue
            clients.append(flow.claude_client)
        return clients

    def active_claude_auth_client_pids(self) -> set[int]:
        """Return Claude SDK client pids owned by in-progress auth flows."""
        pids: set[int] = set()
        for client in self._active_claude_auth_clients():
            pid = get_claude_client_pid(client)
            if pid:
                pids.add(pid)
        return pids

    def has_active_claude_auth_client_with_unknown_pid(self) -> bool:
        """Whether an in-progress Claude auth flow owns a client with no exposed pid."""
        if self._claude_control_flow_starts_in_flight > 0:
            return True
        return any(get_claude_client_pid(client) is None for client in self._active_claude_auth_clients())

    def _t(self, key: str, **kwargs) -> str:
        lang = getattr(self.controller, "_get_lang", lambda: getattr(self.controller.config, "language", "en"))()
        return i18n_t(key, lang, **kwargs)

    def _lang(self) -> str:
        return getattr(self.controller, "_get_lang", lambda: getattr(self.controller.config, "language", "en"))()

    def _resolve_backend_config(self, backend: str):
        """Return the backend's config object regardless of controller shape.

        The IM ``Controller`` carries an ``AppCompatConfig`` with backends at
        the top level (``config.claude``, ``config.codex``, ...), so the long-
        standing ``getattr(self.controller.config, backend, None)`` access
        works there. The web OAuth flow uses ``_WebControllerStub`` which
        exposes the raw ``V2Config`` instead — backends live under
        ``config.agents.<backend>`` and the top-level attribute is ``None``.

        Falling back through both shapes means ``build_claude_subprocess_env``
        (and similar) consistently sees the user's stored ``auth_mode`` /
        ``api_key`` / ``base_url`` whether the call originates from an IM
        session or from the Settings → Backends OAuth panel.
        """
        cfg = getattr(self, "controller", None)
        cfg = getattr(cfg, "config", None) if cfg is not None else None
        if cfg is None:
            return None
        top = getattr(cfg, backend, None)
        if top is not None:
            return top
        return getattr(getattr(cfg, "agents", None), backend, None)

    def _get_im_client(self, context: MessageContext):
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            return getter(context)
        return self.controller.im_client

    def _get_settings_key(self, context: MessageContext) -> str:
        return self.controller._get_settings_key(context)

    def _get_session_key(self, context: MessageContext) -> str:
        getter = getattr(self.controller, "_get_session_key", None)
        if callable(getter):
            return getter(context)
        return self._get_settings_key(context)

    def _make_flow_key(self, context: MessageContext, backend: str) -> str:
        return f"{self._get_settings_key(context)}:{backend}"

    def _get_cli_binary(self, backend: str) -> str:
        # Same dual-shape issue as ``_resolve_backend_config``: V2Config
        # carries the binary under ``config.agents.<backend>.cli_path``,
        # but ``AppCompatConfig`` (the shape live IM controllers run on)
        # exposes it at the top level — and renames it: ``config.claude.
        # cli_path``, ``config.codex.binary``, ``config.opencode.
        # binary``. Without these fallbacks, setup / logout / test
        # flows ignore a non-default cli_path and fall through to
        # ``$PATH``, breaking installs that pin a specific binary.
        backend_cfg = self._resolve_backend_config(backend)
        cli_path = getattr(backend_cfg, "cli_path", None) or getattr(backend_cfg, "binary", None)
        return cli_path or backend

    def _resolve_claude_probe_cwd(self) -> str:
        """Return the cwd that a Settings Claude probe should execute in.

        Plain ``claude -p`` loads project hooks, MCP config, and CLAUDE.md from
        the process cwd. Use the same default runtime cwd as live Agent turns so
        the Settings test reflects the actual Claude runtime instead of the UI
        server launch directory.
        """
        config = getattr(getattr(self, "controller", None), "config", None)
        candidates = (
            getattr(self._resolve_backend_config("claude"), "cwd", None),
            getattr(getattr(config, "runtime", None), "default_cwd", None),
        )
        for raw in candidates:
            if isinstance(raw, str) and raw.strip():
                path = os.path.abspath(os.path.expanduser(raw.strip()))
                try:
                    os.makedirs(path, exist_ok=True)
                    return path
                except OSError as exc:
                    logger.warning("Failed to prepare Claude Settings probe cwd=%s: %s", path, exc)
        return os.getcwd()

    def _build_claude_full_subprocess_env(self, *, force_oauth: bool = False) -> dict[str, str]:
        """Return a complete environment for direct Claude CLI subprocesses.

        ``build_claude_subprocess_env`` intentionally returns only the
        Anthropic/Claude variables that should be visible to Claude SDK clients.
        For direct ``create_subprocess_exec(..., env=...)`` calls we need a full
        process env. Start from ``os.environ`` for PATH/etc, remove every
        Anthropic/Claude variable, then layer back only the allowed values so
        OAuth-mode filtering really deletes stale shell credentials.
        """
        env_override = dict(os.environ)
        for key in list(env_override.keys()):
            if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_"):
                env_override.pop(key, None)

        from vibe.claude_config import build_claude_subprocess_env

        env_override.update(
            build_claude_subprocess_env(
                self._resolve_backend_config("claude"),
                base_env=os.environ,
                force_oauth=force_oauth,
            )
        )
        return env_override

    async def _resolve_opencode_provider(self, context: MessageContext) -> str:
        override_agent = None
        override_model = None
        get_overrides = getattr(self.controller, "get_opencode_overrides", None)
        if callable(get_overrides):
            override_agent, override_model, _ = get_overrides(context)

        if isinstance(override_model, str) and "/" in override_model:
            return override_model.split("/", 1)[0]

        agent_service = getattr(self.controller, "agent_service", None)
        opencode_agent = getattr(agent_service, "agents", {}).get("opencode") if agent_service else None
        if opencode_agent and hasattr(opencode_agent, "_get_server"):
            try:
                server = await opencode_agent._get_server()
                runtime_provider = await self._resolve_opencode_provider_from_existing_session(context, server)
                if runtime_provider:
                    return runtime_provider
                agent_to_use = override_agent or server.get_default_agent_from_config()
                model_str = server.get_agent_model_from_config(agent_to_use)
                if isinstance(model_str, str) and "/" in model_str:
                    return model_str.split("/", 1)[0]
            except Exception as err:  # noqa: BLE001
                logger.info("Falling back to default OpenCode provider after lookup failure: %s", err)

        return "opencode"

    async def _resolve_opencode_provider_from_existing_session(self, context: MessageContext, server) -> str | None:
        session_handler = getattr(self.controller, "session_handler", None)
        sessions = getattr(self.controller, "sessions", None)
        if session_handler is None or sessions is None:
            return None

        get_info = getattr(session_handler, "get_session_info", None)
        if not callable(get_info):
            return None

        try:
            base_session_id, working_path, composite_key = get_info(context)
        except Exception as err:  # noqa: BLE001
            logger.debug("Failed to derive OpenCode session info for provider resolution: %s", err)
            return None

        session_key = self._get_session_key(context)
        get_session_id = getattr(sessions, "get_agent_session_id", None)
        if not callable(get_session_id):
            return None
        session_id = get_session_id(session_key, composite_key, "opencode")
        if not session_id:
            session_id = get_session_id(session_key, base_session_id, "opencode")
        if not session_id:
            return None

        try:
            messages = await server.list_messages(session_id, working_path)
        except Exception as err:  # noqa: BLE001
            logger.debug("Failed to inspect OpenCode session %s for provider resolution: %s", session_id, err)
            return None

        for message in reversed(messages):
            provider = self._extract_opencode_message_provider(message)
            if provider:
                logger.info(
                    "Resolved OpenCode provider %s from existing session %s for %s",
                    provider,
                    session_id,
                    base_session_id,
                )
                return provider
        return None

    def _extract_opencode_message_provider(self, message: dict[str, Any]) -> str | None:
        info = message.get("info")
        if not isinstance(info, dict):
            return None

        direct_provider = info.get("providerID")
        if isinstance(direct_provider, str) and direct_provider:
            return direct_provider

        model = info.get("model")
        if isinstance(model, dict):
            model_provider = model.get("providerID")
            if isinstance(model_provider, str) and model_provider:
                return model_provider

        return None

    def _get_opencode_login_method(self, provider: str) -> str | None:
        if provider == "openai":
            return "ChatGPT Pro/Plus (headless)"
        return None

    def _supports_direct_opencode_api_key_setup(self, provider: str | None) -> bool:
        return provider in OPENCODE_DIRECT_SETUP_URLS

    def _get_opencode_setup_url(self, provider: str | None) -> str:
        return OPENCODE_DIRECT_SETUP_URLS.get(provider or "", "https://opencode.ai/auth")

    async def handle_setup_command(self, context: MessageContext, args: str = "") -> None:
        """Process `/setup`, `/setup <backend>`, or `/setup code <value>`."""
        parts = (args or "").strip().split(maxsplit=2)
        if parts and parts[0].lower() == "code":
            if len(parts) < 2 or not parts[1].strip():
                await self._send_message(context, f"❌ {self._t('command.setup.codeUsage')}")
                return
            if len(parts) == 2:
                await self.submit_code(context, parts[1].strip())
                return
            await self.submit_code(context, parts[2].strip(), backend_hint=parts[1].strip().lower())
            return

        backend_hint = parts[0].strip().lower() if parts else None
        claude_login_method = None
        if backend_hint in {"cc", "claude-code"}:
            backend_hint = "claude"
        elif backend_hint == "cx":
            backend_hint = "codex"

        if backend_hint in {"oc", "open-code"}:
            backend_hint = "opencode"

        if backend_hint == "claude" and len(parts) > 1:
            claude_login_method = self._normalize_claude_login_method(parts[1])
            if claude_login_method is None:
                await self._send_message(context, f"❌ {self._t('command.setup.claudeMethodUsage')}")
                return

        if backend_hint and backend_hint not in {"claude", "codex", "opencode"}:
            await self._send_message(context, f"❌ {self._t('command.setup.unsupportedBackend', backend=backend_hint)}")
            return

        await self.start_setup(
            context,
            backend=backend_hint or None,
            force_reset=True,
            claude_login_method=claude_login_method,
        )

    async def handle_setup_callback(self, context: MessageContext, callback_data: str) -> None:
        """Handle `auth_setup:*` callback buttons."""
        parts = callback_data.split(":")
        backend = parts[1].strip().lower() if len(parts) > 1 else None
        claude_login_method = self._normalize_claude_login_method(parts[2]) if len(parts) > 2 else None
        if backend == "auto":
            backend = None
        await self.start_setup(context, backend=backend, force_reset=True, claude_login_method=claude_login_method)

    async def start_setup(
        self,
        context: MessageContext,
        backend: str | None = None,
        force_reset: bool = True,
        claude_login_method: str | None = None,
    ) -> None:
        """Start an auth flow for the resolved backend."""
        resolved_backend = backend or self.controller.resolve_agent_for_context(context)
        if resolved_backend not in {"claude", "codex", "opencode"}:
            await self._send_message(
                context,
                f"❌ {self._t('command.setup.unsupportedBackend', backend=resolved_backend)}",
            )
            return

        if resolved_backend == "claude" and claude_login_method is None:
            await self._prompt_claude_login_method(context)
            return

        async with self._flow_lock:
            flow_key = self._make_flow_key(context, resolved_backend)
            existing = self._flows.get(flow_key)
            if existing:
                await self._terminate_flow(existing)

            await self._send_message(
                context,
                f"⏳ {self._t('command.setup.starting', backend=resolved_backend)}",
            )

            try:
                if resolved_backend == "codex":
                    process = await self._start_codex_process(force_reset=force_reset)
                    flow = AgentAuthFlow(
                        flow_id=uuid.uuid4().hex[:12],
                        backend=resolved_backend,
                        settings_key=self._get_settings_key(context),
                        initiator_user_id=context.user_id,
                        context=context,
                        process=process,
                        reader_task=asyncio.create_task(asyncio.sleep(0)),
                        waiter_task=asyncio.create_task(asyncio.sleep(0)),
                    )
                elif resolved_backend == "claude":
                    client, manual_url, attempt = await self._start_claude_control_flow(
                        context,
                        force_reset=force_reset,
                        login_with_claude_ai=claude_login_method != "console",
                    )
                    flow = AgentAuthFlow(
                        flow_id=uuid.uuid4().hex[:12],
                        backend=resolved_backend,
                        settings_key=self._get_settings_key(context),
                        initiator_user_id=context.user_id,
                        context=context,
                        process=None,
                        reader_task=asyncio.create_task(asyncio.sleep(0)),
                        waiter_task=asyncio.create_task(asyncio.sleep(0)),
                        claude_client=client,
                        login_prompt_sent=True,
                        url=manual_url,
                        claude_oauth_attempt=attempt,
                    )
                else:
                    provider = await self._resolve_opencode_provider(context)
                    if self._supports_direct_opencode_api_key_setup(provider):
                        flow = AgentAuthFlow(
                            flow_id=uuid.uuid4().hex[:12],
                            backend=resolved_backend,
                            settings_key=self._get_settings_key(context),
                            initiator_user_id=context.user_id,
                            context=context,
                            process=None,
                            reader_task=asyncio.create_task(asyncio.sleep(0)),
                            waiter_task=asyncio.create_task(asyncio.sleep(0)),
                            provider=provider,
                            awaiting_code=True,
                            login_prompt_sent=True,
                            code_prompt_sent=True,
                            url=self._get_opencode_setup_url(provider),
                        )
                    else:
                        process, master_fd, provider = await self._start_opencode_process(context, force_reset=force_reset)
                        flow = AgentAuthFlow(
                            flow_id=uuid.uuid4().hex[:12],
                            backend=resolved_backend,
                            settings_key=self._get_settings_key(context),
                            initiator_user_id=context.user_id,
                            context=context,
                            process=process,
                            reader_task=asyncio.create_task(asyncio.sleep(0)),
                            waiter_task=asyncio.create_task(asyncio.sleep(0)),
                            pty_master_fd=master_fd,
                            provider=provider,
                        )
            except Exception as err:  # noqa: BLE001
                logger.error("Agent auth setup failed to start for %s: %s", resolved_backend, err, exc_info=True)
                await self._send_setup_start_failure(context, resolved_backend, str(err))
                return

            self._flows[flow_key] = flow
            self._flows_by_id[flow.flow_id] = flow
            if resolved_backend == "codex":
                flow.reader_task = asyncio.create_task(self._read_codex_output(process, context, resolved_backend))
                flow.waiter_task = asyncio.create_task(self._wait_for_completion(flow))
            elif resolved_backend == "claude":
                flow.waiter_task = asyncio.create_task(self._wait_for_claude_completion(flow))
                await self._send_message(
                    flow.context,
                    self._t("command.setup.claudeInstructions", url=manual_url),
                )
            else:
                if self._supports_direct_opencode_api_key_setup(flow.provider):
                    await self._send_message(
                        flow.context,
                        self._t(
                            "command.setup.opencodeInstructions",
                            provider=flow.provider or "opencode",
                            url=flow.url or self._get_opencode_setup_url(flow.provider),
                        ),
                    )
                else:
                    assert flow.pty_master_fd is not None
                    flow.reader_task = asyncio.create_task(
                        self._read_pty_output(process, flow.pty_master_fd, context, resolved_backend)
                    )
                    flow.waiter_task = asyncio.create_task(self._wait_for_completion(flow))

    async def submit_code(self, context: MessageContext, code: str, backend_hint: str | None = None) -> None:
        """Submit follow-up code to an active auth flow."""
        flow = self._find_flow_for_submission(context, backend_hint)
        if flow is None:
            await self._send_message(context, f"❌ {self._t('command.setup.noActiveFlow')}")
            return
        if flow.initiator_user_id != context.user_id:
            await self._send_message(context, f"❌ {self._t('command.setup.notFlowOwner')}")
            return
        if flow.backend == "claude":
            if flow.claude_client is None:
                await self._send_message(context, f"❌ {self._t('command.setup.codeNotSupported')}")
                return
            if not self._allows_proactive_code_submission(flow):
                await self._send_message(context, f"❌ {self._t('command.setup.notAwaitingCode')}")
                return

            callback = self._parse_claude_callback_code(code)
            if callback is None:
                await self._send_message(context, f"❌ {self._t('command.setup.claudeCallbackUsage')}")
                return

            authorization_code, state = callback
            await self._send_claude_callback(flow.claude_client, authorization_code, state)
            await self._send_message(context, f"✅ {self._t('command.setup.claudeCallbackSubmitted')}")
            return

        if flow.backend != "opencode":
            await self._send_message(context, f"❌ {self._t('command.setup.codeNotSupported')}")
            return
        normalized_code = code.strip()
        if self._supports_direct_opencode_api_key_setup(flow.provider):
            await self._install_opencode_api_key(flow.provider or "opencode", normalized_code)
            flow.awaiting_code = False
            await self._refresh_backend_runtime("opencode")
            await self._clear_backend_sessions_for_context("opencode", context)
            await self._send_message(context, f"✅ {self._t('command.setup.success', backend=flow.backend)}")
            self._drop_flow(flow)
            return
        if flow.pty_master_fd is None:
            await self._send_message(context, f"❌ {self._t('command.setup.codeNotSupported')}")
            return
        if not flow.awaiting_code and not self._allows_proactive_code_submission(flow):
            await self._send_message(context, f"❌ {self._t('command.setup.notAwaitingCode')}")
            return

        await asyncio.to_thread(os.write, flow.pty_master_fd, f"{normalized_code}\n".encode("utf-8"))
        flow.awaiting_code = False
        await self._send_message(context, f"✅ {self._t('command.setup.codeSubmitted', backend=flow.backend)}")

    async def maybe_consume_setup_reply(self, context: MessageContext, message: str) -> bool:
        """Intercept plain-text replies for active setup flows before normal agent routing."""
        if not message or message.lstrip().startswith("/"):
            return False

        flow = self._find_flow_for_submission(context, "claude")
        if flow is not None and flow.backend == "claude" and flow.initiator_user_id == context.user_id:
            if self._allows_proactive_code_submission(flow) and self._parse_claude_callback_code(message) is not None:
                await self.submit_code(context, message, backend_hint="claude")
                return True

        opencode_flow = self._find_flow_for_submission(context, "opencode")
        if (
            opencode_flow is not None
            and opencode_flow.backend == "opencode"
            and opencode_flow.initiator_user_id == context.user_id
            and opencode_flow.awaiting_code
            and self._looks_like_direct_opencode_credential(message)
        ):
            await self.submit_code(context, message.strip(), backend_hint="opencode")
            return True

        return False

    async def maybe_emit_auth_recovery_message(
        self,
        context: MessageContext,
        backend: str,
        error_text: str,
        *,
        output: MessageOutput | None = None,
        terminal_error: str | None = None,
    ) -> bool:
        """Emit a reset-oauth button when the backend error is auth-related.

        Each backend error-emit site calls this FIRST and emits its own failure
        path only when this returns ``False``. When this DOES handle the error,
        the recovery message is a button row, so settle the turn separately
        through the outbound chokepoint with a silent terminal failure. No-op
        off-workbench (the outbound resolves no session id).
        """
        if not classify_auth_error(backend, error_text):
            return False

        recovery_text = f"{error_text}\n\n{self._t('command.setup.resetPrompt', backend=backend)}"
        await self._send_message_with_button(
            context,
            recovery_text,
            button_text=self._t("button.resetOAuth"),
            callback_data=f"auth_setup:{backend}",
        )
        # The IM send above goes through ``send_message_with_buttons``, which is NOT
        # a durable ``messages`` row, and the web Chat renders only durable rows —
        # so persist the recovery text (error + reset instruction) HERE, the single
        # home for it, rather than each backend persisting an error-only copy that
        # drops the actionable reset prompt (Codex P2). No-op for contexts without
        # a resolvable scope (persist_agent_message guards internally).
        #
        # The durable row has NO inline button, so persist a BUTTON-FREE variant:
        # ``resetPrompt`` says "use the button below", which is a dangling
        # instruction on the workbench Chat. Point at the cross-platform
        # ``/setup {backend}`` command instead so the persisted copy is actionable
        # everywhere (Codex P2).
        durable_text = f"{error_text}\n\n{self._t('command.setup.resetPromptPlain', backend=backend)}"
        try:
            from core.message_mirror import persist_agent_message

            persist_agent_message(context, "notify", durable_text)
        except Exception:
            logger.debug("auth recovery: failed to persist durable notify", exc_info=True)
        # Settle the failed turn through the outbound status chokepoint: a silent
        # terminal failure turns the dot red and releases the SSE waiter
        # without adding a second visible message (the recovery button above is the
        # visible one). No-op off-workbench.
        try:
            await self.controller.emit_agent_message(
                context,
                "result",
                "",
                is_error=True,
                level="silent",
                output=output or terminal_turn_output(),
                terminal_error=terminal_error or error_text,
            )
        except Exception:
            logger.debug("auth recovery: failed to settle turn status", exc_info=True)
        return True

    async def _send_message(self, context: MessageContext, text: str) -> Optional[str]:
        return await self._get_im_client(context).send_message(context, text)

    async def _send_message_with_button(
        self,
        context: MessageContext,
        text: str,
        *,
        button_text: str,
        callback_data: str,
    ) -> Optional[str]:
        keyboard = InlineKeyboard(buttons=[[InlineButton(text=button_text, callback_data=callback_data)]])
        return await self._send_message_with_keyboard(context, text, keyboard)

    async def _send_message_with_keyboard(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        *,
        fallback_text: str | None = None,
    ) -> Optional[str]:
        im_client = self._get_im_client(context)
        if hasattr(im_client, "send_message_with_buttons"):
            button_text = text if not fallback_text else f"{text}\n\n{fallback_text}"
            return await im_client.send_message_with_buttons(context, button_text, keyboard)
        fallback = fallback_text or text
        return await im_client.send_message(context, fallback)

    async def _send_setup_start_failure(
        self,
        context: MessageContext,
        backend: str,
        detail: str,
    ) -> None:
        await self._send_message_with_button(
            context,
            f"❌ {self._t('command.setup.failed', backend=backend, detail=detail)}",
            button_text=self._t("button.resetOAuth"),
            callback_data=f"auth_setup:{backend}",
        )

    async def _prompt_claude_login_method(self, context: MessageContext) -> None:
        text = self._t("command.setup.claudeMethodPrompt")
        keyboard = InlineKeyboard(
            buttons=[
                [
                    InlineButton(text=self._t("button.claudeAi"), callback_data="auth_setup:claude:claudeai"),
                    InlineButton(text=self._t("button.console"), callback_data="auth_setup:claude:console"),
                ]
            ]
        )
        fallback_text = self._t("command.setup.claudeMethodFallback")
        await self._send_message_with_keyboard(context, text, keyboard, fallback_text=fallback_text)

    async def _start_codex_process(self, *, force_reset: bool) -> asyncio.subprocess.Process:
        binary = self._get_cli_binary("codex")
        if force_reset:
            await self._run_utility_command(binary, "logout")
        return await asyncio.create_subprocess_exec(
            binary,
            "login",
            "--device-auth",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def _start_claude_control_flow(
        self,
        context: Optional[MessageContext] = None,
        *,
        force_reset: bool,
        login_with_claude_ai: bool,
    ) -> tuple[ClaudeSDKClient, str, ClaudeOAuthAttempt]:
        if not CLAUDE_SDK_AVAILABLE:
            raise ModuleNotFoundError("claude_agent_sdk is required for Claude setup flows")

        if force_reset:
            await self._run_utility_command(self._get_cli_binary("claude"), "auth", "logout")
        # Claude Code re-applies ``settings.json`` env at startup, so an
        # OAuth flow must clear stale API-key settings before the control
        # client or follow-up probes launch.
        attempt = await self._begin_claude_oauth_attempt()
        client = None
        self._claude_control_flow_starts_in_flight += 1
        try:
            client = await self._create_claude_control_client(context)
            register_claude_owned_process(client, owner=AVIBE_CLAUDE_AUTH_OWNER)
            response = await self._send_claude_control_request(
                client,
                {
                    "subtype": "claude_authenticate",
                    "loginWithClaudeAi": login_with_claude_ai,
                },
            )
        except Exception:
            await self._finish_claude_oauth_attempt(attempt, succeeded=False)
            if client is not None:
                await self._disconnect_claude_client(client)
            raise
        finally:
            self._claude_control_flow_starts_in_flight = max(
                0,
                self._claude_control_flow_starts_in_flight - 1,
            )

        manual_url = str(response.get("manualUrl") or "").strip()
        if not manual_url:
            await self._finish_claude_oauth_attempt(attempt, succeeded=False)
            if client is not None:
                await self._disconnect_claude_client(client)
            raise RuntimeError("Claude auth flow did not return a manual login URL")
        return client, manual_url, attempt

    async def _create_claude_control_client(
        self, context: Optional[MessageContext] = None
    ) -> ClaudeSDKClient:
        session_handler = getattr(self.controller, "session_handler", None)
        get_working_path = getattr(session_handler, "get_working_path", None) if session_handler else None
        if context is not None and callable(get_working_path):
            working_path = get_working_path(context)
        else:
            # Web-initiated flows do not have an IM session; fall back to the
            # process cwd so the SDK client still has a stable cwd to attach
            # its OAuth callback transport to.
            working_path = os.getcwd()

        if not os.path.exists(working_path):
            os.makedirs(working_path, exist_ok=True)

        # Reuse the session-handler env composition so the auth_mode /
        # api_key / base_url overrides apply uniformly. Without this, an
        # OAuth-mode user with ``ANTHROPIC_API_KEY`` in their shell would
        # still get the key into the control-channel SDK client.
        from vibe.claude_config import build_claude_subprocess_env

        # ``force_oauth=True`` because this code path IS the OAuth
        # setup flow — the control-channel SDK client must run with
        # OAuth semantics regardless of ``auth_mode_set`` (a legacy
        # install on its very first sign-in attempt has
        # ``auth_mode_set=False``, so the default env-preserving path
        # would leak inherited ``ANTHROPIC_*`` vars into the OAuth
        # handshake and break the login).
        claude_env = build_claude_subprocess_env(
            self._resolve_backend_config("claude"),
            force_oauth=True,
        )
        claude_env[AVIBE_CLAUDE_PROCESS_OWNER_ENV] = AVIBE_CLAUDE_AUTH_OWNER

        should_mark_isolated = getattr(session_handler, "_should_mark_claude_isolated_env", None)
        if callable(should_mark_isolated) and should_mark_isolated():
            claude_env["IS_SANDBOX"] = "1"

        option_kwargs = {
            "cwd": working_path,
            "env": claude_env,
            "setting_sources": ["user", "project", "local"],
            "max_buffer_size": CLAUDE_SDK_MAX_BUFFER_SIZE,
        }
        permission_mode = getattr(self._resolve_backend_config("claude"), "permission_mode", None)
        if permission_mode:
            option_kwargs["permission_mode"] = permission_mode

        get_cli_override = getattr(session_handler, "_get_claude_cli_path_override", None)
        cli_override = get_cli_override() if callable(get_cli_override) else None
        if cli_override:
            option_kwargs["cli_path"] = cli_override

        client = ClaudeSDKClient(options=ClaudeAgentOptions(**option_kwargs))
        await client.connect()
        governor_from_controller(self.controller).apply_to_pid(
            get_claude_client_pid(client),
            label="claude auth",
        )
        return client

    async def _send_claude_control_request(
        self,
        client: ClaudeSDKClient,
        request: dict[str, object],
        *,
        timeout: float = 900.0,
    ) -> dict[str, object]:
        query = getattr(client, "_query", None)
        sender = getattr(query, "_send_control_request", None)
        if not callable(sender):
            raise RuntimeError("Claude SDK control channel is not available")
        response = await sender(request, timeout=timeout)
        return response if isinstance(response, dict) else {}

    async def _send_claude_callback(
        self,
        client: ClaudeSDKClient,
        authorization_code: str,
        state: str,
    ) -> None:
        transport = getattr(client, "_transport", None)
        if transport is None or not hasattr(transport, "write"):
            raise RuntimeError("Claude SDK transport is not available")

        message = {
            "type": "control_request",
            "request_id": f"auth-callback-{uuid.uuid4().hex}",
            "request": {
                "subtype": "claude_oauth_callback",
                "authorizationCode": authorization_code,
                "state": state,
            },
        }
        await transport.write(json.dumps(message) + "\n")

    async def _disconnect_claude_client(self, client: ClaudeSDKClient) -> None:
        disconnect = getattr(client, "disconnect", None)
        close = getattr(client, "close", None)
        try:
            if callable(disconnect):
                await disconnect()
            elif callable(close):
                await close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error disconnecting Claude auth client: %s", exc)

    async def _start_opencode_process(
        self,
        context: MessageContext,
        *,
        force_reset: bool,
    ) -> tuple[asyncio.subprocess.Process, int, str]:
        binary = self._get_cli_binary("opencode")
        provider = await self._resolve_opencode_provider(context)
        method = self._get_opencode_login_method(provider)
        # OpenCode auth is provider-scoped and may keep multiple credentials.
        # `opencode auth logout` can become interactive, so refresh by re-running
        # login for the target provider instead of forcing a global logout.
        master_fd, slave_fd = os.openpty()
        try:
            cmd = [
                binary,
                "auth",
                "login",
                "-p",
                provider,
            ]
            if method:
                cmd.extend(["-m", method])
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return process, master_fd, provider

    async def _run_utility_command(
        self,
        *cmd: str,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        """Run a short CLI side-call. Returns ``(ok, error_excerpt)``.

        Callers that don't care about the outcome (setup preflight)
        can ignore the return; ``remove_web_auth`` uses it to surface
        ``codex logout`` / ``claude auth logout`` failures so the UI
        doesn't lie about a partial sign-out.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            if process.returncode == 0:
                return True, None
            output = (stdout or b"").decode("utf-8", errors="replace").strip()
            logger.info(
                "Utility command failed (exit=%s) for %s: %s",
                process.returncode,
                " ".join(cmd),
                output[:400] or "<no output>",
            )
            return False, output[:400] or f"exit {process.returncode}"
        except Exception as err:  # noqa: BLE001
            logger.info("Utility command raised for %s: %s", " ".join(cmd), err)
            return False, str(err)

    async def _read_codex_output(
        self,
        process: asyncio.subprocess.Process,
        context: MessageContext,
        backend: str,
    ) -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await self._handle_process_text(context, backend, line.decode("utf-8", errors="replace"))

    async def _read_pty_output(
        self,
        process: asyncio.subprocess.Process,
        master_fd: int,
        context: MessageContext,
        backend: str,
    ) -> None:
        try:
            os.set_blocking(master_fd, False)
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except BlockingIOError:
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.05)
                    continue
                except OSError as err:
                    if err.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.05)
                    continue
                await self._handle_process_text(context, backend, chunk.decode("utf-8", errors="replace"))
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    async def _handle_process_text(self, context: MessageContext, backend: str, text: str) -> None:
        flow = self._flows.get(self._make_flow_key(context, backend))
        if flow is None:
            return

        clean = sanitize_process_output(text)
        if not clean:
            return

        flow.last_status_text = clean

        if backend == "codex":
            maybe_url = CODEX_URL_RE.search(clean)
            maybe_code = CODEX_DEVICE_CODE_RE.search(clean)
            if maybe_url:
                flow.url = maybe_url.group(0)
            if maybe_code:
                flow.device_code = maybe_code.group(0)
            if flow.url and flow.device_code and not flow.login_prompt_sent:
                flow.login_prompt_sent = True
                await self._send_message(
                    flow.context,
                    self._t(
                        "command.setup.codexInstructions",
                        url=flow.url,
                        code=flow.device_code,
                    ),
                )
            return

        maybe_url = URL_RE.search(clean)
        compact = re.sub(r"\s+", "", clean).lower()

        if backend == "claude":
            return

        maybe_code = CODEX_DEVICE_CODE_RE.search(clean)
        if maybe_url:
            flow.url = maybe_url.group(0)
        if maybe_code:
            flow.device_code = maybe_code.group(0)
        if flow.provider == "openai" and flow.url and flow.device_code and not flow.login_prompt_sent:
            flow.login_prompt_sent = True
            await self._send_message(
                flow.context,
                self._t(
                    "command.setup.opencodeDeviceInstructions",
                    url=flow.url,
                    code=flow.device_code,
                ),
            )
            return

        if flow.url and not flow.login_prompt_sent and flow.provider != "openai":
            flow.login_prompt_sent = True
            await self._send_message(
                flow.context,
                self._t(
                    "command.setup.opencodeInstructions",
                    provider=flow.provider or "opencode",
                    url=flow.url,
                ),
            )

        if OPENCODE_API_KEY_PROMPT_RE.search(compact):
            was_awaiting_code = flow.awaiting_code
            flow.awaiting_code = True
            prompt_key = "command.setup.opencodeCodePrompt"
            if flow.code_prompt_sent:
                if was_awaiting_code:
                    return
                prompt_key = "command.setup.opencodeCodeRetryPrompt"
            else:
                flow.code_prompt_sent = True

            await self._send_message(
                flow.context,
                self._t(
                    prompt_key,
                    provider=flow.provider or "opencode",
                ),
            )

    async def _wait_for_completion(self, flow: AgentAuthFlow) -> None:
        try:
            assert flow.process is not None
            await asyncio.wait_for(flow.process.wait(), timeout=self.setup_timeout_seconds)
            await flow.reader_task
            ok, detail = await self._verify_login(flow)
            if ok:
                if flow.backend == "codex":
                    await self._persist_backend_auth_mode(flow.backend, "oauth")
                await self._refresh_backend_runtime(flow.backend)
                await self._send_message(
                    flow.context,
                    f"✅ {self._t('command.setup.success', backend=flow.backend)}",
                )
            else:
                detail_text = detail or self._t("command.setup.unknownFailure")
                await self._send_message_with_button(
                    flow.context,
                    f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=detail_text)}",
                    button_text=self._t("button.resetOAuth"),
                    callback_data=f"auth_setup:{flow.backend}",
                )
        except asyncio.TimeoutError:
            await self._terminate_process_for_timeout(flow)
            await self._send_message_with_button(
                flow.context,
                f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=self._t('command.setup.timedOut', backend=flow.backend))}",
                button_text=self._t("button.resetOAuth"),
                callback_data=f"auth_setup:{flow.backend}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.error("Agent auth flow failed for %s: %s", flow.backend, err, exc_info=True)
            await self._send_message_with_button(
                flow.context,
                f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=str(err))}",
                button_text=self._t("button.resetOAuth"),
                callback_data=f"auth_setup:{flow.backend}",
            )
        finally:
            self._drop_flow(flow)

    async def _wait_for_claude_completion(self, flow: AgentAuthFlow) -> None:
        try:
            if flow.claude_client is None:
                raise RuntimeError("Claude auth flow is missing its SDK client")

            await asyncio.wait_for(
                self._send_claude_control_request(
                    flow.claude_client,
                    {"subtype": "claude_oauth_wait_for_completion"},
                    timeout=self.setup_timeout_seconds,
                ),
                timeout=self.setup_timeout_seconds,
            )
            flow.force_oauth = True
            ok, detail = await self._verify_login(flow)
            if ok:
                await self._persist_backend_auth_mode(flow.backend, "oauth")
                await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=True)
                await self._refresh_backend_runtime(flow.backend)
                await self._send_message(
                    flow.context,
                    f"✅ {self._t('command.setup.success', backend=flow.backend)}",
                )
            else:
                await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
                detail_text = detail or self._t("command.setup.unknownFailure")
                await self._send_message_with_button(
                    flow.context,
                    f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=detail_text)}",
                    button_text=self._t("button.resetOAuth"),
                    callback_data=f"auth_setup:{flow.backend}",
                )
        except asyncio.TimeoutError:
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            await self._send_message_with_button(
                flow.context,
                f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=self._t('command.setup.timedOut', backend=flow.backend))}",
                button_text=self._t("button.resetOAuth"),
                callback_data=f"auth_setup:{flow.backend}",
            )
        except asyncio.CancelledError:
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            raise
        except Exception as err:  # noqa: BLE001
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            logger.error("Claude auth flow failed: %s", err, exc_info=True)
            await self._send_message_with_button(
                flow.context,
                f"❌ {self._t('command.setup.failed', backend=flow.backend, detail=str(err))}",
                button_text=self._t("button.resetOAuth"),
                callback_data=f"auth_setup:{flow.backend}",
            )
        finally:
            if flow.claude_client is not None:
                await self._disconnect_claude_client(flow.claude_client)
            self._drop_flow(flow)

    async def _verify_login(self, flow: AgentAuthFlow) -> tuple[bool, str]:
        backend = flow.backend
        if backend == "codex":
            binary = self._get_cli_binary("codex")
            process = await asyncio.create_subprocess_exec(
                binary,
                "login",
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            text = stdout.decode("utf-8", errors="replace").strip()
            return ("not logged in" not in text.lower(), text)

        if backend == "opencode":
            binary = self._get_cli_binary("opencode")
            process = await asyncio.create_subprocess_exec(
                binary,
                "auth",
                "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            text = stdout.decode("utf-8", errors="replace").strip()
            if process.returncode and process.returncode != 0:
                return False, self._describe_opencode_cli_failure(process.returncode, text)
            return (verify_opencode_auth_list_output(text, flow.provider), text)

        force_oauth = bool(getattr(flow, "force_oauth", False))
        settings_backup: dict[str, str] | None = None
        if force_oauth:
            settings_backup = await self._prepare_claude_oauth_probe(flow.claude_oauth_attempt)
        binary = self._get_cli_binary("claude")
        process = await asyncio.create_subprocess_exec(
            binary,
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._build_claude_full_subprocess_env(force_oauth=force_oauth),
        )
        stdout, _ = await process.communicate()
        text = stdout.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            if force_oauth and flow.claude_oauth_attempt is None:
                await self._restore_transient_claude_oauth_probe_backup(settings_backup)
            return False, text
        logged_in = bool(payload.get("loggedIn"))
        if force_oauth and not logged_in and flow.claude_oauth_attempt is None:
            await self._restore_transient_claude_oauth_probe_backup(settings_backup)
        return (logged_in, text)

    def _describe_opencode_cli_failure(self, returncode: int, text: str) -> str:
        detail = text or ""
        lowered = detail.lower()
        if "segmentation fault" in lowered or returncode == -signal.SIGSEGV:
            return "OpenCode CLI crashed with Segmentation fault during auth verification."
        if returncode < 0:
            try:
                signal_name = signal.Signals(-returncode).name
            except ValueError:
                signal_name = f"signal {-returncode}"
            return f"OpenCode CLI crashed during auth verification ({signal_name})."
        if detail:
            return detail
        return f"OpenCode auth verification failed with exit code {returncode}."

    async def _install_opencode_api_key(self, provider: str, api_key: str) -> None:
        agent_service = getattr(self.controller, "agent_service", None)
        opencode_agent = getattr(agent_service, "agents", {}).get("opencode") if agent_service else None
        if not opencode_agent or not hasattr(opencode_agent, "_get_server"):
            raise RuntimeError("OpenCode agent is not available for auth setup.")

        server = await opencode_agent._get_server()
        setter = getattr(server, "set_api_key_auth", None)
        if not callable(setter):
            raise RuntimeError("OpenCode server does not support non-interactive auth setup.")
        await setter(provider, api_key)
        try:
            await asyncio.to_thread(
                remove_opencode_provider_api_key,
                provider,
                logger_instance=logger,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to clean OpenCode provider option apiKey for %s: %s", provider, err)

    async def _clear_opencode_provider_options_key_for_oauth(self, provider: str) -> None:
        """Remove a Vibe-managed API key after OpenCode OAuth succeeds.

        OpenCode builds SDK options from ``opencode.json`` before falling back
        to auth entries, so leaving ``provider.<id>.options.apiKey`` in place
        would make a successful OAuth login non-authoritative at runtime.
        """

        try:
            await asyncio.to_thread(
                remove_opencode_provider_api_key,
                provider,
                logger_instance=logger,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to clear OpenCode provider option apiKey after OAuth for %s: %s", provider, err)

    async def _refresh_opencode_server(self, *, force: bool = False) -> None:
        agent_service = getattr(self.controller, "agent_service", None)
        opencode_agent = getattr(agent_service, "agents", {}).get("opencode") if agent_service else None
        if not opencode_agent or not hasattr(opencode_agent, "_get_server"):
            return
        server = await opencode_agent._get_server()
        if hasattr(server, "restart_for_auth_refresh"):
            if force:
                await server.restart_for_auth_refresh(force=True)
            else:
                await server.restart_for_auth_refresh()

    def _load_backend_runtime_config(self, backend: str):
        from config.v2_compat import to_app_config
        from config.v2_config import V2Config

        return getattr(to_app_config(V2Config.load()), backend, None)

    def _load_saved_enabled_backends(self) -> list[str] | None:
        try:
            from config.v2_config import V2Config

            agent_config = V2Config.load().agents
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to load saved enabled backends during runtime refresh: %s", err)
            return None

        return [
            backend
            for backend in ("opencode", "claude", "codex")
            if bool(getattr(getattr(agent_config, backend, None), "enabled", False))
        ]

    def _sync_builtin_default_agents(self) -> None:
        store = getattr(self.controller, "vibe_agent_store", None)
        ensure = getattr(store, "ensure_builtin_default_agents", None) if store is not None else None
        if not callable(ensure):
            return
        enabled_backends = self._load_saved_enabled_backends()
        if enabled_backends is None:
            enabled_backends = list(getattr(getattr(self.controller, "agent_service", None), "agents", {}).keys())
        try:
            ensure(enabled_backends)
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to sync built-in Agents after backend runtime refresh: %s", err)

    async def _unregister_disabled_backend_agent(self, backend: str) -> bool:
        agent_service = getattr(self.controller, "agent_service", None)
        agent = getattr(agent_service, "agents", {}).pop(backend, None) if agent_service else None
        if agent is None:
            setattr(self.controller.config, backend, None)
            self._sync_builtin_default_agents()
            return False

        shutdown = getattr(agent, "shutdown_runtime", None)
        if callable(shutdown):
            await shutdown()
        elif backend == "opencode":
            server_manager = getattr(agent, "_client_manager", None)
            reset_config = getattr(server_manager, "reset_config", None)
            if callable(reset_config):
                previous_server = await reset_config(None)
                if previous_server is not None:
                    detach = getattr(previous_server, "detach_after_deferred_refresh", None)
                    if callable(detach):
                        await detach()
        refresh = getattr(agent, "refresh_auth_state", None)
        if callable(refresh):
            await refresh()

        setattr(self.controller.config, backend, None)
        self._sync_builtin_default_agents()
        logger.info("Unregistered disabled %s backend after runtime config refresh", backend)
        return True

    def _register_missing_backend_agent(self, backend: str, runtime_config: Any) -> bool:
        agent_service = getattr(self.controller, "agent_service", None)
        if agent_service is None or backend in getattr(agent_service, "agents", {}):
            return False

        if backend == "codex":
            from modules.agents.codex import CodexAgent

            self.controller.config.codex = runtime_config
            agent_service.register(CodexAgent(self.controller, runtime_config))
        elif backend == "opencode":
            from modules.agents.opencode import OpenCodeAgent

            self.controller.config.opencode = runtime_config
            agent_service.register(OpenCodeAgent(self.controller, runtime_config))
        else:
            return False

        self._sync_builtin_default_agents()

        logger.info("Registered %s backend after runtime config refresh", backend)
        return True

    async def _refresh_backend_runtime(self, backend: str) -> None:
        coordinator = getattr(self.controller, "backend_restart_coordinator", None)
        if coordinator is not None:
            await coordinator.request_restart(backend)
            return
        await self._apply_backend_runtime_refresh(backend, False)

    async def _apply_backend_runtime_refresh(self, backend: str, force: bool = False) -> None:
        agent_service = getattr(self.controller, "agent_service", None)
        runtime_tokens: dict[str, str] = {}
        snapshot_tokens = getattr(agent_service, "runtime_turn_tokens_for_backend", None)
        if callable(snapshot_tokens):
            runtime_tokens = snapshot_tokens(backend)
        try:
            refresh_runtime_config = getattr(agent_service, "refresh_runtime_config", None)
            runtime_config = None
            if callable(refresh_runtime_config):
                runtime_config = self._load_backend_runtime_config(backend)
                if runtime_config is None:
                    await self._unregister_disabled_backend_agent(backend)
                    return
                if runtime_config is not None and self._register_missing_backend_agent(backend, runtime_config):
                    return
                if force and backend == "opencode":
                    agent = getattr(agent_service, "agents", {}).get(backend)
                    refresh_config = getattr(agent, "refresh_runtime_config", None)
                    if callable(refresh_config):
                        await refresh_config(runtime_config, force=True)
                        self._sync_builtin_default_agents()
                        return
                if runtime_config is not None and await refresh_runtime_config(backend, runtime_config):
                    self._sync_builtin_default_agents()
                    return

            agent = getattr(agent_service, "agents", {}).get(backend) if agent_service else None
            refresh_config = getattr(agent, "refresh_runtime_config", None)
            if callable(refresh_config):
                if runtime_config is None:
                    runtime_config = self._load_backend_runtime_config(backend)
                if runtime_config is None:
                    return
                await refresh_config(runtime_config)
                return
            if backend == "opencode":
                if force:
                    await self._refresh_opencode_server(force=True)
                else:
                    await self._refresh_opencode_server()
                return
            refresh = getattr(agent, "refresh_auth_state", None)
            if callable(refresh):
                await refresh()
        finally:
            release_tokens = getattr(agent_service, "release_runtime_turn_tokens", None)
            if callable(release_tokens):
                release_tokens(runtime_tokens)
            else:
                release_turns = getattr(agent_service, "release_runtime_turns_for_backend", None)
                if callable(release_turns):
                    release_turns(backend)

    async def _clear_backend_sessions_for_context(self, backend: str, context: MessageContext) -> None:
        agent_service = getattr(self.controller, "agent_service", None)
        clear_backend_sessions = getattr(agent_service, "clear_backend_sessions", None)
        session_key = self._get_session_key(context)
        if callable(clear_backend_sessions):
            await clear_backend_sessions(backend, session_key)
            return
        agent = getattr(agent_service, "agents", {}).get(backend) if agent_service else None
        clear_sessions = getattr(agent, "clear_sessions", None)
        if not callable(clear_sessions):
            return
        await clear_sessions(session_key)

    def _find_flow_for_submission(self, context: MessageContext, backend_hint: str | None) -> AgentAuthFlow | None:
        settings_key = self._get_settings_key(context)
        if backend_hint:
            return self._flows.get(f"{settings_key}:{backend_hint}")

        candidates = [
            flow
            for flow in self._flows.values()
            if flow.settings_key == settings_key and flow.initiator_user_id == context.user_id
        ]
        awaiting_candidates = [flow for flow in candidates if flow.awaiting_code]
        if awaiting_candidates:
            return awaiting_candidates[-1]

        code_capable_candidates = [
            flow
            for flow in candidates
            if (flow.backend == "claude" and flow.claude_client is not None)
            or (flow.backend == "opencode" and flow.pty_master_fd is not None)
        ]
        if code_capable_candidates:
            return code_capable_candidates[-1]

        return candidates[-1] if candidates else None

    def _allows_proactive_code_submission(self, flow: AgentAuthFlow) -> bool:
        return (
            flow.backend == "claude"
            and flow.claude_client is not None
            and flow.login_prompt_sent
        )

    def _parse_claude_callback_code(self, code: str) -> tuple[str, str] | None:
        authorization_code, separator, state = code.strip().partition("#")
        if separator != "#" or not authorization_code or not state:
            return None
        return authorization_code, state

    def _looks_like_direct_opencode_credential(self, text: str) -> bool:
        candidate = text.strip()
        if len(candidate) < 16 or any(ch.isspace() for ch in candidate):
            return False
        if URL_RE.search(candidate):
            return False

        alnum_count = sum(ch.isalnum() for ch in candidate)
        has_digit = any(ch.isdigit() for ch in candidate)
        has_upper = any(ch.isupper() for ch in candidate)
        has_lower = any(ch.islower() for ch in candidate)
        separator_count = sum(candidate.count(ch) for ch in ("-", "_", ".", ":", "="))

        if alnum_count < 8:
            return False

        if candidate.startswith("sk-"):
            return True

        if separator_count >= 2:
            return True

        return has_digit and ((has_upper and has_lower) or separator_count >= 1)

    def _normalize_claude_login_method(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        aliases = {
            "claude": "claudeai",
            "claude.ai": "claudeai",
            "claudeai": "claudeai",
            "subscription": "claudeai",
            "console": "console",
            "platform": "console",
            "platform.claude.com": "console",
        }
        mapped = aliases.get(normalized)
        return mapped if mapped in CLAUDE_LOGIN_METHODS else None

    async def _terminate_flow(self, flow: AgentAuthFlow) -> None:
        await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
        if flow.waiter_task and not flow.waiter_task.done():
            flow.waiter_task.cancel()
            try:
                await flow.waiter_task
            except asyncio.CancelledError:
                pass
        if flow.reader_task and not flow.reader_task.done():
            flow.reader_task.cancel()
            try:
                await flow.reader_task
            except asyncio.CancelledError:
                pass
        if flow.process is not None and flow.process.returncode is None:
            flow.process.terminate()
            try:
                await asyncio.wait_for(flow.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                flow.process.kill()
                await flow.process.wait()
        if flow.claude_client is not None:
            await self._disconnect_claude_client(flow.claude_client)
        self._drop_flow(flow)

    async def _terminate_process_for_timeout(self, flow: AgentAuthFlow) -> None:
        if flow.reader_task and not flow.reader_task.done():
            flow.reader_task.cancel()
            try:
                await flow.reader_task
            except asyncio.CancelledError:
                pass
        if flow.process is not None and flow.process.returncode is None:
            flow.process.terminate()
            try:
                await asyncio.wait_for(flow.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                flow.process.kill()
                await flow.process.wait()

    def _drop_flow(self, flow: AgentAuthFlow) -> None:
        if self._flows.get(flow.flow_key) is flow:
            self._flows.pop(flow.flow_key, None)
        self._flows_by_id.pop(flow.flow_id, None)

    # ------------------------------------------------------------------
    # Web Settings → Backends OAuth flows
    #
    # These methods power ``Settings → Backends → {Claude,Codex} → Sign in``
    # in the browser. They reuse the same subprocess / SDK plumbing the IM
    # ``/setup`` command uses (``_start_codex_process``,
    # ``_start_claude_control_flow``, ``_verify_login``,
    # ``_refresh_backend_runtime``) but never call ``_send_message`` — every
    # piece of state the UI needs is parked on the ``WebAuthFlow`` record
    # and read by a polling endpoint.
    # ------------------------------------------------------------------

    WEB_BACKENDS = WEB_OAUTH_BACKENDS

    # OpenCode OAuth providers route through the daemon's HTTP API rather
    # than a CLI subprocess. The Settings UI hands us the provider_id and
    # we pick the friendliest method index — see
    # ``_resolve_opencode_oauth_method`` for the heuristic.
    _OPENCODE_OAUTH_PROMPT_ANSWERS: dict[str, dict[str, Any]] = {
        # github-copilot's first prompt is a deployment-type select. We
        # ship github.com support out of the box; enterprise users can
        # still configure via terminal until we surface a select in the UI.
        "github-copilot": {"deploymentType": "github.com"},
    }

    async def start_web_setup(
        self,
        backend: str,
        *,
        force_reset: bool = True,
        provider_id: Optional[str] = None,
    ) -> WebAuthFlow:
        """Start an OAuth flow initiated from the Settings page.

        Returns the freshly-created ``WebAuthFlow``. For Codex the caller
        should poll ``get_web_flow_status`` until ``state == "awaiting_code"``
        appears (URL + device code surfaced) and then again until the user
        completes the device auth on OpenAI's side. For Claude the call
        returns once the manual URL is available; the user then submits the
        callback code via ``submit_web_code``. For OpenCode the caller
        must pass ``provider_id``; the URL (and optional device code) are
        surfaced via ``WebAuthFlow.url`` / ``WebAuthFlow.device_code`` and
        completion is auto-detected by OpenCode's daemon — no code submit
        from the user.
        """
        if backend not in self.WEB_BACKENDS:
            raise ValueError(f"unsupported_backend:{backend}")

        flow_id = uuid.uuid4().hex[:12]
        flow = WebAuthFlow(flow_id=flow_id, backend=backend, state="starting")
        if backend == "opencode":
            flow.provider = provider_id

        async with self._web_flow_lock:
            self._web_flows[flow_id] = flow

        try:
            if backend == "codex":
                flow.process = await self._start_codex_process(force_reset=force_reset)
                flow.reader_task = asyncio.create_task(self._read_codex_output_web(flow))
                flow.waiter_task = asyncio.create_task(self._wait_for_codex_completion_web(flow))
            elif backend == "claude":
                client, manual_url, attempt = await self._start_claude_control_flow(
                    context=None,
                    force_reset=force_reset,
                    login_with_claude_ai=True,
                )
                flow.claude_client = client
                flow.claude_oauth_attempt = attempt
                flow.url = manual_url
                flow.awaiting_code = True
                flow.state = "awaiting_code"
                flow.waiter_task = asyncio.create_task(self._wait_for_claude_completion_web(flow))
            else:  # opencode
                if not isinstance(provider_id, str) or not provider_id.strip():
                    raise ValueError("opencode_provider_id_required")
                await self._start_opencode_oauth_web(flow, provider_id.strip())
        except Exception as err:  # noqa: BLE001
            logger.error("Web auth start failed for %s: %s", backend, err, exc_info=True)
            if backend == "claude":
                await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            flow.state = "failed"
            flow.error = str(err)
        return flow

    async def submit_web_code(self, flow_id: str, code: str) -> dict[str, Any]:
        flow = self._web_flows.get(flow_id)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        if flow.backend == "opencode":
            return await self._submit_opencode_callback_url(flow, code)
        if flow.backend != "claude":
            return {"ok": False, "error": "code_not_supported"}
        if not flow.awaiting_code or flow.claude_client is None:
            return {"ok": False, "error": "not_awaiting_code"}

        raw = (code or "").strip()
        if "#" not in raw:
            return {"ok": False, "error": "invalid_format"}
        auth_code, state_val = raw.split("#", 1)
        auth_code = auth_code.strip()
        state_val = state_val.strip()
        if not auth_code or not state_val:
            return {"ok": False, "error": "invalid_format"}

        try:
            await self._send_claude_callback(flow.claude_client, auth_code, state_val)
        except Exception as err:  # noqa: BLE001
            logger.error("Web Claude callback submit failed: %s", err, exc_info=True)
            return {"ok": False, "error": "submit_failed", "detail": str(err)}

        flow.awaiting_code = False
        flow.state = "verifying"
        return {"ok": True}

    # Terminal flows (success / failed / cancelled) linger on the
    # registry this long so the polling client can observe the final
    # state once before the flow disappears. Long-lived UI servers
    # would otherwise accumulate one entry per sign-in attempt
    # (process / task refs, captured state) and grow unboundedly.
    _WEB_FLOW_TERMINAL_TTL_SECONDS = 300.0
    _WEB_FLOW_TERMINAL_STATES = frozenset({"success", "failed", "cancelled"})

    def _reap_stale_web_flows(self) -> None:
        """Evict terminal web flows whose retention TTL has expired.

        Runs opportunistically on every status read. The first read
        that observes a terminal state stamps ``terminal_at`` on the
        flow; subsequent reads through the TTL window keep returning
        the same payload (clients may poll twice before noticing
        success). After the TTL the flow is removed and any later
        ``GET /status`` for that ``flow_id`` answers
        ``flow_not_found``.
        """
        now = time.time()
        stale: list[str] = []
        for fid, flow in self._web_flows.items():
            if flow.state not in self._WEB_FLOW_TERMINAL_STATES:
                continue
            if flow.terminal_at is None:
                flow.terminal_at = now
                continue
            if now - flow.terminal_at > self._WEB_FLOW_TERMINAL_TTL_SECONDS:
                stale.append(fid)
        for fid in stale:
            self._web_flows.pop(fid, None)

    def get_web_flow_status(self, flow_id: str) -> dict[str, Any]:
        self._reap_stale_web_flows()
        flow = self._web_flows.get(flow_id)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        # Stamp the terminal timestamp on the first observation so
        # later sweeps can age the flow out. ``_reap_stale_web_flows``
        # also does this — duplicating here keeps the timestamp
        # accurate even if the caller hits status before the sweep
        # walks past this entry.
        if (
            flow.state in self._WEB_FLOW_TERMINAL_STATES
            and flow.terminal_at is None
        ):
            flow.terminal_at = time.time()
        return {
            "ok": True,
            "flow_id": flow_id,
            "backend": flow.backend,
            "state": flow.state,
            "url": flow.url,
            "device_code": flow.device_code,
            "awaiting_code": flow.awaiting_code,
            "error": flow.error,
        }

    async def remove_web_auth(self, backend: str) -> dict[str, Any]:
        """Drop the stored credentials for a Claude/Codex backend.

        Runs the backend's own ``logout`` subcommand so the on-disk state
        is in sync, then clears the V2Config ``api_key`` / ``base_url`` and
        flips ``auth_mode`` back to ``oauth`` so the next "Sign in" click
        starts clean. Idempotent — repeated calls return ``ok: true``.
        """
        # remove_web_auth and test_web_auth are claude / codex specific
        # (single-backend subprocess invocations). OpenCode uses the
        # per-provider DELETE / dedicated probe endpoints elsewhere.
        if backend not in {"claude", "codex"}:
            return {"ok": False, "error": "unsupported_backend"}

        binary = self._get_cli_binary(backend)
        settings_cleanup_error: str | None = None
        if backend == "codex":
            logout_ok, logout_error = await self._run_utility_command(binary, "logout")
        else:
            settings_cleanup_error = await self._clear_claude_settings_env_for_logout()
            if settings_cleanup_error:
                logout_ok = False
                logout_error = settings_cleanup_error
            else:
                logout_ok, logout_error = await self._run_utility_command(
                    binary,
                    "auth",
                    "logout",
                    env=self._build_claude_full_subprocess_env(force_oauth=True),
                )

        try:
            config = getattr(self.controller, "config", None)
            target = getattr(getattr(config, "agents", None), backend, None)
            saver = getattr(config, "save", None) if config is not None else None
            if target is not None and callable(saver):
                try:
                    from config.v2_config import CONFIG_LOCK

                    with CONFIG_LOCK:
                        target.auth_mode = "oauth"
                        target.api_key = None
                        # Drop the relay base_url too: if the user
                        # signed in via OAuth after this, the stored
                        # base_url would still get injected as
                        # ``ANTHROPIC_BASE_URL`` / Codex provider
                        # override, sending OAuth requests to a relay
                        # that only accepts API keys (401).
                        target.base_url = None
                        # Sign out is an explicit user choice — flip
                        # the marker so ``build_claude_subprocess_env``
                        # honors ``auth_mode == "oauth"`` strictly
                        # and strips inherited ``ANTHROPIC_*`` env vars
                        # (Codex-only field; setattr is a no-op for
                        # other backends).
                        if backend == "claude":
                            target.auth_mode_set = True
                        saver()
                except ImportError:
                    target.auth_mode = "oauth"
                    target.api_key = None
                    target.base_url = None
                    if backend == "claude":
                        target.auth_mode_set = True
                    saver()
        except Exception as err:  # noqa: BLE001
            # Disk state has already been cleared; surface the V2Config
            # write failure but report partial success so the UI shows
            # the auth as removed (which is the user-visible truth).
            logger.warning("Failed to clear V2Config after remove for %s: %s", backend, err)

        # Notify the live controller — reuse the same hook path the
        # OAuth-success flow uses, but skip the auth_mode persistence
        # since we just rewrote those fields ourselves.
        hook = self._post_web_success_hook
        if callable(hook):
            try:
                await asyncio.to_thread(hook, backend)
            except Exception as err:  # noqa: BLE001
                logger.warning("post_web_success_hook failed after remove for %s: %s", backend, err)
        # Surface logout failures even though V2Config was cleared. The
        # on-disk credentials may still be intact (e.g. ``codex logout``
        # missing, exited non-zero, or timed out), and the user needs
        # to know about that partial sign-out rather than seeing a
        # green toast and assuming the backend is fully signed out.
        if backend == "claude" and settings_cleanup_error:
            return {
                "ok": True,
                "partial": True,
                "warning": "settings_cleanup_failed",
                "detail": settings_cleanup_error,
            }
        if not logout_ok:
            return {
                "ok": True,
                "partial": True,
                "warning": "logout_failed",
                "detail": logout_error or "logout subprocess exited non-zero",
            }
        return {"ok": True}

    async def test_web_auth(
        self,
        backend: str,
        *,
        timeout: float = 45.0,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send a 1-token probe ("Hi") through the backend CLI.

        Validates both the credentials and the endpoint (when ``base_url``
        is configured) by running ``claude --print "Hi"`` /
        ``codex exec "Hi"``. Returns elapsed milliseconds + a short
        response excerpt on success; surfaces stderr on failure.

        ``model`` overrides the CLI's configured default — useful for
        users whose ``config.toml`` selects a slow reasoning model
        (e.g. ``gpt-5.4`` with ``model_reasoning_effort=xhigh``) where
        even "Hi" can take minutes to round-trip. The frontend's Test
        panel exposes a small select pre-filled from the routing
        catalog so the user can probe a specific model.
        """
        # remove_web_auth and test_web_auth are claude / codex specific
        # (single-backend subprocess invocations). OpenCode uses the
        # per-provider DELETE / dedicated probe endpoints elsewhere.
        if backend not in {"claude", "codex"}:
            return {"ok": False, "error": "unsupported_backend"}

        binary = self._get_cli_binary(backend)
        prompt = "Hi"
        probe_cwd = None
        if backend == "claude":
            probe_cwd = self._resolve_claude_probe_cwd()
            # ``-p`` switches Claude Code into non-interactive print mode
            # and exits after the first complete reply. Deliberately do
            # not pass ``--bare`` here: recent Claude Code builds document
            # that bare mode skips OAuth/keychain reads and only accepts
            # API-key auth. This Settings probe should answer the user's
            # real question: whether the current Avibe Claude setup can
            # run an Agent turn. It follows the normal print-mode launch
            # path used by live Claude sessions.
            cmd = [binary]
            cmd.append("-p")
            if isinstance(model, str) and model.strip():
                cmd.extend(["--model", model.strip()])
            cmd.append(prompt)
            backend_cfg = self._resolve_backend_config("claude")
            auth_mode = getattr(backend_cfg, "auth_mode", None)
            auth_mode_set = bool(getattr(backend_cfg, "auth_mode_set", False))
            if auth_mode == "oauth" and auth_mode_set:
                try:
                    await self._clear_claude_settings_env_for_oauth()
                except Exception as err:  # noqa: BLE001
                    return {"ok": False, "error": "settings_cleanup_failed", "detail": str(err)}
            try:
                env_override = self._build_claude_full_subprocess_env()
            except Exception as err:  # noqa: BLE001
                if auth_mode == "oauth" and auth_mode_set:
                    return {"ok": False, "error": "spawn_failed", "detail": str(err)}
                env_override = dict(os.environ)
        else:
            # Codex single-shot mode. ``--skip-git-repo-check`` bypasses
            # Codex's per-project trust gate. We also force
            # ``model_reasoning_effort=low`` so a config.toml that
            # selects ``xhigh`` reasoning (deep thinking + 30 s+ for any
            # prompt) doesn't blow past our 45 s test timeout — the
            # probe is "auth + endpoint reachable", not "exercise the
            # reasoning chain". ``-c key=value`` overrides config.toml
            # entries per invocation.
            #
            # NOT ``minimal``: OpenAI's Responses API rejects ``minimal``
            # reasoning when ``image_gen`` / ``web_search`` tools are
            # attached (which Codex auto-attaches for chat models with
            # no override flag to disable). The 400 reads ``"The
            # following tools cannot be used with reasoning.effort
            # 'minimal'"``. ``low`` is described in the model catalog
            # as "Fast responses with lighter reasoning" — still fast
            # enough for a probe, compatible with all tool sets.
            cmd = [
                binary,
                "exec",
                "--skip-git-repo-check",
                "-c",
                "model_reasoning_effort=low",
            ]
            if isinstance(model, str) and model.strip():
                cmd.extend(["-c", f"model={model.strip()}"])
            cmd.append(prompt)
            env_override = dict(os.environ)

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                # Close stdin explicitly — Codex's ``exec`` mode reads a
                # second prompt from stdin when the parent's stdin is
                # open (e.g. ``codex exec "Hi" < /dev/null`` works fine,
                # but inheriting an open stdin makes it block forever).
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_override,
                cwd=probe_cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                # Drain whatever the CLI managed to emit before we kill it
                # so we can still classify the failure. Codex in particular
                # retries 401 / network errors several times before exit;
                # the killed-mid-retry case loses the real error code if we
                # just report ``timed_out``. Read with a tight wait so the
                # kill path stays fast.
                process.kill()
                partial_stdout = b""
                partial_stderr = b""
                try:
                    partial_stdout, partial_stderr = await asyncio.wait_for(
                        process.communicate(), timeout=3.0
                    )
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
                stdout_partial = (partial_stdout or b"").decode("utf-8", errors="replace").strip()
                stderr_partial = (partial_stderr or b"").decode("utf-8", errors="replace").strip()
                classified = _classify_test_failure(stdout_partial, stderr_partial)
                result = {
                    "ok": False,
                    "error": classified if classified != "cli_failed" else "timed_out",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
                if stderr_partial or stdout_partial:
                    result["detail"] = (stderr_partial or stdout_partial)[:600]
                return result
        except FileNotFoundError:
            return {"ok": False, "error": "cli_not_found", "detail": binary}
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": "spawn_failed", "detail": str(err)}

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_text = (stdout or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            classified = _classify_test_failure(stdout_text, stderr_text)
            return {
                "ok": False,
                "error": classified,
                "exit_code": process.returncode,
                "detail": (stderr_text or stdout_text)[:600],
                "duration_ms": duration_ms,
            }

        # Pick the model's actual response out of chatty CLI output.
        # Codex in particular prepends a session header, a "codex"
        # label line, "tokens used" footer, ``Reconnecting...`` retry
        # warnings, and bubblewrap notices — the first non-blank line
        # would always be a warning or label. Skip known non-content
        # lines and take the first remaining one as the excerpt.
        excerpt = _pick_probe_response_excerpt(stdout_text)
        return {
            "ok": True,
            "duration_ms": duration_ms,
            "excerpt": excerpt,
        }

    async def test_opencode_provider(
        self,
        provider_id: str,
        *,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Probe a single OpenCode provider via the live ``opencode serve`` HTTP API.

        Unlike Claude / Codex (each have one global credential), OpenCode
        ships with N providers and the user typically configures only a
        few. A single backend-wide test button would either fail when one
        provider is unhealthy or silently mask which provider works — so
        each provider card gets its own probe.

        Flow: create a temp session → ``prompt_async("Hi", model=...)``
        → poll messages until the assistant message either completes
        with text or surfaces an ``info.error`` block → abort the
        session. The session record stays on disk (OpenCode doesn't
        expose ``DELETE /session``), but ``abort`` releases the run
        slot, which is what matters for the probe.

        ``model`` is the model id (e.g. ``"gpt-4o-mini"``); we wrap it
        into the ``{providerID, modelID}`` shape OpenCode expects. When
        ``None``, the probe resolves Avibe's effective Agent default first.
        """
        provider_id = (provider_id or "").strip()
        if not provider_id:
            return {"ok": False, "error": "missing_provider"}

        server = await self._opencode_server()
        if server is None:
            return {"ok": False, "error": "opencode_server_unavailable"}

        # Match normal OpenCode turns: caller override, the selected OpenCode
        # agent's model, Avibe's V2 fallback, then the provider catalog.
        chosen_model = (model or "").strip()
        backend_config = self._resolve_backend_config("opencode")
        default_provider = getattr(backend_config, "default_provider", None)
        if not chosen_model:
            try:
                default_agent = server.get_default_agent_from_config()
                runtime_agent_model = server.get_agent_model_from_config(default_agent)
            except Exception:  # noqa: BLE001
                runtime_agent_model = None
            chosen_model = (
                resolve_opencode_configured_default_model(
                    runtime_agent_model,
                    default_provider=default_provider,
                    provider_id=provider_id,
                )
                or ""
            )
        if not chosen_model and backend_config is not None:
            chosen_model = (
                resolve_opencode_configured_default_model(
                    getattr(backend_config, "default_model", None),
                    default_provider=default_provider,
                    provider_id=provider_id,
                )
                or ""
            )
        if not chosen_model:
            try:
                catalog = await server.get_available_models(os.path.expanduser("~"))
            except Exception:  # noqa: BLE001
                catalog = None
            if isinstance(catalog, dict):
                default_map = catalog.get("default") or {}
                if isinstance(default_map, dict):
                    raw_default = default_map.get(provider_id)
                    if isinstance(raw_default, str) and raw_default.strip():
                        chosen_model = raw_default.strip()
                if not chosen_model:
                    providers = catalog.get("providers") or []
                    if isinstance(providers, list):
                        for entry in providers:
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("id") != provider_id:
                                continue
                            models_field = entry.get("models")
                            if isinstance(models_field, dict):
                                keys = sorted(models_field.keys())
                                if keys:
                                    chosen_model = keys[0]
                            elif isinstance(models_field, list):
                                for m in models_field:
                                    if isinstance(m, dict) and isinstance(m.get("id"), str):
                                        chosen_model = m["id"]
                                        break
                                    if isinstance(m, str):
                                        chosen_model = m
                                        break
                            break
        if not chosen_model:
            return {"ok": False, "error": "no_models_available"}

        # OpenCode picks a workdir per request; the probe doesn't touch
        # files so the home dir is fine and matches what the IM /setup
        # flow uses.
        directory = os.path.expanduser("~")
        started = time.monotonic()
        session_id: str | None = None
        active_registered = False
        try:
            try:
                created = await server.create_session(directory, title="vibe-test-probe")
            except Exception as err:  # noqa: BLE001
                detail = str(err)
                return {
                    "ok": False,
                    "error": _classify_test_failure("", detail),
                    "detail": detail[:600],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            session_id = None
            if isinstance(created, dict):
                info = created.get("info") if isinstance(created.get("info"), dict) else created
                session_id = info.get("id") if isinstance(info, dict) else None
            if not session_id:
                return {
                    "ok": False,
                    "error": "session_create_failed",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }

            baseline_ids: set[str] = set()
            try:
                pre_msgs = await server.list_messages(session_id, directory)
                for msg in pre_msgs:
                    mid = msg.get("info", {}).get("id") if isinstance(msg, dict) else None
                    if mid:
                        baseline_ids.add(mid)
            except Exception:  # noqa: BLE001
                pass

            try:
                catalog = await server.get_available_models(directory)
            except Exception:  # noqa: BLE001
                catalog = None
            model_dict = {"providerID": provider_id, "modelID": chosen_model}
            if isinstance(catalog, dict):
                resolved_model_id = resolve_opencode_model_id(catalog, provider_id, chosen_model)
                if resolved_model_id:
                    chosen_model = resolved_model_id
                    model_dict["modelID"] = resolved_model_id
            reasoning_effort = resolve_opencode_reasoning_effort(
                model_dict,
                None,
                catalog if isinstance(catalog, dict) else None,
            )

            try:
                prompt_started_at = time.time()
                await server.prompt_async(
                    session_id=session_id,
                    directory=directory,
                    text="Hi",
                    model=model_dict,
                    reasoning_effort=reasoning_effort,
                    tools={"question": False},
                )
                await server.mark_run_active(session_id)
                active_registered = True
            except Exception as err:  # noqa: BLE001
                detail = str(err)
                return {
                    "ok": False,
                    "error": _classify_test_failure("", detail),
                    "detail": detail[:600],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }

            # Poll until the assistant message completes (``info.time.completed``
            # is set) or carries an ``info.error`` block — both are
            # terminal. Cap on ``timeout`` so a hung provider never wedges
            # the request.
            deadline = started + timeout
            final_text = ""
            error_payload: dict[str, Any] | None = None
            poll_interval = 1.5
            while time.monotonic() < deadline:
                try:
                    messages = await server.list_messages(session_id, directory)
                except Exception as poll_err:  # noqa: BLE001
                    detail = str(poll_err)
                    return {
                        "ok": False,
                        "error": _classify_test_failure("", detail),
                        "detail": detail[:600],
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                terminal = None
                for msg in messages or []:
                    if not isinstance(msg, dict):
                        continue
                    info = msg.get("info") or {}
                    mid = info.get("id")
                    if not mid or mid in baseline_ids:
                        continue
                    if info.get("role") != "assistant":
                        continue
                    if info.get("error"):
                        terminal = msg
                        error_payload = info.get("error")
                        break
                    completed = (info.get("time") or {}).get("completed")
                    if completed and info.get("finish") != "tool-calls":
                        terminal = msg
                        break
                if terminal is not None:
                    if error_payload:
                        break
                    final_text = extract_opencode_response_text(
                        terminal,
                        allow_non_text_fallback=True,
                    )
                    if not final_text and is_empty_terminal_opencode_message(terminal):
                        lang = self._lang()
                        detail = None
                        try:
                            detail = await server.get_recent_session_error(session_id, since=prompt_started_at)
                        except Exception:  # noqa: BLE001
                            detail = None
                        if not detail:
                            try:
                                detail = await server.get_provider_api_diagnostic(provider_id, chosen_model)
                            except Exception:  # noqa: BLE001
                                detail = None
                        classified = _classify_test_failure("", detail or "")
                        if classified != "cli_failed":
                            return {
                                "ok": False,
                                "error": classified,
                                "detail": (detail or "")[:600],
                                "duration_ms": int((time.monotonic() - started) * 1000),
                                "model": chosen_model,
                            }
                        i18n_key = (
                            "error.opencodeProviderRuntimeError"
                            if detail
                            else "error.opencodeEmptyResponse"
                        )
                        return {
                            "ok": False,
                            "error": "empty_response",
                            "detail": i18n_t(
                                i18n_key,
                                lang,
                                provider=provider_id,
                                model=chosen_model,
                                variant=reasoning_effort or i18n_t("common.default", lang),
                                detail=detail or "",
                            ),
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "model": chosen_model,
                        }
                    break
                try:
                    detail = await server.get_recent_session_error(
                        session_id,
                        since=prompt_started_at,
                    )
                except Exception:  # noqa: BLE001
                    detail = None
                if detail:
                    classified = _classify_test_failure("", detail)
                    if classified in {
                        "invalid_credentials",
                        "forbidden",
                        "model_not_found",
                    }:
                        return {
                            "ok": False,
                            "error": classified,
                            "detail": detail[:600],
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "model": chosen_model,
                        }
                await asyncio.sleep(poll_interval)
            else:
                try:
                    detail = await server.get_recent_session_error(
                        session_id,
                        since=prompt_started_at,
                    )
                except Exception:  # noqa: BLE001
                    detail = None
                if detail:
                    classified = _classify_test_failure("", detail)
                    if classified != "cli_failed":
                        return {
                            "ok": False,
                            "error": classified,
                            "detail": detail[:600],
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "model": chosen_model,
                        }
                return {
                    "ok": False,
                    "error": "timed_out",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "model": chosen_model,
                }

            duration_ms = int((time.monotonic() - started) * 1000)
            if error_payload:
                error_name = ""
                error_msg = ""
                if isinstance(error_payload, dict):
                    error_name = str(error_payload.get("name") or "")
                    data = error_payload.get("data")
                    if isinstance(data, dict):
                        error_msg = str(data.get("message") or "")
                    elif isinstance(data, str):
                        error_msg = data
                blob = f"{error_name} {error_msg}".strip() or "OpenCode reported an error"
                classified = _classify_test_failure("", blob)
                return {
                    "ok": False,
                    "error": classified,
                    "detail": blob[:600],
                    "duration_ms": duration_ms,
                }

            excerpt = _pick_probe_response_excerpt(final_text) if final_text else ""
            return {
                "ok": True,
                "duration_ms": duration_ms,
                "excerpt": excerpt or final_text[:240],
                "model": chosen_model,
            }
        finally:
            if session_id:
                try:
                    await server.abort_session(session_id, directory)
                except Exception:  # noqa: BLE001
                    pass
            if active_registered and session_id:
                await server.mark_run_inactive(session_id)

    async def cancel_web_flow(self, flow_id: str) -> dict[str, Any]:
        async with self._web_flow_lock:
            flow = self._web_flows.pop(flow_id, None)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        await self._terminate_web_flow(flow, final_state="cancelled")
        return {"ok": True}

    async def _opencode_server(self):
        """Lazy lookup of the live OpenCode server client.

        Mirrors ``vibe.api._opencode_get_server`` rather than the IM
        controller's ``agent_service.agents['opencode']`` (the web auth
        service runs in the UI process — there is no controller-level
        agent_service). Returns ``None`` when OpenCode is disabled in
        V2Config so the caller can surface a typed error.
        """
        try:
            from config.v2_compat import to_app_config
            from config.v2_config import V2Config
            from core.resource_governance import AgentResourceGovernor, config_from_runtime
            from modules.agents.opencode import OpenCodeServerManager
        except ImportError:
            return None
        try:
            v2_config = V2Config.load()
            compat = to_app_config(v2_config)
        except Exception as err:  # noqa: BLE001
            logger.warning("V2Config.load failed in web OAuth path: %s", err)
            return None
        opencode_cfg = getattr(compat, "opencode", None)
        if opencode_cfg is None:
            return None
        try:
            server = await OpenCodeServerManager.get_instance(
                binary=opencode_cfg.binary,
                port=opencode_cfg.port,
                request_timeout_seconds=opencode_cfg.request_timeout_seconds,
                resource_governor=AgentResourceGovernor(config_from_runtime(v2_config)),
            )
            await server.ensure_running()
            return server
        except Exception as err:  # noqa: BLE001
            logger.warning("OpenCodeServerManager.get_instance failed for web OAuth: %s", err)
            return None

    async def _resolve_opencode_oauth_method(
        self, server, provider_id: str
    ) -> tuple[int, dict[str, Any]]:
        """Pick the friendliest OAuth method index for a provider.

        OpenCode's ``/provider/auth`` exposes an ordered list of methods
        per provider. We prefer the **last** ``oauth`` entry — for OpenAI
        that's the headless device-auth flow (better for remote sessions
        than the localhost-callback browser flow). For providers with a
        single oauth method (github-copilot, gitlab, poe) it returns
        that one. Errors fall back to method 0.
        """
        prompt_answers = self._OPENCODE_OAUTH_PROMPT_ANSWERS.get(provider_id, {})
        try:
            auth_map = await server.get_provider_auth()
        except Exception:  # noqa: BLE001
            return 0, prompt_answers
        methods = auth_map.get(provider_id) if isinstance(auth_map, dict) else None
        if not isinstance(methods, list):
            return 0, prompt_answers
        # Walk in reverse so the last oauth method wins. For OpenAI this
        # picks "ChatGPT Pro/Plus (headless)" over the browser variant.
        chosen = 0
        for idx in range(len(methods) - 1, -1, -1):
            entry = methods[idx]
            if isinstance(entry, dict) and entry.get("type") == "oauth":
                chosen = idx
                break
        return chosen, prompt_answers

    # Matches OpenCode's ``"Enter code: AB1C-D2E3"`` instructions line so
    # we can surface device codes inline rather than asking the user to
    # squint at a copy-pasted URL.
    _OPENCODE_DEVICE_CODE_RE = re.compile(r"Enter code:\s*([A-Za-z0-9-]+)")

    async def _start_opencode_oauth_web(self, flow: WebAuthFlow, provider_id: str) -> None:
        server = await self._opencode_server()
        if server is None:
            raise RuntimeError("opencode_server_unavailable")
        method_index, prompt_answers = await self._resolve_opencode_oauth_method(
            server, provider_id
        )
        flow.last_status_text = None
        authorize = await server.start_provider_oauth(
            provider_id,
            method=method_index,
            prompt_answers=prompt_answers,
        )
        url = authorize.get("url") if isinstance(authorize, dict) else None
        instructions = authorize.get("instructions") if isinstance(authorize, dict) else None
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("opencode_authorize_missing_url")
        flow.url = url.strip()
        if isinstance(instructions, str):
            match = self._OPENCODE_DEVICE_CODE_RE.search(instructions)
            if match:
                flow.device_code = match.group(1)
            flow.last_status_text = instructions
        flow.state = "awaiting_code"
        # OpenCode auto-detects completion (device poll or local callback);
        # no user-submitted code to enter. The waiter long-polls the
        # daemon's /callback endpoint and flips ``state`` when done.
        flow.awaiting_code = False
        flow.waiter_task = asyncio.create_task(
            self._wait_for_opencode_oauth_web(
                flow,
                provider_id,
                method_index,
                prompt_answers,
            )
        )

    async def _submit_opencode_callback_url(self, flow: WebAuthFlow, code: str) -> dict[str, Any]:
        """Forward a manually-pasted 127.0.0.1 callback URL to OpenCode.

        Browser-redirect flows (poe, gitlab, openai-browser) leave the
        user staring at ``http://127.0.0.1:<port>/callback?code=...`` in
        their address bar — but that loopback belongs to the container,
        not their machine, so the redirect can't complete on its own.
        The Settings UI exposes a paste box; we GET the URL from inside
        the container so OpenCode's own listener consumes it.
        """
        raw = (code or "").strip()
        if not raw:
            return {"ok": False, "error": "invalid_callback_url"}
        # Browsers strip the ``http://`` prefix when the user copies
        # from the address bar in some setups, so accept the bare
        # ``127.0.0.1:port/...`` shape too — re-add the scheme before
        # validating so the rest of the flow sees a normal URL.
        lowered = raw.lower()
        if not lowered.startswith(("http://", "https://")):
            raw = "http://" + raw.lstrip("/")
            lowered = raw.lower()
        if not (
            lowered.startswith("http://127.0.0.1")
            or lowered.startswith("http://localhost")
        ):
            return {"ok": False, "error": "invalid_callback_url"}
        callback_url = raw
        provider_id = flow.provider
        if not provider_id:
            return {"ok": False, "error": "flow_missing_provider"}
        server = await self._opencode_server()
        if server is None:
            return {"ok": False, "error": "opencode_server_unavailable"}
        try:
            await server.forward_oauth_redirect(provider_id, callback_url)
        except Exception as err:  # noqa: BLE001
            logger.error(
                "Failed to forward OpenCode OAuth callback for %s: %s",
                provider_id, err, exc_info=True,
            )
            return {"ok": False, "error": "forward_failed", "detail": str(err)}
        # The blocking ``wait_provider_oauth`` task observes completion
        # on its own — the flow's state will flip to ``verifying`` then
        # ``success`` within seconds.
        return {"ok": True}

    async def _wait_for_opencode_oauth_web(
        self,
        flow: WebAuthFlow,
        provider_id: str,
        method_index: int,
        prompt_answers: dict[str, Any],
    ) -> None:
        try:
            server = await self._opencode_server()
            if server is None:
                raise RuntimeError("opencode_server_unavailable")
            await server.wait_provider_oauth(
                provider_id,
                method=method_index,
                prompt_answers=prompt_answers,
                timeout=self.setup_timeout_seconds,
            )
            flow.state = "verifying"
            # OpenCode persists into auth.json itself. Clear any Vibe-managed
            # provider option key so the new OAuth entry becomes the effective
            # credential, then ping the controller-refresh hook so the running
            # OpenCode agent (and the Settings page on the next poll) sees the
            # new auth state.
            await self._clear_opencode_provider_options_key_for_oauth(provider_id)
            await self._invoke_post_web_success_hook(flow.backend)
            flow.state = "success"
        except asyncio.TimeoutError:
            flow.state = "failed"
            flow.error = "timed_out"
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.error(
                "Web OpenCode OAuth flow failed for %s: %s", provider_id, err, exc_info=True
            )
            flow.state = "failed"
            flow.error = str(err)

    async def _read_codex_output_web(self, flow: WebAuthFlow) -> None:
        """Parse ``codex login --device-auth`` stdout for URL + device code.

        Mirrors ``_read_codex_output`` but writes the parsed fields onto the
        ``WebAuthFlow`` instead of pushing an IM message.
        """
        process = flow.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                clean = sanitize_process_output(text)
                if not clean:
                    continue
                flow.last_status_text = clean
                maybe_url = CODEX_URL_RE.search(clean)
                if maybe_url:
                    flow.url = maybe_url.group(0)
                maybe_code = CODEX_DEVICE_CODE_RE.search(clean)
                if maybe_code:
                    flow.device_code = maybe_code.group(0)
                if flow.url and flow.device_code and flow.state == "starting":
                    flow.state = "awaiting_code"
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("Codex web reader stopped: %s", err)

    async def _wait_for_codex_completion_web(self, flow: WebAuthFlow) -> None:
        try:
            assert flow.process is not None
            await asyncio.wait_for(flow.process.wait(), timeout=self.setup_timeout_seconds)
            if flow.reader_task and not flow.reader_task.done():
                try:
                    await flow.reader_task
                except asyncio.CancelledError:
                    pass
            # If the login process emitted an explicit error line before
            # exiting (e.g. ``Error logging in with device code: device
            # code request failed with status 403 Forbidden`` when
            # ``auth.openai.com`` rate-limits or blocks the request),
            # surface THAT instead of falling through to
            # ``_verify_web_login`` — the verify probe runs ``codex login
            # status``, which reports "Not logged in" after every failed
            # login attempt, masking the real cause. The reader writes
            # each non-empty stdout line into ``last_status_text``;
            # checking for the ``error``-prefix on that line catches the
            # device-flow failure modes Codex CLI emits before exit.
            last_line = (flow.last_status_text or "").strip()
            if last_line:
                lowered = last_line.lower()
                if lowered.startswith("error") or "failed with status" in lowered:
                    flow.state = "failed"
                    flow.error = last_line[:400]
                    return
            flow.state = "verifying"
            ok, detail = await self._verify_web_login(flow.backend, force_oauth=flow.backend == "claude")
            if ok:
                await self._invoke_post_web_success_hook(flow.backend)
                await self._refresh_backend_runtime(flow.backend)
                flow.state = "success"
            else:
                flow.state = "failed"
                flow.error = detail or "unknown_failure"
        except asyncio.TimeoutError:
            await self._terminate_web_flow(flow, final_state="failed", error="timed_out")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.error("Web Codex auth flow failed: %s", err, exc_info=True)
            flow.state = "failed"
            flow.error = str(err)

    async def _wait_for_claude_completion_web(self, flow: WebAuthFlow) -> None:
        try:
            if flow.claude_client is None:
                raise RuntimeError("missing_sdk_client")
            await asyncio.wait_for(
                self._send_claude_control_request(
                    flow.claude_client,
                    {"subtype": "claude_oauth_wait_for_completion"},
                    timeout=self.setup_timeout_seconds,
                ),
                timeout=self.setup_timeout_seconds,
            )
            flow.state = "verifying"
            ok, detail = await self._verify_web_login(
                flow.backend,
                force_oauth=True,
                claude_oauth_attempt=flow.claude_oauth_attempt,
            )
            if ok:
                await self._invoke_post_web_success_hook(flow.backend)
                await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=True)
                await self._refresh_backend_runtime(flow.backend)
                flow.state = "success"
            else:
                await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
                flow.state = "failed"
                flow.error = detail or "unknown_failure"
        except asyncio.TimeoutError:
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            flow.state = "failed"
            flow.error = "timed_out"
        except asyncio.CancelledError:
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            raise
        except Exception as err:  # noqa: BLE001
            await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
            logger.error("Web Claude auth flow failed: %s", err, exc_info=True)
            flow.state = "failed"
            flow.error = str(err)
        finally:
            if flow.claude_client is not None:
                await self._disconnect_claude_client(flow.claude_client)
                flow.claude_client = None

    async def _verify_web_login(
        self,
        backend: str,
        *,
        force_oauth: bool = False,
        claude_oauth_attempt: ClaudeOAuthAttempt | None = None,
    ) -> tuple[bool, str]:
        """Re-run the same CLI status probes ``_verify_login`` uses for IM.

        Builds a temporary IM-shaped ``AgentAuthFlow`` shell so the existing
        verifier can run unchanged — the only fields it touches are
        ``backend`` and the controller binary lookup, so the placeholder
        process / context / tasks are safe to leave as ``None``-ish stubs.
        """
        dummy = AgentAuthFlow(
            flow_id="web-verify",
            backend=backend,
            settings_key="web",
            initiator_user_id="web",
            context=None,  # type: ignore[arg-type]
            process=None,
            reader_task=asyncio.create_task(asyncio.sleep(0)),
            waiter_task=asyncio.create_task(asyncio.sleep(0)),
        )
        dummy.force_oauth = force_oauth
        dummy.claude_oauth_attempt = claude_oauth_attempt
        try:
            return await self._verify_login(dummy)
        finally:
            for task in (dummy.reader_task, dummy.waiter_task):
                if task and not task.done():
                    task.cancel()

    async def _invoke_post_web_success_hook(self, backend: str) -> None:
        # OAuth completed via the web UI implies the user wants
        # ``auth_mode = "oauth"``. Persist it before the controller-refresh
        # hook fires so the live agent reloads with the right mode rather
        # than waiting for the user to click an extra Save button. The
        # persist helper also removes backend-specific API-key state when
        # OAuth is now the active mode, keeping the two auth sources
        # mutually exclusive on disk instead of relying on CLI logout side
        # effects.
        #
        # Skipped for opencode: ``OpenCodeConfig`` has no ``auth_mode``
        # field (auth is per-provider, not global), so persisting one
        # would add a stray attribute and could mislead future readers.
        if backend != "opencode":
            await self._persist_backend_auth_mode(backend, "oauth")
        hook = self._post_web_success_hook
        if not callable(hook):
            return
        try:
            await asyncio.to_thread(hook, backend)
        except Exception as err:  # noqa: BLE001
            logger.warning("post_web_success_hook failed for %s: %s", backend, err)

    async def _clear_claude_settings_env_for_oauth(self) -> None:
        try:
            from vibe.claude_config import apply_claude_auth

            await asyncio.to_thread(
                apply_claude_auth,
                auth_mode="oauth",
                api_key=None,
                base_url=None,
            )
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "Failed to clear Claude Code settings env after OAuth flow; "
                "stale ANTHROPIC_* values may still override OAuth."
            ) from err

    async def _clear_codex_api_key_for_oauth(self) -> None:
        try:
            from vibe.codex_config import apply_codex_auth

            await asyncio.to_thread(
                apply_codex_auth,
                auth_mode="oauth",
                api_key=None,
                base_url=None,
            )
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "Failed to clear Codex API-key state after OAuth flow; "
                "stale OPENAI_API_KEY or base_url values may still override OAuth."
            ) from err

    async def _read_pending_claude_oauth_settings_backup(
        self,
    ) -> dict[str, str] | None:
        try:
            from vibe.claude_config import read_claude_oauth_settings_backup

            return await asyncio.to_thread(read_claude_oauth_settings_backup)
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to read pending Claude OAuth settings backup: %s", err)
            return None

    def _recover_interrupted_claude_oauth_settings_backup(self) -> None:
        try:
            from vibe.claude_config import (
                clear_claude_oauth_settings_backup,
                read_claude_oauth_settings_backup,
                restore_claude_settings_env,
            )

            backup = read_claude_oauth_settings_backup()
            if not backup:
                return
            restore_claude_settings_env(backup)
            clear_claude_oauth_settings_backup()
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to recover interrupted Claude OAuth settings backup: %s", err)

    async def _read_rollback_claude_oauth_settings_backup(
        self,
    ) -> dict[str, str] | None:
        return await self._read_pending_claude_oauth_settings_backup()

    async def _write_pending_claude_oauth_settings_backup(
        self,
        settings_backup: dict[str, str] | None,
    ) -> bool:
        if not settings_backup:
            return False
        try:
            from vibe.claude_config import write_claude_oauth_settings_backup

            await asyncio.to_thread(write_claude_oauth_settings_backup, settings_backup)
            return True
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "Failed to persist Claude Code settings backup before OAuth flow; "
                "existing API-key settings were not changed."
            ) from err

    async def _clear_pending_claude_oauth_settings_backup(self) -> None:
        try:
            from vibe.claude_config import clear_claude_oauth_settings_backup

            await asyncio.to_thread(clear_claude_oauth_settings_backup)
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to clear pending Claude OAuth settings backup: %s", err)

    async def _temporarily_clear_claude_settings_env_for_oauth(
        self,
        *,
        persist_backup: bool = False,
        existing_backup: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        try:
            from vibe.claude_config import read_claude_settings_env

            settings_backup = await asyncio.to_thread(read_claude_settings_env)
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "Failed to read Claude Code settings env before OAuth flow; "
                "stale ANTHROPIC_* values may still override OAuth."
            ) from err
        effective_backup = existing_backup or settings_backup or None
        if persist_backup and settings_backup and not existing_backup:
            await self._write_pending_claude_oauth_settings_backup(settings_backup)
        await self._clear_claude_settings_env_for_oauth()
        return effective_backup

    async def _restore_claude_settings_env_after_oauth_failure(
        self, settings_backup: dict[str, str] | None
    ) -> bool:
        if not settings_backup:
            return True
        try:
            from vibe.claude_config import restore_claude_settings_env

            await asyncio.to_thread(
                restore_claude_settings_env,
                settings_backup,
            )
            return True
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to restore Claude settings env after OAuth failure: %s", err)
            return False

    async def _restore_transient_claude_oauth_probe_backup(
        self,
        settings_backup: dict[str, str] | None,
    ) -> None:
        if not settings_backup:
            return
        async with self._claude_oauth_lock:
            if self._claude_oauth_batch is not None:
                return
            await self._restore_claude_settings_env_after_oauth_failure(settings_backup)

    async def _begin_claude_oauth_attempt(self) -> ClaudeOAuthAttempt:
        async with self._claude_oauth_lock:
            batch = self._claude_oauth_batch
            new_batch = batch is None or batch.committed or not batch.attempts
            existing_backup = None
            if new_batch:
                existing_backup = await self._read_rollback_claude_oauth_settings_backup()
            settings_backup = await self._temporarily_clear_claude_settings_env_for_oauth(
                persist_backup=new_batch,
                existing_backup=existing_backup,
            )
            if new_batch:
                batch = ClaudeOAuthBatch(
                    backup=dict(settings_backup or {}) or None,
                    durable_backup=bool(existing_backup or settings_backup),
                )
                self._claude_oauth_batch = batch
            elif batch.backup is None and settings_backup:
                batch.backup = dict(settings_backup)

            self._claude_oauth_attempt_counter += 1
            attempt = ClaudeOAuthAttempt(
                attempt_id=self._claude_oauth_attempt_counter,
                batch=batch,
            )
            batch.attempts[attempt.attempt_id] = attempt
            return attempt

    async def _prepare_claude_oauth_probe(
        self,
        attempt: ClaudeOAuthAttempt | None,
    ) -> dict[str, str] | None:
        async with self._claude_oauth_lock:
            settings_backup = await self._temporarily_clear_claude_settings_env_for_oauth()
            if attempt is None:
                return settings_backup
            if attempt.batch.backup is None and settings_backup:
                attempt.batch.backup = dict(settings_backup)
            return attempt.settings_backup

    async def _finish_claude_oauth_attempt(
        self,
        attempt: ClaudeOAuthAttempt | None,
        *,
        succeeded: bool,
    ) -> None:
        async with self._claude_oauth_lock:
            if attempt is None or not attempt.active:
                return

            attempt.active = False
            attempt.succeeded = succeeded
            batch = attempt.batch
            batch.attempts.pop(attempt.attempt_id, None)
            if succeeded:
                had_durable_backup = batch.durable_backup
                batch.committed = True
                batch.backup = None
                batch.durable_backup = False
                if self._claude_oauth_batch is batch and not batch.attempts:
                    self._claude_oauth_batch = None
                if had_durable_backup:
                    await self._clear_pending_claude_oauth_settings_backup()
                return

            if not batch.attempts:
                backup = batch.backup
                had_durable_backup = batch.durable_backup
                should_restore = not batch.committed
                batch.backup = None
                batch.durable_backup = False
                if self._claude_oauth_batch is batch:
                    self._claude_oauth_batch = None
                restore_ok = True
                if should_restore:
                    restore_ok = await self._restore_claude_settings_env_after_oauth_failure(backup)
                if restore_ok and had_durable_backup:
                    await self._clear_pending_claude_oauth_settings_backup()

    async def _clear_claude_settings_env_for_logout(self) -> str | None:
        try:
            await self._clear_claude_settings_env_for_oauth()
        except RuntimeError as err:
            logger.warning("Failed to clear Claude settings env during logout: %s", err)
            return str(err)
        return None

    async def clear_claude_oauth_credentials_only(self) -> dict[str, Any]:
        """Remove Claude Code account tokens without changing API-key mode.

        Claude Code reapplies ``settings.json`` env values at startup. To make
        ``claude auth logout`` target stored account credentials instead of the
        just-saved API key, temporarily clear Anthropic env overrides, run the
        logout command, then restore the API-key settings exactly.
        """
        async with self._claude_oauth_lock:
            from vibe.claude_config import (
                clear_claude_oauth_credentials_files,
                read_claude_settings_env,
            )

            try:
                settings_backup = await asyncio.to_thread(read_claude_settings_env)
            except Exception as err:  # noqa: BLE001
                detail = (
                    "Failed to read Claude Code settings before clearing OAuth "
                    f"credentials: {err}"
                )
                logger.warning(detail)
                return {
                    "ok": True,
                    "partial": True,
                    "warning": "oauth_cleanup_failed",
                    "detail": detail,
                }

            settings_cleanup_error = await self._clear_claude_settings_env_for_logout()
            logout_ok = False
            logout_error = None
            if not settings_cleanup_error:
                logout_env = self._build_claude_full_subprocess_env(force_oauth=True)
                logout_ok, logout_error = await self._run_utility_command(
                    self._get_cli_binary("claude"),
                    "auth",
                    "logout",
                    env=logout_env,
                )
                try:
                    await asyncio.to_thread(clear_claude_oauth_credentials_files)
                except Exception as err:  # noqa: BLE001
                    logout_ok = False
                    logout_error = str(err)
            restore_ok = await self._restore_claude_settings_env_after_oauth_failure(
                settings_backup or None
            )

            if settings_cleanup_error:
                return {
                    "ok": True,
                    "partial": True,
                    "warning": "oauth_cleanup_failed",
                    "detail": settings_cleanup_error,
                }
            if not restore_ok:
                return {
                    "ok": True,
                    "partial": True,
                    "warning": "oauth_cleanup_failed",
                    "detail": "Claude API-key settings were saved, but Avibe could not restore them after the OAuth cleanup probe.",
                }
            if not logout_ok:
                return {
                    "ok": True,
                    "partial": True,
                    "warning": "oauth_cleanup_failed",
                    "detail": logout_error or "claude auth logout exited non-zero",
                }
            return {"ok": True}

    async def clear_claude_oauth_for_api_key_mode(self) -> dict[str, Any]:
        """Backward-compatible name for API-key save cleanup."""
        return await self.clear_claude_oauth_credentials_only()

    async def _persist_backend_auth_mode(self, backend: str, auth_mode: str) -> None:
        """Persist V2Config.agents.<backend>.auth_mode for web and IM flows."""
        if backend == "claude" and auth_mode == "oauth":
            await self._clear_claude_settings_env_for_oauth()
        if backend == "codex" and auth_mode == "oauth":
            await self._clear_codex_api_key_for_oauth()
        try:
            config = getattr(self.controller, "config", None)
            target = getattr(getattr(config, "agents", None), backend, None)
            saver = getattr(config, "save", None) if config is not None else None
            loaded_config = None
            if target is None or not callable(saver):
                from config.v2_config import V2Config

                loaded_config = V2Config.load()
                target = getattr(getattr(loaded_config, "agents", None), backend, None)
                saver = getattr(loaded_config, "save", None)
            if target is None or not callable(saver):
                return
            # An explicit OAuth save must also flip ``auth_mode_set``
            # for Claude — otherwise a successful OAuth flow on a
            # legacy install never trips the marker (auth_mode was
            # already "oauth" from the schema default), so
            # ``build_claude_subprocess_env`` keeps preserving env-var
            # auth and the OAuth credentials are ignored at launch.
            needs_mode_write = getattr(target, "auth_mode", None) != auth_mode
            needs_marker_write = (
                backend == "claude"
                and not bool(getattr(target, "auth_mode_set", False))
            )
            needs_codex_oauth_cleanup = (
                backend == "codex"
                and auth_mode == "oauth"
                and (
                    bool(getattr(target, "api_key", None))
                    or bool(getattr(target, "base_url", None))
                )
            )
            if not needs_mode_write and not needs_marker_write and not needs_codex_oauth_cleanup:
                return
            try:
                from config.v2_config import CONFIG_LOCK

                with CONFIG_LOCK:
                    if needs_mode_write:
                        target.auth_mode = auth_mode
                    if needs_marker_write:
                        target.auth_mode_set = True
                    if needs_codex_oauth_cleanup:
                        target.api_key = None
                        target.base_url = None
                    saver()
            except ImportError:
                if needs_mode_write:
                    target.auth_mode = auth_mode
                if needs_marker_write:
                    target.auth_mode_set = True
                if needs_codex_oauth_cleanup:
                    target.api_key = None
                    target.base_url = None
                saver()
            if loaded_config is not None and config is not None:
                compat_target = getattr(config, backend, None)
                if compat_target is not None:
                    if needs_mode_write:
                        setattr(compat_target, "auth_mode", auth_mode)
                    if needs_marker_write:
                        setattr(compat_target, "auth_mode_set", True)
                    if needs_codex_oauth_cleanup:
                        setattr(compat_target, "api_key", None)
                        setattr(compat_target, "base_url", None)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "Failed to persist auth_mode=%s after web flow for %s: %s",
                auth_mode, backend, err,
            )

    async def _terminate_web_flow(
        self,
        flow: WebAuthFlow,
        *,
        final_state: WebFlowState,
        error: str | None = None,
    ) -> None:
        await self._finish_claude_oauth_attempt(flow.claude_oauth_attempt, succeeded=False)
        if flow.reader_task and not flow.reader_task.done():
            flow.reader_task.cancel()
            try:
                await flow.reader_task
            except asyncio.CancelledError:
                pass
        if flow.waiter_task and not flow.waiter_task.done():
            flow.waiter_task.cancel()
            try:
                await flow.waiter_task
            except asyncio.CancelledError:
                pass
        if flow.process is not None and flow.process.returncode is None:
            try:
                flow.process.terminate()
                await asyncio.wait_for(flow.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                flow.process.kill()
                await flow.process.wait()
            except ProcessLookupError:
                pass
        if flow.claude_client is not None:
            await self._disconnect_claude_client(flow.claude_client)
            flow.claude_client = None
        flow.state = final_state
        if error:
            flow.error = error
