"""Connected inference is governed by completion and cancellation, not age."""

from __future__ import annotations

import asyncio

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.classification import classify_outcome
from vibe.model_hub_runtime import client as client_module
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import SourceRecord


WIRE = {
    "openai_responses": (
        b'event: response.created\ndata: {"type":"response.created"}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
        b'{"output":[{"content":[{"type":"output_text","text":"ok"}]}]}',
    ),
    "openai_chat": (
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        b"data: [DONE]\n\n",
        b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
    ),
    "anthropic": (
        b'event: message_start\ndata: {"type":"message_start","message":{"role":"assistant"}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        b'{"type":"message","content":[{"type":"text","text":"ok"}]}',
    ),
}


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("phase", ["headers", "first_byte", "prelude", "stream_body", "buffered_body"])
@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
def test_connected_inference_waits_for_its_owner(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    phase: str,
    cancel: bool,
) -> None:
    """MH-RUNTIME-009: connected inference ends by completion or owner cancellation."""

    async def run() -> None:
        blocked = asyncio.Event()
        release = asyncio.Event()
        request_finished = asyncio.Event()
        streaming = phase != "buffered_body"
        metadata, output, terminal, buffered = WIRE[protocol]
        sessions = []
        real_session = client_module.aiohttp.ClientSession

        def track_session(**kwargs):
            timeout = kwargs["timeout"]
            assert timeout.total is None and timeout.sock_read is None
            assert timeout.connect == timeout.sock_connect == 0.02
            session = real_session(**kwargs)
            sessions.append(session)
            return session

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", track_session)

        async def pause(stage: str) -> None:
            if phase == stage:
                blocked.set()
                await release.wait()

        async def respond(request: web.Request) -> web.StreamResponse:
            try:
                await request.json()
                await pause("headers")
                response = web.StreamResponse(
                    headers={"Content-Type": "text/event-stream" if streaming else "application/json"},
                )
                await response.prepare(request)
                await pause("first_byte")
                if streaming:
                    await response.write(metadata)
                    await pause("prelude")
                    await response.write(output)
                    await pause("stream_body")
                    await response.write(terminal)
                else:
                    await response.write(buffered[:1])
                    await pause("buffered_body")
                    await response.write(buffered[1:])
                await response.write_eof()
                return response
            finally:
                request_finished.set()

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        source = SourceRecord(
            source_id="src_patient1",
            vendor="custom",
            protocol=protocol,
            base_url="https://provider.example.test",
            credential_ref="cred_patient1",
            allowed_origins=(),
            model_ids=("patient-model",),
            prefix="patient",
        )
        client = EngineClient(
            EngineConnection(f"http://127.0.0.1:{port}", "fixture-management", "fixture-gateway"),
            timeout=0.02,
        )

        async def consume() -> bytes:
            handle = await client.invoke(source, "patient-model", {}, stream=streaming)
            try:
                assert handle.stream is not None
                body = b"".join([chunk async for chunk in handle.stream])
                outcome = await handle.outcome()
                assert outcome.kind is RawOutcomeKind.SUCCESS
                decision = classify_outcome(outcome)
                assert decision.action == "return"
                assert decision.reason is None and decision.cooldown_seconds == 0
                return body
            finally:
                await handle.close_stream()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(blocked.wait(), timeout=2)
            # Spend several connection budgets while the server owns the wait.
            done, _pending = await asyncio.wait({task}, timeout=0.1)
            assert not done, "inference was terminated solely because model output was delayed"
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                await asyncio.wait_for(request_finished.wait(), timeout=2)
            else:
                release.set()
                assert await asyncio.wait_for(task, timeout=2) == (
                    metadata + output + terminal if streaming else buffered
                )
            assert sessions and all(session.closed for session in sessions)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()

    asyncio.run(run())
