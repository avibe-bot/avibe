"""A silent final reply never displaces the Turn's earlier visible text.

A Turn can show text that does not end it: a completed phase a later input
joined (``output``), or substantial narration (``interim``). When the Turn then
ends on a reply that is empty once ``<silent>`` blocks are stripped, that text
is the Turn's result: typed, rendered, and notified as one, exactly once.
"""

from __future__ import annotations

import json
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
from tests.test_message_dispatcher_interim import FIVE_LINES, FOUR_LINES
from tests.test_message_dispatcher_platform_limits import _StubController

SESSION = "ses_silent_final"
NOW = "2026-10-18T03:00:00Z"
REPORT = "CI is green on the exact head and the review thread is resolved."
PHASE = MessageOutput(completes_turn=False, completes_run=False)


class _Controller(_StubController):
    def __init__(self, platform: str):
        super().__init__(platform)
        self.settings_manager = SimpleNamespace(sessions=None)
        self.dispatcher = ConsolidatedMessageDispatcher(self)

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


class SilentFinalTurnResultTests(unittest.IsolatedAsyncioTestCase):
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
        """A fresh store holding one Workbench Session, and nothing published yet."""
        _ScheduledPush.pushes = []
        self.published: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AVIBE_HOME": home}):
            dispose_cached_sqlite_engines()
            ensure_sqlite_state()
            self.seed_session(SESSION)
            try:
                yield
            finally:
                dispose_cached_sqlite_engines()

    @staticmethod
    def seed_session(session_id: str) -> None:
        engine = create_sqlite_engine()
        try:
            with engine.begin() as conn:
                scope_id = upsert_scope(
                    conn, platform="avibe", scope_type="project", native_id="proj_silent", now=NOW
                )
                conn.execute(
                    agent_sessions.insert().values(
                        id=session_id, scope_id=scope_id, agent_backend="claude", agent_variant="default",
                        session_anchor=f"anchor_{session_id}", native_session_id="", status="active",
                        metadata_json="{}", created_at=NOW, updated_at=NOW, last_active_at=NOW,
                    )
                )
        finally:
            engine.dispose()

    @staticmethod
    def context(platform: str = "avibe", turn: str = "trn_1") -> MessageContext:
        avibe = platform == "avibe"
        return MessageContext(
            user_id="workbench" if avibe else "u1",
            channel_id=SESSION if avibe else "c1",
            platform=platform,
            platform_specific={"agent_session_id": SESSION, "turn_token": turn},
        )

    @staticmethod
    def _visible_rows(session_id: str, *columns):
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                return list(
                    conn.execute(
                        select(*columns)
                        .where(messages.c.session_id == session_id, messages.c.author == "agent")
                        .where(messages.c.type != "assistant")
                        .order_by(messages.c.created_at)
                    )
                )
        finally:
            engine.dispose()

    def agent_rows(self, session_id: str = SESSION) -> list[tuple[str, str]]:
        return [
            (row.type, row.content_text)
            for row in self._visible_rows(session_id, messages.c.type, messages.c.content_text)
        ]

    def content_kinds(self) -> list[str]:
        return [json.loads(row.content_json)["kind"] for row in self._visible_rows(SESSION, messages.c.content_json)]

    async def test_a_silent_final_makes_the_last_visible_text_the_result(self):
        """Scenario: MESSAGE-DELIVERY-325."""
        cases = [
            # A completed phase another input joined, then a silent answer to that input.
            ("output", REPORT, "avibe", ("output", REPORT, {"output": PHASE})),
            # Substantial narration shown as a Web bubble before the silent final.
            ("interim", FIVE_LINES, "avibe", ("assistant", FIVE_LINES, {})),
            # IM delivered the output as a message already; its row becomes the result.
            ("output", REPORT, "slack", ("output", REPORT, {"output": PHASE})),
        ]
        for shown_type, text, platform, (message_type, body, kwargs) in cases:
            with self.subTest(shown_type=shown_type, platform=platform), self.state():
                controller = _Controller(platform)
                context = self.context(platform)
                await controller.emit_agent_message(context, message_type, body, **kwargs)
                self.assertEqual(self.agent_rows(), [(shown_type, text)])

                # The base result path emits a ``<silent>`` final as an empty reply;
                # a retried terminal must neither change nor notify anything again.
                agent = _Agent(controller)
                for _ in range(2):
                    await agent.emit_result_message(context, "<silent>nothing to add</silent>")

                self.assertEqual(self.agent_rows(), [("result", text)])
                self.assertEqual(self.content_kinds(), ["result"])
                updated = [payload for name, payload in self.published if name == "message.updated"]
                if platform == "avibe":
                    [row] = updated
                    self.assertEqual((row["type"], row["text"]), ("result", text))
                    self.assertEqual(
                        [push["message_id"] for push in _ScheduledPush.pushes], [row["id"]]
                    )
                    inbox = [payload for name, payload in self.published if name == "inbox.session.updated"]
                    self.assertEqual(inbox[-1]["unread_count"], 1)
                else:
                    self.assertEqual(updated, [])
                    self.assertEqual(_ScheduledPush.pushes, [])

    async def test_concurrent_turns_in_one_channel_each_promote_their_own_text(self):
        """Scenario: MESSAGE-DELIVERY-325."""
        with self.state():
            for thread in ("t1", "t2"):
                self.seed_session(f"ses_{thread}")
            controller = _Controller("slack")
            agent = _Agent(controller)
            threads = {
                "t1": MessageContext(
                    user_id="u1", channel_id="c1", platform="slack", thread_id="t1",
                    platform_specific={"agent_session_id": "ses_t1", "turn_token": "trn_t1"},
                ),
                "t2": MessageContext(
                    user_id="u1", channel_id="c1", platform="slack", thread_id="t2",
                    platform_specific={"agent_session_id": "ses_t2", "turn_token": "trn_t2"},
                ),
            }
            for thread, context in threads.items():
                await controller.emit_agent_message(context, "output", f"Report from {thread}.", output=PHASE)
            for context in threads.values():
                await agent.emit_result_message(context, "<silent>noted</silent>")

            for thread in threads:
                self.assertEqual(self.agent_rows(f"ses_{thread}"), [("result", f"Report from {thread}.")])

    async def test_nothing_else_promotes_earlier_text(self):
        """Scenario: MESSAGE-DELIVERY-325."""
        request = SimpleNamespace(output=None)
        report = ("output", REPORT, {"output": PHASE})
        silent_reply = {"text": "", "output": terminal_turn_output()}
        cases = {
            "a visible final is the result itself": (
                report, {"text": "Merged."}, [("output", REPORT), ("result", "Merged.")], 1,
            ),
            "a stop has no result": (
                report, {"text": "", "level": "silent", "output": stop_output_for(request)},
                [("output", REPORT)], 0,
            ),
            "a failure settles without one": (
                report, {"text": "", "is_error": True, "level": "silent", "output": terminal_turn_output()},
                [("output", REPORT)], 0,
            ),
            "a settlement is not a reply": (
                report, {"text": "", "level": "silent", "output": terminal_turn_output()},
                [("output", REPORT)], 0,
            ),
            "another Turn's final": (
                report, {**silent_reply, "context": self.context(turn="trn_2")}, [("output", REPORT)], 0,
            ),
            # A per-step preamble stays in Activity: it was never visible text.
            "narration kept in Activity": (("assistant", FOUR_LINES, {}), silent_reply, [], 0),
        }
        for name, ((message_type, body, kwargs), final, expected_rows, pushes) in cases.items():
            with self.subTest(name), self.state():
                controller = _Controller("avibe")
                await controller.emit_agent_message(self.context(), message_type, body, **kwargs)
                final = dict(final)
                context = final.pop("context", self.context())
                text = final.pop("text")
                await controller.emit_agent_message(context, "result", text, **final)

                self.assertEqual(self.agent_rows(), expected_rows)
                self.assertEqual(len(_ScheduledPush.pushes), pushes)
                self.assertNotIn("message.updated", [name for name, _ in self.published])


if __name__ == "__main__":
    unittest.main()
