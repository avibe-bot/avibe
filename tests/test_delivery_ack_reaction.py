"""Admission receipts for IM input that does not start its own turn.

MESSAGE-DELIVERY-301 / MESSAGE-DELIVERY-302.

A message sent while a turn is already running is handed to its durable
Delivery owner and the message handler returns before any processing indicator
exists, so the sender used to see nothing at all. ``ack_delivery_state`` reports
that admission outcome as a reaction on the sender's own message, and the
receipt is cleared once that input's own turn starts.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import (
    ACK_REACTION_EMOJI,
    NOT_DELIVERED_REACTION_EMOJI,
    QUEUED_REACTION_EMOJI,
    STEERED_REACTION_EMOJI,
    UNCONFIRMED_REACTION_EMOJI,
    ProcessingIndicatorService,
)
from core.session_turns import DeliveryResult
from modules.im import MessageContext


def _ctx(message_id="m1", platform="telegram"):
    return MessageContext(
        user_id="u1",
        channel_id="c1",
        message_id=message_id,
        platform=platform,
    )


class _FakeIM:
    def __init__(self, *, add_ok=True):
        self.add_ok = add_ok
        self.calls: list[tuple[str, str, str]] = []

    async def add_reaction(self, context, message_id, emoji):
        self.calls.append(("add", message_id, emoji))
        return self.add_ok

    async def remove_reaction(self, context, message_id, emoji):
        self.calls.append(("remove", message_id, emoji))
        return True

    async def send_typing_indicator(self, context):
        return True

    async def clear_typing_indicator(self, context):
        return True


def _svc(im, *, reactions=True):
    svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
    svc.controller = SimpleNamespace(get_im_client_for_context=lambda ctx: im, im_client=im)
    svc.config = SimpleNamespace(ack_mode="typing")
    svc._indicators_by_turn_token = {}
    svc._admission_acks = {}
    capabilities = SimpleNamespace(
        preferred_processing_indicator="reaction",
        force_preferred_processing_indicator=True,
        supports_typing_indicator=True,
        supports_reaction_indicator=reactions,
        supports_message_indicator=True,
    )
    svc._capabilities = lambda ctx: capabilities
    svc._mode_supported = lambda caps, mode, ctx: mode == "reaction" and reactions
    return svc


class AdmissionAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_steered_input_reports_a_receipt(self):
        im = _FakeIM()
        svc = _svc(im)

        applied = await svc.ack_delivery_state(_ctx(), state="accepted", admission="steered")

        self.assertEqual(applied, STEERED_REACTION_EMOJI)
        self.assertEqual(im.calls, [("add", "m1", STEERED_REACTION_EMOJI)])

    async def test_queued_and_pending_steer_report_the_queued_receipt(self):
        for state in ("queued", "pending_steer"):
            with self.subTest(state=state):
                im = _FakeIM()
                svc = _svc(im)

                applied = await svc.ack_delivery_state(_ctx(), state=state)

                self.assertEqual(applied, QUEUED_REACTION_EMOJI)
                self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

    async def test_unconfirmed_and_undelivered_states_are_distinguishable(self):
        cases = {
            "reconciling_steer": UNCONFIRMED_REACTION_EMOJI,
            "retired": NOT_DELIVERED_REACTION_EMOJI,
        }
        for state, expected in cases.items():
            with self.subTest(state=state):
                im = _FakeIM()
                svc = _svc(im)

                self.assertEqual(await svc.ack_delivery_state(_ctx(), state=state), expected)

    async def test_started_input_is_left_to_its_own_processing_indicator(self):
        # The first message of an idle session starts a turn and already gets 👀
        # from start(); a receipt here would overwrite it on replace-semantics
        # platforms (Telegram) or stack on it (Slack).
        im = _FakeIM()
        svc = _svc(im)

        applied = await svc.ack_delivery_state(_ctx(), state="accepted", admission="started")

        self.assertIsNone(applied)
        self.assertEqual(im.calls, [])

    async def test_turn_owning_states_report_nothing(self):
        for state in ("claimed", "steering", "interrupt_waiting", "reserved", ""):
            with self.subTest(state=state):
                im = _FakeIM()
                svc = _svc(im)

                self.assertIsNone(await svc.ack_delivery_state(_ctx(), state=state))
                self.assertEqual(im.calls, [])

    async def test_platform_without_reactions_stays_silent(self):
        # WeChat-like: no reaction support, and a text bubble per queued message
        # would be worse than silence.
        im = _FakeIM()
        svc = _svc(im, reactions=False)

        self.assertIsNone(await svc.ack_delivery_state(_ctx(), state="queued"))
        self.assertEqual(im.calls, [])

    async def test_rejected_reaction_is_not_remembered(self):
        im = _FakeIM(add_ok=False)
        svc = _svc(im)
        context = _ctx()

        self.assertIsNone(await svc.ack_delivery_state(context, state="queued"))
        await svc.clear_admission_ack(context)

        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

    async def test_receipt_upgrade_replaces_the_previous_one(self):
        # A queued Delivery whose steer later settles reports the newer outcome
        # without stacking two receipts on one message.
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx()

        await svc.ack_delivery_state(context, state="queued")
        await svc.ack_delivery_state(context, state="accepted", admission="steered")

        self.assertEqual(
            im.calls,
            [
                ("add", "m1", QUEUED_REACTION_EMOJI),
                ("remove", "m1", QUEUED_REACTION_EMOJI),
                ("add", "m1", STEERED_REACTION_EMOJI),
            ],
        )

    async def test_repeated_identical_receipt_is_not_reapplied(self):
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx()

        await svc.ack_delivery_state(context, state="queued")
        await svc.ack_delivery_state(context, state="pending_steer")

        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

    async def test_start_clears_the_queued_receipt_before_the_turn_indicator(self):
        # MESSAGE-DELIVERY-302: the promoted Delivery is re-dispatched with its
        # original native message id, so the receipt and the running indicator
        # target the same message and must not coexist.
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx()
        await svc.ack_delivery_state(context, state="queued")

        handle = await svc.start(context, "claude")

        self.assertEqual(
            im.calls,
            [
                ("add", "m1", QUEUED_REACTION_EMOJI),
                ("remove", "m1", QUEUED_REACTION_EMOJI),
            ],
        )
        self.assertTrue(handle.reaction_indicator_selected)
        self.assertIsNone(handle.ack_reaction_emoji)

        # The turn's own indicator owns the message from here.
        await svc.promote_reaction_to_running(handle)
        self.assertEqual(im.calls[-1], ("add", "m1", ACK_REACTION_EMOJI))

    async def test_receipt_is_scoped_to_one_message(self):
        im = _FakeIM()
        svc = _svc(im)
        await svc.ack_delivery_state(_ctx(message_id="m1"), state="queued")

        await svc.clear_admission_ack(_ctx(message_id="m2"))

        self.assertEqual(im.calls, [("add", "m1", QUEUED_REACTION_EMOJI)])

    async def test_missing_message_id_reports_nothing(self):
        im = _FakeIM()
        svc = _svc(im)

        self.assertIsNone(await svc.ack_delivery_state(_ctx(message_id=None), state="queued"))


class ReceiptEmojiMappingTests(unittest.TestCase):
    """Every receipt emoji must resolve on the adapters that will send it."""

    receipts = (
        STEERED_REACTION_EMOJI,
        QUEUED_REACTION_EMOJI,
        UNCONFIRMED_REACTION_EMOJI,
        NOT_DELIVERED_REACTION_EMOJI,
    )

    def test_slack_short_names_are_ascii(self):
        from modules.im.slack import SlackBot

        for emoji in self.receipts:
            with self.subTest(emoji=emoji):
                name = SlackBot._slack_reaction_name(emoji)
                # A non-ASCII value here is an unmapped codepoint, which
                # reactions.add rejects with ``invalid_name``.
                self.assertTrue(name.isascii(), name)

    def test_telegram_uses_allowed_reaction_variants(self):
        from modules.im.telegram import TelegramBot

        # Telegram accepts a fixed reaction set; the writing hand is listed
        # without the U+FE0F presentation selector.
        allowed = {"✍", "👌", "🤔", "🤷"}
        for emoji in self.receipts:
            with self.subTest(emoji=emoji):
                normalized = TelegramBot._normalize_reaction_emoji(None, emoji)
                self.assertIn(normalized, allowed)


class DeliveryAdmissionContractTests(unittest.TestCase):
    def test_delivery_result_defaults_to_no_admission_claim(self):
        result = DeliveryResult("d1", None, "queued")
        self.assertEqual(result.admission, "")

    def test_started_and_steered_are_distinguishable_at_the_same_state(self):
        started = DeliveryResult("d1", "m1", "accepted", "t1", admission="started")
        steered = DeliveryResult("d2", "m2", "accepted", "t1", admission="steered")
        self.assertEqual(started.state, steered.state)
        self.assertNotEqual(started.admission, steered.admission)


if __name__ == "__main__":
    unittest.main()
