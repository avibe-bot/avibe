"""Hermetic tests for AntigravityAgent, rules rendering, model mapping, native sessions, and host hooks."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from core.caller_context import (
    AVIBE_CALLER_RESOURCE_CONTEXT_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
)
from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from core.run_settlement import SETTLED_BY_STOPPED
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
    build_antigravity_command,
    build_antigravity_env,
    render_session_rules,
)
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.providers import DEFAULT_PROVIDER_SPECS
from tests.helpers.antigravity_harness import (
    FakeOwnershipProvider,
    FakeSessions,
    antigravity_config,
    make_agent_harness,
    make_request,
)


@pytest.mark.asyncio
async def test_launch_argv_and_env_contract_for_execution_and_passive_list(tmp_path: Path) -> None:
    async with make_agent_harness(
        tmp_path,
        auth_mode="api_key",
        api_key="test-gemini-key",
        base_url="https://gemini.example.test/v1",
        extra_args=["--sandbox"],
    ) as h:
        req = make_request(message="stream_all", workdir=h.workdir, model="model-alpha", subagent_name="reviewer")
        await h.agent.handle_message(req)

        launches = h.launches()
        assert len(launches) == 1
        assert launches[0]["argv"] == [
            "acp",
            "--disable-telemetry",
            "--dangerously-skip-permissions",
            "--sandbox",
            "--agent",
            "reviewer",
        ]
        assert launches[0]["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert launches[0]["env"]["GEMINI_API_KEY"] == "test-gemini-key"
        assert launches[0]["env"]["GOOGLE_GEMINI_BASE_URL"] == "https://gemini.example.test/v1"
        assert launches[0]["env"][AVIBE_SESSION_ID_ENV] == "ses_1"

        adc_cfg = antigravity_config(
            h.shim,
            auth_mode="agent_platform",
            gcp_project="proj-123",
            gcp_location="us-central1",
        )
        adc_env = build_antigravity_env(adc_cfg, base_env={"PATH": os.environ.get("PATH", "")})
        assert adc_env[AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert adc_env["GOOGLE_CLOUD_PROJECT"] == "proj-123"
        assert adc_env["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert "--dangerously-skip-permissions" not in build_antigravity_command("agy", execution=False)

        provider = AntigravityNativeSessionProvider(cli_path=str(h.shim), config=h.agy_cfg)
        items = await asyncio.to_thread(provider.list_metadata, str(h.workdir))
        assert len(items) == 1
        assert (items[0].agent, items[0].agent_prefix, items[0].native_session_id) == ("antigravity", "ag", "sess-1")
        assert provider.hydrate_preview(items[0]).last_agent_message == "Update greeting"
        title = await asyncio.to_thread(provider.get_title, native_session_id="sess-1", working_path=str(h.workdir))
        assert title is not None and title.title == "Update greeting" and title.source == "backend"

        all_launches = h.launches()
        assert len(all_launches) >= 2
        for passive in all_launches[1:]:
            assert "--dangerously-skip-permissions" not in passive["argv"]
            assert "--disable-telemetry" in passive["argv"]
            assert passive["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert any(spec.agent_name == "antigravity" for spec in DEFAULT_PROVIDER_SPECS)


@pytest.mark.asyncio
async def test_model_and_dependent_effort_applied_in_order_and_fails_explicitly(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, record_rpc=True) as h:
        # Missing model fails BEFORE spawning any child process (no CLI default fallback).
        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir, model=None))
        assert h.launches() == []
        assert len(h.controller.events("notify")) == 1
        assert "No Antigravity model selected" in h.controller.events("notify")[0][1]
        assert h.controller.events("result")[0][2].get("is_error") is True

        # Supported model + dependent thought_level: model RPC MUST precede thought_level RPC.
        h.controller.emitted.clear()
        ok_req = make_request(message="hello", workdir=h.workdir, model="model-beta", effort="high")
        await h.agent.handle_message(ok_req)

        set_opts = [(m["params"]["configId"], m["params"]["value"]) for m in h.rpcs("session/set_config_option")]
        assert set_opts == [("model", "model-beta"), ("thought_level", "high")]
        assert len(h.controller.events("result")) == 1
        assert "model=model-beta" in h.controller.events("result")[0][1]
        session = h.agent._sessions[ok_req.composite_session_id]
        assert (session.current_model, session.current_effort) == ("model-beta", "high")
        assert next(o for o in session.config_options if o["id"] == "thought_level")["currentValue"] == "high"

        # Unsupported model or effort fails explicitly and never sends session/prompt.
        prompts_before = len(h.rpcs("session/prompt"))
        for model, effort, expected_text in (
            ("nonexistent-model", None, "nonexistent-model"),
            ("model-beta", "ultra-invalid", "ultra-invalid"),
        ):
            h.controller.emitted.clear()
            await h.agent.handle_message(make_request(message="hello", workdir=h.workdir, model=model, effort=effort))
            notifies = h.controller.events("notify")
            results = h.controller.events("result")
            assert len(notifies) == 1 and expected_text in notifies[0][1]
            assert len(results) == 1 and results[0][2].get("is_error") is True
        assert len(h.rpcs("session/prompt")) == prompts_before


@pytest.mark.asyncio
async def test_streaming_updates_usage_permissions_and_single_turn_end(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        req = make_request(message="stream_all", workdir=h.workdir)
        await h.agent.handle_message(req)

        narrations = h.controller.events("assistant")
        toolcalls = h.controller.events("toolcall")
        results = h.controller.events("result")
        assert len(narrations) == 1 and narrations[0][1] == "planning edit" and narrations[0][2].get("level") == "process"
        assert len(toolcalls) == 2
        assert any("diff: hello.py" in tc[1] and "print('new')" in tc[1] for tc in toolcalls)
        assert len(results) == 1 and results[0][1] == "Updated hello.py."
        assert h.controller.token_notes == [(req.context, 1200)]

        for prompt, expected_reply in (
            ("request_permission", "permission:allow_once"),
            ("request_unknown", "unknown_req_error:-32601"),
        ):
            h.controller.emitted.clear()
            await h.agent.handle_message(make_request(message=prompt, workdir=h.workdir))
            res = h.controller.events("result")
            assert len(res) == 1 and res[0][1] == expected_reply


@pytest.mark.asyncio
async def test_handle_stop_and_mid_turn_crash_emit_single_terminal_and_recover(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        # handle_stop sends session/cancel and coalesces to exactly ONE TurnEnd(STOPPED).
        stop_req = make_request(message="wait_cancel", workdir=h.workdir)
        turn_task = asyncio.create_task(h.agent.handle_message(stop_req))
        for _ in range(100):
            if h.agent.runtime_has_active_turns():
                break
            await asyncio.sleep(0.02)
        assert h.agent.runtime_has_active_turns()
        assert await h.agent.handle_stop(stop_req) is True
        await turn_task
        stop_results = h.controller.events("result")
        assert len(stop_results) == 1 and stop_results[0][2]["output"].settled_by == SETTLED_BY_STOPPED

        # Mid-turn subprocess crash -> AgentError + single TurnEnd(FAILED) + eviction + clean next-turn recovery.
        h.controller.emitted.clear()
        crash_req = make_request(message="crash_mid_prompt", workdir=h.workdir)
        await h.agent.handle_message(crash_req)
        notifies = h.controller.events("notify")
        crash_results = h.controller.events("result")
        assert len(notifies) == 1
        assert "simulated peer crash" in notifies[0][1] or "exited with code 2" in notifies[0][1]
        assert len(crash_results) == 1 and crash_results[0][2].get("is_error") is True
        assert crash_req.composite_session_id not in h.agent._sessions

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recovered-ok", workdir=h.workdir))
        ok_results = h.controller.events("result")
        assert len(ok_results) == 1 and "echo:recovered-ok" in ok_results[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "server_config", "missing_cli", "platform_specific", "expected_reason", "expected_sub", "forbidden_sub"),
    [
        ("auth_new", {"requireAuth": True}, False, None, "auth_required", "-32000", None),
        (
            "auth_resume",
            {"requireAuth": True},
            False,
            {"agent_session_target": {"id": "ses_existing", "agent_backend": "antigravity", "native_session_id": "sess-1"}},
            "auth_required",
            "-32000",
            "Could not resume",
        ),
        ("cli_not_found", None, True, None, "cli_not_found", "CLI not found", None),
    ],
)
async def test_prewrite_failures_classify_and_emit_single_error(
    tmp_path: Path,
    case_id: str,
    server_config: dict[str, Any] | None,
    missing_cli: bool,
    platform_specific: dict[str, Any] | None,
    expected_reason: str,
    expected_sub: str,
    forbidden_sub: str | None,
) -> None:
    cli_path = (tmp_path / "does-not-exist-agy") if missing_cli else None
    async with make_agent_harness(
        tmp_path,
        server_config=server_config,
        cli_path=cli_path,
        name=f"fake-agy-{case_id}",
    ) as h:
        req = make_request(
            message="hello",
            workdir=h.workdir,
            base_session_id=f"slack_{case_id}",
            platform_specific=platform_specific,
        )
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await h.agent.handle_message(req)

        assert prewrite_failure_evidence(req.context).get("reason") == expected_reason
        notifies = h.controller.events("notify")
        results = h.controller.events("result")
        assert len(notifies) == 1 and expected_sub in notifies[0][1]
        if expected_reason == "auth_required" and case_id == "auth_new":
            assert "authentication required" in notifies[0][1].lower()
        if forbidden_sub is not None:
            assert forbidden_sub not in notifies[0][1]
        assert len(results) == 1 and results[0][2].get("is_error") is True


@pytest.mark.asyncio
async def test_cross_process_resume_and_explicit_missing_resume_failure(tmp_path: Path) -> None:
    shared_sessions = FakeSessions()
    workdir = tmp_path / "repo"
    workdir.mkdir()

    # Process 1 creates sess-1 and runs turn 1.
    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared_sessions, name="resume") as h1:
        req1 = make_request(message="first-turn-marker", workdir=workdir)
        await h1.agent.handle_message(req1)
        assert shared_sessions.get_agent_session_mapping(req1.session_key, "antigravity", req1.base_session_id) == "sess-1"

    # Process 2 (fresh AntigravityAgent sharing state_file) resumes sess-1 via session/resume.
    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared_sessions, name="resume") as h2:
        await h2.agent.handle_message(make_request(message="recall", workdir=workdir))
        results = h2.controller.events("result")
        assert len(results) == 1
        assert "first-turn-marker" in results[0][1]
        assert str(h2.rules_dir) in results[0][1]

        # Missing / invalid native ID fails explicitly and NEVER silently calls session/new.
        h2.controller.emitted.clear()
        bad_req = make_request(
            message="hello",
            workdir=workdir,
            base_session_id="slack_999.9",
            platform_specific={
                "agent_session_target": {
                    "id": "ses_missing",
                    "agent_backend": "antigravity",
                    "native_session_id": "sess-does-not-exist",
                }
            },
        )
        await h2.agent.handle_message(bad_req)
        notifies = h2.controller.events("notify")
        bad_results = h2.controller.events("result")
        assert len(notifies) == 1 and "Could not resume Antigravity session" in notifies[0][1]
        assert len(bad_results) == 1 and bad_results[0][2].get("is_error") is True
        assert list(json.loads(h2.state_file.read_text(encoding="utf-8"))["sessions"].keys()) == ["sess-1"]


@pytest.mark.asyncio
async def test_caller_context_session_stable_reuse_replacement_and_sibling_isolation(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        # Start sibling Session B first; it must remain untouched through all Session A replacements.
        req_b = make_request(
            message="sibling-marker",
            workdir=h.workdir,
            base_session_id="slack_sibling_B",
            session_key="slack::channel::CB",
        )
        await h.agent.handle_message(req_b)
        sess_b = h.agent._sessions[req_b.composite_session_id]
        sibling_pid, sibling_native = sess_b.client.pid, sess_b.native_session_id

        # Turn 1: ordinary user turn (no run_id).
        req1 = make_request(message="turn-1", workdir=h.workdir, user_id="U1", message_id="M1")
        await h.agent.handle_message(req1)
        sess_a = h.agent._sessions[req1.composite_session_id]
        pid1, native_a = sess_a.client.pid, sess_a.native_session_id
        assert pid1 != sibling_pid and native_a != sibling_native
        assert AVIBE_RUN_ID_ENV not in h.launches()[-1]["env"]
        assert len(h.launches()) == 2

        # Turn 2: same session, different IM user/message -> MUST REUSE same child process.
        await h.agent.handle_message(make_request(message="turn-2", workdir=h.workdir, user_id="U2", message_id="M2"))
        assert h.agent._sessions[req1.composite_session_id].client.pid == pid1
        assert len(h.launches()) == 2

        # Turn 3: delegated run -> MUST replace child + resume same native session.
        h.controller.emitted.clear()
        await h.agent.handle_message(
            make_request(
                message="recall",
                workdir=h.workdir,
                user_id="U2",
                message_id="M3",
                platform_specific={
                    "task_execution_id": "run_42",
                    "task_trigger_kind": "agent_run",
                    "source_kind": "agent",
                },
            )
        )
        pid3 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid3 != pid1 and h.agent._sessions[req1.composite_session_id].native_session_id == native_a
        assert h.launches()[-1]["env"][AVIBE_RUN_ID_ENV] == "run_42"
        assert h.launches()[-1]["env"][AVIBE_CALLER_SOURCE_ENV] == "agent_run"
        assert "turn-1" in h.controller.events("result")[0][1] and "turn-2" in h.controller.events("result")[0][1]

        # Turn 4: remote caller with resource_user_context -> MUST replace child + resume native session.
        h.controller.emitted.clear()
        await h.agent.handle_message(
            make_request(
                message="recall",
                workdir=h.workdir,
                platform="avibe",
                user_id="remote:sub-alice",
                message_id="M4",
                platform_specific={"message_metadata": {"resource_user_context": {"sub": "sub-alice", "role": "editor"}}},
            )
        )
        pid4 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid4 != pid3
        assert json.loads(h.launches()[-1]["env"][AVIBE_CALLER_RESOURCE_CONTEXT_ENV]) == {
            "role": "editor",
            "sub": "sub-alice",
        }
        assert "turn-1" in h.controller.events("result")[0][1]

        # Turn 5: ordinary user turn again -> MUST replace child with zero delegated/remote authority leakage.
        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recall", workdir=h.workdir, user_id="U1", message_id="M5"))
        pid5 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid5 != pid4 and h.agent._sessions[req1.composite_session_id].native_session_id == native_a
        assert AVIBE_RUN_ID_ENV not in h.launches()[-1]["env"]
        assert h.launches()[-1]["env"].get(AVIBE_CALLER_SOURCE_ENV) != "agent_run"
        assert "turn-1" in h.controller.events("result")[0][1]

        # Sibling Session B remained alive and strictly isolated throughout.
        assert h.agent._sessions[req_b.composite_session_id].client.pid == sibling_pid
        assert h.agent._sessions[req_b.composite_session_id].client.is_alive
        h.controller.emitted.clear()
        await h.agent.handle_message(
            make_request(
                message="recall",
                workdir=h.workdir,
                base_session_id="slack_sibling_B",
                session_key="slack::channel::CB",
            )
        )
        sib_reply = h.controller.events("result")[0][1]
        assert "sibling-marker" in sib_reply and "turn-1" not in sib_reply


def test_rules_rendering_splits_over_24k_and_skips_unchanged_rewrite(tmp_path: Path) -> None:
    rules_base = tmp_path / "avibe-rules"
    project_dir = tmp_path / "user-project"
    project_dir.mkdir()

    paragraph = ("Rule line: follow Avibe conventions strictly. " * 20).strip()
    large_content = "\n\n".join(f"## Section {i}\n{paragraph}" for i in range(60))
    assert len(large_content.encode("utf-8")) > MAX_RULE_FILE_BYTES * 2

    first = render_session_rules("sess-key:repo", large_content, base_dir=rules_base)
    assert first.written is True and len(first.directories) >= 3
    for directory in first.directories:
        rule_file = Path(directory) / "AGENTS.md"
        assert rule_file.is_file()
        assert len(rule_file.read_bytes()) <= MAX_RULE_FILE_BYTES
        assert Path(directory).is_relative_to(rules_base)

    mtimes_before = {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in first.directories}
    second = render_session_rules("sess-key:repo", large_content, base_dir=rules_base)
    assert second.written is False and second.digest == first.digest and second.directories == first.directories
    assert {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in second.directories} == mtimes_before

    oversized = "\n".join(f"Line {i}: " + ("x" * 100) for i in range(1200))
    assert len(oversized.encode("utf-8")) > MAX_RULE_TOTAL_BYTES
    capped = render_session_rules("sess-key:capped", oversized, base_dir=rules_base)
    assert sum((Path(d) / "AGENTS.md").stat().st_size for d in capped.directories) <= MAX_RULE_TOTAL_BYTES
    assert all((Path(d) / "AGENTS.md").stat().st_size <= MAX_RULE_FILE_BYTES for d in capped.directories)
    assert list(project_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_steering_refuses_unsupported_without_writing(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, cli_path="agy") as h:
        req = make_request(message="hello", workdir=h.workdir)
        target = ActiveSteerTarget(
            runtime_key=req.composite_session_id,
            logical_turn_id="turn-1",
            context=req.context,
            agent_request=req,
            agent=h.agent,
        )
        assert h.agent.steering_native_turn_id(target) is None

        steer_res = await h.agent.steer_active_turn(
            SteerRequest(
                target_session_id="ses_1",
                expected_logical_turn_id="turn-1",
                expected_native_turn_id="native-1",
                text="follow up",
            ),
            target,
        )
        reconcile_res = await h.agent.reconcile_steer_attempt(
            SteerReconcileRequest(
                target_session_id="ses_1",
                expected_logical_turn_id="turn-1",
                expected_native_turn_id="native-1",
                attempt_id="att-1",
            ),
            target,
        )
        assert (steer_res.outcome, steer_res.reason) == (SteerOutcome.REFUSED, "unsupported")
        assert (reconcile_res.outcome, reconcile_res.reason) == (SteerOutcome.REFUSED, "unsupported")


@pytest.mark.asyncio
async def test_idle_eviction_respects_runtime_ownership_and_renewal(tmp_path: Path) -> None:
    ownership = FakeOwnershipProvider(blocks_reclamation=True)
    async with make_agent_harness(tmp_path, ownership=ownership) as h:
        req = make_request(message="hello", workdir=h.workdir)
        await h.agent.handle_message(req)
        session = h.agent._sessions[req.composite_session_id]
        session.last_activity = time.monotonic() - 120.0

        assert await h.agent.evict_idle_transports(10.0) == 0
        assert req.composite_session_id in h.agent._sessions and session.client.is_alive

        ownership.blocks_reclamation = False
        assert await h.agent.evict_idle_transports(10.0) == 1
        assert req.composite_session_id not in h.agent._sessions and not session.client.is_alive
        assert session.activation is not None
        assert not h.controller.runtime_activation.is_current(session.activation)

        # Routine config save keeps session; launch-affecting change bumps generation and reaps when idle.
        await h.agent.handle_message(req)
        session2 = h.agent._sessions[req.composite_session_id]
        await h.agent.renew_runtime(antigravity_config(h.shim, idle_timeout_seconds=300), config_save=True)
        await h.agent.reap_runtime_generations()
        assert req.composite_session_id in h.agent._sessions

        await h.agent.renew_runtime(antigravity_config(h.shim, extra_args=["--sandbox"]), config_save=True)
        await h.agent.reap_runtime_generations()
        assert req.composite_session_id not in h.agent._sessions and not session2.client.is_alive


@pytest.mark.asyncio
async def test_sibling_runtime_isolation_and_running_agents_end(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        req_a = make_request(
            message="hello-a", workdir=h.workdir, base_session_id="slack_thread_A", session_key="slack::channel::C1"
        )
        req_b = make_request(
            message="hello-b", workdir=h.workdir, base_session_id="slack_thread_B", session_key="slack::channel::C2"
        )
        await asyncio.gather(h.agent.handle_message(req_a), h.agent.handle_message(req_b))

        sess_a = h.agent._sessions[req_a.composite_session_id]
        sess_b = h.agent._sessions[req_b.composite_session_id]
        assert sess_a.client.pid != sess_b.client.pid
        assert sess_a.activation != sess_b.activation
        assert sess_a.spec.rules_dirs != sess_b.spec.rules_dirs
        result_texts = [text for _, text, _ in h.controller.events("result")]
        assert any("echo:hello-a" in text for text in result_texts)
        assert any("echo:hello-b" in text for text in result_texts)
        assert h.agent.runtime_activation_identity_for_request(req_a) == sess_a.activation

        # backend_composite_session_id override + dead-session guard on _resolve_live_state/snapshot.
        override_key = f"slack_override_key:{h.workdir}"
        req_override = make_request(
            message="hello-override",
            workdir=h.workdir,
            base_session_id="slack_thread_override",
            session_key="slack::channel::C3",
            platform_specific={"backend_composite_session_id": override_key},
        )
        await h.agent.handle_message(req_override)
        sess_override = h.agent._sessions[override_key]
        assert h.agent.runtime_activation_identity_for_request(req_override) == sess_override.activation
        assert (
            _resolve_live_state(
                h.controller,
                backend="antigravity",
                session_id=None,
                composite_key=override_key,
                base_session_id="slack_thread_override",
            )
            == "idle"
        )
        await sess_override.client.close()
        assert (
            _resolve_live_state(
                h.controller,
                backend="antigravity",
                session_id=None,
                composite_key=override_key,
                base_session_id="slack_thread_override",
            )
            is None
        )
        snap_dead = snapshot_running_agents(h.controller)
        assert "slack_thread_override" not in {
            r["base_session_id"] for r in snap_dead["agents"] if r["backend"] == "antigravity"
        }
        await h.agent.end_session("slack_thread_override")

        assert (
            h.agent.runtime_activation_identity_for_session_binding(
                session_anchor="slack_thread_A",
                workdir=str(h.workdir),
            )
            == sess_a.activation
        )
        snapshots = h.agent.runtime_ownership_snapshots()
        assert snapshots is not None and len(snapshots) == 2

        agy_rows = [r for r in snapshot_running_agents(h.controller)["agents"] if r["backend"] == "antigravity"]
        assert {r["base_session_id"] for r in agy_rows} == {"slack_thread_A", "slack_thread_B"}
        assert all(r["state"] == "idle" and isinstance(r["pid"], int) for r in agy_rows)

        ended = await end_running_agent(
            h.controller, backend="antigravity", state="idle", base_session_id="slack_thread_A"
        )
        assert ended["ok"] is True and ended["process_killed"] is True
        assert req_a.composite_session_id not in h.agent._sessions
        assert req_b.composite_session_id in h.agent._sessions and sess_b.client.is_alive

        assert await h.agent.clear_sessions("slack::channel::C2") == 1
        assert req_b.composite_session_id not in h.agent._sessions and not sess_b.client.is_alive
