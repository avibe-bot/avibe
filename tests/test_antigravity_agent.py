"""Hermetic tests for AntigravityAgent, rules/hooks, model+effort selection, lifecycle, and host hooks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.v2_sessions import SessionsStore
from core.agent_tool_policy import ALWAYS_SESSION_ONLY_TOOL_NAMES, check_tool_call
from core.caller_context import AVIBE_CALLER_RESOURCE_CONTEXT_ENV, AVIBE_CALLER_SOURCE_ENV, AVIBE_RUN_ID_ENV, AVIBE_SESSION_ID_ENV
from core.native_dispatch_phase import DISPATCH_PHASE_PREWRITE, prewrite_failure_evidence, set_dispatch_phase
from core.run_settlement import SETTLED_BY_STOPPED
from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerReconcileRequest, SteerRequest
from core.services.running_agents import _resolve_live_state, end_running_agent, snapshot_running_agents
from modules.agents.acp import AcpRpcError
from modules.agents.antigravity import (
    AGY_CLI_DISABLE_AUTO_UPDATE_ENV,
    MAX_RULE_FILE_BYTES,
    MAX_RULE_TOTAL_BYTES,
    AntigravityAgent,
    AntigravityModelSelectionError,
    apply_model_and_effort,
    build_antigravity_command,
    build_antigravity_env,
    render_session_rules,
)
from modules.agents.antigravity import agent as agy_agent_module
from modules.agents.antigravity.agent import resolve_antigravity_cli_path
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.providers import DEFAULT_PROVIDER_SPECS
from modules.sessions_facade import SessionsFacade
from storage.importer import ensure_sqlite_state
from tests.helpers.antigravity_harness import (
    FakeController,
    FakeOwnershipProvider,
    FakeSessions,
    antigravity_config,
    app_config,
    create_fake_agy_shim,
    make_acp_capabilities,
    make_agent_harness,
    make_model_config_options,
    make_request,
    read_jsonl,
    read_session_row,
)


def _peer_opt(state_file: Path, session_id: str, option_id: str) -> str | None:
    opts = json.loads(state_file.read_text(encoding="utf-8"))["sessions"][session_id]["configOptions"]
    return next((o.get("currentValue") for o in opts if o.get("id") == option_id), None)


@pytest.mark.asyncio
async def test_launch_argv_and_env_contract_for_execution_and_passive_list(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, auth_mode="api_key", api_key="test-gemini-key", base_url="https://gemini.example.test/v1", extra_args=["--sandbox"]) as h:
        await h.agent.handle_message(make_request(message="stream_all", workdir=h.workdir, model="model-alpha", subagent_name="reviewer"))
        launches = h.launches()
        assert len(launches) == 1 and launches[0]["argv"] == ["acp", "--disable-telemetry", "--dangerously-skip-permissions", "--sandbox", "--agent", "reviewer"]
        assert (launches[0]["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV], launches[0]["env"]["GEMINI_API_KEY"], launches[0]["env"]["GOOGLE_GEMINI_BASE_URL"], launches[0]["env"][AVIBE_SESSION_ID_ENV]) == (
            "true", "test-gemini-key", "https://gemini.example.test/v1", "ses_1"
        )

        adc_env = build_antigravity_env(antigravity_config(h.shim, auth_mode="agent_platform", gcp_project="proj-123", gcp_location="us-central1"), base_env={"PATH": os.environ.get("PATH", "")})
        assert (adc_env[AGY_CLI_DISABLE_AUTO_UPDATE_ENV], adc_env["GOOGLE_CLOUD_PROJECT"], adc_env["GOOGLE_CLOUD_LOCATION"]) == ("true", "proj-123", "us-central1")
        assert "--dangerously-skip-permissions" not in build_antigravity_command("agy", execution=False)

        provider = AntigravityNativeSessionProvider(cli_path=str(h.shim), config=h.agy_cfg)
        items = await asyncio.to_thread(provider.list_metadata, str(h.workdir))
        assert len(items) == 1 and (items[0].agent, items[0].agent_prefix, items[0].native_session_id) == ("antigravity", "ag", "sess-1")
        assert provider.hydrate_preview(items[0]).last_agent_message == "Update greeting"
        title = await asyncio.to_thread(provider.get_title, native_session_id="sess-1", working_path=str(h.workdir))
        assert title is not None and (title.title, title.source) == ("Update greeting", "backend")
        assert len(h.launches()) >= 2 and all("--dangerously-skip-permissions" not in p["argv"] and "--disable-telemetry" in p["argv"] and p["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true" for p in h.launches()[1:])
        assert any(spec.agent_name == "antigravity" for spec in DEFAULT_PROVIDER_SPECS)


@pytest.mark.asyncio
async def test_live_env_never_inherits_gemini_api_key_and_missing_key_fails_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited = {
        "PATH": os.environ.get("PATH", ""),
        "GEMINI_API_KEY": "controller-secret-key",
        "GOOGLE_GEMINI_BASE_URL": "https://inherited.example.test/v1",
    }

    for mode, api_key in (("gemini-api-key", ""), ("gemini-api-key", "   "), ("api_key", ""), ("agent-platform", "ignored"), ("oauth-personal", "ignored")):
        env = build_antigravity_env(
            antigravity_config("agy", auth_mode=mode, api_key=api_key),
            base_env=inherited,
            caller_env={"GEMINI_API_KEY": "caller-secret", "GOOGLE_GEMINI_BASE_URL": "https://caller.example.test/v1"},
        )
        assert env[AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert "GEMINI_API_KEY" not in env and "GOOGLE_GEMINI_BASE_URL" not in env

    configured = build_antigravity_env(
        antigravity_config("agy", auth_mode="gemini-api-key", api_key="cfg-key", base_url=""),
        base_env=inherited,
    )
    assert configured["GEMINI_API_KEY"] == "cfg-key"
    assert "GOOGLE_GEMINI_BASE_URL" not in configured

    monkeypatch.setenv("GEMINI_API_KEY", "controller-secret-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://inherited.example.test/v1")
    async with make_agent_harness(tmp_path, auth_mode="gemini-api-key", api_key="", name="empty-key") as h:
        req = make_request(message="hello", workdir=h.workdir, model="model-alpha")
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await h.agent.handle_message(req)
        assert h.launches() == []
        assert prewrite_failure_evidence(req.context).get("reason") == "auth_required"
        assert h.controller.events("result")[0][2].get("is_error") is True
        note = h.controller.events("notify")[0][1]
        assert "-32000" in note and "Gemini API key is not configured." in note

        provider = AntigravityNativeSessionProvider(cli_path=str(h.shim), config=h.agy_cfg)
        assert await asyncio.to_thread(provider.list_metadata, str(h.workdir)) == []
        assert h.launches() == []


@pytest.mark.asyncio
async def test_build_launch_runs_env_and_rules_prep_off_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with make_agent_harness(tmp_path, cli_path="agy") as h:
        loop_tid, observed = threading.get_ident(), {}
        for attr in ("build_antigravity_env", "managed_skill_environment", "render_session_rules"):
            orig = getattr(agy_agent_module, attr)
            monkeypatch.setattr(agy_agent_module, attr, lambda *a, _a=attr, _o=orig, **kw: (observed.__setitem__(_a, threading.get_ident()), _o(*a, **kw))[1])
        req = make_request(message="hello", workdir=h.workdir)
        h.agent.ensure_agent_session_id(req)
        await h.agent._build_launch(req, runtime_key=req.composite_session_id, resolved_model="model-alpha")
        assert len(observed) == 3 and all(tid != loop_tid for tid in observed.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("desktop_runtime", [False, True])
async def test_resolve_antigravity_cli_path_normalizes_explicit_paths_and_launches_from_other_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop_runtime: bool
) -> None:
    service_dir, workspace_dir, empty_path = tmp_path / "service-cwd", tmp_path / "other-workspace", tmp_path / "empty-path"
    for d in (service_dir, workspace_dir, empty_path):
        d.mkdir()
    monkeypatch.chdir(service_dir)
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1") if desktop_runtime else monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)

    tilde_shim = create_fake_agy_shim(Path.home() / ".local" / "bin", state_file=tmp_path / f"t-{desktop_runtime}.json", launch_log=tmp_path / f"tl-{desktop_runtime}.jsonl", name="agy")
    rel_shim = create_fake_agy_shim(service_dir / "bin", state_file=tmp_path / f"r-{desktop_runtime}.json", launch_log=tmp_path / f"rl-{desktop_runtime}.jsonl", name="agy")

    assert resolve_antigravity_cli_path("~/.local/bin/agy") == str(tilde_shim.absolute())
    assert resolve_antigravity_cli_path("./bin/agy") == str(rel_shim.absolute())
    assert resolve_antigravity_cli_path("~/.local/bin/missing-agy") == str((Path.home() / ".local" / "bin" / "missing-agy").absolute())
    assert resolve_antigravity_cli_path("./bin/missing-agy") == str((service_dir / "bin" / "missing-agy").absolute())
    assert resolve_antigravity_cli_path("missing-bare-agy") == "missing-bare-agy"

    for selector, tag in (("~/.local/bin/agy", "tilde"), ("./bin/agy", "rel")):
        async with make_agent_harness(tmp_path, workdir=workspace_dir, cli_path=selector, name=f"exp-{tag}-{desktop_runtime}") as h:
            await h.agent.handle_message(make_request(message="stream_all", workdir=workspace_dir, base_session_id=f"exp_{tag}"))
            res = h.controller.events("result")
            assert len(res) == 1 and res[0][1] == "Updated hello.py." and not res[0][2].get("is_error")


@pytest.mark.asyncio
@pytest.mark.parametrize(("auth_mode", "required_method"), [("api_key", "gemini-api-key"), ("gemini-api-key", "gemini-api-key"), ("agent_platform", "agent-platform"), ("agent-platform", "agent-platform")])
async def test_post_initialize_authenticate_for_adapter_and_native_sessions(tmp_path: Path, auth_mode: str, required_method: str) -> None:
    workdir, shared = tmp_path / "repo", FakeSessions()
    workdir.mkdir()
    cfg_kw = {"auth_mode": auth_mode, "api_key": "secret-test-key"} if required_method == "gemini-api-key" else {"auth_mode": auth_mode, "gcp_project": "test-proj", "gcp_location": "us-central1"}

    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared, server_config={"requireAuthMethod": required_method}, name=f"auth-{auth_mode}", **cfg_kw) as h1:
        await h1.agent.handle_message(make_request(message="stream_all", workdir=workdir))
        assert h1.controller.events("result")[0][1] == "Updated hello.py." and not h1.controller.events("result")[0][2].get("is_error")
        shim, agy_cfg = h1.shim, h1.agy_cfg

    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared, cli_path=shim, name=f"auth2-{auth_mode}", **cfg_kw) as h2:
        await h2.agent.handle_message(make_request(message="recall", workdir=workdir))
        assert "stream_all" in h2.controller.events("result")[0][1]

    listed = await asyncio.to_thread(AntigravityNativeSessionProvider(cli_path=str(shim), config=agy_cfg).list_metadata, str(workdir))
    assert len(listed) == 1 and listed[0].native_session_id == "sess-1" and listed[0].locator["title"] == "Update greeting"

    async with make_agent_harness(tmp_path, workdir=workdir, cli_path=shim, auth_mode="oauth", name=f"unauth-{auth_mode}") as h_bad:
        await h_bad.agent.handle_message(make_request(message="hello", workdir=workdir, base_session_id="unauth"))
        assert "-32000" in h_bad.controller.events("notify")[0][1]
    assert await asyncio.to_thread(AntigravityNativeSessionProvider(cli_path=str(shim), config=antigravity_config(shim, auth_mode="oauth")).list_metadata, str(workdir)) == []


@pytest.mark.asyncio
async def test_model_and_dependent_effort_applied_in_order_and_fails_explicitly(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, record_rpc=True) as h:
        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir, model=None))
        assert h.launches() == [] and "No Antigravity model selected" in h.controller.events("notify")[0][1] and h.controller.events("result")[0][2].get("is_error") is True

        h.controller.emitted.clear()
        ok_req = make_request(message="hello", workdir=h.workdir, model="model-beta", effort="high")
        await h.agent.handle_message(ok_req)
        assert [(m["params"]["configId"], m["params"]["value"]) for m in h.rpcs("session/set_config_option")] == [("model", "model-beta"), ("thought_level", "high")]
        assert "model=model-beta" in h.controller.events("result")[0][1]
        sess = h.agent._sessions[ok_req.composite_session_id]
        assert (sess.current_model, sess.current_effort) == ("model-beta", "high") and next(o for o in sess.config_options if o["id"] == "thought_level")["currentValue"] == "high"

        prompts_before = len(h.rpcs("session/prompt"))
        for model, effort, expected in (("nonexistent-model", None, "nonexistent-model"),):
            h.controller.emitted.clear()
            await h.agent.handle_message(make_request(message="hello", workdir=h.workdir, model=model, effort=effort))
            assert expected in h.controller.events("notify")[0][1] and h.controller.events("result")[0][2].get("is_error") is True
        assert len(h.rpcs("session/prompt")) == prompts_before
        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir, model="model-beta", effort="ultra-invalid"))
        assert not h.controller.events("result")[0][2].get("is_error")
        assert "uses thinking level" in h.controller.events("notify")[0][1]


@pytest.mark.asyncio
async def test_contradictory_current_value_raises_before_prompt(tmp_path: Path) -> None:
    class _MismatchClient:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        async def session_set_config_option(self, _sid: str, cid: str, val: Any) -> dict[str, Any]:
            if self.mode == "model":
                return {"configOptions": make_model_config_options(model="model-alpha", effort="low")}
            return {"configOptions": make_model_config_options(model=str(val) if cid == "model" else "model-beta", effort="low", snake_case=True)}

    base = make_model_config_options(effort="low")
    for mode, effort, reason in (("model", None, "model_mismatch"), ("effort", "high", "effort_mismatch")):
        with pytest.raises(AntigravityModelSelectionError) as exc:
            await apply_model_and_effort(_MismatchClient(mode), "sess-1", base, model="model-beta", effort=effort)  # type: ignore[arg-type]
        assert exc.value.reason == reason

    async with make_agent_harness(tmp_path) as h:
        prompt_called, orig_get = False, h.agent._get_or_create_session

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            sess = await orig_get(*a, **kw)
            sess.client.session_set_config_option = _MismatchClient("model").session_set_config_option  # type: ignore[method-assign]

            async def _guard(*_pa: Any, **_pkw: Any) -> dict[str, Any]:
                nonlocal prompt_called
                prompt_called = True
                return {"stopReason": "end_turn"}

            sess.client.session_prompt = _guard  # type: ignore[method-assign]
            return sess

        h.agent._get_or_create_session = _wrapped  # type: ignore[method-assign]
        await h.agent.handle_message(make_request(message="never-sent", workdir=h.workdir, model="model-beta"))
        assert prompt_called is False and h.controller.events("result")[0][2].get("is_error") is True


@pytest.mark.asyncio
async def test_clearing_effort_override_resets_native_thought_level_on_reused_runtime(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, record_rpc=True, name="effort-reset") as h:
        req1 = make_request(message="turn-1", workdir=h.workdir, model="model-alpha", effort="high")
        await h.agent.handle_message(req1)
        sess = h.agent._sessions[req1.composite_session_id]
        assert (sess.current_model, sess.current_effort, sess.default_efforts.get("model-alpha")) == ("model-alpha", "high", "medium")

        before = len(h.rpcs("session/set_config_option"))
        await h.agent.handle_message(make_request(message="turn-2", workdir=h.workdir, model="model-alpha", effort=None))
        assert len(h.launches()) == 1 and [(m["params"]["configId"], m["params"]["value"]) for m in h.rpcs("session/set_config_option")[before:]] == [("thought_level", "medium")]
        assert sess.current_effort is None and next(o for o in sess.config_options if o["id"] == "thought_level")["currentValue"] == "medium"

        after = len(h.rpcs("session/set_config_option"))
        await h.agent.handle_message(make_request(message="turn-3", workdir=h.workdir, model="model-alpha", effort=None))
        assert len(h.launches()) == 1 and len(h.rpcs("session/set_config_option")) == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_model", "default_effort", "behavior", "expected_calls", "expected_error", "expected_default"),
    [
        ("model-alpha", "medium", "apply", [("thought_level", "medium")], None, "medium"),
        ("model-alpha", "medium", "reject", [("thought_level", "medium")], "effort_rejected", None),
        ("model-alpha", "medium", "stay_high", [("thought_level", "medium")], "effort_mismatch", None),
        ("model-alpha", None, "reset_medium", [("model", "model-alpha")], None, "medium"),
        ("model-alpha", None, "advertise_default", [("model", "model-alpha"), ("thought_level", "medium")], None, "medium"),
        ("model-alpha", None, "stay_high", [("model", "model-alpha")], "effort_reset_unavailable", None),
        ("model-beta", None, "stay_high", [("model", "model-beta")], "effort_reset_unavailable", None),
        ("model-beta", None, "reset_medium", [("model", "model-beta")], None, "medium"),
    ],
)
async def test_apply_model_and_effort_clear_override_unit_contracts(
    target_model: str, default_effort: str | None, behavior: str, expected_calls: list[tuple[str, Any]], expected_error: str | None, expected_default: str | None
) -> None:
    calls: list[tuple[str, Any]] = []

    class _StubClient:
        async def session_set_config_option(self, _sid: str, cid: str, val: Any) -> dict[str, Any]:
            calls.append((cid, val))
            if behavior == "reject":
                raise AcpRpcError(-32602, "effort reset rejected")
            if behavior == "apply":
                return {"configOptions": make_model_config_options(effort=str(val))}
            if behavior == "reset_medium":
                return {"configOptions": make_model_config_options(model=str(val), effort="medium")}
            if behavior == "advertise_default":
                return {"configOptions": make_model_config_options(effort="high" if cid == "model" else str(val), default_effort="medium")}
            return {"configOptions": make_model_config_options(model=str(val) if cid == "model" else "model-alpha", effort="high")}

    opts = make_model_config_options(model="model-alpha", effort="high")
    kw: dict[str, Any] = dict(model=target_model, effort=None, current_model="model-alpha", current_effort="high", **({"default_effort": default_effort} if default_effort else {}))
    if expected_error is not None:
        with pytest.raises(AntigravityModelSelectionError) as exc:
            await apply_model_and_effort(_StubClient(), "sess-1", opts, **kw)  # type: ignore[arg-type]
        assert exc.value.reason == expected_error and calls == expected_calls
    else:
        applied = await apply_model_and_effort(_StubClient(), "sess-1", opts, **kw)  # type: ignore[arg-type]
        assert calls == expected_calls and (applied.model, applied.effort, applied.default_effort) == (target_model, None, expected_default)


@pytest.mark.asyncio
@pytest.mark.parametrize("return_empty", [False, True])
async def test_partial_selection_failure_without_notification_reapplies_model_and_effort(tmp_path: Path, return_empty: bool) -> None:
    async with make_agent_harness(tmp_path, name=f"partial-sel-{return_empty}") as h:
        peer, set_calls, prompt_states, mismatch_next, wrapped = {"model": "model-alpha", "thought_level": "medium"}, [], [], False, set()
        orig_get = h.agent._get_or_create_session

        def _opts() -> list[dict[str, Any]]:
            return make_model_config_options(model=peer["model"], effort=peer["thought_level"], effort_choices=("low", "medium") if peer["model"] == "model-beta" else ("low", "medium", "high"))

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            sess = await orig_get(*a, **kw)
            if id(sess) not in wrapped:
                wrapped.add(id(sess))

                async def _set_opt(_sid: str, cid: str, val: Any) -> dict[str, Any]:
                    nonlocal mismatch_next
                    set_calls.append((cid, val))
                    if cid == "model":
                        peer["model"] = str(val)
                    elif cid == "thought_level":
                        if peer["model"] == "model-beta":
                            raise AcpRpcError(-32602, "native effort change rejected")
                        if mismatch_next:
                            mismatch_next = False
                            peer["thought_level"] = "low"
                            return {"configOptions": _opts()}
                        peer["thought_level"] = str(val)
                    return {} if return_empty else {"configOptions": _opts()}

                async def _prompt(*_pa: Any, **_pkw: Any) -> dict[str, Any]:
                    prompt_states.append((peer["model"], peer["thought_level"]))
                    return {"stopReason": "end_turn"}

                sess.client.session_set_config_option, sess.client.session_prompt = _set_opt, _prompt  # type: ignore[method-assign]
            return sess

        h.agent._get_or_create_session = _wrapped  # type: ignore[method-assign]

        async def _turn(msg: str, model: str, effort: str | None) -> Any:
            h.controller.emitted.clear()
            set_calls.clear()
            req = make_request(message=msg, workdir=h.workdir, model=model, effort=effort)
            await h.agent.handle_message(req)
            return req

        req1 = await _turn("t1", "model-alpha", None)
        sess = h.agent._sessions[req1.composite_session_id]
        assert prompt_states[-1] == ("model-alpha", "medium") and sess.current_model == "model-alpha"

        before = len(prompt_states)
        await _turn("t2", "model-beta", "ultra-invalid")
        assert set_calls == [("model", "model-beta"), ("thought_level", "medium")] and peer["model"] == "model-beta" and len(prompt_states) == before
        assert h.controller.events("result")[0][2].get("is_error") is True and sess.current_model is None and sess.selection_dirty is True

        await _turn("t3", "model-alpha", None)
        assert ("model", "model-alpha") in set_calls and prompt_states[-1] == ("model-alpha", "medium") and sess.selection_dirty is False

        await _turn("t4", "model-alpha", "high")
        before = len(prompt_states)
        await _turn("t5", "model-beta", "ultra-invalid" if return_empty else "high")
        assert set_calls == [("model", "model-beta"), ("thought_level", "medium")] and len(prompt_states) == before and sess.current_model is None and sess.current_effort == "high"

        await _turn("t6", "model-alpha", None)
        assert set_calls == [("model", "model-alpha"), ("thought_level", "medium")] and prompt_states[-1] == ("model-alpha", "medium")
        await _turn("t7", "model-alpha", None)
        assert set_calls == []

        mismatch_next = True
        before = len(prompt_states)
        await _turn("t8", "model-alpha", "high")
        assert set_calls == [("thought_level", "high")] and peer == {"model": "model-alpha", "thought_level": "low"} and len(prompt_states) == before and sess.selection_dirty is True

        await _turn("t9", "model-alpha", None)
        assert set_calls == [("model", "model-alpha"), ("thought_level", "medium")] and prompt_states[-1] == ("model-alpha", "medium")


@pytest.mark.asyncio
async def test_effort_reset_preserved_across_eviction_and_sqlite_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_sqlite_state()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file, rpc_log = tmp_path / "restart-state.json", tmp_path / "restart-rpc.jsonl"
    shim = create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=tmp_path / "restart-launches.jsonl", rpc_log=rpc_log, name="fake-agy-restart")

    loop_tid, db_tids = threading.get_ident(), []
    for attr in ("_load_persisted_selection_state", "_save_persisted_selection_state"):
        orig = getattr(agy_agent_module, attr)
        monkeypatch.setattr(agy_agent_module, attr, lambda *a, _o=orig, **kw: (db_tids.append(threading.get_ident()), _o(*a, **kw))[1])

    async with make_agent_harness(tmp_path, workdir=workdir, cli_path=shim, sessions=SessionsFacade(SessionsStore()), name="r1") as h1:
        req1 = make_request(message="t1", workdir=workdir, model="model-alpha", effort="high")
        await h1.agent.handle_message(req1)
        agent_session_id = str(req1.context.platform_specific["agent_session_id"])
        assert _peer_opt(state_file, "sess-1", "thought_level") == "high"

        await h1.agent._close_session(h1.agent._sessions[req1.composite_session_id], intentional=True, retire_activation=True)
        rpc_log.unlink(missing_ok=True)
        await h1.agent.handle_message(make_request(message="t2", workdir=workdir, model="model-alpha", effort=None))
        rpcs = read_jsonl(rpc_log)
        assert any(r.get("method") == "session/resume" for r in rpcs) and ("thought_level", "medium") in [(r["params"]["configId"], r["params"]["value"]) for r in rpcs if r.get("method") == "session/set_config_option"]
        assert _peer_opt(state_file, "sess-1", "thought_level") == "medium"

        await h1.agent.handle_message(make_request(message="t3", workdir=workdir, model="model-alpha", effort="high"))
        assert _peer_opt(state_file, "sess-1", "thought_level") == "high"

    assert read_session_row(agent_session_id)["metadata"]["antigravity_selection"] == {"native_session_id": "sess-1", "default_efforts": {"model-alpha": "medium"}, "current_effort": "high"}

    rpc_log.unlink(missing_ok=True)
    async with make_agent_harness(tmp_path, workdir=workdir, cli_path=shim, sessions=SessionsFacade(SessionsStore()), name="r2") as h2:
        await h2.agent.handle_message(make_request(message="t4", workdir=workdir, model="model-alpha", effort=None))
        assert not h2.controller.events("result")[0][2].get("is_error") and _peer_opt(state_file, "sess-1", "thought_level") == "medium"
        rpcs2 = read_jsonl(rpc_log)
        assert any(r.get("method") == "session/resume" for r in rpcs2) and ("thought_level", "medium") in [(r["params"]["configId"], r["params"]["value"]) for r in rpcs2 if r.get("method") == "session/set_config_option"]

    assert db_tids and all(tid != loop_tid for tid in db_tids)
    assert read_session_row(agent_session_id)["metadata"]["antigravity_selection"] == {"native_session_id": "sess-1", "default_efforts": {"model-alpha": "medium"}, "current_effort": None}


@pytest.mark.asyncio
async def test_unrecoverable_effort_override_after_eviction_and_restart_fails_explicitly(tmp_path: Path) -> None:
    ensure_sqlite_state()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_file, rpc_log = tmp_path / "unrec-state.json", tmp_path / "unrec-rpc.jsonl"
    shim = create_fake_agy_shim(tmp_path, state_file=state_file, launch_log=tmp_path / "unrec-launches.jsonl", rpc_log=rpc_log, name="fake-agy-unrec")

    async def _assert_fails_reset(agent: AntigravityAgent, ctrl: FakeController, msg: str) -> None:
        rpc_log.unlink(missing_ok=True)
        ctrl.emitted.clear()
        req = make_request(message=msg, workdir=workdir, model="model-beta", effort=None)
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await agent.handle_message(req)
        assert ctrl.events("result")[0][2].get("is_error") is True and prewrite_failure_evidence(req.context).get("reason") == "effort_reset_unavailable"
        rpcs = read_jsonl(rpc_log)
        assert any(r.get("method") == "session/resume" for r in rpcs) and not any(r.get("method") == "session/prompt" for r in rpcs)

    async with make_agent_harness(tmp_path, workdir=workdir, cli_path=shim, sessions=SessionsFacade(SessionsStore()), name="u1") as h1:
        await h1.agent.handle_message(make_request(message="t1", workdir=workdir, model="model-alpha", effort="high"))
        req2 = make_request(message="t2", workdir=workdir, model="model-beta", effort="high")
        await h1.agent.handle_message(req2)
        assert _peer_opt(state_file, "sess-1", "model") == "model-beta" and _peer_opt(state_file, "sess-1", "thought_level") == "high"
        await h1.agent._close_session(h1.agent._sessions[req2.composite_session_id], intentional=True, retire_activation=True)
        await _assert_fails_reset(h1.agent, h1.controller, "t3")

    async with make_agent_harness(tmp_path, workdir=workdir, cli_path=shim, sessions=SessionsFacade(SessionsStore()), name="u2") as h2:
        await _assert_fails_reset(h2.agent, h2.controller, "t4")


@pytest.mark.asyncio
async def test_streaming_updates_usage_permissions_and_single_turn_end(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        req = make_request(message="stream_all", workdir=h.workdir)
        await h.agent.handle_message(req)
        narr, tools, res = h.controller.events("assistant"), h.controller.events("toolcall"), h.controller.events("result")
        assert len(narr) == 1 and narr[0][1] == "planning edit" and narr[0][2].get("level") == "process"
        assert len(tools) == 1 and any("diff: hello.py" in tc[1] and "print('new')" in tc[1] for tc in tools)
        assert len(res) == 1 and res[0][1] == "Updated hello.py." and h.controller.token_notes == [(req.context, 1200)]

        for prompt, expected in (("request_permission", "permission:allow_once"), ("request_unknown", "unknown_req_error:-32601")):
            h.controller.emitted.clear()
            await h.agent.handle_message(make_request(message=prompt, workdir=h.workdir))
            assert h.controller.events("result")[0][1] == expected


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
async def test_strict_stop_reason_settlement_preserves_partial_text_and_fails(tmp_path: Path, prompt: str, expected_partial: str, expected_subtype: str, monkeypatch) -> None:
    calls = []
    original = agy_agent_module.emit_backend_failure
    async def failure(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)
    monkeypatch.setattr(agy_agent_module, "emit_backend_failure", failure)
    async with make_agent_harness(tmp_path, show_duration=True) as h:
        req = make_request(message=prompt, workdir=h.workdir)
        req.started_at -= 1.0
        await h.agent.handle_message(req)
        _, text, opts = h.controller.events("result")[0]
        assert len(calls) == 1  # The shared failure owner supplies the standard Retry affordance.
        assert expected_partial in h.controller.events("assistant")[0][1]
        assert opts.get("is_error") is True and opts.get("terminal_error")


@pytest.mark.asyncio
async def test_localized_zh_failure_and_incomplete_turn_copy(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, cli_path=tmp_path / "missing-agy", language="zh", name="zh-miss") as h:
        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir))
        assert "找不到 Antigravity CLI" in h.controller.events("notify")[0][1]

    async with make_agent_harness(tmp_path, server_config={"requireAuth": True}, language="zh", name="zh-auth") as h:
        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir))
        note = h.controller.events("notify")[0][1]
        assert "需要身份验证" in note and "-32000" in note

    async with make_agent_harness(tmp_path, language="zh", name="zh-stop") as h:
        await h.agent.handle_message(make_request(message="stop_reason:max_tokens", workdir=h.workdir))
        _, text, opts = h.controller.events("result")[0]
        assert "partial:max_tokens" in h.controller.events("assistant")[0][1]
        assert "回合未完整结束" in h.controller.events("notify")[0][1] and opts.get("is_error") is True
        assert opts.get("terminal_error") == "Antigravity turn stopped incomplete (max_tokens)."


@pytest.mark.asyncio
async def test_handle_stop_and_mid_turn_crash_emit_single_terminal_and_recover(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        stop_req = make_request(message="wait_cancel", workdir=h.workdir)
        turn_task = asyncio.create_task(h.agent.handle_message(stop_req))
        for _ in range(100):
            if h.agent.runtime_has_active_turns():
                break
            await asyncio.sleep(0.02)
        assert h.agent.runtime_has_active_turns() and await h.agent.handle_stop(stop_req) is True
        await turn_task
        assert len(h.controller.events("result")) == 1 and h.controller.events("result")[0][2]["output"].settled_by == SETTLED_BY_STOPPED

        h.controller.emitted.clear()
        crash_req = make_request(message="crash_mid_prompt", workdir=h.workdir)
        await h.agent.handle_message(crash_req)
        assert h.controller.events("assistant")[0][1] == "starting before crash"
        assert h.controller.emitted[0][0] == "assistant"
        assert "simulated peer crash" in h.controller.events("notify")[0][1] or "exited with code 2" in h.controller.events("notify")[0][1]
        assert h.controller.events("result")[0][2].get("is_error") is True and crash_req.composite_session_id not in h.agent._sessions

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recovered-ok", workdir=h.workdir))
        assert "echo:recovered-ok" in h.controller.events("result")[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "server_config", "missing_cli", "platform_specific", "expected_reason", "expected_sub", "forbidden_sub"),
    [
        ("auth_new", {"requireAuth": True}, False, None, "auth_required", "-32000", None),
        ("auth_resume", {"requireAuth": True}, False, {"agent_session_target": {"id": "ses_existing", "agent_backend": "antigravity", "native_session_id": "sess-1"}}, "auth_required", "-32000", "Could not resume"),
        ("cli_not_found", None, True, None, "cli_not_found", "CLI not found", None),
    ],
)
async def test_prewrite_failures_classify_and_emit_single_error(
    tmp_path: Path, case_id: str, server_config: dict[str, Any] | None, missing_cli: bool, platform_specific: dict[str, Any] | None, expected_reason: str, expected_sub: str, forbidden_sub: str | None
) -> None:
    async with make_agent_harness(tmp_path, server_config=server_config, cli_path=(tmp_path / "does-not-exist-agy") if missing_cli else None, name=f"fake-agy-{case_id}") as h:
        req = make_request(message="hello", workdir=h.workdir, base_session_id=f"slack_{case_id}", platform_specific=platform_specific)
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await h.agent.handle_message(req)
        assert prewrite_failure_evidence(req.context).get("reason") == expected_reason
        note = h.controller.events("notify")[0][1]
        assert expected_sub in note and h.controller.events("result")[0][2].get("is_error") is True
        if case_id == "auth_new":
            assert "authentication required" in note.lower()
        if forbidden_sub is not None:
            assert forbidden_sub not in note


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "zh"])
async def test_missing_additional_directories_capability_refuses_turn_explicitly(tmp_path: Path, language: str) -> None:
    async with make_agent_harness(tmp_path, record_rpc=True, language=language, server_config={"agentCapabilities": make_acp_capabilities(additional_directories=False)}, name=f"no-add-{language}") as h:
        req = make_request(message="do not run without rules", workdir=h.workdir, model="model-alpha")
        req.vibe_agent_system_prompt = "STRICT AGENT INSTRUCTION: never run without this."
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await h.agent.handle_message(req)
        methods = [r.get("method") for r in h.rpcs()]
        assert "initialize" in methods and not any(m in methods for m in ("session/new", "session/resume", "session/fork", "session/prompt"))
        assert h.agent._sessions == {} and "sessionCapabilities.additionalDirectories" in h.controller.events("notify")[0][1]
        assert prewrite_failure_evidence(req.context).get("reason") == "capability_missing"


@pytest.mark.asyncio
async def test_cross_process_resume_and_explicit_missing_resume_failure(tmp_path: Path) -> None:
    shared, workdir = FakeSessions(), tmp_path / "repo"
    workdir.mkdir()
    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared, name="resume") as h1:
        req1 = make_request(message="first-turn-marker", workdir=workdir)
        await h1.agent.handle_message(req1)
        assert shared.get_agent_session_mapping(req1.session_key, "antigravity", req1.base_session_id) == "sess-1"

    async with make_agent_harness(tmp_path, workdir=workdir, sessions=shared, name="resume") as h2:
        await h2.agent.handle_message(make_request(message="recall", workdir=workdir))
        res_text = h2.controller.events("result")[0][1]
        assert "first-turn-marker" in res_text and str(h2.rules_dir) in res_text

        h2.controller.emitted.clear()
        await h2.agent.handle_message(
            make_request(message="hello", workdir=workdir, base_session_id="slack_999.9", platform_specific={"agent_session_target": {"id": "ses_missing", "agent_backend": "antigravity", "native_session_id": "sess-does-not-exist"}})
        )
        assert "Could not resume Antigravity session" in h2.controller.events("notify")[0][1] and h2.controller.events("result")[0][2].get("is_error") is True
        assert list(json.loads(h2.state_file.read_text(encoding="utf-8"))["sessions"].keys()) == ["sess-1"]


@pytest.mark.asyncio
async def test_caller_context_session_stable_reuse_replacement_and_sibling_isolation(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path) as h:
        req_b = make_request(message="sibling-marker", workdir=h.workdir, base_session_id="slack_sibling_B", session_key="slack::channel::CB")
        await h.agent.handle_message(req_b)
        sib_pid, sib_native = h.agent._sessions[req_b.composite_session_id].client.pid, h.agent._sessions[req_b.composite_session_id].native_session_id

        req1 = make_request(message="turn-1", workdir=h.workdir, user_id="U1", message_id="M1")
        await h.agent.handle_message(req1)
        sess_a = h.agent._sessions[req1.composite_session_id]
        pid1, native_a = sess_a.client.pid, sess_a.native_session_id
        assert pid1 != sib_pid and native_a != sib_native and AVIBE_RUN_ID_ENV not in h.launches()[-1]["env"] and len(h.launches()) == 2

        await h.agent.handle_message(make_request(message="turn-2", workdir=h.workdir, user_id="U2", message_id="M2"))
        assert h.agent._sessions[req1.composite_session_id].client.pid == pid1 and len(h.launches()) == 2

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recall", workdir=h.workdir, user_id="U2", message_id="M3", platform_specific={"task_execution_id": "run_42", "task_trigger_kind": "agent_run", "source_kind": "agent"}))
        pid3 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid3 != pid1 and h.agent._sessions[req1.composite_session_id].native_session_id == native_a
        assert (h.launches()[-1]["env"][AVIBE_RUN_ID_ENV], h.launches()[-1]["env"][AVIBE_CALLER_SOURCE_ENV]) == ("run_42", "agent_run")
        assert "turn-1" in h.controller.events("result")[0][1] and "turn-2" in h.controller.events("result")[0][1]

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recall", workdir=h.workdir, platform="avibe", user_id="remote:sub-alice", message_id="M4", platform_specific={"message_metadata": {"resource_user_context": {"sub": "sub-alice", "role": "editor"}}}))
        pid4 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid4 != pid3 and json.loads(h.launches()[-1]["env"][AVIBE_CALLER_RESOURCE_CONTEXT_ENV]) == {"role": "editor", "sub": "sub-alice"}

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recall", workdir=h.workdir, user_id="U1", message_id="M5"))
        pid5 = h.agent._sessions[req1.composite_session_id].client.pid
        assert pid5 != pid4 and AVIBE_RUN_ID_ENV not in h.launches()[-1]["env"] and h.launches()[-1]["env"].get(AVIBE_CALLER_SOURCE_ENV) != "agent_run"

        assert h.agent._sessions[req_b.composite_session_id].client.pid == sib_pid and h.agent._sessions[req_b.composite_session_id].client.is_alive
        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="recall", workdir=h.workdir, base_session_id="slack_sibling_B", session_key="slack::channel::CB"))
        sib_reply = h.controller.events("result")[0][1]
        assert "sibling-marker" in sib_reply and "turn-1" not in sib_reply


def test_rules_rendering_splits_over_24k_and_skips_unchanged_rewrite(tmp_path: Path) -> None:
    rules_base, project_dir = tmp_path / "avibe-rules", tmp_path / "user-project"
    project_dir.mkdir()
    large = "\n\n".join(f"## Section {i}\n{('Rule line: follow Avibe conventions strictly. ' * 20).strip()}" for i in range(60))
    assert len(large.encode("utf-8")) > MAX_RULE_FILE_BYTES * 2

    first = render_session_rules("sess-key:repo", large, base_dir=rules_base)
    assert first.written is True and len(first.directories) >= 3
    assert all((Path(d) / "AGENTS.md").is_file() and len((Path(d) / "AGENTS.md").read_bytes()) <= MAX_RULE_FILE_BYTES and Path(d).is_relative_to(rules_base) for d in first.directories)

    rendered = (Path(first.directories[0]) / "AGENTS.md").read_text(encoding="utf-8")
    assert "Do not create Antigravity native automations, sidecars" in rendered
    assert all(command in rendered for command in ("vibe task add", "vibe watch add", "vibe agent run"))

    mtimes = {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in first.directories}
    second = render_session_rules("sess-key:repo", large, base_dir=rules_base)
    assert second.written is False and second.digest == first.digest and second.directories == first.directories and {d: (Path(d) / "AGENTS.md").stat().st_mtime_ns for d in second.directories} == mtimes

    capped = render_session_rules("sess-key:capped", "\n".join(f"Line {i}: " + ("x" * 100) for i in range(1200)), base_dir=rules_base)
    assert sum((Path(d) / "AGENTS.md").stat().st_size for d in capped.directories) <= MAX_RULE_TOTAL_BYTES
    assert all((Path(d) / "AGENTS.md").stat().st_size <= MAX_RULE_FILE_BYTES for d in capped.directories) and list(project_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_pre_tool_use_hook_enforces_policy_and_unattributable_permission_rejected(tmp_path: Path) -> None:
    rules_base, workspace = tmp_path / "avibe-rules", tmp_path / "workspace"
    workspace.mkdir()
    first = render_session_rules("sess-hook:repo", "Follow Avibe rules.", base_dir=rules_base)
    primary = Path(first.directories[0])
    hooks_json, hook_script = primary / ".agents" / "hooks.json", primary / ".agents" / "avibe_tool_policy_hook.py"
    assert first.written is True and hooks_json.is_file() and hook_script.is_file()

    entry = json.loads(hooks_json.read_text(encoding="utf-8"))["avibe-tool-policy"]["PreToolUse"][0]
    hook_cmd = entry["hooks"][0]["command"]
    assert str(hook_script.resolve()) in hook_cmd and set(ALWAYS_SESSION_ONLY_TOOL_NAMES) <= set(entry["matcher"].split("|"))

    def _run_hook(raw_input: str) -> dict[str, Any]:
        return json.loads(subprocess.run(hook_cmd, shell=True, cwd=str(workspace), input=raw_input, text=True, capture_output=True, check=True).stdout)

    assert {"run_workflow", "swarm"} <= set(entry["matcher"].split("|"))
    for tool in ("schedule", "invoke_subagent", "define_subagent", "Workflow", "run_workflow", "swarm"):
        out = _run_hook(json.dumps({"hookEventName": "PreToolUse", "toolCall": {"id": "t1", "name": tool, "args": {}}}))
        assert out["decision"] == "deny" and out["reason"] == check_tool_call(tool, {}).reason
    for tool in ("read_file", "run_command", "grep_search", "manage_subagents", "send_message"):
        assert _run_hook(json.dumps({"hookEventName": "PreToolUse", "toolCall": {"id": "t2", "name": tool, "args": {}}})) == {"decision": "allow"}
    assert _run_hook("{not-valid-json") == {"decision": "allow"}

    mtimes = (hooks_json.stat().st_mtime_ns, hook_script.stat().st_mtime_ns)
    assert render_session_rules("sess-hook:repo", "Follow Avibe rules.", base_dir=rules_base).written is False and (hooks_json.stat().st_mtime_ns, hook_script.stat().st_mtime_ns) == mtimes
    assert list(workspace.iterdir()) == [] and not (Path.home() / ".gemini").exists()
    empty_rules = render_session_rules("sess-empty:repo", "", base_dir=rules_base)
    assert empty_rules.written is True and (Path(empty_rules.directories[0]) / ".agents" / "hooks.json").is_file()

    agy_cfg = antigravity_config("agy")
    agent = AntigravityAgent(FakeController(app_config(agy_cfg)), agy_cfg, rules_base_dir=rules_base)
    active_session = SimpleNamespace(
        native_session_id="sess-1",
        retired=False,
        active_turn=SimpleNamespace(native_session_id="sess-1", settled=False, cancelled=False),
    )
    opts = [{"optionId": "allow_once", "kind": "allow_once"}, {"optionId": "reject_once", "kind": "reject_once"}]
    active_session.spec = SimpleNamespace(command=("agy", "acp", "--dangerously-skip-permissions"))
    params = {"sessionId": "sess-1", "toolCall": {"toolCallId": "tc", "title": "Edit config file"}, "options": opts}
    assert await agent._handle_server_request(active_session, "session/request_permission", params) == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
    for bad in ({**params, "toolCall": {}}, {**params, "options": []}, {**params, "sessionId": "wrong"}, {**params, "options": [{"optionId": "m", "kind": "mystery"}]}):
        with pytest.raises(AcpRpcError) as error:
            await agent._handle_server_request(active_session, "session/request_permission", bad)
        assert error.value.code == -32602
    active_session.active_turn = None
    assert await agent._handle_server_request(active_session, "session/request_permission", params) == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.asyncio
async def test_steering_without_a_live_turn_does_not_write(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, cli_path="agy") as h:
        req = make_request(message="hello", workdir=h.workdir)
        target = ActiveSteerTarget(runtime_key=req.composite_session_id, logical_turn_id="turn-1", context=req.context, agent_request=req, agent=h.agent)
        assert h.agent.steering_native_turn_id(target) is None
        steer = await h.agent.steer_active_turn(SteerRequest(target_session_id="ses_1", expected_logical_turn_id="turn-1", expected_native_turn_id="native-1", text="follow up"), target)
        rec = await h.agent.reconcile_steer_attempt(SteerReconcileRequest(target_session_id="ses_1", expected_logical_turn_id="turn-1", expected_native_turn_id="native-1", attempt_id="att-1"), target)
        assert steer.outcome == SteerOutcome.NOT_ACTIVE
        assert (rec.outcome, rec.reason) == (SteerOutcome.UNKNOWN, "attempt_not_found")


@pytest.mark.asyncio
async def test_idle_eviction_respects_runtime_ownership_and_renewal(tmp_path: Path) -> None:
    ownership = FakeOwnershipProvider(blocks_reclamation=True)
    async with make_agent_harness(tmp_path, ownership=ownership) as h:
        req = make_request(message="hello", workdir=h.workdir)
        await h.agent.handle_message(req)
        session = h.agent._sessions[req.composite_session_id]
        session.last_activity = time.monotonic() - 120.0

        assert await h.agent.evict_idle_transports(10.0) == 0 and session.client.is_alive
        ownership.blocks_reclamation = False
        assert await h.agent.evict_idle_transports(10.0) == 1 and not session.client.is_alive and session.activation is not None and not h.controller.runtime_activation.is_current(session.activation)

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
        req_a = make_request(message="hello-a", workdir=h.workdir, base_session_id="slack_thread_A", session_key="slack::channel::C1")
        req_b = make_request(message="hello-b", workdir=h.workdir, base_session_id="slack_thread_B", session_key="slack::channel::C2")
        await asyncio.gather(h.agent.handle_message(req_a), h.agent.handle_message(req_b))

        sess_a, sess_b = h.agent._sessions[req_a.composite_session_id], h.agent._sessions[req_b.composite_session_id]
        assert sess_a.client.pid != sess_b.client.pid and sess_a.activation != sess_b.activation and sess_a.spec.rules_dirs != sess_b.spec.rules_dirs
        texts = [text for _, text, _ in h.controller.events("result")]
        assert any("echo:hello-a" in t for t in texts) and any("echo:hello-b" in t for t in texts) and h.agent.runtime_activation_identity_for_request(req_a) == sess_a.activation

        override_key = f"slack_override_key:{h.workdir}"
        req_ov = make_request(message="hello-override", workdir=h.workdir, base_session_id="slack_thread_override", session_key="slack::channel::C3", platform_specific={"backend_composite_session_id": override_key})
        await h.agent.handle_message(req_ov)
        sess_ov = h.agent._sessions[override_key]
        assert h.agent.runtime_activation_identity_for_request(req_ov) == sess_ov.activation
        assert _resolve_live_state(h.controller, backend="antigravity", session_id=None, composite_key=override_key, base_session_id="slack_thread_override") == "idle"
        await sess_ov.client.close()
        assert _resolve_live_state(h.controller, backend="antigravity", session_id=None, composite_key=override_key, base_session_id="slack_thread_override") is None
        assert "slack_thread_override" not in {r["base_session_id"] for r in snapshot_running_agents(h.controller)["agents"] if r["backend"] == "antigravity"}
        await h.agent.end_session("slack_thread_override")

        assert h.agent.runtime_activation_identity_for_session_binding(session_anchor="slack_thread_A", workdir=str(h.workdir)) == sess_a.activation
        assert len(h.agent.runtime_ownership_snapshots() or ()) == 2
        agy_rows = [r for r in snapshot_running_agents(h.controller)["agents"] if r["backend"] == "antigravity"]
        assert {r["base_session_id"] for r in agy_rows} == {"slack_thread_A", "slack_thread_B"} and all(r["state"] == "idle" and isinstance(r["pid"], int) for r in agy_rows)

        ended = await end_running_agent(h.controller, backend="antigravity", state="idle", base_session_id="slack_thread_A")
        assert ended["ok"] is True and ended["process_killed"] is True and req_a.composite_session_id not in h.agent._sessions and sess_b.client.is_alive
        assert await h.agent.clear_sessions("slack::channel::C2") == 1 and not sess_b.client.is_alive


@pytest.mark.asyncio
async def test_handle_stop_bounds_blocked_session_cancel_drain_within_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_agent_module, "_STOP_CANCEL_GRACE_SECONDS", 0.2)
    async with make_agent_harness(tmp_path, name="stop-blocked-drain") as h:
        stop_req = make_request(message="wait_cancel", workdir=h.workdir)
        turn_task = asyncio.create_task(h.agent.handle_message(stop_req))
        for _ in range(100):
            if h.agent.runtime_has_active_turns():
                break
            await asyncio.sleep(0.02)
        assert h.agent.runtime_has_active_turns()

        sess = h.agent._sessions[stop_req.composite_session_id]
        assert sess.client._process is not None and sess.client._process.stdin is not None
        block_event = asyncio.Event()

        async def _blocked_drain() -> None:
            await block_event.wait()

        monkeypatch.setattr(sess.client._process.stdin, "drain", _blocked_drain)

        started = time.monotonic()
        stopped = await h.agent.handle_stop(stop_req)
        elapsed = time.monotonic() - started
        await asyncio.wait_for(turn_task, timeout=2.0)

        assert stopped is True
        assert elapsed < 2.0
        assert not sess.client.is_alive
        assert stop_req.composite_session_id not in h.agent._sessions
        assert len(h.controller.events("result")) == 1
        assert h.controller.events("result")[0][2]["output"].settled_by == SETTLED_BY_STOPPED


@pytest.mark.asyncio
async def test_effort_option_receipt_model_mismatch_fails_before_prompt(tmp_path: Path) -> None:
    class _EffortModelDriftClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def session_set_config_option(self, _sid: str, cid: str, val: Any) -> dict[str, Any]:
            self.calls.append((cid, val))
            if cid == "model":
                return {"configOptions": make_model_config_options(model="model-alpha", effort="medium")}
            return {"configOptions": make_model_config_options(model="model-beta", effort="high")}

    base = make_model_config_options(model="model-beta", effort="medium")
    drift_client = _EffortModelDriftClient()
    with pytest.raises(AntigravityModelSelectionError) as exc:
        await apply_model_and_effort(drift_client, "sess-1", base, model="model-alpha", effort="high")  # type: ignore[arg-type]
    assert exc.value.reason == "model_mismatch"
    assert drift_client.calls == [("model", "model-alpha"), ("thought_level", "high")]

    async with make_agent_harness(tmp_path, name="effort-model-drift") as h:
        prompt_called, orig_get = False, h.agent._get_or_create_session

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            sess = await orig_get(*a, **kw)
            sess.config_options = tuple(make_model_config_options(model="model-beta", effort="medium"))
            sess.client.session_set_config_option = _EffortModelDriftClient().session_set_config_option  # type: ignore[method-assign]

            async def _guard(*_pa: Any, **_pkw: Any) -> dict[str, Any]:
                nonlocal prompt_called
                prompt_called = True
                return {"stopReason": "end_turn"}

            sess.client.session_prompt = _guard  # type: ignore[method-assign]
            return sess

        h.agent._get_or_create_session = _wrapped  # type: ignore[method-assign]
        req = make_request(message="never-sent", workdir=h.workdir, model="model-alpha", effort="high")
        set_dispatch_phase(req.context, DISPATCH_PHASE_PREWRITE)
        await h.agent.handle_message(req)
        assert prompt_called is False
        assert prewrite_failure_evidence(req.context).get("reason") == "model_mismatch"
        assert h.controller.events("result")[0][2].get("is_error") is True


@pytest.mark.asyncio
async def test_pre_tool_text_flushes_as_narration_before_end_turn_without_duplication(tmp_path: Path) -> None:
    async with make_agent_harness(tmp_path, name="pre-tool-narration") as h:
        paused_after_tool = asyncio.Event()
        resume_prompt = asyncio.Event()
        orig_get = h.agent._get_or_create_session

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            sess = await orig_get(*a, **kw)

            async def _scripted_prompt(sid: str, _blocks: Any, **_pkw: Any) -> dict[str, Any]:
                await h.agent._handle_notification(
                    sess,
                    "session/update",
                    {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Checking files"}}},
                )
                await h.agent._handle_notification(
                    sess,
                    "session/update",
                    {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " before edit."}}},
                )
                await h.agent._handle_notification(
                    sess,
                    "session/update",
                    {"sessionId": sid, "update": {"sessionUpdate": "tool_call", "toolCallId": "tc-1", "title": "Read file", "kind": "read", "status": "in_progress"}},
                )
                paused_after_tool.set()
                await resume_prompt.wait()
                await h.agent._handle_notification(
                    sess, "session/update",
                    {"sessionId": sid, "update": {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "completed"}},
                )
                await h.agent._handle_notification(
                    sess,
                    "session/update",
                    {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Final answer after tool."}}},
                )
                return {"stopReason": "end_turn"}

            sess.client.session_prompt = _scripted_prompt  # type: ignore[method-assign]
            return sess

        h.agent._get_or_create_session = _wrapped  # type: ignore[method-assign]
        turn_task = asyncio.create_task(h.agent.handle_message(make_request(message="multi-step", workdir=h.workdir)))
        await asyncio.wait_for(paused_after_tool.wait(), timeout=5.0)

        # Pre-tool narration is immediate; the single tool row waits for completion.
        assert [(kind, text) for kind, text, _ in h.controller.emitted] == [
            ("assistant", "Checking files before edit."),
        ]
        assert h.controller.events("assistant")[0][2].get("level") is None
        assert h.controller.events("result") == []

        resume_prompt.set()
        await asyncio.wait_for(turn_task, timeout=5.0)

        results = h.controller.events("result")
        assert len(results) == 1
        assert results[0][1] == "Final answer after tool."
        assert "Checking files" not in results[0][1]


def test_format_tool_activity_renders_real_diff_hunk_preview() -> None:
    from modules.agents.acp.events import AcpDiffBlock, AcpToolCallUpdate

    def _render(old_text: str | None, new_text: str) -> str:
        update = AcpToolCallUpdate(
            session_id="sess-1",
            kind="tool_call_update",
            tool_call_id="tc-diff",
            title="Edit file",
            tool_kind="edit",
            status="completed",
            raw_input=None,
            raw_output=None,
            content=(),
            diffs=(AcpDiffBlock(path="app.py", old_text=old_text, new_text=new_text, raw={}),),
            locations=(),
            raw={},
        )
        text, _ = agy_agent_module._format_tool_activity(update, {"title": "Edit file", "status": "completed"})
        return text

    changed = _render("# header\nold\n", "# header\nnew\n")
    assert "- old" in changed and "+ new" in changed
    assert "- # header" not in changed and "+ # header" not in changed

    new_file = _render(None, "l1\nl2\nl3\nl4\nl5\n")
    assert "+ l1" in new_file and "+ l4" in new_file and "+ l5" not in new_file

    identical = _render("# same\nline2\n", "# same\nline2\n")
    assert identical.splitlines() == ["Edit file (completed)", "diff: app.py"]

    long_line = "x" * 240
    truncated = _render("a\n", f"a\n{long_line}\n")
    plus_lines = [line for line in truncated.splitlines() if line.startswith("+ ")]
    assert len(plus_lines) == 1 and plus_lines[0].endswith("...") and len(plus_lines[0][2:]) <= 200


@pytest.mark.asyncio
async def test_large_diff_preview_is_bounded_and_offloaded_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agy_cfg = antigravity_config("agy")
    controller = FakeController(app_config(agy_cfg))
    agent = AntigravityAgent(controller, agy_cfg, rules_base_dir=tmp_path / "rules")
    req = make_request(message="edit large file", workdir=tmp_path)
    runtime_key = req.composite_session_id
    turn = agy_agent_module._ActiveTurn(
        request=req,
        runtime_key=runtime_key,
        native_session_id="sess-large-diff",
        turn_id="turn-1",
        started_at=time.monotonic(),
    )
    session = SimpleNamespace(
        runtime_key=runtime_key,
        native_session_id="sess-large-diff",
        workdir=str(tmp_path),
        retired=False,
        active_turn=turn,
        last_activity=time.monotonic(),
    )
    agent._sessions[runtime_key] = session  # type: ignore[assignment]

    loop_tid = threading.get_ident()
    observed_tids: list[int] = []
    orig_format = agy_agent_module._format_tool_activity

    def _spy_format(update: Any, state: dict[str, Any]) -> tuple[str, str]:
        observed_tids.append(threading.get_ident())
        return orig_format(update, state)

    monkeypatch.setattr(agy_agent_module, "_format_tool_activity", _spy_format)

    repeated = "repeat = 1\n" * 20_000
    old_text = f"{repeated}value = 'old'\n{repeated}"
    new_text = f"{repeated}value = 'new'\n{repeated}"

    started = time.monotonic()
    await agent._handle_notification(
        session,  # type: ignore[arg-type]
        "session/update",
        {
            "sessionId": "sess-large-diff",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-big",
                "title": "Edit big file",
                "kind": "edit",
                "status": "completed",
                "content": [
                    {"type": "diff", "path": "big.py", "oldText": old_text, "newText": new_text},
                ],
            },
        },
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert len(observed_tids) == 1 and observed_tids[0] != loop_tid
    tool_events = controller.events("toolcall")
    assert len(tool_events) == 1
    assert tool_events[0][1].splitlines() == [
        "Edit big file (completed)",
        "diff: big.py",
        "- value = 'old'",
        "+ value = 'new'",
    ]

    # Distant edits spanning 40,000+ lines stay bounded by _DIFF_PREVIEW_COMPARE_WINDOW.
    span_started = time.monotonic()
    span_preview = agy_agent_module._format_diff_preview_lines(
        f"first_old\n{repeated}{repeated}last_old\n",
        f"first_new\n{repeated}{repeated}last_new\n",
    )
    assert time.monotonic() - span_started < 2.0
    assert span_preview == ["- first_old", "+ first_new"]


@pytest.mark.asyncio
async def test_acp_client_session_fork_applies_request_timeout() -> None:
    from modules.agents.acp.client import AcpClient

    client = AcpClient(["agy", "acp"], request_timeout=17.5)
    recorded: list[tuple[str, dict[str, Any], float | None]] = []

    async def _fake_send_request(method: str, params: Any = None, *, timeout: float | None = None) -> dict[str, Any]:
        recorded.append((method, dict(params or {}), timeout))
        return {"sessionId": "forked-1"}

    client.send_request = _fake_send_request  # type: ignore[method-assign]
    resp = await client.session_fork("src-1", "/tmp/work", additional_directories=["/tmp/rules"])
    assert resp == {"sessionId": "forked-1"}
    assert recorded == [
        (
            "session/fork",
            {"sessionId": "src-1", "cwd": "/tmp/work", "mcpServers": [], "additionalDirectories": ["/tmp/rules"]},
            17.5,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected_guidance"),
    [
        ("en", "Run `agy` once in a terminal to complete Google verification"),
        ("zh", "请先在终端运行一次 `agy` 完成验证后再重试"),
    ],
)
async def test_verification_required_reply_fails_turn_and_normal_verification_prose_succeeds(
    tmp_path: Path, language: str, expected_guidance: str
) -> None:
    from core.agent_auth_service import classify_auth_error

    verification_banner = "\n⚠ Verification Required\n"
    normal_prose = "Verification required: run the tests before merging the google branch"
    title_backfills: list[str] = []

    async with make_agent_harness(tmp_path, language=language, name=f"verify-req-{language}") as h:
        orig_get = h.agent._get_or_create_session
        h.agent._maybe_backfill_session_title = lambda _req, sid: title_backfills.append(sid)  # type: ignore[method-assign]

        reply_holder = {"text": verification_banner}

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            sess = await orig_get(*a, **kw)

            async def _scripted_prompt(sid: str, _blocks: Any, **_pkw: Any) -> dict[str, Any]:
                await h.agent._handle_notification(
                    sess,
                    "session/update",
                    {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": reply_holder["text"]},
                        },
                    },
                )
                return {"stopReason": "end_turn"}

            sess.client.session_prompt = _scripted_prompt  # type: ignore[method-assign]
            return sess

        h.agent._get_or_create_session = _wrapped  # type: ignore[method-assign]

        await h.agent.handle_message(make_request(message="hello", workdir=h.workdir))
        results = h.controller.events("result")
        notes = h.controller.events("notify")
        assert len(results) == 1 and results[0][2].get("is_error") is True
        assert not any(not opts.get("is_error") for _, _, opts in results)
        diagnostic = results[0][2].get("terminal_error") or ""
        assert "verification required" in diagnostic.lower()
        assert classify_auth_error("antigravity", diagnostic) is True
        assert len(notes) == 1 and expected_guidance in notes[0][1]
        assert "Verification Required" in notes[0][1]
        assert title_backfills == []

        h.controller.emitted.clear()
        reply_holder["text"] = normal_prose
        await h.agent.handle_message(make_request(message="explain oauth", workdir=h.workdir))
        ok_results = h.controller.events("result")
        assert len(ok_results) == 1
        assert ok_results[0][1] == normal_prose
        assert not ok_results[0][2].get("is_error")
        assert h.controller.events("notify") == []
        assert len(title_backfills) == 1


async def _start_steer_chain(h):
    req = make_request(message="STEER_CHAIN", workdir=h.workdir, platform_specific={"turn_token": "logical-1"})
    target = ActiveSteerTarget(req.composite_session_id, "logical-1", req.context, req, h.agent)
    # Exercise the same synchronous identity lookup that on_native_start performs.
    observed = []
    h.controller.agent_service.mark_runtime_turn_started = lambda *_a, **_kw: observed.append(h.agent.steering_native_turn_id(target))
    task = asyncio.create_task(h.agent.handle_message(req))
    async def wait_started():
        while not h.controller.token_notes:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait_started(), 5)
    session = h.agent._sessions[req.composite_session_id]
    return req, target, task, session, observed


def _chain_steer(h, target, *, native_id=None, attempt_id="attempt-B", **kwargs):
    return SteerRequest(
        target_session_id="ses_1", expected_logical_turn_id=target.logical_turn_id,
        expected_native_turn_id=native_id or h.agent.steering_native_turn_id(target) or "missing",
        text="also include BANANA", attempt_id=attempt_id, **kwargs,
    )


async def _chain_control(session, action, **kwargs):
    await session.client.send_request("test/steer", {"sessionId": session.native_session_id, "action": action, **kwargs})


@pytest.mark.asyncio
@pytest.mark.parametrize("steer_count", [1, 2])
async def test_native_steer_chain_accepts_without_settling_superseded_prompt(tmp_path, steer_count):
    from core.agent_input import AgentInputMetadata
    from modules.im.base import FileAttachment

    async with make_agent_harness(tmp_path, record_rpc=True) as h:
        h.controller.config.include_user_info = True
        req, target, task, session, observed = await _start_steer_chain(h)
        request = _chain_steer(h, target, input_metadata=AgentInputMetadata(user_name="Owner", user_id="owner"), files=(
            FileAttachment(name="pic.png", mimetype="image/png", content=b"image-bytes"),
        ))
        receipt = await h.agent.steer_active_turn(request, target)
        assert receipt.outcome == SteerOutcome.ACCEPTED
        assert observed == [request.expected_native_turn_id] and observed[0]
        assert h.agent.steering_native_turn_id(target) == observed[0]
        assert not task.done() and h.controller.events("result") == []
        prompts = h.rpcs("session/prompt")
        assert len(prompts) == 2
        assert prompts[-1]["params"]["sessionId"] == session.native_session_id
        blocks = prompts[-1]["params"]["prompt"]
        assert "Owner" in blocks[0]["text"] and "also include BANANA" in blocks[0]["text"]
        assert blocks[-1] == {"type": "image", "data": "aW1hZ2UtYnl0ZXM=", "mimeType": "image/png"}
        if steer_count == 2:
            again = await h.agent.steer_active_turn(_chain_steer(h, target, attempt_id="attempt-C"), target)
            assert again.outcome == SteerOutcome.ACCEPTED
            assert h.agent.steering_native_turn_id(target) == observed[0]
            assert not task.done() and h.controller.events("result") == []
        await _chain_control(session, "finish")
        await asyncio.wait_for(task, 3)
        assert len(h.controller.events("result")) == 1
        assert h.controller.events("result")[0][1] == "FIRST BANANA"
        assert len(h.controller.events("toolcall")) == 1
        assert h.controller.token_notes == [(req.context, 100), (req.context, 150)]
        assert h.agent.steering_native_turn_id(target) is None


@pytest.mark.asyncio
async def test_native_steer_after_prompt_resolved_is_not_active_and_does_not_write(tmp_path):
    async with make_agent_harness(tmp_path, record_rpc=True) as h:
        _, target, task, session, _ = await _start_steer_chain(h)
        request = _chain_steer(h, target)
        await _chain_control(session, "finish")
        await asyncio.wait_for(task, 3)
        receipt = await h.agent.steer_active_turn(request, target)
        assert receipt.outcome == SteerOutcome.NOT_ACTIVE
        assert len(h.rpcs("session/prompt")) == 1


@pytest.mark.asyncio
async def test_native_steer_mismatched_chain_is_refused_without_write(tmp_path):
    async with make_agent_harness(tmp_path, record_rpc=True) as h:
        _, target, task, session, _ = await _start_steer_chain(h)
        receipt = await h.agent.steer_active_turn(_chain_steer(h, target, native_id="wrong-chain"), target)
        assert (receipt.outcome, receipt.reason) == (SteerOutcome.REFUSED, "native_turn_mismatch")
        assert len(h.rpcs("session/prompt")) == 1
        await _chain_control(session, "finish")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_stop_steered_chain_settles_once_and_late_superseded_error_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_agent_module, "_STEER_RECEIPT_TIMEOUT_SECONDS", 0.05, raising=False)
    async with make_agent_harness(tmp_path, server_config={"deferSteerReceipt": True}) as h:
        req, target, task, session, _ = await _start_steer_chain(h)
        receipt = await h.agent.steer_active_turn(_chain_steer(h, target), target)
        assert receipt.outcome == SteerOutcome.UNKNOWN
        assert await h.agent.handle_stop(req)
        await asyncio.wait_for(task, 3)
        results = list(h.controller.events("result"))
        assert len(results) == 1 and results[0][2]["output"].settled_by == SETTLED_BY_STOPPED
        await _chain_control(session, "late_error")
        assert h.controller.events("result") == results
        assert h.agent.steering_native_turn_id(target) is None


@pytest.mark.asyncio
async def test_unknown_steer_reconciles_receipt_without_resending_even_after_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_agent_module, "_STEER_RECEIPT_TIMEOUT_SECONDS", 0.05, raising=False)
    async with make_agent_harness(tmp_path, record_rpc=True, server_config={"deferSteerReceipt": True}) as h:
        _, target, task, session, _ = await _start_steer_chain(h)
        request = _chain_steer(h, target)
        receipt = await h.agent.steer_active_turn(request, target)
        assert receipt.outcome == SteerOutcome.UNKNOWN
        reconcile = SteerReconcileRequest(request.target_session_id, request.expected_logical_turn_id, request.expected_native_turn_id, request.attempt_id)
        assert (await h.agent.reconcile_steer_attempt(reconcile, target)).outcome == SteerOutcome.UNKNOWN
        await _chain_control(session, "receipt")
        assert (await h.agent.reconcile_steer_attempt(reconcile, target)).outcome == SteerOutcome.ACCEPTED
        assert not task.done() and h.controller.events("result") == []
        # Retrying an attempt id observes its evidence instead of resending the text.
        assert (await h.agent.steer_active_turn(request, target)).outcome == SteerOutcome.ACCEPTED
        await _chain_control(session, "finish")
        await asyncio.wait_for(task, 3)
        assert h.agent.reconciliation_steer_target(reconcile) == target
        assert (await h.agent.reconcile_steer_attempt(reconcile, target)).outcome == SteerOutcome.ACCEPTED
        assert len(h.rpcs("session/prompt")) == 2


@pytest.mark.asyncio
async def test_tool_activity_emits_one_row_with_command_output_and_readable_path(tmp_path):
    async with make_agent_harness(tmp_path) as h:
        await h.agent.handle_message(make_request(message="tool_details", workdir=h.workdir))
        rows = h.controller.events("toolcall")
        assert len(rows) == 2
        assert "printf hello" in rows[0][1] and "hello" in rows[0][1] and "exit 0" in rows[0][1]
        assert len(rows[0][1]) < 1600
        assert "../" not in rows[1][1]
        assert str((h.workdir / "../outside.md").resolve()) in rows[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("efforts,expected", [(("low", "medium", "high"), "high"), ((), None)])
async def test_model_effort_maps_to_offered_level_or_runs_without_thinking(efforts, expected):
    options = [{"id": "model", "category": "model", "currentValue": "model-beta", "options": [{"value": "model-beta"}]}]
    if efforts:
        options.append({"id": "thought_level", "category": "thought_level", "currentValue": "medium", "options": [{"value": level} for level in efforts]})
    class Peer:
        async def session_set_config_option(self, sid, option_id, value):
            assert option_id == "thought_level" and value == expected
            return {}
    applied = await apply_model_and_effort(Peer(), "sid", options, model="model-beta", effort="xhigh", current_model="model-beta", default_effort=None)
    assert applied.effort == expected
