"""An intermediate assistant message also becomes a Web ``interim`` bubble."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext
from tests.test_message_dispatcher_platform_limits import _StubController

LONG_LINE = "根因已经定位：" + "并发下重入" * 40
THREE_LINES = "Found it.\nThe cache is shared.\nFixing the lock next."
TWO_LINES = "Reading a.py\nthen b.py"


class InterimBubbleTests(unittest.IsolatedAsyncioTestCase):
    async def _persisted(
        self, platform, text, *, reply_enhancements=False, interim_stored=True, **kwargs
    ):
        controller = _StubController(platform)
        controller.config.reply_enhancements = reply_enhancements
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="u", channel_id="c", platform=platform)

        def _persist(_context, message_type, *_args, **_kwargs):
            if message_type == "interim" and not interim_stored:
                return None
            return {"id": f"row-{message_type}"}

        with mock.patch(
            "core.message_dispatcher.persist_agent_message", side_effect=_persist
        ) as persist:
            await dispatcher.emit_agent_message(context, "assistant", text, **kwargs)
        return persist.call_args_list

    async def _persisted_types(self, platform, text, **kwargs):
        return [call.args[1] for call in await self._persisted(platform, text, **kwargs)]

    async def test_promotion_rule(self):
        cases = [
            ("avibe", THREE_LINES, {}, ["interim", "assistant"]),
            ("avibe", LONG_LINE, {}, ["interim", "assistant"]),
            ("avibe", TWO_LINES, {}, ["assistant"]),
            ("avibe", "a\n\n\n\nb", {}, ["assistant"]),
            ("avibe", THREE_LINES, {"level": "process"}, ["assistant"]),
            ("slack", THREE_LINES, {}, ["assistant"]),
        ]
        for platform, text, kwargs, expected in cases:
            with self.subTest(platform=platform, text=text[:20], kwargs=kwargs):
                self.assertEqual(await self._persisted_types(platform, text, **kwargs), expected)

    async def test_bubble_owns_the_narration_and_its_quick_replies(self):
        text = f"{THREE_LINES}\n\n---\n[Check backup] | [Skip]"
        interim, assistant = await self._persisted("avibe", text, reply_enhancements=True)

        # The process-log row keeps the raw narration and tells Activity that the
        # transcript draws it.
        self.assertEqual(assistant.args[1:3], ("assistant", text))
        self.assertEqual(assistant.kwargs["metadata"], {"transcript_copy": "interim"})
        # The bubble parses buttons exactly like a reply.
        self.assertEqual(interim.args[1:3], ("interim", THREE_LINES))
        self.assertEqual(interim.kwargs["quick_replies"], ["Check backup", "Skip"])

        # Activity keeps the narration whenever no bubble holds it: below the
        # threshold, a body that is only buttons, or a bubble that failed to store.
        for text, kwargs in (
            (TWO_LINES, {}),
            ("\n\n---\n[A]\n[B]\n[C]", {"reply_enhancements": True}),
            (THREE_LINES, {"interim_stored": False}),
        ):
            with self.subTest(text=text[:12], kwargs=kwargs):
                calls = await self._persisted("avibe", text, **kwargs)
                [log_only] = [call for call in calls if call.args[1] == "assistant"]
                self.assertIsNone(log_only.kwargs["metadata"])
                self.assertNotIn(
                    "---", "".join(call.args[2] for call in calls if call.args[1] == "interim")
                )


if __name__ == "__main__":
    unittest.main()
