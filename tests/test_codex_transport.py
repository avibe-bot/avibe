import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.codex.transport import (
    AVIBE_APP_SERVER_CONFIG_OVERRIDES,
    CodexTransport,
    STREAM_BUFFER_LIMIT,
)


def _forced_config_args() -> tuple[str, ...]:
    return tuple(
        arg
        for override in AVIBE_APP_SERVER_CONFIG_OVERRIDES
        for arg in ("-c", override)
    )


class CodexTransportHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_initialize_stops_unpublished_transport(self):
        initialize_started = asyncio.Event()

        class _Stream:
            async def readline(self):
                await asyncio.Event().wait()

        process = SimpleNamespace(
            pid=123,
            returncode=None,
            stdin=None,
            stdout=_Stream(),
            stderr=_Stream(),
        )
        transport = CodexTransport(
            binary="codex",
            cwd="/tmp",
            runtime_args=["-c", 'model_provider="avibe"'],
            extra_args=[
                "-c",
                "features.memories=true",
                "-c",
                "features.plugins=true",
                "-c",
                "features.multi_agent=true",
            ],
        )

        async def wait_for_initialize(_method, _params):
            initialize_started.set()
            await asyncio.Event().wait()

        async def stop_transport():
            transport._cleanup_tasks()

        transport.send_request = AsyncMock(side_effect=wait_for_initialize)
        transport.stop = AsyncMock(side_effect=stop_transport)
        spawn = AsyncMock(return_value=process)

        with (
            patch(
                "modules.agents.codex.transport.asyncio.create_subprocess_exec",
                new=spawn,
            ),
            patch("modules.agents.codex.transport.process_identity", return_value={}),
            patch("modules.agents.codex.transport.log_process_snapshot"),
        ):
            task = asyncio.create_task(transport.start())
            await initialize_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        transport.stop.assert_awaited_once_with()
        self.assertEqual(
            spawn.await_args.args,
            (
                "codex",
                "--dangerously-bypass-approvals-and-sandbox",
                "app-server",
                "-c",
                'model_provider="avibe"',
                "-c",
                "features.memories=true",
                "-c",
                "features.plugins=true",
                "-c",
                "features.multi_agent=true",
                *_forced_config_args(),
            ),
        )

        initialize_params = transport.send_request.await_args.args[1]
        self.assertEqual(
            initialize_params,
            {
                "clientInfo": {
                    "name": "avibe",
                    "title": "Avibe",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )

    def test_app_server_policy_disables_competing_host_surfaces(self):
        disabled = set(AVIBE_APP_SERVER_CONFIG_OVERRIDES)

        self.assertTrue(
            {
                "features.apps=false",
                "features.goals=false",
                "features.hooks=false",
                "features.memories=false",
                "features.multi_agent=false",
                "features.plugins=false",
                "features.terminal_visualization_instructions=false",
            }.issubset(disabled)
        )
        # ``agents`` is a role-definition table in older supported Codex
        # releases; native delegation is disabled through the feature gate.
        self.assertNotIn("agents.enabled=false", disabled)
        self.assertNotIn("features.fast_mode=false", disabled)
        self.assertNotIn("features.image_generation=false", disabled)
        self.assertNotIn("features.shell_tool=false", disabled)
        self.assertNotIn("web_search=disabled", disabled)

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

    async def test_cancelled_request_does_not_leave_pending_rpc(self):
        request_written = asyncio.Event()

        class _Stdin:
            def is_closing(self):
                return False

            def write(self, _payload):
                return None

            async def drain(self):
                request_written.set()

        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport._process = SimpleNamespace(returncode=None, stdin=_Stdin())

        task = asyncio.create_task(transport.send_request("turn/interrupt", {}))
        await request_written.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(transport._pending, {})

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

    async def test_wait_closed_waits_for_already_read_notifications(self):
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
        transport._closed_event.set()
        waiter = asyncio.create_task(transport.wait_closed())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        release.set()
        await waiter
        await asyncio.sleep(0)
        transport._notify_task.cancel()
        await transport._notify_task

    def test_stream_buffer_limit_allows_large_codex_thread_responses(self):
        self.assertGreaterEqual(STREAM_BUFFER_LIMIT, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
