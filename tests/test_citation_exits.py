"""Every copy of a cited answer that leaves the dispatcher, asked for the token.

A registered citation travels through delivery as an opaque nonce: an identity
no later stage can guess back, minted at the native input boundary where the
marker grammar still means what it says. Each surface then writes that token
out into the form that surface can show - a Markdown link in a body, a bare
host in a structured field - because each surface holds a different text.

That only works if *every* exit writes it. A token that survives into an IM
message, a stored row, an agent-run record, a Turn snapshot or an upload title
is a private handle showing up in text a person reads, and nothing downstream
will ever turn it back into a link. So this file drives the real
``ConsolidatedMessageDispatcher`` over one real producer run against a
temporary SQLite home, collects every string that run handed to something
outside the dispatcher, and asks all of them the same question at once.

The question is NOT "did every private-use character disappear". This answer
carries three that must come through verbatim: the marker grammar shown as a
code example, a marker the stream cut in half, and an unrelated private-use
character the model simply wrote. A sweep that removed those would pass a
census written the lazy way while deleting the answer's own text.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.citations import _SEP, _START, _TOKEN_RE, CitationSource, register_citations
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, messages
from storage.settings_service import upsert_scope
from tests.citation_bridge import UNRESOLVED_LABEL, marker
from tests.test_message_dispatcher_platform_limits import _StubController, _StubIMClient

REF = "turn0view0"
SOURCE_URL = "https://example.com/x"
SOURCES = {REF: CitationSource(ref_id=REF, url=SOURCE_URL, title="Cited page")}

# A marker the stream cut in half. It is not a complete marker, so it is not
# registered and nothing rewrites it - existing behaviour, asserted here so the
# census cannot be satisfied by deleting it.
TRUNCATED = f"{_START}cite{_SEP}{REF}"
# A private-use character that is simply part of the answer. It is neither
# citation grammar nor an internal token.
LOOSE_PUA = ""


def agent_text(report: Path) -> str:
    """One agent reply, spelled the way the backend hands it to registration."""
    cite = marker(REF)
    return (
        # The citation: one complete marker in prose. The only thing here
        # registration may claim.
        f"Cited. {cite}\n\n"
        # The same grammar shown as a code example. The reader is being shown
        # what a marker looks like, so it has to stay what it looks like.
        f"```\n{cite}\n```\n\n"
        f"Cut off: {TRUNCATED}\n\n"
        f"Loose private use: {LOOSE_PUA}\n\n"
        # Two markers inside text that is lifted OUT of the body into fields no
        # surface parses as Markdown - an upload title and a quick-reply label.
        # They are the exits where a token would land somewhere nothing
        # downstream ever rewrites, so they are the ones worth writing a reply
        # around.
        f"[report {cite}](file://{report})\n\n"
        f"---\n[Open {cite}] | [Done]"
    )


class _Exits:
    """Every string one dispatcher run handed to something outside itself."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def note(self, where: str, value: Any) -> None:
        if isinstance(value, str) and value:
            self.rows.append((where, value))

    def of(self, *where: str) -> list[str]:
        return [value for name, value in self.rows if name in where]

    def names(self) -> set[str]:
        return {name for name, _ in self.rows}


class _RecordingIMClient(_StubIMClient):
    """The platform stub, plus the send shapes a cited result actually uses."""

    def __init__(self, exits: _Exits, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.exits = exits

    async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None):
        self.exits.note("im.send", text)
        self.exits.note("im.subtext", subtext)
        return await super().send_message(context, text, parse_mode=parse_mode)

    async def send_message_with_buttons(
        self, context, text, keyboard, parse_mode=None, subtext=None
    ):
        self.exits.note("im.send", text)
        self.exits.note("im.subtext", subtext)
        for row in getattr(keyboard, "buttons", None) or []:
            for button in row:
                self.exits.note("im.button", getattr(button, "text", None))
                self.exits.note("im.button", getattr(button, "callback_data", None))
        return await super().send_message(context, text, parse_mode=parse_mode)

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        self.exits.note("im.edit", text)
        return await super().edit_message(
            context, message_id, text=text, keyboard=keyboard, parse_mode=parse_mode
        )

    async def upload_markdown(self, context, title, content, filetype=None):
        self.exits.note("im.attachment.title", title)
        self.exits.note("im.attachment", content)
        return "bot-attachment-1"

    async def upload_file_from_path(self, context, file_path, title=None, **kwargs):
        self.exits.note("im.upload.title", title)
        return "bot-upload-1"


class _RecordingRunStore:
    """The Harness run ledger, as the dispatcher reaches it."""

    def __init__(self, exits: _Exits) -> None:
        self.exits = exits

    def record_turn_run_outputs(self, run_ids, *, output_id, text, **kwargs):
        self.exits.note("run.output", text)
        self.exits.note("run.output.error", kwargs.get("error"))

    def record_run_message(self, run_id, *, text, message_id=None, terminal_status=None):
        self.exits.note("run.message", text)

    def close(self):
        return None


class _RecordingTurns:
    """The Turn snapshot a later steer reads the result back out of."""

    def __init__(self, exits: _Exits) -> None:
        self.exits = exits

    def on_terminal_result(self, context, *, is_error, settled_by, terminal_evidence):
        self.exits.note("turn.snapshot", terminal_evidence.get("result_text"))
        self.exits.note("turn.snapshot.error", terminal_evidence.get("terminal_error"))

    def accepted_agent_run_ids_for_turn(self, turn_id):
        return []

    def on_terminal_delivery_complete(self, context):
        return None


class _RecordingController(_StubController):
    """A stub controller that also owns a live SSE sink and a Turn manager."""

    def __init__(
        self,
        platform: str,
        exits: _Exits,
        *,
        max_bytes: int | None = None,
        progress_style: str = "verbose",
    ) -> None:
        super().__init__(platform, max_bytes=max_bytes)
        self.exits = exits
        self._progress_style_value = progress_style
        self.im_client = _RecordingIMClient(
            exits, max_bytes=max_bytes, supports_editing=platform != "wechat"
        )
        self.config.reply_enhancements = True
        self.session_turns = _RecordingTurns(exits)

        async def on_chunk(chunk):
            exits.note("sse.chunk", chunk.get("text"))

        self._sink = {"on_chunk": on_chunk}

    def get_turn_sink(self, key):
        return self._sink

    def get_progress_style_for_context(self, context):
        return self._progress_style_value


class CitationExitTests(unittest.IsolatedAsyncioTestCase):
    """One reply, every exit, one question.

    Each test drives a different delivery shape - inline, split, summarized and
    uploaded, suppressed, notified, logged - because they are different code
    paths that each make their own copy of the body, and a copy made before the
    tokens are written is exactly the defect this is looking for.
    """

    SESSION = "ses_exits"
    NOW = "2026-09-21T12:00:00Z"

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"AVIBE_HOME": self._home.name})
        self._env.start()
        dispose_cached_sqlite_engines()
        ensure_sqlite_state()
        engine = create_sqlite_engine()
        try:
            with engine.begin() as conn:
                scope_id = upsert_scope(
                    conn,
                    platform="avibe",
                    scope_type="project",
                    native_id="proj_exits",
                    now=self.NOW,
                )
                conn.execute(
                    agent_sessions.insert().values(
                        id=self.SESSION,
                        scope_id=scope_id,
                        agent_backend="codex",
                        agent_variant="default",
                        session_anchor=f"anchor_{self.SESSION}",
                        native_session_id="",
                        status="active",
                        metadata_json="{}",
                        created_at=self.NOW,
                        updated_at=self.NOW,
                        last_active_at=self.NOW,
                    )
                )
        finally:
            engine.dispose()

        self._files = tempfile.TemporaryDirectory()
        self.report = Path(self._files.name) / "report.txt"
        self.report.write_text("attached\n", encoding="utf-8")
        self.exits = _Exits()
        self._seen_rows: set[str] = set()

    def tearDown(self):
        dispose_cached_sqlite_engines()
        self._env.stop()
        self._home.cleanup()
        self._files.cleanup()

    # -- driving -----------------------------------------------------------

    def registered(self, *, pad: str = "") -> tuple[str, Any]:
        """The real producer's output for this reply, tokens and all."""
        text, bundle = register_citations(
            agent_text(self.report) + pad, SOURCES, unresolved_label=UNRESOLVED_LABEL
        )
        # A census over a body that never held a token proves nothing, and the
        # three literals must be present to be able to survive.
        self.assertTrue(_TOKEN_RE.search(text))
        self.assertIn(f"```\n{marker(REF)}\n```", text)
        self.assertIn(TRUNCATED, text)
        self.assertIn(LOOSE_PUA, text)
        return text, bundle

    def context(self, platform: str, **spec: Any) -> MessageContext:
        avibe = platform == "avibe"
        payload: dict[str, Any] = dict(spec)
        if avibe:
            payload.setdefault("agent_session_id", self.SESSION)
        return MessageContext(
            user_id="workbench" if avibe else "u1",
            channel_id=self.SESSION if avibe else "c1",
            platform=platform,
            platform_specific=payload or None,
        )

    async def drive(
        self,
        platform: str,
        message_type: str = "result",
        *,
        pad: str = "",
        spec: Optional[dict[str, Any]] = None,
        max_bytes: int | None = None,
        progress_style: str = "verbose",
        **kwargs: Any,
    ) -> _RecordingController:
        text, bundle = self.registered(pad=pad)
        controller = _RecordingController(
            platform, self.exits, max_bytes=max_bytes, progress_style=progress_style
        )
        dispatcher = ConsolidatedMessageDispatcher(controller)
        store = _RecordingRunStore(self.exits)
        with mock.patch(
            "core.message_dispatcher.SQLiteBackgroundTaskStore", return_value=store
        ):
            await dispatcher.emit_agent_message(
                self.context(platform, **(spec or {})),
                message_type,
                text,
                citations=bundle,
                **kwargs,
            )
        self.collect_rows()
        return controller

    def collect_rows(self) -> None:
        """Whatever this run left in the store is an exit too.

        Read back by id so a test that drives twice records each row once:
        the store is cumulative and the exits are per-run.
        """
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                for table, kind in ((messages, "row"), (agent_events, "trace")):
                    for row in conn.execute(
                        select(table.c.id, table.c.content_text, table.c.content_json)
                    ):
                        if row[0] in self._seen_rows:
                            continue
                        self._seen_rows.add(row[0])
                        self.exits.note(f"{kind}.text", row[1])
                        self.exits.note(f"{kind}.content", row[2])
        finally:
            engine.dispose()

    # -- the question ------------------------------------------------------

    def assertNoInternalToken(self, *expected_exits: str) -> None:
        """No copy of this reply carries the private handle it travelled as."""
        self.assertTrue(self.exits.rows, "the drive recorded no exits at all")
        for where, value in self.exits.rows:
            with self.subTest(exit=where):
                self.assertIsNone(
                    _TOKEN_RE.search(value),
                    f"{where} carries an internal citation token",
                )
        # An exit nobody reached cannot leak, so the census only means something
        # if the run actually reached the ones it claims to cover.
        self.assertLessEqual(set(expected_exits), self.exits.names())

    def assertLiteralsSurvive(self, body: str) -> None:
        """The three private-use passages that are not this module's to touch."""
        self.assertIn(f"```\n{marker(REF)}\n```", body)
        self.assertIn(TRUNCATED, body)
        self.assertIn(LOOSE_PUA, body)

    def assertAttributes(self, body: str) -> None:
        """The reader is given the source, not a handle to it."""
        self.assertIn(SOURCE_URL, body)

    # -- the drives --------------------------------------------------------

    async def test_an_im_result_writes_every_copy_it_hands_out(self):
        # The run ids travel with the message because a delivered IM result
        # also writes the agent-run record a Harness caller reads back, and
        # that copy is written from the body+footer fold rather than from the
        # row - a separate exit with a separate write.
        await self.drive(
            "slack",
            result_footer="✅ ⏱️ 5s",
            spec={"turn_token": "t1", "accepted_agent_run_ids": ["run-1"]},
        )

        self.assertNoInternalToken(
            "im.send",
            "im.button",
            "im.upload.title",
            "turn.snapshot",
            "run.output",
            "sse.chunk",
            "row.text",
        )
        [sent] = self.exits.of("im.send")
        self.assertLiteralsSurvive(sent)
        self.assertAttributes(sent)
        # The button and the upload title are structured fields: no surface
        # parses them as Markdown, so the attribution there is the bare host.
        self.assertEqual(self.exits.of("im.button")[0], "Open example.com")
        self.assertEqual(self.exits.of("im.upload.title"), ["report example.com"])
        # The Turn snapshot is read back by a later steer, and it is text with
        # no sidecar riding along.
        [snapshot] = self.exits.of("turn.snapshot")
        self.assertLiteralsSurvive(snapshot)
        self.assertAttributes(snapshot)
        # The agent-run record keeps the footer folded back in, so it is a
        # third spelling of the same answer and is written on its own.
        [recorded] = self.exits.of("run.output")
        self.assertLiteralsSurvive(recorded)
        self.assertAttributes(recorded)
        self.assertIn("⏱️ 5s", recorded)

    async def test_a_long_im_result_is_written_before_it_is_split(self):
        # Discord splits rather than summarizes, so the body is cut into pieces
        # AFTER the citations are written. A copy made before the split would
        # put a token in every part.
        await self.drive("discord", pad="\n\n" + "filler. " * 400)

        self.assertNoInternalToken("im.send", "row.text")
        parts = self.exits.of("im.send")
        self.assertGreater(len(parts), 1)
        self.assertLiteralsSurvive("".join(parts))
        self.assertAttributes("".join(parts))

    async def test_a_result_too_large_to_send_is_written_before_it_is_cut(self):
        # Slack neither splits nor sends this inline: the reader gets a summary
        # and the full body as an attachment. Both are copies.
        await self.drive("slack", pad="\n\n" + "filler. " * 5000)

        self.assertNoInternalToken("im.send", "im.attachment", "row.text")
        [summary] = self.exits.of("im.send")
        self.assertTrue(summary.startswith("Result too long"))
        # The summary keeps the head of the body, which is where the citation
        # and the literals are; the attachment keeps all of it.
        self.assertLiteralsSurvive(summary)
        self.assertAttributes(summary)
        [attachment] = self.exits.of("im.attachment")
        self.assertLiteralsSurvive(attachment)
        self.assertAttributes(attachment)

    async def test_the_workbench_row_carries_a_sidecar_and_never_a_token(self):
        await self.drive("avibe", spec={"accepted_agent_run_ids": ["run-1"]})

        self.assertNoInternalToken("row.text", "row.content", "sse.chunk", "run.output")
        [body] = self.exits.of("row.text")
        self.assertLiteralsSurvive(body)
        self.assertAttributes(body)
        [content] = self.exits.of("row.content")
        stored = json.loads(content)
        # The row is the one copy that also carries the measurement, and the
        # labels lifted out of the body sit beside it.
        self.assertEqual([row["url"] for row in stored["citations"]], [SOURCE_URL])
        self.assertEqual(stored["quick_replies"], ["Open example.com", "Done"])

    async def test_a_notify_writes_its_outbound_and_its_stream_copy(self):
        await self.drive("slack", "notify")

        self.assertNoInternalToken("im.send", "sse.chunk", "row.text")
        for copy in self.exits.of("im.send", "sse.chunk", "row.text"):
            self.assertLiteralsSurvive(copy)
            self.assertAttributes(copy)

    async def test_an_intermediate_log_row_its_tool_trace_and_its_bubble(self):
        # Three copies of an intermediate emit, and they are not the same text:
        # the row keeps the body, the trace event keeps the body, and the
        # concise bubble keeps a one-line label DERIVED from it.
        await self.drive("slack", "assistant")
        await self.drive("slack", "toolcall")
        await self.drive("slack", "assistant", progress_style="concise")

        self.assertNoInternalToken("im.send", "row.text", "trace.text")
        [trace] = self.exits.of("trace.text")
        self.assertLiteralsSurvive(trace)
        self.assertAttributes(trace)
        for logged in self.exits.of("im.send"):
            self.assertAttributes(logged)

    async def test_a_silent_result_still_writes_the_record_a_steer_reads(self):
        # Nobody is shown this answer: a silent-level result returns before any
        # delivery, persistence or streaming. Two copies still leave - the Turn
        # snapshot a later steer reads back, and the agent-run record that
        # settles the run - and neither has a sidecar to travel with.
        await self.drive(
            "slack",
            level="silent",
            spec={"turn_token": "t2", "accepted_agent_run_ids": ["run-2"]},
        )

        self.assertNoInternalToken("turn.snapshot", "run.output")
        self.assertEqual(self.exits.of("im.send"), [])
        for copy in self.exits.of("turn.snapshot", "run.output"):
            self.assertLiteralsSurvive(copy)
            self.assertAttributes(copy)

    async def test_a_suppressed_harness_result_writes_its_run_records(self):
        # Nothing is delivered outward: the run ledger and the stored row are
        # the only places this answer exists, which is why a token reaching
        # them would never be corrected by a later stage.
        await self.drive(
            "slack",
            spec={
                "suppress_delivery": True,
                "task_trigger_kind": "scheduled",
                "task_execution_id": "run-9",
            },
        )

        self.assertNoInternalToken("run.output", "row.text")
        self.assertEqual(self.exits.of("im.send"), [])
        [recorded] = self.exits.of("run.output")
        self.assertLiteralsSurvive(recorded)
        self.assertAttributes(recorded)


if __name__ == "__main__":
    unittest.main()
