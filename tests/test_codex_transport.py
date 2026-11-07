import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.codex.transport import (
    AVIBE_APP_SERVER_CONFIG_OVERRIDES,
    CodexRPCError,
    CodexResponseTooLargeError,
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
    async def test_oversized_stdout_fails_all_pending_requests_with_typed_cause(self):
        """MESSAGE-DELIVERY-032: preserve framing failure instead of retryable EOF."""
        transport = CodexTransport(binary="codex", cwd="/tmp")
        stdout = asyncio.StreamReader(limit=64)
        stdout.feed_data(b'{"result":"' + b"x" * 100 + b'"}\n')
        transport._process = SimpleNamespace(stdout=stdout, returncode=None)
        pending = [asyncio.get_running_loop().create_future() for _ in range(2)]
        transport._pending = dict(enumerate(pending))
        transport._reader_task = asyncio.create_task(transport._reader_loop())
        await transport._reader_task
        for future in pending:
            with self.assertRaises(CodexResponseTooLargeError) as caught:
                await future
            self.assertNotIsInstance(caught.exception, ConnectionError)
        self.assertFalse(transport.is_alive)
        self.assertEqual(transport._pending, {})

    async def test_normal_stdout_eof_remains_a_connection_error(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        transport._process = SimpleNamespace(stdout=stdout, returncode=None)
        pending = asyncio.get_running_loop().create_future()
        transport._pending[1] = pending
        await transport._reader_loop()
        with self.assertRaisesRegex(ConnectionError, "stdout closed"):
            await pending

    async def test_rpc_errors_preserve_protocol_rejection_identity(self):
        transport = CodexTransport(binary="codex", cwd="/tmp")
        for code in (-32600, -32601, -32602, -32603, -32000):
            with self.subTest(code=code):
                future = asyncio.get_running_loop().create_future()
                transport._pending[1] = future
                await transport._dispatch({"id": 1, "error": {"code": code, "message": "failure"}})
                with self.assertRaises(CodexRPCError) as caught:
                    await future
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.request_rejected, code in {-32600, -32601, -32602})
                self.assertNotIn(1, transport._pending)

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
                "-c",
                "tools.experimental_request_user_input.enabled=true",
            ],
        )

        async def wait_for_initialize(method, _params):
            if method == "initialize":
                initialize_started.set()
                await asyncio.Event().wait()
            return {}

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
                "-c",
                "tools.experimental_request_user_input.enabled=true",
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

    async def test_start_probes_turn_collaboration_mode_support(self):
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
        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport.send_request = AsyncMock(
            side_effect=[{}, {"data": [{"mode": "default"}]}]
        )
        transport.send_notification = AsyncMock()
        transport.stop = AsyncMock(side_effect=transport._cleanup_tasks)

        with (
            patch(
                "modules.agents.codex.transport.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("modules.agents.codex.transport.process_identity", return_value={}),
            patch("modules.agents.codex.transport.log_process_snapshot"),
        ):
            await transport.start()

        self.assertTrue(transport.supports_turn_collaboration_mode)
        self.assertEqual(
            [call.args[0] for call in transport.send_request.await_args_list],
            ["initialize", "collaborationMode/list"],
        )
        transport.stop.assert_not_awaited()
        transport._cleanup_tasks()

    async def test_start_falls_back_when_turn_collaboration_probe_is_unsupported(self):
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
        transport = CodexTransport(binary="codex", cwd="/tmp")
        transport.send_request = AsyncMock(
            side_effect=[{}, RuntimeError("method not found")]
        )
        transport.send_notification = AsyncMock()
        transport.stop = AsyncMock(side_effect=transport._cleanup_tasks)

        with (
            patch(
                "modules.agents.codex.transport.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("modules.agents.codex.transport.process_identity", return_value={}),
            patch("modules.agents.codex.transport.log_process_snapshot"),
        ):
            await transport.start()

        self.assertFalse(transport.supports_turn_collaboration_mode)
        self.assertTrue(transport.is_initialized)
        transport.stop.assert_not_awaited()
        transport._cleanup_tasks()

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
                "skills.include_instructions=false",
                "tools.experimental_request_user_input.enabled=false",
            }.issubset(disabled)
        )
        # ``agents`` is a role-definition table in older supported Codex
        # releases; native delegation is disabled through the feature gate.
        self.assertNotIn("agents.enabled=false", disabled)
        self.assertNotIn("features.fast_mode=false", disabled)
        self.assertNotIn("features.image_generation=false", disabled)
        self.assertNotIn("features.shell_tool=false", disabled)
        self.assertNotIn("web_search=disabled", disabled)

    def test_app_server_policy_preserves_client_developer_messages(self):
        self.assertIn(
            "features.retain_client_developer_messages=true",
            AVIBE_APP_SERVER_CONFIG_OVERRIDES,
        )

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
        # Cancelled while its write is still draining, or while awaiting the answer.
        for phase in ("writing", "awaiting_response"):
            with self.subTest(phase=phase):
                request_written = asyncio.Event()

                class _Stdin:
                    def is_closing(self):
                        return False

                    def write(self, _payload):
                        return None

                    async def drain(self):
                        request_written.set()
                        if phase == "writing":
                            await asyncio.Event().wait()

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


class NativeMcpSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_requests_suppress_effective_native_servers(self):
        # The transport owns suppression, including callers that bypass CodexAgent.
        for method in ("thread/start", "thread/resume", "thread/fork"):
            for cwd in (None, "/project"):
                for config in ({}, {"mcp_servers": {"project-only": {}, "foo.bar": {}, 'a"b': {}}},
                               {"mcp_servers": None}, {"mcp_servers": []}, "rpc-error"):
                    with self.subTest(method=method, cwd=cwd, config=config):
                        transport = CodexTransport(binary="codex", cwd="/server")
                        transport._process = SimpleNamespace(returncode=None)
                        writes = []

                        async def write(message):
                            writes.append(message)
                            if message["method"] == "config/read":
                                response = ({"error": {"code": -32603, "message": "read failed"}}
                                            if config == "rpc-error" else {"result": {"config": config}})
                            else:
                                response = {"result": {"thread": {"id": "thread"}}}
                            await transport._dispatch({"id": message["id"], **response})

                        transport._write_message = write
                        params = {"config": {"keep": True, "mcp_servers": {"project-only": {"command": "keep"}}}}
                        if cwd:
                            params["cwd"] = cwd
                        invalid = config == "rpc-error" or (
                            "mcp_servers" in config and not isinstance(config["mcp_servers"], dict))
                        if invalid:
                            with self.assertRaises((RuntimeError, CodexRPCError)):
                                await transport.send_request(method, params)
                            self.assertEqual(len(writes), 1)
                        else:
                            await transport.send_request(method, params)
                            expected = {"keep": True, "mcp_servers": {"project-only": {"command": "keep"}}}
                            if config:
                                expected["mcp_servers"] = {
                                    "project-only": {"command": "keep", "enabled": False},
                                    "foo.bar": {"enabled": False},
                                    'a"b': {"enabled": False},
                                }
                            self.assertEqual(writes[1]["params"]["config"], expected)
                            self.assertEqual(writes[1]["method"], method)
                        self.assertEqual(writes[0]["params"], {
                            "includeLayers": True, **({"cwd": cwd} if cwd else {})})
                        self.assertEqual(params["config"], {"keep": True, "mcp_servers": {"project-only": {"command": "keep"}}})

            for effective, layers, expected_names in (
                ({}, [{"config": {"mcp_servers": {"disabled-only": {}}}}], {"disabled-only"}),
                (
                    {"mcp_servers": {"effective-only": {
                        "command": "run",
                        "env": {"TOKEN": "secret"},
                        "headers": {"Authorization": "Bearer secret"},
                    }}},
                    [],
                    {"effective-only"},
                ),
                ({"mcp_servers": {"both": {}}}, [{"config": {"mcp_servers": {"both": {}, "layer-only": {}}}}], {"both", "layer-only"}),
            ):
                with self.subTest(effective=effective, layers=layers):
                    transport = CodexTransport(binary="codex", cwd="/server")
                    transport._process = SimpleNamespace(returncode=None)
                    writes = []

                    async def write(message):
                        writes.append(message)
                        result = {"config": effective, "layers": layers} if message["method"] == "config/read" else {"thread": {"id": "thread"}}
                        await transport._dispatch({"id": message["id"], "result": result})

                    transport._write_message = write
                    await transport.send_request("thread/start", {"cwd": "/project", "config": {}})
                    actual = writes[1]["params"]["config"]["mcp_servers"]
                    self.assertEqual(set(actual), expected_names)
                    if "effective-only" in expected_names:
                        self.assertEqual(actual["effective-only"], {"command": "run", "enabled": False})
                    self.assertNotIn("env", str(actual))
                    self.assertNotIn("headers", str(actual))

    async def test_real_cli_does_not_start_project_mcp_before_or_after_trust(self):
        import json
        import os
        import shutil
        import tempfile

        from tests.conftest import REAL_USER_HOME, _AMBIENT_AVIBE_HOME

        config_path = Path(_AMBIENT_AVIBE_HOME or REAL_USER_HOME / ".avibe") / "config/config.json"
        configured = json.loads(config_path.read_text()) if config_path.exists() else {}
        binary = shutil.which(configured.get("agents", {}).get("codex", {}).get("cli_path", "codex"))
        if not binary:
            self.skipTest("Avibe-configured Codex executable unavailable")

        for trusted in (False, True):
            with self.subTest(trusted=trusted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                home = root / "home"
                codex_home = home / ".codex"
                project = root / "project"
                codex_home.mkdir(parents=True)
                (project / ".codex").mkdir(parents=True)
                marker = root / "started"
                if trusted:
                    codex_home.joinpath("config.toml").write_text(
                        f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n'
                    )
                else:
                    codex_home.joinpath("config.toml").write_text("")
                project.joinpath(".codex/config.toml").write_text(
                    "[mcp_servers.acc]\ncommand = \"/usr/bin/touch\"\n"
                    f"args = [{json.dumps(str(marker))}]\n"
                )
                env = {
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(home / ".local/share"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                    "TMPDIR": str(root),
                }
                transport = CodexTransport(binary=binary, cwd=str(project), runtime_env=env)
                try:
                    await transport.start()
                    await transport.send_request("thread/start", {
                        "cwd": str(project),
                        "model": "gpt-5",
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "ephemeral": True,
                    })
                    await asyncio.sleep(0.5)
                    self.assertFalse(marker.exists())
                finally:
                    await transport.stop()
