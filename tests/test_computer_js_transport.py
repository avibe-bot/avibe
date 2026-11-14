"""Transport/lifecycle regressions without reaching a real desktop.

A Unix listener substitutes only the Windows named-pipe adapter so the real
Node path-based channel runs on POSIX. Win32 ACL/PID/handle behavior has separate
contract and native tests; this surrogate does not prove Windows containment.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import socket
import tempfile

import pytest

from core import computer_js_kernels as kernels
from tests.test_computer_js_kernel import NODE, SESSION, _harness


pytestmark = pytest.mark.skipif(NODE is None or os.name != "posix", reason="POSIX Node test harness")


@pytest.fixture
def pipe_transport(monkeypatch):
    processes = []
    pipes = []

    class Pipe:
        def __init__(self):
            self.directory = tempfile.mkdtemp(prefix="avibe-js-pipe-", dir="/tmp")
            self.path = str(Path(self.directory) / "kernel.sock")
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(self.path)
            self.listener.listen(1)
            self.listener.setblocking(False)
            self.closed = False
            pipes.append(self)

        async def connect(self, process, *, limit):
            assert process in processes
            connection, _ = await asyncio.get_running_loop().sock_accept(self.listener)
            return await asyncio.open_connection(sock=connection, limit=limit)

        def close(self):
            self.listener.close()
            shutil.rmtree(self.directory, ignore_errors=True)
            self.closed = True

    async def create(command, **kwargs):
        assert "AVIBE_COMPUTER_KERNEL_PIPE" in kwargs["env"]
        assert "AVIBE_COMPUTER_KERNEL_FD" not in kwargs["env"]
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(kernels, "_WINDOWS", True)
    monkeypatch.setattr(kernels, "WindowsKernelPipe", Pipe)
    monkeypatch.setattr(kernels, "create_windows_process", create)
    return processes, pipes


def test_path_transport_preserves_bindings_and_discards_endpoint_environment(pipe_transport):
    """The named-path Node branch must work without POSIX inherited descriptors."""
    async def scenario():
        processes, pipes = pipe_transport
        async with _harness() as h:
            for _ in range(2):
                report = await h.js("let saved = 40; saved + 2")
                assert report["ok"] is True and report["result"] == 42, report
                assert (await h.js("saved"))["result"] == 40
                report = await h.js("typeof process.env.AVIBE_COMPUTER_KERNEL_PIPE")
                assert report["result"] == "undefined"
                assert pipes[-1].closed
                await h.reset()
        assert all(process.returncode is not None for process in processes)

    asyncio.run(scenario())


@pytest.mark.parametrize("windows_transport", [False, True])
def test_request_cancellation_resets_kernel_before_next_call(monkeypatch, pipe_transport, windows_transport):
    """A lost request cannot leave the next call stuck behind unsettled JS."""
    monkeypatch.setattr(kernels, "_WINDOWS", windows_transport)

    async def scenario():
        async with _harness() as h:
            assert (await h.js("let prior = 1"))["ok"]
            process = h.kernel_process()
            sent = asyncio.Event()
            send = h.kernels._send

            async def track(kernel, message):
                await send(kernel, message)
                if message["type"] == "eval":
                    sent.set()

            monkeypatch.setattr(h.kernels, "_send", track)
            pending = asyncio.create_task(h.kernels.evaluate(SESSION, "await new Promise(() => {})", 30000))
            await asyncio.wait_for(sent.wait(), 3)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(process.wait(), 3)
            report = await h.js("typeof prior", 500)
            assert report["kernel_fresh"] is True and report["result"] == "undefined", report

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["deadline", "turn", "request"])
def test_blocked_transport_write_cannot_escape_deadline_or_cancellation(monkeypatch, ending):
    """Timeout used to start after drain, leaving a backpressured send unbounded."""
    async def scenario():
        async with _harness(kill_grace_seconds=0.05) as h:
            assert (await h.js("1"))["ok"]
            process = h.kernel_process()
            blocked = asyncio.Event()
            send = h.kernels._send

            async def block(kernel, message):
                if message["type"] == "eval":
                    blocked.set()
                    await asyncio.Event().wait()
                await send(kernel, message)

            monkeypatch.setattr(h.kernels, "_send", block)
            pending = asyncio.create_task(h.kernels.evaluate(SESSION, "42", 50 if ending == "deadline" else 30000))
            await asyncio.wait_for(blocked.wait(), 3)
            if ending == "turn":
                h.controller.session_turns.in_flight[SESSION].task.cancel()
            elif ending == "request":
                pending.cancel()
            if ending == "request":
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                report = await asyncio.wait_for(pending, 2)
                assert report["error"]["code"] == "js_kernel_reset"
            await asyncio.wait_for(process.wait(), 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["request_during_spawn", "connect_timeout"])
def test_path_startup_failure_reaps_owned_child(monkeypatch, pipe_transport, failure):
    """Cleanup covers ownership transfer and pre-admission startup, not only ready."""
    async def scenario():
        real_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()
        processes = []
        artifact_dirs = []

        async def spawn(*args, **kwargs):
            if failure == "connect_timeout":
                args = (str(NODE), "-e", "setInterval(() => {}, 1000)")
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            artifact_dirs.append(Path(kwargs["cwd"]))
            spawned.set()
            if failure == "request_during_spawn":
                await release.wait()
            return process

        monkeypatch.setattr(kernels.asyncio, "create_subprocess_exec", spawn)
        if failure == "connect_timeout":
            monkeypatch.setattr(kernels, "SPAWN_TIMEOUT_SECONDS", 0.1)
        async with _harness() as h:
            pending = asyncio.create_task(h.kernels.evaluate(SESSION, "42", 1000))
            await asyncio.wait_for(spawned.wait(), 3)
            if failure == "request_during_spawn":
                pending.cancel()
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                report = await asyncio.wait_for(pending, 3)
                assert report["error"]["code"] == "js_kernel_unavailable", report
            await asyncio.wait_for(processes[0].wait(), 3)
            assert all(pipe.closed for pipe in pipe_transport[1])
            assert not (artifact_dirs[0] / ".active").exists()

    asyncio.run(scenario())
