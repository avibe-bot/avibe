"""Antigravity agent backend adapter over stdio ACP."""

from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from config import paths
from core.agent_input import AgentInputMetadata
from core.agent_output import (
    Narration,
    Notice,
    ToolActivity,
    TurnEnd,
    TurnOutcome,
    emit_output,
)
from core.agent_path import prepend_managed_tools_to_path
from core.backend_failure import emit_backend_failure
from core.caller_context import (
    AVIBE_NATIVE_SESSION_ID_ENV,
    CALLER_CONTEXT_ENV_NAMES,
    caller_env_for_platform_payload,
)
from core.managed_skills import (
    managed_skill_environment,
    managed_skill_project_base,
)
from core.message_context import resolve_context_platform
from core.message_output import terminal_output_for
from core.native_dispatch_phase import (
    mark_backend_dispatch_attempted,
    mark_prewrite_recovery_required,
)
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.runtime_activation import RuntimeActivationIdentity
from core.runtime_ownership import (
    RuntimeResourceTarget,
    RuntimeSessionBinding,
    wake_runtime_ownership,
)
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.services.session_fork import (
    FORK_HANDOFF_MODE_NATIVE,
    FORK_HANDOFF_MODE_SUMMARY,
    build_fork_summary_handoff_text,
    can_use_native_tip_fork,
    clear_pending_fork_handoff,
    pending_native_fork,
    pending_summary_fork_handoff_for_session,
    record_fork_handoff_mode,
)
from core.system_prompt_injection import (
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
)
from modules.agents.acp import (
    ACP_AUTH_REQUIRED,
    JSONRPC_METHOD_NOT_FOUND,
    AcpClient,
    AcpConfigOptionUpdate,
    AcpMessageChunkUpdate,
    AcpRpcError,
    AcpSessionInfoUpdate,
    AcpThoughtChunkUpdate,
    AcpToolCallUpdate,
    AcpTransportError,
    AcpUsageUpdate,
    parse_session_update,
)
from modules.agents.antigravity.auth import (
    antigravity_acp_command,
    authenticate_antigravity_client,
    build_antigravity_auth_env,
    is_antigravity_verification_required_reply,
    normalize_antigravity_auth_mode,
    resolve_antigravity_cli_path as resolve_antigravity_cli_path,
)
from modules.agents.antigravity.models import (
    AntigravityModelSelectionError,
    apply_model_and_effort,
    auth_config_fingerprint as auth_config_fingerprint,
    extract_current_model_and_effort,
    extract_initial_default_effort,
    has_model_config_option,
    record_antigravity_live_models,
    resolve_requested_model_and_effort,
)
from modules.agents.antigravity.rules import render_session_rules
from modules.agents.base import (
    AGENT_RUNTIME_TURN_KEY,
    AgentRequest,
    BaseAgent,
    message_with_files,
)
from vibe.desktop_backends import desktop_backend_subprocess_environment
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

AGY_CLI_DISABLE_AUTO_UPDATE_ENV = "AGY_CLI_DISABLE_AUTO_UPDATE"
_CLIENT_INFO = {"name": "avibe", "version": "1.0.0"}
_STOP_CANCEL_GRACE_SECONDS = 3.0
_STEER_RECEIPT_TIMEOUT_SECONDS = 1.0
_MAX_STEER_ATTEMPTS = 512
_LIVE_CONFIG_FIELDS = frozenset({"idle_timeout_seconds", "auto_update"})
_SELECTION_METADATA_KEY = "antigravity_selection"
_MAX_CACHED_SELECTION_STATES = 512
_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


def _image_extension(raw: bytes, mime_type: str) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    return _IMAGE_MIME_EXTENSIONS.get(normalized_mime, ".png")


def _write_reply_image_bytes(runtime_key: str, encoded_data: str, mime_type: str) -> Path | None:
    payload = encoded_data.strip()
    if payload.startswith("data:") and "," in payload:
        header, payload = payload.split(",", 1)
        if not mime_type and ";" in header:
            mime_type = header[5:].split(";", 1)[0].strip().lower()
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("Ignoring invalid base64 image in Antigravity agent_message_chunk")
        return None
    if not raw:
        return None

    ext = _image_extension(raw, mime_type)
    slug = hashlib.sha256(runtime_key.encode("utf-8")).hexdigest()[:24]
    target_dir = paths.get_attachments_dir() / "antigravity" / slug
    destination = target_dir / f"image-{hashlib.sha256(raw).hexdigest()[:24]}{ext}"
    if destination.is_file():
        return destination.resolve()

    temp_path: Path | None = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=str(target_dir))
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    except OSError as exc:
        logger.warning("Failed to persist Antigravity reply image: %s", exc)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return None
    return destination.resolve()


class AntigravityResumeUnavailableError(RuntimeError):
    """Raised when a stored Antigravity native session cannot be resumed."""

    def __init__(self, native_session_id: str, detail: str = "") -> None:
        self.native_session_id = native_session_id
        msg = (
            f"Could not resume Antigravity session ({native_session_id}); "
            "not starting an empty conversation to avoid silently losing context."
        )
        super().__init__(f"{msg} ({detail})" if detail else msg)


class AntigravityCapabilityError(RuntimeError):
    """Raised when the Antigravity ACP peer lacks a capability Avibe requires."""

    def __init__(self, capability: str, message: str) -> None:
        self.capability = capability
        super().__init__(message)


def _config_cli_path(config: Any) -> str:
    cli_path = getattr(config, "cli_path", None) or getattr(config, "binary", None) or "agy"
    text = str(cli_path).strip()
    return text or "agy"


def _config_str(config: Any, field_name: str) -> str:
    value = getattr(config, field_name, None)
    return str(value).strip() if value is not None else ""


def build_antigravity_env(
    antigravity_config: Any,
    *,
    cli_path: str = "agy",
    base_env: Mapping[str, str] | None = None,
    working_dir: str | None = None,
    caller_env: Mapping[str, str] | None = None,
    managed_skills_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment for an Avibe-managed ``agy acp`` child."""
    source = os.environ if base_env is None else base_env
    env = {k: v for k, v in source.items() if k not in CALLER_CONTEXT_ENV_NAMES}

    desktop_env = desktop_backend_subprocess_environment("antigravity", cli_path, env)
    if desktop_env is not None:
        env = desktop_env

    prepend_managed_tools_to_path(env, base_env=env, working_dir=working_dir)

    if managed_skills_env:
        env.update(managed_skills_env)

    if caller_env:
        env.update(caller_env)

    env = build_antigravity_auth_env(antigravity_config, base_env=env)
    return env


def _launch_config_snapshot(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    return {
        key: value
        for key, value in vars(config).items()
        if key not in _LIVE_CONFIG_FIELDS
    }


@dataclass(frozen=True)
class AntigravityLaunchSpec:
    """Process-level inputs for one per-runtime-key ``agy acp`` child."""

    command: tuple[str, ...]
    cwd: str
    auth_fingerprint: str
    caller_env: tuple[tuple[str, str], ...]
    managed_skills_env: tuple[tuple[str, str], ...]
    managed_path: str
    rules_dirs: tuple[str, ...]
    rules_digest: str
    renewal_epoch: int


@dataclass
class _ActiveTurn:
    request: AgentRequest
    runtime_key: str
    native_session_id: str
    turn_id: str
    started_at: float
    reply_chunks: list[str] = field(default_factory=list)
    thought_chunks: list[str] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: bool = False
    settled: bool = False
    prompt_accepted: bool = False
    prompt_task: asyncio.Task[dict[str, Any]] | None = None
    prompt_tasks: list[asyncio.Task[dict[str, Any]]] = field(default_factory=list)
    prompt_changed: asyncio.Event = field(default_factory=asyncio.Event)
    steer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _SteerAttempt:
    target: ActiveSteerTarget
    predecessor: asyncio.Task[dict[str, Any]]
    prompt: asyncio.Task[dict[str, Any]] | None = None
    write_started: bool = False
    prewrite_receipt: SteerResult | None = None


class _SteerPrewriteRejected(Exception):
    def __init__(self, receipt: SteerResult) -> None:
        self.receipt = receipt


@dataclass(frozen=True)
class _PersistedSelectionState:
    default_efforts: dict[str, str] = field(default_factory=dict)
    current_effort: str | None = None
    native_session_id: str | None = None


def _load_json_dict(raw: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _serialize_selection_state(
    *,
    default_efforts: Mapping[str, str],
    current_effort: str | None,
    native_session_id: str | None,
) -> dict[str, Any]:
    cleaned_defaults = {
        str(model).strip(): str(effort).strip()
        for model, effort in sorted(default_efforts.items())
        if isinstance(model, str) and isinstance(effort, str) and model.strip() and effort.strip()
    }
    cleaned_effort = (
        current_effort.strip()
        if isinstance(current_effort, str) and current_effort.strip()
        else None
    )
    cleaned_native = (
        native_session_id.strip()
        if isinstance(native_session_id, str) and native_session_id.strip()
        else None
    )
    payload: dict[str, Any] = {
        "default_efforts": cleaned_defaults,
        "current_effort": cleaned_effort,
    }
    if cleaned_native is not None:
        payload["native_session_id"] = cleaned_native
    return payload


def _parse_selection_state(raw: Any) -> _PersistedSelectionState | None:
    if not isinstance(raw, Mapping):
        return None
    raw_defaults = raw.get("default_efforts")
    default_efforts: dict[str, str] = {}
    if isinstance(raw_defaults, Mapping):
        for model, effort in raw_defaults.items():
            if isinstance(model, str) and isinstance(effort, str):
                cleaned_model = model.strip()
                cleaned_effort = effort.strip()
                if cleaned_model and cleaned_effort:
                    default_efforts[cleaned_model] = cleaned_effort
    raw_effort = raw.get("current_effort")
    current_effort = (
        raw_effort.strip()
        if isinstance(raw_effort, str) and raw_effort.strip()
        else None
    )
    raw_native = raw.get("native_session_id")
    native_session_id = (
        raw_native.strip()
        if isinstance(raw_native, str) and raw_native.strip()
        else None
    )
    return _PersistedSelectionState(
        default_efforts=default_efforts,
        current_effort=current_effort,
        native_session_id=native_session_id,
    )


def _load_persisted_selection_state(
    agent_session_id: str,
    *,
    expected_native_session_id: str | None = None,
    db_path: Path | None = None,
) -> _PersistedSelectionState | None:
    cleaned_id = (agent_session_id or "").strip()
    if not cleaned_id:
        return None
    path = db_path or paths.get_sqlite_state_path()
    if not path.is_file():
        return None

    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    engine = create_sqlite_engine(path, read_only=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions.c.metadata_json)
                .where(agent_sessions.c.id == cleaned_id)
                .limit(1)
            ).mappings().first()
        if row is None:
            return None
        metadata = _load_json_dict(row["metadata_json"])
        parsed = _parse_selection_state(metadata.get(_SELECTION_METADATA_KEY))
        if parsed is None:
            return None
        if (
            expected_native_session_id
            and parsed.native_session_id
            and parsed.native_session_id != expected_native_session_id
        ):
            return None
        return parsed
    except OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        logger.debug("Antigravity selection state load skipped for %s: %s", cleaned_id, exc)
        return None
    except Exception:
        logger.debug("Antigravity selection state load failed for %s", cleaned_id, exc_info=True)
        return None
    finally:
        engine.dispose()


def _save_persisted_selection_state(
    agent_session_id: str,
    *,
    default_efforts: Mapping[str, str],
    current_effort: str | None,
    native_session_id: str | None,
    db_path: Path | None = None,
) -> bool:
    cleaned_id = (agent_session_id or "").strip()
    if not cleaned_id:
        return False
    path = db_path or paths.get_sqlite_state_path()
    if not path.is_file():
        return False

    from sqlalchemy import select, update
    from sqlalchemy.exc import OperationalError

    from storage.agent_session_rows import reserve_write_lock
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    serialized = _serialize_selection_state(
        default_efforts=default_efforts,
        current_effort=current_effort,
        native_session_id=native_session_id,
    )
    engine = create_sqlite_engine(path)
    try:
        with engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agent_sessions.c.metadata_json)
                .where(agent_sessions.c.id == cleaned_id)
                .limit(1)
            ).mappings().first()
            if row is None:
                return False
            metadata = _load_json_dict(row["metadata_json"])
            if metadata.get(_SELECTION_METADATA_KEY) == serialized:
                return True
            metadata[_SELECTION_METADATA_KEY] = serialized
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == cleaned_id)
                .values(metadata_json=json.dumps(metadata, ensure_ascii=False))
            )
            return True
    except OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        logger.debug("Antigravity selection state save skipped for %s: %s", cleaned_id, exc)
        return False
    except Exception:
        logger.debug("Antigravity selection state save failed for %s", cleaned_id, exc_info=True)
        return False
    finally:
        engine.dispose()


@dataclass
class _RuntimeSession:
    runtime_key: str
    base_session_id: str
    session_key: str
    agent_session_id: str
    workdir: str
    spec: AntigravityLaunchSpec
    client: AcpClient
    native_session_id: str | None = None
    config_options: tuple[dict[str, Any], ...] = ()
    current_model: str | None = None
    current_effort: str | None = None
    default_efforts: dict[str, str] = field(default_factory=dict)
    selection_dirty: bool = False
    persist_selection_to_db: bool = True
    last_persisted_selection: tuple[str | None, tuple[tuple[str, str], ...], str | None] | None = None
    activation: RuntimeActivationIdentity | None = None
    active_turn: _ActiveTurn | None = None
    last_activity: float = field(default_factory=time.monotonic)
    turn_started_at: float | None = None
    retired: bool = False
    intentional_teardown: bool = False
    exit_failure: tuple[str, str] | None = None
    reported_title: str | None = None
    pending_fork_handoff: str | None = None


def _specs_compatible(existing: _RuntimeSession, desired: AntigravityLaunchSpec) -> bool:
    """Compare launch specs while allowing native_session_id to appear once bound."""
    if (
        existing.spec.command != desired.command
        or existing.spec.cwd != desired.cwd
        or existing.spec.auth_fingerprint != desired.auth_fingerprint
        or existing.spec.managed_skills_env != desired.managed_skills_env
        or existing.spec.managed_path != desired.managed_path
        or existing.spec.rules_dirs != desired.rules_dirs
        or existing.spec.rules_digest != desired.rules_digest
        or existing.spec.renewal_epoch != desired.renewal_epoch
    ):
        return False

    if existing.spec.caller_env == desired.caller_env:
        return True

    old_env = dict(existing.spec.caller_env)
    new_env = dict(desired.caller_env)
    if (
        existing.native_session_id
        and AVIBE_NATIVE_SESSION_ID_ENV not in old_env
        and new_env.get(AVIBE_NATIVE_SESSION_ID_ENV) == existing.native_session_id
    ):
        new_without_native = {k: v for k, v in new_env.items() if k != AVIBE_NATIVE_SESSION_ID_ENV}
        return old_env == new_without_native
    return False


_DIFF_PREVIEW_MAX_LINES = 4
_DIFF_PREVIEW_MAX_LINE_CHARS = 200
_DIFF_PREVIEW_COMPARE_WINDOW = 64


def _truncate_diff_line(line: str, max_chars: int = _DIFF_PREVIEW_MAX_LINE_CHARS) -> str:
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."


def _format_diff_preview_lines(old_text: str | None, new_text: str) -> list[str]:
    new_lines = new_text.splitlines() if new_text else []
    if old_text is None:
        return [f"+ {_truncate_diff_line(line)}" for line in new_lines[:_DIFF_PREVIEW_MAX_LINES]]

    old_lines = old_text.splitlines() if old_text else []
    old_end = len(old_lines)
    new_end = len(new_lines)
    prefix = 0
    min_len = min(old_end, new_end)
    while prefix < min_len and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    while old_end > prefix and new_end > prefix and old_lines[old_end - 1] == new_lines[new_end - 1]:
        old_end -= 1
        new_end -= 1
    if prefix == old_end and prefix == new_end:
        return []

    old_window = old_lines[prefix : min(old_end, prefix + _DIFF_PREVIEW_COMPARE_WINDOW)]
    new_window = new_lines[prefix : min(new_end, prefix + _DIFF_PREVIEW_COMPARE_WINDOW)]
    matcher = difflib.SequenceMatcher(None, old_window, new_window, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        preview: list[str] = []
        if tag in ("replace", "delete"):
            for line in old_window[i1:i2][:_DIFF_PREVIEW_MAX_LINES]:
                preview.append(f"- {_truncate_diff_line(line)}")
        if tag in ("replace", "insert"):
            for line in new_window[j1:j2][:_DIFF_PREVIEW_MAX_LINES]:
                preview.append(f"+ {_truncate_diff_line(line)}")
        return preview
    return []


def _format_tool_activity(update: AcpToolCallUpdate, state: dict[str, Any]) -> tuple[str, str]:
    """Render one ACP tool call or incremental update into concise activity text."""
    title = state.get("title") or state.get("tool_kind") or update.tool_call_id
    if title.startswith("File ") and state.get("cwd"):
        path = (Path(state["cwd"]) / title[5:]).resolve()
        try:
            readable = str(path.relative_to(state["cwd"]))
        except ValueError:
            readable = str(path)
        title = f"File {readable}"
    status = state.get("status")
    header = f"{title} ({status})" if status else title
    lines = [header]
    raw_input = state.get("raw_input")
    if state.get("tool_kind") == "execute" and isinstance(raw_input, Mapping):
        command = next((raw_input.get(key) for key in ("CommandLine", "command", "commandLine", "command_line") if raw_input.get(key)), None)
        if isinstance(command, str):
            lines.append(command[:1000])
    raw_output = state.get("raw_output")
    if isinstance(raw_output, Mapping):
        for key in ("exitCode", "exit_code"):
            if key in raw_output:
                lines.append(f"exit {raw_output[key]}")
                break
        summary = raw_output.get("stdout") or raw_output.get("output") or raw_output.get("stderr")
    else:
        summary = raw_output
    if isinstance(summary, str) and summary.strip():
        lines.extend(summary.strip()[:1000].splitlines()[:4])

    for diff in state.get("diffs", update.diffs):
        lines.append(f"diff: {diff.path}")
        lines.extend(_format_diff_preview_lines(diff.old_text, diff.new_text))

    return "\n".join(lines), title


class AntigravityAgent(BaseAgent):
    """Avibe agent adapter directly composing ``AcpClient`` for ``agy acp``."""

    name = "antigravity"

    def __init__(
        self,
        controller: Any,
        antigravity_config: Any = None,
        *,
        rules_base_dir: Path | None = None,
    ) -> None:
        super().__init__(controller)
        self.antigravity_config = (
            antigravity_config
            if antigravity_config is not None
            else getattr(controller.config, "antigravity", None)
        )
        self._rules_base_dir = rules_base_dir
        self._sessions: dict[str, _RuntimeSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._selection_states: dict[str, _PersistedSelectionState] = {}
        self._steer_attempts: dict[tuple[str, str, str, str], _SteerAttempt] = {}
        self._renewal_epoch: int = 0
        self._shutting_down: bool = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _session_lock(self, runtime_key: str) -> asyncio.Lock:
        lock = self._session_locks.get(runtime_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[runtime_key] = lock
        return lock

    def _activation_registry(self):
        return getattr(self.controller, "runtime_activation", None)

    def _attach_activation(self, runtime_key: str) -> RuntimeActivationIdentity | None:
        registry = self._activation_registry()
        if registry is None:
            return None
        return registry.attach(self.name, runtime_key)

    def _retire_activation(self, session: _RuntimeSession) -> None:
        registry = self._activation_registry()
        if registry is None or session.activation is None:
            return
        registry.retire_if_current(session.activation, lambda: True)

    def _caller_env(self, request: AgentRequest) -> dict[str, str]:
        fallback_platform = resolve_context_platform(
            request.context,
            fallback_platform=getattr(self.controller, "primary_platform", None),
        )
        return caller_env_for_platform_payload(
            request.context.platform_specific,
            message=request.context,
            fallback_platform=fallback_platform,
            session_stable_only=True,
        )

    def _resolve_stored_native_session_id(
        self,
        request: AgentRequest,
        *,
        previous_native_session_id: str | None = None,
    ) -> str | None:
        use_backend_anchor = self._uses_namespaced_backend_session(
            request.context,
            subagent_name=request.subagent_name,
        )
        if not use_backend_anchor:
            reserved = self._reserved_native_session_id(request.context, backend=self.name)
            if reserved:
                return reserved

        if previous_native_session_id:
            return previous_native_session_id

        payload = request.context.platform_specific or {}
        if not use_backend_anchor and isinstance(payload.get("agent_session_target"), Mapping):
            # An explicit Workbench target with an empty native_session_id means a
            # fresh session, backend switch, or pending fork — never fall back to
            # an older session-key mapping.
            return None

        session_id_getter = getattr(self.sessions, "get_agent_session_id", None)
        if callable(session_id_getter):
            stored = session_id_getter(request.session_key, request.base_session_id, self.name)
            if isinstance(stored, str) and stored.strip():
                return stored.strip()

        mapping_getter = getattr(self.sessions, "get_agent_session_mapping", None)
        if callable(mapping_getter):
            stored = mapping_getter(request.session_key, self.name, request.base_session_id)
            if isinstance(stored, str) and stored.strip():
                return stored.strip()
        return None

    async def _build_launch(
        self,
        request: AgentRequest,
        *,
        runtime_key: str,
        resolved_model: str,
    ) -> tuple[AntigravityLaunchSpec, dict[str, str], dict[str, Any] | None]:
        cli_path = await asyncio.to_thread(
            resolve_antigravity_cli_path,
            _config_cli_path(self.antigravity_config),
        )
        command = tuple(antigravity_acp_command(
            cli_path,
            execution=True,
            subagent_name=request.subagent_name,
        ))

        caller_env = self._caller_env(request)
        skills_project_base = managed_skill_project_base(request.context)
        managed_skills_env = await asyncio.to_thread(
            managed_skill_environment,
            request.working_path,
            project_base=skills_project_base,
        )
        launch_env = await asyncio.to_thread(
            build_antigravity_env,
            self.antigravity_config,
            cli_path=cli_path,
            working_dir=request.working_path,
            caller_env=caller_env,
            managed_skills_env=managed_skills_env,
        )

        enabled_agents = await asyncio.to_thread(get_enabled_agents_for_prompt, self.controller)
        skill_catalog_sink: list[dict[str, Any]] = []
        prompt_text = await asyncio.to_thread(
            build_system_prompt_injection,
            agent_instructions=request.vibe_agent_system_prompt or "",
            backend=self.name,
            include_quick_replies=bool(getattr(self.config, "reply_enhancements", True)),
            context=request.context,
            fallback_platform=getattr(self.controller, "primary_platform", None),
            enabled_agents=enabled_agents,
            skills_cwd=request.working_path,
            skills_project_base=skills_project_base,
            skill_catalog_sink=skill_catalog_sink,
            model=resolved_model,
        )
        rendered_rules = await asyncio.to_thread(
            render_session_rules,
            runtime_key,
            prompt_text,
            base_dir=self._rules_base_dir,
        )

        spec = AntigravityLaunchSpec(
            command=command,
            cwd=request.working_path,
            auth_fingerprint=auth_config_fingerprint(self.antigravity_config),
            caller_env=tuple(sorted(caller_env.items())),
            managed_skills_env=tuple(sorted(managed_skills_env.items())),
            managed_path=launch_env.get("PATH", ""),
            rules_dirs=rendered_rules.directories,
            rules_digest=rendered_rules.digest,
            renewal_epoch=self._renewal_epoch,
        )
        skill_catalog = skill_catalog_sink[0] if skill_catalog_sink else None
        return spec, launch_env, skill_catalog

    def _cache_session_selection_state(
        self,
        session: _RuntimeSession,
    ) -> _PersistedSelectionState | None:
        native_id = (session.native_session_id or "").strip()
        if not native_id:
            return None
        state = _PersistedSelectionState(
            default_efforts=dict(session.default_efforts),
            current_effort=session.current_effort,
            native_session_id=native_id,
        )
        self._selection_states.pop(native_id, None)
        self._selection_states[native_id] = state
        while len(self._selection_states) > _MAX_CACHED_SELECTION_STATES:
            oldest_key = next(iter(self._selection_states))
            self._selection_states.pop(oldest_key, None)
        return state

    async def _persist_session_selection_state(self, session: _RuntimeSession) -> None:
        state = self._cache_session_selection_state(session)
        if state is None or not session.persist_selection_to_db or not session.agent_session_id:
            return
        fingerprint = (
            state.native_session_id,
            tuple(sorted(state.default_efforts.items())),
            state.current_effort,
        )
        if session.last_persisted_selection == fingerprint:
            return
        saved = await asyncio.to_thread(
            _save_persisted_selection_state,
            session.agent_session_id,
            default_efforts=state.default_efforts,
            current_effort=state.current_effort,
            native_session_id=state.native_session_id,
        )
        if saved:
            session.last_persisted_selection = fingerprint

    async def _restore_selection_state(
        self,
        *,
        agent_session_id: str,
        native_session_id: str,
        fallback_native_id: str | None = None,
        load_from_db: bool = True,
    ) -> _PersistedSelectionState | None:
        mem_state = self._selection_states.get(native_session_id)
        if mem_state is None and fallback_native_id and fallback_native_id != native_session_id:
            mem_state = self._selection_states.get(fallback_native_id)

        db_state: _PersistedSelectionState | None = None
        if load_from_db and agent_session_id:
            expected_native = fallback_native_id or native_session_id
            db_state = await asyncio.to_thread(
                _load_persisted_selection_state,
                agent_session_id,
                expected_native_session_id=expected_native,
            )
            if db_state is None and expected_native != native_session_id:
                db_state = await asyncio.to_thread(
                    _load_persisted_selection_state,
                    agent_session_id,
                    expected_native_session_id=native_session_id,
                )

        if mem_state is None and db_state is None:
            return None
        merged_defaults: dict[str, str] = {}
        if db_state is not None:
            merged_defaults.update(db_state.default_efforts)
        if mem_state is not None:
            merged_defaults.update(mem_state.default_efforts)
            current_effort = mem_state.current_effort
        else:
            assert db_state is not None
            current_effort = db_state.current_effort
        return _PersistedSelectionState(
            default_efforts=merged_defaults,
            current_effort=current_effort,
            native_session_id=native_session_id,
        )

    async def _close_session(
        self,
        session: _RuntimeSession,
        *,
        intentional: bool = True,
        retire_activation: bool = True,
    ) -> bool:
        if session.native_session_id and (session.default_efforts or session.current_effort is not None):
            self._cache_session_selection_state(session)
        session.retired = True
        if intentional:
            session.intentional_teardown = True
        if self._sessions.get(session.runtime_key) is session:
            self._sessions.pop(session.runtime_key, None)
        if retire_activation:
            self._retire_activation(session)
        was_alive = session.client.is_alive
        await session.client.close()
        return was_alive

    async def _get_or_create_session(
        self,
        request: AgentRequest,
        *,
        runtime_key: str,
        agent_session_id: str,
        spec: AntigravityLaunchSpec,
        launch_env: dict[str, str],
    ) -> _RuntimeSession:
        async with self._session_lock(runtime_key):
            existing = self._sessions.get(runtime_key)
            previous_native_id: str | None = None
            if existing is not None:
                if not existing.retired and existing.client.is_alive and _specs_compatible(existing, spec):
                    existing.agent_session_id = agent_session_id or existing.agent_session_id
                    existing.session_key = request.session_key
                    existing.last_activity = time.monotonic()
                    return existing
                previous_native_id = existing.native_session_id
                await self._close_session(existing, intentional=True, retire_activation=True)

            client = AcpClient(spec.command, cwd=spec.cwd, env=launch_env)
            session = _RuntimeSession(
                runtime_key=runtime_key,
                base_session_id=request.base_session_id,
                session_key=request.session_key,
                agent_session_id=agent_session_id,
                workdir=request.working_path,
                spec=spec,
                client=client,
            )

            async def _notify(method: str, params: dict[str, Any]) -> None:
                await self._handle_notification(session, method, params)

            async def _server_req(method: str, params: dict[str, Any]) -> dict[str, Any]:
                return await self._handle_server_request(session, method, params)

            client.on_notification(_notify)
            client.on_server_request(_server_req)

            fork_handoff_mode: str | None = None
            try:
                await client.start()
                await client.initialize(client_info=_CLIENT_INFO, client_capabilities={})
                await authenticate_antigravity_client(
                    client,
                    _config_str(self.antigravity_config, "auth_mode"),
                )
                if spec.rules_dirs and not client.supports_additional_directories:
                    raise AntigravityCapabilityError(
                        "sessionCapabilities.additionalDirectories",
                        (
                            "Antigravity ACP server does not advertise "
                            "sessionCapabilities.additionalDirectories, so Avibe cannot attach "
                            "Agent instructions and tool-policy rules. Upgrade the Antigravity CLI "
                            "(agy) or enable additionalDirectories support before retrying."
                        ),
                    )
                add_dirs = list(spec.rules_dirs) if spec.rules_dirs else None

                use_backend_anchor = self._uses_namespaced_backend_session(
                    request.context,
                    subagent_name=request.subagent_name,
                )
                stored_native_id = await asyncio.to_thread(
                    self._resolve_stored_native_session_id,
                    request,
                    previous_native_session_id=previous_native_id,
                )
                pending_fork = (
                    None
                    if (stored_native_id or use_backend_anchor)
                    else pending_native_fork(request.context, self.name)
                )

                if stored_native_id:
                    if not client.supports_resume:
                        raise AntigravityResumeUnavailableError(
                            stored_native_id,
                            "ACP server does not advertise session/resume",
                        )
                    try:
                        resp = await client.session_resume(
                            stored_native_id,
                            request.working_path,
                            mcp_servers=(),
                            additional_directories=add_dirs,
                        )
                    except AcpRpcError as exc:
                        if exc.code == ACP_AUTH_REQUIRED:
                            raise
                        raise AntigravityResumeUnavailableError(stored_native_id, str(exc)) from exc
                    except AcpTransportError as exc:
                        raise AntigravityResumeUnavailableError(stored_native_id, str(exc)) from exc
                    native_session_id = str(resp.get("sessionId") or stored_native_id)
                else:
                    resp = None
                    if pending_fork is not None and client.supports_fork:
                        can_native_fork = await asyncio.to_thread(
                            can_use_native_tip_fork,
                            pending_fork,
                        )
                        if not can_native_fork:
                            logger.info(
                                "Skipping native Antigravity session/fork for %s because source %s "
                                "advanced past reserved boundary %s; falling back to bounded summary handoff",
                                agent_session_id or runtime_key,
                                pending_fork.get("source_session_id"),
                                pending_fork.get("source_message_id"),
                            )
                        else:
                            try:
                                resp = await client.session_fork(
                                    pending_fork["source_native_session_id"],
                                    request.working_path,
                                    mcp_servers=(),
                                    additional_directories=add_dirs,
                                )
                                fork_handoff_mode = FORK_HANDOFF_MODE_NATIVE
                            except AcpRpcError as exc:
                                if exc.code != JSONRPC_METHOD_NOT_FOUND:
                                    raise
                                resp = None

                    if resp is None:
                        resp = await client.session_new(
                            request.working_path,
                            mcp_servers=(),
                            additional_directories=add_dirs,
                        )
                        if pending_fork is not None:
                            fork_handoff_mode = FORK_HANDOFF_MODE_SUMMARY
                            if not (request.input_metadata is not None and request.input_metadata.handoff):
                                session.pending_fork_handoff = await asyncio.to_thread(
                                    build_fork_summary_handoff_text,
                                    pending_fork,
                                    backend=self.name,
                                )

                    native_session_id = str(resp.get("sessionId") or "")
                    if not native_session_id:
                        raise AcpTransportError("ACP session creation response missing sessionId")

                raw_options = resp.get("configOptions")
                session.native_session_id = native_session_id
                session.config_options = (
                    tuple(dict(item) for item in raw_options if isinstance(item, Mapping))
                    if isinstance(raw_options, list)
                    else ()
                )
                await asyncio.to_thread(
                    record_antigravity_live_models,
                    self.antigravity_config,
                    resp,
                    auth_fingerprint=spec.auth_fingerprint,
                )
                session.activation = self._attach_activation(runtime_key)
                self._sessions[runtime_key] = session
            except BaseException:
                session.retired = True
                await client.close()
                raise

            bound_id = self.bind_agent_session_id(request, native_session_id)
            if bound_id:
                session.agent_session_id = bound_id
            reserved_id = self._reserved_agent_session_id(request.context)
            session.persist_selection_to_db = not (
                use_backend_anchor and reserved_id and session.agent_session_id == reserved_id
            )

            if stored_native_id:
                restored = await self._restore_selection_state(
                    agent_session_id=session.agent_session_id,
                    native_session_id=native_session_id,
                    fallback_native_id=stored_native_id,
                    load_from_db=session.persist_selection_to_db,
                )
                if restored is not None:
                    session.default_efforts.update(restored.default_efforts)
                    session.current_effort = restored.current_effort
            elif fork_handoff_mode == FORK_HANDOFF_MODE_NATIVE and isinstance(pending_fork, Mapping):
                source_native_id = str(pending_fork.get("source_native_session_id") or "").strip()
                source_session_id = str(
                    pending_fork.get("source_session_id")
                    or pending_fork.get("fork_source_session_id")
                    or ""
                ).strip()
                inherited = await self._restore_selection_state(
                    agent_session_id=source_session_id,
                    native_session_id=source_native_id,
                    load_from_db=bool(source_session_id),
                )
                if inherited is not None:
                    session.default_efforts.update(inherited.default_efforts)
                    session.current_effort = inherited.current_effort
            seeded_default = extract_initial_default_effort(
                session.config_options,
                allow_current_value=not stored_native_id and not (
                    fork_handoff_mode == FORK_HANDOFF_MODE_NATIVE and isinstance(pending_fork, Mapping)
                ),
            )
            if seeded_default is not None:
                init_model, init_effort = seeded_default
                session.default_efforts[init_model] = init_effort
            if session.default_efforts or session.current_effort is not None:
                await self._persist_session_selection_state(session)
            if fork_handoff_mode == FORK_HANDOFF_MODE_SUMMARY:
                await emit_output(self.controller, request.context, Notice(text=i18n_t(
                    "antigravityNotice.summaryFork", self._language(),
                )))
            if fork_handoff_mode and session.agent_session_id:
                await asyncio.to_thread(
                    record_fork_handoff_mode,
                    session.agent_session_id,
                    mode=fork_handoff_mode,
                    pending=(
                        bool(session.pending_fork_handoff)
                        if fork_handoff_mode == FORK_HANDOFF_MODE_SUMMARY
                        else False
                    ),
                )
            elif (
                stored_native_id
                and not use_backend_anchor
                and session.agent_session_id
                and not (request.input_metadata is not None and request.input_metadata.handoff)
            ):
                pending_summary_fork = await asyncio.to_thread(
                    pending_summary_fork_handoff_for_session,
                    session.agent_session_id,
                )
                if pending_summary_fork is not None:
                    session.pending_fork_handoff = await asyncio.to_thread(
                        build_fork_summary_handoff_text,
                        pending_summary_fork,
                        backend=self.name,
                    )
            return session

    async def _handle_server_request(
        self,
        session: _RuntimeSession,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if method != "session/request_permission":
            raise AcpRpcError(JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

        req_session_id = params.get("sessionId")
        tool_call, options = params.get("toolCall"), params.get("options")
        if (
            not isinstance(req_session_id, str) or req_session_id != session.native_session_id
            or not isinstance(tool_call, Mapping) or not isinstance(tool_call.get("toolCallId"), str)
            or not tool_call["toolCallId"].strip()
            or not isinstance(options, list) or not options
            or any(not isinstance(option, Mapping) or option.get("kind") not in {
                "allow_once", "allow_always", "reject_once", "reject_always",
            } or not isinstance(option.get("optionId"), str) or not option["optionId"].strip() for option in options)
        ):
            raise AcpRpcError(-32602, "Invalid session/request_permission shape")
        turn = session.active_turn
        if session.retired or turn is None or turn.settled or turn.cancelled:
            return {"outcome": {"outcome": "cancelled"}}
        for option in options:
            if option["kind"] == "allow_once":
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        raise AcpRpcError(-32602, "Managed permission request has no allow-once option")

    async def _handle_notification(
        self,
        session: _RuntimeSession,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if method != "session/update":
            return
        # Late notifications from a retired binding can never affect a successor.
        if session.retired or self._sessions.get(session.runtime_key) is not session:
            return

        try:
            update = parse_session_update(params)
        except ValueError as exc:
            logger.debug("Ignoring malformed ACP session/update: %s", exc)
            return

        if session.native_session_id and update.session_id != session.native_session_id:
            return

        if isinstance(update, AcpConfigOptionUpdate):
            session.config_options = update.config_options
            seeded_default = extract_initial_default_effort(
                session.config_options,
                allow_current_value=False,
            )
            if seeded_default is not None:
                upd_model, upd_effort = seeded_default
                session.default_efforts[upd_model] = upd_effort
            return

        if isinstance(update, AcpSessionInfoUpdate):
            if update.title and update.title.strip():
                session.reported_title = update.title.strip()
                self._schedule_live_title_backfill(session, session.reported_title)
            return

        turn = session.active_turn
        if turn is None or turn.settled:
            return
        # A completed reply may arrive while session/cancel is in flight. Keep its
        # text until the newest native result decides completed versus stopped.
        if turn.cancelled and not isinstance(update, AcpMessageChunkUpdate):
            return

        turn.prompt_accepted = True
        session.last_activity = time.monotonic()

        if isinstance(update, AcpUsageUpdate):
            if update.used is not None and update.used > 0:
                note_tokens = getattr(self.controller, "note_session_tokens", None)
                if callable(note_tokens):
                    note_tokens(turn.request.context, total=update.used)
            return

        if isinstance(update, AcpThoughtChunkUpdate):
            if update.text:
                turn.thought_chunks.append(update.text)
                await emit_output(
                    self.controller,
                    turn.request.context,
                    Narration(text=update.text, process_only=True),
                )
            return

        if isinstance(update, AcpToolCallUpdate):
            if turn.reply_chunks:
                pre_tool_text = "".join(turn.reply_chunks).strip()
                if pre_tool_text:
                    turn.reply_chunks.clear()
                    await emit_output(
                        self.controller,
                        turn.request.context,
                        Narration(text=pre_tool_text),
                    )
            state = turn.tool_calls.setdefault(update.tool_call_id, {})
            if update.title is not None:
                state["title"] = update.title
            if update.tool_kind is not None:
                state["tool_kind"] = update.tool_kind
            if update.status is not None:
                state["status"] = update.status
            if update.raw_input is not None:
                state["raw_input"] = update.raw_input
            if update.raw_output is not None:
                state["raw_output"] = update.raw_output
            if update.locations:
                state["locations"] = update.locations
            if update.diffs:
                state["diffs"] = update.diffs
            if state.get("status") not in {"completed", "failed"} or state.get("emitted"):
                return
            state["emitted"] = True
            state["cwd"] = session.workdir
            text, status_label = await asyncio.to_thread(_format_tool_activity, update, dict(state))
            await emit_output(
                self.controller,
                turn.request.context,
                ToolActivity(text=text, status_label=status_label),
            )
            return

        if isinstance(update, AcpMessageChunkUpdate):
            if update.text:
                turn.reply_chunks.append(update.text)
                return
            if update.content.get("type") == "image":
                image_markdown = await self._materialize_reply_image(turn, update.content)
                if image_markdown:
                    if turn.reply_chunks and not turn.reply_chunks[-1].endswith("\n"):
                        turn.reply_chunks.append("\n\n")
                    turn.reply_chunks.append(image_markdown)
            return

    async def _materialize_reply_image(
        self,
        turn: _ActiveTurn,
        content: Mapping[str, Any],
    ) -> str | None:
        raw_data = content.get("data")
        if isinstance(raw_data, str) and raw_data.strip():
            mime_type = str(content.get("mimeType") or content.get("mime_type") or "").strip().lower()
            path = await asyncio.to_thread(_write_reply_image_bytes, turn.runtime_key, raw_data, mime_type)
            if path is not None:
                return f"![image]({path.as_uri()})"
            return None

        raw_uri = content.get("uri")
        if isinstance(raw_uri, str) and raw_uri.strip():
            uri = raw_uri.strip()
            if uri.startswith(("file://", "http://", "https://")):
                return f"![image]({uri})"
            if os.path.isabs(uri):
                return f"![image]({Path(uri).as_uri()})"
        return None

    def _build_prompt_blocks_sync(
        self,
        request: AgentRequest,
        input_metadata: AgentInputMetadata | None,
        *,
        supports_images: bool,
    ) -> list[dict[str, Any]]:
        image_blocks: list[dict[str, Any]] = []
        text_files = []

        for attachment in request.files or ():
            mimetype = str(attachment.mimetype or "").strip()
            if supports_images and mimetype.startswith("image/"):
                raw_bytes: bytes | None = None
                if isinstance(attachment.content, (bytes, bytearray)) and attachment.content:
                    raw_bytes = bytes(attachment.content)
                elif attachment.local_path:
                    try:
                        raw_bytes = Path(attachment.local_path).read_bytes()
                    except OSError as exc:
                        logger.debug(
                            "Failed to read image attachment %s for ACP block; falling back to path reference: %s",
                            attachment.local_path,
                            exc,
                        )
                if raw_bytes is not None:
                    image_blocks.append(
                        {
                            "type": "image",
                            "data": base64.b64encode(raw_bytes).decode("ascii"),
                            "mimeType": mimetype,
                        }
                    )
                    continue
            text_files.append(attachment)

        prompt_text = message_with_files(request.message, text_files or None)
        prompt_text = self.render_input(prompt_text, input_metadata)
        blocks: list[dict[str, Any]] = []
        if prompt_text or not image_blocks:
            blocks.append({"type": "text", "text": prompt_text})
        blocks.extend(image_blocks)
        return blocks

    def _language(self) -> str:
        return str(getattr(self.config, "language", "en") or "en")

    @staticmethod
    def _classify_failure(error: Exception, *, language: str = "en") -> tuple[str, str, str | None]:
        if isinstance(error, AntigravityCapabilityError):
            diagnostic = str(error)
            display = i18n_t(
                "error.antigravityCapabilityMissing",
                language,
                capability=error.capability,
            )
            return diagnostic, f"❌ {display}", "capability_missing"
        if isinstance(error, AcpRpcError) and error.code == ACP_AUTH_REQUIRED:
            if isinstance(error.data, Mapping) and error.data.get("verification_required"):
                diagnostic = (
                    f"Antigravity authentication required (ACP {error.code}, verification required): "
                    f"{error.message}"
                )
                display = i18n_t(
                    "error.antigravityVerificationRequired",
                    language,
                    detail=error.message,
                )
                return diagnostic, f"❌ {display}", "auth_required"
            diagnostic = f"Antigravity authentication required (ACP {error.code}): {error.message}"
            display = i18n_t(
                "error.antigravityAuthRequired",
                language,
                code=error.code,
                detail=error.message,
            )
            return diagnostic, f"❌ {display}", "auth_required"
        if isinstance(error, FileNotFoundError) or (
            isinstance(error, AcpTransportError) and isinstance(error.__cause__, FileNotFoundError)
        ):
            diagnostic = f"Antigravity CLI not found: {error}"
            display = i18n_t("error.antigravityCliNotFound", language, detail=str(error))
            return diagnostic, f"❌ {display}", "cli_not_found"
        if isinstance(error, OSError) or (
            isinstance(error, AcpTransportError) and isinstance(error.__cause__, OSError)
        ):
            diagnostic = f"Antigravity CLI failed to launch: {error}"
            display = i18n_t("error.antigravityLaunchFailed", language, detail=str(error))
            return diagnostic, f"❌ {display}", "launch_failed"
        diagnostic = str(error)
        display = i18n_t("error.antigravityError", language, detail=diagnostic)
        return diagnostic, f"❌ {display}", None

    def _schedule_live_title_backfill(self, session: _RuntimeSession, title: str) -> None:
        if not session.agent_session_id or not session.native_session_id:
            return

        async def _persist() -> None:
            await asyncio.to_thread(
                self._persist_live_title,
                session.agent_session_id,
                session.native_session_id or "",
                title,
            )

        task = asyncio.create_task(_persist())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _persist_live_title(self, agent_session_id: str, native_session_id: str, title: str) -> None:
        db_path = paths.get_sqlite_state_path()
        if not db_path.is_file():
            return
        from core.inbox_events import bus
        from core.services import sessions as workbench_sessions_service
        from storage import messages_service
        from storage.db import create_sqlite_engine

        inbox_row: dict[str, Any] | None = None
        engine = create_sqlite_engine(db_path)
        try:
            with engine.begin() as conn:
                updated = workbench_sessions_service.backfill_session_title(
                    conn,
                    agent_session_id,
                    title=title,
                    backend=self.name,
                    source="backend",
                    confidence="high",
                    native_session_id=native_session_id,
                )
                if updated is not None:
                    inbox_row = messages_service.get_inbox_session(conn, agent_session_id, platform="avibe")
        except Exception:
            logger.debug("Antigravity live title backfill skipped", exc_info=True)
            return
        finally:
            engine.dispose()

        if updated is not None:
            bus.publish(
                "session.activity",
                {
                    "session_id": updated["id"],
                    "scope_id": updated.get("scope_id"),
                    "event": "updated",
                    "title": updated.get("title"),
                },
            )
            if inbox_row is not None:
                bus.publish("inbox.session.updated", inbox_row)

    async def _settle_turn_stopped(self, turn: _ActiveTurn) -> None:
        if turn.settled:
            return
        turn.settled = True
        await emit_output(
            self.controller,
            turn.request.context,
            TurnEnd(
                outcome=TurnOutcome.STOPPED,
                silent=True,
                output=terminal_output_for(turn.request),
            ),
        )
        await self._remove_ack_reaction(turn.request, terminal_emoji=STOPPED_REACTION_EMOJI)

    async def _settle_turn_teardown(self, turn: _ActiveTurn) -> None:
        if turn.settled:
            return
        turn.settled = True
        await emit_output(
            self.controller,
            turn.request.context,
            TurnEnd(
                outcome=TurnOutcome.TEARDOWN,
                silent=True,
                output=terminal_output_for(turn.request),
            ),
        )
        await self._remove_ack_reaction(turn.request)

    async def _settle_turn_done(self, session: _RuntimeSession, turn: _ActiveTurn) -> None:
        if turn.settled:
            return
        final_text = "".join(turn.reply_chunks)
        if is_antigravity_verification_required_reply(final_text):
            err = AcpRpcError(
                ACP_AUTH_REQUIRED,
                final_text.strip()[:400],
                data={"verification_required": True},
            )
            await self._settle_turn_failure(session, turn, turn.request, err)
            return
        turn.settled = True
        await self.emit_result_message(
            turn.request.context,
            final_text,
            subtype="success",
            started_at=turn.started_at,
            request=turn.request,
        )
        if session.native_session_id and not session.reported_title:
            self._maybe_backfill_session_title(turn.request, session.native_session_id)

    async def _flush_partial_reply(self, turn: _ActiveTurn) -> None:
        text = "".join(turn.reply_chunks).strip()
        turn.reply_chunks.clear()
        if text and not is_antigravity_verification_required_reply(text):
            await emit_output(self.controller, turn.request.context, Narration(text=text))

    async def _settle_turn_incomplete(
        self, session: _RuntimeSession, turn: _ActiveTurn, *, stop_reason: str,
    ) -> None:
        del session
        if turn.settled:
            return
        turn.settled = True
        await self._flush_partial_reply(turn)
        if stop_reason in ("max_tokens", "max_turn_requests"):
            diagnostic = f"Antigravity turn stopped incomplete ({stop_reason})."
            localized = i18n_t("error.antigravityTurnIncomplete", self._language(), reason=stop_reason)
        elif stop_reason == "refusal":
            diagnostic = "Antigravity refused to complete the turn (refusal)."
            localized = i18n_t("error.antigravityTurnRefused", self._language())
        elif stop_reason:
            diagnostic = f"Antigravity turn ended with unexpected stopReason: {stop_reason}."
            localized = i18n_t("error.antigravityTurnUnexpectedStop", self._language(), reason=stop_reason)
        else:
            diagnostic = "Antigravity turn ended without a stopReason."
            localized = i18n_t("error.antigravityTurnMissingStopReason", self._language())
        await emit_backend_failure(
            self.controller, turn.request.context, self.name, diagnostic,
            display_text=f"❌ {localized}", request=turn.request,
        )
        await self._remove_ack_reaction(turn.request)

    async def _settle_turn_failure(
        self,
        session: _RuntimeSession | None,
        turn: _ActiveTurn | None,
        request: AgentRequest,
        error: Exception,
    ) -> None:
        if turn is not None:
            if turn.settled:
                return
            turn.settled = True
            await self._flush_partial_reply(turn)
        diagnostic, display_text, _ = self._classify_failure(error, language=self._language())
        if session is not None and not session.client.is_alive:
            session.exit_failure = (diagnostic, display_text)
            await self._close_session(session, intentional=False, retire_activation=True)
        await emit_backend_failure(
            self.controller,
            request.context,
            self.name,
            diagnostic,
            display_text=display_text,
            request=request,
            cause=error,
        )
        await self._remove_ack_reaction(request)

    async def _refuse_disabled_turn(self, request: AgentRequest) -> None:
        language = self._language()
        await emit_backend_failure(
            self.controller,
            request.context,
            self.name,
            "antigravity backend disabled",
            display_text=f"❌ {i18n_t('error.agentRuntimeRetired', language, agent='Antigravity')}",
            request=request,
        )
        await self._remove_ack_reaction(request)

    @staticmethod
    def _invalidate_session_selection(
        session: _RuntimeSession,
        error: BaseException,
    ) -> None:
        """Invalidate cached selection state after any failed model/effort apply.

        Preserves ``default_efforts`` and any active or server-mutated
        ``current_effort`` override so the next turn (even after transport
        eviction or restart) explicitly reapplies the requested model and
        restores the baseline effort when an override is cleared.
        """
        session.current_model = None
        session.selection_dirty = True
        if not isinstance(error, AntigravityModelSelectionError):
            return
        if error.model and error.default_effort:
            session.default_efforts[error.model] = error.default_effort
        if error.config_options and has_model_config_option(error.config_options):
            session.config_options = error.config_options
            seeded_default = extract_initial_default_effort(
                error.config_options,
                allow_current_value=False,
            )
            if seeded_default is not None:
                init_model, init_effort = seeded_default
                session.default_efforts[init_model] = init_effort
            observed_model, observed_effort = extract_current_model_and_effort(error.config_options)
            known_default = session.default_efforts.get(observed_model) if observed_model else None
            if observed_effort is not None and (
                error.reason == "effort_mismatch"
                or (known_default is not None and observed_effort != known_default)
            ):
                session.current_effort = observed_effort

    async def handle_message(self, request: AgentRequest) -> None:
        if self._shutting_down or getattr(self.antigravity_config, "enabled", True) is False:
            await self._refuse_disabled_turn(request)
            return

        runtime_key = self.runtime_turn_key(request)
        resolved_model, resolved_effort = resolve_requested_model_and_effort(request)
        if not resolved_model:
            err = AntigravityModelSelectionError(
                "No Antigravity model selected; Avibe requires an explicit model and never falls back to the CLI default.",
                reason="model_selection_missing",
            )
            mark_prewrite_recovery_required(request.context, err.reason)
            await self._settle_turn_failure(None, None, request, err)
            return

        agent_session_id = self.ensure_agent_session_id(request) or ""
        session: _RuntimeSession | None = None
        turn: _ActiveTurn | None = None

        try:
            if (
                normalize_antigravity_auth_mode(self.antigravity_config) == "gemini-api-key"
                and not _config_str(self.antigravity_config, "api_key")
            ):
                raise AcpRpcError(ACP_AUTH_REQUIRED, "Gemini API key is not configured.")
            spec, launch_env, skill_catalog = await self._build_launch(
                request,
                runtime_key=runtime_key,
                resolved_model=resolved_model,
            )
            session = await self._get_or_create_session(
                request,
                runtime_key=runtime_key,
                agent_session_id=agent_session_id,
                spec=spec,
                launch_env=launch_env,
            )
            assert session.native_session_id is not None

            try:
                applied = await apply_model_and_effort(
                    session.client,
                    session.native_session_id,
                    session.config_options,
                    model=resolved_model,
                    effort=resolved_effort,
                    current_model=session.current_model,
                    current_effort=session.current_effort,
                    default_effort=session.default_efforts.get(resolved_model),
                    force_reapply=session.selection_dirty,
                )
            except BaseException as exc:
                self._invalidate_session_selection(session, exc)
                if isinstance(exc, Exception):
                    await self._persist_session_selection_state(session)
                else:
                    self._cache_session_selection_state(session)
                raise
            if resolved_effort is not None and applied.effort != resolved_effort:
                await emit_output(self.controller, request.context, Notice(text=i18n_t(
                    "antigravityNotice.effortAdjusted" if applied.effort else "antigravityNotice.noEffort",
                    self._language(), model=applied.model, effort=applied.effort or "",
                )))
            session.config_options = applied.config_options
            session.current_model = applied.model
            session.current_effort = applied.effort
            session.selection_dirty = False
            if applied.default_effort is not None:
                session.default_efforts[applied.model] = applied.default_effort
            if applied.config_options:
                await asyncio.to_thread(
                    record_antigravity_live_models,
                    self.antigravity_config,
                    applied.config_options,
                    auth_fingerprint=spec.auth_fingerprint,
                )
            await self._persist_session_selection_state(session)
        except AntigravityResumeUnavailableError as exc:
            mark_prewrite_recovery_required(request.context, "native_session_not_found")
            await self._settle_turn_failure(session, None, request, exc)
            return
        except AntigravityModelSelectionError as exc:
            mark_prewrite_recovery_required(request.context, exc.reason)
            await self._settle_turn_failure(session, None, request, exc)
            return
        except Exception as exc:
            _, _, prewrite_reason = self._classify_failure(exc)
            if prewrite_reason:
                mark_prewrite_recovery_required(request.context, prewrite_reason)
            await self._settle_turn_failure(session, None, request, exc)
            return

        input_metadata = request.input_metadata
        delivering_fork_handoff = False
        if session.pending_fork_handoff:
            handoff_text = session.pending_fork_handoff
            if input_metadata is None:
                input_metadata = AgentInputMetadata(handoff=handoff_text)
                delivering_fork_handoff = True
            elif not input_metadata.handoff:
                input_metadata = replace(input_metadata, handoff=handoff_text)
                delivering_fork_handoff = True

        prompt_blocks = await asyncio.to_thread(
            self._build_prompt_blocks_sync,
            request,
            input_metadata,
            supports_images=session.client.supports_images,
        )

        now = time.monotonic()
        turn = _ActiveTurn(
            request=request,
            runtime_key=runtime_key,
            native_session_id=session.native_session_id,
            turn_id=uuid.uuid4().hex,
            started_at=request.started_at,
        )
        session.active_turn = turn
        session.turn_started_at = now
        session.last_activity = now

        turn.prompt_task = asyncio.create_task(
            session.client.session_prompt(session.native_session_id, prompt_blocks, timeout=None)
        )
        turn.prompt_tasks.append(turn.prompt_task)
        turn.prompt_task.add_done_callback(self._consume_prompt_result)

        mark_backend_dispatch_attempted(request.context)
        self.mark_runtime_turn_started(request.context, activation_identity=session.activation)

        if skill_catalog is not None:
            from core.skill_observability import accept_catalog

            accept_catalog(self.controller, request.context, skill_catalog, backend=self.name)

        fork_handoff_cleared = False

        async def _mark_fork_handoff_delivered() -> None:
            nonlocal fork_handoff_cleared
            if not delivering_fork_handoff or fork_handoff_cleared:
                return
            fork_handoff_cleared = True
            session.pending_fork_handoff = None
            if session.agent_session_id:
                await asyncio.to_thread(clear_pending_fork_handoff, session.agent_session_id)

        try:
            response = await self._wait_prompt_chain(turn)
            turn.prompt_accepted = True
            await _mark_fork_handoff_delivered()
            raw_stop_reason = response.get("stopReason")
            stop_reason = raw_stop_reason.strip() if isinstance(raw_stop_reason, str) else ""
            if stop_reason == "end_turn" and "".join(turn.reply_chunks).strip():
                await self._settle_turn_done(session, turn)
            elif turn.cancelled or stop_reason == "cancelled":
                await self._settle_turn_stopped(turn)
            elif stop_reason == "end_turn":
                await self._settle_turn_done(session, turn)
            else:
                await self._settle_turn_incomplete(session, turn, stop_reason=stop_reason)
        except asyncio.CancelledError:
            if turn.cancelled:
                if turn.prompt_accepted:
                    await _mark_fork_handoff_delivered()
                await self._settle_turn_stopped(turn)
            elif session.intentional_teardown:
                if turn.prompt_accepted:
                    await _mark_fork_handoff_delivered()
                await self._settle_turn_teardown(turn)
            else:
                if turn.prompt_accepted:
                    session.pending_fork_handoff = None
                raise
        except (AcpTransportError, AcpRpcError) as exc:
            if turn.prompt_accepted:
                await _mark_fork_handoff_delivered()
            if turn.cancelled:
                await self._settle_turn_stopped(turn)
            elif session.intentional_teardown:
                await self._settle_turn_teardown(turn)
            else:
                await self._settle_turn_failure(session, turn, request, exc)
        except Exception as exc:
            if turn.prompt_accepted:
                await _mark_fork_handoff_delivered()
            await self._settle_turn_failure(session, turn, request, exc)
        finally:
            for pending_prompt in turn.prompt_tasks:
                if not pending_prompt.done():
                    pending_prompt.cancel()
            if session.active_turn is turn:
                session.active_turn = None
                session.turn_started_at = None
                session.last_activity = time.monotonic()

    @staticmethod
    def _complete_latest_reply(turn: _ActiveTurn) -> bool:
        prompt = turn.prompt_task
        if prompt is None or not prompt.done() or prompt.cancelled() or not "".join(turn.reply_chunks).strip():
            return False
        try:
            return prompt.result().get("stopReason") == "end_turn"
        except Exception:
            return False

    async def handle_stop(self, request: AgentRequest) -> bool:
        runtime_key = self.runtime_turn_key(request)
        session = self._sessions.get(runtime_key)
        if session is None or session.active_turn is None or session.active_turn.settled:
            request.stop_failure_reason = "not_active"
            return False

        turn = session.active_turn
        if self._complete_latest_reply(turn):
            await self._settle_turn_done(session, turn)
            return True
        turn.cancelled = True
        deadline = time.monotonic() + _STOP_CANCEL_GRACE_SECONDS
        prompt_task = turn.prompt_task
        cancel_timed_out = False

        if session.client.is_alive:
            try:
                await asyncio.wait_for(
                    session.client.session_cancel(turn.native_session_id),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                cancel_timed_out = True
                logger.warning("Antigravity session/cancel timed out during stop; closing session")
            except AcpTransportError as exc:
                logger.debug("Antigravity session/cancel failed during stop: %s", exc)

        if cancel_timed_out:
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
            await self._close_session(session, intentional=True, retire_activation=True)
        elif prompt_task is not None and not prompt_task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                prompt_task.cancel()
                await self._close_session(session, intentional=True, retire_activation=True)
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(prompt_task), timeout=remaining)
                except asyncio.TimeoutError:
                    prompt_task.cancel()
                    await self._close_session(session, intentional=True, retire_activation=True)
                except (AcpTransportError, AcpRpcError) as exc:
                    logger.debug("Antigravity prompt ended with error during stop: %s", exc)
                except asyncio.CancelledError:
                    if not prompt_task.cancelled():
                        raise

        if self._complete_latest_reply(turn):
            await self._settle_turn_done(session, turn)
        else:
            await self._settle_turn_stopped(turn)
        return True

    # ------------------------------------------------------------------
    # Native steering: one logical Turn owns a chain of ACP prompt requests.
    # ------------------------------------------------------------------

    @staticmethod
    def _consume_prompt_result(task: asyncio.Task[dict[str, Any]]) -> None:
        # Superseded requests have no terminal authority, including on late failure.
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _wait_prompt_chain(turn: _ActiveTurn) -> dict[str, Any]:
        while True:
            prompt = turn.prompt_task
            assert prompt is not None
            turn.prompt_changed.clear()
            changed = asyncio.create_task(turn.prompt_changed.wait())
            try:
                await asyncio.wait((prompt, changed), return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
            if prompt is not turn.prompt_task:
                continue
            return prompt.result()

    def _live_steer_turn(self, target: ActiveSteerTarget) -> tuple[_RuntimeSession, _ActiveTurn] | None:
        session = self._sessions.get(target.runtime_key)
        turn = session.active_turn if session else None
        if (
            self._shutting_down or session is None or session.retired or session.intentional_teardown
            or not session.client.is_alive or turn is None or turn.cancelled or turn.settled
            or turn.prompt_task is None or turn.prompt_task.done()
        ):
            return None
        if str((turn.request.context.platform_specific or {}).get("turn_token") or "") != target.logical_turn_id:
            return None
        return session, turn

    def _steer_unavailable_receipt(self, target: ActiveSteerTarget) -> SteerResult:
        session = self._sessions.get(target.runtime_key)
        if session is not None and (
            self._shutting_down or session.retired or session.intentional_teardown
            or not session.client.is_alive or (session.active_turn is not None and session.active_turn.cancelled)
        ):
            return steer_result(SteerOutcome.REFUSED, reason="runtime_closing", backend=self.name)
        return steer_result(SteerOutcome.NOT_ACTIVE, reason="not_active", backend=self.name)

    def steering_native_turn_id(self, target: ActiveSteerTarget) -> str | None:
        live = self._live_steer_turn(target)
        if live is None:
            return None
        session, turn = live
        return f"antigravity:{session.native_session_id}:{turn.turn_id}"

    @staticmethod
    def _steer_key(request: SteerRequest | SteerReconcileRequest) -> tuple[str, str, str, str]:
        return (request.target_session_id, request.expected_logical_turn_id,
                request.expected_native_turn_id, request.attempt_id)

    def _steer_receipt(self, attempt: _SteerAttempt) -> SteerResult:
        if attempt.prewrite_receipt is not None:
            return attempt.prewrite_receipt
        if attempt.write_started and attempt.predecessor.done() and not attempt.predecessor.cancelled():
            try:
                response = attempt.predecessor.result()
            except Exception:
                pass
            else:
                if response.get("stopReason") == "end_turn":
                    return steer_result(SteerOutcome.ACCEPTED, backend=self.name)
        return steer_result(SteerOutcome.UNKNOWN, reason="acceptance_unconfirmed", backend=self.name)

    def reconciliation_steer_target(self, request: SteerReconcileRequest) -> ActiveSteerTarget | None:
        attempt = self._steer_attempts.get(self._steer_key(request))
        return attempt.target if attempt else None

    async def steer_active_turn(self, request: SteerRequest, target: ActiveSteerTarget) -> SteerResult:
        if request.expected_logical_turn_id != target.logical_turn_id:
            return steer_result(SteerOutcome.REFUSED, reason="logical_turn_mismatch", backend=self.name)
        previous_attempt = self._steer_attempts.get(self._steer_key(request))
        if previous_attempt is not None:
            return self._steer_receipt(previous_attempt)
        live = self._live_steer_turn(target)
        if live is None:
            return self._steer_unavailable_receipt(target)
        if self.steering_native_turn_id(target) != request.expected_native_turn_id:
            return steer_result(SteerOutcome.REFUSED, reason="native_turn_mismatch", backend=self.name)
        if not request.attempt_id:
            return steer_result(SteerOutcome.REFUSED, reason="missing_attempt_identity", backend=self.name)
        session, turn = live
        # Use exactly the normal input renderer, including file references and image blocks.
        steered_request = replace(turn.request, message=request.text, user_message=request.text, files=list(request.files))
        try:
            blocks = await asyncio.to_thread(
                self._build_prompt_blocks_sync, steered_request, request.input_metadata,
                supports_images=session.client.supports_images,
            )
        except Exception:
            return steer_result(SteerOutcome.REFUSED, reason="unsupported_content", backend=self.name)
        async with turn.steer_lock:
            previous_attempt = self._steer_attempts.get(self._steer_key(request))
            if previous_attempt is not None:
                return self._steer_receipt(previous_attempt)
            current = self._live_steer_turn(target)
            if current is None or current[1] is not turn:
                return self._steer_unavailable_receipt(target)
            assert turn.prompt_task is not None
            attempt = _SteerAttempt(target, turn.prompt_task)
            self._steer_attempts[self._steer_key(request)] = attempt
            while len(self._steer_attempts) > _MAX_STEER_ATTEMPTS:
                self._steer_attempts.pop(next(iter(self._steer_attempts)))

            def claim_prompt() -> None:
                # Runs synchronously under AcpClient's write lock immediately before stdin.write.
                current = self._live_steer_turn(target)
                if current is None or current[1] is not turn or turn.prompt_task is not attempt.predecessor:
                    raise _SteerPrewriteRejected(self._steer_unavailable_receipt(target))
                attempt.write_started = True
                turn.prompt_task = attempt.prompt
                turn.prompt_changed.set()

            async def send_prompt() -> dict[str, Any]:
                try:
                    return await session.client.session_prompt(
                        turn.native_session_id, blocks, timeout=None, before_write=claim_prompt,
                    )
                except _SteerPrewriteRejected as exc:
                    attempt.prewrite_receipt = exc.receipt
                    return {}

            attempt.prompt = asyncio.create_task(send_prompt())
            turn.prompt_tasks.append(attempt.prompt)
            attempt.prompt.add_done_callback(self._consume_prompt_result)
            await asyncio.wait(
                (attempt.predecessor, attempt.prompt), timeout=_STEER_RECEIPT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not attempt.write_started and attempt.prewrite_receipt is None:
                attempt.prompt.cancel()
                attempt.prewrite_receipt = (
                    self._steer_unavailable_receipt(target) if attempt.predecessor.done()
                    else steer_result(SteerOutcome.REFUSED, reason="write_not_started", backend=self.name)
                )
            return self._steer_receipt(attempt)

    async def reconcile_steer_attempt(self, request: SteerReconcileRequest, target: ActiveSteerTarget) -> SteerResult:
        del target
        attempt = self._steer_attempts.get(self._steer_key(request))
        if attempt is None:
            return steer_result(SteerOutcome.UNKNOWN, reason="attempt_not_found", backend=self.name)
        return self._steer_receipt(attempt)

    # ------------------------------------------------------------------
    # AgentService, Runtime Ownership, and Lifecycle Parity Hooks
    # ------------------------------------------------------------------

    def runtime_turn_keys(self) -> set[str]:
        return set(self._sessions.keys())

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        agent_map = self.sessions.list_agent_sessions(session_key, self.name)
        session_bases = set(agent_map.keys()) if isinstance(agent_map, Mapping) else set()
        keys: set[str] = set()
        for runtime_key, session in self._sessions.items():
            if session.session_key == session_key or session.base_session_id in session_bases:
                keys.add(runtime_key)
        return keys

    def runtime_activation_identity_for_request(
        self,
        request: Any,
    ) -> RuntimeActivationIdentity | None:
        context = getattr(request, "context", None)
        payload = getattr(context, "platform_specific", None) or {}
        backend_key = str(payload.get("backend_composite_session_id") or "").strip()
        runtime_key = backend_key or str(getattr(request, "composite_session_id", "") or "").strip()
        if runtime_key:
            session = self._sessions.get(runtime_key)
            return session.activation if session is not None else None

        session_key = str(getattr(request, "session_key", "") or "").strip()
        identities = tuple(
            dict.fromkeys(
                session.activation
                for session in self._sessions.values()
                if session_key and session.session_key == session_key and session.activation is not None
            )
        )
        if len(identities) > 1:
            raise ValueError("ambiguous Antigravity Session route maps to live runtimes")
        return identities[0] if identities else None

    def runtime_activation_identity_for_session_binding(
        self,
        *,
        session_anchor: str,
        workdir: str | None,
    ) -> RuntimeActivationIdentity | None:
        normalized_anchor = str(session_anchor or "").strip()
        normalized_workdir = str(workdir or "").strip()
        if not normalized_anchor:
            return None
        anchor_sessions = [
            session
            for session in self._sessions.values()
            if session.base_session_id == normalized_anchor
        ]
        if not anchor_sessions:
            return None
        exact_sessions = [
            session
            for session in anchor_sessions
            if session.workdir == normalized_workdir
        ]
        if len(exact_sessions) != 1:
            raise ValueError("Antigravity Session binding is ambiguous or changed workdir")
        identity = exact_sessions[0].activation
        if identity is None:
            raise ValueError("Antigravity runtime generation is not attached")
        return identity

    def _ownership_known_keys(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        sessions = tuple(self._sessions.values())
        activity_keys = tuple(sorted({s.runtime_key for s in sessions if s.runtime_key}))
        route_keys = tuple(sorted({s.session_key for s in sessions if s.session_key}))
        return activity_keys, route_keys

    def _ownership_target(
        self,
        session: _RuntimeSession,
        *,
        known_activity_keys: tuple[str, ...],
        known_route_keys: tuple[str, ...],
    ) -> RuntimeResourceTarget | None:
        if (
            not session.base_session_id
            or not session.agent_session_id
            or not session.workdir
            or not session.session_key
            or not session.runtime_key
        ):
            logger.error(
                "Antigravity runtime ownership mapping is incomplete for resource=%s",
                session.runtime_key,
            )
            return None
        return RuntimeResourceTarget(
            backend=self.name,
            resource_key=session.runtime_key,
            bindings=(
                RuntimeSessionBinding(
                    session_id=session.agent_session_id,
                    session_anchor=session.base_session_id,
                    workdir=session.workdir,
                    activity_runtime_keys=(session.runtime_key,),
                    fallback_route_keys=(session.session_key,),
                ),
            ),
            known_activity_runtime_keys=known_activity_keys,
            known_fallback_route_keys=known_route_keys,
        )

    def _ownership_targets(
        self,
        sessions: Sequence[_RuntimeSession] | None = None,
    ) -> dict[str, RuntimeResourceTarget]:
        selected = tuple(sessions) if sessions is not None else tuple(self._sessions.values())
        known_activity_keys, known_route_keys = self._ownership_known_keys()
        targets: dict[str, RuntimeResourceTarget] = {}
        for session in selected:
            target = self._ownership_target(
                session,
                known_activity_keys=known_activity_keys,
                known_route_keys=known_route_keys,
            )
            if target is not None:
                targets[session.runtime_key] = target
        return targets

    def runtime_ownership_snapshots(self) -> tuple[Any, ...] | None:
        sessions = tuple(self._sessions.values())
        if not sessions:
            return ()
        targets_by_key = self._ownership_targets(sessions)
        if len(targets_by_key) != len(sessions):
            return None
        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot_many = getattr(provider, "snapshot_many", None)
        if not callable(snapshot_many):
            logger.error("Antigravity runtime ownership batch provider unavailable")
            return None
        results = tuple(snapshot_many(tuple(targets_by_key.values())))
        if len(results) != len(targets_by_key):
            logger.error("Antigravity runtime ownership batch returned incomplete results")
            return None
        for result in results:
            wake_runtime_ownership(self.controller, result)
        return results

    def runtime_has_active_turns(self) -> bool:
        return any(
            session.active_turn is not None and not session.active_turn.settled
            for session in self._sessions.values()
        )

    def backend_alive(self, context: Any) -> Optional[bool]:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        if not runtime_key:
            return None
        session = self._sessions.get(runtime_key)
        if session is None:
            return None
        return not session.retired and session.client.is_alive

    def capture_backend_liveness(self, context: Any) -> Callable[[], Optional[bool]]:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        session = self._sessions.get(runtime_key) if runtime_key else None
        if session is None:
            return lambda: None
        return lambda: not session.retired and session.client.is_alive

    def capture_backend_exit_failure(
        self,
        context: Any,
    ) -> Callable[[], tuple[str, str] | None] | None:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        session = self._sessions.get(runtime_key) if runtime_key else None
        if session is None:
            return None
        return lambda: session.exit_failure

    def record_runtime_turn_start(
        self,
        *,
        runtime_key: str,
        request: AgentRequest | None,
    ) -> None:
        del request
        session = self._sessions.get(runtime_key)
        if session is None:
            return
        now = time.monotonic()
        session.turn_started_at = now
        session.last_activity = now

    async def release_native_runtime(self, request: AgentRequest) -> None:
        runtime_key = self.runtime_turn_key(request)
        targets = [
            key
            for key, session in list(self._sessions.items())
            if key == runtime_key or session.base_session_id == request.base_session_id
        ]
        for key in targets:
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is not None:
                    await self._close_session(session, intentional=True, retire_activation=True)

    async def end_session(self, base_session_id: str) -> dict[str, bool]:
        matching_keys = [
            key
            for key, session in list(self._sessions.items())
            if session.base_session_id == base_session_id
        ]
        interrupted = False
        process_killed = False
        for key in matching_keys:
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is None:
                    continue
                turn = session.active_turn
                if turn is not None and not turn.settled:
                    interrupted = True
                    turn.cancelled = True
                    if session.client.is_alive:
                        try:
                            await session.client.session_cancel(turn.native_session_id)
                        except AcpTransportError as exc:
                            logger.debug("Antigravity session/cancel failed during end_session: %s", exc)
                    if turn.prompt_task is not None and not turn.prompt_task.done():
                        turn.prompt_task.cancel()
                    await self._settle_turn_stopped(turn)
                killed = await self._close_session(session, intentional=True, retire_activation=True)
                process_killed = process_killed or killed
            self._session_locks.pop(key, None)
        return {"interrupted": interrupted, "process_killed": process_killed}

    async def clear_sessions(self, session_key: str) -> int:
        keys_to_clear = self.runtime_turn_keys_for_session_key(session_key)
        agent_map = self.sessions.list_agent_sessions(session_key, self.name)
        stored_count = len(agent_map) if isinstance(agent_map, Mapping) else 0
        self.sessions.clear_agent_sessions(session_key, self.name)

        cleared = 0
        for key in keys_to_clear:
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is not None:
                    await self._close_session(session, intentional=True, retire_activation=True)
                    cleared += 1
            self._session_locks.pop(key, None)
        return cleared or stored_count

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        del session_key
        keys = [
            key
            for key, session in list(self._sessions.items())
            if session.base_session_id == base_session_id and session.workdir == working_path
        ]
        for key in keys:
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is not None:
                    await self._close_session(session, intentional=True, retire_activation=True)

    def _idle_eviction_after(self, session: _RuntimeSession, idle_timeout: float) -> float | None:
        if session.spec.renewal_epoch != self._renewal_epoch:
            return 0.0
        return idle_timeout if idle_timeout > 0 else None

    async def evict_idle_transports(self, idle_timeout: float) -> int:
        """Evict idle or renewed-epoch runtimes only when durable ownership permits."""
        candidates = [
            session
            for session in list(self._sessions.values())
            if self._idle_eviction_after(session, idle_timeout) is not None
        ]
        if not candidates:
            return 0

        provider = getattr(self.controller, "runtime_ownership", None)
        snapshot_many = getattr(provider, "snapshot_many", None)
        snapshot_one = getattr(provider, "snapshot", None)
        if not callable(snapshot_many) or not callable(snapshot_one):
            logger.error("Antigravity runtime ownership provider unavailable; skipping idle eviction")
            return 0

        targets_by_key = self._ownership_targets(candidates)
        if not targets_by_key:
            return 0

        results = tuple(await asyncio.to_thread(snapshot_many, tuple(targets_by_key.values())))
        ownership_by_key = {result.resource_key: result for result in results}
        if len(ownership_by_key) != len(targets_by_key):
            logger.error("Antigravity runtime ownership batch returned incomplete results")
            return 0
        for result in results:
            wake_runtime_ownership(self.controller, result)

        now = time.monotonic()
        expired_keys: list[str] = []
        for session in candidates:
            if session.active_turn is not None and not session.active_turn.settled:
                continue
            ownership = ownership_by_key.get(session.runtime_key)
            if ownership is None or ownership.blocks_reclamation:
                continue
            evict_after = self._idle_eviction_after(session, idle_timeout)
            if evict_after is not None and (now - session.last_activity) >= evict_after:
                expired_keys.append(session.runtime_key)

        evicted = 0
        registry = self._activation_registry()
        for key in expired_keys:
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is None or (session.active_turn is not None and not session.active_turn.settled):
                    continue
                target = self._ownership_targets((session,)).get(key)
                if target is None:
                    continue

                reservation = None
                if registry is not None and session.activation is not None:
                    reservation = registry.reserve_retirement(session.activation)
                    if reservation is None:
                        continue

                retired = False
                try:
                    ownership = await asyncio.to_thread(snapshot_one, target)
                    wake_runtime_ownership(self.controller, ownership)
                    evict_after = self._idle_eviction_after(session, idle_timeout)
                    allowed = bool(
                        self._sessions.get(key) is session
                        and (session.active_turn is None or session.active_turn.settled)
                        and not ownership.blocks_reclamation
                        and evict_after is not None
                        and (time.monotonic() - session.last_activity) >= evict_after
                    )
                    if reservation is None:
                        retired = allowed
                    else:
                        retired = bool(registry.finish_retirement(reservation, retire=allowed) and allowed)
                        reservation = None
                finally:
                    if reservation is not None:
                        registry.finish_retirement(reservation, retire=False)

                if not retired:
                    continue
                await self._close_session(session, intentional=True, retire_activation=False)
                evicted += 1
        return evicted

    async def adopt_model_hub_catalog(self) -> None:
        """No-op for Direct-only Antigravity: each turn selects model/effort at admission."""

    async def renew_runtime(self, antigravity_config: Any, *, config_save: bool = False) -> None:
        """Adopt persisted runtime config without interrupting live turns."""
        previous = self.antigravity_config
        self.antigravity_config = antigravity_config
        if hasattr(self.controller.config, "antigravity"):
            self.controller.config.antigravity = antigravity_config
        if config_save and _launch_config_snapshot(previous) == _launch_config_snapshot(antigravity_config):
            return
        self._renewal_epoch += 1

    async def reap_runtime_generations(self) -> None:
        """Reclaim idle sessions belonging to superseded renewal epochs."""
        await self.evict_idle_transports(0.0)

    async def refresh_auth_state(self) -> None:
        """Stop all live runtimes so subsequent turns launch with fresh credentials."""
        base_session_ids = {session.base_session_id for session in self._sessions.values()}
        turn_manager = getattr(self.controller, "session_turns", None)
        release = getattr(turn_manager, "release_for_backend_refresh", None)
        if callable(release) and base_session_ids:
            await release(backend=self.name, base_session_ids=base_session_ids)

        for key in list(self._sessions.keys()):
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is None:
                    continue
                turn = session.active_turn
                session.intentional_teardown = True
                if turn is not None and not turn.settled and turn.prompt_task is not None and not turn.prompt_task.done():
                    turn.prompt_task.cancel()
                await self._close_session(session, intentional=True, retire_activation=True)

    async def refresh_runtime_config(self, antigravity_config: Any) -> None:
        """Reload persisted runtime config and retire cached sessions."""
        self.antigravity_config = antigravity_config
        if hasattr(self.controller.config, "antigravity"):
            self.controller.config.antigravity = antigravity_config
        self._renewal_epoch += 1
        await self.refresh_auth_state()

    async def shutdown_runtime(self, settle_reason: str | None = None) -> None:
        """Shut down all Antigravity subprocesses cleanly."""
        self._shutting_down = True
        for key in list(self._sessions.keys()):
            async with self._session_lock(key):
                session = self._sessions.get(key)
                if session is None:
                    continue
                session.intentional_teardown = True
                turn = session.active_turn
                if turn is not None and not turn.settled:
                    if session.client.is_alive:
                        try:
                            await session.client.session_cancel(turn.native_session_id)
                        except AcpTransportError as exc:
                            logger.debug("Antigravity session/cancel failed during shutdown: %s", exc)
                    if turn.prompt_task is not None and not turn.prompt_task.done():
                        turn.prompt_task.cancel()
                    if settle_reason is not None:
                        await self._settle_turn_teardown(turn)
                await self._close_session(session, intentional=True, retire_activation=True)
