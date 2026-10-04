"""C-9 context management in the Avibe Agent (``agent-core-contracts/context.md`` section 9).

Through the adapter's real boundaries (the transcript store on a temporary SQLite database, the Delivery rows, and
the shared dispatcher), with a scripted provider: the limits come from the Model Hub model definition; a checkpoint
carries the adapter's lookup command and state, and the user sees nothing of it; the pause is silent, and a context
that cannot fit ends the Turn with the one notice; Model Hub hears only of failures the served source produced; the
first input after a checkpoint carries every environment field the context no longer shows. The host's parts are
checked against real rows and the real read-only query guard.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.agent_core.ai.provider import Done, ModelCapabilities, ProviderError
from core.agent_core.harness.context import SkillRef, StateRequest, state_cap, text_tokens
from core.agent_core.messages import ToolCallBlock, UserMessage, text
from core.agent_core.tools.base import ToolResult
from core.managed_skills import ManagedSkill
from modules.agents.avibe.context import AvibeContextHost, SkillScope, mark_skill_loads
from modules.agents.avibe.prompt import current_environment
from storage.read_only_query import run_read_only_query
from tests.agent_core.fakes import FakeTool, assistant
from tests.test_avibe_agent import (  # noqa: F401 (fixtures)
    SESSION,
    _SCOPES,
    _Harness,
    _insert_session,
    engine,
    published,
    session,
)
from vibe.i18n import t as i18n_t

CHECKPOINT = "# 1. Self and method\n- the checkpoint"


def tokens(count: int) -> str:
    return "x" * (4 * count)


def is_checkpoint(request) -> bool:
    last = request.messages[-1]
    return isinstance(last, UserMessage) and (last.content[0].text or "").startswith("<context-checkpoint-request>")


async def _turn(harness: _Harness, body: str) -> None:
    await harness.agent.handle_message(harness.request(body))


def _texts(harness: _Harness, message_type: str) -> list[str]:
    return [row["content_text"] for row in harness.rows(message_type)]


# --- the limits ---------------------------------------------------------------------------------------


async def test_a_turn_runs_with_context_management_on_the_model_hub_limits(engine, session, tmp_path):
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok"))]])
    harness.controller.hub_limits = {"context_window": 200_000, "max_output_tokens": 16_000}
    await _turn(harness, "hello")
    # O is the hop's own maximum from the Model Hub model definition, not the engine's 8,192 default.
    assert harness.provider.requests[0].max_tokens == 16_000
    response = [row for row in await harness.context_rows() if row.kind == "response"][0]
    assert response.payload["request"]["tokens"] > 0  # context management is on: the anchor's facts are stored


@pytest.mark.parametrize(
    "window,maximum,asked", [(262_144, 262_144, 65_536), (8_000, None, 2_000)], ids=["maximum fills", "small window"]
)
async def test_a_route_whose_output_maximum_fills_the_window_still_answers(
    engine, session, tmp_path, window, maximum, asked
):
    # Many Model Hub definitions list an output maximum as large as the window (models.dev), and a small manual route
    # may list none: asking for all of it, or for the 8,192 default, would leave no room for the context. The Agent
    # asks for at most a quarter of any window.
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("hi"))]])
    harness.controller.hub_limits = {"context_window": window, "max_output_tokens": maximum}
    await _turn(harness, "hello")
    assert _texts(harness, "result") == ["hi"]
    assert harness.provider.requests[0].max_tokens == asked


# --- a checkpoint ---------------------------------------------------------------------------------------


async def test_a_threshold_checkpoint_carries_the_lookup_and_the_state_and_shows_nothing(
    engine, session, tmp_path, published
):
    scripts = [[Done(assistant("noted"))], [Done(assistant(CHECKPOINT))], [Done(assistant("done"))]]
    harness = _Harness(engine, tmp_path, "avibe", scripts)
    await _turn(harness, tokens(11_000))
    await _turn(harness, tokens(9_000))
    assert is_checkpoint(harness.provider.requests[1])
    compaction = [row for row in await harness.context_rows() if row.kind == "compaction"][0]
    through = compaction.payload["summarized_to_seq"]
    assert f"session_id = '{SESSION}' and context_seq <= {through}" in compaction.payload["summary"]
    # Inside a run, the checkpoint carries the full environment block.
    assert any(item.startswith("<environment>") for item in compaction.payload["state"])
    # Silent: the user sees the two replies and nothing about the compaction.
    assert _texts(harness, "result") == ["noted", "done"] and not harness.rows("notify")


async def test_the_environment_survives_a_checkpoint_that_splits_the_turn(engine, session, tmp_path):
    # The Turn's input carries its environment block; a checkpoint that splits the Turn copies that model-facing
    # input into <current-request>, so the rest of the Turn still has it.
    def read(call_id: str) -> list:
        return [Done(assistant(calls=[ToolCallBlock(call_id, "read", {"path": "a.py"})]))]

    scripts = [read("r1"), read("r2"), [Done(assistant(CHECKPOINT))], [Done(assistant("done"))]]
    reader = FakeTool("read", result=ToolResult((text(tokens(12_000)),)))
    harness = _Harness(engine, tmp_path, "avibe", scripts, tools=[reader])
    await _turn(harness, "hello")
    (compaction,) = [row for row in await harness.context_rows() if row.kind == "compaction"]
    current = compaction.payload["current_request"]
    assert current.startswith("<environment>") and "\ncwd: " in current and current.endswith("hello")
    assert f"<current-request>\n{current}\n</current-request>" in compaction.payload["summary"]
    assert _texts(harness, "result") == ["done"]


async def test_the_first_input_after_a_checkpoint_carries_every_field_the_context_no_longer_shows(engine, session, tmp_path):
    scripts = [
        [Done(assistant("noted"))],
        [Done(assistant(CHECKPOINT))],
        [Done(assistant("done"))],
        [Done(assistant("again"))],
    ]
    harness = _Harness(engine, tmp_path, "avibe", scripts)
    await _turn(harness, tokens(11_000))
    await _turn(harness, tokens(9_000))
    await _turn(harness, "next")
    inputs = [row.message for row in await harness.context_rows() if row.kind == "input"]
    second, third = inputs[-2], inputs[-1]
    # Nothing changed for the second input; the third follows a checkpoint that summarized the first block.
    assert not (second.content[0].text or "").startswith("<environment>")
    block = third.content[0].text or ""
    assert block.startswith("<environment>") and "\ncwd: " in block and "\nos: " in block


async def test_each_resolved_hop_gets_its_own_output_budget(engine, session, tmp_path):
    harness = None

    async def busy_then_fallback(request, cancel):
        # The retry resolves again, to a fallback whose output maximum equals its 62,000-token window.
        harness.controller.hub_limits = {"context_window": 62_000, "max_output_tokens": 62_000}
        yield ProviderError("rate_limit", "busy", True)

    harness = _Harness(engine, tmp_path, "avibe", [busy_then_fallback, [Done(assistant("ok"))]])
    harness.controller.hub_limits = {"context_window": 200_000, "max_output_tokens": 16_000}
    await _turn(harness, "hello")
    assert [request.max_tokens for request in harness.provider.requests] == [16_000, 15_500]
    assert _texts(harness, "result") == ["ok"]


async def test_a_compaction_refreshes_the_session_token_snapshot(engine, session, tmp_path):
    scripts = [[Done(assistant("noted"))], [Done(assistant(CHECKPOINT))], [Done(assistant("done"))]]
    harness = _Harness(engine, tmp_path, "avibe", scripts)
    noted: list[int] = []
    harness.controller.note_session_tokens = lambda context, *, total: noted.append(total)
    await _turn(harness, tokens(11_000))
    await _turn(harness, tokens(9_000))
    compaction = [row for row in await harness.context_rows() if row.kind == "compaction"][0]
    assert noted == [compaction.payload["tokens_after_estimate"]]


async def test_a_skill_load_through_bash_is_marked_in_its_context_row(engine, session, tmp_path, published):
    loaded = '<skill_content name="parser" directory="/skills/parser">\nParse it.\n</skill_content>'
    bash = FakeTool("bash", result=ToolResult((text(loaded),)))
    call = ToolCallBlock(id="call_skill", name="bash", arguments={"command": "vibe skill load -- parser"})
    scripts = [[Done(assistant("", calls=(call,)))], [Done(assistant("loaded"))]]
    harness = _Harness(engine, tmp_path, "avibe", scripts, tools=[bash])
    await _turn(harness, "load the parser skill")
    (result,) = [row for row in await harness.context_rows() if row.kind == "tool_result"]
    # Clearing spares it and a checkpoint carries it (C-9 sections 4 and 7).
    assert result.payload["details"]["skills"] == [{"name": "parser"}]


async def test_a_context_that_cannot_fit_locally_is_never_reported_as_a_source_failure(engine, session, tmp_path):
    reported: list[str] = []

    async def record_native_failure(context, diagnostic) -> bool:
        reported.append(diagnostic)
        return False

    harness = _Harness(engine, tmp_path, "avibe", [])
    harness.controller.model_hub_runtime.record_native_failure = record_native_failure
    await _turn(harness, tokens(30_000))
    assert not harness.provider.requests and not reported  # stopped before the provider: no source was involved
    assert harness.controller.terminals[-1]["is_error"] is True
    # The one user-visible compaction text: the conversation has grown too long; start a new session.
    assert _texts(harness, "notify") == [f"❌ {i18n_t('avibeAgent.error.contextExhausted', 'en')}"]


async def _failing_commit(harness: _Harness) -> None:
    async def append_payloads(session_id, entries):
        raise RuntimeError("database is locked")

    harness.agent.store.append_payloads = append_payloads


class _ClosingProvider:
    """Each stream answers one terminal; a stream marked failing raises when the loop closes it (a diagnostic)."""

    protocol = "anthropic"

    def __init__(self, *streams) -> None:
        self.streams = list(streams)

    def stream(self, request, cancel):
        terminal, failing = self.streams.pop(0)

        class Stream:
            done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return terminal

            async def aclose(self):
                if failing:
                    raise OSError("stream close failed")

        return Stream()


_CUT_CALL = [Done(assistant(calls=[ToolCallBlock("cut", "bash", {"command": "ls"})], stop_reason="length"))]


@pytest.mark.parametrize(
    "case,recorded",
    [
        ("provider server error", 1),
        ("store failure after a good checkpoint", 0),
        ("provider refused", 1),
        ("tool call cut by the output limit past the retries", 1),
        ("tool calls under a stop", 1),
        ("a cleanup diagnostic before a provider error", 1),
    ],
)
async def test_model_hub_hears_only_of_failures_the_served_source_produced(engine, session, tmp_path, case, recorded):
    reported: list[str] = []

    async def record_native_failure(context, diagnostic) -> bool:
        reported.append(diagnostic)
        return False

    if case == "provider server error":
        scripts = [[ProviderError("server", "upstream failed", False)]]
    elif case == "provider refused":
        scripts = [[Done(assistant("", stop_reason="refusal"))]]
    elif case == "tool call cut by the output limit past the retries":
        scripts = [_CUT_CALL] * 10
    elif case == "tool calls under a stop":
        scripts = [[Done(assistant(calls=[ToolCallBlock("call", "bash", {"command": "ls"})], stop_reason="stop"))]]
    else:
        scripts = [[Done(assistant("noted"))], [Done(assistant(CHECKPOINT))]]
    providers = None
    if case == "a cleanup diagnostic before a provider error":
        provider = _ClosingProvider(
            [Done(assistant(calls=[ToolCallBlock("call", "bash", {"command": "true"})])), True],
            [ProviderError("server", "upstream failed", False), False],
        )
        providers = lambda protocol: provider  # noqa: E731
    harness = _Harness(engine, tmp_path, "avibe", scripts, providers=providers)
    harness.controller.model_hub_runtime.record_native_failure = record_native_failure
    if case == "store failure after a good checkpoint":
        await _turn(harness, tokens(11_000))
        await _failing_commit(harness)
        await _turn(harness, tokens(9_000))
    else:
        await _turn(harness, "hello")
    assert len(reported) == recorded and harness.controller.terminals[-1]["is_error"] is True


# --- what the user is told -------------------------------------------------------------------------------


async def test_a_pause_is_silent_and_a_context_that_then_cannot_fit_ends_with_the_one_notice(engine, session, tmp_path):
    failing = [Done(assistant(CHECKPOINT, stop_reason="length"))]
    scripts = [
        [Done(assistant("one"))],
        failing,
        [Done(assistant("two"))],
        failing,
        [Done(assistant("three"))],
        failing,
        [Done(assistant("four"))],
    ]
    harness = _Harness(engine, tmp_path, "avibe", scripts, language="zh")
    for body in (tokens(10_000), tokens(10_000), "a", "b"):
        await _turn(harness, body)
    # Three failed checkpoints pause auto-compaction, and the user is told nothing.
    assert _texts(harness, "result") == ["one", "two", "three", "four"] and not harness.rows("notify")
    sent = len(harness.provider.requests)
    await _turn(harness, tokens(8_000))
    # Paused, nothing compacts: a request that cannot be sent ends the Turn with the one notice.
    assert len(harness.provider.requests) == sent
    assert not [row for row in await harness.context_rows() if row.kind == "compaction"]
    assert _texts(harness, "notify") == [f"❌ {i18n_t('avibeAgent.error.contextExhausted', 'zh')}"]


# --- the host's parts ------------------------------------------------------------------------------------


async def test_the_lookup_command_reads_the_summarized_rows_of_the_session_and_its_fork_source(
    engine, session, tmp_path
):
    scripts = [[Done(assistant("first reply"))], [Done(assistant("second reply"))], [Done(assistant("child reply"))]]
    harness = _Harness(engine, tmp_path, "avibe", scripts)
    await _turn(harness, "parser keyword one")
    await _turn(harness, "parser keyword two")
    rows = await harness.context_rows()
    anchor = [row for row in rows if row.kind == "response"][0]  # the fork inherits through the first reply
    with engine.begin() as conn:
        metadata = {
            "created_via": "session_fork",
            "fork_source_session_id": SESSION,
            "fork_source_message_id": anchor.row_id,
            "fork_source_context_seq": anchor.context_seq,
        }
        _insert_session(conn, "ses_child", _SCOPES["avibe"], metadata)
    child = _Harness(engine, tmp_path, "avibe", [[Done(assistant("child reply"))]], session_id="ses_child")
    await _turn(child, "parser keyword child")
    child_rows = await child.context_rows()
    host = AvibeContextHost(engine, environment=lambda _: {}, skills=lambda _: None)
    command = host.earlier_record("ses_child", child_rows[-1].context_seq)
    assert command.startswith('vibe data query --limit 100 --sql "') and command.endswith('"')
    sql = command[len('vibe data query --limit 100 --sql "') : -1]
    found = run_read_only_query(sql.replace("KEYWORD", "parser keyword"), page_request=None).rows
    texts = [row["text"] for row in found]
    # The source's rows only through the fork anchor, then the child's own: never the source's later turn.
    assert any("parser keyword one" in text for text in texts) and any("parser keyword child" in text for text in texts)
    assert not any("parser keyword two" in text for text in texts)
    # It searches what the model read, not the display text: the environment block lives only in the model input.
    found = run_read_only_query(sql.replace("KEYWORD", "cwd: "), page_request=None).rows
    assert found and all(row["type"] == "user" and "<environment>" in row["text"] for row in found)


class _Skills(SkillScope):
    def __init__(self, skills: dict[str, ManagedSkill]) -> None:
        super().__init__(cwd=None)
        object.__setattr__(self, "_found", skills)
        object.__setattr__(self, "snapshots", 0)
        object.__setattr__(self, "loads", [])

    def catalog(self):
        object.__setattr__(self, "snapshots", self.snapshots + 1)
        return self._found

    def load(self, name, catalog=None):
        self.loads.append(name)
        return (catalog if catalog is not None else self._found).get(name)


def _skill(name: str, body: str) -> ManagedSkill:
    return ManagedSkill(name=name, description=f"{name} skill", directory=Path(f"/skills/{name}"), priority=(0,), body=body)


async def test_the_state_carries_skills_and_the_core_environment_fields(engine, session):
    many = {f"big{index}": _skill(f"big{index}", tokens(6_000)) for index in range(6)}
    skills = _Skills({"parser": _skill("parser", "Parse it.\n"), "huge": _skill("huge", tokens(9_000)), **many})
    fields = {"cwd": "/work", "os": "macOS", "watches": 'wd_1 "CI" command running'}
    host = AvibeContextHost(engine, environment=lambda _: fields, skills=lambda _: skills)
    refs = (SkillRef("parser"), SkillRef("huge"), SkillRef("gone"))
    environment, parser, huge, gone = await host.render_state(StateRequest(SESSION, refs, 25_000))
    # The environment's core fields only: pending work is the model's "Waiting on", not state.
    assert environment.startswith("<environment>") and "\ncwd: /work" in environment and "watches" not in environment
    assert parser.startswith('<skill_content name="parser" revision="') and "Parse it." in parser
    assert text_tokens(huge) <= 5_000 and "vibe skill load -- huge" in huge  # cut, saying how to get all of it
    assert gone.startswith('<skill-unavailable name="gone">')
    # Past the cap, the remaining skills are named, not carried, and not even loaded; the catalog is resolved once
    # per checkpoint, however many skills it carries.
    skills.loads.clear()
    capped = await host.render_state(StateRequest(SESSION, tuple(SkillRef(name) for name in many), 25_000))
    assert sum(text_tokens(item) for item in capped) <= 25_000
    assert capped[-1].startswith('<left-out of="skills">') and "big5" in capped[-1]
    assert skills.snapshots == 2 and "big5" not in skills.loads
    (only,) = await host.render_state(StateRequest(SESSION, (), 25_000))
    assert only.startswith("<environment>")


@pytest.mark.parametrize("window", [8_000, 200_000], ids=["8K window", "200K window"])
async def test_the_state_fits_the_state_cap_of_the_route(engine, session, window):
    # A long Session can carry hundreds of skills and run in a deeply nested directory. On any route the
    # environment's core fields are bounded by construction (the cwd cut in the middle to 160 characters), and the
    # skill bodies, unavailable notices, and the names left out fit inside the route's cap.
    found = {f"skill-{index:03}": _skill(f"skill-{index:03}", tokens(3_000)) for index in range(0, 400, 2)}
    deep = "/work/" + "nested/" * 460  # about 3,200 characters
    skills = _Skills(found)
    host = AvibeContextHost(engine, environment=lambda _: current_environment(deep, []), skills=lambda _: skills)
    refs = tuple(SkillRef(f"skill-{index:03}") for index in range(400))
    cap = state_cap(ModelCapabilities(context_window=window))
    state = await host.render_state(StateRequest(SESSION, refs, cap))
    assert sum(text_tokens(item) for item in state) <= cap
    environment = state[0]
    assert all(f"\n{field}: " in environment for field in ("cwd", "os", "shell")) and text_tokens(environment) <= 100
    cwd = next(line for line in environment.splitlines() if line.startswith("cwd: "))
    assert len(cwd) == len("cwd: ") + 160 and "…" in cwd
    skills_left_out = state[-1]
    assert skills_left_out.startswith('<left-out of="skills">') and skills_left_out.count("skill-") == 20
    assert re.search(r", and \d+ more\.", skills_left_out)


async def test_every_skill_a_successful_bash_result_loaded_is_recorded():
    # The record is what `vibe skill load` wrote into the result: a compound command, quoting, and a path to `vibe`
    # do not matter. A failed command, or one that loaded nothing, records nothing.
    def loaded(*names: str) -> ToolResult:
        blocks = (f'<skill_content name="{name}" directory="/s/{name}">\nBody.\n</skill_content>' for name in names)
        return ToolResult((text("\n".join(blocks)),))

    outputs = {
        "vibe skill load -- parser && vibe skill load -- 'lint'": loaded("parser", "lint"),
        '/usr/local/bin/vibe skill load "parser"': loaded("parser"),
        "vibe skill load -- missing": ToolResult((text("Skill not found: missing"),), is_error=True),
        "ls": ToolResult((text("a b"),)),
    }

    async def run(arguments, ctx):
        return outputs[arguments["command"]]

    (marked,) = mark_skill_loads([FakeTool("bash", execute=run)])
    assert marked.spec.name == "bash"
    compound = await marked.execute({"command": "vibe skill load -- parser && vibe skill load -- 'lint'"}, None)
    assert compound.details["skills"] == [{"name": "parser"}, {"name": "lint"}]
    path = await marked.execute({"command": '/usr/local/bin/vibe skill load "parser"'}, None)
    assert path.details["skills"] == [{"name": "parser"}]
    for command in ("vibe skill load -- missing", "ls"):
        assert "skills" not in (await marked.execute({"command": command}, None)).details
    # Only the outer blocks: an example tag inside a skill's body is not a load.
    example = '<skill_content name="review" directory="/s/review">\nReview.\n</skill_content>'
    nested = f'<skill_content name="writer" directory="/s/writer">\nShow it like this:\n{example}\n</skill_content>'
    outputs["vibe skill load -- writer"] = ToolResult((text(nested),))
    result = await marked.execute({"command": "vibe skill load -- writer"}, None)
    assert result.details["skills"] == [{"name": "writer"}]


# --- end to end, through the real provider adapter ------------------------------------------------------------


def _sse(text_value: str) -> str:
    delta = json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text_value}})
    return (
        'data: {"type":"message_start","message":{"usage":{}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        f"data: {delta}\n\n"
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )


async def test_a_threshold_checkpoint_and_an_overflow_roll_through_the_real_loop_and_wire(
    engine, session, tmp_path, published
):
    """Anthropic over a MockTransport gateway: a Turn crosses T and checkpoints by fork; a later Turn's request is
    refused as overflow, its normal checkpoint request is refused too, and the ladder rolls; the user sees only the
    replies."""
    import httpx

    from modules.agents.avibe.models import registry_providers

    served = json.dumps({"provider": "upstream", "api": "anthropic", "model": "served-model"}, separators=(",", ":"))
    overflow = json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 40000 tokens > 32000 maximum"}}
    )
    phase = {"turn": 0, "seen": []}
    refusals = {3: ["conversation", "checkpoint"]}  # the third Turn: its request, then its normal checkpoint
    replies = {1: "noted", 2: "done", 3: "after the roll"}

    def gateway(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = "checkpoint" if "<context-checkpoint-request>" in json.dumps(body["messages"][-1]) else "conversation"
        phase["seen"].append((phase["turn"], kind))
        pending = refusals.get(phase["turn"], [])
        if pending and pending[0] == kind:
            pending.pop(0)
            return httpx.Response(400, headers={"content-type": "application/json"}, text=overflow)
        answer = CHECKPOINT if kind == "checkpoint" else replies[phase["turn"]]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-avibe-served-hop": served}, text=_sse(answer)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        harness = _Harness(engine, tmp_path, "avibe", [], providers=registry_providers(media_loader=None, client=client))
        for turn, body in ((1, tokens(11_000)), (2, tokens(9_000)), (3, "and now?")):
            phase["turn"] = turn
            await _turn(harness, body)

    compactions = [row.payload for row in await harness.context_rows() if row.kind == "compaction"]
    assert [(item["mode"], item["reason"]) for item in compactions] == [("normal", "threshold"), ("rolling", "overflow")]
    assert phase["seen"] == [
        (1, "conversation"),
        (2, "checkpoint"),
        (2, "conversation"),
        (3, "conversation"),  # refused as overflow
        (3, "checkpoint"),  # the normal checkpoint request, refused as overflow
        (3, "checkpoint"),  # the rolling one
        (3, "conversation"),
    ]
    assert _texts(harness, "result") == ["noted", "done", "after the roll"] and not harness.rows("notify")
