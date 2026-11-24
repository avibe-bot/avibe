"""Hermetic Slice 3 workflow parity and cross-backend acceptance suite for Antigravity."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from core.agent_tool_policy import ALWAYS_SESSION_ONLY_TOOL_NAMES, check_tool_call
from core.caller_context import (
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
)
from core.handlers.message_handler import MessageHandler
from core.managed_skills import (
    SKILL_WORKING_DIR_ENV,
    load_skill,
    resolve_skills,
)
from core.mcp_hub import native as mcp_native
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition
from core.services.session_fork import (
    FORK_HANDOFF_MODE_SUMMARY,
    reserve_forked_session,
)
from core.session_handoff import HANDOFF_BUDGET_CHARS
from core.vibe_agents import VibeAgentStore
from modules.agents.antigravity import (
    AGY_CLI_DISABLE_AUTO_UPDATE_ENV,
    AntigravityAgent,
)
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest
from modules.agents.subagent_router import (
    list_antigravity_subagents,
    load_antigravity_subagent,
)
from modules.im import MessageContext
from modules.im.base import FileAttachment
from storage import messages_service
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope
from tests.helpers.fake_acp_server import FAKE_ACP_SERVER_PATH


def _create_recording_fake_agy_shim(
    tmp_path: Path,
    *,
    state_file: Path,
    launch_log: Path,
    rpc_log: Path | None = None,
    server_config: dict[str, Any] | None = None,
    name: str = "fake-agy-parity",
) -> Path:
    """Create a real executable shim that logs argv/env (and optionally JSON-RPC stdin) and runs FakeAcpServer."""
    cfg_json = json.dumps(server_config or {}, separators=(",", ":"))
    shim = tmp_path / name
    if rpc_log is None:
        script = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"log_path = {str(launch_log)!r}\n"
            "record = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'env': dict(os.environ),\n"
            "    'cwd': os.getcwd(),\n"
            "    'pid': os.getpid(),\n"
            "}\n"
            "with open(log_path, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(record) + '\\n')\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, '-u', {str(FAKE_ACP_SERVER_PATH)!r}, "
            f"'--state-file', {str(state_file)!r}, '--config-json', {cfg_json!r}])\n"
        )
    else:
        # Tee client->server NDJSON frames to rpc_log so tests inspect exact wire payloads.
        script = (
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, threading\n"
            f"log_path = {str(launch_log)!r}\n"
            f"rpc_path = {str(rpc_log)!r}\n"
            "record = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'env': dict(os.environ),\n"
            "    'cwd': os.getcwd(),\n"
            "    'pid': os.getpid(),\n"
            "}\n"
            "with open(log_path, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(record) + '\\n')\n"
            f"proc = subprocess.Popen([{sys.executable!r}, '-u', {str(FAKE_ACP_SERVER_PATH)!r}, "
            f"'--state-file', {str(state_file)!r}, '--config-json', {cfg_json!r}], "
            "stdin=subprocess.PIPE, stdout=sys.stdout, stderr=sys.stderr)\n"
            "def _pump():\n"
            "    assert proc.stdin is not None\n"
            "    try:\n"
            "        for raw in sys.stdin.buffer:\n"
            "            with open(rpc_path, 'ab') as rf:\n"
            "                rf.write(raw)\n"
            "            proc.stdin.write(raw)\n"
            "            proc.stdin.flush()\n"
            "    except BrokenPipeError:\n"
            "        pass\n"
            "    finally:\n"
            "        try:\n"
            "            proc.stdin.close()\n"
            "        except OSError:\n"
            "            pass\n"
            "t = threading.Thread(target=_pump, daemon=True)\n"
            "t.start()\n"
            "rc = proc.wait()\n"
            "t.join(timeout=1.0)\n"
            "sys.exit(rc)\n"
        )
    shim.write_text(script, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    return shim


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _InMemorySessions:
    """Minimal SessionsFacade-compatible in-memory store for hermetic adapter tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], str] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def ensure_agent_session_id(
        self,
        session_key: str,
        agent_name: str,
        anchor: str,
        **kwargs: Any,
    ) -> str:
        key = (session_key, agent_name, anchor)
        if key not in self._rows:
            self._seq += 1
            sid = f"ses_{self._seq}"
            self._rows[key] = sid
            self._by_id[sid] = {"id": sid, "native_session_id": "", **kwargs}
        return self._rows[key]

    def bind_agent_session(
        self,
        session_key: str,
        agent_name: str,
        anchor: str,
        native_session_id: str,
        **kwargs: Any,
    ) -> str:
        sid = self.ensure_agent_session_id(session_key, agent_name, anchor, **kwargs)
        self._by_id[sid]["native_session_id"] = native_session_id
        return sid

    def bind_agent_session_by_id(
        self,
        agent_session_id: str,
        native_session_id: str,
        **kwargs: Any,
    ) -> str:
        row = self._by_id.setdefault(agent_session_id, {"id": agent_session_id})
        row["native_session_id"] = native_session_id
        row.update(kwargs)
        return agent_session_id

    def get_agent_session_id(self, session_key: str, anchor: str, agent_name: str) -> str | None:
        sid = self._rows.get((session_key, agent_name, anchor))
        if not sid:
            return None
        return self._by_id.get(sid, {}).get("native_session_id") or None

    def list_agent_sessions(self, session_key: str, agent_name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for (sk, backend, anchor), sid in self._rows.items():
            if sk == session_key and backend == agent_name:
                native = self._by_id.get(sid, {}).get("native_session_id")
                if native:
                    out[anchor] = native
        return out

    def clear_agent_sessions(self, session_key: str, agent_name: str) -> None:
        for key in list(self._rows.keys()):
            if key[0] == session_key and key[1] == agent_name:
                sid = self._rows.pop(key)
                self._by_id.pop(sid, None)


class _FakeOwnershipProvider:
    def snapshot_many(self, targets: tuple[Any, ...]) -> tuple[RuntimeTargetOwnershipSnapshot, ...]:
        return tuple(self.snapshot(t) for t in targets)

    def snapshot(self, target: Any) -> RuntimeTargetOwnershipSnapshot:
        return RuntimeTargetOwnershipSnapshot(
            backend=target.backend,
            resource_key=target.resource_key,
            activity_runtime_keys=tuple(target.known_activity_runtime_keys),
            sessions=(),
            sessionless_active_activity_ids=(),
            sessionless_fallback_run_ids=(),
            disposition=SessionRuntimeDisposition.RECLAIMABLE,
        )


class _DummyFormatter:
    def format_result_footer(self, subtype: str, duration_ms: int, *, token_field: str = "") -> str:
        return f"{subtype}:{duration_ms}ms:{token_field}"

    def escape_special_chars(self, text: str) -> str:
        return text


@dataclass
class _DummyController:
    config: Any
    sessions: Any = field(default_factory=_InMemorySessions)
    runtime_activation: RuntimeActivationRegistry = field(default_factory=RuntimeActivationRegistry)
    runtime_ownership: _FakeOwnershipProvider = field(default_factory=_FakeOwnershipProvider)
    emitted: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    token_totals: dict[str, int] = field(default_factory=dict)
    primary_platform: str = "slack"

    def __post_init__(self) -> None:
        self.settings_manager = self.sessions
        self.im_client = SimpleNamespace(
            formatter=_DummyFormatter(),
            prepare_turn_context=AsyncMock(side_effect=lambda ctx, _src: ctx),
            add_reaction=AsyncMock(),
        )
        self.agent_service = SimpleNamespace(agents={})
        self.agent_auth_service = None
        self.processing_indicator = None

    async def emit_agent_message(
        self,
        context: Any,
        message_type: str,
        text: str,
        **options: Any,
    ) -> str:
        self.emitted.append((message_type, text, dict(options)))
        return f"msg-{len(self.emitted)}"

    def note_session_tokens(self, context: Any, *, total: int) -> None:
        key = f"{context.channel_id}:{context.thread_id or context.channel_id}"
        self.token_totals[key] = int(total)

    def session_token_field(self, context: Any) -> str:
        key = f"{context.channel_id}:{context.thread_id or context.channel_id}"
        total = self.token_totals.get(key, 0)
        return f"{total} tok" if total > 0 else ""


def _antigravity_config(cli_path: Path | str, **overrides: Any) -> SimpleNamespace:
    data = {
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


def _app_config(agy_cfg: Any, *, show_duration: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        antigravity=agy_cfg,
        language="en",
        show_duration=show_duration,
        include_time_info=False,
        include_user_info=False,
        reply_enhancements=False,
    )


def _make_request(
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


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME, AVIBE_HOME, XDG, Codex, and Claude paths inside tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    avibe_home = tmp_path / "avibe-home"
    avibe_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    return fake_home


# ---------------------------------------------------------------------------
# 1. Attachments & outputs (E7-E8, E12, G5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachments_image_blocks_when_supported_and_path_fallback_when_not(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    img_path = tmp_path / "diagram.png"
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
    img_path.write_bytes(png_bytes)
    doc_path = tmp_path / "notes.pdf"
    doc_path.write_bytes(b"%PDF-1.4\nfake-pdf")

    attachments = [
        FileAttachment(
            name="diagram.png",
            mimetype="image/png",
            local_path=str(img_path),
            size=len(png_bytes),
        ),
        FileAttachment(
            name="notes.pdf",
            mimetype="application/pdf",
            local_path=str(doc_path),
            size=doc_path.stat().st_size,
        ),
    ]

    # Case A: promptCapabilities.image == True -> image sent as ACP image block; PDF appended via message_with_files.
    rpc_log_a = tmp_path / "rpc-img-on.jsonl"
    shim_a = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "state-a.json",
        launch_log=tmp_path / "launch-a.jsonl",
        rpc_log=rpc_log_a,
        name="fake-agy-img-on",
    )
    agy_cfg_a = _antigravity_config(shim_a)
    ctrl_a = _DummyController(_app_config(agy_cfg_a))
    agent_a = AntigravityAgent(ctrl_a, agy_cfg_a, rules_base_dir=tmp_path / "rules-a")
    try:
        req_a = _make_request(message="inspect these", workdir=workdir, files=attachments)
        await agent_a.handle_message(req_a)
    finally:
        await agent_a.shutdown_runtime()

    prompt_rpcs_a = [m for m in _read_jsonl(rpc_log_a) if m.get("method") == "session/prompt"]
    assert len(prompt_rpcs_a) == 1
    blocks_a = prompt_rpcs_a[0]["params"]["prompt"]
    text_blocks_a = [b for b in blocks_a if b["type"] == "text"]
    image_blocks_a = [b for b in blocks_a if b["type"] == "image"]
    assert len(image_blocks_a) == 1
    assert image_blocks_a[0]["mimeType"] == "image/png"
    assert base64.b64decode(image_blocks_a[0]["data"]) == png_bytes
    assert len(text_blocks_a) == 1
    assert "inspect these" in text_blocks_a[0]["text"]
    assert f"- File: {doc_path}" in text_blocks_a[0]["text"]
    assert f"- Image: {img_path}" not in text_blocks_a[0]["text"]

    # Case B: promptCapabilities.image == False -> zero image blocks; both attachments via message_with_files.
    rpc_log_b = tmp_path / "rpc-img-off.jsonl"
    no_img_caps = {
        "loadSession": True,
        "promptCapabilities": {"image": False, "embeddedContext": True},
        "sessionCapabilities": {"resume": {}, "list": {}, "close": {}, "additionalDirectories": {}},
    }
    shim_b = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "state-b.json",
        launch_log=tmp_path / "launch-b.jsonl",
        rpc_log=rpc_log_b,
        server_config={"agentCapabilities": no_img_caps},
        name="fake-agy-img-off",
    )
    agy_cfg_b = _antigravity_config(shim_b)
    ctrl_b = _DummyController(_app_config(agy_cfg_b))
    agent_b = AntigravityAgent(ctrl_b, agy_cfg_b, rules_base_dir=tmp_path / "rules-b")
    try:
        req_b = _make_request(message="inspect these", workdir=workdir, files=attachments)
        await agent_b.handle_message(req_b)
    finally:
        await agent_b.shutdown_runtime()

    prompt_rpcs_b = [m for m in _read_jsonl(rpc_log_b) if m.get("method") == "session/prompt"]
    assert len(prompt_rpcs_b) == 1
    blocks_b = prompt_rpcs_b[0]["params"]["prompt"]
    assert [b for b in blocks_b if b["type"] == "image"] == []
    assert len(blocks_b) == 1 and blocks_b[0]["type"] == "text"
    assert f"- Image: {img_path}" in blocks_b[0]["text"]
    assert f"- File: {doc_path}" in blocks_b[0]["text"]


@pytest.mark.asyncio
async def test_usage_update_context_gauge_and_live_title_backfill(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    ensure_sqlite_state(primary_platform="avibe")
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_title_usage",
                now="2026-11-24T00:00:00Z",
            )
            blank_session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="ses_blank_anchor",
                agent_backend="antigravity",
                agent_variant="antigravity",
                workdir=str(workdir),
                native_session_id="",
                title="",
            )
            titled_session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="ses_titled_anchor",
                agent_backend="antigravity",
                agent_variant="antigravity",
                workdir=str(workdir),
                native_session_id="",
                title="User Named Session",
                metadata={"title_source": "user"},
            )
    finally:
        engine.dispose()

    shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "state.json",
        launch_log=tmp_path / "launch.jsonl",
    )
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg, show_duration=True))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req_blank = _make_request(
            message="stream_all",
            workdir=workdir,
            base_session_id="ses_blank_anchor",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": blank_session_id,
                    "agent_backend": "antigravity",
                    "native_session_id": "",
                }
            },
        )
        await agent.handle_message(req_blank)

        req_titled = _make_request(
            message="stream_all",
            workdir=workdir,
            base_session_id="ses_titled_anchor",
            platform="avibe",
            platform_specific={
                "agent_session_target": {
                    "id": titled_session_id,
                    "agent_backend": "antigravity",
                    "native_session_id": "",
                }
            },
        )
        await agent.handle_message(req_titled)

        if agent._background_tasks:
            await asyncio.gather(*list(agent._background_tasks))

        # Usage gauge recorded from usage_update (used=1200) and reflected in result footer, with no fabricated cost.
        assert controller.token_totals["C123:100.1"] == 1200
        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 2
        assert "1200 tok" in (results[0][2].get("result_footer") or "")
        assert "$" not in (results[0][2].get("result_footer") or "")

        # Live session_info_update backfilled the blank SQLite row and preserved the user-owned title.
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                blank_row = conn.execute(
                    select(agent_sessions).where(agent_sessions.c.id == blank_session_id)
                ).mappings().one()
                titled_row = conn.execute(
                    select(agent_sessions).where(agent_sessions.c.id == titled_session_id)
                ).mappings().one()
        finally:
            engine.dispose()

        assert blank_row["title"] == "Update greeting"
        blank_meta = json.loads(blank_row["metadata_json"])
        assert blank_meta["title_source"] == "backend"
        assert blank_meta["title_backend"] == "antigravity"
        assert titled_row["title"] == "User Named Session"
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 2. Fork bounded handoff (D3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_without_native_capability_builds_bounded_summary_handoff(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    ensure_sqlite_state(primary_platform="avibe")
    from config import paths

    db_path = paths.get_sqlite_state_path()
    SQLiteSessionsService(db_path).close()
    store = VibeAgentStore(db_path)
    try:
        agy_worker = store.create(name="agy-worker", backend="antigravity", model="model-alpha")
    finally:
        store.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_agy_fork",
                now="2026-11-24T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(workdir),
                    agent_name="agy-worker",
                    agent_backend="antigravity",
                    agent_variant="antigravity",
                    model="model-alpha",
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-11-24T00:00:00Z",
                    updated_at="2026-11-24T00:00:00Z",
                )
            )
            source_session_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor=None,
                agent_backend="antigravity",
                agent_variant="antigravity",
                agent_id=agy_worker.id,
                agent_name="agy-worker",
                model="model-alpha",
                workdir=str(workdir),
                native_session_id="sess-source-native",
                title="Long Antigravity Session",
            )
            # Seed 40 large messages (far exceeding HANDOFF_BUDGET_CHARS) to prove bounded summary handoff.
            for idx in range(40):
                messages_service.append(
                    conn,
                    scope_id=scope_id,
                    session_id=source_session_id,
                    platform="avibe",
                    author="user" if idx % 2 == 0 else "agent",
                    message_type="user" if idx % 2 == 0 else "result",
                    text=f"turn-marker-{idx:02d} " + ("payload " * 250),
                )
    finally:
        engine.dispose()

    fork_result = reserve_forked_session(
        source_session_id=source_session_id,
        db_path=db_path,
    )
    assert fork_result.agent_backend == "antigravity"
    assert fork_result.fork.source_native_session_id == "sess-source-native"

    no_fork_caps = {
        "loadSession": True,
        "promptCapabilities": {"image": True, "embeddedContext": True},
        "sessionCapabilities": {
            "resume": {},
            "list": {},
            "close": {},
            "additionalDirectories": {},
        },
    }
    rpc_log = tmp_path / "fork-rpc.jsonl"
    shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "fork-state.json",
        launch_log=tmp_path / "fork-launch.jsonl",
        rpc_log=rpc_log,
        server_config={"agentCapabilities": no_fork_caps},
    )
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        fork_target = {
            "id": fork_result.session_id,
            "agent_backend": "antigravity",
            "native_session_id": "",
            "native_session_fork": fork_result.fork.to_metadata(),
        }
        req1 = _make_request(
            message="continue from fork",
            workdir=workdir,
            base_session_id=fork_result.session_id,
            platform="avibe",
            model="model-alpha",
            platform_specific={"agent_session_target": fork_target},
        )
        await agent.handle_message(req1)

        # Follow-up turn on the same forked session must NOT re-inject the handoff.
        req2 = _make_request(
            message="second turn after fork",
            workdir=workdir,
            base_session_id=fork_result.session_id,
            platform="avibe",
            model="model-alpha",
            platform_specific={"agent_session_target": fork_target},
        )
        await agent.handle_message(req2)
    finally:
        await agent.shutdown_runtime()

    rpcs = _read_jsonl(rpc_log)
    methods = [m.get("method") for m in rpcs]
    assert "session/fork" not in methods
    assert "session/new" in methods

    prompt_rpcs = [m for m in rpcs if m.get("method") == "session/prompt"]
    assert len(prompt_rpcs) == 2
    first_prompt = prompt_rpcs[0]["params"]["prompt"][0]["text"]
    second_prompt = prompt_rpcs[1]["params"]["prompt"][0]["text"]

    handoff_end = first_prompt.index("[End of Avibe handoff]") + len("[End of Avibe handoff]")
    handoff_block = first_prompt[:handoff_end]
    assert handoff_block.startswith("[Avibe handoff]")
    assert len(handoff_block) <= HANDOFF_BUDGET_CHARS
    assert "bounded summary handoff" in handoff_block
    assert "rather than full native history" in handoff_block
    assert "switched" not in handoff_block
    assert "(earlier messages omitted)" in handoff_block
    assert "turn-marker-39" in handoff_block
    assert "turn-marker-00" not in handoff_block
    assert handoff_block.index("turn-marker-38") < handoff_block.index("turn-marker-39")
    assert f"WHERE session_id='{source_session_id}'" in handoff_block
    assert "continue from fork" in first_prompt

    assert "[Avibe handoff]" not in second_prompt
    assert "second turn after fork" in second_prompt

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            child_row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == fork_result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()
    child_meta = json.loads(child_row["metadata_json"])
    assert child_meta["fork_handoff_mode"] == FORK_HANDOFF_MODE_SUMMARY


# ---------------------------------------------------------------------------
# 3. Subagents (F10-F11)
# ---------------------------------------------------------------------------


def test_antigravity_native_subagent_discovery_and_project_override(tmp_path: Path, _hermetic_env: Path) -> None:
    fake_home = _hermetic_env
    global_agents = fake_home / ".gemini" / "config" / "agents"
    global_agents.mkdir(parents=True)
    (global_agents / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: Global reviewer\n"
        "model: model-alpha\n"
        "thought_level: low\n"
        "---\n\n"
        "Global review instructions.\n",
        encoding="utf-8",
    )
    (global_agents / "planner.md").write_text(
        "---\n"
        "name: planner\n"
        "description: Global planner\n"
        "model: model-alpha\n"
        "---\n\n"
        "Draft plans carefully.\n",
        encoding="utf-8",
    )
    # Non-frontmatter markdown files must be ignored.
    (global_agents / "README.md").write_text("# Agents directory\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    project_agents = workspace / ".agents" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: Project reviewer\n"
        "model: model-beta\n"
        "thought_level: high\n"
        "---\n\n"
        "Review workspace diffs for regressions.\n",
        encoding="utf-8",
    )

    discovered = list_antigravity_subagents(project_root=workspace)
    assert set(discovered.keys()) == {"reviewer", "planner"}
    assert discovered["planner"].source == "global"
    assert discovered["planner"].description == "Global planner"
    assert discovered["planner"].developer_instructions == "Draft plans carefully."

    reviewer = load_antigravity_subagent("REVIEWER", project_root=workspace)
    assert reviewer is not None
    assert reviewer.name == "reviewer"
    assert reviewer.source == "project"
    assert reviewer.description == "Project reviewer"
    assert reviewer.model == "model-beta"
    assert reviewer.reasoning_effort == "high"
    assert reviewer.developer_instructions == "Review workspace diffs for regressions."
    assert reviewer.path == project_agents / "reviewer.md"


@pytest.mark.asyncio
async def test_message_handler_routes_antigravity_subagent_with_isolated_process(
    tmp_path: Path,
    _hermetic_env: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project_agents = workspace / ".agents" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "description: Code reviewer\n"
        "model: model-beta\n"
        "thought_level: high\n"
        "---\n\n"
        "Check edge cases.\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "subagent-state.json"
    launch_log = tmp_path / "subagent-launches.jsonl"
    rpc_log = tmp_path / "subagent-rpcs.jsonl"
    shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=state_file,
        launch_log=launch_log,
        rpc_log=rpc_log,
    )
    agy_cfg = _antigravity_config(shim)
    sessions = _InMemorySessions()
    controller = _DummyController(_app_config(agy_cfg), sessions=sessions)
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")

    # Wire real MessageHandler -> AntigravityAgent with minimal controller plumbing.
    async def _dispatch_to_agent(backend: str, req: AgentRequest) -> None:
        assert backend == "antigravity"
        await agent.handle_message(req)

    controller.agent_service = SimpleNamespace(
        agents={"antigravity": agent},
        handle_message=AsyncMock(side_effect=_dispatch_to_agent),
    )
    controller.session_manager = sessions
    controller.receiver_tasks = {}
    controller.resolve_agent_for_context = lambda _ctx: "antigravity"
    controller.resolve_vibe_agent_for_context = lambda _ctx, **_kw: SimpleNamespace(
        id="va_agy",
        name="antigravity",
        backend="antigravity",
        model="model-alpha",
        reasoning_effort="low",
        system_prompt="Follow Avibe instructions.",
    )
    controller.processing_indicator = SimpleNamespace(
        start=AsyncMock(return_value=None),
        apply_to_request=lambda *_a, **_kw: None,
        finish=AsyncMock(),
    )
    controller.get_settings_manager_for_context = lambda _ctx: SimpleNamespace(
        get_channel_routing=lambda _key: None,
    )
    controller.get_im_client_for_context = lambda _ctx: controller.im_client
    controller.sessions = sessions

    handler = MessageHandler(controller)
    handler.set_session_handler(
        SimpleNamespace(
            get_session_info=lambda _ctx, source="human": (
                "slack_thread_1",
                str(workspace),
                f"slack_thread_1:{workspace}",
            ),
            should_allocate_scheduled_anchor=lambda _ctx, source="human": False,
        )
    )
    handler._claim_native_human_event = lambda _ctx: True  # type: ignore[method-assign]
    handler._get_settings_key = lambda _ctx: "slack::channel::C1"  # type: ignore[method-assign]
    handler._get_session_key = lambda _ctx: "slack::channel::C1"  # type: ignore[method-assign]
    handler._attach_backend_handoff = AsyncMock()  # type: ignore[method-assign]

    parent_row_id = sessions.ensure_agent_session_id("slack::channel::C1", "antigravity", "slack_thread_1")

    def _ctx(msg_id: str) -> MessageContext:
        return MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="avibe",
            thread_id="thread_1",
            message_id=msg_id,
            platform_specific={
                "agent_session_target": {
                    "id": parent_row_id,
                    "agent_backend": "antigravity",
                    "native_session_id": sessions._by_id[parent_row_id].get("native_session_id", ""),
                    "model": "model-alpha",
                    "reasoning_effort": "low",
                }
            },
        )

    try:
        # Turn 1: parent session turn.
        await handler.handle_user_message(_ctx("m1"), "parent-secret-marker")
        parent_key = f"slack_thread_1:{workspace}"
        assert parent_key in agent._sessions
        parent_sess = agent._sessions[parent_key]
        parent_pid = parent_sess.client.pid
        parent_native_id = parent_sess.native_session_id
        assert parent_native_id == "sess-1"

        # Turn 2: subagent-prefixed turn -> spawns separate --agent reviewer process on its own runtime_turn_key.
        controller.emitted.clear()
        await handler.handle_user_message(_ctx("m2"), "reviewer: audit-subagent-marker")
        subagent_key = f"slack_thread_1:reviewer:{workspace}"
        assert subagent_key in agent._sessions
        assert parent_key in agent._sessions
        sub_sess = agent._sessions[subagent_key]
        assert sub_sess.client.pid != parent_pid
        assert sub_sess.native_session_id != parent_native_id
        assert sub_sess.spec.rules_dirs != parent_sess.spec.rules_dirs
        assert sub_sess.current_model == "model-beta"
        assert sub_sess.current_effort == "high"
        assert parent_sess.client.is_alive
        # Parent Workbench row still points to the parent native session ID, never overwritten by subagent.
        assert sessions._by_id[parent_row_id]["native_session_id"] == parent_native_id

        launches = _read_jsonl(launch_log)
        assert len(launches) == 2
        assert "--agent" not in launches[0]["argv"]
        assert launches[1]["argv"][-2:] == ["--agent", "reviewer"]

        # Turn 3: follow-up to subagent reuses subagent process/session; Turn 4 reuses parent process/session.
        controller.emitted.clear()
        await handler.handle_user_message(_ctx("m3"), "reviewer: subagent-followup")
        sub_reply = [text for kind, text, _ in controller.emitted if kind == "result"][0]
        assert "subagent-followup" in sub_reply
        assert "model=model-beta" in sub_reply
        assert agent._sessions[subagent_key].client.pid == sub_sess.client.pid

        controller.emitted.clear()
        await handler.handle_user_message(_ctx("m4"), "parent-followup")
        parent_reply = [text for kind, text, _ in controller.emitted if kind == "result"][0]
        assert "parent-followup" in parent_reply
        assert "model=model-alpha" in parent_reply
        assert agent._sessions[parent_key].client.pid == parent_pid
        assert len(_read_jsonl(launch_log)) == 2

        # Wire prompts sent to each native session stay strictly isolated between parent and subagent.
        prompts_by_session: dict[str, list[str]] = {}
        for msg in _read_jsonl(rpc_log):
            if msg.get("method") == "session/prompt":
                sid = msg["params"]["sessionId"]
                text = "\n".join(b.get("text", "") for b in msg["params"]["prompt"] if b.get("type") == "text")
                prompts_by_session.setdefault(sid, []).append(text)
        parent_prompts = "\n".join(prompts_by_session[parent_native_id])
        sub_prompts = "\n".join(prompts_by_session[sub_sess.native_session_id])
        assert "parent-secret-marker" in parent_prompts and "parent-followup" in parent_prompts
        assert "audit-subagent-marker" not in parent_prompts
        assert "audit-subagent-marker" in sub_prompts and "subagent-followup" in sub_prompts
        assert "parent-secret-marker" not in sub_prompts
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 4. Combined fixture parity contract (F1-F8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combined_fixture_skills_mcp_rules_tool_policy_and_managed_launch(
    tmp_path: Path,
    _hermetic_env: Path,
) -> None:
    fake_home = _hermetic_env
    workspace = tmp_path / "combined-workspace"
    workspace.mkdir()

    # 1. Workspace skill (.agents/skills/) & Global Antigravity skill (~/.gemini/antigravity-cli/skills/)
    repo_skill_dir = workspace / ".agents" / "skills" / "repo-checks"
    repo_skill_dir.mkdir(parents=True)
    (repo_skill_dir / "SKILL.md").write_text(
        "---\nname: repo-checks\ndescription: Run repository parity checks\n---\n\nRun pytest locally.\n",
        encoding="utf-8",
    )
    global_skill_dir = fake_home / ".gemini" / "antigravity-cli" / "skills" / "agy-helper"
    global_skill_dir.mkdir(parents=True)
    (global_skill_dir / "SKILL.md").write_text(
        "---\nname: agy-helper\ndescription: Antigravity workspace helper\n---\n\nCheck status first.\n",
        encoding="utf-8",
    )

    # 2. Native Antigravity MCP configs: global ~/.gemini/config/mcp_config.json & workspace .agents/mcp_config.json
    global_mcp_path = fake_home / ".gemini" / "config" / "mcp_config.json"
    global_mcp_path.parent.mkdir(parents=True, exist_ok=True)
    global_mcp_bytes = json.dumps(
        {
            "mcpServers": {
                "global_docs": {
                    "command": sys.executable,
                    "args": ["-c", "print('ok')"],
                }
            }
        },
        indent=2,
    ).encode("utf-8")
    global_mcp_path.write_bytes(global_mcp_bytes)

    project_mcp_path = workspace / ".agents" / "mcp_config.json"
    project_mcp_path.parent.mkdir(parents=True, exist_ok=True)
    project_mcp_bytes = json.dumps(
        {
            "mcpServers": {
                "repo_tools": {
                    "serverUrl": "https://mcp.example.test/sse",
                }
            }
        },
        indent=2,
    ).encode("utf-8")
    project_mcp_path.write_bytes(project_mcp_bytes)

    # 3. Project AGENTS.md and global GEMINI.md rules (must stay untouched while Avibe rules load via additionalDirectories)
    project_rules_path = workspace / "AGENTS.md"
    project_rules_bytes = b"# Project Rules\nPROJECT_RULE_MARKER: keep diffs small.\n"
    project_rules_path.write_bytes(project_rules_bytes)
    global_rules_path = fake_home / ".gemini" / "GEMINI.md"
    global_rules_bytes = b"# Global Rules\nGLOBAL_RULE_MARKER: always verify.\n"
    global_rules_path.write_bytes(global_rules_bytes)

    # Launch ONE Antigravity session in this combined fixture and inspect all wire/env/disk effects.
    state_file = tmp_path / "combined-state.json"
    launch_log = tmp_path / "combined-launch.jsonl"
    rpc_log = tmp_path / "combined-rpc.jsonl"
    shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=state_file,
        launch_log=launch_log,
        rpc_log=rpc_log,
    )
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "avibe-rules")
    try:
        req = _make_request(message="recall", workdir=workspace, model="model-alpha")
        await agent.handle_message(req)

        # Verify managed execution launch flags and env.
        launches = _read_jsonl(launch_log)
        assert len(launches) == 1
        launch = launches[0]
        assert launch["argv"][:3] == ["acp", "--disable-telemetry", "--dangerously-skip-permissions"]
        assert launch["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert launch["env"][SKILL_WORKING_DIR_ENV] == str(workspace)
        assert launch["env"][AVIBE_SESSION_ID_ENV] == "ses_1"

        # Verify session/new passed mcpServers=[] and Avibe-owned additionalDirectories.
        new_rpcs = [m for m in _read_jsonl(rpc_log) if m.get("method") == "session/new"]
        assert len(new_rpcs) == 1
        new_params = new_rpcs[0]["params"]
        assert new_params["mcpServers"] == []
        add_dirs = new_params["additionalDirectories"]
        assert len(add_dirs) >= 1
        rendered_avibe_rules = "\n".join(
            (Path(d) / "AGENTS.md").read_text(encoding="utf-8") for d in add_dirs
        )
        assert "repo-checks" in rendered_avibe_rules
        assert "agy-helper" in rendered_avibe_rules

        # Verify managed skills catalog & load through core/managed_skills.py.
        resolved = {s.name: s for s in resolve_skills(workspace)}
        assert "repo-checks" in resolved
        assert "agy-helper" in resolved
        loaded_repo = load_skill("repo-checks", workspace)
        loaded_global = load_skill("agy-helper", workspace)
        assert loaded_repo is not None and "Run pytest locally." in (loaded_repo.body or "")
        assert loaded_global is not None and "Check status first." in (loaded_global.body or "")

        # Verify Avibe MCP Hub reads both global & workspace Antigravity native MCP configs.
        global_servers = mcp_native.read_all()
        assert "global_docs" in global_servers
        assert global_servers["global_docs"].source == mcp_native.ANTIGRAVITY
        assert global_servers["global_docs"].error is None

        project_servers = mcp_native.read_project(workspace)
        assert "repo_tools" in project_servers
        assert project_servers["repo_tools"].source == mcp_native.ANTIGRAVITY
        assert project_servers["repo_tools"].error is None

        # Verify user/project rules and MCP configs were NEVER mutated.
        assert project_rules_path.read_bytes() == project_rules_bytes
        assert global_rules_path.read_bytes() == global_rules_bytes
        assert global_mcp_path.read_bytes() == global_mcp_bytes
        assert project_mcp_path.read_bytes() == project_mcp_bytes

        # Verify core/agent_tool_policy.py blocks Antigravity session-only background tools & allows sync tools.
        for blocked_tool in ("schedule", "invoke_subagent", "define_subagent"):
            assert blocked_tool in ALWAYS_SESSION_ONLY_TOOL_NAMES
            decision = check_tool_call(blocked_tool, {})
            assert decision.denied is True
            assert "Harness" in decision.reason

        for sync_tool in ("read_file", "run_command", "grep_search"):
            assert check_tool_call(sync_tool, {}).allowed is True

        # Verify AntigravityAgent's ACP permission handler enforces core/agent_tool_policy.py.
        session = agent._sessions[req.composite_session_id]
        perm_options = [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
        ]
        denied_resp = await agent._handle_server_request(
            session,
            "session/request_permission",
            {
                "sessionId": session.native_session_id,
                "toolCall": {"toolCallId": "tc-sched", "name": "schedule", "rawInput": {}},
                "options": perm_options,
            },
        )
        assert denied_resp == {"outcome": {"outcome": "selected", "optionId": "reject_once"}}

        allowed_resp = await agent._handle_server_request(
            session,
            "session/request_permission",
            {
                "sessionId": session.native_session_id,
                "toolCall": {"toolCallId": "tc-read", "name": "read_file", "rawInput": {"path": "a.py"}},
                "options": perm_options,
            },
        )
        assert allowed_resp == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 5. Failure recovery & Harness isolation (E18, G1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_recovery_auth_launch_and_mid_turn_crash(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()

    # 5a. -32000 auth error on fresh session/new -> prewrite auth_required + AgentError + single TurnEnd(FAILED).
    auth_shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "auth-state.json",
        launch_log=tmp_path / "auth-launch.jsonl",
        server_config={"requireAuth": True},
        name="fake-agy-auth",
    )
    auth_cfg = _antigravity_config(auth_shim)
    ctrl_auth = _DummyController(_app_config(auth_cfg))
    agent_auth = AntigravityAgent(ctrl_auth, auth_cfg, rules_base_dir=tmp_path / "rules-auth")
    try:
        req_auth = _make_request(message="hello", workdir=workdir)
        set_dispatch_phase(req_auth.context, DISPATCH_PHASE_PREWRITE)
        await agent_auth.handle_message(req_auth)

        assert prewrite_failure_evidence(req_auth.context).get("reason") == "auth_required"
        notifies = [e for e in ctrl_auth.emitted if e[0] == "notify"]
        results = [e for e in ctrl_auth.emitted if e[0] == "result"]
        assert len(notifies) == 1
        assert "authentication required" in notifies[0][1].lower()
        assert "-32000" in notifies[0][1]
        assert len(results) == 1
        assert results[0][2].get("is_error") is True

        # 5b. -32000 auth error on session/resume must stay classified as auth_required, NOT resume-unavailable.
        ctrl_auth.emitted.clear()
        req_resume_auth = _make_request(
            message="hello",
            workdir=workdir,
            base_session_id="slack_resume_auth",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_existing",
                    "agent_backend": "antigravity",
                    "native_session_id": "sess-1",
                }
            },
        )
        set_dispatch_phase(req_resume_auth.context, DISPATCH_PHASE_PREWRITE)
        await agent_auth.handle_message(req_resume_auth)

        assert prewrite_failure_evidence(req_resume_auth.context).get("reason") == "auth_required"
        resume_notifies = [e for e in ctrl_auth.emitted if e[0] == "notify"]
        resume_results = [e for e in ctrl_auth.emitted if e[0] == "result"]
        assert len(resume_notifies) == 1
        assert "-32000" in resume_notifies[0][1]
        assert "Could not resume" not in resume_notifies[0][1]
        assert len(resume_results) == 1
        assert resume_results[0][2].get("is_error") is True
    finally:
        await agent_auth.shutdown_runtime()

    # 5c. Launch failure (FileNotFoundError) -> prewrite cli_not_found + AgentError + single TurnEnd(FAILED).
    missing_cfg = _antigravity_config(tmp_path / "does-not-exist-agy")
    ctrl_missing = _DummyController(_app_config(missing_cfg))
    agent_missing = AntigravityAgent(ctrl_missing, missing_cfg, rules_base_dir=tmp_path / "rules-missing")
    try:
        req_missing = _make_request(message="hello", workdir=workdir)
        set_dispatch_phase(req_missing.context, DISPATCH_PHASE_PREWRITE)
        await agent_missing.handle_message(req_missing)

        assert prewrite_failure_evidence(req_missing.context).get("reason") == "cli_not_found"
        notifies = [e for e in ctrl_missing.emitted if e[0] == "notify"]
        results = [e for e in ctrl_missing.emitted if e[0] == "result"]
        assert len(notifies) == 1
        assert "CLI not found" in notifies[0][1]
        assert len(results) == 1
        assert results[0][2].get("is_error") is True
    finally:
        await agent_missing.shutdown_runtime()

    # 5d. Mid-turn crash -> visible AgentError + single TurnEnd(FAILED) + recovery on next turn.
    crash_shim = _create_recording_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "crash-state.json",
        launch_log=tmp_path / "crash-launch.jsonl",
        name="fake-agy-crash",
    )
    crash_cfg = _antigravity_config(crash_shim)
    ctrl_crash = _DummyController(_app_config(crash_cfg))
    agent_crash = AntigravityAgent(ctrl_crash, crash_cfg, rules_base_dir=tmp_path / "rules-crash")
    try:
        req_crash = _make_request(message="crash_mid_prompt", workdir=workdir)
        await agent_crash.handle_message(req_crash)

        notifies = [e for e in ctrl_crash.emitted if e[0] == "notify"]
        results = [e for e in ctrl_crash.emitted if e[0] == "result"]
        assert len(notifies) == 1
        assert "simulated peer crash" in notifies[0][1] or "exited with code 2" in notifies[0][1]
        assert len(results) == 1
        assert results[0][2].get("is_error") is True
        assert req_crash.composite_session_id not in agent_crash._sessions

        # Recovery: next turn spawns a fresh process and succeeds cleanly.
        ctrl_crash.emitted.clear()
        req_recover = _make_request(message="recovered-ok", workdir=workdir)
        await agent_crash.handle_message(req_recover)
        ok_results = [e for e in ctrl_crash.emitted if e[0] == "result"]
        assert len(ok_results) == 1
        assert "echo:recovered-ok" in ok_results[0][1]
    finally:
        await agent_crash.shutdown_runtime()


@pytest.mark.asyncio
async def test_harness_delegated_run_authority_isolation_and_sibling_isolation(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "harness-state.json"
    launch_log = tmp_path / "harness-launches.jsonl"
    shim = _create_recording_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        # Start sibling Session B first and record its PID and native session ID.
        req_sibling = _make_request(
            message="sibling-marker",
            workdir=workdir,
            base_session_id="slack_sibling_B",
            session_key="slack::channel::CB",
        )
        await agent.handle_message(req_sibling)
        sibling_sess = agent._sessions[req_sibling.composite_session_id]
        sibling_pid = sibling_sess.client.pid
        sibling_native = sibling_sess.native_session_id

        # Session A Turn 1: ordinary user turn (no run_id).
        req_a1 = _make_request(
            message="user-turn-1",
            workdir=workdir,
            base_session_id="slack_session_A",
            session_key="slack::channel::CA",
        )
        await agent.handle_message(req_a1)
        pid_a1 = agent._sessions[req_a1.composite_session_id].client.pid
        native_a = agent._sessions[req_a1.composite_session_id].native_session_id
        assert pid_a1 != sibling_pid
        assert native_a != sibling_native

        launches = _read_jsonl(launch_log)
        assert AVIBE_RUN_ID_ENV not in launches[-1]["env"]

        # Session A Turn 2: delegated Harness run switches process cleanly and resumes Session A's native ID.
        controller.emitted.clear()
        req_a2 = _make_request(
            message="recall",
            workdir=workdir,
            base_session_id="slack_session_A",
            session_key="slack::channel::CA",
            platform_specific={
                "task_execution_id": "run_901",
                "task_trigger_kind": "agent_run",
                "source_kind": "agent",
            },
        )
        await agent.handle_message(req_a2)
        pid_a2 = agent._sessions[req_a2.composite_session_id].client.pid
        assert pid_a2 != pid_a1
        assert agent._sessions[req_a2.composite_session_id].native_session_id == native_a
        launches = _read_jsonl(launch_log)
        assert launches[-1]["env"][AVIBE_RUN_ID_ENV] == "run_901"
        assert launches[-1]["env"][AVIBE_CALLER_SOURCE_ENV] == "agent_run"
        res_a2 = [text for kind, text, _ in controller.emitted if kind == "result"][0]
        assert "user-turn-1" in res_a2

        # Session A Turn 3: subsequent user turn MUST replace process again -> ZERO authority leakage!
        controller.emitted.clear()
        req_a3 = _make_request(
            message="recall",
            workdir=workdir,
            base_session_id="slack_session_A",
            session_key="slack::channel::CA",
        )
        await agent.handle_message(req_a3)
        pid_a3 = agent._sessions[req_a3.composite_session_id].client.pid
        assert pid_a3 != pid_a2
        assert agent._sessions[req_a3.composite_session_id].native_session_id == native_a
        launches = _read_jsonl(launch_log)
        assert AVIBE_RUN_ID_ENV not in launches[-1]["env"]
        assert launches[-1]["env"].get(AVIBE_CALLER_SOURCE_ENV) != "agent_run"
        res_a3 = [text for kind, text, _ in controller.emitted if kind == "result"][0]
        assert "user-turn-1" in res_a3

        # Sibling Session B remained completely untouched throughout Session A's replacements.
        assert agent._sessions[req_sibling.composite_session_id].client.pid == sibling_pid
        assert agent._sessions[req_sibling.composite_session_id].client.is_alive
        controller.emitted.clear()
        req_sibling_recall = _make_request(
            message="recall",
            workdir=workdir,
            base_session_id="slack_sibling_B",
            session_key="slack::channel::CB",
        )
        await agent.handle_message(req_sibling_recall)
        sibling_res = [text for kind, text, _ in controller.emitted if kind == "result"][0]
        assert "sibling-marker" in sibling_res
        assert "user-turn-1" not in sibling_res
    finally:
        await agent.shutdown_runtime()
