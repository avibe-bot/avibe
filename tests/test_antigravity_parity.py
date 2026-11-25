"""Hermetic workflow parity and cross-backend acceptance suite for Antigravity."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from config import paths
from core.caller_context import AVIBE_SESSION_ID_ENV
from core.handlers.command_handlers import CommandHandlers
from core.handlers.message_handler import MessageHandler
from core.managed_skills import SKILL_WORKING_DIR_ENV, load_skill, resolve_skills
from core.mcp_hub import native as mcp_native
from core.reply_enhancer import process_reply
from core.services import sessions as sessions_service
from core.services.session_fork import (
    FORK_HANDOFF_MODE_NATIVE,
    FORK_HANDOFF_MODE_SUMMARY,
    can_use_native_tip_fork,
    pending_summary_fork_handoff_for_session,
    reserve_forked_session,
)
from core.session_handoff import HANDOFF_BUDGET_CHARS
from core.vibe_agents import VibeAgentStore, parse_agent_file
from modules.agents.antigravity import AGY_CLI_DISABLE_AUTO_UPDATE_ENV
from modules.agents.antigravity import agent as agy_agent_module
from modules.agents.base import AgentRequest
from modules.agents.native_sessions.antigravity import AntigravityNativeSessionProvider
from modules.agents.native_sessions.service import AgentNativeSessionService
from modules.agents.subagent_router import list_antigravity_subagents, load_antigravity_subagent
from modules.im import MessageContext
from modules.im.base import FileAttachment
from storage import messages_service
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.settings_service import upsert_scope
from tests.helpers.antigravity_harness import (
    make_acp_capabilities,
    make_agent_harness,
    make_model_config_options,
    make_request,
    read_session_row,
    seed_fork_source_session,
)


def _write_agent_md(path: Path, body: str, **frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    path.write_text(f"---\n{header}\n---\n\n{body}\n", encoding="utf-8")


def _fork_target(fork_result: Any, native_session_id: str = "") -> dict[str, Any]:
    target: dict[str, Any] = {"id": fork_result.session_id, "agent_backend": "antigravity", "native_session_id": native_session_id}
    if not native_session_id:
        target["native_session_fork"] = fork_result.fork.to_metadata()
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize("image_supported", [True, False])
async def test_attachments_image_blocks_when_supported_and_path_fallback_when_not(tmp_path: Path, image_supported: bool) -> None:
    img_path, doc_path, png_bytes = tmp_path / "diagram.png", tmp_path / "notes.pdf", b"\x89PNG\r\n\x1a\nfake-png-payload"
    img_path.write_bytes(png_bytes)
    doc_path.write_bytes(b"%PDF-1.4\nfake-pdf")
    attachments = [
        FileAttachment(name="diagram.png", mimetype="image/png", local_path=str(img_path), size=len(png_bytes)),
        FileAttachment(name="notes.pdf", mimetype="application/pdf", local_path=str(doc_path), size=doc_path.stat().st_size),
    ]
    server_cfg = None if image_supported else {"agentCapabilities": make_acp_capabilities(image=False, logout=False)}
    async with make_agent_harness(tmp_path, record_rpc=True, server_config=server_cfg, name=f"img-{image_supported}") as h:
        await h.agent.handle_message(make_request(message="inspect these", workdir=h.workdir, files=attachments))
        blocks = h.rpcs("session/prompt")[0]["params"]["prompt"]
        text_blocks, image_blocks = [b for b in blocks if b["type"] == "text"], [b for b in blocks if b["type"] == "image"]
        assert len(text_blocks) == 1 and "inspect these" in text_blocks[0]["text"] and f"- File: {doc_path}" in text_blocks[0]["text"]
        if image_supported:
            assert len(image_blocks) == 1 and image_blocks[0]["mimeType"] == "image/png" and base64.b64decode(image_blocks[0]["data"]) == png_bytes and f"- Image: {img_path}" not in text_blocks[0]["text"]
        else:
            assert image_blocks == [] and f"- Image: {img_path}" in text_blocks[0]["text"]


@pytest.mark.asyncio
async def test_agent_message_chunk_image_blocks_materialized_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop_tid, write_tids, orig_writer = threading.get_ident(), [], agy_agent_module._write_reply_image_bytes
    monkeypatch.setattr(agy_agent_module, "_write_reply_image_bytes", lambda *a, **kw: (write_tids.append(threading.get_ident()), orig_writer(*a, **kw))[1])

    async with make_agent_harness(tmp_path) as h:
        await h.agent.handle_message(make_request(message="emit_image_only", workdir=h.workdir))
        reply_text = h.controller.events("result")[0][1]
        assert reply_text.startswith("![image](file://") and write_tids and all(t != loop_tid for t in write_tids)
        enhanced = process_reply(reply_text, include_quick_replies=False)
        saved = Path(enhanced.files[0].path)
        assert len(enhanced.files) == 1 and enhanced.files[0].is_image is True and saved.is_file() and saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert saved.is_relative_to(paths.get_vibe_remote_dir() / "attachments" / "antigravity")

        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="emit_image_uri", workdir=h.workdir))
        assert h.controller.events("result")[0][1] == "![image](https://example.test/chart.png)"


@pytest.mark.asyncio
async def test_usage_update_context_gauge_and_live_title_backfill(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    ensure_sqlite_state(primary_platform="avibe")
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_title_usage", now="2026-11-24T00:00:00Z")
            blank_id = create_agent_session_row(conn, scope_id=scope_id, session_anchor="ses_blank_anchor", agent_backend="antigravity", agent_variant="antigravity", workdir=str(workdir), native_session_id="", title="")
            titled_id = create_agent_session_row(conn, scope_id=scope_id, session_anchor="ses_titled_anchor", agent_backend="antigravity", agent_variant="antigravity", workdir=str(workdir), native_session_id="", title="User Named Session", metadata={"title_source": "user"})
    finally:
        engine.dispose()

    async with make_agent_harness(tmp_path, workdir=workdir, show_duration=True) as h:
        for anchor, row_id in (("ses_blank_anchor", blank_id), ("ses_titled_anchor", titled_id)):
            await h.agent.handle_message(make_request(message="stream_all", workdir=workdir, base_session_id=anchor, platform="avibe", platform_specific={"agent_session_target": {"id": row_id, "agent_backend": "antigravity", "native_session_id": ""}}))
        if h.agent._background_tasks:
            await asyncio.gather(*list(h.agent._background_tasks))
        footer = h.controller.events("result")[0][2].get("result_footer") or ""
        assert h.controller.token_totals["C123:100.1"] == 1200 and "1200 tok" in footer and "$" not in footer

    blank_row, titled_row = read_session_row(blank_id), read_session_row(titled_id)
    assert blank_row["title"] == "Update greeting" and blank_row["metadata"]["title_source"] == "backend" and blank_row["metadata"]["title_backend"] == "antigravity"
    assert titled_row["title"] == "User Named Session"


@pytest.mark.asyncio
async def test_antigravity_title_backfill_and_native_discovery_do_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_sqlite_state()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_ag_backfill", now="2026-07-26T08:00:00Z")
            session = sessions_service.create_session(conn, scope_id=scope_id, agent_backend="antigravity")
    finally:
        engine.dispose()

    loop_tid, seen_threads = threading.get_ident(), []

    async def _slow_list(self: AntigravityNativeSessionProvider, path: str) -> list[dict[str, Any]]:
        seen_threads.append(threading.get_ident())
        await asyncio.sleep(0.2)
        return [{"sessionId": "ag-sess-slow", "cwd": path, "title": "Offloaded Antigravity Title", "updatedAt": "2026-07-26T08:01:00Z"}]

    monkeypatch.setattr(AntigravityNativeSessionProvider, "_list_raw_sessions", _slow_list)
    provider = AntigravityNativeSessionProvider(cli_path="agy")
    for call in (lambda: provider.list_metadata(str(workdir)), lambda: provider.get_title(native_session_id="ag-sess-slow", working_path=str(workdir))):
        with pytest.raises(RuntimeError, match="running event loop"):
            call()

    async def _timer() -> float:
        t0 = time.monotonic()
        await asyncio.sleep(0.01)
        return time.monotonic() - t0

    async_items, elapsed = await asyncio.gather(provider.list_metadata_async(str(workdir)), _timer())
    assert elapsed < 0.1 and [i.native_session_id for i in async_items] == ["ag-sess-slow"]
    async_title = await provider.get_title_async(working_path=str(workdir), native_session_id="ag-sess-slow")
    assert async_title is not None and async_title.title == "Offloaded Antigravity Title"

    thread_items, t_elapsed = await asyncio.gather(asyncio.to_thread(provider.list_metadata, str(workdir)), _timer())
    assert t_elapsed < 0.1 and len(thread_items) == 1 and seen_threads[-1] != loop_tid

    cmd_ctrl = SimpleNamespace(
        config=SimpleNamespace(platform="slack"), im_client=SimpleNamespace(), settings_manager=SimpleNamespace(), session_manager=SimpleNamespace(),
        get_cwd=lambda _ctx: str(workdir), get_native_session_service=lambda: AgentNativeSessionService(providers=[provider]), agent_service=SimpleNamespace(agents={"antigravity": object()}),
    )
    (listed_path, listed), cmd_elapsed = await asyncio.gather(
        CommandHandlers(cmd_ctrl)._list_recent_native_sessions_async(MessageContext(user_id="U1", channel_id="C1", platform="slack"), limit=10), _timer()
    )
    assert cmd_elapsed < 0.1 and listed_path == str(workdir) and [i.native_session_id for i in listed] == ["ag-sess-slow"] and seen_threads[-1] != loop_tid

    seen_threads.clear()
    async with make_agent_harness(tmp_path, workdir=workdir, cli_path="agy") as h:
        req = make_request(message="hello", workdir=workdir)
        req.context.platform_specific["agent_session_id"] = session["id"]
        before = set(asyncio.all_tasks())
        h.agent._maybe_backfill_session_title(req, "ag-sess-slow")
        tasks = [t for t in asyncio.all_tasks() if t not in before]
        assert len(tasks) == 1 and await _timer() < 0.1
        await asyncio.gather(*tasks)
    assert seen_threads and seen_threads[-1] != loop_tid and read_session_row(session["id"])["title"] == "Offloaded Antigravity Title"


@pytest.mark.asyncio
@pytest.mark.parametrize("language,phrase", [("en", "summary of the conversation"), ("zh", "对话摘要")])
async def test_fork_without_native_capability_builds_bounded_summary_handoff(tmp_path: Path, language, phrase) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    msgs = [("user" if i % 2 == 0 else "agent", "user" if i % 2 == 0 else "result", f"turn-marker-{i:02d} " + ("payload " * 250)) for i in range(40)]
    db_path, _, source_id = seed_fork_source_session(workdir, native_id="proj_agy_fork", worker_name="agy-worker", native_session_id="sess-source-native", messages=msgs)
    fork_result = reserve_forked_session(source_session_id=source_id, db_path=db_path)
    assert (fork_result.agent_backend, fork_result.fork.source_native_session_id) == ("antigravity", "sess-source-native")

    async with make_agent_harness(tmp_path, workdir=workdir, record_rpc=True, server_config={"agentCapabilities": make_acp_capabilities(fork=False, logout=False)}, name="fork", language=language) as h:
        for msg in ("continue from fork", "second turn after fork"):
            await h.agent.handle_message(make_request(message=msg, workdir=workdir, base_session_id=fork_result.session_id, platform="avibe", platform_specific={"agent_session_target": _fork_target(fork_result)}))
        methods = [m.get("method") for m in h.rpcs()]
        assert "session/fork" not in methods and "session/new" in methods
        first_prompt, second_prompt = [r["params"]["prompt"][0]["text"] for r in h.rpcs("session/prompt")]

    handoff = first_prompt[: first_prompt.index("[End of Avibe handoff]") + len("[End of Avibe handoff]")]
    assert handoff.startswith("[Avibe handoff]") and len(handoff) <= HANDOFF_BUDGET_CHARS
    assert "bounded summary handoff" in handoff and "rather than full native history" in handoff and "switched" not in handoff and "(earlier messages omitted)" in handoff
    assert "turn-marker-39" in handoff and "turn-marker-00" not in handoff and handoff.index("turn-marker-38") < handoff.index("turn-marker-39")
    assert f"WHERE session_id='{source_id}'" in handoff and "continue from fork" in first_prompt and "[Avibe handoff]" not in second_prompt and "second turn after fork" in second_prompt
    notices = h.controller.events("notify")
    assert len(notices) == 1 and phrase in notices[0][1]
    assert read_session_row(fork_result.session_id, db_path)["metadata"]["fork_handoff_mode"] == FORK_HANDOFF_MODE_SUMMARY


@pytest.mark.asyncio
async def test_summary_fork_handoff_survives_failed_turn_and_eviction_until_accepted(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    db_path, _, source_id = seed_fork_source_session(
        workdir, native_id="proj_agy_fork_retry", worker_name="agy-fork-worker", native_session_id="sess-source-native",
        messages=[("user", "user", "CRITICAL-SOURCE-CONTEXT-MARKER: keep this context across fork retry"), ("agent", "result", "Acknowledged CRITICAL-SOURCE-CONTEXT-MARKER.")],
    )
    fork_result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    async with make_agent_harness(tmp_path, workdir=workdir, record_rpc=True, server_config={"agentCapabilities": make_acp_capabilities(fork=False, logout=False)}, name="fork-retry") as h:
        await h.agent.handle_message(make_request(message="turn 1 invalid model", workdir=workdir, base_session_id=fork_result.session_id, platform="avibe", model="nonexistent-model", platform_specific={"agent_session_target": _fork_target(fork_result)}))
        assert h.rpcs("session/prompt") == [] and len(h.rpcs("session/new")) == 1
        persisted_native_id = h.controller.sessions._by_id[fork_result.session_id]["native_session_id"]
        assert persisted_native_id and read_session_row(fork_result.session_id, db_path)["metadata"].get("fork_handoff_pending") is True
        assert pending_summary_fork_handoff_for_session(fork_result.session_id, db_path=db_path) is not None

        await h.agent.end_session(fork_result.session_id)
        h.controller.emitted.clear()
        t2_target = _fork_target(fork_result, persisted_native_id)
        await h.agent.handle_message(make_request(message="turn 2 valid retry", workdir=workdir, base_session_id=fork_result.session_id, platform="avibe", model="model-alpha", platform_specific={"agent_session_target": t2_target}))
        assert h.rpcs("session/resume")[0]["params"]["sessionId"] == persisted_native_id and not h.controller.events("result")[0][2].get("is_error")
        t2_prompt = h.rpcs("session/prompt")[0]["params"]["prompt"][0]["text"]
        assert "[Avibe handoff]" in t2_prompt and "CRITICAL-SOURCE-CONTEXT-MARKER" in t2_prompt and "turn 2 valid retry" in t2_prompt
        meta2 = read_session_row(fork_result.session_id, db_path)["metadata"]
        assert meta2.get("fork_handoff_mode") == FORK_HANDOFF_MODE_SUMMARY and "fork_handoff_pending" not in meta2
        assert pending_summary_fork_handoff_for_session(fork_result.session_id, db_path=db_path) is None

        await h.agent.end_session(fork_result.session_id)
        await h.agent.handle_message(make_request(message="turn 3 after restart", workdir=workdir, base_session_id=fork_result.session_id, platform="avibe", model="model-alpha", platform_specific={"agent_session_target": t2_target}))
        t3_prompt = h.rpcs("session/prompt")[1]["params"]["prompt"][0]["text"]
        assert len(h.rpcs("session/resume")) == 2 and "[Avibe handoff]" not in t3_prompt and "turn 3 after restart" in t3_prompt


@pytest.mark.asyncio
async def test_native_fork_preserves_reserved_boundary_and_falls_back_when_source_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    db_path, scope_id, source_id = seed_fork_source_session(
        workdir, native_id="proj_agy_fork_boundary", worker_name="agy-boundary-worker", native_session_id="sess-1",
        messages=[("user", "user", "PRE-FORK-USER-FACT: keep alpha"), ("agent", "result", "PRE-FORK-AGENT-FACT: acknowledged alpha")],
    )

    loop_tid, check_tids, orig_check = threading.get_ident(), [], agy_agent_module.can_use_native_tip_fork
    monkeypatch.setattr(agy_agent_module, "can_use_native_tip_fork", lambda *a, **kw: (check_tids.append(threading.get_ident()), orig_check(*a, **kw))[1])

    unadvanced, advanced = reserve_forked_session(source_session_id=source_id, db_path=db_path), reserve_forked_session(source_session_id=source_id, db_path=db_path)
    assert can_use_native_tip_fork(unadvanced.fork.to_metadata(), db_path=db_path) is True
    assert can_use_native_tip_fork({**unadvanced.fork.to_metadata(), "trim_latest_running_turn": True}, db_path=db_path) is False
    assert can_use_native_tip_fork({"source_session_id": "missing-ses", "source_native_session_id": "sess-1", "source_backend": "antigravity"}, db_path=tmp_path / "nonexistent.sqlite3") is True

    async with make_agent_harness(tmp_path, workdir=workdir, record_rpc=True, server_config={"agentCapabilities": make_acp_capabilities(fork=True, logout=False)}, name="fork-boundary") as h:
        h.state_file.write_text(
            json.dumps({"authenticated": True, "next_session_num": 2, "sessions": {"sess-1": {"sessionId": "sess-1", "cwd": str(workdir), "mcpServers": [], "additionalDirectories": [], "configOptions": make_model_config_options()[:1], "mode": "code", "model": "model-alpha", "title": "Source", "history": [{"prompt": "PRE-FORK-NATIVE-HISTORY", "reply": "ok"}]}}}),
            encoding="utf-8",
        )

        await h.agent.handle_message(make_request(message="recall", workdir=workdir, base_session_id=unadvanced.session_id, platform_specific={"agent_session_target": _fork_target(unadvanced)}))
        assert "PRE-FORK-NATIVE-HISTORY" in h.controller.events("result")[0][1] and any(r.get("method") == "session/fork" for r in h.rpcs()) and not any(r.get("method") == "session/new" for r in h.rpcs())
        assert read_session_row(unadvanced.session_id, db_path)["metadata"].get("fork_handoff_mode") == FORK_HANDOFF_MODE_NATIVE

        engine = create_sqlite_engine(db_path)
        try:
            with engine.begin() as conn:
                for author, mtype, text in (("user", "user", "POST-FORK-SECRET-LEAK: must not appear in child"), ("agent", "result", "POST-FORK-AGENT-LEAK: must not appear in child")):
                    messages_service.append(conn, scope_id=scope_id, session_id=source_id, platform="avibe", author=author, message_type=mtype, text=text)
        finally:
            engine.dispose()
        assert can_use_native_tip_fork(advanced.fork.to_metadata(), db_path=db_path) is False

        assert h.rpc_log is not None
        h.rpc_log.unlink(missing_ok=True)
        h.controller.emitted.clear()
        await h.agent.handle_message(make_request(message="continue from reserved boundary", workdir=workdir, base_session_id=advanced.session_id, platform_specific={"agent_session_target": _fork_target(advanced)}))
        methods2 = [r.get("method") for r in h.rpcs()]
        assert "session/fork" not in methods2 and "session/new" in methods2
        sent_text = "\n".join(b.get("text", "") for b in h.rpcs("session/prompt")[0]["params"]["prompt"] if b.get("type") == "text")
        assert "[Avibe handoff]" in sent_text and "PRE-FORK-USER-FACT: keep alpha" in sent_text and "PRE-FORK-AGENT-FACT: acknowledged alpha" in sent_text
        assert "POST-FORK-SECRET-LEAK" not in sent_text and "POST-FORK-AGENT-LEAK" not in sent_text
        assert read_session_row(advanced.session_id, db_path)["metadata"].get("fork_handoff_mode") == FORK_HANDOFF_MODE_SUMMARY

    assert check_tids and all(tid != loop_tid for tid in check_tids)


def test_antigravity_native_subagent_discovery_override_and_thought_level_aliases(tmp_path: Path) -> None:
    ensure_sqlite_state(primary_platform="avibe")
    global_agents = Path.home() / ".gemini" / "config" / "agents"
    _write_agent_md(global_agents / "reviewer.md", "Global review instructions.", name="reviewer", description="Global reviewer", model="model-alpha", thought_level="low")
    _write_agent_md(global_agents / "planner.md", "Draft plans carefully.", name="planner", description="Global planner", model="model-alpha")
    (global_agents / "README.md").write_text("# Agents directory\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    agents_dir = workspace / ".agents" / "agents"
    project_reviewer, snake_file, camel_file, both_file = agents_dir / "reviewer.md", agents_dir / "snake-reviewer.md", agents_dir / "camel-reviewer.md", agents_dir / "precedence-reviewer.md"
    _write_agent_md(project_reviewer, "Review workspace diffs for regressions.", name="reviewer", description="Project reviewer", model="model-beta", thought_level="high")
    _write_agent_md(snake_file, "Review carefully.", name="snake-reviewer", description="Snake thought level", model="model-alpha", thought_level="high", custom_tag="keep-me")
    _write_agent_md(camel_file, "Fast check.", name="camel-reviewer", description="Camel thought level", model="model-beta", thoughtLevel="low")
    _write_agent_md(both_file, "Precedence test.", name="precedence-reviewer", model="model-alpha", reasoning_effort="medium", thought_level="high", thoughtLevel="low")

    discovered = list_antigravity_subagents(project_root=workspace)
    assert set(discovered) == {"reviewer", "planner", "snake-reviewer", "camel-reviewer", "precedence-reviewer"}
    assert (discovered["planner"].source, discovered["planner"].description, discovered["planner"].developer_instructions) == ("global", "Global planner", "Draft plans carefully.")
    reviewer = load_antigravity_subagent("REVIEWER", project_root=workspace)
    assert reviewer is not None and (reviewer.name, reviewer.source, reviewer.model, reviewer.reasoning_effort, reviewer.path) == ("reviewer", "project", "model-beta", "high", project_reviewer)

    snake_c, camel_c, both_c = [parse_agent_file(f, backend="antigravity") for f in (snake_file, camel_file, both_file)]
    assert (snake_c.reasoning_effort, snake_c.metadata, camel_c.reasoning_effort, camel_c.metadata, both_c.reasoning_effort) == ("high", {"custom_tag": "keep-me"}, "low", {}, "medium")
    assert "thought_level" not in both_c.metadata and "thoughtLevel" not in both_c.metadata
    for cand in (snake_c, camel_c, both_c):
        sub = load_antigravity_subagent(cand.name, project_root=workspace)
        assert sub is not None and (sub.reasoning_effort, sub.model, sub.description) == (cand.reasoning_effort, cand.model, cand.description)

    store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        res = store.import_candidates([snake_c, camel_c, both_c])
        by_name = {a.name: a for a in res.imported}
        assert res.skipped == [] and by_name["snake-reviewer"].reasoning_effort == "high" and by_name["snake-reviewer"].metadata.get("custom_tag") == "keep-me"
        assert by_name["camel-reviewer"].reasoning_effort == "low" and by_name["precedence-reviewer"].reasoning_effort == "medium"
        assert all("thought_level" not in a.metadata and "thoughtLevel" not in a.metadata for a in by_name.values())
    finally:
        store.close()

    claude_cand = parse_agent_file(snake_file, backend="claude")
    assert claude_cand.reasoning_effort is None and claude_cand.metadata.get("thought_level") == "high"


@pytest.mark.asyncio
async def test_message_handler_routes_antigravity_subagent_with_isolated_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_agent_md(workspace / ".agents" / "agents" / "reviewer.md", "Check edge cases.", name="reviewer", description="Code reviewer", model="model-beta", thought_level="high")

    async with make_agent_harness(tmp_path, workdir=workspace, record_rpc=True, name="subagent") as h:
        ctrl, sessions = h.controller, h.controller.sessions

        async def _dispatch(backend: str, req: AgentRequest) -> None:
            assert backend == "antigravity"
            await h.agent.handle_message(req)

        ctrl.agent_service = SimpleNamespace(agents={"antigravity": h.agent}, handle_message=AsyncMock(side_effect=_dispatch))
        ctrl.session_manager, ctrl.receiver_tasks = sessions, {}
        ctrl.resolve_agent_for_context = lambda _ctx: "antigravity"
        ctrl.resolve_vibe_agent_for_context = lambda _ctx, **_kw: SimpleNamespace(id="va_agy", name="antigravity", backend="antigravity", model="model-alpha", reasoning_effort="low", system_prompt="Follow Avibe instructions.")
        ctrl.processing_indicator = SimpleNamespace(start=AsyncMock(return_value=None), apply_to_request=lambda *_a, **_kw: None, finish=AsyncMock())
        ctrl.get_settings_manager_for_context = lambda _ctx: SimpleNamespace(get_channel_routing=lambda _key: None)
        ctrl.get_im_client_for_context = lambda _ctx: ctrl.im_client

        handler = MessageHandler(ctrl)
        handler.set_session_handler(SimpleNamespace(get_session_info=lambda _ctx, source="human": ("slack_thread_1", str(workspace), f"slack_thread_1:{workspace}"), should_allocate_scheduled_anchor=lambda _ctx, source="human": False))
        handler._claim_native_human_event = lambda _ctx: True  # type: ignore[method-assign]
        handler._get_settings_key = handler._get_session_key = lambda _ctx: "slack::channel::C1"  # type: ignore[method-assign]
        handler._attach_backend_handoff = AsyncMock()  # type: ignore[method-assign]
        parent_row_id = sessions.ensure_agent_session_id("slack::channel::C1", "antigravity", "slack_thread_1")

        def _ctx(msg_id: str) -> MessageContext:
            return MessageContext(
                user_id="U1", channel_id="C1", platform="avibe", thread_id="thread_1", message_id=msg_id,
                platform_specific={"agent_session_target": {"id": parent_row_id, "agent_backend": "antigravity", "native_session_id": sessions._by_id[parent_row_id].get("native_session_id", ""), "model": "model-alpha", "reasoning_effort": "low"}},
            )

        await handler.handle_user_message(_ctx("m1"), "parent-secret-marker")
        parent_key, subagent_key = f"slack_thread_1:{workspace}", f"slack_thread_1:reviewer:{workspace}"
        parent_sess = h.agent._sessions[parent_key]
        parent_pid, parent_native_id = parent_sess.client.pid, parent_sess.native_session_id
        assert parent_native_id == "sess-1"

        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m2"), "reviewer: audit-subagent-marker")
        sub_sess = h.agent._sessions[subagent_key]
        assert sub_sess.client.pid != parent_pid and sub_sess.native_session_id != parent_native_id and sub_sess.spec.rules_dirs != parent_sess.spec.rules_dirs
        assert (sub_sess.current_model, sub_sess.current_effort) == ("model-beta", "high") and parent_sess.client.is_alive and sessions._by_id[parent_row_id]["native_session_id"] == parent_native_id
        assert len(h.launches()) == 2 and "--agent" not in h.launches()[0]["argv"] and h.launches()[1]["argv"][-2:] == ["--agent", "reviewer"]

        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m3"), "reviewer: subagent-followup")
        assert "subagent-followup" in ctrl.events("result")[0][1] and "model=model-beta" in ctrl.events("result")[0][1] and h.agent._sessions[subagent_key].client.pid == sub_sess.client.pid

        ctrl.emitted.clear()
        await handler.handle_user_message(_ctx("m4"), "parent-followup")
        assert "parent-followup" in ctrl.events("result")[0][1] and "model=model-alpha" in ctrl.events("result")[0][1] and h.agent._sessions[parent_key].client.pid == parent_pid and len(h.launches()) == 2

        by_sess: dict[str, list[str]] = {}
        for msg in h.rpcs("session/prompt"):
            by_sess.setdefault(msg["params"]["sessionId"], []).append("\n".join(b.get("text", "") for b in msg["params"]["prompt"] if b.get("type") == "text"))
        parent_p, sub_p = "\n".join(by_sess[parent_native_id]), "\n".join(by_sess[sub_sess.native_session_id])
        assert "parent-secret-marker" in parent_p and "parent-followup" in parent_p and "audit-subagent-marker" not in parent_p
        assert "audit-subagent-marker" in sub_p and "subagent-followup" in sub_p and "parent-secret-marker" not in sub_p


@pytest.mark.asyncio
async def test_combined_fixture_skills_mcp_rules_and_managed_launch(tmp_path: Path) -> None:
    fake_home, workspace = Path.home(), tmp_path / "combined-workspace"
    workspace.mkdir()
    for skill_dir, name, desc, body in (
        (workspace / ".agents" / "skills" / "repo-checks", "repo-checks", "Run repository parity checks", "Run pytest locally."),
        (fake_home / ".gemini" / "antigravity-cli" / "skills" / "agy-helper", "agy-helper", "Antigravity workspace helper", "Check status first."),
    ):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")

    files_bytes = {
        fake_home / ".gemini" / "config" / "mcp_config.json": json.dumps({"mcpServers": {"global_docs": {"command": sys.executable, "args": ["-c", "print('ok')"]}}}).encode(),
        workspace / ".agents" / "mcp_config.json": json.dumps({"mcpServers": {"repo_tools": {"serverUrl": "https://mcp.example.test/sse"}}}).encode(),
        workspace / "AGENTS.md": b"# Project Rules\nPROJECT_RULE_MARKER: keep diffs small.\n",
        fake_home / ".gemini" / "GEMINI.md": b"# Global Rules\nGLOBAL_RULE_MARKER: always verify.\n",
    }
    for path, content in files_bytes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async with make_agent_harness(tmp_path, workdir=workspace, record_rpc=True, name="combined") as h:
        await h.agent.handle_message(make_request(message="recall", workdir=workspace))
        launch = h.launches()[0]
        assert launch["argv"][:3] == ["acp", "--disable-telemetry", "--dangerously-skip-permissions"]
        assert (launch["env"][AGY_CLI_DISABLE_AUTO_UPDATE_ENV], launch["env"][SKILL_WORKING_DIR_ENV], launch["env"][AVIBE_SESSION_ID_ENV]) == ("true", str(workspace), "ses_1")

        new_rpc = h.rpcs("session/new")[0]["params"]
        assert new_rpc["mcpServers"] == [] and len(new_rpc["additionalDirectories"]) >= 1
        rendered = "\n".join((Path(d) / "AGENTS.md").read_text(encoding="utf-8") for d in new_rpc["additionalDirectories"])
        assert "repo-checks" in rendered and "agy-helper" in rendered
        assert {"repo-checks", "agy-helper"} <= {s.name for s in resolve_skills(workspace)}
        assert "Run pytest locally." in (load_skill("repo-checks", workspace).body or "")  # type: ignore[union-attr]
        assert "Check status first." in (load_skill("agy-helper", workspace).body or "")  # type: ignore[union-attr]
        assert mcp_native.read_all()["global_docs"].source == mcp_native.ANTIGRAVITY and mcp_native.read_project(workspace)["repo_tools"].source == mcp_native.ANTIGRAVITY
        assert all(p.read_bytes() == content for p, content in files_bytes.items())
