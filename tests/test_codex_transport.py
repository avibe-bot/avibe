import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.codex.transport import CodexTransport, STREAM_BUFFER_LIMIT


class CodexTransportHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_task_failure_marks_transport_not_alive(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport._process = SimpleNamespace(returncode=None)

        async def done():
            return None

        task = asyncio.create_task(done())
        await task
        transport._reader_task = task

        self.assertFalse(transport.is_alive)
        self.assertFalse(transport.is_initialized)

    async def test_send_request_fails_fast_when_reader_task_is_done(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport._process = SimpleNamespace(returncode=None)

        async def done():
            return None

        task = asyncio.create_task(done())
        await task
        transport._reader_task = task

        with self.assertRaises(ConnectionError):
            await transport.send_request("thread/start", {})

        self.assertEqual(transport._pending, {})

    async def test_send_notification_fails_fast_when_reader_task_is_done(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport._process = SimpleNamespace(returncode=None)

        async def done():
            return None

        task = asyncio.create_task(done())
        await task
        transport._reader_task = task

        with self.assertRaises(ConnectionError):
            await transport.send_notification("initialized")

    async def test_pending_notification_keeps_terminal_pipeline_alive(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle(_method, _params):
            started.set()
            await release.wait()

        transport._notification_cb = handle
        transport._notify_task = asyncio.create_task(transport._notify_worker())
        transport._notify_queue.put_nowait(("turn/completed", {}))
        await started.wait()
        try:
            self.assertTrue(transport.has_pending_notifications)
            self.assertTrue(transport._notify_queue.empty())
        finally:
            release.set()
            await asyncio.sleep(0)
            transport._notify_task.cancel()
            await transport._notify_task

        self.assertFalse(transport.has_pending_notifications)

    def test_stream_buffer_limit_allows_large_codex_thread_responses(self):
        self.assertGreaterEqual(STREAM_BUFFER_LIMIT, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
