"""Antigravity vendor auth evidence, env construction, and ACP auth/connection probes.

Never passes ``--dangerously-skip-permissions``, never writes to ``~/.gemini`` or
shell profiles, and never logs or returns existing session listings during passive
status reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from modules.agents import acp
from modules.agents.antigravity.models import (
    AntigravityModelSelectionError,
    apply_model_and_effort,
    auth_config_fingerprint,
    extract_live_model_catalog_entries,
    has_model_config_option,
    record_antigravity_live_models,
    validate_model_selection_receipt,
)

logger = logging.getLogger(__name__)

ANTIGRAVITY_AUTH_MODES = ("oauth-personal", "oauth-business", "gemini-api-key", "agent-platform")
ANTIGRAVITY_OAUTH_METHODS = frozenset({"oauth-personal", "oauth-business"})
ANTIGRAVITY_ADC_LOGIN_COMMAND = "gcloud auth application-default login"
ANTIGRAVITY_CODE_PROMPT_RE = re.compile(
    r"(?:enter|paste|submit|input)\b.{0,40}\b(?:code|token|url|callback)\b", re.IGNORECASE
)

_ACP_CLIENT_INFO: dict[str, str] = {"name": "avibe", "version": "1.0"}
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_OAUTH_URL_TERMINATED_RE = re.compile(r"https?://[^\s<>\"'\)\]\x00-\x1f\x7f]+(?=[\s<>\"'\)\]\x00-\x1f\x7f])")
_OAUTH_URL_EOF_RE = re.compile(r"https?://[^\s<>\"'\)\]\x00-\x1f\x7f]+")
_ACP_AUTH_CODE_RE = re.compile(r"(?<![\w/.-])-32000(?=$|[\s,;:)}\]\"'])")
_HTTP_401_RE = re.compile(
    r"""(?<![\w/.-])(?:http(?:/\d(?:\.\d)?|error)?|status(?:[_ ]?code)?"""
    r"""|(?:api )?error(?: code)?|failed to (?:send message|start async prompt))"""
    r"""["']?\s*[:=]?\s*["']?401(?=$|[\s,;:)}\]"'])"""
)
_HTTP_401_CLIENT_RE = re.compile(r"(?<![\w/.-])401\s+client error\b")
_AUTH_ERROR_NEEDLES = (
    "authentication required", "authentication", "unauthorized", "not logged in",
    "login required", "logged out", "invalid api key", "api key not valid",
    "invalid credentials", "credentials expired", "token expired", "re-auth",
    "re-authenticate", "application default credentials",
    "could not find default credentials", "oauth", "verification required",
)
_VERIFICATION_REQUIRED_BANNER_RE = re.compile(
    r"^(?:\*{1,2}\s*)?⚠\ufe0f?\s*verification\s+required\b",
    re.IGNORECASE,
)
_VERIFICATION_REQUIRED_PLAIN_RE = re.compile(
    r"^(?:\*{1,2}\s*)?verification\s+required(?:\s*(?:\*{1,2})?\s*(?:[:\-\n]|$))",
    re.IGNORECASE,
)
_VERIFICATION_REQUIRED_CUES = (
    "verify",
    "google",
    "account",
    "agy",
    "http://",
    "https://",
    "eligibility",
    "sign in",
    "log in",
)


_ANTIGRAVITY_AUTH_MODE_ALIASES = {
    "api_key": "gemini-api-key",
    "agent_platform": "agent-platform",
    "adc": "agent-platform",
}


def normalize_antigravity_auth_mode(config: Any | None = None, auth_mode: str | None = None) -> str:
    """Resolve and validate an Antigravity auth mode, defaulting to ``oauth-personal``."""
    raw = (auth_mode or getattr(config, "auth_mode", None) or "oauth-personal").strip().lower()
    mode = _ANTIGRAVITY_AUTH_MODE_ALIASES.get(raw, raw)
    return mode if mode in ANTIGRAVITY_AUTH_MODES else "oauth-personal"


def _is_explicit_cli_path(value: str) -> bool:
    return bool(value) and (
        value.startswith("~")
        or os.path.isabs(value)
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _normalize_explicit_cli_path(value: str) -> str:
    return str(Path(os.path.expanduser(value)).absolute())


def resolve_antigravity_cli_path(raw_cli_path: str | None) -> str:
    """Resolve an Antigravity CLI selector without invoking ``npm``.

    On desktop-managed runtimes, delegates to ``runtime_agent_cli_path`` so
    bare selectors and stale desktop paths resolve to the published private
    desktop install (or user-installed ``agy`` outside the GUI PATH). Outside
    desktop runtimes, bare selectors (no path separator) are still resolved via
    ``resolve_cli_path(..., include_npm_global=False)``. Explicit paths (tilde,
    absolute, or containing a path separator) are normalized to expanded
    absolute paths so subprocess execution never passes raw ``~`` or
    cwd-dependent relative paths (while preserving ``cli_not_found`` reporting
    for missing explicit paths).
    """
    selected = str(raw_cli_path).strip() if raw_cli_path is not None else ""
    selected = selected or "agy"
    from config.v2_compat import runtime_agent_cli_path
    from vibe.cli_paths import resolve_cli_path
    from vibe.upgrade import is_desktop_managed_runtime

    if is_desktop_managed_runtime():
        resolved = runtime_agent_cli_path(selected, "agy", resolve_agent_paths=True)
        if _is_explicit_cli_path(resolved):
            return _normalize_explicit_cli_path(resolved)
        return resolved
    if not _is_explicit_cli_path(selected):
        resolved = resolve_cli_path(selected, include_npm_global=False) or selected
        if _is_explicit_cli_path(resolved):
            return _normalize_explicit_cli_path(resolved)
        return resolved
    return _normalize_explicit_cli_path(selected)


def antigravity_acp_command(binary: str, *, login: bool = False) -> list[str]:
    """Build argv for Antigravity auth/status/logout/probe ACP calls (never permission bypass)."""
    return [binary, "acp", *(["--login"] if login else []), "--disable-telemetry"]


def extract_antigravity_oauth_url(raw_buffer: str, *, eof: bool = False) -> str | None:
    """Extract a complete OAuth URL from accumulated PTY output.

    Waits for whitespace, newline, quote, closing bracket, control/ANSI boundary,
    or ``eof=True`` so URLs split across PTY reads are never published truncated.
    """
    if not raw_buffer:
        return None
    normalized = _ANSI_ESCAPE_RE.sub(" ", raw_buffer)
    match = (_OAUTH_URL_EOF_RE if eof else _OAUTH_URL_TERMINATED_RE).search(normalized)
    return (match.group(0).rstrip(".,;:") or None) if match else None


def detect_antigravity_adc(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read-only check for Google Application Default Credentials (ADC)."""
    source_env = os.environ if env is None else env
    explicit = (source_env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if explicit:
        explicit_path = os.path.abspath(os.path.expanduser(explicit))
        if os.path.isfile(explicit_path):
            return {"present": True, "source": "env", "path": explicit_path, "login_command": None}

    base_dir = (
        os.path.join(source_env["APPDATA"], "gcloud")
        if os.name == "nt" and source_env.get("APPDATA")
        else os.path.join(source_env.get("HOME") or os.path.expanduser("~"), ".config", "gcloud")
    )
    default_path = os.path.join(base_dir, "application_default_credentials.json")
    if os.path.isfile(default_path):
        return {"present": True, "source": "gcloud_default", "path": default_path, "login_command": None}
    return {"present": False, "source": None, "path": None, "login_command": ANTIGRAVITY_ADC_LOGIN_COMMAND}


def build_antigravity_auth_env(
    config: Any | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
    auth_mode: str | None = None,
) -> dict[str, str]:
    """Build subprocess env for Antigravity ACP auth/status/logout/probe calls."""
    env = dict(os.environ if base_env is None else base_env)
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    mode = normalize_antigravity_auth_mode(config, auth_mode)

    if mode == "gemini-api-key":
        for attr, key in (("api_key", "GEMINI_API_KEY"), ("base_url", "GOOGLE_GEMINI_BASE_URL")):
            val = (getattr(config, attr, None) or "").strip()
            if val:
                env[key] = val
            else:
                env.pop(key, None)
        return env

    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_GEMINI_BASE_URL", None)
    if mode == "agent-platform":
        for attr, key in (("gcp_project", "GOOGLE_CLOUD_PROJECT"), ("gcp_location", "GOOGLE_CLOUD_LOCATION")):
            if val := (getattr(config, attr, None) or "").strip():
                env[key] = val
    return env


def extract_acp_live_models(session_payload: Mapping[str, Any]) -> list[str]:
    """Extract live model ids advertised by an ACP ``session/new`` response."""
    return [entry["id"] for entry in extract_live_model_catalog_entries(session_payload)]


def classify_antigravity_auth_error(error_text: str) -> bool:
    """Return True when an Antigravity error indicates missing or invalid auth."""
    text = (error_text or "").strip().lower()
    if not text:
        return False
    if text == "401" or _HTTP_401_RE.search(text) or _HTTP_401_CLIENT_RE.search(text):
        return True
    return bool(_ACP_AUTH_CODE_RE.search(text)) or any(needle in text for needle in _AUTH_ERROR_NEEDLES)


def is_antigravity_verification_required_reply(text: str) -> bool:
    """Return True when an Antigravity reply is a Google account verification notice."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _VERIFICATION_REQUIRED_BANNER_RE.match(stripped):
        return True
    if _VERIFICATION_REQUIRED_PLAIN_RE.match(stripped):
        if len(stripped) <= 32:
            return True
        lowered = stripped.lower()
        return any(cue in lowered for cue in _VERIFICATION_REQUIRED_CUES)
    return False


async def authenticate_antigravity_client(client: acp.AcpClient, auth_mode: str | None) -> str:
    """Run explicit ACP ``authenticate`` after ``initialize`` when required by ``auth_mode``.

    Shared by passive probes, connection probes, execution sessions, and native
    session history. ``gemini-api-key`` and ``agent-platform`` always invoke
    ``client.authenticate(mode)`` and fail explicitly if unsupported/rejected.
    """
    mode = normalize_antigravity_auth_mode(auth_mode=auth_mode)
    if mode in {"gemini-api-key", "agent-platform"}:
        await client.authenticate(mode)
    return mode


def _probe_context(
    config: Any | None,
    base_env: Mapping[str, str] | None,
    auth_mode: str | None = None,
) -> tuple[str, dict[str, str], bool, str | None]:
    """Return ``(mode, env, local_creds_present, missing_prereq)`` for ACP probes."""
    mode = normalize_antigravity_auth_mode(config, auth_mode)
    env = build_antigravity_auth_env(config, base_env=base_env, auth_mode=mode)
    api_key = (getattr(config, "api_key", None) or "").strip()
    adc_present = bool(detect_antigravity_adc(env)["present"])
    if mode == "gemini-api-key":
        return mode, env, bool(api_key), (None if api_key else "missing_api_key")
    if mode == "agent-platform":
        return mode, env, adc_present, None
    return mode, env, False, None


def _is_cli_not_found(err: BaseException, detail: str) -> bool:
    return isinstance(err, FileNotFoundError) or "no such file" in detail.lower()


def _passive_status(
    *,
    ok: bool = True,
    authenticated: bool = False,
    credentials_present: bool = False,
    uncertain: bool = False,
    auth_methods: list[str] | None = None,
    supports_logout: bool = False,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "authenticated": authenticated,
        "credentials_present": credentials_present,
        "catalog_available": False,
        "inference_verified": False,
        "uncertain": uncertain,
        "auth_methods": list(ANTIGRAVITY_AUTH_MODES) if auth_methods is None else auth_methods,
        "supports_logout": supports_logout,
        "error": error,
        **extra,
    }


async def probe_antigravity_passive_status(
    binary: str,
    *,
    config: Any | None = None,
    base_env: Mapping[str, str] | None = None,
    auth_mode: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Read-only Antigravity auth probe via ``AcpClient.session_list()``.

    Never passes ``--dangerously-skip-permissions``, never opens a session,
    never runs inference, and discards returned session listings immediately.
    """
    mode, env, local_creds, missing = _probe_context(config, base_env, auth_mode)
    if missing == "missing_api_key":
        return _passive_status(error="missing_api_key")

    try:
        resolved_binary = await asyncio.to_thread(resolve_antigravity_cli_path, binary)
        cmd = antigravity_acp_command(resolved_binary, login=False)
        async with acp.AcpClient(cmd, env=env, request_timeout=timeout) as client:
            await client.initialize(client_info=_ACP_CLIENT_INFO)
            meta = {
                "auth_methods": [
                    str(item["id"])
                    for item in client.auth_methods
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"].strip()
                ],
                "supports_logout": client.supports_logout,
                "agent_version": client.agent_info.get("version"),
            }
            await authenticate_antigravity_client(client, mode)
            if not client.supports_list:
                return _passive_status(credentials_present=local_creds, uncertain=True, error="list_unsupported", **meta)
            # Discard the returned session listing immediately; never store or log it.
            await client.session_list()
            return _passive_status(authenticated=True, credentials_present=True, **meta)
    except acp.AcpRpcError as err:
        is_auth_err = err.code == acp.ACP_AUTH_REQUIRED or classify_antigravity_auth_error(err.message)
        error = (
            "invalid_credentials"
            if is_auth_err and mode == "gemini-api-key"
            else ("not_logged_in" if is_auth_err else f"acp_error_{err.code}")
        )
        extra: dict[str, Any] = {}
        if mode == "agent-platform" and is_auth_err and not local_creds:
            extra["adc_command"] = ANTIGRAVITY_ADC_LOGIN_COMMAND
        return _passive_status(credentials_present=local_creds, uncertain=not is_auth_err, error=error, **extra)
    except (acp.AcpTransportError, FileNotFoundError, OSError, asyncio.TimeoutError) as err:
        detail = str(err)
        return _passive_status(
            ok=False,
            credentials_present=local_creds,
            uncertain=True,
            error="cli_not_found" if _is_cli_not_found(err, detail) else "probe_failed",
            detail=detail[:400],
        )


async def logout_antigravity_via_acp(
    binary: str,
    *,
    config: Any | None = None,
    base_env: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[bool, str | None]:
    """Sign out Antigravity via ACP ``logout`` when advertised (no permission bypass)."""
    env = build_antigravity_auth_env(config, base_env=base_env, auth_mode="oauth-personal")
    try:
        resolved_binary = await asyncio.to_thread(resolve_antigravity_cli_path, binary)
        cmd = antigravity_acp_command(resolved_binary, login=False)
        async with acp.AcpClient(cmd, env=env, request_timeout=timeout) as client:
            await client.initialize(client_info=_ACP_CLIENT_INFO)
            if client.supports_logout:
                await client.logout()
                return True, None
            if not client.supports_list:
                return False, "logout_unsupported"
            try:
                await client.session_list()
            except acp.AcpRpcError as err:
                if err.code == acp.ACP_AUTH_REQUIRED or classify_antigravity_auth_error(err.message):
                    return True, None
                return False, "logout_unsupported"
            return False, "logout_unsupported"
    except acp.AcpRpcError as err:
        return (True, None) if err.code == acp.ACP_AUTH_REQUIRED else (False, str(err)[:400])
    except (acp.AcpTransportError, FileNotFoundError, OSError, asyncio.TimeoutError) as err:
        return False, str(err)[:400]


def _default_pick_excerpt(text: str) -> str:
    return next((line.strip()[:240] for line in (text or "").splitlines() if line.strip()), "")


def _default_classify_failure(_stdout: str, stderr: str, auth_mode: str) -> str:
    if classify_antigravity_auth_error(stderr):
        return "invalid_credentials" if auth_mode == "gemini-api-key" else "not_logged_in"
    return "cli_failed"


async def _apply_probe_model(
    client: acp.AcpClient,
    session_id: str,
    session: Mapping[str, Any],
    model: str,
) -> tuple[dict[str, Any], ...]:
    raw_options = session.get("configOptions")
    config_options = (
        tuple(dict(item) for item in raw_options if isinstance(item, Mapping))
        if isinstance(raw_options, list)
        else ()
    )
    if has_model_config_option(config_options):
        try:
            applied = await apply_model_and_effort(
                client,
                session_id,
                config_options,
                model=model,
                effort=None,
                force_reapply=True,
            )
            return applied.config_options
        except AntigravityModelSelectionError as exc:
            if not (
                exc.reason == "model_rejected"
                and isinstance(exc.__cause__, acp.AcpRpcError)
                and exc.__cause__.code == acp.JSONRPC_METHOD_NOT_FOUND
            ):
                raise

    resp = await client.session_set_model(session_id, model)
    return validate_model_selection_receipt(resp, model)


async def run_antigravity_connection_probe(
    *,
    binary: str,
    cwd: str,
    model: str,
    config: Any | None = None,
    base_env: Mapping[str, str] | None = None,
    timeout: float = 45.0,
    classify_failure: Callable[[str, str, str], str] | None = None,
    pick_excerpt: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run explicit Antigravity ACP probe distinguishing auth, catalog, and inference."""
    started = time.monotonic()
    auth_mode, env, local_creds, missing = _probe_context(config, base_env)
    probe_fingerprint = auth_config_fingerprint(config)
    classifier = classify_failure or _default_classify_failure
    excerpt_picker = pick_excerpt or _default_pick_excerpt
    state: dict[str, Any] = {
        "credentials_present": local_creds,
        "authenticated": False,
        "catalog_available": False,
        "inference_verified": False,
        "models": [],
        "model": model,
        "auth_mode": auth_mode,
    }

    def _result(ok: bool, **extra: Any) -> dict[str, Any]:
        return {"ok": ok, **extra, "duration_ms": int((time.monotonic() - started) * 1000), **state}

    if missing == "missing_api_key":
        return _result(False, error="invalid_credentials", detail="Gemini API key is not configured.")

    chunks: list[str] = []
    active_session_id: str | None = None

    async def _on_notification(method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            try:
                update = acp.parse_session_update(params)
            except ValueError:
                return
            if (
                isinstance(update, acp.AcpMessageChunkUpdate)
                and update.text
                and active_session_id is not None
                and update.session_id == active_session_id
            ):
                chunks.append(update.text)

    async def _execute() -> dict[str, Any]:
        nonlocal active_session_id
        session_id: str | None = None
        resolved_binary = await asyncio.to_thread(resolve_antigravity_cli_path, binary)
        cmd = antigravity_acp_command(resolved_binary, login=False)
        async with acp.AcpClient(cmd, cwd=cwd, env=env, request_timeout=timeout) as client:
            client.on_notification(_on_notification)
            await client.initialize(client_info=_ACP_CLIENT_INFO)
            await authenticate_antigravity_client(client, auth_mode)
            session = await client.session_new(cwd, mcp_servers=())
            session_id = str(session.get("sessionId") or "").strip() or None
            active_session_id = session_id
            live_models = extract_acp_live_models(session)
            state.update(authenticated=True, credentials_present=True, models=live_models, catalog_available=bool(live_models))
            await asyncio.to_thread(
                record_antigravity_live_models,
                config,
                session,
                auth_fingerprint=probe_fingerprint,
            )

            try:
                if not session_id:
                    return _result(False, error="session_create_failed", detail="ACP session/new returned no sessionId.")
                applied_options = await _apply_probe_model(client, session_id, session, model)
                if applied_options:
                    await asyncio.to_thread(
                        record_antigravity_live_models,
                        config,
                        applied_options,
                        auth_fingerprint=probe_fingerprint,
                    )
                prompt_result = await client.session_prompt(session_id, [{"type": "text", "text": "Hi"}], timeout=timeout)
                raw_stop_reason = prompt_result.get("stopReason") if isinstance(prompt_result, Mapping) else None
                stop_reason = raw_stop_reason.strip() if isinstance(raw_stop_reason, str) else ""
                if stop_reason != "end_turn":
                    return _result(
                        False,
                        error="incomplete_response",
                        detail=(
                            "Antigravity ACP prompt did not complete normally "
                            f"(stopReason={stop_reason or 'missing'})."
                        ),
                    )
                response_text = "".join(chunks).strip()
                if not response_text:
                    return _result(False, error="empty_response", detail="Antigravity ACP prompt returned no response text.")
                if is_antigravity_verification_required_reply(response_text):
                    state["authenticated"] = False
                    state["inference_verified"] = False
                    excerpt = excerpt_picker(response_text)
                    guidance = (
                        "Antigravity account verification is required. "
                        "Run `agy` once in a terminal to complete Google verification, then retry."
                    )
                    detail = f"{guidance}\n{excerpt}" if excerpt else guidance
                    return _result(False, error="not_logged_in", detail=detail[:600], excerpt=excerpt)
                state["inference_verified"] = True
                return _result(True, excerpt=excerpt_picker(response_text))
            finally:
                if session_id and client.supports_close:
                    try:
                        await client.session_close(session_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("Best-effort ACP session/close failed for probe", exc_info=True)

    try:
        return await asyncio.wait_for(_execute(), timeout=timeout)
    except asyncio.TimeoutError:
        return _result(False, error="timed_out")
    except AntigravityModelSelectionError as exc:
        return _result(False, error=exc.reason, detail=str(exc)[:600])
    except acp.AcpRpcError as err:
        detail = f"{err.code}: {err.message}"
        is_auth_err = err.code == acp.ACP_AUTH_REQUIRED or classify_antigravity_auth_error(err.message)
        error = (
            ("invalid_credentials" if auth_mode == "gemini-api-key" else "not_logged_in")
            if is_auth_err
            else classifier("", detail, auth_mode)
        )
        if is_auth_err:
            state["authenticated"] = False
        extra: dict[str, Any] = {}
        if auth_mode == "agent-platform" and not local_creds and (is_auth_err or error == "not_logged_in"):
            extra["login_command"] = ANTIGRAVITY_ADC_LOGIN_COMMAND
        return _result(False, error=error, detail=detail[:600], **extra)
    except (acp.AcpTransportError, FileNotFoundError, OSError) as err:
        detail = str(err).strip()
        is_missing = _is_cli_not_found(err, detail)
        is_auth_err = not is_missing and classify_antigravity_auth_error(detail)
        error = (
            "cli_not_found"
            if is_missing
            else (
                ("invalid_credentials" if auth_mode == "gemini-api-key" else "not_logged_in")
                if is_auth_err
                else classifier("", detail, auth_mode)
            )
        )
        if is_auth_err:
            state["authenticated"] = False
        extra = {}
        if auth_mode == "agent-platform" and not local_creds and (is_auth_err or error == "not_logged_in"):
            extra["login_command"] = ANTIGRAVITY_ADC_LOGIN_COMMAND
        return _result(False, error=error, detail=detail[:600], **extra)
