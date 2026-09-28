"""The typing keepalive ends on its own when its turn does (#2164).

"<bot> is typing…" claims a turn is working on this conversation now. Every exit
path should finish the indicator, but the loop must not depend on that: each
tick re-checks that its turn is still current, a hard cap backs that up, and a
target that keeps rejecting sends ends the loop. The interval is patched to zero
so a tick is one event-loop turn instead of five seconds.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.processing_indicator as indicator_module
from core.processing_indicator import ProcessingIndicatorHandle, ProcessingIndicatorService
from modules.im import MessageContext


class _TypingIM:
    def __init__(self, results=None):
        # One entry per send after the first; True/False or an exception.
        self.results = list(results or [])
        self.typing_calls = 0
        self.clear_calls = 0

    async def send_typing_indicator(self, _context):
        self.typing_calls += 1
        if self.typing_calls == 1 or not self.results:
            return True
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def clear_typing_indicator(self, _context):
        self.clear_calls += 1
        return True


def _service(im):
    controller = SimpleNamespace(
        config=SimpleNamespace(ack_mode="typing", language="en"),
        get_im_client_for_context=lambda _ctx: im,
        im_client=im,
    )
    return ProcessingIndicatorService(controller)


def _handle(platform="slack"):
    context = MessageContext(user_id="U1", channel_id="C1", thread_id="T1", message_id="m1", platform=platform)
    return ProcessingIndicatorHandle(context=context)


class TypingKeepaliveBoundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(indicator_module, "TYPING_KEEPALIVE_INTERVAL_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _typing_after(self, svc, handle, owner_body):
        """Start typing inside an owner task (as the turn does) and return both."""

        started = asyncio.Event()

        async def _owner():
            await svc._start_typing_indicator(handle)
            started.set()
            await owner_body()

        owner = asyncio.create_task(_owner())
        await started.wait()
        return owner, handle.typing_indicator_task

    async def test_loop_ends_within_one_tick_once_its_unhanded_owner_is_gone(self):
        # Pre-tracking window: no runtime gate token exists yet, so the task that
        # started the indicator is the turn. It exits without finishing it.
        for platform, clears in (("slack", 0), ("wechat", 1)):
            with self.subTest(platform=platform):
                im = _TypingIM()
                svc = _service(im)
                handle = _handle(platform)
                release = asyncio.Event()
                owner, typing_task = await self._typing_after(svc, handle, release.wait)
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertFalse(typing_task.done())
                self.assertGreater(im.typing_calls, 1)

                release.set()
                await owner
                sends_at_turn_end = im.typing_calls
                await asyncio.wait_for(typing_task, timeout=1)

                self.assertEqual(im.typing_calls, sends_at_turn_end)
                # A platform whose typing persists until cancelled is cleared.
                self.assertEqual(im.clear_calls, clears)

    async def test_hard_cap_ends_a_loop_whose_turn_is_still_current(self):
        im = _TypingIM()
        svc = _service(im)
        handle = _handle()
        forever = asyncio.Event()
        with patch.object(indicator_module, "TYPING_KEEPALIVE_MAX_SECONDS", 0):
            with self.assertLogs("core.processing_indicator", level="WARNING") as logs:
                owner, typing_task = await self._typing_after(svc, handle, forever.wait)
                await asyncio.wait_for(typing_task, timeout=1)
        try:
            self.assertFalse(owner.done())
            self.assertEqual(im.typing_calls, 1)
            self.assertTrue(any("cap" in line for line in logs.output), logs.output)
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_consecutive_failed_sends_end_the_loop_with_one_info_log(self):
        cases = {
            # A success in between resets the count: calls are the start send,
            # then F, F, T, F, F, F.
            "rejected": ([False, False, True, False, False, False], 7),
            "raising": ([RuntimeError("404 Unknown Channel")] * 3, 4),
        }
        for name, (results, expected_calls) in cases.items():
            with self.subTest(name):
                im = _TypingIM(results)
                svc = _service(im)
                handle = _handle()
                forever = asyncio.Event()
                with self.assertLogs("core.processing_indicator", level="INFO") as logs:
                    owner, typing_task = await self._typing_after(svc, handle, forever.wait)
                    await asyncio.wait_for(typing_task, timeout=1)
                try:
                    self.assertFalse(owner.done())
                    self.assertEqual(im.typing_calls, expected_calls)
                    stops = [record for record in logs.records if "failed sends" in record.getMessage()]
                    self.assertEqual(len(stops), 1, logs.output)
                    self.assertEqual(stops[0].levelname, "INFO")
                    self.assertIn("channel=C1 thread=T1", stops[0].getMessage())
                finally:
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
