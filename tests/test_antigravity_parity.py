"""Hermetic workflow parity and cross-backend acceptance suite for Antigravity."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config import paths
from core.agent_tool_policy import ALWAYS_SESSION_ONLY_TOOL_NAMES, check_tool_call
from core.caller_context import AVIBE_SESSION_ID_ENV
from core.handlers.message_handler import MessageHandler
from core.managed_skills import SKILL_WORKING_DIR_ENV, load_skill, resolve_skills
from core.mcp_hub import native as mcp_native
from core.services.session_fork import FORK_HANDOFF_MODE_SUMMARY, reserve_forked_session
from core.session_handoff import HANDOFF_BUDGET_CHARS
from core.vibe_agents import VibeAgentStore
from modules.agents.antigravity import AGY_CLI_DISABLE_AUTO_UPDATE_ENV
from modules.agents.base import AgentRequest
from modules.agents.subagent_router import list_antigravity_subagents, load_antigravity_subagent
from modules.im import MessageContext
from modules.im.base import FileAttachment
from storage import messages_service
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope
from tests.helpers.antigravity_harness import (
    make_acp_capabilities,
    make_agent_harness,
    make_request,
)


def _write_agent_md(path: Path, *, name: str, description: str, model: str, body: str, effort: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    effort_line = f"thought_level: {effort}\n" if effort else ""
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nmodel: {model}\n{effort_line}---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("image_supported", [True, False])
async def test_attachments_image_blocks_when_supported_and_path_fallback_when_not(
    tmp_path: Path,
    image_supported: bool,
) -> None:
    img_path = tmp_path / "diagram.png"
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
    img_path.write_bytes(png_bytes)
    doc_path = tmp_path / "notes.pdf"
    doc_path.write_bytes(b"%PDF-1.4\nfake-pdf")
    attachments = [
        FileAttachment(name="diagram.png", mimetype="image/png", local_path=str(img_path), size=len(png_bytes)),
        FileAttachment(name="notes.pdf", mimetype="application/pdf", local_path=str(doc_path), size=doc_path.stat().st_size),
    ]

    server_cfg = None if image_supported else {"agentCapabilities": make_acp_capabilities(image=False, logout=False)}
    async with make_agent_harness(tmp_path, record_rpc=True, server_config=server_cfg, name=f"img-{image_supported}") as h:
        await h.agent.handle_message(make_request(message="inspect these", workdir=h.workdir, files=attachments))

        prompt_rpcs = h.rpcs("session/prompt")
        assert len(prompt_rpcs) == 1
        blocks = prompt_rpcs[0]["params"]["prompt"]
        text_blocks = [b for b in blocks if b["type"] == "text"]
        image_blocks = [b for b in blocks if b["type"] == "image"]
        assert len(text_blocks) == 1
        assert "inspect these" in text_blocks[0]["text"]
        assert f"- File: {doc_path}" in text_blocks[0]["text"]
        if image_supported:
            assert len(image_blocks) == 1 and image_blocks[0]["mimeType"] == "image/png"
            assert base64.b64decode(image_blocks[0]["data"]) == png_bytes
            assert f"- Image: {img_path}" not in text_blocks[0]["text"]
        else:
            assert image_blocks == []
            assert f"- Image: {img_path}" in text_blocks[0]["text"]


@pytest.mark.asyncio
async def test_usage_update_context_gauge_and_live_title_backfill(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    ensure_sqlite_state(primary_platform="avibe")
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn, platform="avibe", scope_type="project", native_id="proj_title_usage", now="2026-11-24T00:00:00Z"
            )
            blank_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="ses_blank_anchor",
                agent_backend="antigravity",
                agent_variant="antigravity",
                workdir=str(workdir),
                native_session_id="",
                title="",
            )
            titled_id = create_agent_session_row(
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

    async with make_agent_harness(tmp_path, workdir=workdir, show_duration=True) as h:
        for anchor, row_id in (("ses_blank_anchor", blank_id), ("ses_titled_anchor", titled_id)):
            await h.agent.handle_message(
                make_request(
                    message="stream_all",
                    workdir=workdir,
                    base_session_id=anchor,
                    platform="avibe",
                    platform_specific={
                        "agent_session_target": {"id": row_id, "agent_backend": "antigravity", "native_session_id": ""}
                    },
                )
            )
        if h.agent._background_tasks:
            await asyncio.gather(*list(h.agent._background_tasks))

        assert h.controller.token_totals["C123:100.1"] == 1200
        results = h.controller.events("result")
        assert len(results) == 2
        footer = results[0][2].get("result_footer") or ""
        assert "1200 tok" in footer and "$" not in footer

    engine = create_sqlite_engine()
    try:
        with engine.connect() as conn:
            blank_row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == blank_id)).mappings().one()
            titled_row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == titled_id)).mappings().one()
    finally:
        engine.dispose()

    assert blank_row["title"] == "Update greeting"
    blank_meta = json.loads(blank_row["metadata_json"])
    assert blank_meta["title_source"] == "backend" and blank_meta["title_backend"] == "antigravity"
    assert titled_row["title"] == "User Named Session"


@pytest.mark.asyncio
async def test_fork_without_native_capability_builds_bounded_summary_handoff(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    ensure_sqlite_state(primary_platform="avibe")
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
                conn, platform="avibe", scope_type="project", native_id="proj_agy_fork", now="2026-11-24T00:00:00Z"
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

    fork_result = reserve_forked_session(source_session_id=source_session_id, db_path=db_path)
    assert fork_result.agent_backend == "antigravity"
    assert fork_result.fork.source_native_session_id == "sess-source-native"

    async with make_agent_harness(
        tmp_path,
        workdir=workdir,
        record_rpc=True,
        server_config={"agentCapabilities": make_acp_capabilities(fork=False, logout=False)},
        name="fork",
    ) as h:
        fork_target = {
            "id": fork_result.session_id,
            "agent_backend": "antigravity",
            "native_session_id": "",
            "native_session_fork": fork_result.fork.to_metadata(),
        }
        for msg in ("continue from fork", "second turn after fork"):
            await h.agent.handle_message(
                make_request(
                    message=msg,
                    workdir=workdir,
                    base_session_id=fork_result.session_id,
                    platform="avibe",
                    platform_specific={"agent_session_target": fork_target},
                )
            )

        methods = [m.get("method") for m in h.rpcs()]
        assert "session/fork" not in methods and "session/new" in methods
        prompt_rpcs = h.rpcs("session/prompt")
        assert len(prompt_rpcs) == 2
        first_prompt = prompt_rpcs[0]["params"]["prompt"][0]["text"]
        second_prompt = prompt_rpcs[1]["params"]["prompt"][0]["text"]

    handoff_end = first_prompt.index("[End of Avibe handoff]") + len("[End of Avibe handoff]")
    handoff_block = first_prompt[:handoff_end]
    assert handoff_block.startswith("[Avibe handoff]") and len(handoff_block) <= HANDOFF_BUDGET_CHARS
    assert "bounded summary handoff" in handoff_block and "rather than full native history" in handoff_block
    assert "switched" not in handoff_block and "(earlier messages omitted)" in handoff_block
    assert "turn-marker-39" in handoff_block and "turn-marker-00" not in handoff_block
    assert handoff_block.index("turn-marker-38") < handoff_block.index("turn-marker-39")
    assert f"WHERE session_id='{source_session_id}'" in handoff_block and "continue from fork" in first_prompt
    assert "[Avibe handoff]" not in second_prompt and "second turn after fork" in second_prompt

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            child_row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == fork_result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()
    assert json.loads(child_row["metadata_json"])["fork_handoff_mode"] == FORK_HANDOFF_MODE_SUMMARY


def test_antigravity_native_subagent_discovery_and_project_override(tmp_path: Path) -> None:
    fake_home = Path.home()
    global_agents = fake_home / ".gemini" / "config" / "agents"
    _write_agent_md(
        global_agents / "reviewer.md",
        name="reviewer",
        description="Global reviewer",
        model="model-alpha",
        effort="low",
        body="Global review instructions.",
    )
    _write_agent_md(
        global_agents / "planner.md",
        name="planner",
        description="Global planner",
        model="model-alpha",
        body="Draft plans carefully.",
    )
    (global_agents / "README.md").write_text("# Agents directory\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    project_reviewer = workspace / ".agents" / "agents" / "reviewer.md"
    _write_agent_md(
        project_reviewer,
        name="reviewer",
        description="Project reviewer",
        model="model-beta",
        effort="high",
        body="Review workspace diffs for regressions.",
    )

    discovered = list_antigravity_subagents(project_root=workspace)
    assert set(discovered.keys()) == {"reviewer", "planner"}
    assert (discovered["planner"].source, discovered["planner"].description, discovered["planner"].developer_instructions) == (
        "global",
        "Global planner",
        "Draft plans carefully.",
    )

    reviewer = load_antigravity_subagent("REVIEWER", project_root=workspace)
    assert reviewer is not None
    assert (reviewer.name, reviewer.source, reviewer.description, reviewer.model, reviewer.reasoning_effort) == (
        "reviewer",
        "project",
        "Project reviewer",
        "model-beta",
        "high",
    )
    assert reviewer.developer_instructions == "Review workspace diffs for regressions."
    assert reviewer.path == project_reviewer


@pytest.mark.asyncio
async def test_message_handler_routes_antigravity_subagent_with_isolated_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_agent_md(
        workspace / ".agents" / "agents" / "reviewer.md",
        name="reviewer",
        description="Code reviewer",
        model="model-beta",
        effort="high",
        body="Check edge cases.",
    )

    async with make_agent_harness(tmp_path, workdir=workspace, record_rpc=True, name="subagent") as h:
        ctrl = h.controller
        sessions = ctrl.sessions

        async def _dispatch_to_agent(backend: str, req: AgentRequest) -> None:
            assert backend == "antigravity"
            await h.agent.handle_message(req)

        ctrl.agent_service = SimpleNamespace(
            agents={"antigravity": h.agent},
            handle_message=AsyncMock(side_effect=_dispatch_to_agent),
        )
        ctrl.session_manager = sessions
        ctrl.receiver_tasks = {}
        ctrl.resolve_agent_for_context = lambda _ctx: "antigravity"
        ctrl.resolve_vibe_agent_for_context = lambda _ctx, **_kw: SimpleNamespace(
            id="va_agy",
            name="antigravity",
            backend="antigravity",
            model="model-alpha",
            reasoning_effort="low",
            system_prompt="Follow Avibe instructions.",
        )
        ctrl.processing_indicator = SimpleNamespace(
            start=AsyncMock(return_value=None),
            apply_to_request=lambda *_a, **_kw: None,
            finish=AsyncMock(),
        )
        ctrl.get_settings_manager_for_context = lambda _ctx: SimpleNamespace(get_channel_routing=lambda _key: None)
        ctrl.get_im_client_for_context = lambda _ctx: ctrl.im_client

        handler = MessageHandler(ctrl)
        handler.set_session_handler(
            SimpleNamespace(
                get_session_info=lambda _ctx, source="human": ("slack_thread_1", str(workspace), f"slack_thread_1:{workspace}"),
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

        await handler.handle_user_message(_ctx("m1"), "parent-secret-marker")
        parent_key = f"slack_thread_1:{workspace}"
        parent_sess = h.agent._sessions[parent_key]
        parent_pid, parent_native_id = parent_sess.client.pid, parent_sess.native_session_id
        assert parent_native_id == "sess-1"

        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m2"), "reviewer: audit-subagent-marker")
        subagent_key = f"slack_thread_1:reviewer:{workspace}"
        sub_sess = h.agent._sessions[subagent_key]
        assert sub_sess.client.pid != parent_pid and sub_sess.native_session_id != parent_native_id
        assert sub_sess.spec.rules_dirs != parent_sess.spec.rules_dirs
        assert (sub_sess.current_model, sub_sess.current_effort) == ("model-beta", "high")
        assert parent_sess.client.is_alive
        assert sessions._by_id[parent_row_id]["native_session_id"] == parent_native_id

        launches = h.launches()
        assert len(launches) == 2 and "--agent" not in launches[0]["argv"] and launches[1]["argv"][-2:] == ["--agent", "reviewer"]

        # Follow-ups reuse each isolated process/session.
        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m3"), "reviewer: subagent-followup")
        sub_reply = ctrl.events("result")[0][1]
        assert "subagent-followup" in sub_reply and "model=model-beta" in sub_reply
        assert h.agent._sessions[subagent_key].client.pid == sub_sess.client.pid

        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m4"), "parent-followup")
        parent_reply = ctrl.events("result")[0][1]
        assert "parent-followup" in parent_reply and "model=model-alpha" in parent_reply
        assert h.agent._sessions[parent_key].client.pid == parent_pid
        assert len(h.launches()) == 2

        prompts_by_session: dict[str, list[str]] = {}
        for msg in h.rpcs("session/prompt"):
            text = "\n".join(b.get("text", "") for b in msg["params"]["prompt"] if b.get("type") == "text")
            prompts_by_session.setdefault(msg["params"]["sessionId"], []).append(text)
        parent_prompts = "\n".join(prompts_by_session[parent_native_id])
        sub_prompts = "\n".join(prompts_by_session[sub_sess.native_session_id])
        assert "parent-secret-marker" in parent_prompts and "parent-followup" in parent_prompts
        assert "audit-subagent-marker" not in parent_prompts
        assert "audit-subagent-marker" in sub_prompts and "subagent-followup" in sub_prompts
        assert "parent-secret-marker" not in sub_prompts


@pytest.mark.asyncio
async def test_combined_fixture_skills_mcp_rules_tool_policy_and_managed_launch(tmp_path: Path) -> None:
    fake_home = Path.home()
    workspace = tmp_path / "combined-workspace"
    workspace.mkdir()

    # 1. Workspace & global Antigravity skills
    for skill_dir, name, desc, body in (
        (workspace / ".agents" / "skills" / "repo-checks", "repo-checks", "Run repository parity checks", "Run pytest locally."),
        (fake_home / ".gemini" / "antigravity-cli" / "skills" / "agy-helper", "agy-helper", "Antigravity workspace helper", "Check status first."),
    ):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")

    # 2. Native Antigravity MCP configs (global + workspace)
    global_mcp = fake_home / ".gemini" / "config" / "mcp_config.json"
    global_mcp.parent.mkdir(parents=True, exist_ok=True)
    global_mcp_bytes = json.dumps({"mcpServers": {"global_docs": {"command": sys.executable, "args": ["-c", "print('ok')"]}}}).encode()
    global_mcp.write_bytes(global_mcp_bytes)

    project_mcp = workspace / ".agents" / "mcp_config.json"
    project_mcp.parent.mkdir(parents=True, exist_ok=True)
    project_mcp_bytes = json.dumps({"mcpServers": {"repo_tools": {"serverUrl": "https://mcp.example.test/sse"}}}).encode()
    project_mcp.write_bytes(project_mcp_bytes)

    # 3. Project AGENTS.md and global GEMINI.md rules (must stay byte-identical)
    project_rules = workspace / "AGENTS.md"
    project_rules_bytes = b"# Project Rules\nPROJECT_RULE_MARKER: keep diffs small.\n"
    project_rules.write_bytes(project_rules_bytes)
    global_rules = fake_home / ".gemini" / "GEMINI.md"
    global_rules_bytes = b"# Global Rules\nGLOBAL_RULE_MARKER: always verify.\n"
    global_rules.write_bytes(global_rules_bytes)

    async with make_agent_harness(tmp_path, workdir=workspace, record_rpc=True, name="combined") as h:
        req = make_request(message="recall", workdir=workspace)
        await h.agent.handle_message(req)

        launches = h.launches()
        assert len(launches) == 1
        assert launches[0]["argv"][:3] == ["acp", "--disable-telemetry", "--dangerously-skip-permissions"]
        assert launches[0]["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV] == "true"
        assert launches[0]["env"][SKILL_WORKING_DIR_ENV] == str(workspace)
        assert launches[0]["env"][AVIBE_SESSION_ID_ENV] == "ses_1"

        new_rpcs = h.rpcs("session/new")
        assert len(new_rpcs) == 1 and new_rpcs[0]["params"]["mcpServers"] == []
        add_dirs = new_rpcs[0]["params"]["additionalDirectories"]
        assert len(add_dirs) >= 1
        rendered_rules = "\n".join((Path(d) / "AGENTS.md").read_text(encoding="utf-8") for d in add_dirs)
        assert "repo-checks" in rendered_rules and "agy-helper" in rendered_rules

        resolved = {s.name: s for s in resolve_skills(workspace)}
        assert {"repo-checks", "agy-helper"} <= set(resolved)
        loaded_repo = load_skill("repo-checks", workspace)
        loaded_global = load_skill("agy-helper", workspace)
        assert loaded_repo is not None and "Run pytest locally." in (loaded_repo.body or "")
        assert loaded_global is not None and "Check status first." in (loaded_global.body or "")

        global_servers = mcp_native.read_all()
        assert "global_docs" in global_servers and global_servers["global_docs"].source == mcp_native.ANTIGRAVITY
        assert global_servers["global_docs"].error is None
        project_servers = mcp_native.read_project(workspace)
        assert "repo_tools" in project_servers and project_servers["repo_tools"].source == mcp_native.ANTIGRAVITY
        assert project_servers["repo_tools"].error is None

        assert project_rules.read_bytes() == project_rules_bytes
        assert global_rules.read_bytes() == global_rules_bytes
        assert global_mcp.read_bytes() == global_mcp_bytes
        assert project_mcp.read_bytes() == project_mcp_bytes

        for blocked_tool in ("schedule", "invoke_subagent", "define_subagent"):
            assert blocked_tool in ALWAYS_SESSION_ONLY_TOOL_NAMES
            decision = check_tool_call(blocked_tool, {})
            assert decision.denied is True and "Harness" in decision.reason
        for sync_tool in ("read_file", "run_command", "grep_search"):
            assert check_tool_call(sync_tool, {}).allowed is True

        session = h.agent._sessions[req.composite_session_id]
        perm_options = [
            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
        ]
        for tool_name, raw_input, expected_opt in (
            ("schedule", {}, "reject_once"),
            ("read_file", {"path": "a.py"}, "allow_once"),
        ):
            resp = await h.agent._handle_server_request(
                session,
                "session/request_permission",
                {
                    "sessionId": session.native_session_id,
                    "toolCall": {"toolCallId": f"tc-{tool_name}", "name": tool_name, "rawInput": raw_input},
                    "options": perm_options,
                },
            )
            assert resp == {"outcome": {"outcome": "selected", "optionId": expected_opt}}
