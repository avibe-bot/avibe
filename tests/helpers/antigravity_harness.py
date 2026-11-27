"""Shared hermetic test harness and fake `agy` CLI shim for Antigravity tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from modules.agents.base import AgentRequest
    from modules.im.base import FileAttachment

# The shim runs this file directly from a test workspace.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.helpers.fake_acp_server import FakeAcpServer

HARNESS_PATH = Path(__file__).resolve()
SENTINEL_SESSION_TITLE = "TOP-SECRET-EXISTING-SESSION-TITLE-999"

_AUTH_DEFAULT_METHODS = [
    {"id": "oauth-personal", "name": "Log in with Google"},
    {"id": "oauth-business", "name": "Log in with Gemini Enterprise"},
    {"id": "gemini-api-key", "name": "Gemini API key"},
    {"id": "agent-platform", "name": "Gemini Enterprise Agent Platform"},
]
_AUTH_DEFAULT_CONFIG_OPTIONS = [
    {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "gemini-4-neon",
        "options": [{"value": "gemini-4-neon", "name": "Gemini 4 Neon"}, {"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"}],
    },
]


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def seed_fork_source_session(
    workdir: Path, *, native_id: str, worker_name: str, native_session_id: str, messages: list[tuple[str, str, str]], title: str = "Source Antigravity Session"
) -> tuple[Path, str, str]:
    from config import paths
    from core.vibe_agents import VibeAgentStore
    from storage import messages_service
    from storage.agent_session_rows import create_agent_session_row
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.sessions_service import SQLiteSessionsService
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(primary_platform="avibe")
    db_path = paths.get_sqlite_state_path()
    SQLiteSessionsService(db_path).close()
    store = VibeAgentStore(db_path)
    try:
        worker = store.create(name=worker_name, backend="antigravity", model="model-alpha")
    finally:
        store.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id=native_id, now="2026-11-24T00:00:00Z")
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id, enabled=1, role=None, workdir=str(workdir), agent_name=worker_name, agent_backend="antigravity",
                    agent_variant="antigravity", model="model-alpha", reasoning_effort=None, require_mention=None,
                    settings_version=1, settings_json="{}", created_at="2026-11-24T00:00:00Z", updated_at="2026-11-24T00:00:00Z",
                )
            )
            source_id = create_agent_session_row(
                conn, scope_id=scope_id, session_anchor=None, agent_backend="antigravity", agent_variant="antigravity",
                agent_id=worker.id, agent_name=worker_name, model="model-alpha", workdir=str(workdir), native_session_id=native_session_id, title=title,
            )
            for author, msg_type, text in messages:
                messages_service.append(conn, scope_id=scope_id, session_id=source_id, platform="avibe", author=author, message_type=msg_type, text=text)
    finally:
        engine.dispose()
    return db_path, scope_id, source_id


def read_session_row(session_id: str, db_path: Path | None = None) -> dict[str, Any]:
    from sqlalchemy import select

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    engine = create_sqlite_engine(db_path) if db_path is not None else create_sqlite_engine()
    try:
        with engine.connect() as conn:
            row = dict(conn.execute(select(agent_sessions).where(agent_sessions.c.id == session_id)).mappings().one())
    finally:
        engine.dispose()
    row["metadata"] = json.loads(row.get("metadata_json") or "{}")
    return row


def create_fake_agy_shim(
    tmp_path: Path,
    *,
    state_file: Path,
    launch_log: Path,
    rpc_log: Path | None = None,
    server_config: dict[str, Any] | None = None,
    config_file: Path | None = None,
    auth_mode: bool = False,
    name: str = "fake-agy",
) -> Path:
    cmd = [sys.executable, "-u", str(HARNESS_PATH), "--launch-log", str(launch_log), "--state-file", str(state_file)]
    if rpc_log is not None:
        cmd.extend(["--rpc-log", str(rpc_log)])
    if config_file is not None:
        cmd.extend(["--config-file", str(config_file)])
    elif server_config is not None:
        cmd.extend(["--config-json", json.dumps(server_config, separators=(",", ":"))])
    if auth_mode:
        cmd.append("--auth-mode")
    shim = tmp_path / name
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f'#!/bin/sh\nexec {" ".join(shlex.quote(p) for p in cmd)} -- "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return shim


class FakeAgyAuthFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fake-agy"
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_log, self.rpc_log = self.root / "audit.jsonl", self.root / "rpc.jsonl"
        self.state_file, self.config_file = self.root / "state.json", self.root / "config.json"
        self.binary = create_fake_agy_shim(
            self.root, state_file=self.state_file, launch_log=self.audit_log, rpc_log=self.rpc_log, config_file=self.config_file, auth_mode=True, name="agy"
        )
        self.set_state(authenticated=True)
        self.set_config({})

    def set_state(self, *, authenticated: bool, include_sentinel_session: bool = True) -> None:
        sessions = (
            {"sess-existing": {"sessionId": "sess-existing", "cwd": str(self.root), "mcpServers": [], "additionalDirectories": [], "configOptions": [], "mode": "code", "model": "gemini-4-neon", "title": SENTINEL_SESSION_TITLE, "history": []}}
            if include_sentinel_session
            else {}
        )
        self.state_file.write_text(json.dumps({"authenticated": authenticated, "next_session_num": 2, "sessions": sessions}, indent=2), encoding="utf-8")

    def set_config(self, payload: dict[str, Any]) -> None:
        self.config_file.write_text(json.dumps(payload), encoding="utf-8")

    def clear_logs(self) -> None:
        self.audit_log.unlink(missing_ok=True)
        self.rpc_log.unlink(missing_ok=True)

    def invocations(self) -> list[dict[str, Any]]:
        return read_jsonl(self.audit_log)

    def rpc_methods(self) -> list[str]:
        return [e["method"] for e in read_jsonl(self.rpc_log) if "method" in e]


class FakeSessions:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], str] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def ensure_agent_session_id(self, session_key: str, agent_name: str, anchor: str, **kwargs: Any) -> str:
        key = (session_key, agent_name, anchor)
        if key not in self._rows:
            self._seq += 1
            sid = f"ses_{self._seq}"
            self._rows[key] = sid
            self._by_id[sid] = {"id": sid, "native_session_id": "", **kwargs}
        return self._rows[key]

    def bind_agent_session(self, session_key: str, agent_name: str, anchor: str, native_session_id: str, **kwargs: Any) -> str:
        sid = self.ensure_agent_session_id(session_key, agent_name, anchor, **kwargs)
        self._by_id[sid]["native_session_id"] = native_session_id
        return sid

    def bind_agent_session_by_id(self, agent_session_id: str, native_session_id: str, **kwargs: Any) -> str:
        self._by_id.setdefault(agent_session_id, {"id": agent_session_id}).update({"native_session_id": native_session_id, **kwargs})
        return agent_session_id

    def get_agent_session_mapping(self, session_key: str, agent_name: str, anchor: str) -> str | None:
        sid = self._rows.get((session_key, agent_name, anchor))
        return (self._by_id.get(sid, {}).get("native_session_id") or None) if sid else None

    def get_agent_session_id(self, session_key: str, anchor: str, agent_name: str) -> str | None:
        return self.get_agent_session_mapping(session_key, agent_name, anchor)

    def list_agent_sessions(self, session_key: str, agent_name: str) -> dict[str, str]:
        return {a: self._by_id[s]["native_session_id"] for (sk, b, a), s in self._rows.items() if sk == session_key and b == agent_name and self._by_id.get(s, {}).get("native_session_id")}

    def clear_agent_sessions(self, session_key: str, agent_name: str) -> None:
        for k in [k for k in self._rows if k[0] == session_key and k[1] == agent_name]:
            self._by_id.pop(self._rows.pop(k), None)


class FakeOwnershipProvider:
    def __init__(self, *, blocks_reclamation: bool = False) -> None:
        self.blocks_reclamation = blocks_reclamation
        self.calls: list[tuple[str, ...]] = []

    def _make_snapshot(self, target: Any) -> Any:
        from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition

        return RuntimeTargetOwnershipSnapshot(
            backend=target.backend, resource_key=target.resource_key, activity_runtime_keys=tuple(target.known_activity_runtime_keys),
            sessions=(), sessionless_active_activity_ids=(), sessionless_fallback_run_ids=(),
            disposition=SessionRuntimeDisposition.ACTIVE if self.blocks_reclamation else SessionRuntimeDisposition.RECLAIMABLE,
        )

    def snapshot_many(self, targets: tuple[Any, ...]) -> tuple[Any, ...]:
        self.calls.append(tuple(t.resource_key for t in targets))
        return tuple(self._make_snapshot(t) for t in targets)

    def snapshot(self, target: Any) -> Any:
        self.calls.append((target.resource_key,))
        return self._make_snapshot(target)


class _DummyFormatter:
    def format_result_footer(self, subtype: str, duration_ms: int, *, token_field: str = "") -> str:
        return f"{subtype}:{duration_ms}ms:{token_field}"

    def escape_special_chars(self, text: str) -> str:
        return text


class FakeIMClient:
    def __init__(self) -> None:
        self.formatter = _DummyFormatter()
        self.prepare_turn_context = AsyncMock(side_effect=lambda ctx, _src: ctx)
        self.add_reaction = AsyncMock()
        self.sent_plain: list[str] = []
        self.sent_buttons: list[tuple[str, str]] = []

    async def send_message(self, _ctx: Any, text: str) -> str:
        self.sent_plain.append(text)
        return f"m-{len(self.sent_plain)}"

    async def send_message_with_buttons(self, _ctx: Any, text: str, keyboard: Any) -> str:
        self.sent_buttons.append((text, keyboard.buttons[0][0].callback_data))
        return f"mb-{len(self.sent_buttons)}"


@dataclass
class FakeController:
    config: Any
    sessions: Any = field(default_factory=FakeSessions)
    runtime_activation: Any = None
    runtime_ownership: FakeOwnershipProvider = field(default_factory=FakeOwnershipProvider)
    emitted: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    token_notes: list[tuple[Any, int]] = field(default_factory=list)
    token_noted: asyncio.Event = field(default_factory=asyncio.Event)
    token_totals: dict[str, int] = field(default_factory=dict)
    primary_platform: str = "slack"

    def __post_init__(self) -> None:
        if self.runtime_activation is None:
            from core.runtime_activation import RuntimeActivationRegistry

            self.runtime_activation = RuntimeActivationRegistry()
        self.settings_manager = self.sessions
        self.im_client = FakeIMClient()
        self.agent_service: Any = SimpleNamespace(agents={})
        self.agent_auth_service = None
        self.processing_indicator: Any = None

    async def emit_agent_message(self, _context: Any, message_type: str, text: str, **options: Any) -> str:
        self.emitted.append((message_type, text, dict(options)))
        return f"msg-{len(self.emitted)}"

    def events(self, message_type: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [e for e in self.emitted if e[0] == message_type]

    def note_session_tokens(self, context: Any, *, total: int) -> None:
        self.token_noted.set()
        self.token_notes.append((context, total))
        self.token_totals[f"{context.channel_id}:{context.thread_id or context.channel_id}"] = int(total)

    def session_token_field(self, context: Any) -> str:
        total = self.token_totals.get(f"{context.channel_id}:{context.thread_id or context.channel_id}", 0)
        return f"{total} tok" if total > 0 else ""


def antigravity_config(cli_path: Path | str, **overrides: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "enabled": True, "cli_path": str(cli_path), "auth_mode": "oauth-personal", "api_key": None, "base_url": None,
            "gcp_project": None, "gcp_location": None, "idle_timeout_seconds": 600, **overrides,
        }
    )


def make_native_provider(*, cli_path="agy", config=None):
    from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider

    provider = AntigravityNativeSessionProvider()
    provider._effective_config = lambda: config if config is not None else antigravity_config(cli_path)
    return provider


def app_config(agy_cfg: Any, *, show_duration: bool = False, language: str = "en") -> SimpleNamespace:
    return SimpleNamespace(antigravity=agy_cfg, language=language, show_duration=show_duration, include_time_info=False, include_user_info=False, reply_enhancements=False)


def make_request(
    *,
    message: str,
    workdir: Path,
    base_session_id: str = "slack_100.1",
    session_key: str = "slack::channel::C123",
    user_id: str = "U1",
    message_id: str = "M1",
    platform: str = "slack",
    model: str | None = "model-alpha",
    effort: str | None = None,
    subagent_name: str | None = None,
    files: list[FileAttachment] | None = None,
    platform_specific: dict[str, Any] | None = None,
) -> AgentRequest:
    from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest
    from modules.im import MessageContext

    composite = f"{base_session_id}:{workdir}"
    ps = {AGENT_RUNTIME_TURN_KEY: composite, **(platform_specific or {})}
    ctx = MessageContext(user_id=user_id, channel_id="C123", platform=platform, thread_id="100.1", message_id=message_id, platform_specific=ps, files=files)
    return AgentRequest(
        context=ctx, message=message, user_message=message, working_path=str(workdir), base_session_id=base_session_id,
        composite_session_id=composite, session_key=session_key, vibe_agent_model=model, vibe_agent_reasoning_effort=effort, subagent_name=subagent_name, files=files,
    )


@dataclass
class AntigravityHarness:
    workdir: Path
    state_file: Path
    launch_log: Path
    rpc_log: Path | None
    rules_dir: Path
    shim: Path
    agy_cfg: SimpleNamespace
    controller: FakeController
    agent: Any

    turns: list[asyncio.Task] = field(default_factory=list)

    def start_turn(self, request: AgentRequest) -> asyncio.Task:
        # A lost protocol event must unwind the test even if both peers are waiting.
        # This is a deadlock guard for the whole turn, not a readiness deadline.
        task = asyncio.create_task(asyncio.wait_for(self.agent.handle_message(request), timeout=60))
        self.turns.append(task)
        return task

    async def wait_for_signal(self, signal: asyncio.Event, turn: asyncio.Task) -> None:
        waiter = asyncio.create_task(signal.wait())
        try:
            await asyncio.wait((waiter, turn), return_when=asyncio.FIRST_COMPLETED)
            if turn.done():
                await turn  # Surface unexpected failure rather than waiting for a missing signal.
                raise AssertionError(f"Turn finished before checkpoint: {self.controller.events('result')}")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def launches(self) -> list[dict[str, Any]]:
        return read_jsonl(self.launch_log)

    def rpcs(self, method: str | None = None) -> list[dict[str, Any]]:
        rows = read_jsonl(self.rpc_log) if self.rpc_log is not None else []
        return [m for m in rows if m.get("method") == method] if method else rows

    async def __aenter__(self) -> AntigravityHarness:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self.agent.shutdown_runtime()
        finally:
            for task in self.turns:
                task.cancel()
            await asyncio.gather(*self.turns, return_exceptions=True)


def make_agent_harness(
    tmp_path: Path,
    *,
    workdir: Path | None = None,
    record_rpc: bool = False,
    server_config: dict[str, Any] | None = None,
    cli_path: Path | str | None = None,
    sessions: Any = None,
    ownership: FakeOwnershipProvider | None = None,
    show_duration: bool = False,
    language: str = "en",
    name: str = "fake-agy",
    **cfg_overrides: Any,
) -> AntigravityHarness:
    from modules.agents.antigravity.agent import AntigravityAgent

    repo = workdir or (tmp_path / "repo")
    repo.mkdir(parents=True, exist_ok=True)
    state_file, launch_log = tmp_path / f"{name}-state.json", tmp_path / f"{name}-launches.jsonl"
    rpc_log = (tmp_path / f"{name}-rpc.jsonl") if record_rpc else None
    rules_dir = tmp_path / f"{name}-rules"
    shim = Path(cli_path) if cli_path is not None else create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log, rpc_log=rpc_log, server_config=server_config, name=name)
    agy_cfg = antigravity_config(shim, **cfg_overrides)
    controller = FakeController(app_config(agy_cfg, show_duration=show_duration, language=language), sessions=sessions if sessions is not None else FakeSessions(), runtime_ownership=ownership or FakeOwnershipProvider())
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=rules_dir)
    controller.agent_service.agents["antigravity"] = agent
    return AntigravityHarness(repo, state_file, launch_log, rpc_log, rules_dir, shim, agy_cfg, controller, agent)


def _run_login(cfg: dict[str, Any], state_file: Path) -> int:
    if cfg.get("business_unsupported"):
        sys.stdout.write("Interactive browser login required for Gemini Enterprise.\n")
        sys.stdout.flush()
        return 1
    sys.stdout.write("Open this URL to sign in:\nhttps://accounts.google.com/o/oauth2/auth?client_id=agy&state=fixture-state\nEnter authorization code: ")
    sys.stdout.flush()
    if sys.stdin.readline().strip() != cfg.get("expected_code", "valid-agy-code"):
        sys.stdout.write("Error: invalid authorization code\n")
        sys.stdout.flush()
        return 1
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"authenticated": False, "next_session_num": 1, "sessions": {}}
    state["authenticated"] = True
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    sys.stdout.write("Login successful.\n")
    sys.stdout.flush()
    return 0


def _run_acp(cfg: dict[str, Any], state_file: Path, rpc_log: Path | None, auth_mode: bool) -> int:
    class _AuditingServer(FakeAcpServer):
        def _dispatch(self, msg: dict[str, Any]) -> None:
            if rpc_log is not None and "method" in msg:
                _append_jsonl(rpc_log, msg)
            super()._dispatch(msg)

        def _handle_sync_request(self, req_id: int | str, method: str, params: dict[str, Any]) -> None:
            if auth_mode and method == "authenticate":
                mid = params.get("methodId")
                if mid == "gemini-api-key" and os.environ.get("GEMINI_API_KEY", "") != cfg.get("expected_api_key", "AIza-fixture-key-1234"):
                    return self._send_error(req_id, -32000, "API key not valid")
                if mid == "agent-platform" and not os.environ.get("GOOGLE_CLOUD_PROJECT") and cfg.get("require_gcp_project"):
                    return self._send_error(req_id, -32000, "Authentication required: missing project")
            super()._handle_sync_request(req_id, method, params)

    server_cfg = dict(cfg.get("acp_config") or {}) if auth_mode else dict(cfg)
    server_cfg.setdefault("authMethods", _AUTH_DEFAULT_METHODS)
    if auth_mode:
        server_cfg.setdefault("configOptions", _AUTH_DEFAULT_CONFIG_OPTIONS)
    # Concurrent peers can opt out of the single-writer JSON fixture.
    peer_state = None if server_cfg.get("inMemoryState") else state_file
    asyncio.run(_AuditingServer(state_file=peer_state, config=server_cfg).run())
    return 0


def _cli_main(raw_argv: list[str]) -> int:
    split_at = raw_argv.index("--") if "--" in raw_argv else len(raw_argv)
    parser = argparse.ArgumentParser()
    for flag in ("--launch-log", "--state-file"):
        parser.add_argument(flag, required=True)
    for flag in ("--rpc-log", "--config-file", "--config-json"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--auth-mode", action="store_true")
    opts = parser.parse_args(raw_argv[:split_at])
    agy_argv = raw_argv[split_at + 1 :] if split_at < len(raw_argv) else []

    _append_jsonl(Path(opts.launch_log), {"argv": agy_argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()})
    if agy_argv == ["--version"]:
        sys.stdout.write("1.5.16\n")
        return 0
    cfg = json.loads(Path(opts.config_file).read_text(encoding="utf-8")) if opts.config_file and Path(opts.config_file).exists() else (json.loads(opts.config_json) if opts.config_json else {})
    if agy_argv and agy_argv[0] == "acp":
        sf = Path(opts.state_file)
        return _run_login(cfg, sf) if "--login" in agy_argv else _run_acp(cfg, sf, Path(opts.rpc_log) if opts.rpc_log else None, opts.auth_mode)
    sys.stderr.write(f"Unexpected argv: {agy_argv}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
