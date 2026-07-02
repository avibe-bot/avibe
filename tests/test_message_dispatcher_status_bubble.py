"""Concise status-bubble flow (Slack/Discord).

Covers docs/plans/agent-progress-status-bubble.md P0 + P1:
- process messages render as ONE status bubble that REPLACES (not appends);
- the result EDITS that same bubble into the final answer (case 1, S4 id) with a
  ``✅ <elapsed>`` completion footer;
- a non-inline / failed-edit result tidies the orphan bubble (B3);
- ``progress_style=off`` shows no process bubble;
- non-editing platforms keep the legacy verbose append path (no regression);
- liveness footer rendering (elapsed / no-output hint / backend-dead) + heartbeat
  render + S3 cancel-before-edit ordering;
- to_status_label produces clean single-line labels (S1).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext
from modules.im.formatters.base_formatter import to_status_label
from modules.im.formatters.slack_formatter import SlackFormatter


class _StubSettingsManager:
    def _canonicalize_message_type(self, message_type):
        return message_type

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class _StubSessionHandler:
    def finalize_scheduled_delivery(self, context, sent_message_id):
        pass


class _EditClient:
    """IM stub that records sends and edits; edit return value is configurable."""

    def __init__(self, edit_result: bool = True, delete_result: bool = True):
        self.sent = []  # (message_id, text, subtext)
        self.edits = []  # (message_id, text, subtext)
        self.deletes = []  # message_id
        self._n = 0
        self._edit_result = edit_result
        self._delete_result = delete_result

    def should_use_thread_for_reply(self):
        return False

    async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None):
        self._n += 1
        mid = f"msg-{self._n}"
        self.sent.append((mid, text, subtext))
        return mid

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None, subtext=None):
        self.edits.append((message_id, text, subtext))
        return self._edit_result

    async def delete_message(self, context, message_id):
        self.deletes.append(message_id)
        return self._delete_result


class _StubController:
    def __init__(self, *, platform="slack", im_client=None, progress_style="concise", backend_alive=None):
        self.config = type(
            "Config", (), {"platform": platform, "language": "en", "reply_enhancements": False}
        )()
        self.session_handler = _StubSessionHandler()
        self.im_client = im_client or _EditClient()
        self.agent_service = None
        self._progress_style_value = progress_style
        self._backend_alive_value = backend_alive

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{context.platform}::{context.channel_id}"

    def get_settings_manager_for_context(self, context):
        return _StubSettingsManager()

    def get_im_client_for_context(self, context):
        return self.im_client

    def get_progress_style_for_context(self, context):
        return self._progress_style_value

    def backend_alive(self, context):
        return self._backend_alive_value


def _ctx(platform="slack"):
    return MessageContext(user_id="U1", channel_id="C1", platform=platform)


def _dispatcher(controller, *, now=1000.0, disable_heartbeat=True):
    d = ConsolidatedMessageDispatcher(controller)
    d._now = lambda: now
    if disable_heartbeat:
        d._start_status_heartbeat = lambda *a, **k: None
    return d


class StatusBubbleProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_progress_getter_defaults_off(self):
        controller = object()
        d = ConsolidatedMessageDispatcher(controller)
        self.assertEqual(d._progress_style(_ctx()), "off")

    async def test_process_messages_replace_in_one_bubble(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            id1 = await d.emit_agent_message(ctx, "toolcall", "🔧 `Read` `{\"file_path\":\"a.py\"}`")
            id2 = await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{\"command\":\"pytest\"}`")
        self.assertEqual(id1, "msg-1")
        self.assertEqual(id2, "msg-1")  # same bubble
        self.assertEqual([m for m, _, _ in controller.im_client.sent], ["msg-1"])  # one send
        # Second emit EDITED (replace, not append): body + footer kept separate.
        # Two emits → 2 steps; hourglass on the 2nd render is ⌛ (cycles ⏳/⌛).
        self.assertEqual(
            controller.im_client.edits,
            [("msg-1", "🔧 Bash {\"command\":\"pytest\"}", "⌛ 0s · 2 st")],
        )
        self.assertNotIn("`", controller.im_client.edits[0][1])

    async def test_status_label_renders_clean_label_not_raw_json(self):
        # A backend-computed clean label (claude-pipe style) is used for the
        # bubble body in place of the raw format_toolcall JSON text.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            mid = await d.emit_agent_message(
                ctx,
                "toolcall",
                "🔧 `Bash` `{\"command\":\"pytest tests/test_x.py\"}`",
                status_label="🔧 Bash: pytest tests/test_x.py",
            )
        self.assertEqual(mid, "msg-1")
        body, _subtext = controller.im_client.sent[0][1], controller.im_client.sent[0][2]
        self.assertEqual(body, "🔧 Bash: pytest tests/test_x.py")
        self.assertNotIn("{", body)  # raw JSON not used as the bubble body

    async def test_empty_status_label_falls_back_to_to_status_label(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(
                ctx, "toolcall", "🔧 `Bash` `{\"command\":\"pytest\"}`", status_label=""
            )
        self.assertEqual(controller.im_client.sent[0][1], "🔧 Bash {\"command\":\"pytest\"}")

    async def test_progress_style_off_shows_no_bubble(self):
        controller = _StubController(platform="slack", progress_style="off")
        d = _dispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            result = await d.emit_agent_message(_ctx(), "toolcall", "🔧 `Read` `{}`")
        self.assertIsNone(result)
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(controller.im_client.edits, [])

    async def test_non_editing_platform_uses_verbose_append(self):
        controller = _StubController(platform="lark")
        d = _dispatcher(controller)
        self.assertEqual(d._concise_progress_style(_ctx("lark")), "verbose")


class StatusBubbleResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_posts_fresh_message_and_deletes_bubble(self):
        # The result is delivered as a NEW message (so the IM fires a push
        # notification), carrying the ✅ done footer; the transient status bubble is
        # then DELETED so the turn ends as just the fresh result.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            result_id = await d.emit_agent_message(ctx, "result", "Final answer")
        self.assertEqual(result_id, "msg-2")  # a brand-new message, NOT the bubble
        # Fresh result send carries the done footer (show_duration off → no time).
        self.assertIn(("msg-2", "Final answer", "✅ done"), controller.im_client.sent)
        # The status bubble was deleted, not edited into the answer.
        self.assertEqual(controller.im_client.deletes, ["msg-1"])
        # Persisted text is the clean answer (no footer).
        result_persists = [c for c in persist.call_args_list if c.args[1] == "result"]
        self.assertEqual(result_persists[-1].args[2], "Final answer")

    async def test_result_done_footer_keeps_session_tokens(self):
        # A backend that reports session tokens before the result → the fresh
        # result message's done footer carries "✅ done · {n} tok".
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            d.note_session_tokens(ctx, total=248000)  # backend reports occupancy
            await d.emit_agent_message(ctx, "result", "Final answer")
        self.assertIn(("msg-2", "Final answer", "✅ done · 248k tok"), controller.im_client.sent)
        self.assertEqual(controller.im_client.deletes, ["msg-1"])

    async def test_bubble_collapsed_when_delete_unsupported_or_fails(self):
        # Fallback: when the bubble delete fails (or the platform can't delete) the
        # bubble is collapsed to a terminal marker instead, so it never lingers as
        # "running". The result is still delivered as a fresh message.
        controller = _StubController(platform="slack", im_client=_EditClient(delete_result=False))
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            result_id = await d.emit_agent_message(ctx, "result", "Final answer")
        self.assertEqual(result_id, "msg-2")  # fresh result message
        self.assertIn(("msg-2", "Final answer", "✅ done"), controller.im_client.sent)
        self.assertEqual(controller.im_client.deletes, ["msg-1"])  # delete attempted
        # Delete returned False → fall back to collapsing the bubble to a marker.
        self.assertIn(("msg-1", "", "✅ done"), controller.im_client.edits)

    async def test_result_without_prior_bubble_sends_normally(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            result_id = await d.emit_agent_message(_ctx(), "result", "Just an answer")
        self.assertEqual(result_id, "msg-1")
        self.assertEqual([t for _, t, _ in controller.im_client.sent], ["Just an answer"])
        self.assertEqual(controller.im_client.edits, [])

    async def test_long_discord_result_splits_fresh_and_deletes_bubble(self):
        # Discord long result → split path: every chunk is a NEW send (no bubble
        # reuse), and the transient bubble is deleted afterwards.
        controller = _StubController(platform="discord")
        d = _dispatcher(controller)
        ctx = _ctx("discord")
        long_text = "x" * 2500  # exceeds discord 1900-char inline limit → splits
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            result_id = await d.emit_agent_message(ctx, "result", long_text)
        self.assertEqual(result_id, "msg-2")  # first fresh chunk (delivery anchor)
        # At least two fresh chunk sends, all starting with the long body.
        chunk_sends = [(t, st) for m, t, st in controller.im_client.sent if t.startswith("x")]
        self.assertGreaterEqual(len(chunk_sends), 2)
        # The done footer rides ONLY the last chunk (mid-stream footer reads wrong).
        self.assertIsNone(chunk_sends[0][1])
        self.assertEqual(chunk_sends[-1][1], "✅ done")
        # The bubble was deleted (never reused as a chunk).
        self.assertEqual(controller.im_client.deletes, ["msg-1"])

    async def test_split_later_chunk_failure_keeps_first_chunk(self):
        # Discord long result: chunk 0 sends fine, a later chunk send fails. The
        # failure must NOT propagate; the first chunk stays delivered and the
        # bubble is still retired (deleted).
        class _FailLaterChunk(_EditClient):
            def __init__(self):
                super().__init__()
                self._x_sends = 0

            async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None):
                if text.startswith("x"):
                    self._x_sends += 1
                    if self._x_sends >= 2:  # fail the 2nd+ chunk
                        raise RuntimeError("later chunk send failed")
                return await super().send_message(context, text, parse_mode, reply_to, subtext)

        controller = _StubController(platform="discord", im_client=_FailLaterChunk())
        d = _dispatcher(controller)
        ctx = _ctx("discord")
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            result_id = await d.emit_agent_message(ctx, "result", "x" * 2500)
        self.assertEqual(result_id, "msg-2")  # first chunk delivered
        self.assertEqual(controller.im_client.deletes, ["msg-1"])  # bubble still retired

    async def test_split_head_chunk_failure_returns_none(self):
        # If the HEAD chunk fails to send, the split is abandoned (returns None) so
        # the caller's fallback delivers the COMPLETE content — never a head-less
        # partial marked as delivered just because a later chunk happened to send.
        class _FailFirstChunk(_EditClient):
            def __init__(self):
                super().__init__()
                self._x_sends = 0

            async def send_message(self, context, text, parse_mode=None, reply_to=None, subtext=None):
                if text.startswith("x"):
                    self._x_sends += 1
                    if self._x_sends == 1:  # fail ONLY the head chunk
                        raise RuntimeError("head chunk send failed")
                return await super().send_message(context, text, parse_mode, reply_to, subtext)

        controller = _StubController(platform="discord", im_client=_FailFirstChunk())
        d = _dispatcher(controller)
        ctx = _ctx("discord")
        result = await d._send_split_result_messages(
            controller.im_client, ctx, "x" * 2500, [], "markdown"
        )
        self.assertIsNone(result)  # abandoned → caller falls back to full delivery
        # No chunk was kept as the anchor (head missing).
        self.assertEqual([t for _, t, _ in controller.im_client.sent if t.startswith("x")][:1], [])

    async def test_slack_result_delivered_via_native_markdown_then_bubble_deleted(self):
        # A Slack result is delivered inline via the native markdown sender as a
        # FRESH message (carrying the done footer as subtext); the bubble is then
        # deleted. The native sender accepts ``subtext`` like the real adapter.
        class _MarkdownClient(_EditClient):
            def __init__(self):
                super().__init__()
                self.markdown_sends = []

            async def send_markdown_message(self, context, text, keyboard=None, reply_to=None, subtext=None):
                self._n += 1
                mid = f"native-{self._n}"
                self.markdown_sends.append((mid, text, subtext))
                return mid

        controller = _StubController(platform="slack", im_client=_MarkdownClient())
        d = _dispatcher(controller)
        ctx = _ctx()
        long_text = "x" * 20000  # within Slack's 30000-char result limit → inline
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1 (send_message)
            result_id = await d.emit_agent_message(ctx, "result", long_text)
        self.assertEqual(result_id, "native-2")  # fresh native-markdown send
        self.assertEqual(controller.im_client.markdown_sends, [("native-2", long_text, "✅ done")])
        self.assertEqual(controller.im_client.deletes, ["msg-1"])  # bubble deleted

    async def test_late_process_emit_does_not_resurrect_finalized_bubble(self):
        # C1: once the turn finalized (result edited the bubble to ✅ done), a late
        # in-flight process emit for the SAME key must bail instead of overwriting
        # the terminal bubble back to a "working" line.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            await d.emit_agent_message(ctx, "result", "Final answer")  # finalize → ✅ done
        edits_after_finalize = len(controller.im_client.edits)
        # A late process emit for the same turn key (e.g. a straggler before
        # teardown) must NOT edit/resurrect the now-finalized bubble.
        # NB: _clear_consolidated_state ran in the result path, but the key is in
        # _status_finalized for the duration; re-seed it to simulate the race
        # window where finalize landed but the late emit shares the key.
        key = d._get_consolidated_message_key(ctx)
        d._status_finalized.add(key)
        d._consolidated_message_ids[key] = "msg-1"
        late = await d._render_concise_status(controller.im_client, ctx, "🔧 `Read` `{}`")
        self.assertIsNone(late)  # bailed
        self.assertEqual(len(controller.im_client.edits), edits_after_finalize)  # no new edit

    async def test_late_emit_after_result_does_not_reopen_bubble(self):
        # P2: the runtime gate is released only AFTER the result path's teardown,
        # so a late same-turn process emit in that window still passes the
        # runtime-turn guard. The finalized marker must SURVIVE _drop_status_keys
        # (not re-seeded here) so the late emit bails instead of opening a new
        # running bubble after the answer was posted.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            await d.emit_agent_message(ctx, "result", "Final answer")  # delete bubble + fresh result
        sends_before = len(controller.im_client.sent)
        edits_before = len(controller.im_client.edits)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            late = await d.emit_agent_message(ctx, "toolcall", "🔧 `Read` `{}`")
        self.assertIsNone(late)  # bailed on the surviving finalized marker
        self.assertEqual(len(controller.im_client.sent), sends_before)  # no new bubble posted
        self.assertEqual(len(controller.im_client.edits), edits_before)

    async def test_error_result_collapse_fallback_uses_failed_marker(self):
        # C4: on an is_error turn where the bubble delete fails, the collapse
        # fallback reflects the failure outcome (⏹ failed), NOT a hardcoded ✅ done.
        controller = _StubController(platform="slack", im_client=_EditClient(delete_result=False))
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            result_id = await d.emit_agent_message(ctx, "result", "Failed answer", is_error=True)
        self.assertEqual(result_id, "msg-2")  # fresh result message
        self.assertEqual(controller.im_client.deletes, ["msg-1"])  # delete attempted (failed)
        self.assertIn(("msg-1", "", "⏹ failed"), controller.im_client.edits)  # collapse fallback
        self.assertNotIn(("msg-1", "", "✅ done"), controller.im_client.edits)

    async def test_result_teardown_drops_lock_dict_entry(self):
        # C6: after a turn ends, the per-key consolidated lock must be dropped from
        # _consolidated_message_locks so it can't grow unbounded across turns.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            key = d._get_consolidated_message_key(ctx)
            self.assertIn(key, d._consolidated_message_locks)  # lock created
            await d.emit_agent_message(ctx, "result", "Final answer")
        # Per-turn state (lock + message id) is dropped so they can't grow
        # unbounded. The finalized marker intentionally SURVIVES teardown — it must
        # outlive the turn's gate-release window so a late same-turn emit can't
        # re-open the bubble (P2); it is bounded separately in _finalize_status_key.
        self.assertNotIn(key, d._consolidated_message_locks)
        self.assertNotIn(key, d._consolidated_message_ids)
        self.assertIn(key, d._status_finalized)

    async def test_result_stops_heartbeat_before_edit(self):
        # Heartbeat ENABLED here: emit a process message to start it, then the
        # result must cancel it (S3) so no stale tick stomps the answer.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller, disable_heartbeat=False)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")
            key = d._get_consolidated_message_key(ctx)
            self.assertIn(key, d._status_heartbeat_tasks)  # heartbeat running
            await d.emit_agent_message(ctx, "result", "Done")
        self.assertNotIn(key, d._status_heartbeat_tasks)  # cancelled + cleared


class BeginStatusBubbleTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_creates_bubble_then_first_emit_edits_same_id(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        await d.begin_status_bubble(ctx)
        # One immediate send with an EMPTY body (footer-only): the running footer
        # alone conveys the agent started; no redundant "starting agent" line.
        self.assertEqual([m for m, _, _ in controller.im_client.sent], ["msg-1"])
        self.assertEqual(controller.im_client.sent[0][1], "")
        # Footer-only turn-start bubble: just the hourglass + elapsed, no step
        # (empty label is not a step) and no token field (none reported yet).
        self.assertEqual(controller.im_client.sent[0][2], "⏳ 0s")
        self.assertEqual(controller.im_client.edits, [])
        # First real process emit finds the existing id and EDITS it (no 2nd send),
        # filling in the action-label body. That emit is the 1st step; the glyph
        # cycles to ⌛ on this second render.
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            emit_id = await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")
        self.assertEqual(emit_id, "msg-1")
        self.assertEqual([m for m, _, _ in controller.im_client.sent], ["msg-1"])  # still one send
        self.assertEqual(controller.im_client.edits, [("msg-1", "🔧 Bash {}", "⌛ 0s · 1 st")])

    async def test_begin_is_idempotent(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        await d.begin_status_bubble(ctx)
        await d.begin_status_bubble(ctx)  # second call: bubble already exists → no-op
        self.assertEqual([m for m, _, _ in controller.im_client.sent], ["msg-1"])
        self.assertEqual(controller.im_client.edits, [])

    async def test_begin_noop_when_suppress_delivery(self):
        # No-delivery runs (scheduled / watch targeting a Slack/Discord no-delivery
        # session) must NOT leak a visible turn-start bubble — the turn-start post
        # runs before the chokepoint's suppress-delivery branch, so it is guarded here.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        ctx.platform_specific = {"suppress_delivery": True}
        await d.begin_status_bubble(ctx)
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(controller.im_client.edits, [])

    async def test_begin_noop_when_style_off(self):
        controller = _StubController(platform="slack", progress_style="off")
        d = _dispatcher(controller)
        await d.begin_status_bubble(_ctx())
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(controller.im_client.edits, [])

    async def test_begin_noop_on_non_status_bubble_platform(self):
        controller = _StubController(platform="lark")
        d = _dispatcher(controller)
        await d.begin_status_bubble(_ctx("lark"))
        self.assertEqual(controller.im_client.sent, [])
        self.assertEqual(controller.im_client.edits, [])

    async def test_empty_error_result_tidies_starting_bubble(self):
        # Regression: a turn that posts the footer-only bubble then ends via the
        # empty terminal result (missing agent / exception) must collapse the
        # bubble to a terminal marker instead of leaving it stuck "working".
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        await d.begin_status_bubble(ctx)  # posts footer-only msg-1
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            result_id = await d.emit_agent_message(ctx, "result", "", is_error=True)
        self.assertIsNone(result_id)  # empty result delivers nothing
        # is_error + non-silent → "failed": ⏹ failed marker, not ✅.
        self.assertIn(("msg-1", "", "⏹ failed"), controller.im_client.edits)

    async def test_silent_result_tidies_starting_bubble(self):
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        await d.begin_status_bubble(ctx)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "result", "🛑 stopped", level="silent")
        # Silent but NOT is_error → clean ✅ done marker.
        self.assertIn(("msg-1", "", "✅ done"), controller.im_client.edits)

    async def test_error_silent_result_tidies_starting_bubble_with_stop_marker(self):
        # A manually-stopped (silent + is_error) terminal collapses to ⏹ stopped.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        await d.begin_status_bubble(ctx)
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "result", "", level="silent", is_error=True)
        self.assertIn(("msg-1", "", "⏹ stopped"), controller.im_client.edits)

    async def test_error_result_posts_fresh_message_with_failed_footer(self):
        # A non-empty errored (non-silent) result is delivered as a fresh message
        # whose done footer carries the "failed" reason (show_duration off → no
        # time); the bubble is then deleted.
        controller = _StubController(platform="slack")
        d = _dispatcher(controller)
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")  # bubble msg-1
            await d.emit_agent_message(ctx, "result", "Interrupted", is_error=True)
        self.assertIn(("msg-2", "Interrupted", "⏹ failed"), controller.im_client.sent)
        self.assertEqual(controller.im_client.deletes, ["msg-1"])


class HeartbeatCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_heartbeat_terminates_even_mid_edit(self):
        # Blocker regression: CancelledError in the heartbeat edit must propagate
        # so task.cancel() terminates the loop; otherwise _stop_status_heartbeat
        # would hang and block result delivery.
        started = asyncio.Event()

        class _BlockingClient(_EditClient):
            async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None, subtext=None):
                started.set()
                await asyncio.sleep(3600)  # block until cancelled

        controller = _StubController(platform="slack", im_client=_BlockingClient())
        d = ConsolidatedMessageDispatcher(controller)
        d._now = lambda: 1000.0
        d._heartbeat_interval_s = lambda ctx: 0.0  # fire immediately
        ctx = _ctx()
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")
        key = d._get_consolidated_message_key(ctx)
        await asyncio.wait_for(started.wait(), timeout=2.0)  # heartbeat entered the edit
        await asyncio.wait_for(d._stop_status_heartbeat(key), timeout=2.0)  # must NOT hang
        self.assertNotIn(key, d._status_heartbeat_tasks)


class LivenessFooterTests(unittest.IsolatedAsyncioTestCase):
    def test_format_elapsed(self):
        d = _dispatcher(_StubController())
        self.assertEqual(d._format_elapsed(0), "0s")
        self.assertEqual(d._format_elapsed(59), "59s")
        self.assertEqual(d._format_elapsed(60), "1:00")
        self.assertEqual(d._format_elapsed(125), "2:05")

    def test_footer_running_and_done(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        # Running footer (compact): {hourglass} {elapsed}; 0-value fields omitted.
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=5), "⏳ 5s")
        # The hourglass glyph is whatever the caller passes (cycled by compose).
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=5, hourglass="⌛"), "⌛ 5s")
        # show_duration off (default stub config) → done footer is the reason word.
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=18, done=True), "✅ done")
        # A stopped terminal turn shows ⏹ stopped; a failed turn shows ⏹ failed.
        self.assertEqual(
            d._status_footer_text(ctx, elapsed_s=18, done=True, reason="stopped"), "⏹ stopped"
        )
        self.assertEqual(
            d._status_footer_text(ctx, elapsed_s=18, done=True, reason="failed"), "⏹ failed"
        )

    def test_footer_running_with_steps_and_tokens(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        # steps appended only when > 0; tokens read from the session total.
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=18, steps=6), "⏳ 18s · 6 st")
        d.note_session_tokens(ctx, total=12345)
        self.assertEqual(
            d._status_footer_text(ctx, elapsed_s=18, steps=6, hourglass="⌛"),
            "⌛ 18s · 6 st · 12.3k tok",
        )

    def test_footer_done_with_show_duration_appends_time(self):
        controller = _StubController()
        controller.config.show_duration = True
        d = _dispatcher(controller)
        ctx = _ctx()
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=18, done=True), "✅ done · 18s")
        self.assertEqual(
            d._status_footer_text(ctx, elapsed_s=84, done=True, reason="stopped"), "⏹ stopped · 1:24"
        )
        self.assertEqual(
            d._status_footer_text(ctx, elapsed_s=84, done=True, reason="failed"), "⏹ failed · 1:24"
        )

    def test_footer_done_keeps_session_tokens(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        d.note_session_tokens(ctx, total=248000)
        # Terminal footer keeps the session token total (show_duration off here).
        self.assertEqual(d._status_footer_text(ctx, elapsed_s=84, done=True), "✅ done · 248k tok")

    def test_token_count_formatting(self):
        d = _dispatcher(_StubController())
        self.assertEqual(d._format_token_count(0), "0")
        self.assertEqual(d._format_token_count(999), "999")
        self.assertEqual(d._format_token_count(12345), "12.3k")
        self.assertEqual(d._format_token_count(248000), "248k")
        self.assertEqual(d._format_token_count(1_400_000), "1.4M")

    def test_note_session_tokens_sets_absolute_snapshot(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        key = d._token_session_key(ctx)
        d.note_session_tokens(ctx, total=38000)
        self.assertEqual(d._session_token_total[key], 38000)
        # Pure SET (not accumulate): a later snapshot replaces, and can DROP
        # (e.g. after a /compact) — never adds.
        d.note_session_tokens(ctx, total=12000)
        self.assertEqual(d._session_token_total[key], 12000)
        # Invalid values are ignored (keep the last good snapshot).
        d.note_session_tokens(ctx, total="nope")
        d.note_session_tokens(ctx, total=-5)
        self.assertEqual(d._session_token_total[key], 12000)

    def test_token_totals_isolated_per_thread(self):
        # Two threads in the SAME channel must not share the occupancy figure — a
        # token update in one thread must not change the other thread's footer (P2).
        d = _dispatcher(_StubController())
        a = MessageContext(user_id="U1", channel_id="C1", platform="slack", thread_id="T-a")
        b = MessageContext(user_id="U1", channel_id="C1", platform="slack", thread_id="T-b")
        d.note_session_tokens(a, total=38000)
        d.note_session_tokens(b, total=120000)
        self.assertEqual(d._token_field(a), "38.0k tok")
        self.assertEqual(d._token_field(b), "120k tok")
        self.assertNotEqual(d._token_session_key(a), d._token_session_key(b))

    def test_body_action_time_decoration(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        # Below the hint threshold → body unchanged.
        self.assertEqual(
            d._decorate_body_with_action_time(ctx, "🔧 Bash", 5, backend_dead=False), "🔧 Bash"
        )
        # Past the threshold → append the action's own runtime.
        self.assertEqual(
            d._decorate_body_with_action_time(ctx, "🔧 Bash", 150, backend_dead=False),
            "🔧 Bash · 2:30",
        )
        # Past the no-output threshold (180s default), alive → ⚠️ emphasis.
        self.assertEqual(
            d._decorate_body_with_action_time(ctx, "🔧 Bash", 200, backend_dead=False),
            "🔧 Bash · ⚠️ 3:20",
        )
        # Empty body is never decorated.
        self.assertEqual(d._decorate_body_with_action_time(ctx, "", 300, backend_dead=False), "")

    def test_footer_backend_dead(self):
        d = _dispatcher(_StubController())
        ctx = _ctx()
        footer = d._status_footer_text(ctx, elapsed_s=12, backend_dead=True)
        self.assertEqual(footer, "⚠️ backend not responding · 12s")

    def test_backend_dead_probe(self):
        # Unknown (no probe / None) → not dead; explicit False → dead.
        self.assertFalse(_dispatcher(_StubController(backend_alive=None))._backend_dead(_ctx()))
        self.assertTrue(_dispatcher(_StubController(backend_alive=False))._backend_dead(_ctx()))
        self.assertFalse(_dispatcher(_StubController(backend_alive=True))._backend_dead(_ctx()))

    async def test_heartbeat_render_updates_elapsed(self):
        controller = _StubController(platform="discord")
        clock = {"t": 1000.0}
        d = ConsolidatedMessageDispatcher(controller)
        d._now = lambda: clock["t"]
        d._start_status_heartbeat = lambda *a, **k: None  # control manually
        ctx = _ctx("discord")
        with mock.patch("core.message_dispatcher.persist_agent_message"):
            await d.emit_agent_message(ctx, "toolcall", "🔧 `Bash` `{}`")
        key = d._get_consolidated_message_key(ctx)
        last_activity_before = d._status_last_activity_at[key]
        clock["t"] = 1075.0  # +75s elapsed
        await d._status_heartbeat_render_once(ctx, controller.im_client, key, "msg-1")
        # Body + footer are passed separately; the adapter owns -# styling now.
        # 75s since the only emit → the body gains the action's own runtime
        # ("🔧 Bash {} · 1:15"); footer is the compact running form (1 step).
        # emit composed once (⏳), heartbeat composes again → glyph cycles to ⌛.
        self.assertEqual(
            controller.im_client.edits[-1], ("msg-1", "🔧 Bash {} · 1:15", "⌛ 1:15 · 1 st")
        )
        # Invariant: a heartbeat re-render must NOT reset last-activity — that is
        # what lets the action runtime keep growing during a quiet stretch.
        self.assertEqual(d._status_last_activity_at[key], last_activity_before)

    def test_hourglass_cycles_across_renders(self):
        # The hourglass glyph alternates ⏳ ⌛ ⏳ ⌛ on each running render so the
        # bubble animates across heartbeats/emits without any new event.
        d = _dispatcher(_StubController())
        ctx = _ctx()
        key = d._get_consolidated_message_key(ctx)
        d._consolidated_message_buffers[key] = "🔧 Bash {}"
        glyphs = []
        for _ in range(4):
            _, footer = d._compose_status_message(ctx, key)
            glyphs.append(footer.split(" ", 1)[0])  # leading hourglass glyph
        self.assertEqual(glyphs, ["⏳", "⌛", "⏳", "⌛"])


class StatusLabelTests(unittest.TestCase):
    def test_strips_backticks_and_takes_first_line(self):
        self.assertEqual(
            to_status_label("🔧 `Bash` `{\"command\":\"pytest\"}`"),
            "🔧 Bash {\"command\":\"pytest\"}",
        )

    def test_multiline_assistant_uses_first_line(self):
        self.assertEqual(to_status_label("Reading the file.\n\nThen editing."), "Reading the file.")

    def test_truncates_with_ellipsis_and_keeps_underscores(self):
        out = to_status_label("🔧 Read message_dispatcher_really_long_name.py extra tail here", max_len=20)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 21)
        self.assertIn("_", out)

    def test_empty(self):
        self.assertEqual(to_status_label(""), "")


class FormatToolcallLabelTests(unittest.TestCase):
    """Clean claude-pipe-style tool-call labels: ``🔧 <Tool>: <primary-arg>``."""

    def setUp(self):
        self.fmt = SlackFormatter()

    def test_bash_uses_command(self):
        self.assertEqual(
            self.fmt.format_toolcall_label("Bash", {"command": "pytest tests/test_x.py"}),
            "🔧 Bash: pytest tests/test_x.py",
        )

    def test_read_file_path_strips_workspace_prefix(self):
        out = self.fmt.format_toolcall_label(
            "Read",
            {"file_path": "/workspace/repo/core/message_dispatcher.py"},
            get_relative_path=lambda p: p.replace("/workspace/repo/", ""),
        )
        self.assertEqual(out, "🔧 Read: core/message_dispatcher.py")

    def test_grep_pattern_strips_surrounding_quotes(self):
        self.assertEqual(
            self.fmt.format_toolcall_label("Grep", {"pattern": '"consolidated"'}),
            "🔧 Grep: consolidated",
        )

    def test_empty_input_yields_bare_tool(self):
        self.assertEqual(self.fmt.format_toolcall_label("TodoWrite", {}), "🔧 TodoWrite")
        self.assertEqual(self.fmt.format_toolcall_label("TodoWrite", None), "🔧 TodoWrite")

    def test_no_usable_string_arg_yields_bare_tool(self):
        # Non-string values don't count as a salient arg.
        self.assertEqual(self.fmt.format_toolcall_label("Foo", {"limit": 5}), "🔧 Foo")

    def test_long_arg_truncates_with_ellipsis(self):
        out = self.fmt.format_toolcall_label(
            "Bash", {"command": "echo " + "x" * 200}, max_len=30
        )
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 31)

    def test_first_line_and_collapsed_whitespace(self):
        out = self.fmt.format_toolcall_label("Bash", {"command": "make build\nmake test"})
        self.assertEqual(out, "🔧 Bash: make build")

    def test_command_priority_over_file_path(self):
        out = self.fmt.format_toolcall_label(
            "Tool", {"file_path": "a.py", "command": "ls"}
        )
        self.assertEqual(out, "🔧 Tool: ls")

    def test_fallback_to_first_string_value(self):
        out = self.fmt.format_toolcall_label("Custom", {"foo": "bar"})
        self.assertEqual(out, "🔧 Custom: bar")

    def test_preserves_underscores_in_file_name(self):
        out = self.fmt.format_toolcall_label("Read", {"file_path": "message_dispatcher.py"})
        self.assertEqual(out, "🔧 Read: message_dispatcher.py")


if __name__ == "__main__":
    unittest.main()
