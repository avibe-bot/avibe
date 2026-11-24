"""Hermetic regression tests for Antigravity adapter P1/P2 review fixes."""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agent_tool_policy import ALWAYS_SESSION_ONLY_TOOL_NAMES, check_tool_call
from core.reply_enhancer import process_reply
from modules.agents.antigravity import (
    AntigravityAgent,
    AntigravityModelSelectionError,
    apply_model_and_effort,
    render_session_rules,
)
from modules.agents.antigravity import agent as agy_agent_module
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.im import MessageContext
from tests.helpers.fake_acp_server import FAKE_ACP_SERVER_PATH


def _create_fake_agy_shim(
    tmp_path: Path,
    *,
    state_file: Path,
    server_config: dict[str, Any] | None = None,
    require_auth_method: str | None = None,
    name: str = "fake-agy-review",
) -> Path:
    cfg_json = json.dumps(server_config or {}, separators=(",", ":"))
    extra_cli: list[str] = []
    if require_auth_method is not None:
        extra_cli = ["--require-auth-method", require_auth_method]
    shim = tmp_path / name
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"argv = [{sys.executable!r}, '-u', {str(FAKE_ACP_SERVER_PATH)!r}, "
        f"'--state-file', {str(state_file)!r}, '--config-json', {cfg_json!r}] + {extra_cli!r}\n"
        f"os.execv({sys.executable!r}, argv)\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    return shim


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

    def get_agent_session_mapping(self, session_key: str, agent_name: str, anchor: str) -> str | None:
        sid = self._rows.get((session_key, agent_name, anchor))
        if not sid:
            return None
        return self._by_id.get(sid, {}).get("native_session_id") or None


class _DummyFormatter:
    def format_result_footer(self, subtype: str, duration_ms: int, *, token_field: str = "") -> str:
        return f"{subtype}:{duration_ms}ms:{token_field}"


@dataclass
class _DummyController:
    config: Any
    sessions: _InMemorySessions = field(default_factory=_InMemorySessions)
    emitted: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    primary_platform: str = "slack"

    def __post_init__(self) -> None:
        self.settings_manager = self.sessions
        self.im_client = SimpleNamespace(formatter=_DummyFormatter())
        self.processing_indicator = None

    async def emit_agent_message(
        self,
        context: Any,
        message_type: str,
        text: str,
        **options: Any,
    ) -> str:
        del context
        self.emitted.append((message_type, text, dict(options)))
        return f"msg-{len(self.emitted)}"


def _antigravity_config(
    cli_path: Path | str,
    *,
    auth_mode: str = "oauth",
    api_key: str | None = None,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        cli_path=str(cli_path),
        auth_mode=auth_mode,
        api_key=api_key,
        base_url=None,
        gcp_project=gcp_project,
        gcp_location=gcp_location,
        extra_args=[],
        idle_timeout_seconds=600,
    )


def _app_config(
    agy_cfg: Any,
    *,
    language: str = "en",
    show_duration: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        antigravity=agy_cfg,
        language=language,
        show_duration=show_duration,
        include_time_info=False,
        include_user_info=False,
        reply_enhancements=False,
    )


def _make_request(
    *,
    message: str,
    workdir: Path,
    base_session_id: str = "slack_review_1",
    model: str | None = "model-alpha",
    effort: str | None = None,
) -> AgentRequest:
    composite = f"{base_session_id}:{workdir}"
    ctx = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform="slack",
        thread_id="100.1",
        message_id="M1",
        platform_specific={AGENT_RUNTIME_TURN_KEY: composite},
    )
    return AgentRequest(
        context=ctx,
        message=message,
        user_message=message,
        working_path=str(workdir),
        base_session_id=base_session_id,
        composite_session_id=composite,
        session_key="slack::channel::C1",
        vibe_agent_model=model,
        vibe_agent_reasoning_effort=effort,
    )


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    avibe_home = tmp_path / "avibe-home"
    avibe_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return fake_home


# ---------------------------------------------------------------------------
# 1. _build_launch runs env & rules preparation off the asyncio event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_launch_runs_env_and_rules_prep_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    agy_cfg = _antigravity_config("agy")
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")

    loop_tid = threading.get_ident()
    observed_tids: dict[str, int] = {}

    orig_build_env = agy_agent_module.build_antigravity_env
    orig_skills_env = agy_agent_module.managed_skill_environment
    orig_render_rules = agy_agent_module.render_session_rules

    def _recording_build_env(*args: Any, **kwargs: Any) -> dict[str, str]:
        observed_tids["build_antigravity_env"] = threading.get_ident()
        return orig_build_env(*args, **kwargs)

    def _recording_skills_env(*args: Any, **kwargs: Any) -> dict[str, str]:
        observed_tids["managed_skill_environment"] = threading.get_ident()
        return orig_skills_env(*args, **kwargs)

    def _recording_render_rules(*args: Any, **kwargs: Any) -> Any:
        observed_tids["render_session_rules"] = threading.get_ident()
        return orig_render_rules(*args, **kwargs)

    monkeypatch.setattr(agy_agent_module, "build_antigravity_env", _recording_build_env)
    monkeypatch.setattr(agy_agent_module, "managed_skill_environment", _recording_skills_env)
    monkeypatch.setattr(agy_agent_module, "render_session_rules", _recording_render_rules)

    req = _make_request(message="hello", workdir=workdir)
    agent.ensure_agent_session_id(req)
    await agent._build_launch(req, runtime_key=req.composite_session_id, resolved_model="model-alpha")

    assert observed_tids["build_antigravity_env"] != loop_tid
    assert observed_tids["managed_skill_environment"] != loop_tid
    assert observed_tids["render_session_rules"] != loop_tid


# ---------------------------------------------------------------------------
# 2. Post-initialize authenticate(...) in both execution adapter & native history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_mode", "required_method"),
    [
        ("api_key", "gemini-api-key"),
        ("gemini-api-key", "gemini-api-key"),
        ("agent_platform", "agent-platform"),
        ("agent-platform", "agent-platform"),
    ],
)
async def test_post_initialize_authenticate_for_adapter_and_native_sessions(
    tmp_path: Path,
    auth_mode: str,
    required_method: str,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file = tmp_path / f"state-{required_method}.json"
    shim = _create_fake_agy_shim(
        tmp_path,
        state_file=state_file,
        require_auth_method=required_method,
        name=f"fake-agy-{auth_mode}",
    )

    agy_cfg = _antigravity_config(
        shim,
        auth_mode=auth_mode,
        api_key="secret-test-key" if required_method == "gemini-api-key" else None,
        gcp_project="test-proj" if required_method == "agent-platform" else None,
        gcp_location="us-central1" if required_method == "agent-platform" else None,
    )
    shared_sessions = _InMemorySessions()

    # Process 1: execution adapter authenticates and creates session.
    ctrl1 = _DummyController(_app_config(agy_cfg), sessions=shared_sessions)
    agent1 = AntigravityAgent(ctrl1, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req1 = _make_request(message="stream_all", workdir=workdir)
        await agent1.handle_message(req1)
        results1 = [e for e in ctrl1.emitted if e[0] == "result"]
        assert len(results1) == 1
        assert results1[0][1] == "Updated hello.py."
        assert not results1[0][2].get("is_error")
    finally:
        await agent1.shutdown_runtime()

    # Process 2: fresh execution adapter authenticates and resumes session.
    ctrl2 = _DummyController(_app_config(agy_cfg), sessions=shared_sessions)
    agent2 = AntigravityAgent(ctrl2, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req2 = _make_request(message="recall", workdir=workdir)
        await agent2.handle_message(req2)
        results2 = [e for e in ctrl2.emitted if e[0] == "result"]
        assert len(results2) == 1
        assert "stream_all" in results2[0][1]
    finally:
        await agent2.shutdown_runtime()

    # Process 3: fresh native_sessions provider authenticates and lists session.
    provider = AntigravityNativeSessionProvider(cli_path=str(shim), config=agy_cfg)
    listed = await asyncio.to_thread(provider.list_metadata, str(workdir))
    assert len(listed) == 1
    assert listed[0].native_session_id == "sess-1"
    assert listed[0].locator["title"] == "Update greeting"

    # Negative check: unauthenticated mode against --require-auth-method fails cleanly.
    wrong_cfg = _antigravity_config(shim, auth_mode="oauth")
    ctrl_bad = _DummyController(_app_config(wrong_cfg))
    agent_bad = AntigravityAgent(ctrl_bad, wrong_cfg, rules_base_dir=tmp_path / "rules")
    try:
        await agent_bad.handle_message(_make_request(message="hello", workdir=workdir, base_session_id="unauth"))
        notifies = [e for e in ctrl_bad.emitted if e[0] == "notify"]
        assert len(notifies) == 1
        assert "-32000" in notifies[0][1]
    finally:
        await agent_bad.shutdown_runtime()

    bad_provider = AntigravityNativeSessionProvider(cli_path=str(shim), config=wrong_cfg)
    assert await asyncio.to_thread(bad_provider.list_metadata, str(workdir)) == []


# ---------------------------------------------------------------------------
# 3. PreToolUse hook enforces tool policy & unattributable permissions rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_tool_use_hook_enforces_policy_and_unattributable_permission_rejected(
    tmp_path: Path,
    _hermetic_home: Path,
) -> None:
    rules_base = tmp_path / "avibe-rules"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = render_session_rules("sess-hook:repo", "Follow Avibe rules.", base_dir=rules_base)
    assert first.written is True
    assert len(first.directories) == 1

    primary_dir = Path(first.directories[0])
    hooks_json_path = primary_dir / ".agents" / "hooks.json"
    hook_script_path = primary_dir / ".agents" / "avibe_tool_policy_hook.py"
    assert hooks_json_path.is_file()
    assert hook_script_path.is_file()

    hooks_doc = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    pre_entries = hooks_doc["avibe-tool-policy"]["PreToolUse"]
    assert len(pre_entries) == 1
    hook_command = pre_entries[0]["hooks"][0]["command"]
    for blocked in ALWAYS_SESSION_ONLY_TOOL_NAMES:
        assert blocked in pre_entries[0]["matcher"].split("|")

    # Execute the exact configured PreToolUse command from the user's workspace CWD
    # (proves hooks.json emits an absolute script path and does not rely on .agents/ CWD).
    assert str(hook_script_path.resolve()) in hook_command
    for blocked_tool in ("schedule", "invoke_subagent", "define_subagent", "Workflow"):
        proc = subprocess.run(
            hook_command,
            shell=True,
            cwd=str(workspace),
            input=json.dumps({"hookEventName": "PreToolUse", "toolCall": {"id": "t1", "name": blocked_tool, "args": {}}}),
            text=True,
            capture_output=True,
            check=True,
        )
        output = json.loads(proc.stdout)
        assert output["decision"] == "deny"
        assert output["reason"] == check_tool_call(blocked_tool, {}).reason

    for sync_tool in ("read_file", "run_command", "grep_search"):
        proc = subprocess.run(
            hook_command,
            shell=True,
            cwd=str(workspace),
            input=json.dumps({"hookEventName": "PreToolUse", "toolCall": {"id": "t2", "name": sync_tool, "args": {}}}),
            text=True,
            capture_output=True,
            check=True,
        )
        assert json.loads(proc.stdout) == {"decision": "allow"}

    # Malformed stdin never exits non-zero in a fail-closed hook runner.
    malformed_proc = subprocess.run(
        hook_command,
        shell=True,
        cwd=str(workspace),
        input="{not-valid-json",
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(malformed_proc.stdout) == {"decision": "allow"}

    # Identical re-render skips disk writes (preserves hook mtimes) and never touches user dirs.
    hooks_mtime = hooks_json_path.stat().st_mtime_ns
    script_mtime = hook_script_path.stat().st_mtime_ns
    second = render_session_rules("sess-hook:repo", "Follow Avibe rules.", base_dir=rules_base)
    assert second.written is False
    assert hooks_json_path.stat().st_mtime_ns == hooks_mtime
    assert hook_script_path.stat().st_mtime_ns == script_mtime
    assert list(workspace.iterdir()) == []
    assert not (_hermetic_home / ".gemini").exists()

    # Empty rules content still renders the PreToolUse policy hook directory.
    empty_rules = render_session_rules("sess-empty:repo", "", base_dir=rules_base)
    assert empty_rules.written is True
    assert (Path(empty_rules.directories[0]) / ".agents" / "hooks.json").is_file()

    # Unexpected session/request_permission without an identifiable allowed tool MUST NOT allow_once.
    agy_cfg = _antigravity_config("agy")
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=rules_base)
    dummy_session = SimpleNamespace(native_session_id="sess-1")
    options = [
        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
    ]

    for bad_params in (
        {"sessionId": "sess-1", "options": options},
        {"sessionId": "sess-1", "toolCall": {}, "options": options},
        {"sessionId": "sess-1", "toolCall": {"toolCallId": "tc-1"}, "options": options},
        {"sessionId": "sess-1", "toolCall": {"toolCallId": "tc-1", "name": "   "}, "options": options},
        {"sessionId": "sess-1", "toolCall": {"toolCallId": "tc-1", "name": "unknown"}, "options": options},
        {"sessionId": "sess-1", "toolCall": {"toolCallId": "tc-1", "name": "schedule"}, "options": options},
    ):
        resp = await agent._handle_server_request(dummy_session, "session/request_permission", bad_params)  # type: ignore[arg-type]
        assert resp == {"outcome": {"outcome": "selected", "optionId": "reject_once"}}

    # Unrecognized option kinds -> cancelled cleanly (never blindly selects unknown options).
    unrecognized_opts_resp = await agent._handle_server_request(
        dummy_session,  # type: ignore[arg-type]
        "session/request_permission",
        {
            "sessionId": "sess-1",
            "toolCall": {"toolCallId": "tc-2", "name": "read_file"},
            "options": [{"optionId": "mystery", "kind": "mystery_kind"}],
        },
    )
    assert unrecognized_opts_resp == {"outcome": {"outcome": "cancelled"}}

    # Identifiable synchronous tool with allow_once option -> allowed.
    ok_resp = await agent._handle_server_request(
        dummy_session,  # type: ignore[arg-type]
        "session/request_permission",
        {
            "sessionId": "sess-1",
            "toolCall": {"toolCallId": "tc-3", "name": "read_file", "rawInput": {"path": "a.py"}},
            "options": options,
        },
    )
    assert ok_resp == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}


# ---------------------------------------------------------------------------
# 4. Strict stopReason settlement preserves partial text & settles FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_partial", "expected_subtype"),
    [
        ("stop_reason:max_tokens", "partial:max_tokens", "error_max_turns"),
        ("stop_reason:max_turn_requests", "partial:max_turn_requests", "error_max_turns"),
        ("stop_reason:refusal", "partial:refusal", "error_during_execution"),
        ("stop_reason:unexpected_custom", "partial:unexpected_custom", "error_during_execution"),
        ("stop_reason_missing", "partial:missing", "error_during_execution"),
    ],
)
async def test_strict_stop_reason_settlement_preserves_partial_text_and_fails(
    tmp_path: Path,
    prompt: str,
    expected_partial: str,
    expected_subtype: str,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    shim = _create_fake_agy_shim(tmp_path, state_file=tmp_path / "state.json")
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg, show_duration=True))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    try:
        req = _make_request(message=prompt, workdir=workdir)
        req.started_at = req.started_at - 1.0
        await agent.handle_message(req)

        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        _, text, opts = results[0]
        assert expected_partial in text
        assert opts.get("is_error") is True
        assert opts.get("terminal_error")
        assert (opts.get("result_footer") or "").startswith(f"{expected_subtype}:")
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 5. Contradictory returned currentValue fails before session/prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradictory_current_value_raises_before_prompt(tmp_path: Path) -> None:
    base_options = [
        {
            "id": "model",
            "category": "model",
            "currentValue": "model-alpha",
            "options": [
                {"value": "model-alpha"},
                {"value": "model-beta"},
            ],
        },
        {
            "id": "thought_level",
            "category": "thought_level",
            "currentValue": "low",
            "options": [
                {"value": "low"},
                {"value": "high"},
            ],
        },
    ]

    class _ContradictingModelClient:
        async def session_set_config_option(self, session_id: str, config_id: str, value: Any) -> dict[str, Any]:
            del session_id, config_id, value
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "currentValue": "model-alpha",
                        "options": [{"value": "model-alpha"}, {"value": "model-beta"}],
                    }
                ]
            }

    with pytest.raises(AntigravityModelSelectionError) as model_exc:
        await apply_model_and_effort(
            _ContradictingModelClient(),  # type: ignore[arg-type]
            "sess-1",
            base_options,
            model="model-beta",
            effort=None,
        )
    assert model_exc.value.reason == "model_mismatch"

    class _ContradictingEffortClient:
        async def session_set_config_option(self, session_id: str, config_id: str, value: Any) -> dict[str, Any]:
            del session_id
            if config_id == "model":
                return {
                    "configOptions": [
                        {
                            "id": "model",
                            "category": "model",
                            "current_value": value,
                            "options": [{"value": "model-alpha"}, {"value": "model-beta"}],
                        },
                        {
                            "id": "thought_level",
                            "category": "thought_level",
                            "current_value": "low",
                            "options": [{"value": "low"}, {"value": "high"}],
                        },
                    ]
                }
            # Contradictory snake_case current_value returned for effort
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "current_value": "model-beta",
                        "options": [{"value": "model-alpha"}, {"value": "model-beta"}],
                    },
                    {
                        "id": "thought_level",
                        "category": "thought_level",
                        "current_value": "low",
                        "options": [{"value": "low"}, {"value": "high"}],
                    },
                ]
            }

    with pytest.raises(AntigravityModelSelectionError) as effort_exc:
        await apply_model_and_effort(
            _ContradictingEffortClient(),  # type: ignore[arg-type]
            "sess-1",
            base_options,
            model="model-beta",
            effort="high",
        )
    assert effort_exc.value.reason == "effort_mismatch"

    # End-to-end: contradictory currentValue aborts BEFORE session/prompt is ever sent.
    workdir = tmp_path / "repo"
    workdir.mkdir()
    shim = _create_fake_agy_shim(tmp_path, state_file=tmp_path / "state.json")
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    prompt_called = False

    orig_get_or_create = agent._get_or_create_session

    async def _wrapped_get_or_create(*args: Any, **kwargs: Any) -> Any:
        sess = await orig_get_or_create(*args, **kwargs)

        async def _bad_set_opt(session_id: str, config_id: str, value: Any) -> dict[str, Any]:
            del session_id, config_id, value
            return {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "currentValue": "model-alpha",
                        "options": [{"value": "model-alpha"}, {"value": "model-beta"}],
                    }
                ]
            }

        async def _guarded_prompt(*p_args: Any, **p_kwargs: Any) -> dict[str, Any]:
            nonlocal prompt_called
            prompt_called = True
            return {"stopReason": "end_turn"}

        sess.client.session_set_config_option = _bad_set_opt  # type: ignore[method-assign]
        sess.client.session_prompt = _guarded_prompt  # type: ignore[method-assign]
        return sess

    agent._get_or_create_session = _wrapped_get_or_create  # type: ignore[method-assign]
    try:
        await agent.handle_message(_make_request(message="never-sent", workdir=workdir, model="model-beta"))
        assert prompt_called is False
        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        assert results[0][2].get("is_error") is True
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 6. Image blocks in agent_message_chunk materialized off-loop into reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_message_chunk_image_blocks_materialized_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    shim = _create_fake_agy_shim(tmp_path, state_file=tmp_path / "state.json")
    agy_cfg = _antigravity_config(shim)
    controller = _DummyController(_app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")

    loop_tid = threading.get_ident()
    write_tids: list[int] = []
    orig_writer = agy_agent_module._write_reply_image_bytes

    def _recording_writer(*args: Any, **kwargs: Any) -> Path | None:
        write_tids.append(threading.get_ident())
        return orig_writer(*args, **kwargs)

    monkeypatch.setattr(agy_agent_module, "_write_reply_image_bytes", _recording_writer)

    try:
        await agent.handle_message(_make_request(message="emit_image_only", workdir=workdir))
        results = [e for e in controller.emitted if e[0] == "result"]
        assert len(results) == 1
        reply_text = results[0][1]
        assert reply_text.startswith("![image](file://")
        assert write_tids and all(tid != loop_tid for tid in write_tids)

        enhanced = process_reply(reply_text, include_quick_replies=False)
        assert len(enhanced.files) == 1
        assert enhanced.files[0].is_image is True
        saved_file = Path(enhanced.files[0].path)
        assert saved_file.is_file()
        assert saved_file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert saved_file.is_relative_to(tmp_path / "avibe-home" / "attachments" / "antigravity")

        controller.emitted.clear()
        await agent.handle_message(_make_request(message="emit_image_uri", workdir=workdir))
        uri_results = [e for e in controller.emitted if e[0] == "result"]
        assert len(uri_results) == 1
        assert uri_results[0][1] == "![image](https://example.test/chart.png)"
    finally:
        await agent.shutdown_runtime()


# ---------------------------------------------------------------------------
# 7. Localized backend user-facing failure copy via vibe/i18n
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_localized_zh_failure_and_incomplete_turn_copy(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()

    # 7a. CLI not found in zh
    missing_cfg = _antigravity_config(tmp_path / "missing-agy-cli")
    ctrl_missing = _DummyController(_app_config(missing_cfg, language="zh"))
    agent_missing = AntigravityAgent(ctrl_missing, missing_cfg, rules_base_dir=tmp_path / "rules-missing")
    try:
        await agent_missing.handle_message(_make_request(message="hello", workdir=workdir))
        notifies = [e for e in ctrl_missing.emitted if e[0] == "notify"]
        assert len(notifies) == 1
        assert "找不到 Antigravity CLI" in notifies[0][1]
    finally:
        await agent_missing.shutdown_runtime()

    # 7b. Auth required (-32000) in zh
    auth_shim = _create_fake_agy_shim(
        tmp_path,
        state_file=tmp_path / "auth-state.json",
        server_config={"requireAuth": True},
        name="fake-agy-zh-auth",
    )
    auth_cfg = _antigravity_config(auth_shim)
    ctrl_auth = _DummyController(_app_config(auth_cfg, language="zh"))
    agent_auth = AntigravityAgent(ctrl_auth, auth_cfg, rules_base_dir=tmp_path / "rules-auth")
    try:
        await agent_auth.handle_message(_make_request(message="hello", workdir=workdir))
        notifies = [e for e in ctrl_auth.emitted if e[0] == "notify"]
        assert len(notifies) == 1
        assert "需要身份验证" in notifies[0][1]
        assert "-32000" in notifies[0][1]
    finally:
        await agent_auth.shutdown_runtime()

    # 7c. Incomplete stopReason (max_tokens) in zh
    shim = _create_fake_agy_shim(tmp_path, state_file=tmp_path / "zh-stop-state.json", name="fake-agy-zh-stop")
    stop_cfg = _antigravity_config(shim)
    ctrl_stop = _DummyController(_app_config(stop_cfg, language="zh"))
    agent_stop = AntigravityAgent(ctrl_stop, stop_cfg, rules_base_dir=tmp_path / "rules-stop")
    try:
        await agent_stop.handle_message(_make_request(message="stop_reason:max_tokens", workdir=workdir))
        results = [e for e in ctrl_stop.emitted if e[0] == "result"]
        assert len(results) == 1
        assert "partial:max_tokens" in results[0][1]
        assert "回合未完整结束" in results[0][1]
        assert results[0][2].get("is_error") is True
        assert results[0][2].get("terminal_error") == "Antigravity turn stopped incomplete (max_tokens)."
    finally:
        await agent_stop.shutdown_runtime()


@pytest.mark.asyncio
async def test_try_reuse_antigravity_login_rejects_cross_mode_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import agent_auth_service as auth_mod

    probe_calls: list[str] = []

    async def _fake_probe(binary: str, *, config: Any = None, auth_mode: str | None = None, **kwargs: Any) -> dict[str, Any]:
        probe_calls.append(str(auth_mode))
        return {"ok": True, "authenticated": True}

    monkeypatch.setattr(auth_mod, "probe_antigravity_passive_status", _fake_probe)

    service = auth_mod.AgentAuthService.__new__(auth_mod.AgentAuthService)
    saved_mode = {"value": "oauth-personal"}
    service._resolve_backend_config = lambda backend: SimpleNamespace(auth_mode=saved_mode["value"])
    service._get_cli_binary = lambda backend: "agy"

    # Switching oauth-personal -> oauth-business must NOT reuse the existing login.
    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-business")) is False
    assert probe_calls == []

    # Switching oauth-business -> oauth-personal must NOT reuse the existing login.
    saved_mode["value"] = "oauth-business"
    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-personal")) is False
    assert probe_calls == []

    # Matching saved OAuth mode is allowed to probe and reuse.
    assert await service._try_reuse_antigravity_login(SimpleNamespace(auth_method="oauth-business")) is True
    assert probe_calls == ["oauth-business"]


def test_extract_antigravity_oauth_url_buffers_split_pty_chunks() -> None:
    from modules.agents.antigravity.auth import extract_antigravity_oauth_url

    part1 = "Open URL: \x1b[36mhttps://accounts.google.com/o/oauth2/auth?client_id=agy"
    part2 = "&state=split-token-123\x1b[0m"
    part3 = "\nPaste code: "

    # Incomplete chunk without boundary must not publish a truncated URL before EOF.
    assert extract_antigravity_oauth_url(part1, eof=False) is None
    assert extract_antigravity_oauth_url(part1 + "&state=half", eof=False) is None

    # ANSI escape boundary or newline terminates the URL cleanly.
    expected = "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=split-token-123"
    assert extract_antigravity_oauth_url(part1 + part2, eof=False) == expected
    assert extract_antigravity_oauth_url(part1 + part2 + part3, eof=False) == expected

    # At EOF, a non-terminated URL at the end of output is still extracted.
    assert extract_antigravity_oauth_url(part1 + "&state=eof-only", eof=True) == (
        "https://accounts.google.com/o/oauth2/auth?client_id=agy&state=eof-only"
    )
