"""Avibe Agent backend adapter (``modules/agents/avibe``) over Avibe's real rows.

Properties, each through the adapter's real boundaries (the transcript store on
a temporary SQLite database, the Delivery rows, and the shared outbound
dispatcher), with a scripted provider and test-owned tools:

* a Workbench Turn and an IM Turn commit the same context rows and show the
  same reply once, and every request equals the context rebuilt from the
  tables (A10);
* a P1 steer enters the running loop after the tool batch, even when its row
  appears after the steer was accepted;
* Stop aborts the run and settles the Turn as stopped;
* a committed row whose delivery a crash interrupted is re-delivered once on
  Workbench, and part by part on IM (recovery.md D1/D2);
* resume settles open tool calls and admits accepted, unconsumed inputs before
  the next model call, never running the model on its own (T2, T3, T4);
* failures show localized copy in English and Chinese.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import select, update

from core.agent_core.agent.recovery import UNRECORDED_EFFECT
from core.agent_core.ai.provider import Done
from core.agent_core.harness.projection import project
from core.agent_core.messages import AssistantMessage, TextBlock, ToolCallBlock, UserMessage, text
from core.agent_core.tools.base import JobStatus, ToolResult
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerRequest
from modules.agents.avibe import AvibeAgent
from modules.agents.avibe.errors import _KIND_KEYS, error_key
from modules.agents.avibe.tools import ToolSuite
from modules.agents.base import AgentRequest
from modules.agents.model_hub import ModelHubLaunch
from modules.im import MessageContext
from storage import message_deliveries
from storage.agent_transcript import resolve_fork_anchor_seq
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import messages, session_turns
from storage.settings_service import upsert_scope
from tests.agent_core.fakes import ORIGIN, FakeJobHost, FakeTool, ScriptedProvider, assistant
from vibe.i18n import t as i18n_t

NOW = "2026-10-02T00:00:00.000000Z"
SESSION = "ses_avibe"


@pytest.fixture()
def engine():
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def published(monkeypatch) -> list[tuple[str, dict]]:
    from core.inbox_events import bus

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(bus, "publish", lambda name, payload: events.append((name, payload)))
    return events


class _Formatter:
    def format_toolcall(self, name: str, arguments: Optional[dict] = None) -> str:
        return f"🔧 {name} {json.dumps(arguments or {}, sort_keys=True)}"

    def format_toolcall_label(self, name: str, arguments: Optional[dict] = None) -> str:
        return f"🔧 {name}"

    def format_result_footer(self, subtype: str, *_args: Any, **_kwargs: Any) -> str:
        return "❌ failed" if subtype == "error" else "✅ done"


class _IMClient:
    """Records every send; ``fail_sends`` names 1-based send attempts the platform rejects.

    ``routes`` records ``(channel, route, text)``: ``reply`` for the result path
    (native Markdown sender), ``message`` for a plain send such as the process log.
    """

    def __init__(self) -> None:
        self.formatter = _Formatter()
        self.sent: list[str] = []
        self.routes: list[tuple[str, str, str]] = []
        self.uploads: list[str] = []
        self.attempts = 0
        self.fail_sends: set[int] = set()
        self.fail_uploads = 0
        # 1-based attempts whose send "succeeds" without evidence, and uploads that return this.
        self.send_results: dict[int, Any] = {}
        self.upload_results: list[Any] = []

    def should_use_thread_for_reply(self) -> bool:
        return False

    def supports_message_editing(self, context=None) -> bool:
        return context is not None and context.platform != "wechat"

    async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None, *, _route="message"):
        self.attempts += 1
        if self.attempts in self.fail_sends:
            raise RuntimeError("the platform rejected this part")
        if self.attempts in self.send_results:
            return self.send_results[self.attempts]
        self.sent.append(text)
        self.routes.append((context.channel_id, _route, text))
        return f"im-{self.attempts}"

    async def send_markdown_message(self, context, text, keyboard=None, subtext=None):
        return await self.send_message(context, text, subtext=subtext, _route="reply")

    async def upload_file_from_path(self, context, file_path, title=None):
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError("the platform rejected the upload")
        if self.upload_results:
            return self.upload_results.pop(0)
        self.uploads.append(file_path)
        return f"file-{len(self.uploads)}"

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None, subtext=None):
        return await self.send_message(context, text, parse_mode=parse_mode)

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None, subtext=None):
        return True


class _SettingsManager:
    def __init__(self) -> None:
        self.hidden: set[str] = set()

    def _canonicalize_message_type(self, message_type: str) -> str:
        return message_type

    def is_message_type_hidden(self, settings_key: str, canonical_type: str) -> bool:
        return canonical_type in self.hidden


class _Controller:
    """The controller surface the adapter and the shared dispatcher use, over real rows."""

    def __init__(self, engine, platform: str, *, language: str = "en") -> None:
        self.engine = engine
        self.platform = platform
        self.config = SimpleNamespace(
            platform=platform,
            language=language,
            reply_enhancements=True,
            show_duration=False,
            include_time_info=False,
            include_user_info=False,
        )
        self.im_client = _IMClient()
        self.settings_manager = _SettingsManager()
        self.sessions = SimpleNamespace(bind_agent_session_by_id=lambda session_id, *_args, **_kwargs: session_id)
        self.session_handler = SimpleNamespace(finalize_scheduled_delivery=lambda *_args: None)
        self.terminals: list[dict] = []
        self.terminal_texts: list[Optional[str]] = []
        self.released: list[str] = []
        self.started: list[Optional[str]] = []
        self.hub_calls: list[str] = []
        self.agent: Optional[AvibeAgent] = None
        self.agent_service = SimpleNamespace(
            mark_runtime_turn_started=self._native_start,
            release_runtime_turn=lambda context, **_kw: self.released.append(_turn(context)),
        )
        self.session_turns = SimpleNamespace(
            on_terminal_result=self._terminal,
            on_terminal_delivery_complete=lambda _context: None,
            model_hub_turn_id_for_task=lambda: None,
            _build_context=lambda session_id: _context(platform, session_id, turn_id="redelivery", delivery_id=""),
        )
        self.model_hub_runtime = SimpleNamespace(resolve=self._resolve)
        self.message_dispatcher = ConsolidatedMessageDispatcher(self)

    # --- controller ---------------------------------------------------------

    def get_settings_manager_for_context(self, context):
        return self.settings_manager

    def get_im_client_for_context(self, context):
        return self.im_client

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{context.platform}::{context.channel_id}"

    async def emit_agent_message(self, context, message_type, text, parse_mode="markdown", **kwargs):
        return await self.message_dispatcher.emit_agent_message(context, message_type, text, parse_mode, **kwargs)

    # --- collaborators --------------------------------------------------------

    def _terminal(self, context, *, is_error, settled_by, terminal_evidence) -> None:
        self.terminals.append({"turn": _turn(context), "is_error": is_error, "settled_by": settled_by})
        self.terminal_texts.append(terminal_evidence.get("result_text"))

    async def _resolve(self, backend, requested_model, *, process_scope=None, turn_id=None) -> ModelHubLaunch:
        self.hub_calls.append(requested_model)
        return ModelHubLaunch(
            backend=backend,
            channel="hub",
            requested_model=requested_model,
            target_model=requested_model,
            runtime_model=requested_model,
            source_id="src_test",
            gateway_base_url="http://hub.invalid/avibe",
            gateway_token="hub-token",
            context_window=32000,
            max_output_tokens=4096,
            supports_tools=True,
            protocol="anthropic",
            provider="test-provider",
            supports_images=True,
        )

    def _native_start(self, context, *, activation_identity=None) -> None:
        """The Turn owner's native start: bind the steer identity, materialize the input row."""
        turn_id = _turn(context)
        target = ActiveSteerTarget("runtime", turn_id, context, None, self.agent)
        native = self.agent.steering_native_turn_id(target)
        self.started.append(native)
        with self.engine.begin() as conn:
            turn = message_deliveries.get_turn(conn, turn_id)
            assert message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(turn["version"]),
                runtime_key="runtime",
                runtime_turn_id=turn_id,
                native_turn_id=native,
            )
            assert message_deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test"})


def _turn(context) -> str:
    return str((context.platform_specific or {}).get("turn_token") or "")


def _context(platform: str, session_id: str, *, turn_id: str, delivery_id: str) -> MessageContext:
    return MessageContext(
        user_id="u1",
        channel_id=session_id if platform == "avibe" else "C1",
        platform=platform,
        platform_specific={
            "agent_session_id": session_id,
            "agent_session_target": {"id": session_id, "agent_backend": "avibe"},
            "workbench_session_id": session_id if platform == "avibe" else None,
            "platform": platform,
            "turn_token": turn_id,
            "delivery_id": delivery_id,
        },
    )


class _Harness:
    def __init__(
        self, engine, tmp_path: Path, platform: str, scripts, *, tools=None, suite=None, language="en", session_id=SESSION,
        providers=None,
    ):
        self.session_id = session_id
        self.engine = engine
        self.platform = platform
        self.tmp_path = tmp_path
        self.controller = _Controller(engine, platform, language=language)
        self.provider = ScriptedProvider(scripts)
        self.providers = providers or (lambda protocol: self.provider)
        self.jobs = FakeJobHost()
        self.tools = list(tools or [FakeTool("echo")])
        self.suite = suite or ToolSuite(
            jobs=self.jobs,
            create_tools=lambda jobs, sink: list(self.tools),
            render_recovered=lambda *args: ToolResult((text("unused"),)),
            find_job=lambda session_id, call_id: None,
        )
        self.agent = self.new_agent()
        self._turns = 0

    def new_agent(self) -> AvibeAgent:
        """A fresh adapter over the same rows: what a restarted process starts with."""
        agent = AvibeAgent(
            self.controller,
            engine=self.engine,
            providers=self.providers,
            tool_suite=self.suite,
            state_dir=self.tmp_path / "agent_core",
        )
        self.controller.agent = agent
        self.agent = agent
        return agent

    def scope(self) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                select(messages.c.scope_id).where(messages.c.session_id == self.session_id).limit(1)
            ).scalar() or _SCOPES[self.platform]

    def request(self, body: str) -> AgentRequest:
        """An input admitted through the real Delivery path, claimed for a new Turn."""
        self._turns += 1
        turn_id = f"turn_{self._turns}_{id(self)}"
        delivery_id = f"dlv_{turn_id}"
        with self.engine.begin() as conn:
            # The Turn owner settles the previous Turn before it claims the next one.
            conn.execute(
                update(session_turns)
                .where(session_turns.c.session_id == self.session_id, session_turns.c.state != "terminal")
                .values(state="terminal", terminal_outcome="completed", terminal_at=NOW)
            )
            delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=self.session_id,
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=_SCOPES[self.platform],
                    session_id=self.session_id,
                    platform=self.platform,
                    author="user",
                    source="user",
                    message_type="user",
                    text=body,
                ),
                dispatch_text=body,
                now=NOW,
            )
            message_deliveries.claim_start_batch(
                conn, turn_id=turn_id, session_id=self.session_id, backend="avibe", deliveries=[delivery], dispatch_text=body
            )
        context = _context(self.platform, self.session_id, turn_id=turn_id, delivery_id=delivery_id)
        return AgentRequest(
            context=context,
            message=body,
            user_message=body,
            working_path=str(self.tmp_path),
            base_session_id=self.session_id,
            composite_session_id=self.session_id,
            session_key=f"{self.platform}::{context.channel_id}",
            vibe_agent_model="test-model",
        )

    def open_steer(self, body: str, turn_id: str, native_turn_id: str) -> tuple[str, str]:
        delivery_id = f"dlv_steer_{body[:8].replace(' ', '_')}"
        attempt_id = f"att_{delivery_id}"
        with self.engine.begin() as conn:
            delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=self.session_id,
                priority="p1",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=_SCOPES[self.platform],
                    session_id=self.session_id,
                    platform=self.platform,
                    author="user",
                    source="user",
                    message_type="user",
                    text=body,
                ),
                dispatch_text=body,
                now=NOW,
            )
            assert message_deliveries.open_steer_attempt(
                conn,
                delivery_id,
                expected_version=int(delivery["version"]),
                turn_id=turn_id,
                attempt_id=attempt_id,
                expected_native_turn_id=native_turn_id,
            )
        return delivery_id, attempt_id

    def accept_steer(self, delivery_id: str, attempt_id: str, turn_id: str) -> None:
        """What ``SessionTurnManager._finish_steer`` commits after an ACCEPTED receipt."""
        with self.engine.begin() as conn:
            assert message_deliveries.materialize_steer_acceptance(
                conn, leader_delivery_id=delivery_id, expected_attempt_id=attempt_id, turn_id=turn_id, evidence={}
            )

    def rows(self, message_type: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(messages).where(messages.c.session_id == self.session_id, messages.c.type == message_type)
                ).mappings()
            ]

    async def context_rows(self):
        return list(await self.agent.store.load(self.session_id))


_SCOPES: dict[str, str] = {}


@pytest.fixture()
def session(engine):
    """One Avibe Agent Session, with a Workbench project scope and an IM channel scope."""
    with engine.begin() as conn:
        _SCOPES["avibe"] = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_1", now=NOW)
        for platform in ("telegram", "wechat"):
            _SCOPES[platform] = upsert_scope(conn, platform=platform, scope_type="channel", native_id="C1", now=NOW)
        _insert_session(conn, SESSION, _SCOPES["avibe"])
    return SESSION


def _insert_session(conn, session_id: str, scope_id: str, metadata: Optional[dict] = None) -> None:
    conn.exec_driver_sql(
        "insert into agent_sessions (id, scope_id, agent_name, agent_backend, agent_variant, session_anchor, "
        "workdir, native_session_id, status, visibility, pinned, agent_status, metadata_json, created_at, "
        "updated_at, last_active_at) values (?, ?, 'avibe', 'avibe', 'avibe', ?, '/tmp', ?, 'active', "
        "'foreground', 0, 'idle', ?, ?, ?, ?)",
        (session_id, scope_id, session_id, session_id, json.dumps(metadata or {}), NOW, NOW, NOW),
    )


def _tool_turn() -> list:
    call = ToolCallBlock(id="call_1", name="echo", arguments={"path": "src"})
    return [[Done(assistant("Listing.", calls=(call,)))], [Done(assistant("两个文件。"))]]


def _shape(entries) -> list[tuple]:
    """Context rows without their row ids, which differ per Session."""
    return [(entry.context_seq, entry.kind, entry.message) for entry in entries]


@pytest.mark.parametrize("platform", ["avibe", "telegram"])
async def test_a_turn_commits_its_context_once_and_shows_the_reply_once(
    engine, session, tmp_path, published, platform
) -> None:
    harness = _Harness(engine, tmp_path, platform, _tool_turn())
    request = harness.request("看一下目录")

    await harness.agent.handle_message(request)

    rows = await harness.context_rows()
    assert [entry.kind for entry in rows] == ["input", "response", "tool_result", "response"]
    environment, typed = rows[0].message.content
    assert environment.text.startswith("<environment>\ncwd: ") and typed == TextBlock(text="看一下目录")
    # The steer identity existed when the Turn owner bound the native start.
    assert harness.controller.started == [f"avibe:{_turn(request.context)}"]
    # A10: every request is the context rebuilt from the tables at that point.
    assert [request_.messages for request_ in harness.provider.requests] == [
        project(rows[:1]).messages,
        project(rows[:3]).messages,
    ]
    assert harness.provider.requests[0].endpoint.base_url == "http://hub.invalid/avibe/v1"
    # One row per response: the dispatcher delivered the committed rows and persisted no copies.
    [result] = harness.rows("result")
    [narration] = harness.rows("assistant")
    assert result["id"] == rows[3].row_id and result["content_text"] == "两个文件。"
    assert narration["id"] == rows[1].row_id
    assert await harness.agent.store.pending_deliveries(SESSION) == []
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "terminal_result"}
    ]
    if platform == "avibe":
        announced = [payload["id"] for name, payload in published if name == "message.new"]
        assert announced.count(result["id"]) == 1
        # The row is the Workbench message; nothing sends the reply as a separate copy.
        assert "两个文件。" not in harness.controller.im_client.sent
    else:
        assert harness.controller.im_client.sent.count("两个文件。") == 1
        assert harness.controller.im_client.sent[0] == "Listing."


async def test_a_steer_enters_after_the_tool_batch_even_when_its_row_arrives_late(
    engine, session, tmp_path, published
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(arguments, ctx):
        started.set()
        await release.wait()
        return ToolResult((text("a.py"),))

    harness = _Harness(engine, tmp_path, "avibe", _tool_turn(), tools=[FakeTool("echo", execute=slow)])
    request = harness.request("list files")
    turn_id = _turn(request.context)
    running = asyncio.create_task(harness.agent.handle_message(request))
    await started.wait()

    native = harness.controller.started[0]
    delivery_id, attempt_id = harness.open_steer("also check tests", turn_id, native)
    receipt = await harness.agent.steer_active_turn(
        SteerRequest(SESSION, turn_id, native, "also check tests", attempt_id=attempt_id),
        ActiveSteerTarget("runtime", turn_id, request.context, request, harness.agent),
    )
    assert receipt.outcome is SteerOutcome.ACCEPTED
    release.set()
    # The Turn owner materializes the steer's row only after the receipt.
    await asyncio.sleep(0.05)
    harness.accept_steer(delivery_id, attempt_id, turn_id)
    await running

    rows = await harness.context_rows()
    assert [(entry.kind, entry.row_id) for entry in rows[2:4]] == [
        ("tool_result", rows[2].row_id),
        ("input", delivery_id),
    ]
    assert rows[3].message == UserMessage((text("also check tests"),))
    assert harness.provider.requests[1].messages == project(rows[:4]).messages
    # After the run, a steer for the settled Turn is refused and falls back to the queue.
    late = await harness.agent.steer_active_turn(
        SteerRequest(SESSION, turn_id, native, "too late", attempt_id="att_none"),
        ActiveSteerTarget("runtime", turn_id, request.context, request, harness.agent),
    )
    assert late.outcome is SteerOutcome.NOT_ACTIVE


async def test_stop_aborts_the_running_tool_and_settles_the_turn_as_stopped(
    engine, session, tmp_path, published
) -> None:
    started = asyncio.Event()
    observed: list[bool] = []

    async def wait_for_cancel(arguments, ctx):
        started.set()
        await ctx.cancel.wait()
        observed.append(ctx.cancel.cancelled)
        return ToolResult((text("Command aborted"),), is_error=True)

    harness = _Harness(engine, tmp_path, "telegram", _tool_turn(), tools=[FakeTool("echo", execute=wait_for_cancel)])
    request = harness.request("run it")
    stop = AgentRequest(**{**request.__dict__, "message": "stop"})
    assert await harness.agent.handle_stop(stop) is False
    assert stop.stop_failure_reason == "not_active"

    running = asyncio.create_task(harness.agent.handle_message(request))
    await started.wait()
    assert await harness.agent.handle_stop(stop) is True
    await running

    assert observed == [True]
    assert len(harness.provider.requests) == 1
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "stopped"}
    ]
    assert not any("❌" in sent for sent in harness.controller.im_client.sent)


async def test_a_committed_reply_a_crash_left_undelivered_reaches_workbench_once(
    engine, session, tmp_path, published
) -> None:
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok again"))]])
    request = harness.request("hello")
    # The first process commits the reply and dies before delivering it.
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("hello"),))
    )
    orphan = await harness.agent.store.append_response(SESSION, assistant("answer before the crash"), final=True)

    # The restarted process delivers it once its surface is ready; later Turns do not deliver it again.
    harness.new_agent()
    assert await harness.agent.restore_pending_deliveries({"avibe"}) == 1
    harness.new_agent()
    await harness.agent.handle_message(harness.request("next"))

    announced = [payload["id"] for name, payload in published if name == "message.new"]
    assert announced.count(orphan.row_id) == 1
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_an_im_reply_resends_only_its_unconfirmed_parts(engine, session, tmp_path, published) -> None:
    long_reply = "你" * 1000  # 3,000 bytes: two WeChat parts
    harness = _Harness(engine, tmp_path, "wechat", [[Done(assistant(long_reply))], [Done(assistant("ok"))]])
    harness.controller.im_client.fail_sends = {2}
    harness.controller.im_client.fail_uploads = 1  # and the document fallback fails too

    await harness.agent.handle_message(harness.request("write a long answer"))

    [reply] = await harness.agent.store.pending_deliveries(SESSION)
    first_part = harness.controller.im_client.sent[0]
    assert [part is not None for part in reply.parts] == [True, False]

    harness.new_agent()
    await harness.agent.handle_message(harness.request("next"))

    sent = harness.controller.im_client.sent
    # The first attempt sent part one and a delivery-failure notice; resume sends part two only.
    # The first attempt sent part one, then the existing delivery-failure notice; resume sends part two only.
    rest = long_reply[len(first_part):]
    assert sent.count(first_part) == 1 and sent.count(rest) == 1
    assert sent[1] == i18n_t("error.resultDeliveryFailed", "en")
    assert await harness.agent.store.pending_deliveries(SESSION) == []
    with engine.connect() as conn:
        delivery = json.loads(
            conn.execute(select(messages.c.metadata_json).where(messages.c.id == reply.row_id)).scalar_one()
        )["delivery"]
    assert delivery["state"] == "delivered" and len(delivery["parts"]) == 2


async def test_resume_settles_open_calls_and_admits_unconsumed_inputs_before_the_next_call(
    engine, session, tmp_path, published
) -> None:
    jobs = FakeJobHost()
    rendered: list[tuple] = []

    def render(call, job_id, status, watch_id):
        rendered.append((call.id, job_id, status.state, watch_id))
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs,
        create_tools=lambda jobs_, sink: [FakeTool("bash"), FakeTool("edit")],
        render_recovered=render,
        find_job=lambda session_id, call_id: "job_1" if (session_id, call_id) == (SESSION, "call_bash") else None,
    )
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("resumed"))]], suite=suite)
    # The crashed process: a Turn whose response opened two calls, one with a running job,
    # and a steer it accepted but never consumed.
    first = harness.request("fix the test")
    harness.controller._native_start(first.context)
    await harness.agent.store.consume_input(
        SESSION, first.context.platform_specific["delivery_id"], UserMessage((text("fix the test"),))
    )
    calls = (
        ToolCallBlock(id="call_bash", name="bash", arguments={"command": "pytest -q"}),
        ToolCallBlock(id="call_edit", name="edit", arguments={"path": "a.py", "edits": []}),
    )
    await harness.agent.store.append_response(SESSION, assistant("Running.", calls=calls), final=False)
    jobs.states["job_1"] = JobStatus("running")
    steer_id, attempt = harness.open_steer("and the docs", _turn(first.context), f"avibe:{_turn(first.context)}")
    harness.accept_steer(steer_id, attempt, _turn(first.context))

    harness.new_agent()
    second = harness.request("continue")
    await harness.agent.handle_message(second)

    rows = await harness.context_rows()
    assert [(entry.kind, entry.row_id) for entry in rows[2:5]] == [
        ("tool_result", rows[2].row_id),
        ("tool_result", rows[3].row_id),
        ("input", steer_id),
    ]
    assert rendered == [("call_bash", "job_1", "running", "watch_job_1")]
    assert rows[3].message.content == (text(UNRECORDED_EFFECT),)
    assert rows[5].row_id == second.context.platform_specific["delivery_id"]
    # The model ran once, for the new input, with everything that was settled before it.
    [only] = harness.provider.requests
    assert only.messages == project(rows[:6]).messages


async def test_a_fork_from_an_earlier_message_continues_only_the_inherited_prefix(
    engine, session, tmp_path, published
) -> None:
    source = _Harness(engine, tmp_path, "avibe", [[Done(assistant("first answer"))], [Done(assistant("later"))]])
    await source.agent.handle_message(source.request("first question"))
    [anchor] = source.rows("result")
    with engine.begin() as conn:
        # What fork reservation records (core/services/session_fork.py, C-5 section 4).
        _insert_session(
            conn,
            "ses_child",
            _SCOPES["avibe"],
            {
                "created_via": "session_fork",
                "fork_source_session_id": SESSION,
                "fork_source_message_id": anchor["id"],
                "fork_source_context_seq": resolve_fork_anchor_seq(conn, SESSION, anchor["id"]),
            },
        )
    # The source moves on after the fork; none of it may reach the child.
    await source.agent.handle_message(source.request("source continues"))

    child = _Harness(engine, tmp_path, "avibe", [[Done(assistant("child answer"))]], session_id="ses_child")
    await child.agent.handle_message(child.request("child question"))

    inherited = [entry for entry in await source.context_rows() if entry.context_seq <= 2]
    rows = await child.context_rows()
    assert rows[:2] == inherited
    assert [(entry.session_id, entry.context_seq, entry.kind) for entry in rows[2:]] == [
        ("ses_child", 3, "input"),
        ("ses_child", 4, "response"),
    ]
    [request] = child.provider.requests
    assert request.messages == project(rows[:3]).messages
    assert "source continues" not in json.dumps([str(message) for message in request.messages], ensure_ascii=False)


@pytest.mark.parametrize("kind", sorted({*_KIND_KEYS, "unknown", "context_exhausted"}))
def test_every_error_kind_has_english_and_chinese_copy(kind: str) -> None:
    key = error_key(kind)
    english, chinese = i18n_t(key, "en"), i18n_t(key, "zh")
    assert english != key and chinese != key and english != chinese


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_a_failed_run_shows_localized_copy_and_a_refusal_shows_its_explanation_once(
    engine, session, tmp_path, published, language
) -> None:
    refusal = AssistantMessage((), ORIGIN, "refusal")
    harness = _Harness(
        engine, tmp_path, "telegram", [[Done(assistant(""))], [Done(refusal)]], language=language
    )

    await harness.agent.handle_message(harness.request("say nothing"))
    await harness.agent.handle_message(harness.request("say something unsafe"))

    sent = harness.controller.im_client.sent
    empty_copy = i18n_t("avibeAgent.error.emptyResponse", language)
    refusal_copy = i18n_t("avibeAgent.error.refusal", language)
    assert sent == [f"❌ {empty_copy}", refusal_copy]
    [_, refusal_row] = harness.rows("result")
    assert refusal_row["content_text"] == refusal_copy
    assert [terminal["is_error"] for terminal in harness.controller.terminals] == [True, True]


async def test_a_refused_model_route_fails_before_the_input_is_written(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [])

    async def refuse(*_args, **_kwargs):
        raise RuntimeError("no source serves this model")

    harness.controller.model_hub_runtime = SimpleNamespace(resolve=refuse)
    request = harness.request("hello")
    await harness.agent.handle_message(request)

    assert harness.controller.started == []
    assert await harness.context_rows() == []
    assert harness.provider.requests == []
    assert harness.controller.im_client.sent == [f"❌ {i18n_t('avibeAgent.error.generic', 'en')}"]
    assert [terminal["is_error"] for terminal in harness.controller.terminals] == [True]



# --- a committed row is delivered from its persisted plan (recovery.md D1/D2) ---------------


async def _crash_after_commit(harness: _Harness, body: str, *messages: AssistantMessage, finals=None) -> list:
    """The first process commits rows for a Turn and dies before delivering any of them."""
    request = harness.request(body)
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        harness.session_id, request.context.platform_specific["delivery_id"], UserMessage((text(body),))
    )
    rows = []
    for index, message in enumerate(messages):
        final = (finals or [False] * len(messages))[index]
        rows.append(await harness.agent.store.append_response(harness.session_id, message, final=final))
    return rows


async def test_recovered_narration_stays_process_output(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [])
    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    await _crash_after_commit(harness, "go", assistant("Checking the files first.", calls=(call,)))

    harness.new_agent()
    assert await harness.agent.restore_pending_deliveries({"telegram"}) == 1

    assert harness.controller.im_client.routes == [("C1", "message", "Checking the files first.")]
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_a_recovered_reply_goes_to_the_scheduled_override_target(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("report ready"))]])
    request = harness.request("scheduled report")
    request.context.platform_specific["delivery_override"] = {
        "platform": "telegram", "channel_id": "C-report", "user_id": "u-report",
    }
    harness.controller.im_client.fail_sends = {1}
    harness.controller.im_client.fail_uploads = 1  # and the document fallback fails too

    await harness.agent.handle_message(request)
    harness.new_agent()
    await harness.agent.restore_pending_deliveries({"telegram"})

    replies = [(channel, text) for channel, route, text in harness.controller.im_client.routes if route == "reply"]
    assert replies == [("C-report", "report ready")]
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_recovery_resends_the_persisted_split_after_a_limit_change(
    engine, session, tmp_path, published, monkeypatch
) -> None:
    long_reply = "你" * 1000  # 3,000 bytes: two WeChat parts at today's limit
    harness = _Harness(engine, tmp_path, "wechat", [[Done(assistant(long_reply))]])
    harness.controller.im_client.fail_sends = {2}
    harness.controller.im_client.fail_uploads = 1  # and the document fallback fails too
    await harness.agent.handle_message(harness.request("write a long answer"))
    first_part = harness.controller.im_client.sent[0]
    # A later release splits WeChat replies differently.
    monkeypatch.setattr(ConsolidatedMessageDispatcher, "_get_result_max_bytes", lambda self, context: 900)

    harness.new_agent()
    await harness.agent.restore_pending_deliveries({"wechat"})

    sent = harness.controller.im_client.sent
    assert sent.count(first_part) == 1 and first_part + sent[-1] == long_reply
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_an_attachment_is_acknowledged_only_after_its_upload(engine, session, tmp_path, published) -> None:
    report = tmp_path / "report.txt"
    report.write_text("numbers", encoding="utf-8")
    reply = f"Here is the report: [report](file://{report})"
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant(reply))]])
    harness.controller.im_client.fail_uploads = 1

    await harness.agent.handle_message(harness.request("send me the report"))
    [pending] = await harness.agent.store.pending_deliveries(SESSION)
    assert harness.controller.im_client.uploads == []

    harness.new_agent()
    await harness.agent.restore_pending_deliveries({"telegram"})

    assert harness.controller.im_client.uploads == [str(report)]
    assert [text for _channel, route, text in harness.controller.im_client.routes if route == "reply"] == [
        "Here is the report: report"
    ]
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_the_footer_reports_the_runs_final_outcome(engine, session, tmp_path, published) -> None:
    class _LeakyJobs(FakeJobHost):
        async def kill(self, job_id, *, reason="killed") -> None:
            raise RuntimeError("the job's process group cannot be signalled")

    jobs = _LeakyJobs()

    def tools(tracking, sink):
        async def start_and_leave(arguments, ctx):
            await tracking.start("sleep 600", cwd=ctx.cwd, env={}, timeout_s=None, session_id=ctx.session_id,
                                 tool_call_id=ctx.tool_call_id)
            return ToolResult((text("started"),))

        return [FakeTool("echo", execute=start_and_leave)]

    suite = ToolSuite(jobs=jobs, create_tools=tools, render_recovered=lambda *a: None, find_job=lambda *a: None)
    harness = _Harness(engine, tmp_path, "avibe", _tool_turn(), suite=suite)
    harness.controller.config.show_duration = True

    await harness.agent.handle_message(harness.request("start it"))

    # The reply committed before cleanup failed, so only settlement knows the run failed.
    [result] = harness.rows("result")
    assert json.loads(result["content_json"])["result_footer"] == "❌ failed"
    assert harness.controller.terminals[-1]["is_error"] is True


async def test_the_system_prompt_lists_every_tool_the_run_offers(engine, session, tmp_path, published) -> None:
    harness = _Harness(
        engine, tmp_path, "avibe", [[Done(assistant("ok"))]],
        tools=[FakeTool(name) for name in ("read", "bash", "edit", "write")],
    )

    await harness.agent.handle_message(harness.request("hi"))

    system = harness.provider.requests[0].system
    assert all(f"- {name}: " in system for name in ("read", "bash", "edit", "write"))
    assert "Use edit for precise changes" in system


async def test_an_idle_session_holds_no_adapter_state(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "avibe", _tool_turn() + [[Done(assistant("again"))]])

    await harness.agent.handle_message(harness.request("first"))
    await harness.agent.handle_message(harness.request("second"))

    assert harness.agent._runtimes == {}
    assert harness.agent.store._env_state == {}
    assert not getattr(harness.agent.store, "_responses", {})


# --- round 2: receipts are evidence, recovery reads only the plan, the final row owns the outcome ---


def _receipts(engine, row_id: str) -> dict:
    with engine.connect() as conn:
        return json.loads(conn.execute(select(messages.c.metadata_json).where(messages.c.id == row_id)).scalar_one())[
            "delivery"
        ]


async def test_recovery_waits_for_the_planned_target_transport(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("report ready"))]])
    request = harness.request("scheduled report")
    request.context.platform_specific["delivery_override"] = {
        "platform": "wechat", "channel_id": "W-report", "user_id": "u-report",
    }
    harness.controller.im_client.fail_sends = {1}  # the override target is not reachable yet
    harness.controller.im_client.fail_uploads = 1  # and the document fallback fails too
    await harness.agent.handle_message(request)

    harness.new_agent()
    # The source transport becoming ready does not deliver a row planned for another target.
    assert await harness.agent.restore_pending_deliveries({"telegram"}) == 0
    assert await harness.agent.restore_pending_deliveries({"wechat"}) == 1
    assert [(channel, text) for channel, route, text in harness.controller.im_client.routes if route == "reply"] == [
        ("W-report", "report ready")
    ]
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_an_interim_copy_is_acknowledged_only_once_it_is_persisted(
    engine, session, tmp_path, published, monkeypatch
) -> None:
    import core.message_dispatcher as dispatcher_module

    narration = "Reading the configuration and the tests before changing anything. " * 4  # interim-worthy
    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant(narration, calls=(call,)))], [Done(assistant("ok"))]])
    real_persist = dispatcher_module.persist_agent_message
    failures = {"interim": 1}

    def flaky_persist(context, canonical_type, text, **kwargs):
        if failures.get(canonical_type):
            failures[canonical_type] -= 1
            return None  # persist_agent_message reports a failed write as None
        return real_persist(context, canonical_type, text, **kwargs)

    monkeypatch.setattr(dispatcher_module, "persist_agent_message", flaky_persist)
    await harness.agent.handle_message(harness.request("look around"))

    [pending] = await harness.agent.store.pending_deliveries(SESSION)
    harness.new_agent()
    await harness.agent.restore_pending_deliveries({"avibe"})
    await harness.agent.restore_pending_deliveries({"avibe"})

    assert await harness.agent.store.pending_deliveries(SESSION) == []
    assert len(harness.rows("interim")) == 1
    assert pending.row_id in {row["id"] for row in harness.rows("assistant")}


async def test_narration_receipts_say_what_happened(engine, session, tmp_path, published) -> None:
    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    script = [[Done(assistant("First look.", calls=(call,)))], [Done(assistant("done"))]]
    hidden = _Harness(engine, tmp_path, "telegram", script)
    hidden.controller.settings_manager.hidden = {"assistant"}
    await hidden.agent.handle_message(hidden.request("go"))
    [narration] = hidden.rows("assistant")
    receipt = _receipts(engine, narration["id"])
    # Hidden by the channel's settings: an explicit skipped receipt, decided before sending.
    assert receipt["state"] == "delivered" and receipt["parts"][0]["skipped"] == "hidden"
    assert "First look." not in hidden.controller.im_client.sent

    failing = _Harness(engine, tmp_path, "telegram", [[Done(assistant("Second look.", calls=(call,)))], [Done(assistant("ok"))]])
    failing.controller.im_client.fail_sends = {1}
    await failing.agent.handle_message(failing.request("again"))
    [still_pending] = await failing.agent.store.pending_deliveries(SESSION)
    # A send failure never acknowledges.
    assert _receipts(engine, still_pending.row_id)["parts"] == []


async def test_a_background_final_settles_with_its_committed_text(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("the nightly summary"))]])
    request = harness.request("summarize the night")
    request.context.platform_specific["suppress_delivery"] = True

    await harness.agent.handle_message(request)

    assert harness.controller.im_client.sent == []
    assert harness.controller.terminal_texts == ["the nightly summary"]
    assert harness.controller.terminals[-1]["is_error"] is False


async def test_a_stop_after_the_final_reply_committed_loses_the_race(
    engine, session, tmp_path, published, monkeypatch
) -> None:
    import modules.agents.avibe.agent as agent_module
    from core.agent_core.agent.events import MessageCommitted, RunEnded
    from core.agent_core.agent.loop import Agent

    stopped = asyncio.Event()

    class _FinalThenStop(Agent):
        """The loop's event order when Stop lands after the final commit but before the run ends."""

        async def run(self, input, *, turn_id):
            await self.store.consume_input(self.session_id, input.message_id, input.message)
            row = await self.store.append_response(self.session_id, assistant("the answer"), final=True)
            yield MessageCommitted(turn_id, 0, row.row_id, row.context_seq, True)
            await stopped.wait()
            yield RunEnded(turn_id, 1, "aborted")

        def abort(self, reason: str = "aborted") -> None:
            stopped.set()

        async def take_pending_inputs(self):
            return ()

    monkeypatch.setattr(agent_module, "Agent", _FinalThenStop)
    harness = _Harness(engine, tmp_path, "telegram", [])
    request = harness.request("answer me")
    running = asyncio.create_task(harness.agent.handle_message(request))
    while not harness.rows("result"):
        await asyncio.sleep(0.01)

    assert await harness.agent.handle_stop(AgentRequest(**{**request.__dict__, "message": "stop"})) is True
    await running

    assert harness.controller.im_client.sent.count("the answer") == 1
    assert harness.controller.terminals[-1] == {
        "turn": _turn(request.context), "is_error": False, "settled_by": "terminal_result"
    }
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_the_config_refresh_registers_and_retires_the_backend(engine) -> None:
    from config.v2_compat import to_app_config
    from config.v2_config import V2Config
    from core.agent_auth_service import AgentAuthService

    agents: dict[str, Any] = {}

    async def refresh_runtime_config(name, runtime_config) -> bool:
        return False

    service = SimpleNamespace(
        agents=agents,
        register=lambda agent: agents.__setitem__(agent.name, agent),
        refresh_runtime_config=refresh_runtime_config,
        runtime_turn_tokens_for_backend=lambda _backend: {},
        release_runtime_turn_tokens=lambda _tokens: None,
    )
    config = V2Config.default()
    controller = SimpleNamespace(
        agent_service=service, im_client=_IMClient(), settings_manager=_SettingsManager(), config=None
    )
    owner = AgentAuthService(controller)
    for enabled in (True, False, True):
        config.agents.avibe.enabled = enabled
        config.save()
        controller.config = to_app_config(config)
        await owner._apply_backend_runtime_refresh("avibe")
        assert isinstance(agents.get("avibe"), AvibeAgent) is enabled


async def test_startup_hands_an_orphaned_foreground_job_to_its_watch(engine, session, tmp_path, published) -> None:
    jobs = FakeJobHost()
    rendered: list[tuple] = []

    def render(call, job_id, status, watch_id):
        rendered.append((call.id, job_id, watch_id))
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs,
        create_tools=lambda jobs_, sink: [FakeTool("bash")],
        render_recovered=render,
        find_job=lambda session_id, call_id: "job_1" if (session_id, call_id) == (SESSION, "call_bash") else None,
    )
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    # The crashed process left a running foreground command; its tool-call response had
    # nothing to show, so no outbox row is pending for this Session.
    request = harness.request("run the slow suite")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run the slow suite"),))
    )
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": "pytest -q"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    jobs.states["job_1"] = JobStatus("running")
    assert await harness.agent.store.pending_deliveries(SESSION) == []

    for _ in range(2):
        harness.new_agent()
        await harness.agent.restore_pending_deliveries({"telegram"})

    rows = await harness.context_rows()
    assert rows[-1].kind == "tool_result" and "now Watch watch_job_1" in rows[-1].message.content[0].text
    assert rendered == [("call_bash", "job_1", "watch_job_1")] and jobs.watches == {"job_1": "watch_job_1"}
    assert harness.provider.requests == []


# --- round 3: one evidence rule, one outcome rule, every recovery trigger ------------------


@pytest.mark.parametrize(
    "part, failure",
    [
        ("text", ""),
        ("text", None),
        ("text", False),
        ("text", "raise"),
        ("file", ""),
        ("file", None),
        ("file", "raise"),
    ],
)
async def test_a_part_without_evidence_is_never_acknowledged(
    engine, session, tmp_path, published, part, failure
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("numbers", encoding="utf-8")
    reply = f"Here is the report: [report](file://{report})"
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant(reply))]])
    client = harness.controller.im_client
    if part == "text":
        # The send and the live path's document fallback both come back without evidence.
        if failure == "raise":
            client.fail_sends, client.fail_uploads = {1}, 1
        else:
            client.send_results, client.upload_results = {1: failure}, [failure]
    elif failure == "raise":
        client.fail_uploads = 1
    else:
        client.upload_results = [failure]

    await harness.agent.handle_message(harness.request("send me the report"))
    [pending] = await harness.agent.store.pending_deliveries(SESSION)
    index = 0 if part == "text" else 1
    assert pending.parts == () or pending.parts[index] is None

    harness.new_agent()
    await harness.agent.restore_pending_deliveries({"telegram"})
    assert await harness.agent.store.pending_deliveries(SESSION) == []
    assert client.uploads == [str(report)]


async def test_recovery_records_an_unexplained_refusal_as_failed(engine, session, tmp_path, published) -> None:
    from core.session_turns import SessionTurnManager

    harness = _Harness(engine, tmp_path, "telegram", [[Done(AssistantMessage((), ORIGIN, "refusal"))]], language="zh")
    request = harness.request("say something unsafe")
    turn_id = _turn(request.context)

    class _Crash(BaseException):
        pass

    async def crash_before_settlement(_run) -> None:
        raise _Crash()

    # The process dies after the run committed an empty refusal, before settling the Turn.
    harness.agent._settle = crash_before_settlement
    with pytest.raises(_Crash):
        await harness.agent.handle_message(request)
    with engine.connect() as conn:
        plan = json.loads(conn.execute(select(messages.c.metadata_json).where(messages.c.type == "result")).scalar())
    assert plan["delivery"]["plan"]["turn_id"] == turn_id

    manager = SessionTurnManager(harness.controller, build_context=lambda sid: request.context)
    manager._engine = engine
    notices: list[str] = []

    async def emit(_context, kind, body, **_kwargs):
        notices.append(kind)
        return "msg"

    manager.controller = SimpleNamespace(**{**vars(harness.controller), "emit_agent_message": emit})
    manager._active_identity = lambda *_args: None
    await manager.recover_durable_delivery_state(SESSION, service_restart=True)

    with engine.connect() as conn:
        turn = message_deliveries.get_turn(conn, turn_id)
    assert (turn["terminal_outcome"], turn["settled_by"], turn["terminal_evidence_kind"]) == (
        "failed", "terminal_result", "committed_final"
    )
    assert json.loads(turn["terminal_evidence_json"])["result_text"] == i18n_t("avibeAgent.error.refusal", "zh")
    assert notices == []


async def test_enabling_the_backend_live_delivers_rows_left_pending(engine, session, tmp_path, published) -> None:
    from config.v2_config import V2Config
    from core.agent_auth_service import AgentAuthService

    harness = _Harness(engine, tmp_path, "telegram", [])
    # The previous process committed a reply and stopped before delivering it; avibe then
    # started disabled, so no readiness callback could restore it.
    await _crash_after_commit(harness, "go", assistant("the answer"), finals=[True])
    controller = harness.controller
    agents: dict[str, Any] = {}

    async def refresh_runtime_config(_name, _runtime_config) -> bool:
        return False

    controller.agent_service.agents = agents
    controller.agent_service.register = lambda agent: agents.__setitem__(agent.name, agent)
    controller.agent_service.refresh_runtime_config = refresh_runtime_config
    controller.agent_service.runtime_turn_tokens_for_backend = lambda _backend: {}
    controller.agent_service.release_runtime_turn_tokens = lambda _tokens: None
    controller.im_clients = {"telegram": controller.im_client}
    controller.is_im_transport_ready = lambda platform: True
    config = V2Config.default()
    config.agents.avibe.enabled = True
    config.save()

    await AgentAuthService(controller)._apply_backend_runtime_refresh("avibe")

    assert isinstance(agents.get("avibe"), AvibeAgent)
    assert controller.im_client.sent.count("the answer") == 1
    assert await harness.agent.store.pending_deliveries(SESSION) == []


async def test_a_provider_that_fails_to_close_does_not_fail_a_delivered_turn(
    engine, session, tmp_path, published
) -> None:
    closed: list[str] = []

    class _Provider(ScriptedProvider):
        def __init__(self, scripts, name: str, *, fails: bool) -> None:
            super().__init__(scripts)
            self.name, self.fails = name, fails

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.fails:
                raise RuntimeError("the connection pool would not close")

    provider = _Provider([[Done(assistant("done"))]], "anthropic", fails=True)
    harness = _Harness(engine, tmp_path, "telegram", [], providers=lambda protocol: provider)

    await harness.agent.handle_message(harness.request("hi"))

    assert harness.controller.im_client.sent == ["done"]
    assert harness.controller.terminals[-1]["is_error"] is False
    assert closed == ["anthropic"]
    # Every adapter is closed even when an earlier one fails to close.
    from modules.agents.avibe.models import HubModelRouter

    adapters = {"anthropic": _Provider([], "anthropic", fails=True), "google": _Provider([], "google", fails=False)}
    router = HubModelRouter(lambda: None, lambda protocol: adapters[protocol])
    router.provider_for("anthropic"), router.provider_for("google")
    closed.clear()
    await router.aclose()
    assert closed == ["anthropic", "google"]
