"""Local after hooks: real scripts, local tools, adapter transcript, and restart recovery.

Only provider replies are scripted; filesystem/command effects and stored results
are real and test-owned. No browser, clipboard, keychain or external account.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from core.agent_core.ai.provider import Done
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import ImageBlock, ToolCallBlock, UserMessage, message_to_dict, text
from core.agent_core.tools.base import CallInstance, JobStatus, ToolContext, ToolResult
from core.agent_core.tools.read import ReadTool
from core.handlers.model_hub.source_filter import HOOK_NAMES, SourceFilterHooks, hook_dir
from modules.agents.model_hub import ModelHubRuntimeRouter
from modules.agents.vibey.source_filter import RECOVERY_MARKER, filter_local_tools
from modules.agents.vibey.tools import ToolSuite, local_tool_suite
from storage.models import messages
from tests.agent_core.fakes import FakeJobHost, FakeTool, assistant
from tests.test_vibey_agent import SESSION, _Harness, _owner_settled, engine, published, session  # noqa: F401
from vibe.i18n import t


@pytest.fixture
def hooks():
    # Keep the same test home as the database fixture and the real dispatcher.
    folder = hook_dir()
    folder.mkdir(parents=True)

    def write(name, body):
        (folder / f"{name}.py").write_text("import json, sys\nreq=json.load(sys.stdin)\n" + textwrap.dedent(body))

    write.folder = folder
    return write


def say(key, **values):
    return t(key, "en", **values)


def context(tmp_path):
    return ToolContext("session", "call", CallInstance("session", "response", 1, "call"), str(tmp_path), {}, CancelToken())


def wrapped(tool, runner=None):
    return filter_local_tools([tool], runner or SourceFilterHooks(), say)[0]


@pytest.mark.asyncio
async def test_read_rewrites_only_the_result_not_the_file_or_arguments(tmp_path, hooks):
    path = tmp_path / "notes.txt"
    path.write_text("first\noriginal\nlast\n")
    hooks("after_read", '''
        assert req['v'] == 1 and req['origin'] == 'tool' and req['tool'] == 'read'
        assert req['args']['offset'] == 2 and 'original' in req['output']['content'][0]['text']
        req['output']['content'][0]['text'] = 'replacement'
        json.dump({'action': 'replace', 'output': req['output']}, sys.stdout)
    ''')
    args = {"path": str(path), "offset": 2, "limit": 1}
    tool = wrapped(ReadTool())
    assert tool.concurrent and tool.spec.name == "read"
    result = await tool.execute(args, context(tmp_path))
    assert result.content == (text("replacement"),) and not result.is_error
    assert path.read_text() == "first\noriginal\nlast\n" and args["offset"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["read", "bash", "powershell"])
@pytest.mark.parametrize("action", ["missing", "pass", "replace", "block", "invalid", "shape", "crash", "respond"])
async def test_after_actions_preserve_execution_facts(tmp_path, hooks, name, action):
    hook = "after_read" if name == "read" else "after_bash"
    scripts = {
        "pass": "json.dump({'action':'pass'}, sys.stdout)",
        "replace": "json.dump({'action':'replace', 'output':{'content':[{'type':'text','text':'changed'}], 'is_error':False}}, sys.stdout)",
        "block": "json.dump({'action':'block', 'reason':'my policy'}, sys.stdout)",
        "invalid": "print('not json')",
        "shape": "json.dump({'action':'replace', 'output':{'content':[{'type':'text','text':4}], 'is_error':False}}, sys.stdout)",
        "crash": "sys.exit(7)",
        "respond": "json.dump({'action':'respond','output':{}}, sys.stdout)",
    }
    if action in scripts:
        hooks(hook, scripts[action])
    seen = []
    original = ToolResult((text("original"),), is_error=True,
                          details={"job_id": "job1", "watch_id": "watch1", "exit_code": 7}, terminate=True)

    async def execute(args, ctx):
        seen.append((dict(args), ctx))
        assert ctx.on_progress is None
        return original

    runner = SourceFilterHooks()
    tool = wrapped(FakeTool(name, execute=execute), runner)
    result = await tool.execute({"command": "unchanged"}, context(tmp_path))
    assert len(seen) == 1 and seen[0][0] == {"command": "unchanged"}
    assert result.details == original.details and result.terminate is True
    if action in {"missing", "pass"}:
        assert result == original
    else:
        assert "original" not in str(result.content)
        assert result.is_error is (action != "replace")
    if action in {"invalid", "shape", "crash", "respond"}:
        assert runner.status()["hooks"][hook]["last_failure"]


@pytest.mark.asyncio
async def test_images_survive_pass_and_text_replacement(tmp_path, hooks):
    image = ImageBlock("image/png", "test-media", "picture.png")
    result = ToolResult((text("header"), image))
    hooks("after_read", '''
        assert req['output']['content'][1]['type'] == 'image'
        req['output']['content'][0]['text'] = 'new header'
        json.dump({'action':'replace','output':req['output']}, sys.stdout)
    ''')

    async def execute(*args):
        return result

    changed = await wrapped(FakeTool("read", execute=execute)).execute({}, context(tmp_path))
    assert changed.content == (text("new header"), image)


@pytest.mark.asyncio
async def test_errors_also_pass_after_and_can_be_replaced(tmp_path, hooks):
    hooks("after_read", '''
        assert req['output']['is_error'] is True
        assert req['args']['path'] == 'missing.txt'
        json.dump({'action':'replace','output':{'content':[{'type':'text','text':'custom result'}], 'is_error':False}}, sys.stdout)
    ''')
    result = await wrapped(ReadTool()).execute({"path": "missing.txt"}, context(tmp_path))
    assert result.content == (text("custom result"),) and not result.is_error


@pytest.mark.asyncio
async def test_tool_exception_is_filtered_not_retried(tmp_path, hooks):
    hooks("after_bash", "assert req['output']['is_error']; json.dump({'action':'block'},sys.stdout)")
    calls = []

    async def execute(*args):
        calls.append(1)
        raise OSError("raw diagnostic")

    result = await wrapped(FakeTool("bash", execute=execute)).execute({}, context(tmp_path))
    assert calls == [1] and "raw diagnostic" not in str(result.content) and result.is_error


def test_disabled_and_unrelated_tools_remain_the_original_instances():
    tools = [FakeTool(name) for name in ("read", "bash", "write", "edit", "web_fetch")]
    assert filter_local_tools(tools, None, say) == tuple(tools)
    assert filter_local_tools(tools, SourceFilterHooks(), say)[2:] == tuple(tools[2:])
    assert HOOK_NAMES == ("before_search", "after_search", "before_fetch", "after_fetch", "after_read", "after_bash")


def test_router_reads_only_the_turns_frozen_switch():
    runner = SourceFilterHooks()
    frozen = {"on": SimpleNamespace(source_filter=True), "off": SimpleNamespace(source_filter=False)}
    router = ModelHubRuntimeRouter(
        service=SimpleNamespace(source_filter_hooks=runner),
        turn_gateway=SimpleNamespace(correlation=SimpleNamespace(turn_hosted_tools=lambda turn: frozen.get(turn))),
    )
    assert router.local_source_filter_hooks("on") is runner
    assert router.local_source_filter_hooks("off") is None
    assert router.local_source_filter_hooks(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read", "bash"])
async def test_adapter_commits_only_filtered_results(engine, session, tmp_path, published, hooks, tool_name):
    file = tmp_path / "source.txt"
    file.write_text("original content\n")
    hook = f"after_{tool_name}"
    hooks(hook, "req['output']['content']=[{'type':'text','text':'filtered content'}]; json.dump({'action':'replace','output':req['output']},sys.stdout)")
    args = ({"path": str(file)} if tool_name == "read" else
            {"command": f'"{sys.executable}" -c "from pathlib import Path; print(Path(\'{file}\').read_text())"'})
    call = ToolCallBlock("local-call", tool_name, args)
    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness = _Harness(engine, tmp_path, "avibe", [
        [Done(assistant(calls=(call,)))], [Done(assistant("done"))],
    ], suite=suite)
    harness.controller.model_hub_runtime.local_source_filter_hooks = lambda turn: SourceFilterHooks()
    await harness.agent.handle_message(harness.request("read it"))
    results = [entry for entry in await harness.context_rows() if entry.kind == "tool_result"]
    assert len(results) == 1 and results[0].message.content == (text("filtered content"),)
    request_text = json.dumps([message_to_dict(message) for message in harness.provider.requests[-1].messages])
    assert "filtered content" in request_text and "original content" not in request_text
    with engine.connect() as conn:
        rows = conn.execute(select(messages.c.metadata_json).where(messages.c.id.in_(
            [entry.row_id for entry in await harness.context_rows() if entry.kind == "response"]
        ))).scalars().all()
    assert rows and all(json.loads(row)[RECOVERY_MARKER] is True for row in rows)
    assert file.read_text() == "original content\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [True, False])
async def test_restart_recovery_never_releases_a_filtered_calls_raw_output(
    engine, session, tmp_path, published, filtered,
):
    jobs = FakeJobHost()

    async def render(*args):
        return ToolResult((text("original content"),), details={"exit_code": 7})

    suite = ToolSuite(jobs=jobs, create_tools=lambda *a: [], render_recovered=render, find_job=lambda call: "job_1")
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    request = harness.request("command")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("command"),)),
    )
    response = await harness.agent.store.append_response(
        SESSION, assistant(calls=(ToolCallBlock("call", "bash", {"command": "original command"}),)), final=False,
    )
    with engine.begin() as conn:
        raw = json.loads(conn.execute(select(messages.c.metadata_json).where(messages.c.id == response.row_id)).scalar())
        raw[RECOVERY_MARKER] = filtered
        conn.execute(update(messages).where(messages.c.id == response.row_id).values(metadata_json=json.dumps(raw)))
    jobs.states["job_1"] = JobStatus("exited", exit_code=7)
    _owner_settled(engine, request)
    harness.new_agent()  # loses in-memory policy; the current setting is off.
    await harness.agent.recover_runtime_state()
    await harness.agent.recover_runtime_state()  # idempotent, never reruns a hook/model/command.
    results = [entry for entry in await harness.context_rows() if entry.kind == "tool_result"]
    assert len(results) == 1 and not harness.provider.requests
    assert ("original content" not in str(results[0].message.content)) is filtered
    assert results[0].message.is_error is filtered


@pytest.mark.asyncio
async def test_cancellation_and_timeout_do_not_return_the_original(tmp_path, hooks):
    hooks("after_read", "import pathlib,time; pathlib.Path('ready').touch(); time.sleep(30)")
    runner = SourceFilterHooks(timeout=0.1)
    result = await wrapped(ReadTool(), runner).execute({"path": "missing.txt"}, context(tmp_path))
    assert result.is_error and "timed out" in str(result.content)
    runner.timeout = 30
    (hooks.folder / "ready").unlink(missing_ok=True)
    task = asyncio.create_task(wrapped(ReadTool(), runner).execute({"path": "missing.txt"}, context(tmp_path)))
    try:
        async def ready():
            while not (hooks.folder / "ready").exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_bash_handover_filters_once_without_losing_watch_ownership(tmp_path, hooks):
    from core.agent_core.tools.bash import BashTool
    from core.agent_core.tools.jobs import LocalJobHost

    handed = []

    async def hand_over(meta):
        handed.append(meta["job_id"])
        return "watch-owned"

    host = LocalJobHost(str(tmp_path / "jobs"), on_hand_over=hand_over)
    hooks("after_bash", '''
        import pathlib
        assert 'Watch' in req['output']['content'][0]['text']
        with pathlib.Path('runs').open('a') as f: f.write('once!')
        req['output']['content']=[{'type':'text','text':'filtered handover'}]
        json.dump({'action':'replace','output':req['output']},sys.stdout)
    ''')
    tool = wrapped(BashTool(host, foreground_window_s=0.05))
    try:
        result = await tool.execute({"command": f'"{sys.executable}" -c "import time; print(123,flush=True); time.sleep(10)"', "watch": True}, context(tmp_path))
        assert len(handed) == 1 and result.details["watch_id"] == "watch-owned"
        assert result.content == (text("filtered handover"),)
        assert (hooks.folder / "runs").read_text() == "once!"
    finally:
        for job_id in handed:
            await host.kill(job_id)


@pytest.mark.asyncio
async def test_stop_during_after_bash_recovers_without_raw_output_or_command_rerun(
    engine, session, tmp_path, published, hooks,
):
    effects = tmp_path / "effects"
    hooks("after_bash", "import pathlib,time; pathlib.Path('ready').touch(); time.sleep(30)")
    call = ToolCallBlock("local-call", "bash", {"command":
        f'"{sys.executable}" -c "from pathlib import Path; Path(\'{effects}\').write_text(\'one execution\'); print(\'raw output\')"'})
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant(calls=(call,)))]],
                       suite=local_tool_suite(str(tmp_path / "jobs")))
    harness.controller.model_hub_runtime.local_source_filter_hooks = lambda turn: SourceFilterHooks()
    request = harness.request("run command")
    task = asyncio.create_task(harness.agent.handle_message(request))
    try:
        async def ready():
            while not (hooks.folder / "ready").exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), 10)
        assert await harness.agent.handle_stop(request)
        await asyncio.wait_for(task, 10)
        results = [entry for entry in await harness.context_rows() if entry.kind == "tool_result"]
        assert len(results) == 1 and results[0].message.is_error
        assert "raw output" not in str(results[0].message.content)
        assert "withheld" in str(results[0].message.content)
        assert effects.read_text() == "one execution"
        assert len(harness.provider.requests) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
