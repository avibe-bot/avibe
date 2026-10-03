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
* a committed row is delivered through the shared dispatcher, which writes its
  display into that row instead of persisting a second one;
* resume settles open tool calls and admits accepted, unconsumed inputs before
  the next model call, never running the model on its own (T2, T3, T4);
* failures show localized copy in English and Chinese.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import select, update

from core.agent_core.agent.recovery import UNRECORDED_EFFECT
from core.agent_core.ai.provider import Done
from core.agent_core.harness.projection import project
from core.agent_core.messages import AssistantMessage, TextBlock, ToolCallBlock, ToolResultMessage, UserMessage, text
from core.agent_core.tools.base import JobStatus, ToolResult
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerRequest
from modules.agents.avibe import AvibeAgent
from modules.agents.avibe.errors import _KIND_KEYS, error_key
from modules.agents.avibe.tools import ToolSuite, local_tool_suite
from modules.agents.base import AgentRequest
from modules.agents.model_hub import ModelHubLaunch
from modules.im import MessageContext
from storage import message_deliveries
from storage.agent_transcript import resolve_fork_anchor_seq
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, messages, session_turns
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
    def format_toolcall(self, name: str, arguments: Optional[dict] = None, get_relative_path=None) -> str:
        return f"🔧 {name} {json.dumps(arguments or {}, sort_keys=True)}"

    def format_toolcall_label(self, name: str, arguments: Optional[dict] = None, get_relative_path=None) -> str:
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
        self.hub_protocol = "anthropic"
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
            protocol=self.hub_protocol,
            provider="test-provider",
            supports_images=True,
        )

    def _native_start(self, context, *, activation_identity=None) -> None:
        """The Turn owner's native start: bind the steer identity, materialize the input row."""
        turn_id = _turn(context)
        target = ActiveSteerTarget("runtime", turn_id, context, None, self.agent)
        # Outside a run (a crash simulation), the identity a previous process's adapter bound.
        native = self.agent.steering_native_turn_id(target) or f"avibe:previous-process:{turn_id}"
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


async def _unused_renderer(*_args) -> ToolResult:
    return ToolResult((text("unused"),))


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
            render_recovered=_unused_renderer,
            find_job=lambda session_id, call_id, **_: None,
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
    # The clock fields follow include_time_info (off here), as every input prefix does.
    assert "date:" not in environment.text and "timezone:" not in environment.text
    # The steer identity existed when the Turn owner bound the native start.
    [native] = harness.controller.started
    assert native.startswith("avibe:") and native.endswith(f":{_turn(request.context)}")
    # A10: every request is the context rebuilt from the tables at that point.
    assert [request_.messages for request_ in harness.provider.requests] == [
        project(rows[:1]).messages,
        project(rows[:3]).messages,
    ]
    assert harness.provider.requests[0].endpoint.base_url == "http://hub.invalid/avibe/v1"
    # One row per response: the dispatcher wrote its display into the committed rows, never a copy.
    [result] = harness.rows("result")
    [narration] = harness.rows("assistant")
    assert result["id"] == rows[3].row_id and result["content_text"] == "两个文件。"
    assert narration["id"] == rows[1].row_id
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "terminal_result"}
    ]
    if platform == "avibe":
        announced = [payload["id"] for name, payload in published if name == "message.new"]
        assert announced.count(result["id"]) == 1
        # The model payload is context, never part of a message a client receives.
        assert not any("model" in (payload.get("content") or {}) for name, payload in published if name == "message.new")
    else:
        assert harness.controller.im_client.sent.count("两个文件。") == 1
        assert harness.controller.im_client.sent[0] == "Listing."


async def test_a_stop_before_dispatch_settles_the_turn_as_stopped(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("an answer nobody wants"))]])
    resolving, release = asyncio.Event(), asyncio.Event()
    resolve = harness.controller._resolve

    async def slow_route(*args, **kwargs):
        resolving.set()
        await release.wait()
        return await resolve(*args, **kwargs)

    # Stop arrives while Model Hub is still resolving the route, before anything is dispatched.
    harness.controller.model_hub_runtime = SimpleNamespace(resolve=slow_route)
    request = harness.request("long question")
    running = asyncio.create_task(harness.agent.handle_message(request))
    await resolving.wait()

    assert await harness.agent.handle_stop(AgentRequest(**{**request.__dict__, "message": "stop"})) is True
    release.set()
    await running

    assert harness.controller.started == [] and harness.provider.requests == []
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "stopped"}
    ]


async def test_a_setup_failure_after_the_route_resolved_fails_the_hub_attempt(
    engine, session, tmp_path, published, monkeypatch
) -> None:
    import core.system_prompt_injection as injection

    reported: list[str] = []

    async def record_native_failure(context, diagnostic) -> bool:
        reported.append(diagnostic)
        return False

    def broken(**_kwargs):
        raise RuntimeError("the skills directory could not be read")

    monkeypatch.setattr(injection, "build_system_prompt_injection", broken)
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("unreachable"))]])
    harness.controller.model_hub_runtime.record_native_failure = record_native_failure

    await harness.agent.handle_message(harness.request("hello"))

    # Through the same Hub-aware failure path as any other run failure, before any dispatch.
    assert len(reported) == 1 and harness.controller.started == []
    assert harness.controller.terminals[-1]["is_error"] is True


async def test_committed_rows_keep_the_agent_that_ran_the_turn(engine, session, tmp_path, published) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(arguments, ctx):
        started.set()
        await release.wait()
        return ToolResult((text("a.py"),))

    harness = _Harness(engine, tmp_path, "avibe", _tool_turn(), tools=[FakeTool("echo", execute=slow)])
    request = harness.request("list files")
    request.vibe_agent_name = "avibe"
    running = asyncio.create_task(harness.agent.handle_message(request))
    await started.wait()
    with engine.begin() as conn:
        # The user picks another Agent while this Turn runs.
        conn.execute(
            update(agent_sessions).where(agent_sessions.c.id == SESSION).values(agent_name="reviewer", agent_backend="codex")
        )
    release.set()
    await running

    with engine.connect() as conn:
        names = {
            row.author_name
            for row in conn.execute(
                select(messages.c.author_name).where(
                    messages.c.session_id == SESSION, messages.c.author == "agent", messages.c.context_seq.is_not(None)
                )
            )
        } | {
            row.agent_name
            for row in conn.execute(
                select(agent_events.c.agent_name).where(
                    agent_events.c.session_id == SESSION, agent_events.c.context_seq.is_not(None)
                )
            )
        }
    assert names == {"avibe"}
    with engine.connect() as conn:
        backends = set(conn.execute(
            select(agent_events.c.backend).where(agent_events.c.session_id == SESSION, agent_events.c.context_seq.is_not(None))
        ).scalars())
    assert backends == {"avibe"}


async def test_the_environment_names_watches_without_their_commands(engine, session, tmp_path, published) -> None:
    from core.watches import ManagedWatch

    secret = "curl -H 'Authorization: Bearer sk-live-secret' https://api.example"
    watches = [
        ManagedWatch(id="wch_named", name="nightly sync", session_key="k", session_id=SESSION, shell_command=secret),
        ManagedWatch(
            id="wch_job", name=None, session_key="k", session_id=SESSION, shell_command=secret,
            metadata={"watch_target": {"kind": "job", "job_id": "job_1", "command": secret}},
        ),
    ]
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("ok"))]])
    harness.controller.watch_service = SimpleNamespace(store=SimpleNamespace(list_watches=lambda: watches))

    await harness.agent.handle_message(harness.request("status?"))

    # The block is model context: a Watch is named by its id, name, and kind, never its command.
    block = (await harness.context_rows())[0].message.content[0].text
    assert "sk-live-secret" not in block and "curl" not in block
    assert 'wch_named "nightly sync" command' in block and "wch_job job" in block


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


async def test_reconcile_reports_a_steer_attempt_only_from_evidence(engine, session, tmp_path, published) -> None:
    from core.services.agent_steering import SteerReconcileRequest
    from modules.im.base import FileAttachment

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
    _delivery_id, attempt_id = harness.open_steer("look at this", turn_id, native)
    preparing, fail = asyncio.Event(), asyncio.Event()

    async def vanished(*_args, **_kwargs):
        preparing.set()
        await fail.wait()
        raise RuntimeError("the snapshot store refused the image")

    # The steer's image is still being snapshotted when the Turn owner reconciles the attempt.
    harness.agent.media.snapshot_file = vanished
    attachment = FileAttachment(name="shot.png", mimetype="image/png", local_path=str(tmp_path / "shot.png"))
    steering = asyncio.create_task(
        harness.agent.steer_active_turn(
            SteerRequest(SESSION, turn_id, native, "look at this", attempt_id=attempt_id, files=(attachment,)),
            ActiveSteerTarget("runtime", turn_id, request.context, request, harness.agent),
        )
    )
    await preparing.wait()
    reconcile = SteerReconcileRequest(SESSION, turn_id, native, attempt_id)

    async def outcome() -> SteerOutcome:
        return (await harness.agent.reconcile_steer_attempt(reconcile, None)).outcome

    try:
        # No evidence the run has the steer, and its live call is in flight: neither accepted nor a negative.
        assert await outcome() is SteerOutcome.UNKNOWN
        fail.set()
        assert (await steering).outcome is SteerOutcome.REFUSED
        # The live call returned (its receipt may have been lost) while the run goes on in a long tool:
        # nothing here can settle the attempt any more, so it is not held until the run ends.
        assert await outcome() is SteerOutcome.NOT_ACTIVE
    finally:
        fail.set()
        release.set()
        await running
    assert await outcome() is SteerOutcome.NOT_ACTIVE


async def test_reconcile_settles_an_attempt_another_adapter_instance_opened(engine, session, tmp_path, published) -> None:
    from core.services.agent_steering import SteerReconcileRequest

    harness = _Harness(engine, tmp_path, "avibe", [])
    request = harness.request("long task")
    turn_id = _turn(request.context)
    harness.controller._native_start(request.context)
    [native] = harness.controller.started
    _delivery_id, attempt_id = harness.open_steer("and this", turn_id, native)
    # A retired adapter (the backend disabled and enabled again, even within one second) or a
    # previous process: the native turn id names the instance that ran the Turn, so no clock
    # is needed to tell that no call of this adapter can settle the attempt.
    harness.new_agent()

    receipt = await harness.agent.reconcile_steer_attempt(SteerReconcileRequest(SESSION, turn_id, native, attempt_id), None)

    assert receipt.outcome is SteerOutcome.NOT_ACTIVE


async def test_a_run_ended_by_design_still_answers_the_steer_its_turn_accepted(
    engine, session, tmp_path, published
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def finish(arguments, ctx):
        started.set()
        await release.wait()
        return ToolResult((text("handed off"),), terminate=True)

    call = ToolCallBlock(id="call_1", name="finish", arguments={})
    harness = _Harness(
        engine, tmp_path, "telegram",
        [[Done(assistant("", calls=(call,)))], [Done(assistant("Docs checked too."))]],
        tools=[FakeTool("finish", execute=finish)],
    )
    request = harness.request("wrap up")
    turn_id = _turn(request.context)
    running = asyncio.create_task(harness.agent.handle_message(request))
    await started.wait()
    native = harness.controller.started[0]
    delivery_id, attempt_id = harness.open_steer("also check the docs", turn_id, native)
    receipt = await harness.agent.steer_active_turn(
        SteerRequest(SESSION, turn_id, native, "also check the docs", attempt_id=attempt_id),
        ActiveSteerTarget("runtime", turn_id, request.context, request, harness.agent),
    )
    assert receipt.outcome is SteerOutcome.ACCEPTED
    release.set()
    harness.accept_steer(delivery_id, attempt_id, turn_id)
    await running

    # The terminating tool ended the run before its queue drained; the Turn answers the steer.
    rows = await harness.context_rows()
    assert [(entry.kind, entry.row_id) for entry in rows[2:]] == [
        ("tool_result", rows[2].row_id),
        ("input", delivery_id),
        ("response", rows[4].row_id),
    ]
    assert harness.provider.requests[1].messages == project(rows[:4]).messages
    assert harness.controller.im_client.sent.count("Docs checked too.") == 1
    assert harness.controller.terminals == [
        {"turn": turn_id, "is_error": False, "settled_by": "terminal_result"}
    ]


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
    receipts: list[Optional[str]] = []

    async def finish(_request, *, terminal_emoji=None) -> None:
        receipts.append(terminal_emoji)

    async def delete_ack_message(_request, **_kwargs) -> None:
        receipts.append("ack deleted")

    harness.controller.processing_indicator = SimpleNamespace(finish=finish, delete_ack_message=delete_ack_message)
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
    # The ack message goes at native start, and Stop leaves IM's ⏹️ receipt, as for the other backends.
    assert receipts[:2] == ["ack deleted", "⏹️"]
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "stopped"}
    ]
    assert not any("❌" in sent for sent in harness.controller.im_client.sent)


async def test_resume_settles_open_calls_and_admits_unconsumed_inputs_before_the_next_call(
    engine, session, tmp_path, published
) -> None:
    jobs = FakeJobHost()
    rendered: list[tuple] = []

    async def render(call, job_id, status, watch_id):
        rendered.append((call.id, job_id, status.state, watch_id))
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs,
        create_tools=lambda jobs_, sink: [FakeTool("bash"), FakeTool("edit")],
        render_recovered=render,
        find_job=lambda session_id, call_id, **_: "job_1" if (session_id, call_id) == (SESSION, "call_bash") else None,
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
    steer_id, attempt = harness.open_steer("and the docs", _turn(first.context), harness.controller.started[-1])
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


async def test_resume_renders_a_recovered_steer_as_the_live_path_would(engine, session, tmp_path, published) -> None:
    from types import MethodType

    from core.handlers.message_handler import MessageHandler
    from core.session_turns import SessionTurnManager

    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok"))]])
    # The live steer path's owner of sender facts: the Turn owner and the message handler.
    owner = SessionTurnManager(
        harness.controller, build_context=lambda sid: _context("avibe", sid, turn_id="", delivery_id="")
    )
    owner._engine = engine
    harness.controller.session_turns._steer_input_metadata = owner._steer_input_metadata
    handler = SimpleNamespace(_source_session_id=MessageHandler._source_session_id)
    harness.controller.message_handler = SimpleNamespace(
        prepare_input_metadata=MethodType(MessageHandler.prepare_input_metadata, handler)
    )
    first = harness.request("watch the deploy")
    turn_id = _turn(first.context)
    harness.controller._native_start(first.context)
    await harness.agent.store.consume_input(
        SESSION, first.context.platform_specific["delivery_id"], UserMessage((text("watch the deploy"),))
    )
    with engine.begin() as conn:
        # Another Session's agent steers this Turn; the process exits before the loop consumes it.
        steer = message_deliveries.insert_delivery(
            conn,
            delivery_id="dlv_agent_steer",
            session_id=SESSION,
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=_SCOPES["avibe"], session_id=SESSION, platform="avibe", author="harness",
                source="harness", message_type="harness", text="the deploy finished",
                metadata={"scheduled_provenance": {
                    "task_execution_id": "run_1",
                    "platform_specific": {"task_trigger_kind": "agent_run", "source_session_id": "ses_source"},
                }},
            ),
            dispatch_text="the deploy finished",
            now=NOW,
        )
        assert message_deliveries.open_steer_attempt(
            conn, steer["id"], expected_version=int(steer["version"]), turn_id=turn_id,
            attempt_id="att_agent_steer", expected_native_turn_id=harness.controller.started[-1],
        )
        assert message_deliveries.materialize_steer_acceptance(
            conn, leader_delivery_id=steer["id"], expected_attempt_id="att_agent_steer", turn_id=turn_id, evidence={}
        )

    harness.new_agent()
    await harness.agent.handle_message(harness.request("anything new?"))

    [recovered] = [entry for entry in await harness.context_rows() if entry.row_id == "dlv_agent_steer"]
    assert "From: #ses_source" in recovered.message.content[-1].text


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


async def test_a_fork_settles_a_call_it_inherited_open_with_the_sources_result(
    engine, session, tmp_path, published
) -> None:
    # The source's job is gone (J5 pruned it once the source's call settled).
    suite = ToolSuite(
        jobs=FakeJobHost(),
        create_tools=lambda jobs_, sink: [FakeTool("bash")],
        render_recovered=_unused_renderer,
        find_job=lambda *_args, **_kwargs: None,
    )
    call = ToolCallBlock(id="call_1", name="bash", arguments={"command": "make release"})
    source = _Harness(
        engine, tmp_path, "avibe", [[Done(assistant("Releasing.", calls=(call,)))], [Done(assistant("Released."))]],
        tools=[FakeTool("bash", result=ToolResult((text("release 1.2.0 published"),)))],
    )
    await source.agent.handle_message(source.request("ship it"))
    narration = (await source.context_rows())[1]
    with engine.begin() as conn:
        # A fork from the narration message: its anchor precedes the call's result.
        _insert_session(
            conn,
            "ses_child",
            _SCOPES["avibe"],
            {
                "created_via": "session_fork",
                "fork_source_session_id": SESSION,
                "fork_source_message_id": narration.row_id,
                "fork_source_context_seq": resolve_fork_anchor_seq(conn, SESSION, narration.row_id),
            },
        )
    child = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok"))]], suite=suite, session_id="ses_child")

    await child.agent.handle_message(child.request("what happened?"))

    rows = await child.context_rows()
    assert [(entry.session_id, entry.kind) for entry in rows[:3]] == [
        (SESSION, "input"), (SESSION, "response"), ("ses_child", "tool_result")
    ]
    # The child settles with what the source recorded, not with an uncertain effect.
    assert rows[2].message.content == (text("release 1.2.0 published"),)
    assert child.provider.requests[0].messages == project(rows[:4]).messages


async def test_a_nested_fork_takes_the_result_of_its_own_call_instance(engine, session, tmp_path, published) -> None:
    # Grandparent SESSION -> parent ses_parent -> child ses_child; providers reuse the call id.
    reused = ToolCallBlock(id="call_1", name="bash", arguments={})
    grandparent = _Harness(
        engine, tmp_path, "avibe",
        [[Done(assistant("hello"))], [Done(assistant("", calls=(reused,)))], [Done(assistant("checked"))]],
        tools=[FakeTool("bash", result=ToolResult((text("the grandparent's own check"),)))],
    )
    await grandparent.agent.handle_message(grandparent.request("hi"))
    [anchor] = grandparent.rows("result")
    with engine.begin() as conn:
        _insert_session(conn, "ses_parent", _SCOPES["avibe"], {
            "created_via": "session_fork",
            "fork_source_session_id": SESSION,
            "fork_source_message_id": anchor["id"],
            "fork_source_context_seq": resolve_fork_anchor_seq(conn, SESSION, anchor["id"]),
        })
    started, release = asyncio.Event(), asyncio.Event()

    async def release_build(arguments, ctx):
        started.set()
        await release.wait()
        return ToolResult((text("release 1.2.0 published"),))

    parent = _Harness(
        engine, tmp_path, "avibe",
        [[Done(assistant("Releasing.", calls=(reused,)))], [Done(assistant("Released."))]],
        tools=[FakeTool("bash", execute=release_build)], session_id="ses_parent",
    )
    running = asyncio.create_task(parent.agent.handle_message(parent.request("ship it")))
    await started.wait()
    # The grandparent continues on its own and settles its own call_1 first.
    await grandparent.agent.handle_message(grandparent.request("check something"))
    release.set()
    await running
    narration = next(entry for entry in await parent.context_rows() if entry.session_id == "ses_parent"
                     and entry.kind == "response")
    with engine.begin() as conn:
        _insert_session(conn, "ses_child", _SCOPES["avibe"], {
            "created_via": "session_fork",
            "fork_source_session_id": "ses_parent",
            "fork_source_message_id": narration.row_id,
            "fork_source_context_seq": resolve_fork_anchor_seq(conn, "ses_parent", narration.row_id),
        })
    suite = ToolSuite(
        jobs=FakeJobHost(), create_tools=lambda jobs_, sink: [FakeTool("bash")],
        render_recovered=_unused_renderer, find_job=lambda *_args, **_kwargs: None,
    )
    child = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok"))]], suite=suite, session_id="ses_child")

    await child.agent.handle_message(child.request("what happened?"))

    [settled] = [entry for entry in await child.context_rows()
                 if entry.session_id == "ses_child" and entry.kind == "tool_result"]
    assert settled.message.content == (text("release 1.2.0 published"),)


async def test_a_google_hop_is_called_over_chat_at_the_gateway_prefix(engine, session, tmp_path, published) -> None:
    asked: list[str] = []
    harness = _Harness(
        engine, tmp_path, "avibe", [[Done(assistant("hi"))]],
        providers=lambda protocol: asked.append(protocol) or harness.provider,
    )
    harness.controller.hub_protocol = "google"

    await harness.agent.handle_message(harness.request("hello"))

    # Model Hub's google hop is its /v1beta Gemini surface; the agent speaks Chat to /v1.
    [request] = harness.provider.requests
    assert asked == ["openai_chat"]
    assert (request.endpoint.protocol, request.endpoint.base_url, request.endpoint.provider) == (
        "openai_chat", "http://hub.invalid/avibe/v1", "test-provider"
    )
    assert harness.controller.terminals[-1]["is_error"] is False


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
    reported: list[str] = []

    async def record_native_failure(context, diagnostic) -> bool:
        reported.append(diagnostic)
        return False

    harness.controller.model_hub_runtime.record_native_failure = record_native_failure

    await harness.agent.handle_message(harness.request("say nothing"))
    await harness.agent.handle_message(harness.request("say something unsafe"))

    sent = harness.controller.im_client.sent
    empty_copy = i18n_t("avibeAgent.error.emptyResponse", language)
    refusal_copy = i18n_t("avibeAgent.error.refusal", language)
    # Each failed final carries its own explanation in its row.
    assert sent == [empty_copy, refusal_copy]
    assert sorted(row["content_text"] for row in harness.rows("error")) == sorted([empty_copy, refusal_copy])
    assert [terminal["is_error"] for terminal in harness.controller.terminals] == [True, True]
    # A final that failed by itself is a failed Turn for Model Hub too.
    assert len(reported) == 2


async def test_a_run_failure_is_reported_to_model_hub_like_the_other_backends(engine, session, tmp_path, published) -> None:
    from core.agent_core.ai.provider import ProviderError

    reported: list[str] = []

    async def record_native_failure(context, diagnostic) -> bool:
        reported.append(diagnostic)
        return False

    harness = _Harness(
        engine, tmp_path, "telegram", [[ProviderError("invalid_request", "the served response broke its protocol", False)]]
    )
    harness.controller.model_hub_runtime.record_native_failure = record_native_failure

    await harness.agent.handle_message(harness.request("hello"))

    # The served attempt is replaced by the backend's terminal failure, so the Hub's copy can apply.
    assert len(reported) == 1 and harness.controller.terminals[-1]["is_error"] is True


async def test_an_unreadable_image_is_listed_by_path_and_the_turn_runs(engine, session, tmp_path, published) -> None:
    from modules.im.base import FileAttachment

    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("seen"))], [Done(assistant("still here"))]])
    request = harness.request("look")
    gone = str(tmp_path / "deleted.png")
    request.files = [FileAttachment(name="deleted.png", mimetype="image/png", local_path=gone)]

    await harness.agent.handle_message(request)
    await harness.agent.handle_message(harness.request("and now?"))

    # Listed by path as the native backends pass it; the Session is not stuck on it.
    first = (await harness.context_rows())[0]
    assert f"- File: {gone} (image/png)" in first.message.content[-1].text
    assert harness.controller.im_client.sent == ["seen", "still here"]


async def test_resume_sends_a_recovered_initial_input_as_its_turns_dispatch_text(
    engine, session, tmp_path, published
) -> None:
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("ok"))]])
    first = harness.request("deploy it")
    turn_id = _turn(first.context)
    with engine.begin() as conn:
        # What the Turn owner dispatches carries notes the displayed row does not.
        conn.execute(
            update(session_turns).where(session_turns.c.id == turn_id).values(
                dispatch_text="deploy it\n\n[Attachment download errors]\n- notes.pdf: timed out"
            )
        )
    # The process exits after native acceptance, before the loop consumed the input.
    harness.controller._native_start(first.context)

    harness.new_agent()
    await harness.agent.handle_message(harness.request("well?"))

    recovered = (await harness.context_rows())[0]
    assert recovered.row_id == first.context.platform_specific["delivery_id"]
    assert "[Attachment download errors]" in recovered.message.content[-1].text


async def test_a_session_is_titled_from_its_first_prompt(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("done"))]])

    await harness.agent.handle_message(harness.request("Refactor the parser"))

    def title() -> Optional[str]:
        with engine.connect() as conn:
            return conn.execute(select(agent_sessions.c.title).where(agent_sessions.c.id == SESSION)).scalar()

    # As the native backends title a session without metadata: derived from the first prompt.
    await _until(lambda: bool(title()), "the Session was never titled")
    assert title().startswith("Refactor")


async def test_a_route_model_hub_refuses_mid_run_shows_the_hubs_copy(engine, session, tmp_path, published) -> None:
    from core.handlers.model_hub.service import ModelHubError
    from modules.agents.model_hub import launch_refusal_copy

    from core.handlers.model_hub.service import produce_turn_outcome

    refused = ModelHubError("engine_down", status=503, turn_outcome=produce_turn_outcome("turn.engine_down"))
    harness = _Harness(engine, tmp_path, "telegram", _tool_turn())
    resolve, calls = harness.controller._resolve, []

    async def route(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise refused
        return await resolve(*args, **kwargs)

    # Every model call resolves again; the second one (after the tool batch) is refused.
    harness.controller.model_hub_runtime = SimpleNamespace(resolve=route)

    await harness.agent.handle_message(harness.request("look"))

    copy = launch_refusal_copy(harness.controller, refused)
    assert copy and harness.controller.im_client.sent[-1] == f"❌ {copy}"


async def test_an_im_stop_finds_the_turn_by_its_runtime_key(engine, session, tmp_path, published) -> None:
    started = asyncio.Event()

    async def wait_for_cancel(arguments, ctx):
        started.set()
        await ctx.cancel.wait()
        return ToolResult((text("Command aborted"),), is_error=True)

    harness = _Harness(engine, tmp_path, "telegram", _tool_turn(), tools=[FakeTool("echo", execute=wait_for_cancel)])
    request = harness.request("run it")
    running = asyncio.create_task(harness.agent.handle_message(request))
    await started.wait()
    # IM /stop: the raw channel context names no Session, only the runtime identity.
    raw = MessageContext(user_id="u1", channel_id="C1", platform="telegram", platform_specific={})
    stop = AgentRequest(
        context=raw, message="stop", user_message="", working_path=str(tmp_path),
        base_session_id=request.base_session_id, composite_session_id=request.composite_session_id,
        session_key=request.session_key,
    )

    assert await harness.agent.handle_stop(stop) is True
    await running
    assert harness.controller.terminals[-1]["settled_by"] == "stopped"


async def test_a_turn_queued_behind_a_stopped_one_settles_the_call_it_left_open(
    engine, session, tmp_path, published
) -> None:
    started, closing, close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(arguments, ctx):
        started.set()
        await ctx.cancel.wait()
        raise asyncio.CancelledError()

    class _SlowClose(ScriptedProvider):
        async def aclose(self) -> None:
            closing.set()
            await close.wait()

    provider = _SlowClose([[Done(assistant("", calls=(ToolCallBlock(id="call_1", name="echo", arguments={}),)))],
                           [Done(assistant("next answer"))]])
    harness = _Harness(engine, tmp_path, "telegram", [], tools=[FakeTool("echo", execute=wait_for_cancel)],
                       providers=lambda protocol: provider)
    first = harness.request("run it")
    stopped = asyncio.create_task(harness.agent.handle_message(first))
    await started.wait()
    await harness.agent.handle_stop(AgentRequest(**{**first.__dict__, "message": "stop"}))
    await closing.wait()
    # The next Turn arrives while the stopped one is still closing its provider.
    second = asyncio.create_task(harness.agent.handle_message(harness.request("and now?")))
    await asyncio.sleep(0.05)
    close.set()
    await stopped
    await second

    rows = await harness.context_rows()
    results = [entry for entry in rows if entry.kind == "tool_result"]
    assert [entry.message.tool_call_id for entry in results] == ["call_1"]
    assert results[0].context_seq < next(entry.context_seq for entry in rows if entry.row_id.startswith("dlv_turn_2"))


@pytest.mark.parametrize("phase", ["terminal_result", "preflight_failure"])
async def test_a_failure_while_settling_reaches_the_shared_owner(engine, session, tmp_path, published, phase) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("the answer"))]])
    if phase == "preflight_failure":
        async def refuse(*_args, **_kwargs):
            raise RuntimeError("no source serves this model")

        harness.controller.model_hub_runtime = SimpleNamespace(resolve=refuse)
    emit = harness.controller.emit_agent_message

    async def broken_terminal(context, message_type, text, *args, **kwargs):
        if message_type in ("result", "error", "notify"):
            raise RuntimeError("the settings lookup failed")
        return await emit(context, message_type, text, *args, **kwargs)

    harness.controller.emit_agent_message = broken_terminal

    # AgentService's fallback releases the runtime gate only if the failure reaches it.
    with pytest.raises(RuntimeError, match="settings lookup"):
        await harness.agent.handle_message(harness.request("hello"))


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


# --- settlement and run shape -----------------------------------------------------------


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

    suite = ToolSuite(jobs=jobs, create_tools=tools, render_recovered=_unused_renderer, find_job=lambda *a: None)
    harness = _Harness(engine, tmp_path, "avibe", _tool_turn(), suite=suite)
    harness.controller.config.show_duration = True

    await harness.agent.handle_message(harness.request("start it"))

    # The reply committed before cleanup failed, so only settlement knows the run failed: the
    # dispatcher writes the failed outcome into that row, as for any failed terminal result.
    [result] = harness.rows("error")
    assert result["content_text"] == "两个文件。"
    assert json.loads(result["content_json"])["result_footer"] == "❌ failed"
    assert (await harness.context_rows())[-1].row_id == result["id"]
    assert harness.controller.terminals[-1]["is_error"] is True


@pytest.mark.parametrize("platform", ["avibe", "telegram"])
async def test_a_silent_final_shows_nothing_and_completes(engine, session, tmp_path, published, platform) -> None:
    harness = _Harness(engine, tmp_path, platform, [[Done(assistant("<silent>nothing to add</silent>"))]])

    await harness.agent.handle_message(harness.request("fyi"))

    # The reply stays in the context as a hidden response: no transcript row, inbox reply, or
    # unread result, as the other backends persist nothing visible for a silent reply.
    final = (await harness.context_rows())[-1]
    assert final.message.content[0] == text("<silent>nothing to add</silent>")
    assert harness.rows("result") == [] and harness.rows("error") == []
    [hidden] = [row for row in harness.rows("assistant") if row["id"] == final.row_id]
    assert hidden["content_text"] == ""
    assert harness.controller.im_client.sent == []
    assert harness.controller.terminals[-1]["is_error"] is False


async def test_a_silent_final_whose_run_failed_after_its_commit_is_typed_by_its_row(
    engine, session, tmp_path, published
) -> None:
    class _LeakyJobs(FakeJobHost):
        async def kill(self, job_id, *, reason="killed") -> None:
            raise RuntimeError("the job's process group cannot be signalled")

    def tools(tracking, sink):
        async def start_and_leave(arguments, ctx):
            await tracking.start("sleep 600", cwd=ctx.cwd, env={}, timeout_s=None, session_id=ctx.session_id,
                                 tool_call_id=ctx.tool_call_id)
            return ToolResult((text("started"),))

        return [FakeTool("echo", execute=start_and_leave)]

    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    suite = ToolSuite(jobs=_LeakyJobs(), create_tools=tools, render_recovered=_unused_renderer, find_job=lambda *a: None)
    harness = _Harness(
        engine, tmp_path, "avibe",
        [[Done(assistant("", calls=(call,)))], [Done(assistant("<silent>nothing to add</silent>"))]], suite=suite,
    )

    await harness.agent.handle_message(harness.request("start it"))

    # The failure settles through the committed final itself, never a second notice beside it.
    final = (await harness.context_rows())[-1]
    assert harness.rows("result") == []
    [row] = harness.rows("error")
    assert row["id"] == final.row_id and row["content_text"].strip()
    assert harness.controller.terminals[-1]["is_error"] is True


async def test_a_relative_tool_path_is_shown_under_the_runs_cwd(engine, session, tmp_path, published) -> None:
    from modules.agents.avibe.agent import _relative_to

    project = tmp_path / "project"
    shown = _relative_to(str(project))
    # The tool reads src/app.py under the run's cwd; the line shows it as such, not under the controller's cwd.
    assert shown("src/app.py") == "src/app.py"
    assert shown(str(project / "core" / "foo.py")) == "core/foo.py"
    assert shown("/etc/hosts") == "/etc/hosts"


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
    assert harness.agent.store._env_state == {} and harness.agent.store._store._locks == {}
    assert not getattr(harness.agent.store, "_responses", {})


async def test_a_background_final_settles_with_its_committed_text(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("the nightly summary"))]])
    request = harness.request("summarize the night")
    request.context.platform_specific["suppress_delivery"] = True

    await harness.agent.handle_message(request)

    assert harness.controller.im_client.sent == []
    assert harness.controller.terminal_texts == ["the nightly summary"]
    assert harness.controller.terminals[-1]["is_error"] is False


async def test_a_stop_while_the_input_is_prepared_starts_no_run(engine, session, tmp_path, published) -> None:
    from modules.im.base import FileAttachment

    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    harness = _Harness(engine, tmp_path, "telegram", [[Done(assistant("an answer nobody wants"))]])
    preparing, release = asyncio.Event(), asyncio.Event()
    snapshot = harness.agent.media.snapshot

    async def slow_snapshot(*args, **kwargs):
        preparing.set()
        await release.wait()
        return await snapshot(*args, **kwargs)

    harness.agent.media.snapshot = slow_snapshot
    request = harness.request("look at this")
    request.files = [FileAttachment(name="shot.png", mimetype="image/png", local_path=str(image))]
    running = asyncio.create_task(harness.agent.handle_message(request))
    await preparing.wait()

    assert await harness.agent.handle_stop(AgentRequest(**{**request.__dict__, "message": "stop"})) is True
    release.set()
    await running

    # The acknowledged Stop holds: no model call, nothing sent, the Turn settles as stopped.
    assert harness.provider.requests == []
    assert harness.controller.im_client.sent == []
    assert harness.controller.terminals == [
        {"turn": _turn(request.context), "is_error": False, "settled_by": "stopped"}
    ]


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


async def test_the_config_refresh_registers_and_retires_the_backend(engine, monkeypatch) -> None:
    from config.v2_compat import to_app_config
    from config.v2_config import V2Config
    from core.agent_auth_service import AgentAuthService

    agents: dict[str, Any] = {}
    recovered: list[AvibeAgent] = []

    async def recover(agent) -> None:
        recovered.append(agent)

    # A backend enabled after startup missed the startup recovery (T2, J5); it runs on registration.
    monkeypatch.setattr(AvibeAgent, "recover_runtime_state", recover)

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
    assert len(recovered) == 2 and recovered[-1] is agents["avibe"]


async def test_startup_hands_an_orphaned_foreground_job_to_its_watch(engine, session, tmp_path, published) -> None:
    jobs = FakeJobHost()
    rendered: list[tuple] = []

    async def render(call, job_id, status, watch_id):
        rendered.append((call.id, job_id, watch_id))
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs,
        create_tools=lambda jobs_, sink: [FakeTool("bash")],
        render_recovered=render,
        find_job=lambda session_id, call_id, **_: "job_1" if (session_id, call_id) == (SESSION, "call_bash") else None,
    )
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    # The crashed process left a running foreground command, and no Turn will resume this
    # Session: startup itself must settle the open call.
    request = harness.request("run the slow suite")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run the slow suite"),))
    )
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": "pytest -q"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    jobs.states["job_1"] = JobStatus("running")

    # A pass that failed before it settled anything runs again in full.
    harness.new_agent()
    scan = harness.agent._sessions_with_open_tail

    def unavailable():
        raise RuntimeError("database is locked")

    harness.agent._sessions_with_open_tail = unavailable
    with pytest.raises(RuntimeError):
        await harness.agent.recover_runtime_state()
    assert harness.agent.recovering
    harness.agent.stop_recovery()
    harness.agent._sessions_with_open_tail = scan
    await harness.agent.recover_runtime_state()
    rows = await harness.context_rows()
    assert rows[-1].kind == "tool_result" and "now Watch watch_job_1" in rows[-1].message.content[0].text
    # Another process start settles nothing twice.
    harness.new_agent()
    await harness.agent.recover_runtime_state()

    assert await harness.context_rows() == rows
    assert rendered == [("call_bash", "job_1", "watch_job_1")] and jobs.watches == {"job_1": "watch_job_1"}
    assert harness.provider.requests == []


async def test_startup_recovers_a_session_routed_to_another_agent_since(engine, session, tmp_path, published) -> None:
    jobs = FakeJobHost()

    async def render(call, job_id, status, watch_id):
        return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

    suite = ToolSuite(
        jobs=jobs,
        create_tools=lambda jobs_, sink: [FakeTool("bash")],
        render_recovered=render,
        find_job=lambda session_id, call_id, **_: "job_1" if (session_id, call_id) == (SESSION, "call_bash") else None,
    )
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    request = harness.request("run the slow suite")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run the slow suite"),))
    )
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": "pytest -q"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    jobs.states["job_1"] = JobStatus("running")
    with engine.begin() as conn:
        # The user switched this Session to another Agent while the command ran; then the process died.
        conn.execute(
            update(agent_sessions).where(agent_sessions.c.id == SESSION).values(agent_name="codex", agent_backend="codex")
        )

    harness.new_agent()
    await harness.agent.recover_runtime_state()

    assert jobs.watches == {"job_1": "watch_job_1"}
    assert (await harness.context_rows())[-1].kind == "tool_result"


async def test_recovered_tool_results_keep_the_agent_that_made_the_call(engine, session, tmp_path, published) -> None:
    harness = _Harness(engine, tmp_path, "avibe", [])
    request = harness.request("run the slow suite")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run the slow suite"),))
    )
    harness.agent.store.bind_agent(SESSION, "builder")
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": "pytest -q"})
    owner = await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    harness.agent.store.bind_agent(SESSION, None)
    with engine.begin() as conn:
        # The Session is switched to another Agent; then the process restarts with the call open.
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == SESSION).values(agent_name="reviewer"))

    harness.new_agent()
    await harness.agent.recover_runtime_state()

    # Startup T2 writes outside any Turn: the result takes the Agent of the response that made the call.
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.author_name).where(messages.c.id == owner.row_id)).scalar() == "builder"
        [recovered] = conn.execute(
            select(agent_events.c.agent_name).where(
                agent_events.c.session_id == SESSION, agent_events.c.event_type == "tool_result"
            )
        ).scalars().all()
    assert recovered == "builder"


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


async def test_startup_recovery_never_waits_for_a_running_session(engine, session, tmp_path, published) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(arguments, ctx):
        started.set()
        await release.wait()
        return ToolResult((text("ok"),))

    harness = _Harness(engine, tmp_path, "avibe", _tool_turn(), tools=[FakeTool("echo", execute=slow)])
    running = asyncio.create_task(harness.agent.handle_message(harness.request("work")))
    await started.wait()
    try:
        # The running Session's own resume already did T2 and T3; startup recovery must not block on it.
        await asyncio.wait_for(harness.agent.recover_runtime_state(), timeout=2)
    finally:
        release.set()
        await running


# --- narration is live best-effort output; failed finals carry error semantics -------------


async def test_im_narration_is_live_best_effort_output(engine, session, tmp_path, published) -> None:
    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    shown = _Harness(engine, tmp_path, "telegram", [[Done(assistant("First look.", calls=(call,)))], [Done(assistant("done"))]])
    await shown.agent.handle_message(shown.request("go"))
    # Shown live through the process log, like the other backends.
    assert shown.controller.im_client.routes[0] == ("C1", "message", "First look.")

    failing = _Harness(engine, tmp_path, "telegram", [[Done(assistant("Second look.", calls=(call,)))], [Done(assistant("ok"))]])
    failing.controller.im_client.fail_sends = {1}
    await failing.agent.handle_message(failing.request("again"))
    failing.new_agent()
    await failing.agent.recover_runtime_state()
    # Best effort: a failed narration send is not retried; the final reply was delivered.
    assert "Second look." not in failing.controller.im_client.sent
    assert failing.controller.im_client.sent.count("ok") == 1

    hidden = _Harness(engine, tmp_path, "telegram", [[Done(assistant("Third look.", calls=(call,)))], [Done(assistant("fine"))]])
    hidden.controller.settings_manager.hidden = {"assistant"}
    await hidden.agent.handle_message(hidden.request("once more"))
    assert "Third look." not in hidden.controller.im_client.sent


@pytest.mark.parametrize("final, row_type", [("answer", "result"), ("empty", "error"), ("refusal", "error")])
async def test_a_final_is_stored_with_its_outcome_semantics(
    engine, session, tmp_path, published, final, row_type
) -> None:
    message = {
        "answer": assistant("here it is"),
        "empty": assistant(""),
        "refusal": AssistantMessage((), ORIGIN, "refusal"),
    }[final]
    harness = _Harness(engine, tmp_path, "avibe", [[Done(message)]])

    await harness.agent.handle_message(harness.request("answer me"))

    [row] = harness.rows(row_type)
    assert json.loads(row["content_json"])["kind"] == row_type
    # The row is still the Turn's final response in the context and in delivery.
    entries = await harness.context_rows()
    assert entries[-1].kind == "response" and entries[-1].row_id == row["id"]
    assert [name for name, payload in published if name == "message.new" and payload.get("id") == row["id"]] == [
        "message.new"
    ]


# --- the dispatcher writes a committed row's display instead of a second row ---------------

_SHIP = "Shipped.\n\n---\n[✅ Merge] | [👀 Review]"


@pytest.mark.parametrize(
    "platform, suppressed, site",
    [
        ("avibe", False, "result"),
        ("telegram", False, "result"),
        ("avibe", True, "result"),
        ("telegram", True, "result"),
        ("avibe", False, "assistant"),
        ("telegram", True, "assistant"),
    ],
)
async def test_every_persist_site_writes_the_committed_row_instead_of_a_second_one(
    engine, session, tmp_path, published, platform, suppressed, site
) -> None:
    call = ToolCallBlock(id="call_1", name="echo", arguments={})
    narration, final = (_SHIP, "ok") if site == "assistant" else ("Looking.", _SHIP)
    harness = _Harness(
        engine, tmp_path, platform, [[Done(assistant(narration, calls=(call,)))], [Done(assistant(final))]]
    )
    request = harness.request("ship it")
    request.context.platform_specific["suppress_delivery"] = suppressed

    await harness.agent.handle_message(request)

    responses = {entry.row_id: entry.message for entry in await harness.context_rows() if entry.kind == "response"}
    agent_rows = harness.rows("assistant") + harness.rows("result") + harness.rows("error")
    # Every row the dispatcher persisted for the agent is a committed response, once.
    assert sorted(row["id"] for row in agent_rows) == sorted(responses)
    [row] = [row for row in agent_rows if row["type"] == site]
    body = json.loads(row["content_json"])
    assert body["kind"] == site and body["text"] == row["content_text"]
    # The model payload is untouched: the context still reads the raw reply.
    assert responses[row["id"]].content[0] == text(_SHIP)
    assert json.loads(row["metadata_json"]).get("delivery_suppressed", False) is suppressed
    if site == "result":
        # The dispatcher's display copy, as for any backend: the quick replies left the text.
        assert row["content_text"].startswith("Shipped.") and "[✅ Merge]" not in row["content_text"]
        if platform == "avibe":
            assert body["quick_replies"] == ["✅ Merge", "👀 Review"]


async def _committed_final(harness: _Harness, body: str):
    """A final response the store committed, before anything delivered it."""
    request = harness.request("summarize")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("summarize"),))
    )
    return request, await harness.agent.store.append_response(SESSION, assistant(body), final=True)


def _announced(published, row_id: str) -> int:
    return sum(1 for name, payload in published if name == "message.new" and payload.get("id") == row_id)


@pytest.mark.parametrize("platform", ["avibe", "telegram"])
async def test_a_retried_output_is_recognized_by_the_committed_row_it_wrote(
    engine, session, tmp_path, published, platform
) -> None:
    from core.message_output import MessageOutput

    harness = _Harness(engine, tmp_path, platform, [])
    request, final = await _committed_final(harness, "the summary")
    output = MessageOutput(idempotency_key="reply", persisted_row_id=final.row_id)

    for _ in range(2):
        await harness.controller.emit_agent_message(request.context, "result", "the summary", output=output)

    # The committed row carries the output's identity, so the retry is a duplicate.
    [row] = harness.rows("result")
    assert row["id"] == final.row_id and row["native_message_id"] == output.native_message_id(request.context)
    if platform == "avibe":
        assert _announced(published, final.row_id) == 1
    else:
        assert harness.controller.im_client.sent == ["the summary"]


async def test_a_committed_row_never_takes_a_native_id_another_row_holds(engine, session, tmp_path, published) -> None:
    from core.message_mirror import persist_agent_message

    harness = _Harness(engine, tmp_path, "avibe", [])
    request, first = await _committed_final(harness, "first")
    later = await harness.agent.store.append_response(SESSION, assistant("later"), final=True)
    persist_agent_message(request.context, "result", "first", native_message_id="out_1", existing_row_id=first.row_id)

    # A concurrent duplicate of the same output, written into a different committed row.
    persist_agent_message(request.context, "result", "later", native_message_id="out_1", existing_row_id=later.row_id)

    rows = {row["id"]: row for row in harness.rows("result")}
    assert rows[first.row_id]["native_message_id"] == "out_1"
    assert rows[later.row_id]["native_message_id"] is None
    assert json.loads(rows[later.row_id]["content_json"])["kind"] == "result"


async def test_a_suppressed_committed_row_becomes_a_receipt_when_its_output_is_sent(
    engine, session, tmp_path, published
) -> None:
    from core.message_output import MessageOutput

    harness = _Harness(engine, tmp_path, "avibe", [])
    request, final = await _committed_final(harness, "the nightly summary")
    output = MessageOutput(idempotency_key="reply", persisted_row_id=final.row_id)
    background = MessageContext(
        **{**request.context.__dict__, "platform_specific": {**request.context.platform_specific, "suppress_delivery": True}}
    )

    await harness.controller.emit_agent_message(background, "result", "the nightly summary", output=output)
    [local] = harness.rows("result")
    assert json.loads(local["metadata_json"])["delivery_suppressed"] is True
    assert _announced(published, final.row_id) == 0

    await harness.controller.emit_agent_message(request.context, "result", "the nightly summary", output=output)
    [shown] = harness.rows("result")
    assert shown["id"] == final.row_id and shown["native_message_id"] == local["native_message_id"]
    assert "delivery_suppressed" not in json.loads(shown["metadata_json"])
    assert _announced(published, final.row_id) == 1


# --- real wiring: the merged tools, job host and Watch hand-over ---------------------------


async def _real_job(suite, command: str, *, call_id: str, cwd: Path) -> str:
    return await suite.jobs.start(
        command,
        cwd=str(cwd),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout_s=None,
        session_id=SESSION,
        tool_call_id=call_id,
    )


async def _until(predicate, what: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(what)
        await asyncio.sleep(0.05)


_TEXT_STREAMS = {
    "anthropic": (
        "/avibe/v1/messages",
        'data: {"type":"message_start","message":{"usage":{}}}\n\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'data: {"type":"message_stop"}\n\n',
    ),
    "openai_responses": (
        "/avibe/v1/responses",
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1"}}\n\n'
        'data: {"type":"response.output_text.delta","output_index":0,"delta":"hi"}\n\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":null}}\n\n',
    ),
    "openai_chat": (
        "/avibe/v1/chat/completions",
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n",
    ),
}


@pytest.mark.parametrize("hub_protocol", ["anthropic", "openai_responses", "openai_chat", "google"])
async def test_a_turn_runs_through_the_real_provider_registry(engine, session, tmp_path, published, hub_protocol) -> None:
    import httpx

    from modules.agents.avibe.models import registry_providers

    wire = "openai_chat" if hub_protocol == "google" else hub_protocol
    path, body = _TEXT_STREAMS[wire]
    served = json.dumps({"provider": "upstream", "api": wire, "model": "served-model"}, separators=(",", ":"))
    seen: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-avibe-served-hop": served}, text=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        harness = _Harness(
            engine, tmp_path, "telegram", [], providers=registry_providers(media_loader=None, client=client)
        )
        harness.controller.hub_protocol = hub_protocol
        await harness.agent.handle_message(harness.request("hello"))

    # Each protocol's adapter from the real registry speaks to its gateway path; a google hop is
    # called over Chat at the gateway prefix; the served origin comes from x-avibe-served-hop.
    assert seen == [path]
    final = (await harness.context_rows())[-1].message
    assert final.content == (TextBlock(text="hi"),)
    assert (final.origin.provider, final.origin.api, final.origin.model) == ("upstream", wire, "served-model")
    assert harness.controller.im_client.sent == ["hi"]


async def test_a_turn_runs_bash_through_the_real_job_host(engine, session, tmp_path, published) -> None:
    command = "printf 'hi from bash'"
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": command})
    harness = _Harness(
        engine,
        tmp_path,
        "avibe",
        [[Done(assistant("", calls=(call,)))], [Done(assistant("done"))]],
        suite=local_tool_suite(str(tmp_path / "jobs")),
    )

    await harness.agent.handle_message(harness.request("say hi"))

    rows = await harness.context_rows()
    assert [entry.kind for entry in rows] == ["input", "response", "tool_result", "response"]
    assert "hi from bash" in rows[2].message.content[0].text and not rows[2].message.is_error
    assert "hi from bash" in str(harness.provider.requests[1].messages[-1])


async def test_bash_runs_with_the_turns_caller_environment(engine, session, tmp_path, published) -> None:
    from core.caller_context import AVIBE_CALLER_BACKEND_ENV, AVIBE_SESSION_ID_ENV, CALLER_CONTEXT_ENV_NAMES

    # Resolved through PATH, as ordinary project commands and the vibe CLI are.
    command = "env | sort"
    call = ToolCallBlock(id="call_env", name="bash", arguments={"command": command})
    harness = _Harness(
        engine, tmp_path, "telegram",
        [[Done(assistant("", calls=(call,)))], [Done(assistant("done"))]],
        suite=local_tool_suite(str(tmp_path / "jobs")),
    )
    request = harness.request("show the environment")

    await harness.agent.handle_message(request)

    output = (await harness.context_rows())[2].message.content[0].text
    assert not (await harness.context_rows())[2].message.is_error
    seen = dict(line.split("=", 1) for line in output.splitlines() if "=" in line and line.split("=", 1)[0].isupper())
    # The service's environment: PATH and HOME, so the command resolved at all.
    assert seen.get("PATH") and seen.get("HOME")
    # The Turn's caller provenance, as the other backends give their shells.
    assert seen[AVIBE_SESSION_ID_ENV] == SESSION
    assert seen[AVIBE_CALLER_BACKEND_ENV] == "avibe"
    assert {key for key in seen if key in CALLER_CONTEXT_ENV_NAMES} >= {AVIBE_SESSION_ID_ENV, AVIBE_CALLER_BACKEND_ENV}
    # The Model Hub gateway token stays in the adapter: never in a command's environment.
    assert "hub-token" not in output


@pytest.mark.parametrize("state", ["exited", "running"])
async def test_resume_settles_a_real_bash_job_through_the_real_renderer(
    engine, session, tmp_path, published, state
) -> None:
    from core.watches import ManagedWatchStore

    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    request = harness.request("run it")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run it"),))
    )
    command = "printf done" if state == "exited" else "sleep 30"
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": command})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    # The crashed process had started the command; nothing committed its result.
    job_id = await _real_job(suite, command, call_id="call_bash", cwd=tmp_path)
    try:
        if state == "exited":
            await _until(lambda: suite.jobs.status(job_id).state == "exited", "the command never exited")

        harness.new_agent()
        await harness.agent.recover_runtime_state()

        result = (await harness.context_rows())[-1]
        assert result.kind == "tool_result"
        body = result.message.content[0].text
        if state == "exited":
            assert "done" in body and not result.message.is_error
        else:
            watch_id = ManagedWatchStore().find_job_watch(job_id)
            assert watch_id and f"now Watch {watch_id}" in body
            assert suite.jobs.status(job_id).state == "running"
    finally:
        if suite.jobs.status(job_id).state == "running":
            await suite.jobs.kill(job_id)


async def test_resume_settles_a_reused_call_id_from_the_job_its_own_call_started(
    engine, session, tmp_path, published, monkeypatch
) -> None:
    import secrets

    import core.agent_core.tools.jobs as jobs_module
    from core.watches import ManagedWatchStore

    # The earlier call's job id sorts first, so a lookup by (session, call id) alone finds it.
    job_ids, token_hex = iter(["0" * 16, "f" * 16]), secrets.token_hex
    monkeypatch.setattr(jobs_module.secrets, "token_hex", lambda n=None: next(job_ids) if n == 8 else token_hex(n))
    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    first = harness.request("print it")
    harness.controller._native_start(first.context)
    await harness.agent.store.consume_input(
        SESSION, first.context.platform_specific["delivery_id"], UserMessage((text("print it"),))
    )
    reused = ToolCallBlock(id="call_0", name="bash", arguments={"command": "printf old"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(reused,)), final=False)
    old = await _real_job(suite, "printf old", call_id="call_0", cwd=tmp_path)
    await _until(lambda: suite.jobs.status(old).state == "exited", "the first command never exited")
    await harness.agent.store.append_tool_result(
        SESSION, ToolResultMessage("call_0", "bash", (text("old"),)), details={"job_id": old}
    )
    await harness.agent.store.append_response(SESSION, assistant("printed"), final=True)
    # The next Turn's provider reuses call_0; the crash leaves its command running.
    second = harness.request("wait for it")
    harness.controller._native_start(second.context)
    await harness.agent.store.consume_input(
        SESSION, second.context.platform_specific["delivery_id"], UserMessage((text("wait for it"),))
    )
    again = ToolCallBlock(id="call_0", name="bash", arguments={"command": "sleep 30"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(again,)), final=False)
    current = await _real_job(suite, "sleep 30", call_id="call_0", cwd=tmp_path)
    try:
        harness.new_agent()
        await harness.agent.recover_runtime_state()

        result = (await harness.context_rows())[-1]
        watch_id = ManagedWatchStore().find_job_watch(current)
        assert result.kind == "tool_result" and watch_id and f"now Watch {watch_id}" in result.message.content[0].text
        assert ManagedWatchStore().find_job_watch(old) is None
    finally:
        await suite.jobs.kill(current)


async def test_startup_hands_a_foreground_job_to_its_watch_with_the_backend_disabled(
    engine, session, tmp_path, published
) -> None:
    from core.controller import Controller
    from core.watches import ManagedWatchStore

    harness = _Harness(engine, tmp_path, "avibe", [], suite=local_tool_suite())
    request = harness.request("run it")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run it"),))
    )
    call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": "sleep 30"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    suite = harness.suite
    job_id = await _real_job(suite, "sleep 30", call_id="call_bash", cwd=tmp_path)
    # The process restarts with agents.avibe disabled: no adapter is registered.
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(agents={}),
        config=SimpleNamespace(avibe=None, platform="avibe", language="en"),
        im_client=None,
        settings_manager=SimpleNamespace(),
    )
    try:
        await Controller._recover_avibe_agent_runtime_state(controller)

        watch_id = ManagedWatchStore().find_job_watch(job_id)
        assert watch_id is not None
        result = (await harness.context_rows())[-1]
        assert result.kind == "tool_result" and f"now Watch {watch_id}" in result.message.content[0].text
    finally:
        if suite.jobs.status(job_id).state == "running":
            await suite.jobs.kill(job_id)


async def _two_sessions_with_open_calls(engine, tmp_path, suite) -> tuple[_Harness, str, str]:
    """SESSION's open call has a running job; ses_b's has one that exited."""
    with engine.begin() as conn:
        _insert_session(conn, "ses_b", _SCOPES["avibe"])
    jobs = {}
    for session_id, command in ((SESSION, "sleep 30"), ("ses_b", "printf done")):
        harness = _Harness(engine, tmp_path, "avibe", [], suite=suite, session_id=session_id)
        request = harness.request("run it")
        harness.controller._native_start(request.context)
        await harness.agent.store.consume_input(
            session_id, request.context.platform_specific["delivery_id"], UserMessage((text("run it"),))
        )
        call = ToolCallBlock(id="call_bash", name="bash", arguments={"command": command})
        await harness.agent.store.append_response(session_id, assistant("", calls=(call,)), final=False)
        jobs[session_id] = await suite.jobs.start(
            command, cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_s=None, session_id=session_id, tool_call_id="call_bash",
        )
    await _until(lambda: suite.jobs.status(jobs["ses_b"]).state == "exited", "ses_b's job never exited")
    return harness, jobs[SESSION], jobs["ses_b"]


def _hand_over_failing_once(monkeypatch) -> list[str]:
    """Watch hand-over raises on its first call, as a transient DB or Watch failure would."""
    import core.watches as watches_module
    import modules.agents.avibe.agent as agent_module

    calls: list[str] = []
    real = watches_module.hand_over_job

    async def flaky(meta, **kwargs):
        calls.append(str(meta.get("job_id")))
        if len(calls) == 1:
            raise RuntimeError("the Watch store is locked")
        return await real(meta, **kwargs)

    monkeypatch.setattr(watches_module, "hand_over_job", flaky)
    monkeypatch.setattr(agent_module, "_RECOVERY_RETRY_DELAYS_S", (0.05, 0.05), raising=False)
    monkeypatch.setattr(agent_module, "_RECOVERY_RETRY_PERIOD_S", 0.05, raising=False)
    return calls


async def _results(engine, session_id: str) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(
            select(agent_events.c.content_text).where(
                agent_events.c.session_id == session_id, agent_events.c.event_type == "tool_result"
            )
        ).scalars())


async def test_startup_recovery_retries_a_session_it_could_not_settle(engine, session, tmp_path, published, monkeypatch) -> None:
    from core.watches import ManagedWatchStore

    _hand_over_failing_once(monkeypatch)
    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness, running, _exited = await _two_sessions_with_open_calls(engine, tmp_path, suite)
    agent = harness.new_agent()
    try:
        # The first pass settles ses_b and fails SESSION; it reports that, not success.
        assert await agent.recover_runtime_state() == [SESSION]
        assert len(await _results(engine, "ses_b")) == 1
        # Without any Turn, a retry hands SESSION's running job to its Watch.
        await _until(lambda: ManagedWatchStore().find_job_watch(running) is not None, "the retry never handed over")
        await _until(lambda: not agent.recovering, "the retry never finished")
        assert len(await _results(engine, SESSION)) == 1 and len(await _results(engine, "ses_b")) == 1
    finally:
        agent.stop_recovery()
        await suite.jobs.kill(running)


async def test_startup_recovery_retries_with_the_backend_disabled(engine, session, tmp_path, published, monkeypatch) -> None:
    from core.controller import Controller
    from core.watches import ManagedWatchStore

    _hand_over_failing_once(monkeypatch)
    harness, running, _exited = await _two_sessions_with_open_calls(engine, tmp_path, local_tool_suite())
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(agents={}),
        config=SimpleNamespace(avibe=None, platform="avibe", language="en"),
        im_client=None,
        settings_manager=SimpleNamespace(),
    )
    try:
        await Controller._recover_avibe_agent_runtime_state(controller)
        # The unregistered adapter is kept while its retry runs, and the retry settles the Session.
        assert controller._avibe_recovery_agent.recovering
        await _until(lambda: ManagedWatchStore().find_job_watch(running) is not None, "the retry never handed over")
    finally:
        controller._avibe_recovery_agent.stop_recovery()
        await harness.suite.jobs.kill(running)


async def test_the_default_job_host_lives_in_the_watch_jobs_dir(engine, session, tmp_path) -> None:
    from core.watches import agent_jobs_dir

    controller = _Controller(engine, "avibe")
    suite = AvibeAgent(controller, engine=engine)._tools()
    job_id = await _real_job(suite, "true", call_id="call_true", cwd=tmp_path)
    try:
        # vibe stop's stop_all_jobs and the job-Watch sweep both look in this one directory.
        assert Path(suite.jobs.output_path(job_id)).is_relative_to(Path(os.path.realpath(agent_jobs_dir())))
    finally:
        await _until(lambda: suite.jobs.status(job_id).state != "running", "the job never ended")


async def test_startup_prunes_only_jobs_whose_call_and_watch_have_settled(engine, session, tmp_path, published) -> None:
    from core.watches import ManagedWatchStore

    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness = _Harness(engine, tmp_path, "avibe", [], suite=suite)
    request = harness.request("run them")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run them"),))
    )
    calls = tuple(ToolCallBlock(id=f"call_{name}", name="bash", arguments={"command": "printf out"}) for name in "abc")
    await harness.agent.store.append_response(SESSION, assistant("", calls=calls), final=False)
    # d's call is in no committed response, so nothing can ever settle it.
    jobs = {name: await _real_job(suite, "printf out", call_id=f"call_{name}", cwd=tmp_path) for name in "abcd"}
    for job_id in jobs.values():
        await _until(lambda job_id=job_id: suite.jobs.status(job_id).state == "exited", "a job never exited")
    # a and b have durable results and b's Watch still owns it; c's call is still open.
    for name in "ab":
        await harness.agent.store.append_tool_result(
            SESSION, ToolResultMessage(f"call_{name}", "bash", (text("ok"),)), details={"job_id": jobs[name]}
        )
    # e reuses a's call id after a's result committed (a model call apart): a's result does not settle it.
    await asyncio.sleep(0.01)
    jobs["e"] = await _real_job(suite, "printf out", call_id="call_a", cwd=tmp_path)
    await _until(lambda: suite.jobs.status(jobs["e"]).state == "exited", "a job never exited")
    await suite.jobs.hand_over(jobs["b"])
    assert ManagedWatchStore().job_watch_settled(jobs["b"]) is False
    week_ago = time.time() - 8 * 24 * 3600
    for job_id in jobs.values():
        for entry in Path(suite.jobs.output_path(job_id)).parent.iterdir():
            os.utime(entry, (week_ago, week_ago))

    harness.new_agent()
    await harness.agent.recover_runtime_state()

    # Startup settles c's open call from its job's output (T2) before any file goes.
    [c_result] = [entry for entry in await harness.context_rows() if entry.kind == "tool_result"][-1:]
    assert c_result.message.tool_call_id == "call_c" and "out" in c_result.message.content[0].text
    present = {name: Path(suite.jobs.output_path(job_id)).parent.exists() for name, job_id in jobs.items()}
    # J5: a job is removed only once its call has a durable result and no Watch still manages it.
    assert present == {"a": False, "b": True, "c": False, "d": True, "e": True}



async def test_a_running_service_prunes_settled_jobs_after_its_runs(engine, session, tmp_path, published) -> None:
    suite = local_tool_suite(str(tmp_path / "jobs"))
    harness = _Harness(engine, tmp_path, "avibe", [[Done(assistant("done"))]], suite=suite)
    request = harness.request("run it")
    harness.controller._native_start(request.context)
    await harness.agent.store.consume_input(
        SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("run it"),))
    )
    call = ToolCallBlock(id="call_old", name="bash", arguments={"command": "printf out"})
    await harness.agent.store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    job_id = await _real_job(suite, "printf out", call_id="call_old", cwd=tmp_path)
    await _until(lambda: suite.jobs.status(job_id).state == "exited", "the job never exited")
    await harness.agent.store.append_tool_result(
        SESSION, ToolResultMessage("call_old", "bash", (text("out"),)), details={"job_id": job_id}
    )
    await harness.agent.store.append_response(SESSION, assistant("finished"), final=True)
    job_dir = Path(suite.jobs.output_path(job_id)).parent
    week_ago = time.time() - 8 * 24 * 3600
    for entry in job_dir.iterdir():
        os.utime(entry, (week_ago, week_ago))

    # No restart: the service has been up since the job ran, and the next Turn ends.
    await harness.agent.handle_message(harness.request("anything else?"))

    await _until(lambda: not job_dir.exists(), "a settled job outlived its retention while the service ran")
