"""A reply is not the end of its Turn (docs/plans/reply-and-turn-end.md).

Scenario: MESSAGE-DELIVERY-334. A reply is a ``result`` whether or not its emit
also ends the Turn, so a Turn can reply more than once; only the Turn-end emit
settles the Run, writes the Turn snapshot, commits the Turn, and finalizes the
IM surfaces.

Scenario: MESSAGE-DELIVERY-335. A Turn end with no reply of its own (a silent final,
an empty end, a footer-only end) is content-free: it adds no row, and every attached
Run and the Turn snapshot settle with the Run copy of the Turn's last delivered reply,
which the dispatcher wrote onto the durable Turn when it delivered that reply, or ""
when the Turn delivered none.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.citations import CitationSource, register_citations
from core import message_dispatcher as dispatcher_module
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput, stop_output_for, terminal_turn_output
from core.session_turns import SessionTurnManager
from modules.agents.base import AgentRequest, BaseAgent
from modules.im import MessageContext
from modules.im.formatters.slack_formatter import SlackFormatter
from storage import message_deliveries
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages
from storage.settings_service import upsert_scope
from tests.test_message_dispatcher_interim import FIVE_LINES
from tests.test_message_dispatcher_platform_limits import _StubController
from tests.test_message_dispatcher_status_bubble import (
    _StubController as _BubbleController,
    _ctx as _bubble_context,
    _dispatcher as _bubble_dispatcher,
)

SESSION = "ses_turn_end"
NOW = "2026-10-19T03:00:00Z"
FIRST = "The cache is shared; two writers race. Fixed in #12."
SECOND = "Yes, the fix also covers the CLI path."
REPLY = MessageOutput(completes_turn=False, completes_run=False)


class _Controller(_StubController):
    def __init__(self, platform: str):
        super().__init__(platform)
        self.settings_manager = SimpleNamespace(sessions=None)
        # The duration footer a base result end composes.
        self.config.show_duration = True
        self.im_client.formatter = SlackFormatter()
        self.dispatcher = ConsolidatedMessageDispatcher(self)
        # The Turn's snapshot, and its commit: the commit runs once the Turn end armed it.
        self.snapshots: list[dict] = []
        self.committed_turns: list[MessageContext] = []
        # Runs a steer attached to the Turn (the Turn owner's durable attribution).
        self.steered_runs: list[str] = []
        # The durable Turn row's own reads and writes are the real Turn owner's.
        turns = SessionTurnManager()
        self.session_turns = SimpleNamespace(
            on_terminal_result=lambda _context, **kwargs: self.snapshots.append(kwargs),
            on_terminal_delivery_complete=self.committed_turns.append,
            accepted_agent_run_ids_for_turn=lambda _turn_id: list(self.steered_runs),
            record_turn_reply=turns.record_turn_reply,
            turn_last_reply=turns.turn_last_reply,
        )

    async def emit_agent_message(self, *args, **kwargs):
        return await self.dispatcher.emit_agent_message(*args, **kwargs)


def _live_turn(conn, scope_id: str) -> None:
    """The durable Turn ``trn_1``, accepted and running, as the Turn owner leaves it."""
    delivery = message_deliveries.insert_delivery(
        conn,
        delivery_id="dlv_trn_1",
        session_id=SESSION,
        priority="p3",
        state="reserved",
        snapshot=message_deliveries.message_snapshot(
            scope_id=scope_id, session_id=SESSION, platform="avibe", author="user", source="user",
            message_type="user", text="is the cache safe?",
        ),
        dispatch_text="is the cache safe?",
        now=NOW,
    )
    claimed = message_deliveries.claim_start_batch(
        conn, turn_id="trn_1", session_id=SESSION, backend="claude", deliveries=[delivery],
        dispatch_text="is the cache safe?",
    )
    assert message_deliveries.bind_native_start(
        conn, "trn_1", expected_version=int(claimed["turn"]["version"]), runtime_key="runtime",
        runtime_turn_id="trn_1", native_turn_id="trn_1",
    )
    assert message_deliveries.materialize_start_acceptance(conn, turn_id="trn_1", evidence={"kind": "test"})


@contextmanager
def _no_logs():
    yield


class _Agent(BaseAgent):
    name = "stub"

    async def handle_message(self, request: AgentRequest) -> None:
        return None


class _ScheduledPush:
    """``threading.Thread`` as Web Push uses it: records the push it would send."""

    pushes: list[dict] = []

    def __init__(self, *, target, args, daemon):
        self.payload = args[0]

    def start(self) -> None:
        self.pushes.append(self.payload)


class ReplyAndTurnEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        push_thread = mock.patch(
            "core.web_push_notifications.threading", SimpleNamespace(Thread=_ScheduledPush)
        )
        push_thread.start()
        self.addCleanup(push_thread.stop)
        bus = mock.patch(
            "core.inbox_events.bus.publish",
            side_effect=lambda name, payload: self.published.append((name, payload)),
        )
        bus.start()
        self.addCleanup(bus.stop)

    @contextmanager
    def state(self):
        """A fresh store holding one Workbench Session and one running Harness Run."""
        from core.scheduled_tasks import TaskExecutionStore

        _ScheduledPush.pushes = []
        self.published: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AVIBE_HOME": home}):
            dispose_cached_sqlite_engines()
            ensure_sqlite_state()
            engine = create_sqlite_engine()
            try:
                with engine.begin() as conn:
                    scope_id = upsert_scope(
                        conn, platform="avibe", scope_type="project", native_id="proj_turn_end", now=NOW
                    )
                    conn.execute(
                        agent_sessions.insert().values(
                            id=SESSION, scope_id=scope_id, agent_backend="claude", agent_variant="default",
                            session_anchor=f"anchor_{SESSION}", native_session_id="", status="active",
                            metadata_json="{}", created_at=NOW, updated_at=NOW, last_active_at=NOW,
                        )
                    )
                    _live_turn(conn, scope_id)
            finally:
                engine.dispose()
            self.runs = TaskExecutionStore()
            self.run_id = self.running_run("task-turn-end")
            try:
                yield
            finally:
                dispose_cached_sqlite_engines()

    def context(self, platform: str = "avibe") -> MessageContext:
        avibe = platform == "avibe"
        return MessageContext(
            user_id="workbench" if avibe else "u1",
            channel_id=SESSION if avibe else "c1",
            platform=platform,
            platform_specific={
                "agent_session_id": SESSION,
                "turn_token": "trn_1",
                "task_trigger_kind": "scheduled",
                "task_execution_id": self.run_id,
            },
        )

    @staticmethod
    def agent_rows() -> list[tuple[str, str]]:
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                return [
                    (row.type, row.content_text)
                    for row in conn.execute(
                        select(messages.c.type, messages.c.content_text)
                        .where(messages.c.session_id == SESSION, messages.c.author == "agent")
                        .order_by(messages.c.created_at, messages.c.id)
                    )
                ]
        finally:
            engine.dispose()

    @staticmethod
    def agent_row_ids() -> list:
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                return list(
                    conn.execute(
                        select(messages.c.id).where(messages.c.session_id == SESSION, messages.c.author == "agent")
                    )
                )
        finally:
            engine.dispose()

    def running_run(self, task_id: str) -> str:
        run = self.runs.enqueue_task_run(task_id)
        assert self.runs.claim(run.id) is not None
        return run.id

    def harness_run(self, run_id: str | None = None) -> dict:
        return self.runs.get_run(run_id or self.run_id)

    @staticmethod
    def agent_presentations() -> list[tuple[str, dict]]:
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                return [
                    (row.type, json.loads(row.metadata_json or "{}").get("presentation"))
                    for row in conn.execute(
                        select(messages.c.type, messages.c.metadata_json)
                        .where(messages.c.session_id == SESSION, messages.c.author == "agent")
                        .order_by(messages.c.created_at, messages.c.id)
                    )
                ]
        finally:
            engine.dispose()

    def pushed_texts(self) -> list[str]:
        return [push["body"] for push in _ScheduledPush.pushes]

    async def test_a_turn_replies_twice_and_ends_once_after_both(self):
        """Scenario: MESSAGE-DELIVERY-334 / MESSAGE-DELIVERY-335.

        The reply before a steer stays a reply, the steered input's answer is a
        second one, and the Turn ends once, after both: on its own final reply, or
        on a content-free end.
        """
        for ends_with in ("its final reply", "a content-free end"):
            with self.subTest(ends_with=ends_with), self.state():
                controller = _Controller("avibe")
                context = self.context()
                await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
                # Replying did not end the Turn.
                self.assertEqual((controller.snapshots, controller.committed_turns), ([], []))
                self.assertEqual(self.harness_run()["status"], "running")
                if ends_with == "its final reply":
                    await _Agent(controller).emit_result_message(context, SECOND)
                else:
                    await controller.emit_agent_message(context, "result", SECOND, output=REPLY)
                    await controller.emit_agent_message(context, "result", "", output=terminal_turn_output())

                self.assertEqual(self.agent_rows(), [("result", FIRST), ("result", SECOND)])
                self.assertEqual(self.pushed_texts(), [FIRST, SECOND])
                self.assertNotIn("message.updated", [name for name, _ in self.published])
                # Either way the Turn settles with its last reply: its own final, or the
                # one it delivered before a content-free end (MESSAGE-DELIVERY-335).
                [snapshot] = controller.snapshots
                self.assertEqual((snapshot["is_error"], snapshot["terminal_evidence"]["result_text"]), (False, SECOND))
                self.assertEqual(len(controller.committed_turns), 1)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"]), ("succeeded", SECOND))
                # Each delivered reply is on the Run's output log; a content-free end
                # adds its settlement under the terminal identity, as every final does.
                outputs = [entry["text"] for entry in run["result_payload"]["outputs"]]
                self.assertEqual(outputs[:2], [FIRST, SECOND])
                self.assertEqual(outputs[2:], [] if ends_with == "its final reply" else [SECOND])

    async def test_an_end_without_a_reply_settles_with_the_last_delivered_reply(self):
        """Scenario: MESSAGE-DELIVERY-335 (and MESSAGE-DELIVERY-334: a content-free end
        after a reply promotes nothing).

        Every content-free end of a done Turn (a silent final, an empty end with the
        duration footer shown or hidden, a backend's bare end) adds no row and pushes
        nothing, and the snapshot and every Run attached to the Turn, one a steer
        attached after the reply included, settle with the last reply it delivered:
        the copy the dispatcher wrote (quick replies and file links stripped,
        citations written out), A after A, B, A, read from the durable Turn by the
        end in a new process.
        """
        source = (
            "Two writers raced.\ue200cite\ue202turn0view0\ue201 See [the report](file:///tmp/report.md)."
            "\n\n---\n[👍 Thanks] | [🔁 Rerun]"
        )
        written = "Two writers raced. [example.com](https://example.com/race) See the report."
        registered, citations = register_citations(
            source,
            {"turn0view0": CitationSource(ref_id="turn0view0", title="Race report", url="https://example.com/race")},
            unresolved_label="(source unavailable)",
        )

        async def silent_final(agent, controller, context):
            await agent.emit_result_message(context, "<silent>noted</silent>")

        async def empty_end(show_duration):
            async def end(agent, controller, context):
                controller.config.show_duration = show_duration
                await agent.emit_result_message(context, "", duration_ms=5000)

            return end

        async def bare_end(agent, controller, context):
            await controller.emit_agent_message(context, "result", "", output=terminal_turn_output())

        ends = {
            "a silent final": silent_final,
            "an empty end, duration shown": await empty_end(True),
            "an empty end, duration hidden": await empty_end(False),
            "a bare end": bare_end,
        }
        for platform in ("avibe", "slack"):
            for name, end in ends.items():
                with self.subTest(platform=platform, end=name), self.state():
                    context = self.context(platform)
                    process = _Controller(platform)
                    process.config.reply_enhancements = True
                    await process.emit_agent_message(context, "result", registered, output=REPLY, citations=citations)
                    steered = self.running_run("task-steered")
                    await process.emit_agent_message(context, "result", SECOND, output=REPLY)
                    await process.emit_agent_message(context, "result", registered, output=REPLY, citations=citations)

                    # The Turn ends in a new process: nothing in memory carries the reply.
                    restarted = _Controller(platform)
                    restarted.steered_runs = [steered]
                    await end(_Agent(restarted), restarted, context)

                    self.assertEqual([row[0] for row in self.agent_rows()], ["result"] * 3)
                    self.assertEqual(len(self.pushed_texts()), 3 if platform == "avibe" else 0)
                    self.assertNotIn("message.updated", [event for event, _ in self.published])
                    [snapshot] = restarted.snapshots
                    self.assertEqual(snapshot["terminal_evidence"]["result_text"], written)
                    self.assertEqual(len(restarted.committed_turns), 1)
                    for run_id in (self.run_id, steered):
                        run = self.harness_run(run_id)
                        self.assertEqual((run["status"], run["result_text"]), ("succeeded", written))

    async def test_only_a_delivered_reply_is_what_the_turn_settles_with(self):
        """Scenario: MESSAGE-DELIVERY-335: a reply whose every send failed never reached the user,
        so a content-free end settles with the one before it; a Turn that delivered no reply
        settles with nothing (and so sends no callback), and a failure or a stop reports its
        outcome, never an earlier reply (``test_only_a_reply_is_a_completed_turns_answer``)."""
        for delivered in ((FIRST,), ()):
            with self.subTest(delivered=delivered), self.state():
                controller = _Controller("slack")
                context = self.context("slack")
                send = controller.im_client.send_message

                async def lossy_send(target, text, *args, **kwargs):
                    if SECOND in text:
                        raise RuntimeError("Slack is down")
                    return await send(target, text, *args, **kwargs)

                controller.im_client.send_message = lossy_send
                for reply in delivered:
                    await controller.emit_agent_message(context, "result", reply, output=REPLY)
                await controller.emit_agent_message(context, "result", SECOND, output=REPLY)
                await _Agent(controller).emit_result_message(context, "", duration_ms=5000)

                expected = delivered[-1] if delivered else ""
                [snapshot] = controller.snapshots
                self.assertEqual(snapshot["terminal_evidence"]["result_text"], expected)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"] or ""), ("succeeded", expected))

    async def test_a_turns_last_reply_is_recorded_with_its_receipt(self):
        """Scenario: MESSAGE-DELIVERY-335.

        A reply's record as the Turn's last reply commits in the transaction that writes
        its receipt: a process that dies right after that commit still leaves it for the
        Turn's end; a record that cannot be written fails the receipt and leaves nothing
        behind; a replay of an earlier reply finds its receipt and records nothing, so it
        never displaces a newer one. Each reply is its own delivery by its identity, the
        same text twice included.
        """

        def reply(key: str) -> MessageOutput:
            return MessageOutput(completes_turn=False, completes_run=False, idempotency_key=key)

        for case in ("the process dies after the receipt", "the record fails", "an older reply replays"):
            with self.subTest(case), self.state():
                context = self.context()
                controller = _Controller("avibe")
                await controller.emit_agent_message(context, "result", FIRST, output=reply("out_1"))
                if case == "the process dies after the receipt":
                    real_persist = dispatcher_module.persist_agent_message

                    def persist_then_die(*args, **kwargs):
                        real_persist(*args, **kwargs)
                        raise RuntimeError("the process died after the receipt committed")

                    with mock.patch.object(dispatcher_module, "persist_agent_message", persist_then_die):
                        with self.assertRaises(RuntimeError):
                            await controller.emit_agent_message(context, "result", SECOND, output=reply("out_2"))
                    expected, rows = SECOND, [FIRST, SECOND]
                elif case == "the record fails":
                    with mock.patch(
                        "storage.message_deliveries.record_turn_reply",
                        side_effect=RuntimeError("database is locked"),
                    ):
                        await controller.emit_agent_message(context, "result", SECOND, output=reply("out_2"))
                    expected, rows = FIRST, [FIRST]
                else:
                    await controller.emit_agent_message(context, "result", SECOND, output=reply("out_2"))
                    await controller.emit_agent_message(context, "result", SECOND, output=reply("out_3"))
                    await controller.emit_agent_message(context, "result", FIRST, output=reply("out_1"))
                    expected, rows = SECOND, [FIRST, SECOND, SECOND]

                # The Turn ends in a new process.
                restarted = _Controller("avibe")
                await restarted.emit_agent_message(context, "result", "", output=terminal_turn_output())

                self.assertEqual([text for _, text in self.agent_rows()], rows)
                [snapshot] = restarted.snapshots
                self.assertEqual(snapshot["terminal_evidence"]["result_text"], expected)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"]), ("succeeded", expected))
                if case == "an older reply replays":
                    # Each delivered reply is on the Run's log by its identity: the same
                    # text twice is two entries, and the replay adds none.
                    self.assertEqual(
                        [entry["id"] for entry in run["result_payload"]["outputs"]],
                        ["out_1", "out_2", "out_3", "terminal"],
                    )

    async def test_a_replys_derived_writes_wait_for_its_delivery(self):
        """Scenario: MESSAGE-DELIVERY-335: every write derived from a reply that does not end
        its Turn waits for its delivery evidence. An IM reply whose every send failed leaves
        no Run output entry, no record as the Turn's last reply, and no delivery evidence
        for its backend to mark; the same reply delivered writes each exactly once."""
        with self.state():
            controller = _Controller("slack")
            context = self.context("slack")
            send = controller.im_client.send_message

            async def down(*_args, **_kwargs):
                raise RuntimeError("Slack is down")

            controller.im_client.send_message = down
            evidence = await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
            self.assertIsNone(evidence)
            self.assertEqual((self.harness_run()["result_payload"] or {}).get("outputs") or [], [])
            self.assertIsNone(controller.session_turns.turn_last_reply("trn_1"))

            controller.im_client.send_message = send
            evidence = await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
            self.assertIsNotNone(evidence)
            self.assertEqual([entry["text"] for entry in self.harness_run()["result_payload"]["outputs"]], [FIRST])
            self.assertEqual(controller.session_turns.turn_last_reply("trn_1"), FIRST)
            self.assertEqual(self.agent_rows(), [("result", FIRST)])

    async def test_a_background_emit_returns_its_receipt_or_nothing(self):
        """Scenario: MESSAGE-DELIVERY-335: an emit's return value is its delivery evidence. A
        background (``suppress_delivery``) output's receipt is its local row: its id when the
        row was written, and nothing, never a synthetic id, when the write failed, so a caller
        that waits for evidence (OpenCode's emitted mark, an Activity's delivery check) never
        takes a failed write for a delivery."""
        for persisted in (True, False):
            with self.subTest(persisted=persisted), self.state():
                controller = _Controller("avibe")
                context = self.context()
                context.platform_specific["suppress_delivery"] = True
                if persisted:
                    evidence = await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
                    [row_id] = [row.id for row in self.agent_row_ids()]
                    self.assertEqual(evidence, row_id)
                    self.assertEqual(controller.session_turns.turn_last_reply("trn_1"), FIRST)
                else:
                    with mock.patch.object(dispatcher_module, "persist_agent_message", return_value=None):
                        evidence = await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
                    self.assertIsNone(evidence)
                    self.assertIsNone(controller.session_turns.turn_last_reply("trn_1"))
                    self.assertEqual((self.harness_run()["result_payload"] or {}).get("outputs") or [], [])

    async def test_a_failed_last_reply_read_is_retried_then_read_as_none(self):
        """Scenario: MESSAGE-DELIVERY-335: rule 5 is best effort. A read of the Turn's last
        reply that fails is retried briefly; one that keeps failing is logged and read as
        no reply, so the end settles with "" instead of holding or failing the Turn's end."""
        for failures, expected in ((2, FIRST), (3, "")):
            with self.subTest(failures=failures), self.state():
                context = self.context()
                await _Controller("avibe").emit_agent_message(context, "result", FIRST, output=REPLY)
                restarted = _Controller("avibe")
                read = restarted.session_turns.turn_last_reply
                attempts = []

                def flaky_read(turn_id, read=read, attempts=attempts, failures=failures):
                    attempts.append(turn_id)
                    if len(attempts) <= failures:
                        raise RuntimeError("database is locked")
                    return read(turn_id)

                restarted.session_turns.turn_last_reply = flaky_read
                with self.assertLogs("core.message_dispatcher", level="WARNING") if failures == 3 else _no_logs():
                    await restarted.emit_agent_message(context, "result", "", output=terminal_turn_output())

                self.assertEqual(len(attempts), 3)
                [snapshot] = restarted.snapshots
                self.assertEqual(snapshot["terminal_evidence"]["result_text"], expected)
                self.assertEqual(len(restarted.committed_turns), 1)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"] or ""), ("succeeded", expected))

    async def test_each_message_records_and_follows_its_presentation_decision(self):
        """Scenario: MESSAGE-DELIVERY-337: every message the dispatcher writes records the
        presentation decision it executed (``metadata["presentation"]``): a Web reply
        is a pushed transcript message, long narration keeps a muted ``interim`` copy
        beside its Activity row, and the released ``output`` type stays a muted,
        unpushed transcript row. The decision is what the dispatcher executes: a policy
        that declines the push leaves the reply's row without one."""
        with self.state():
            controller = _Controller("avibe")
            context = self.context()
            await controller.emit_agent_message(context, "assistant", FIVE_LINES)
            await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
            await controller.emit_agent_message(
                context,
                "output",
                "Generated images:\n\n![a](/tmp/a.png)",
                output=MessageOutput(completes_turn=False, completes_run=False, records_run_output=False),
            )

            recorded = {row_type: presentation for row_type, presentation in self.agent_presentations()}
            self.assertEqual(
                recorded,
                {
                    "assistant": {"policy": "default/1", "transcript": True, "muted": True, "web_push": False, "im": "log"},
                    "interim": {"policy": "default/1", "transcript": True, "muted": True, "web_push": False, "im": "log"},
                    "result": {"policy": "default/1", "transcript": True, "muted": False, "web_push": True, "im": "message"},
                    "output": {"policy": "default/1", "transcript": True, "muted": True, "web_push": False, "im": "message"},
                },
            )
            self.assertEqual(self.pushed_texts(), [FIRST])

        class _NoPush:
            id = "no-push/1"

            def __init__(self, inner):
                self.inner = inner

            def decide(self, presentation):
                return replace(self.inner.decide(presentation), policy=self.id, web_push=False)

        with self.state():
            controller = _Controller("avibe")
            controller.dispatcher.presentation_policy = _NoPush(controller.dispatcher.presentation_policy)
            await controller.emit_agent_message(self.context(), "result", FIRST, output=REPLY)

            self.assertEqual(self.agent_rows(), [("result", FIRST)])
            self.assertEqual(self.pushed_texts(), [])
            [(_, presentation)] = self.agent_presentations()
            self.assertEqual((presentation["policy"], presentation["web_push"]), ("no-push/1", False))

    async def test_a_reply_or_a_failure_keeps_its_type_attached_or_detached(self):
        """Scenario: MESSAGE-DELIVERY-334.

        Lifecycle authority never decides the type: a failure is ``error`` whether it
        ends the current Turn or reports detached work, and a detached completion
        keeps ``result`` (its provenance, not its type, says it is detached).
        """
        detached = MessageOutput(completes_turn=False, completes_run=False, detached=True)
        cases = {
            "an attached failure": (terminal_turn_output(), True, "error"),
            "a detached completion": (detached, False, "result"),
            "a detached failure": (detached, True, "error"),
        }
        for name, (output, is_error, expected) in cases.items():
            with self.subTest(name), self.state():
                controller = _Controller("avibe")
                await controller.emit_agent_message(
                    self.context(), "result", FIRST, output=output, is_error=is_error
                )

                self.assertEqual(self.agent_rows(), [(expected, FIRST)])
                [row] = [payload for event, payload in self.published if event == "message.new"]
                self.assertIs(bool(row["metadata"].get("detached")), output.detached)

    async def test_only_a_reply_is_a_completed_turns_answer(self):
        """Scenario: MESSAGE-DELIVERY-334 / MESSAGE-DELIVERY-335.

        Narration stays narration, and a Turn that fails or is stopped ends once
        with its outcome, never an earlier reply as its answer.
        """
        cases = {
            "narration before tool calls": (
                ("assistant", FIVE_LINES, {}),
                {"text": "<silent>noted</silent>", "output": terminal_turn_output()},
                [("assistant", FIVE_LINES)], "succeeded", "",
            ),
            "a failure after a reply": (
                ("result", FIRST, {"output": REPLY}),
                {"text": "", "is_error": True, "level": "silent", "output": terminal_turn_output()},
                [("result", FIRST)], "failed", "",
            ),
            "a stop after a reply": (
                ("result", FIRST, {"output": REPLY}),
                {"text": "", "level": "silent", "output": stop_output_for(SimpleNamespace(output=None))},
                [("result", FIRST)], "running", None,
            ),
        }
        for name, (shown, end, rows, status, result_text) in cases.items():
            with self.subTest(name), self.state():
                controller = _Controller("avibe")
                context = self.context()
                message_type, body, kwargs = shown
                await controller.emit_agent_message(context, message_type, body, **kwargs)
                end = dict(end)
                await controller.emit_agent_message(context, "result", end.pop("text"), **end)

                # ``interim`` is the Web copy of long narration; it is not a reply.
                self.assertEqual([row for row in self.agent_rows() if row[0] != "interim"], rows)
                [snapshot] = controller.snapshots
                self.assertEqual(snapshot["terminal_evidence"]["result_text"], "")
                self.assertEqual(len(controller.committed_turns), 1)
                run = self.harness_run()
                # A stop's own writer settles its Run; the Turn end leaves it alone.
                self.assertEqual((run["status"], run["result_text"] or None), (status, result_text or None))


class _Indicator:
    def __init__(self) -> None:
        self.finished = 0

    async def finish_terminal_turn(self, _context) -> None:
        self.finished += 1


class TurnEndFinalizesImOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_im_surfaces_finalize_once_at_the_turn_end(self):
        """Scenario: MESSAGE-DELIVERY-334.

        A reply that does not end the Turn is its own message and leaves the
        running reaction and the status bubble alone; the Turn's end retires them
        once, whether it carries the final reply or nothing.
        """
        for ends_with in ("its final reply", "a content-free end"):
            with self.subTest(ends_with=ends_with), mock.patch("core.message_dispatcher.persist_agent_message"):
                controller = _BubbleController(platform="slack")
                controller.processing_indicator = _Indicator()
                dispatcher = _bubble_dispatcher(controller)
                context = _bubble_context()
                context.platform_specific = {"turn_token": "trn_1"}
                client = controller.im_client

                await dispatcher.emit_agent_message(context, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
                await dispatcher.emit_agent_message(context, "result", FIRST, output=REPLY)
                self.assertEqual(client.sent[-1], ("msg-2", FIRST, None))
                self.assertEqual((client.deletes, controller.processing_indicator.finished), ([], 0))
                await dispatcher.emit_agent_message(context, "toolcall", "🔧 `Read` `{}`")
                self.assertEqual(client.edits[-1][0], "msg-1")

                if ends_with == "its final reply":
                    await dispatcher.emit_agent_message(context, "result", SECOND, output=terminal_turn_output())
                    self.assertEqual(client.sent[-1], ("msg-3", SECOND, "✅ done"))
                    self.assertEqual(client.deletes, ["msg-1"])
                else:
                    await dispatcher.emit_agent_message(context, "result", "", output=terminal_turn_output())
                    self.assertEqual(client.edits[-1][:2], ("msg-1", ""))
                    self.assertTrue(client.edits[-1][2].startswith("✅ done"))
                    self.assertEqual(len(client.sent), 2)
                self.assertEqual(controller.processing_indicator.finished, 1)

    async def test_a_footer_only_end_posts_nothing_and_still_ends_the_turn(self):
        """Scenario: MESSAGE-DELIVERY-335.

        With the duration shown and nothing to say, the end is content-free: no
        message carries the footer alone, in concise or verbose mode. The Turn still
        visibly ends: the concise status bubble collapses to its done line (with the
        run time), the running indicator finishes, and the request's reaction clears.
        """
        for style in ("concise", "verbose"):
            with self.subTest(style=style), mock.patch("core.message_dispatcher.persist_agent_message"):
                controller = _BubbleController(platform="slack", progress_style=style)
                controller.processing_indicator = _Indicator()
                controller.settings_manager = SimpleNamespace(sessions=None)
                controller.config.show_duration = True
                controller.im_client.formatter = SlackFormatter()
                dispatcher = _bubble_dispatcher(controller)
                controller.emit_agent_message = dispatcher.emit_agent_message
                context = _bubble_context()
                context.platform_specific = {"turn_token": "trn_1"}
                client = controller.im_client
                agent = _Agent(controller)
                agent._remove_ack_reaction = mock.AsyncMock()
                request = SimpleNamespace(output=None, context=context)

                await dispatcher.emit_agent_message(context, "toolcall", "🔧 `Bash` `{}`")
                sent_before = list(client.sent)
                await agent.emit_result_message(context, "", duration_ms=5000, request=request)

                self.assertEqual(client.sent, sent_before)
                if style == "concise":
                    self.assertEqual(client.edits[-1][:2], ("msg-1", ""))
                    self.assertTrue(client.edits[-1][2].startswith("✅ done"))
                self.assertEqual(controller.processing_indicator.finished, 1)
                agent._remove_ack_reaction.assert_awaited_once_with(request)


if __name__ == "__main__":
    unittest.main()
