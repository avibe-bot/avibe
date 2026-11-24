"""Hermetic tests for AntigravityAgent, rules rendering, model mapping, native sessions, and host hooks."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.caller_context import (
    AVIBE_CALLER_RESOURCE_CONTEXT_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
)
from core.run_settlement import SETTLED_BY_STOPPED
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
)
from core.services.running_agents import _resolve_live_state, end_running_agent, snapshot_running_agents
from modules.agents.antigravity import (
    AGY_CLI_DISABLE_AUTO_UPDATE_ENV,
    MAX_RULE_FILE_BYTES,
    MAX_RULE_TOTAL_BYTES,
    AntigravityAgent,
    build_antigravity_command,
    build_antigravity_env,
    render_session_rules,
)
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.providers import DEFAULT_PROVIDER_SPECS
from modules.im import MessageContext
from tests.helpers.fake_acp_server import FAKE_ACP_SERVER_PATH


def _create_fake_agy_shim(
    tmp_path: Path,
    *,
    state_file: Path,
    launch_log: Path,
    server_config: dict[str, Any] | None = None,
    name: str = "fake-agy",
) -> Path:
    """Create a real executable shim that logs argv/env and execs FakeAcpServer."""
    cfg_json = json.dumps(server_config or {}, separators=(",", ":"))
    shim = tmp_path / name
    shim.write_text(
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
        f"'--state-file', {str(state_file)!r}, '--config-json', {cfg_json!r}])\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    return shim


def _read_launches(launch_log: Path) -> list[dict[str, Any]]:
    if not launch_log.exists():
        return []
    return [json.loads(line) for line in launch_log.read_text(encoding="utf-8").splitlines() if line.strip()]


class _InMemorySessions:
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

    def get_agent_session_mapping(self, session_key: str, agent_name: str, anchor: str) -> str | None:
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
    def __init__(self, *, blocks_reclamation: bool = False) -> None:
        self.blocks_reclamation = blocks_reclamation
        self.calls: list[tuple[str, ...]] = []

    def _make_snapshot(self, target: Any) -> RuntimeTargetOwnershipSnapshot:
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

    def snapshot_many(self, targets: tuple[Any, ...]) -> tuple[RuntimeTargetOwnershipSnapshot, ...]:
        self.calls.append(tuple(t.resource_key for t in targets))
        return tuple(self._make_snapshot(t) for t in targets)

    def snapshot(self, target: Any) -> RuntimeTargetOwnershipSnapshot:
        self.calls.append((target.resource_key,))
        return self._make_snapshot(target)


class _DummyFormatter:
    def format_result_footer(self, subtype: str, duration_ms: int, *, token_field: str = "") -> str:
        return f"{subtype}:{duration_ms}ms:{token_field}"


@dataclass
class _DummyController:
    config: Any
    sessions: _InMemorySessions = field(default_factory=_InMemorySessions)
    runtime_activation: RuntimeActivationRegistry = field(default_factory=RuntimeActivationRegistry)
    runtime_ownership: _FakeOwnershipProvider = field(default_factory=_FakeOwnershipProvider)
    emitted: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    token_notes: list[tuple[Any, int]] = field(default_factory=list)
    primary_platform: str = "slack"

    def __post_init__(self) -> None:
        self.settings_manager = self.sessions
        self.im_client = SimpleNamespace(formatter=_DummyFormatter())
        self.agent_service = SimpleNamespace(agents={})
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
        self.token_notes.append((context, total))


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
    )


def _antigravity_config(
    cli_path: Path | str,
    *,
    enabled: bool = True,
    auth_mode: str = "oauth",
    api_key: str | None = None,
    base_url: str | None = None,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
    extra_args: list[str] | None = None,
    idle_timeout_seconds: int = 600,
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        cli_path=str(cli_path),
        auth_mode=auth_mode,
        api_key=api_key,
        base_url=base_url,
        gcp_project=gcp_project,
        gcp_location=gcp_location,
        extra_args=list(extra_args or []),
        idle_timeout_seconds=idle_timeout_seconds,
    )


def _app_config(agy_cfg: Any) -> SimpleNamespace:
    return SimpleNamespace(
        antigravity=agy_cfg,
        language="en",
        show_duration=False,
        include_time_info=False,
        include_user_info=False,
        reply_enhancements=False,
    )


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no test ever reads or writes real user HOME or ~/.avibe state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    avibe_home = tmp_path / "avibe-home"
    avibe_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


# ---------------------------------------------------------------------------
# 1. Launch argv/env contract & passive native_sessions provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_argv_and_env_contract_for_execution_and_passive_list(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(
        shim,
        auth_mode="api_key",
        api_key="test-gemini-key",
        base_url="https://gemini.example.test/v1",
        extra_args=["--sandbox"],
    )
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req = _make_request(
            message="stream_all",
            workdir=workdir,
            model="model-alpha",
            subagent_name="reviewer",
        )
        await agent.handle_message(req)

        launches = _read_launches(launch_log)
        assert len(launches) == 1
        exec_launch = launches[0]
        assert exec_launch["argv"] == [
            "acp",
            "--disable-telemetry",
            "--dangerously-skip-permissions",
            "--sandbox",
            "--agent",
            "reviewer",
        ]
        assert exec_launch["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert exec_launch["env"]["GEMINI_API_KEY"] == "test-gemini-key"
        assert exec_launch["env"]["GOOGLE_GEMINI_BASE_URL"] == "https://gemini.example.test/v1"
        assert exec_launch["env"][AVIBE_SESSION_ID_ENV] == "ses_1"

        # Verify agent-platform auth env mapping cleanly as well.
        adc_cfg = _antigravity_config(
            shim,
            auth_mode="agent_platform",
            gcp_project="proj-123",
            gcp_location="us-central1",
        )
        adc_env = build_antigravity_env(adc_cfg, base_env={"PATH": os.environ.get("PATH", "")})
        assert adc_env[AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert adc_env["GOOGLE_CLOUD_PROJECT"] == "proj-123"
        assert adc_env["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert "--dangerously-skip-permissions" not in build_antigravity_command("agy", execution=False)

        # Passive native_sessions provider MUST NOT pass --dangerously-skip-permissions.
        provider = AntigravityNativeSessionProvider(cli_path=str(shim), config=agy_cfg)
        items = await asyncio.to_thread(provider.list_metadata, str(workdir))
        assert len(items) == 1
        assert items[0].agent == "antigravity"
        assert items[0].agent_prefix == "ag"
        assert items[0].native_session_id == "sess-1"
        hydrated = provider.hydrate_preview(items[0])
        assert hydrated.last_agent_message == "Update greeting"
        title = await asyncio.to_thread(
            provider.get_title,
            native_session_id="sess-1",
            working_path=str(workdir),
        )
        assert title is not None
        assert title.title == "Update greeting"
        assert title.source == "backend"

        launches = _read_launches(launch_log)
        assert len(launches) >= 2
        for passive_launch in launches[1:]:
            assert "--dangerously-skip-permissions" not in passive_launch["argv"]
            assert "--disable-telemetry" in passive_launch["argv"]
            assert passive_launch["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"

        assert any(spec.agent_name == "antigravity" for spec in DEFAULT_PROVIDER_SPECS)
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 2. Explicit model + dependent effort setting & failure on unsupported/missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_and_dependent_effort_applied_in_order_and_fails_explicitly(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        # Missing model -> fails explicitly BEFORE spawning child (never falls back to CLI default).
        missing_req = _make_request(message="hello", workdir=workdir, model=None)
        await agent.handle_message(missing_req)
        assert _read_launches(launch_log) == []
        notify_events = [e for e in controller.emitted if e[0] == "notify"]
        result_events = [e for e in controller.emitted if e[0] == "result"]
        assert len(notify_events) == 1
        assert "No Antigravity model selected" in notify_events[0][1]
        assert len(result_events) == 1
        assert result_events[0][2].get("is_error") is True

        # Supported model + dependent effort: model-beta + high.
        # Verify model is applied BEFORE dependent thought_level by checking RPC order.
        controller.emitted.clear()
        rpc_order: list[tuple[str, Any]] = []
        orig_set_opt = agent._get_or_create_session

        async def _instrumented_get_or_create(*args: Any, **kwargs: Any) -> Any:
            sess = await orig_set_opt(*args, **kwargs)
            orig_rpc = sess.client.session_set_config_option

            async def _wrapped_set_opt(session_id: str, config_id: str, value: Any) -> dict[str, Any]:
                rpc_order.append((config_id, value))
                return await orig_rpc(session_id, config_id, value)

            sess.client.session_set_config_option = _wrapped_set_opt  # type: ignore[method-assign]
            return sess

        agent._get_or_create_session = _instrumented_get_or_create  # type: ignore[method-assign]
        try:
            ok_req = _make_request(message="hello", workdir=workdir, model="model-beta", effort="high")
            await agent.handle_message(ok_req)
        finally:
            agent._get_or_create_session = orig_set_opt  # type: ignore[method-assign]

        assert rpc_order == [("model", "model-beta"), ("thought_level", "high")]
        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        assert "model=model-beta" in results[0][1]
        session = agent._sessions[ok_req.composite_session_id]
        assert session.current_model == "model-beta"
        assert session.current_effort == "high"
        thought_opt = next(opt for opt in session.config_options if opt["id"] == "thought_level")
        assert thought_opt["currentValue"] == "high"

        # Unsupported model -> explicit failure, never falls back to CLI default.
        controller.emitted.clear()
        bad_model_req = _make_request(message="hello", workdir=workdir, model="nonexistent-model")
        await agent.handle_message(bad_model_req)
        bad_notifies = [e for e in controller.emitted if e[0] == "notify"]
        bad_results = [e for e in controller.emitted if e[0] == "result"]
        assert len(bad_notifies) == 1
        assert "nonexistent-model" in bad_notifies[0][1]
        assert len(bad_results) == 1
        assert bad_results[0][2].get("is_error") is True

        # Unsupported effort -> explicit failure, never sends prompt.
        controller.emitted.clear()
        bad_effort_req = _make_request(
            message="hello",
            workdir=workdir,
            model="model-beta",
            effort="ultra-invalid",
        )
        await agent.handle_message(bad_effort_req)
        bad_effort_notifies = [e for e in controller.emitted if e[0] == "notify"]
        assert len(bad_effort_notifies) == 1
        assert "ultra-invalid" in bad_effort_notifies[0][1]
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 3. Streaming Narration, ToolActivity (incl. diff), usage_update, permissions, single TurnEnd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_updates_usage_permissions_and_single_turn_end(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req = _make_request(message="stream_all", workdir=workdir, model="model-alpha")
        await agent.handle_message(req)

        narrations = [e for e in controller.emitted if e[0] == "assistant"]
        toolcalls = [e for e in controller.emitted if e[0] == "toolcall"]
        results = [e for e in controller.emitted if e[0] == "result"]

        assert len(narrations) == 1
        assert narrations[0][1] == "planning edit"
        assert narrations[0][2].get("level") == "process"

        assert len(toolcalls) == 2
        assert any("diff: hello.py" in tc[1] and "print('new')" in tc[1] for tc in toolcalls)

        assert len(results) == 1
        assert results[0][1] == "Updated hello.py."
        assert controller.token_notes == [(req.context, 1200)]

        # Permission request selects explicit allow_once; unknown server request gets -32601.
        controller.emitted.clear()
        perm_req = _make_request(message="request_permission", workdir=workdir, model="model-alpha")
        await agent.handle_message(perm_req)
        perm_results = [e for e in controller.emitted if e[0] == "result"]
        assert len(perm_results) == 1
        assert perm_results[0][1] == "permission:allow_once"

        controller.emitted.clear()
        unk_req = _make_request(message="request_unknown", workdir=workdir, model="model-alpha")
        await agent.handle_message(unk_req)
        unk_results = [e for e in controller.emitted if e[0] == "result"]
        assert len(unk_results) == 1
        assert unk_results[0][1] == "unknown_req_error:-32601"
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 4. handle_stop (session/cancel) & mid-turn process exit -> single TurnEnd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stop_and_mid_turn_crash_emit_single_terminal(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        # 4a. handle_stop sends session/cancel and coalesces to exactly ONE TurnEnd(STOPPED).
        stop_req = _make_request(message="wait_cancel", workdir=workdir, model="model-alpha")
        turn_task = asyncio.create_task(agent.handle_message(stop_req))

        for _ in range(100):
            if agent.runtime_has_active_turns():
                break
            await asyncio.sleep(0.02)
        assert agent.runtime_has_active_turns()

        stopped = await agent.handle_stop(stop_req)
        assert stopped is True
        await turn_task

        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        assert results[0][2]["output"].settled_by == SETTLED_BY_STOPPED

        # 4b. Mid-turn subprocess crash -> AgentError + exactly ONE TurnEnd(FAILED), exit failure recorded.
        controller.emitted.clear()
        crash_req = _make_request(message="crash_mid_prompt", workdir=workdir, model="model-alpha")
        await agent.handle_message(crash_req)

        notifies = [e for e in controller.emitted if e[0] == "notify"]
        crash_results = [e for e in controller.emitted if e[0] == "result"]
        assert len(notifies) == 1
        assert "simulated peer crash" in notifies[0][1] or "exited with code 2" in notifies[0][1]
        assert len(crash_results) == 1
        assert crash_results[0][2].get("is_error") is True
        assert crash_req.composite_session_id not in agent._sessions
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 5. Cross-process resume, explicit missing-resume failure, and fork fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_process_resume_and_explicit_missing_resume_failure(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    shared_sessions = _InMemorySessions()

    # Process 1 creates sess-1 and runs turn 1.
    controller1 = _DummyController(_app_config(agy_cfg), sessions=shared_sessions)
    agent1 = AntigravityAgent(controller1, agy_cfg, rules_base_dir=tmp_path / "rules")
    req1 = _make_request(message="first-turn-marker", workdir=workdir, model="model-alpha")
    await agent1.handle_message(req1)
    assert shared_sessions.get_agent_session_mapping(req1.session_key, "antigravity", req1.base_session_id) == "sess-1"
    await agent1.shutdown_runtime()

    # Process 2 (fresh AntigravityAgent) resumes sess-1 via session/resume and recalls turn 1.
    controller2 = _DummyController(_app_config(agy_cfg), sessions=shared_sessions)
    agent2 = AntigravityAgent(controller2, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req2 = _make_request(message="recall", workdir=workdir, model="model-alpha")
        await agent2.handle_message(req2)
        results = [e for e in controller2.emitted if e[0] == "result"]
        assert len(results) == 1
        assert "first-turn-marker" in results[0][1]
        # Verify additionalDirectories passed our Avibe-owned rules directory.
        assert str(tmp_path / "rules") in results[0][1]

        # Missing / invalid native ID -> explicit failure, NEVER silently calls session/new.
        controller2.emitted.clear()
        bad_req = _make_request(
            message="hello",
            workdir=workdir,
            base_session_id="slack_999.9",
            model="model-alpha",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_missing",
                    "agent_backend": "antigravity",
                    "native_session_id": "sess-does-not-exist",
                }
            },
        )
        await agent2.handle_message(bad_req)
        notifies = [e for e in controller2.emitted if e[0] == "notify"]
        bad_results = [e for e in controller2.emitted if e[0] == "result"]
        assert len(notifies) == 1
        assert "Could not resume Antigravity session" in notifies[0][1]
        assert len(bad_results) == 1
        assert bad_results[0][2].get("is_error") is True
        # Ensure fake server still has only sess-1 (no silent session/new created).
        saved_state = json.loads(state_file.read_text(encoding="utf-8"))
        assert list(saved_state["sessions"].keys()) == ["sess-1"]
    finally:
        await agent2.shutdown_runtime()


@pytest.mark.asyncio
async def test_fork_without_native_capability_uses_bounded_handoff(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    no_fork_caps = {
        "loadSession": True,
        "promptCapabilities": {"image": True, "embeddedContext": True},
        "sessionCapabilities": {
            "resume": {},
            "list": {},
            "close": {},
            "additionalDirectories": {},
        },
        "auth": {"logout": {}},
    }
    shim = _create_fake_agy_shim(
        tmp_path,
        state_file=state_file,
        launch_log=launch_log,
        server_config={"agentCapabilities": no_fork_caps},
    )
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        fork_req = _make_request(
            message="continue work",
            workdir=workdir,
            model="model-alpha",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_fork_child",
                    "agent_backend": "antigravity",
                    "native_session_id": "",
                    "native_session_fork": {
                        "source_session_id": "ses_parent_1",
                        "source_native_session_id": "sess-parent",
                        "source_backend": "antigravity",
                    },
                }
            },
        )
        await agent.handle_message(fork_req)
        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        assert "[Avibe handoff]" in results[0][1]
        assert "ses_parent_1" in results[0][1]
        assert "continue work" in results[0][1]
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 6. CallerContext.session_stable() reuse vs replacement + resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_context_session_stable_reuse_and_delegated_or_remote_replacement(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        # Turn 1: IM user U1, message M1
        req1 = _make_request(message="turn-1", workdir=workdir, user_id="U1", message_id="M1")
        await agent.handle_message(req1)
        pid1 = agent._sessions[req1.composite_session_id].client.pid
        assert len(_read_launches(launch_log)) == 1

        # Turn 2: SAME session, DIFFERENT IM user U2 and message M2 -> MUST REUSE same child process!
        req2 = _make_request(message="turn-2", workdir=workdir, user_id="U2", message_id="M2")
        await agent.handle_message(req2)
        pid2 = agent._sessions[req2.composite_session_id].client.pid
        assert pid2 == pid1
        assert len(_read_launches(launch_log)) == 1

        # Turn 3: Delegated run (task_execution_id / source_kind) -> MUST REPLACE child + resume native session!
        controller.emitted.clear()
        req3 = _make_request(
            message="recall",
            workdir=workdir,
            user_id="U2",
            message_id="M3",
            platform_specific={
                "task_execution_id": "run_42",
                "task_trigger_kind": "agent_run",
                "source_kind": "agent",
            },
        )
        await agent.handle_message(req3)
        pid3 = agent._sessions[req3.composite_session_id].client.pid
        assert pid3 != pid1
        launches = _read_launches(launch_log)
        assert len(launches) == 2
        assert launches[-1]["env"][AVIBE_RUN_ID_ENV] == "run_42"
        assert launches[-1]["env"][AVIBE_CALLER_SOURCE_ENV] == "agent_run"
        # Resumed same native session -> recalls turn-1 and turn-2!
        results3 = [e for e in controller.emitted if e[0] == "result"]
        assert "turn-1" in results3[0][1] and "turn-2" in results3[0][1]

        # Turn 4: Remote caller with resource_user_context -> MUST REPLACE child + resume native session!
        controller.emitted.clear()
        req4 = _make_request(
            message="recall",
            workdir=workdir,
            platform="avibe",
            user_id="remote:sub-alice",
            message_id="M4",
            platform_specific={
                "message_metadata": {
                    "resource_user_context": {"sub": "sub-alice", "role": "editor"},
                }
            },
        )
        await agent.handle_message(req4)
        pid4 = agent._sessions[req4.composite_session_id].client.pid
        assert pid4 != pid3
        launches = _read_launches(launch_log)
        assert len(launches) == 3
        assert json.loads(launches[-1]["env"][AVIBE_CALLER_RESOURCE_CONTEXT_ENV]) == {
            "role": "editor",
            "sub": "sub-alice",
        }
        results4 = [e for e in controller.emitted if e[0] == "result"]
        assert "turn-1" in results4[0][1]
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 7. rules.py splitting, 24,000-byte/file cap, 20,000-token budget, skip rewrite
# ---------------------------------------------------------------------------


def test_rules_rendering_splits_over_24k_and_skips_unchanged_rewrite(tmp_path: Path) -> None:
    rules_base = tmp_path / "avibe-rules"
    project_dir = tmp_path / "user-project"
    project_dir.mkdir()

    # Build content > 24,000 bytes (~52 KB across multiple paragraphs).
    paragraph = ("Rule line: follow Avibe conventions strictly. " * 20).strip()
    large_content = "\n\n".join(f"## Section {i}\n{paragraph}" for i in range(60))
    assert len(large_content.encode("utf-8")) > MAX_RULE_FILE_BYTES * 2

    first = render_session_rules("sess-key:repo", large_content, base_dir=rules_base)
    assert first.written is True
    assert len(first.directories) >= 3
    for directory in first.directories:
        rule_file = Path(directory) / "AGENTS.md"
        assert rule_file.is_file()
        assert len(rule_file.read_bytes()) <= MAX_RULE_FILE_BYTES
        assert Path(directory).is_relative_to(rules_base)

    # Record mtimes and re-render identical content -> MUST skip rewrite.
    mtimes_before = {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in first.directories}
    second = render_session_rules("sess-key:repo", large_content, base_dir=rules_base)
    assert second.written is False
    assert second.digest == first.digest
    assert second.directories == first.directories
    mtimes_after = {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in second.directories}
    assert mtimes_after == mtimes_before

    # Huge content exceeding 20,000-token (~80,000-byte) total budget is capped cleanly.
    oversized = "\n".join(f"Line {i}: " + ("x" * 100) for i in range(1200))
    assert len(oversized.encode("utf-8")) > MAX_RULE_TOTAL_BYTES
    capped = render_session_rules("sess-key:capped", oversized, base_dir=rules_base)
    total_bytes = sum((Path(d) / "AGENTS.md").stat().st_size for d in capped.directories)
    assert total_bytes <= MAX_RULE_TOTAL_BYTES
    for directory in capped.directories:
        assert (Path(directory) / "AGENTS.md").stat().st_size <= MAX_RULE_FILE_BYTES

    # User project directory remained completely untouched.
    assert list(project_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# 8. Steering refuses cleanly before any write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steering_refuses_unsupported_without_writing(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    agy_cfg = _antigravity_config("agy")
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")

    req = _make_request(message="hello", workdir=workdir)
    target = ActiveSteerTarget(
        runtime_key=req.composite_session_id,
        logical_turn_id="turn-1",
        context=req.context,
        agent_request=req,
        agent=agent,
    )
    assert agent.steering_native_turn_id(target) is None

    steer_res = await agent.steer_active_turn(
        SteerRequest(
            target_session_id="ses_1",
            expected_logical_turn_id="turn-1",
            expected_native_turn_id="native-1",
            text="follow up",
        ),
        target,
    )
    assert steer_res.outcome is SteerOutcome.REFUSED
    assert steer_res.reason == "unsupported"

    reconcile_res = await agent.reconcile_steer_attempt(
        SteerReconcileRequest(
            target_session_id="ses_1",
            expected_logical_turn_id="turn-1",
            expected_native_turn_id="native-1",
            attempt_id="att-1",
        ),
        target,
    )
    assert reconcile_res.outcome is SteerOutcome.REFUSED
    assert reconcile_res.reason == "unsupported"


# ---------------------------------------------------------------------------
# 9. Idle eviction respects runtime_ownership.blocks_reclamation & renewal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_eviction_respects_runtime_ownership_and_renewal(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    ownership = _FakeOwnershipProvider(blocks_reclamation=True)
    controller = _DummyController(_app_config(agy_cfg), runtime_ownership=ownership)
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req = _make_request(message="hello", workdir=workdir, model="model-alpha")
        await agent.handle_message(req)
        session = agent._sessions[req.composite_session_id]
        session.last_activity = time.monotonic() - 120.0

        # Blocked by durable ownership -> MUST NOT evict even though idle > timeout.
        assert await agent.evict_idle_transports(10.0) == 0
        assert req.composite_session_id in agent._sessions
        assert session.client.is_alive

        # Once durable ownership permits reclamation -> evicts cleanly and retires activation.
        ownership.blocks_reclamation = False
        assert await agent.evict_idle_transports(10.0) == 1
        assert req.composite_session_id not in agent._sessions
        assert not session.client.is_alive
        assert session.activation is not None
        assert not controller.runtime_activation.is_current(session.activation)

        # Routine config save (live field only) does NOT bump epoch; launch change DOES and reaps when idle.
        await agent.handle_message(req)
        session2 = agent._sessions[req.composite_session_id]
        await agent.renew_runtime(_antigravity_config(shim, idle_timeout_seconds=300), config_save=True)
        await agent.reap_runtime_generations()
        assert req.composite_session_id in agent._sessions

        await agent.renew_runtime(_antigravity_config(shim, extra_args=["--sandbox"]), config_save=True)
        await agent.reap_runtime_generations()
        assert req.composite_session_id not in agent._sessions
        assert not session2.client.is_alive
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 10. Sibling runtime isolation (runtime_turn_key) & running_agents wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sibling_runtime_isolation_and_running_agents_end(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / "acp-state.json"
    launch_log = tmp_path / "launches.jsonl"
    shim = _create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=launch_log)

    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    controller.agent_service.agents["antigravity"] = agent
    try:
        req_a = _make_request(
            message="hello-a",
            workdir=workdir,
            base_session_id="slack_thread_A",
            session_key="slack::channel::C1",
        )
        req_b = _make_request(
            message="hello-b",
            workdir=workdir,
            base_session_id="slack_thread_B",
            session_key="slack::channel::C2",
        )
        await asyncio.gather(agent.handle_message(req_a), agent.handle_message(req_b))

        sess_a = agent._sessions[req_a.composite_session_id]
        sess_b = agent._sessions[req_b.composite_session_id]
        assert sess_a.client.pid != sess_b.client.pid
        assert sess_a.activation != sess_b.activation
        assert sess_a.spec.rules_dirs != sess_b.spec.rules_dirs
        result_texts = [text for msg_type, text, _ in controller.emitted if msg_type == "result"]
        assert any("echo:hello-a" in text for text in result_texts)
        assert any("echo:hello-b" in text for text in result_texts)
        assert agent.runtime_activation_identity_for_request(req_a) == sess_a.activation
        # backend_composite_session_id takes precedence over composite_session_id (matches runtime_turn_key).
        override_key = f"slack_override_key:{workdir}"
        req_override = _make_request(
            message="hello-override",
            workdir=workdir,
            base_session_id="slack_thread_override",
            session_key="slack::channel::C3",
            platform_specific={"backend_composite_session_id": override_key},
        )
        await agent.handle_message(req_override)
        sess_override = agent._sessions[override_key]
        assert agent.runtime_activation_identity_for_request(req_override) == sess_override.activation
        assert (
            _resolve_live_state(
                controller,
                backend="antigravity",
                session_id=None,
                composite_key=override_key,
                base_session_id="slack_thread_override",
            )
            == "idle"
        )
        # Once the child dies without an active turn, both _resolve_live_state and snapshot skip it.
        await sess_override.client.close()
        assert (
            _resolve_live_state(
                controller,
                backend="antigravity",
                session_id=None,
                composite_key=override_key,
                base_session_id="slack_thread_override",
            )
            is None
        )
        snap_with_dead = snapshot_running_agents(controller)
        assert "slack_thread_override" not in {
            r["base_session_id"] for r in snap_with_dead["agents"] if r["backend"] == "antigravity"
        }
        await agent.end_session("slack_thread_override")

        assert agent.runtime_activation_identity_for_session_binding(
            session_anchor="slack_thread_A",
            workdir=str(workdir),
        ) == sess_a.activation
        snapshots = agent.runtime_ownership_snapshots()
        assert snapshots is not None and len(snapshots) == 2

        # snapshot_running_agents sees both live idle rows.
        snap = snapshot_running_agents(controller)
        agy_rows = [r for r in snap["agents"] if r["backend"] == "antigravity"]
        assert {r["base_session_id"] for r in agy_rows} == {"slack_thread_A", "slack_thread_B"}
        assert all(r["state"] == "idle" and isinstance(r["pid"], int) for r in agy_rows)

        # Ending sibling A kills only A; sibling B stays alive and unaffected.
        ended = await end_running_agent(
            controller,
            backend="antigravity",
            state="idle",
            base_session_id="slack_thread_A",
        )
        assert ended["ok"] is True
        assert ended["process_killed"] is True
        assert req_a.composite_session_id not in agent._sessions
        assert req_b.composite_session_id in agent._sessions
        assert sess_b.client.is_alive

        # clear_sessions clears only the targeted session_key.
        cleared = await agent.clear_sessions("slack::channel::C2")
        assert cleared == 1
        assert req_b.composite_session_id not in agent._sessions
        assert not sess_b.client.is_alive
    finally:
        await agent.shutdown_runtime()
