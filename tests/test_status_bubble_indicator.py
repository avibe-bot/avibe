"""Step E: in concise mode the processing indicator keeps ONLY typing.

The concise status bubble is the visual indicator, so an ack message / reaction
would be a duplicate signal (and a Slack ack message can't be deleted). B2.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.controller import Controller
from core.processing_indicator import ProcessingIndicatorService
from config.v2_config import V2Config
from modules.im import MessageContext
from tests.test_api_save_config_merge import _full_config_payload


def _cfg(style):
    payload = _full_config_payload()
    payload["agent_progress_style"] = style
    return V2Config.from_payload(payload)


def _ctx(platform="slack"):
    return MessageContext(user_id="U1", channel_id="C1", platform=platform)


class UsesConciseStatusBubbleTests(unittest.TestCase):
    def _fake(self, platform, style):
        cfg = _cfg(style)
        fake = types.SimpleNamespace(primary_platform=platform, config=cfg)
        fake.get_progress_style_for_context = lambda ctx: Controller.get_progress_style_for_context(fake, ctx)
        return fake

    def test_slack_concise_true(self):
        fake = self._fake("slack", "concise")
        self.assertTrue(Controller.uses_concise_status_bubble(fake, _ctx("slack")))

    def test_discord_concise_true(self):
        fake = self._fake("discord", "concise")
        self.assertTrue(Controller.uses_concise_status_bubble(fake, _ctx("discord")))

    def test_slack_off_false(self):
        fake = self._fake("slack", "off")
        self.assertFalse(Controller.uses_concise_status_bubble(fake, _ctx("slack")))

    def test_non_editing_platform_false(self):
        fake = self._fake("lark", "concise")
        self.assertFalse(Controller.uses_concise_status_bubble(fake, _ctx("lark")))


class ProcessingModeSuppressionTests(unittest.TestCase):
    def test_non_concise_keeps_all_modes(self):
        svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
        svc._capabilities = lambda ctx: object()
        svc._candidate_modes = lambda caps: ["message", "typing", "reaction"]
        svc._mode_supported = lambda caps, mode, ctx: True
        self.assertEqual(svc._processing_modes(_ctx()), ["message", "typing", "reaction"])

    def test_active_check_defensive_when_controller_lacks_method(self):
        svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
        svc.controller = types.SimpleNamespace()  # no uses_concise_status_bubble
        self.assertFalse(svc._concise_status_bubble_active(_ctx()))


class StartConciseTests(unittest.IsolatedAsyncioTestCase):
    """In concise mode start() SELECTS the reaction (added later at the runtime
    gate as queued 👌 → running 👀) and keeps typing keepalive eager, but never
    the ack message. The reaction is no longer added eagerly here."""

    def _svc(self, *, concise, reaction_supported=True, typing_supported=True):
        svc = ProcessingIndicatorService.__new__(ProcessingIndicatorService)
        svc.controller = types.SimpleNamespace(uses_concise_status_bubble=lambda ctx: concise)
        svc._capabilities = lambda ctx: object()

        def mode_supported(caps, mode, ctx):
            return {"reaction": reaction_supported, "typing": typing_supported}.get(mode, True)

        svc._mode_supported = mode_supported
        svc._processing_modes = lambda ctx: ["message", "typing", "reaction"]
        svc.calls = []

        async def _reaction(handle):
            svc.calls.append("reaction")
            return True

        async def _typing(handle):
            svc.calls.append("typing")
            return True

        async def _message(handle, agent_name):
            svc.calls.append("message")
            return True

        svc._start_reaction_indicator = _reaction
        svc._start_typing_indicator = _typing
        svc._start_message_indicator = _message
        return svc

    async def test_concise_selects_reaction_and_keeps_typing_no_message(self):
        svc = self._svc(concise=True)
        handle = await svc.start(_ctx(), "claude")
        # Reaction is deferred to the gate (not added here); typing stays eager.
        self.assertEqual(svc.calls, ["typing"])
        self.assertTrue(handle.reaction_indicator_selected)

    async def test_concise_skips_reaction_when_unsupported(self):
        svc = self._svc(concise=True, reaction_supported=False)
        handle = await svc.start(_ctx(), "claude")
        self.assertEqual(svc.calls, ["typing"])
        self.assertFalse(handle.reaction_indicator_selected)

    async def test_non_concise_uses_first_wins(self):
        svc = self._svc(concise=False)
        handle = await svc.start(_ctx(), "claude")
        self.assertEqual(svc.calls, ["message"])  # first candidate wins, unchanged
        self.assertFalse(handle.reaction_indicator_selected)

    async def test_non_concise_reaction_mode_is_selected_but_deferred(self):
        svc = self._svc(concise=False)
        svc._processing_modes = lambda ctx: ["reaction"]
        handle = await svc.start(_ctx(), "claude")
        # Reaction selected but NOT added eagerly (added at the gate).
        self.assertEqual(svc.calls, [])
        self.assertTrue(handle.reaction_indicator_selected)


if __name__ == "__main__":
    unittest.main()
