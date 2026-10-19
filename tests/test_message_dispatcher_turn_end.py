"""A reply is not the end of its Turn (docs/plans/reply-and-turn-end.md).

Scenario: MESSAGE-DELIVERY-334. A reply is a ``result`` whether or not its emit
also ends the Turn, so a Turn can reply more than once; only the Turn-end emit
settles the Run, writes the Turn snapshot, commits the Turn, and finalizes the
IM surfaces. A Turn end with no reply of its own settles each Run with the latest
reply recorded on that Run's output ledger, under its own id, appending nothing.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_output import MessageOutput, stop_output_for, terminal_turn_output
from modules.agents.base import AgentRequest, BaseAgent
from modules.im import MessageContext
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
        self.dispatcher = ConsolidatedMessageDispatcher(self)
        # The Turn's snapshot, and its commit: the commit runs once the Turn end armed it.
        self.snapshots: list[dict] = []
        self.committed_turns: list[MessageContext] = []
        # Runs a steer attached to the Turn after it started (the Turn owner's record).
        self.steered_runs: list[str] = []
        self.session_turns = SimpleNamespace(
            on_terminal_result=lambda _context, **kwargs: self.snapshots.append(kwargs),
            on_terminal_delivery_complete=self.committed_turns.append,
            accepted_agent_run_ids_for_turn=lambda _turn_id: list(self.steered_runs),
        )

    async def emit_agent_message(self, *args, **kwargs):
        return await self.dispatcher.emit_agent_message(*args, **kwargs)


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

    def running_run(self, task_id: str) -> str:
        run = self.runs.enqueue_task_run(task_id)
        assert self.runs.claim(run.id) is not None
        return run.id

    def harness_run(self, run_id: str | None = None) -> dict:
        return self.runs.get_run(run_id or self.run_id)

    def ledger(self, run_id: str | None = None) -> list[str]:
        payload = self.harness_run(run_id).get("result_payload") or {}
        return [entry["id"] for entry in payload.get("outputs") or []]

    def pushed_texts(self) -> list[str]:
        return [push["body"] for push in _ScheduledPush.pushes]

    async def test_a_turn_replies_twice_and_ends_once_after_both(self):
        """Scenario: MESSAGE-DELIVERY-334.

        The reply before a steer stays a reply, the steered input's answer is a
        second one, and the Turn ends once, after both: on its own final reply,
        or on a content-free end that settles the Run with the last reply.
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
                [snapshot] = controller.snapshots
                # The snapshot carries what the end itself said.
                own_text = SECOND if ends_with == "its final reply" else ""
                self.assertEqual((snapshot["is_error"], snapshot["terminal_evidence"]["result_text"]), (False, own_text))
                self.assertEqual(len(controller.committed_turns), 1)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"]), ("succeeded", SECOND))
                # Each reply is on the Run's output ledger once.
                outputs = [entry["text"] for entry in run["result_payload"]["outputs"]]
                self.assertEqual(outputs, [FIRST, SECOND])

    async def test_a_silent_final_settles_with_the_last_reply_and_promotes_nothing(self):
        """Scenario: MESSAGE-DELIVERY-334."""
        for platform in ("avibe", "slack"):
            with self.subTest(platform=platform), self.state():
                controller = _Controller(platform)
                context = self.context(platform)
                await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
                # The base result path emits a ``<silent>`` final as the content-free end.
                await _Agent(controller).emit_result_message(context, "<silent>nothing to add</silent>")

                self.assertEqual(self.agent_rows(), [("result", FIRST)])
                self.assertNotIn("message.updated", [name for name, _ in self.published])
                self.assertEqual(self.pushed_texts(), [FIRST] if platform == "avibe" else [])
                [snapshot] = controller.snapshots
                self.assertEqual(snapshot["terminal_evidence"]["result_text"], "")
                self.assertEqual(len(controller.committed_turns), 1)
                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"]), ("succeeded", FIRST))
                self.assertEqual([entry["text"] for entry in run["result_payload"]["outputs"]], [FIRST])

    async def test_a_restart_settles_with_this_turns_reply_not_detached_work(self):
        """Scenario: MESSAGE-DELIVERY-334 (Codex: restrict resumed replies to the current Turn).

        A detached completion recorded through the Turn's own context, and a new
        process that ends the Turn: the Run settles with this Turn's reply.
        """
        for rerun in (False, True):
            with self.subTest(vibey_resume=rerun), self.state():
                controller = _Controller("avibe")
                context = self.context()
                await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
                await controller.emit_agent_message(
                    context, "result", "The nightly job finished.",
                    output=MessageOutput(completes_turn=False, completes_run=False, detached=True),
                )
                # Both are on the Run's ledger; only the first is this Turn's reply.
                self.assertEqual(len(self.ledger()), 2)
                end = self.context()
                end.platform_specific["vibey_resume"] = rerun
                await _Controller("avibe").emit_agent_message(end, "result", "", output=terminal_turn_output())

                run = self.harness_run()
                self.assertEqual((run["status"], run["result_text"]), ("succeeded", FIRST))

    async def test_a_replayed_earlier_reply_never_displaces_a_later_one(self):
        """Scenario: MESSAGE-DELIVERY-334 (Codex: preserve an unpersisted reply across older retries).

        The later reply reached IM though its row was never written; a replay of
        the earlier one, found by its receipt, does not become the Run's answer.
        """
        from core import message_mirror

        def reply(key: str) -> MessageOutput:
            return MessageOutput(completes_turn=False, completes_run=False, idempotency_key=key)

        def persist_all_but_second(context, message_type, text, **kwargs):
            if text == SECOND:
                return None
            return message_mirror.persist_agent_message(context, message_type, text, **kwargs)

        with self.state():
            controller = _Controller("slack")
            context = self.context("slack")
            await controller.emit_agent_message(context, "result", FIRST, output=reply("r1"))
            with mock.patch("core.message_dispatcher.persist_agent_message", side_effect=persist_all_but_second):
                await controller.emit_agent_message(context, "result", SECOND, output=reply("r2"))
            await controller.emit_agent_message(context, "result", FIRST, output=reply("r1"))
            await controller.emit_agent_message(context, "result", "", output=terminal_turn_output())

            self.assertEqual([sent[2] for sent in controller.im_client.sent], [FIRST, SECOND])
            self.assertEqual(self.harness_run()["result_text"], SECOND)
            self.assertEqual(self.ledger(), ["r1", "r2"])

    async def test_each_reply_has_one_ledger_entry_across_a_restart(self):
        """Scenario: MESSAGE-DELIVERY-334 (Codex: reuse the persisted reply output identity after restart).

        Whatever identity a reply was recorded under, a content-free end in a new
        process settles with it and appends nothing.
        """
        with self.state():
            context = self.context()
            await _Controller("avibe").emit_agent_message(context, "result", FIRST, output=REPLY)
            await _Controller("avibe").emit_agent_message(
                context, "result", SECOND,
                output=MessageOutput(completes_turn=False, completes_run=False, idempotency_key="reply-2"),
            )
            recorded = self.ledger()
            rerun = self.context()
            rerun.platform_specific["vibey_resume"] = True
            await _Controller("avibe").emit_agent_message(rerun, "result", "", output=terminal_turn_output())

            run = self.harness_run()
            self.assertEqual((run["status"], run["result_text"]), ("succeeded", SECOND))
            self.assertEqual(len(recorded), 2)
            self.assertEqual(recorded[1], "reply-2")
            self.assertEqual(self.ledger(), recorded)

    async def test_a_run_steered_in_after_the_last_reply_settles_empty(self):
        """Scenario: MESSAGE-DELIVERY-334.

        A Run settles with the latest reply recorded while it was attached. One
        steered in after the Turn's last reply, whose Turn then ends silently, was
        answered by nothing and settles empty, with no callback text.
        """
        with self.state():
            controller = _Controller("avibe")
            context = self.context()
            await controller.emit_agent_message(context, "result", FIRST, output=REPLY)
            steered = self.running_run("task-steered")
            controller.steered_runs.append(steered)
            await _Agent(controller).emit_result_message(context, "<silent>noted</silent>")

            attached, late = self.harness_run(), self.harness_run(steered)
            self.assertEqual((attached["status"], attached["result_text"]), ("succeeded", FIRST))
            self.assertEqual((late["status"], late["result_text"] or ""), ("succeeded", ""))
            self.assertEqual(self.ledger(steered), [])

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
        """Scenario: MESSAGE-DELIVERY-334.

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


if __name__ == "__main__":
    unittest.main()
