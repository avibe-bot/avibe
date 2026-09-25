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
    async def _persisted_types(self, platform, text, **kwargs):
        controller = _StubController(platform)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="u", channel_id="c", platform=platform)
        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            await dispatcher.emit_agent_message(context, "assistant", text, **kwargs)
        return [call.args[1] for call in persist.call_args_list]

    async def test_promotion_rule(self):
        cases = [
            ("avibe", THREE_LINES, {}, ["assistant", "interim"]),
            ("avibe", LONG_LINE, {}, ["assistant", "interim"]),
            ("avibe", TWO_LINES, {}, ["assistant"]),
            ("avibe", "a\n\n\n\nb", {}, ["assistant"]),
            ("avibe", THREE_LINES, {"level": "process"}, ["assistant"]),
            ("slack", THREE_LINES, {}, ["assistant"]),
        ]
        for platform, text, kwargs, expected in cases:
            with self.subTest(platform=platform, text=text[:20], kwargs=kwargs):
                self.assertEqual(await self._persisted_types(platform, text, **kwargs), expected)


if __name__ == "__main__":
    unittest.main()
