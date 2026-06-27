"""Queued (👌) → running (👀) reaction lifecycle for the processing indicator.

The reaction is SELECTED in start() but ADDED at the runtime gate: a message
waiting behind a running turn shows the queued 👌, which is promoted to the
running 👀 when its turn actually starts, and removed on finish. See
core/processing_indicator.py and AgentService.handle_message.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import (
    ACK_REACTION_EMOJI,
    QUEUED_REACTION_EMOJI,
    ProcessingIndicatorHandle,
    ProcessingIndicatorService,
)
from modules.im import MessageContext


def _ctx(message_id="m1"):
    return MessageContext(user_id="u1", channel_id="c1", message_id=message_id, platform="slack")


class _FakeIM:
    def __init__(self, *, add_ok=True, remove_ok=True, typing_ok=True):
        self.add_ok = add_ok
        self.remove_ok = remove_ok
        self.typing_ok = typing_ok
        self.calls: list[tuple[str, str, str]] = []
        self.typing_calls: list[str] = []
        self.clear_calls: list[str] = []

    async def add_reaction(self, context, message_id, emoji):
        self.calls.append(("add", message_id, emoji))
        return self.add_ok

    async def remove_reaction(self, context, message_id, emoji):
        self.calls.append(("remove", message_id, emoji))
        return self.remove_ok

    async def send_typing_indicator(self, context):
        self.typing_calls.append(context.channel_id)
        return self.typing_ok

    async def clear_typing_indicator(self, context):
        self.clear_calls.append(context.channel_id)
        return True


def _svc(im):
    svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
    svc.controller = SimpleNamespace(get_im_client_for_context=lambda ctx: im, im_client=im)
    svc.config = SimpleNamespace()
    svc._indicators_by_turn_token = {}
    return svc


def _request(handle):
    return SimpleNamespace(
        processing_indicator=handle,
        ack_message_id=None,
        ack_reaction_message_id=None,
        ack_reaction_emoji=None,
        typing_indicator_active=False,
        typing_indicator_task=None,
    )


class QueuedReactionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_show_queued_then_promote_then_finish(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        applied = await svc.show_queued_reaction(handle)
        self.assertTrue(applied)
        self.assertEqual(handle.ack_reaction_emoji, QUEUED_REACTION_EMOJI)
        self.assertEqual(handle.ack_reaction_message_id, "m1")
        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

        await svc.promote_reaction_to_running(handle)
        self.assertEqual(handle.ack_reaction_emoji, ACK_REACTION_EMOJI)
        self.assertEqual(
            im.calls,
            [
                ("add", "m1", QUEUED_REACTION_EMOJI),
                ("remove", "m1", QUEUED_REACTION_EMOJI),
                ("add", "m1", ACK_REACTION_EMOJI),
            ],
        )

        await svc.finish(handle)
        self.assertIsNone(handle.ack_reaction_emoji)
        self.assertIsNone(handle.ack_reaction_message_id)
        self.assertEqual(im.calls[-1], ("remove", "m1", ACK_REACTION_EMOJI))

    async def test_promote_without_queued_adds_running_directly(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        await svc.promote_reaction_to_running(handle)
        self.assertEqual(handle.ack_reaction_emoji, ACK_REACTION_EMOJI)
        self.assertEqual(im.calls, [("add", "m1", ACK_REACTION_EMOJI)])

    async def test_promote_is_idempotent(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        await svc.promote_reaction_to_running(handle)
        await svc.promote_reaction_to_running(handle)
        self.assertEqual(im.calls, [("add", "m1", ACK_REACTION_EMOJI)])

    async def test_show_queued_noop_when_reaction_not_selected(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=False)

        applied = await svc.show_queued_reaction(handle)
        self.assertFalse(applied)
        self.assertEqual(im.calls, [])
        self.assertIsNone(handle.ack_reaction_emoji)

    async def test_promote_noop_when_reaction_not_selected(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=False)

        await svc.promote_reaction_to_running(handle)
        self.assertEqual(im.calls, [])

    async def test_show_queued_does_not_double_add(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        self.assertTrue(await svc.show_queued_reaction(handle))
        self.assertFalse(await svc.show_queued_reaction(handle))
        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

    async def test_show_queued_returns_false_when_platform_rejects(self):
        # WeChat-like: add_reaction returns False — no handle state, no crash.
        im = _FakeIM(add_ok=False)
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        applied = await svc.show_queued_reaction(handle)
        self.assertFalse(applied)
        self.assertIsNone(handle.ack_reaction_emoji)
        self.assertIsNone(handle.ack_reaction_message_id)

    async def test_finish_removes_queued_reaction_on_cancel_while_queued(self):
        # The cancel-while-queued path calls finish() with only 👌 shown.
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)
        await svc.show_queued_reaction(handle)

        await svc.finish(handle)
        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI), ("remove", "m1", QUEUED_REACTION_EMOJI)])
        self.assertIsNone(handle.ack_reaction_emoji)

    async def test_promote_keeps_queued_reaction_when_remove_fails(self):
        # If removing 👌 fails on promote, keep owning 👌 (don't stack 👀); finish()
        # must still be able to clear the queued reaction so it never leaks.
        im = _FakeIM(remove_ok=False)
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)
        await svc.show_queued_reaction(handle)

        await svc.promote_reaction_to_running(handle)
        # 👀 was NOT stacked on top; handle still owns 👌.
        self.assertEqual(handle.ack_reaction_emoji, QUEUED_REACTION_EMOJI)
        self.assertEqual([c for c in im.calls if c[0] == "add"], [("add", "m1", QUEUED_REACTION_EMOJI)])

        await svc.finish(handle)
        self.assertIsNone(handle.ack_reaction_emoji)
        self.assertIsNone(handle.ack_reaction_message_id)

    async def test_promote_falls_back_to_typing_when_reaction_add_fails(self):
        # P2: if the reaction add fails at runtime (e.g. missing Slack scope), the
        # deferred reaction has no lower candidate to fall through to, so promote
        # must fall back to a typing indicator rather than leave no ack at all.
        im = _FakeIM(add_ok=False)
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        await svc.promote_reaction_to_running(handle)
        self.assertIsNone(handle.ack_reaction_emoji)  # reaction did not stick
        self.assertTrue(handle.typing_indicator_active)  # fell back to typing
        self.assertTrue(im.typing_calls)

        await svc.finish(handle)  # cancels the typing keepalive task
        self.assertFalse(handle.typing_indicator_active)

    async def test_promote_falls_back_to_message_when_typing_unsupported(self):
        # P2 follow-up: a platform with reactions + message but NO typing
        # (Lark/Feishu). A failed reaction add must fall through to the ack message,
        # not leave the user with no indicator.
        im = _FakeIM(add_ok=False)
        svc = _svc(im)
        svc._capabilities = lambda ctx: object()
        svc._mode_supported = lambda caps, mode, ctx: {"typing": False, "message": True}.get(mode, True)
        started: list = []

        async def _msg(handle, agent_name):
            started.append(("message", agent_name))
            handle.ack_message_id = "ack-1"
            return True

        svc._start_message_indicator = _msg
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)

        await svc.promote_reaction_to_running(handle, agent_name="claude")
        self.assertIsNone(handle.ack_reaction_emoji)  # reaction did not stick
        self.assertEqual(started, [("message", "claude")])  # fell back to ack message
        self.assertEqual(handle.ack_message_id, "ack-1")

    async def test_request_parallel_fields_synced(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)
        request = _request(handle)

        await svc.show_queued_reaction(request)
        self.assertEqual(request.ack_reaction_emoji, QUEUED_REACTION_EMOJI)
        self.assertEqual(request.ack_reaction_message_id, "m1")

        await svc.promote_reaction_to_running(request)
        self.assertEqual(request.ack_reaction_emoji, ACK_REACTION_EMOJI)
        self.assertEqual(request.ack_reaction_message_id, "m1")


if __name__ == "__main__":
    unittest.main()
