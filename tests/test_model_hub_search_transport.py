"""Search final admission owns a connected transport before it can refuse.

Fake-session custody tests cannot detect an orphaned socket. This local-only
real-aiohttp probe verifies EOF on refused/cancelled admission, with no request
bytes sent and no external host or credential store contacted.
"""

import asyncio

import aiohttp
import pytest

from vibe.model_hub_runtime import adapter as runtime_adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["refused", "cancelled", "stopped"])
async def test_search_rejected_final_admission_closes_the_real_connected_socket(monkeypatch, admission):
    accepted, closed, checking = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received = []
    peers, handlers = [], []

    async def peer(reader, writer):
        handlers.append(asyncio.current_task())
        peers.append(writer)
        accepted.set()
        try:
            received.append(await reader.read(1))
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_session = aiohttp.ClientSession

    class LocalSession:
        def __init__(self, **kwargs):
            self.session = real_session(**kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.session.close()

        def request(self, method, url, **kwargs):
            # The production endpoint has already passed catalog HTTPS/host
            # validation. Replace egress only, retaining the real connector and
            # ClientRequest lifecycle without needing a local test CA.
            assert url == "https://api.tavily.com/search"
            return self.session.request(method, f"http://127.0.0.1:{port}/search", **kwargs)

    class Store:
        def read_api_key(self, _ref):
            return "test-only-search-key"

        def credential_metadata(self, _ref):
            return {"vendor": "tavily"}

    async def on_sent():
        await accepted.wait()
        checking.set()
        if admission == "refused":
            raise ValueError("final admission refused")
        if admission == "cancelled":
            raise asyncio.CancelledError
        await asyncio.Future()

    monkeypatch.setattr(runtime_adapter.aiohttp, "ClientSession", LocalSession)
    adapter = runtime_adapter.CLIProxyEngineAdapter(supervisor=object(), state_store=Store())
    task = asyncio.create_task(adapter.search("src_test", "tavily", "tavily/search", "cred_test", {"query": "test"}, on_sent=on_sent))
    try:
        await asyncio.wait_for(checking.wait(), timeout=2)
        if admission == "stopped":
            task.cancel()
        response = await asyncio.wait_for(task, timeout=2)
        assert response.http_status is None and response.on_sent_fired is False
        assert response.error == ("connection" if admission == "refused" else "cancelled")
        await asyncio.wait_for(closed.wait(), timeout=1)
        assert received == [b""]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        for writer in peers:
            writer.transport.abort()
        for handler in handlers:
            handler.cancel()
        await asyncio.wait_for(asyncio.gather(*handlers, return_exceptions=True), timeout=1)
        await asyncio.wait_for(server.wait_closed(), timeout=1)
