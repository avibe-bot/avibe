"""Shared hermetic test harness and fake `agy` CLI shim for Antigravity tests.

Can also be executed directly by generated shell shims to emulate `agy`
(`--version`, `acp --login`, and `acp` backed by ``FakeAcpServer``) with
argv/env launch auditing and optional JSON-RPC frame recording.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
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
        "options": [
            {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
            {"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"},
        ],
    },
]


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_acp_capabilities(*, fork: bool = True, image: bool = True, logout: bool = True) -> dict[str, Any]:
    session_caps: dict[str, Any] = {"resume": {}, "list": {}, "close": {}, "additionalDirectories": {}}
    if fork:
        session_caps["fork"] = {}
    caps: dict[str, Any] = {
        "loadSession": True,
        "promptCapabilities": {"image": image, "embeddedContext": True},
        "sessionCapabilities": session_caps,
    }
    if logout:
        caps["auth"] = {"logout": {}}
    return caps


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
    """Create an executable shim invoking this module's fake `agy` CLI runner."""
    cmd = [
        sys.executable,
        "-u",
        str(HARNESS_PATH),
        "--launch-log",
        str(launch_log),
        "--state-file",
        str(state_file),
    ]
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
    quoted = " ".join(shlex.quote(part) for part in cmd)
    shim.write_text(f'#!/bin/sh\nexec {quoted} -- "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return shim


class FakeAgyAuthFixture:
    """Hermetic fake `agy` binary supporting `--version`, `acp --login`, and auditing ACP."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fake-agy"
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_log = self.root / "audit.jsonl"
        self.rpc_log = self.root / "rpc.jsonl"
        self.state_file = self.root / "state.json"
        self.config_file = self.root / "config.json"
        self.binary = create_fake_agy_shim(
            self.root,
            state_file=self.state_file,
            launch_log=self.audit_log,
            rpc_log=self.rpc_log,
            config_file=self.config_file,
            auth_mode=True,
            name="agy",
        )
        self.set_state(authenticated=True)
        self.set_config({})

    def set_state(self, *, authenticated: bool, include_sentinel_session: bool = True) -> None:
        sessions = {}
        if include_sentinel_session:
            sessions["sess-existing"] = {
                "sessionId": "sess-existing",
                "cwd": str(self.root),
                "mcpServers": [],
                "additionalDirectories": [],
                "configOptions": [],
                "mode": "code",
                "model": "gemini-4-neon",
                "title": SENTINEL_SESSION_TITLE,
                "history": [],
            }
        self.state_file.write_text(
            json.dumps({"authenticated": authenticated, "next_session_num": 2, "sessions": sessions}, indent=2),
            encoding="utf-8",
        )

    def set_config(self, payload: dict[str, Any]) -> None:
        self.config_file.write_text(json.dumps(payload), encoding="utf-8")

    def clear_logs(self) -> None:
        self.audit_log.unlink(missing_ok=True)
        self.rpc_log.unlink(missing_ok=True)

    def invocations(self) -> list[dict[str, Any]]:
        return read_jsonl(self.audit_log)

    def rpc_methods(self) -> list[str]:
        return [entry["method"] for entry in read_jsonl(self.rpc_log) if "method" in entry]


class FakeSessions:
    """Minimal in-memory SessionsFacade store for hermetic Antigravity tests."""

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

    def bind_agent_session(
        self, session_key: str, agent_name: str, anchor: str, native_session_id: str, **kwargs: Any
    ) -> str:
        sid = self.ensure_agent_session_id(session_key, agent_name, anchor, **kwargs)
        self._by_id[sid]["native_session_id"] = native_session_id
        return sid

    def bind_agent_session_by_id(self, agent_session_id: str, native_session_id: str, **kwargs: Any) -> str:
        row = self._by_id.setdefault(agent_session_id, {"id": agent_session_id})
        row["native_session_id"] = native_session_id
        row.update(kwargs)
        return agent_session_id

    def get_agent_session_mapping(self, session_key: str, agent_name: str, anchor: str) -> str | None:
        sid = self._rows.get((session_key, agent_name, anchor))
        return (self._by_id.get(sid, {}).get("native_session_id") or None) if sid else None

    def get_agent_session_id(self, session_key: str, anchor: str, agent_name: str) -> str | None:
        return self.get_agent_session_mapping(session_key, agent_name, anchor)

    def list_agent_sessions(self, session_key: str, agent_name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for (sk, backend, anchor), sid in self._rows.items():
            if sk == session_key and backend == agent_name:
                native = self._by_id.get(sid, {}).get("native_session_id")
                if native:
                    out[anchor] = native
        return out

    def clear_agent_sessions(self, session_key: str, agent_name: str) -> None:
        for key in list(self._rows):
            if key[0] == session_key and key[1] == agent_name:
                self._by_id.pop(self._rows.pop(key), None)


class FakeOwnershipProvider:
    def __init__(self, *, blocks_reclamation: bool = False) -> None:
        self.blocks_reclamation = blocks_reclamation
        self.calls: list[tuple[str, ...]] = []

    def _make_snapshot(self, target: Any) -> Any:
        from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition

        disposition = (
            SessionRuntimeDisposition.ACTIVE
            if self.blocks_reclamation
            else SessionRuntimeDisposition.RECLAIMABLE
        )
        return RuntimeTargetOwnershipSnapshot(
            backend=target.backend,
            resource_key=target.resource_key,
            activity_runtime_keys=tuple(target.known_activity_runtime_keys),
            sessions=(),
            sessionless_active_activity_ids=(),
            sessionless_fallback_run_ids=(),
            disposition=disposition,
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
    """Shared stub IM client capturing outbound plain/button messages and formatter hooks."""

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

    async def emit_agent_message(self, context: Any, message_type: str, text: str, **options: Any) -> str:
        self.emitted.append((message_type, text, dict(options)))
        return f"msg-{len(self.emitted)}"

    def events(self, message_type: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [event for event in self.emitted if event[0] == message_type]

    def note_session_tokens(self, context: Any, *, total: int) -> None:
        self.token_notes.append((context, total))
        key = f"{context.channel_id}:{context.thread_id or context.channel_id}"
        self.token_totals[key] = int(total)

    def session_token_field(self, context: Any) -> str:
        key = f"{context.channel_id}:{context.thread_id or context.channel_id}"
        total = self.token_totals.get(key, 0)
        return f"{total} tok" if total > 0 else ""


def antigravity_config(cli_path: Path | str, **overrides: Any) -> SimpleNamespace:
    data: dict[str, Any] = {
        "enabled": True,
        "cli_path": str(cli_path),
        "auth_mode": "oauth",
        "api_key": None,
        "base_url": None,
        "gcp_project": None,
        "gcp_location": None,
        "extra_args": [],
        "idle_timeout_seconds": 600,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def app_config(agy_cfg: Any, *, show_duration: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        antigravity=agy_cfg,
        language="en",
        show_duration=show_duration,
        include_time_info=False,
        include_user_info=False,
        reply_enhancements=False,
    )


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
    ps = dict(platform_specific or {})
    ps.setdefault(AGENT_RUNTIME_TURN_KEY, composite)
    ctx = MessageContext(
        user_id=user_id,
        channel_id="C123",
        platform=platform,
        thread_id="100.1",
        message_id=message_id,
        platform_specific=ps,
        files=files,
    )
    return AgentRequest(
        context=ctx,
        message=message,
        user_message=message,
        working_path=str(workdir),
        base_session_id=base_session_id,
        composite_session_id=composite,
        session_key=session_key,
        vibe_agent_model=model,
        vibe_agent_reasoning_effort=effort,
        subagent_name=subagent_name,
        files=files,
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

    def launches(self) -> list[dict[str, Any]]:
        return read_jsonl(self.launch_log)

    def rpcs(self, method: str | None = None) -> list[dict[str, Any]]:
        if self.rpc_log is None:
            return []
        rows = read_jsonl(self.rpc_log)
        return [m for m in rows if m.get("method") == method] if method else rows

    async def __aenter__(self) -> AntigravityHarness:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.agent.shutdown_runtime()


def make_agent_harness(
    tmp_path: Path,
    *,
    workdir: Path | None = None,
    record_rpc: bool = False,
    server_config: dict[str, Any] | None = None,
    cli_path: Path | str | None = None,
    sessions: FakeSessions | None = None,
    ownership: FakeOwnershipProvider | None = None,
    show_duration: bool = False,
    name: str = "fake-agy",
    **cfg_overrides: Any,
) -> AntigravityHarness:
    """Build a ready-to-use async-context AntigravityAgent backed by the fake shim."""
    from modules.agents.antigravity import AntigravityAgent

    repo = workdir or (tmp_path / "repo")
    repo.mkdir(parents=True, exist_ok=True)
    state_file = tmp_path / f"{name}-state.json"
    launch_log = tmp_path / f"{name}-launches.jsonl"
    rpc_log = (tmp_path / f"{name}-rpc.jsonl") if record_rpc else None
    rules_dir = tmp_path / f"{name}-rules"
    shim = (
        Path(cli_path)
        if cli_path is not None
        else create_fake_agy_shim(
            tmp_path,
            state_file=state_file,
            launch_log=launch_log,
            rpc_log=rpc_log,
            server_config=server_config,
            name=name,
        )
    )
    agy_cfg = antigravity_config(shim, **cfg_overrides)
    controller = FakeController(
        app_config(agy_cfg, show_duration=show_duration),
        sessions=sessions or FakeSessions(),
        runtime_ownership=ownership or FakeOwnershipProvider(),
    )
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=rules_dir)
    controller.agent_service.agents["antigravity"] = agent
    return AntigravityHarness(
        workdir=repo,
        state_file=state_file,
        launch_log=launch_log,
        rpc_log=rpc_log,
        rules_dir=rules_dir,
        shim=shim,
        agy_cfg=agy_cfg,
        controller=controller,
        agent=agent,
    )


# ---------------------------------------------------------------------------
# Direct CLI entrypoint for generated fake `agy` shims
# ---------------------------------------------------------------------------


def _run_login(cfg: dict[str, Any], state_file: Path) -> int:
    if cfg.get("business_unsupported"):
        sys.stdout.write("Interactive browser login required for Gemini Enterprise.\n")
        sys.stdout.flush()
        return 1
    sys.stdout.write(
        "Open this URL to sign in:\n"
        "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=fixture-state\n"
        "Enter authorization code: "
    )
    sys.stdout.flush()
    if sys.stdin.readline().strip() != cfg.get("expected_code", "valid-agy-code"):
        sys.stdout.write("Error: invalid authorization code\n")
        sys.stdout.flush()
        return 1
    state = (
        json.loads(state_file.read_text(encoding="utf-8"))
        if state_file.exists()
        else {"authenticated": False, "next_session_num": 1, "sessions": {}}
    )
    state["authenticated"] = True
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    sys.stdout.write("Login successful.\n")
    sys.stdout.flush()
    return 0


def _load_fake_acp_server_class() -> type[Any]:
    module_name = "_avibe_fake_acp_server"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached.FakeAcpServer
    server_path = HARNESS_PATH.with_name("fake_acp_server.py")
    spec = importlib.util.spec_from_file_location(module_name, server_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load fake ACP server from {server_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.FakeAcpServer


def _run_acp(cfg: dict[str, Any], state_file: Path, rpc_log: Path | None, auth_mode: bool) -> int:
    FakeAcpServer = _load_fake_acp_server_class()

    class _AuditingFakeAcpServer(FakeAcpServer):
        def _dispatch(self, msg: dict[str, Any]) -> None:
            if rpc_log is not None and "method" in msg:
                _append_jsonl(rpc_log, msg)
            super()._dispatch(msg)

        def _handle_sync_request(self, req_id: int | str, method: str, params: dict[str, Any]) -> None:
            if auth_mode and method == "authenticate":
                method_id = params.get("methodId")
                if method_id == "gemini-api-key":
                    if os.environ.get("GEMINI_API_KEY", "") != cfg.get("expected_api_key", "AIza-fixture-key-1234"):
                        self._send_error(req_id, -32000, "API key not valid")
                        return
                elif method_id == "agent-platform":
                    if not os.environ.get("GOOGLE_CLOUD_PROJECT") and cfg.get("require_gcp_project"):
                        self._send_error(req_id, -32000, "Authentication required: missing project")
                        return
            super()._handle_sync_request(req_id, method, params)

    if auth_mode:
        server_cfg = dict(cfg.get("acp_config") or {})
        server_cfg.setdefault("authMethods", _AUTH_DEFAULT_METHODS)
        server_cfg.setdefault("configOptions", _AUTH_DEFAULT_CONFIG_OPTIONS)
    else:
        server_cfg = dict(cfg)
        server_cfg.setdefault("authMethods", _AUTH_DEFAULT_METHODS)

    asyncio.run(_AuditingFakeAcpServer(state_file=state_file, config=server_cfg).run())
    return 0


def _cli_main(raw_argv: list[str]) -> int:
    split_at = raw_argv.index("--") if "--" in raw_argv else len(raw_argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-log", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--rpc-log", default=None)
    parser.add_argument("--config-file", default=None)
    parser.add_argument("--config-json", default=None)
    parser.add_argument("--auth-mode", action="store_true")
    opts = parser.parse_args(raw_argv[:split_at])
    agy_argv = raw_argv[split_at + 1 :] if split_at < len(raw_argv) else []

    _append_jsonl(
        Path(opts.launch_log),
        {"argv": agy_argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()},
    )
    if agy_argv == ["--version"]:
        sys.stdout.write("1.5.16\n")
        return 0
    cfg: dict[str, Any] = {}
    if opts.config_file:
        cfg_path = Path(opts.config_file)
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    elif opts.config_json:
        cfg = json.loads(opts.config_json)
    if agy_argv and agy_argv[0] == "acp":
        state_file = Path(opts.state_file)
        if "--login" in agy_argv:
            return _run_login(cfg, state_file)
        return _run_acp(cfg, state_file, Path(opts.rpc_log) if opts.rpc_log else None, opts.auth_mode)
    sys.stderr.write(f"Unexpected argv: {agy_argv}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
