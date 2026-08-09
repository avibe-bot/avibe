"""Admission receipts for IM input that does not start its own turn.

MESSAGE-DELIVERY-301 / 302 / 304 / 305.

A message sent while a turn is already running is handed to its durable
Delivery owner and the message handler returns before any processing indicator
exists, so the sender used to see nothing at all. ``ack_delivery_state`` reports
that admission outcome as a reaction on the sender's own message, and the
receipt is cleared once that input's own turn starts.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import (
    _ADMISSION_ACK_REGISTRY_LIMIT,
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


class _GatedIM(_FakeIM):
    """Reaction calls that only complete when the test lets them."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def add_reaction(self, context, message_id, emoji):
        self.entered.set()
        await self.release.wait()
        return await super().add_reaction(context, message_id, emoji)


def _svc(im, *, reactions=True):
    svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
    svc.controller = SimpleNamespace(get_im_client_for_context=lambda ctx: im, im_client=im)
    svc.config = SimpleNamespace(ack_mode="typing")
    svc._indicators_by_turn_token = {}
    svc._admission_acks = {}
    svc._admission_ack_locks = {}
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

    async def test_start_clears_every_receipt_merged_into_its_turn(self):
        # MESSAGE-DELIVERY-309: Deliveries without a native message id (quick
        # replies) merge into one Turn, but only the first hydrates the dispatch
        # context. The rest are accepted as part of that Turn and never
        # dispatched, so nothing else would ever take their receipt down.
        im = _FakeIM()
        svc = _svc(im)
        first = _ctx(message_id=None)
        first.platform_specific = {"processing_indicator_message_id": "echo-1"}
        second = _ctx(message_id=None)
        second.platform_specific = {"processing_indicator_message_id": "echo-2"}
        await svc.ack_delivery_state(first, state="queued")
        await svc.ack_delivery_state(second, state="queued")

        dispatch = _ctx(message_id=None)
        dispatch.platform_specific = {
            "processing_indicator_message_id": "echo-1",
            "delivery_ack_targets": ["echo-1", "echo-2"],
        }
        await svc.start(dispatch, "claude")

        self.assertEqual(
            im.calls,
            [
                ("add", "echo-1", QUEUED_REACTION_EMOJI),
                ("add", "echo-2", QUEUED_REACTION_EMOJI),
                ("remove", "echo-1", QUEUED_REACTION_EMOJI),
                ("remove", "echo-2", QUEUED_REACTION_EMOJI),
            ],
        )

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

    async def test_accepted_without_steer_provenance_reports_nothing(self):
        # An idempotent re-entry observes an already accepted Delivery and
        # returns no admission; that observation cannot tell whether the input
        # joined a running turn or started its own, and guessing ✍️ would stack
        # on (or replace) a live processing indicator.
        im = _FakeIM()
        svc = _svc(im)

        self.assertIsNone(await svc.ack_delivery_state(_ctx(), state="accepted"))
        self.assertEqual(im.calls, [])

    async def test_receipt_in_flight_when_the_turn_starts_is_still_cleared(self):
        # Both halves await a platform call. Without serialization the add
        # records its emoji after start() has looked for one, stranding 👌 next to
        # the running indicator.
        im = _GatedIM()
        svc = _svc(im)
        context = _ctx()

        ack = asyncio.ensure_future(svc.ack_delivery_state(context, state="queued"))
        await im.entered.wait()
        clear = asyncio.ensure_future(svc.start(context, "claude"))
        await asyncio.sleep(0)
        im.release.set()
        await ack
        await clear

        self.assertEqual(
            im.calls,
            [
                ("add", "m1", QUEUED_REACTION_EMOJI),
                ("remove", "m1", QUEUED_REACTION_EMOJI),
            ],
        )

    async def test_receipt_arriving_after_the_turn_started_is_suppressed(self):
        # The other ordering of the same race: the promoted turn already owns
        # the message, so a late queued receipt must not decorate it.
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx()

        await svc.start(context, "claude")
        applied = await svc.ack_delivery_state(context, state="queued")

        self.assertIsNone(applied)
        self.assertEqual(im.calls, [])

    async def test_terminal_receipt_is_replaced_rather_than_stacked(self):
        # Two Deliveries can share one reaction target: a quick-reply callback
        # reacts on its bot echo, so a second click settles on the message a
        # first one already decorated. A platform shows one reaction per
        # (message, emoji, bot), so the receipt describes the message — the
        # terminal ✍️ is removed rather than left next to the new 👌.
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx(message_id="echo-7")

        await svc.ack_delivery_state(context, state="accepted", admission="steered")
        await svc.ack_delivery_state(context, state="queued")

        self.assertEqual(
            im.calls,
            [
                ("add", "echo-7", STEERED_REACTION_EMOJI),
                ("remove", "echo-7", STEERED_REACTION_EMOJI),
                ("add", "echo-7", QUEUED_REACTION_EMOJI),
            ],
        )

    async def test_terminal_receipt_is_cleared_when_a_turn_takes_the_message(self):
        # Same shared-target case, other ordering: the second Delivery is the one
        # that dispatches, so its turn's own indicator replaces the stale ✍️.
        im = _FakeIM()
        svc = _svc(im)
        context = _ctx(message_id="echo-7")

        await svc.ack_delivery_state(context, state="retired")
        await svc.clear_admission_ack(context)

        self.assertEqual(
            im.calls,
            [
                ("add", "echo-7", NOT_DELIVERED_REACTION_EMOJI),
                ("remove", "echo-7", NOT_DELIVERED_REACTION_EMOJI),
            ],
        )

    async def test_receipts_are_bounded(self):
        # Terminal receipts are remembered too, so the FIFO cap is the only thing
        # keeping one entry per mid-turn message from living for the life of the
        # process. Evicting the oldest only forfeits a later replace or clear.
        im = _FakeIM()
        svc = _svc(im)

        for index in range(_ADMISSION_ACK_REGISTRY_LIMIT + 10):
            await svc.ack_delivery_state(_ctx(message_id=f"q-{index}"), state="queued")
        for index in range(_ADMISSION_ACK_REGISTRY_LIMIT + 10):
            await svc.ack_delivery_state(
                _ctx(message_id=f"s-{index}"),
                state="accepted",
                admission="steered",
            )

        self.assertLessEqual(len(svc._admission_acks), _ADMISSION_ACK_REGISTRY_LIMIT)

    async def test_quick_reply_echo_target_keeps_one_receipt_key(self):
        # A quick-reply callback is dispatched with message_id=None and reacts on
        # its bot echo. Durable hydration replaces the message id with the
        # synthetic delivery id, so the receipt is only clearable when the echo
        # target survives admission.
        im = _FakeIM()
        svc = _svc(im)
        before = _ctx(message_id=None)
        before.platform_specific = {"processing_indicator_message_id": "echo-7"}
        hydrated = _ctx(message_id="delivery-abc")
        hydrated.platform_specific = {"processing_indicator_message_id": "echo-7"}
        stripped = _ctx(message_id="delivery-abc")

        await svc.ack_delivery_state(before, state="queued")
        await svc.clear_admission_ack(hydrated)

        self.assertEqual(
            im.calls,
            [
                ("add", "echo-7", QUEUED_REACTION_EMOJI),
                ("remove", "echo-7", QUEUED_REACTION_EMOJI),
            ],
        )
        self.assertNotEqual(
            svc._admission_ack_key(before),
            svc._admission_ack_key(stripped),
        )


class ReactionTargetSurvivalTests(unittest.TestCase):
    """The reaction target must survive admission and durable hydration."""

    def test_quick_reply_echo_is_captured_from_the_ingress_context(self):
        from core.handlers.message_handler import MessageHandler

        context = _ctx(message_id=None)
        context.platform_specific = {"processing_indicator_message_id": "echo-7"}

        self.assertEqual(MessageHandler._reaction_target(context), "echo-7")

    def test_ordinary_message_carries_no_separate_target(self):
        from core.handlers.message_handler import MessageHandler

        self.assertIsNone(MessageHandler._reaction_target(_ctx()))

    def test_hydrated_context_gets_its_reaction_target_back(self):
        from core.handlers.message_handler import MessageHandler

        hydrated = _ctx(message_id="delivery-abc")
        hydrated.platform_specific = {"delivery_id": "delivery-abc"}

        MessageHandler._restore_reaction_target(
            hydrated,
            {"processing_indicator_message_id": "echo-7"},
        )

        self.assertEqual(
            (hydrated.platform_specific or {}).get("processing_indicator_message_id"),
            "echo-7",
        )

    def test_restore_is_a_no_op_without_a_captured_target(self):
        from core.handlers.message_handler import MessageHandler

        hydrated = _ctx(message_id="delivery-abc")

        MessageHandler._restore_reaction_target(hydrated, {"message_handler_route": {}})
        MessageHandler._restore_reaction_target(hydrated, None)

        self.assertIsNone(
            (hydrated.platform_specific or {}).get("processing_indicator_message_id")
        )


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
