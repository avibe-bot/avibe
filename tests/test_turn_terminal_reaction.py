"""Terminal receipts (⏹️ stopped / ⚠️ interrupted) on the triggering message.

A turn that emits a result needs no second receipt — the result IS the receipt,
so finish() keeps its historical plain removal. A turn that ends WITHOUT one is
ambiguous: a stop and a dead runtime both leave a message whose reaction simply
vanished, which is also what a healthy silent completion looks like. These two
emojis replace the running 👀 to say which happened. See
core/processing_indicator.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import (
    ACK_REACTION_EMOJI,
    INTERRUPTED_REACTION_EMOJI,
    STOPPED_REACTION_EMOJI,
    ProcessingIndicatorHandle,
    ProcessingIndicatorService,
)
from modules.im import MessageContext


def _ctx(message_id="m1"):
    return MessageContext(user_id="u1", channel_id="c1", message_id=message_id, platform="slack")


class _FakeIM:
    def __init__(self, *, remove_ok=True, remove_raises=False, rejected_emojis=()):
        self.remove_ok = remove_ok
        self.remove_raises = remove_raises
        self.rejected_emojis = set(rejected_emojis)
        self.calls: list[tuple[str, str, str]] = []

    async def add_reaction(self, context, message_id, emoji):
        self.calls.append(("add", message_id, emoji))
        return emoji not in self.rejected_emojis

    async def remove_reaction(self, context, message_id, emoji):
        self.calls.append(("remove", message_id, emoji))
        if self.remove_raises:
            raise RuntimeError("platform rejected the removal")
        return self.remove_ok

    async def send_typing_indicator(self, context):
        return True

    async def clear_typing_indicator(self, context):
        return True


def _svc(im):
    svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
    svc.controller = SimpleNamespace(get_im_client_for_context=lambda ctx: im, im_client=im)
    svc.config = SimpleNamespace()
    svc._indicators_by_turn_token = {}
    return svc


async def _running(svc, im):
    handle = ProcessingIndicatorHandle(context=_ctx(), reaction_indicator_selected=True)
    await svc.promote_reaction_to_running(handle)
    im.calls.clear()
    return handle


class TerminalReactionOnFinishTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_replaces_running_reaction_with_receipt(self):
        im = _FakeIM()
        svc = _svc(im)
        handle = await _running(svc, im)

        await svc.finish(handle, terminal_emoji=STOPPED_REACTION_EMOJI)

        self.assertEqual(
            im.calls,
            [
                ("remove", "m1", ACK_REACTION_EMOJI),
                ("add", "m1", STOPPED_REACTION_EMOJI),
            ],
        )
        # The handle is cleared either way — the receipt is not tracked state.
        self.assertIsNone(handle.ack_reaction_emoji)
        self.assertIsNone(handle.ack_reaction_message_id)

    async def test_finish_without_terminal_emoji_only_removes(self):
        # Regression guard: a turn that produced a result must not gain a receipt.
        im = _FakeIM()
        svc = _svc(im)
        handle = await _running(svc, im)

        await svc.finish(handle)

        self.assertEqual(im.calls, [("remove", "m1", ACK_REACTION_EMOJI)])

    async def test_receipt_is_skipped_when_removal_fails(self):
        # Stacking ⏹️ on a 👀 that is still there would read as "running AND
        # stopped". If the 👀 could not be retired, leave the message alone.
        im = _FakeIM(remove_raises=True)
        svc = _svc(im)
        handle = await _running(svc, im)

        await svc.finish(handle, terminal_emoji=STOPPED_REACTION_EMOJI)

        self.assertEqual(im.calls, [("remove", "m1", ACK_REACTION_EMOJI)])
        self.assertIsNone(handle.ack_reaction_emoji)

    async def test_receipt_rejected_by_platform_still_clears_cleanly(self):
        # WeChat-like: the terminal emoji is unsupported. The removal is what the
        # bookkeeping is keyed on, so the indicator still ends cleared.
        im = _FakeIM(rejected_emojis={STOPPED_REACTION_EMOJI})
        svc = _svc(im)
        handle = await _running(svc, im)

        await svc.finish(handle, terminal_emoji=STOPPED_REACTION_EMOJI)

        self.assertEqual(
            im.calls,
            [
                ("remove", "m1", ACK_REACTION_EMOJI),
                ("add", "m1", STOPPED_REACTION_EMOJI),
            ],
        )
        self.assertIsNone(handle.ack_reaction_emoji)


class OrphanedTerminalReactionTests(unittest.IsolatedAsyncioTestCase):
    """Restart recovery has no handle — only the durable Delivery's message id."""

    async def test_stamps_interrupted_over_stale_running_reaction(self):
        im = _FakeIM()
        svc = _svc(im)

        stamped = await svc.stamp_orphaned_terminal_reaction(
            _ctx(), "m1", INTERRUPTED_REACTION_EMOJI
        )

        self.assertTrue(stamped)
        self.assertEqual(
            im.calls,
            [
                ("remove", "m1", ACK_REACTION_EMOJI),
                ("add", "m1", INTERRUPTED_REACTION_EMOJI),
            ],
        )

    async def test_receipt_still_stamped_when_stale_reaction_is_gone(self):
        # Unlike finish(), the two halves are independent here: recovery cannot
        # know whether the 👀 ever landed, so a failed removal must not veto the
        # notice-bearing receipt.
        im = _FakeIM(remove_raises=True)
        svc = _svc(im)

        stamped = await svc.stamp_orphaned_terminal_reaction(
            _ctx(), "m1", INTERRUPTED_REACTION_EMOJI
        )

        self.assertTrue(stamped)
        self.assertEqual(im.calls[-1], ("add", "m1", INTERRUPTED_REACTION_EMOJI))

    async def test_noop_without_a_message_id(self):
        im = _FakeIM()
        svc = _svc(im)

        stamped = await svc.stamp_orphaned_terminal_reaction(
            _ctx(), "", INTERRUPTED_REACTION_EMOJI
        )

        self.assertFalse(stamped)
        self.assertEqual(im.calls, [])


if __name__ == "__main__":
    unittest.main()
