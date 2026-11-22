"""Native UI listener contract; no Avibe process, LAN bind, or external connection."""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace

from vibe.ui_network_access import active_listeners, loopback_target


def test_native_loopback_listener_lifecycle():
    async def exercise():
        loop = asyncio.get_running_loop()
        accepted = loop.create_future()

        def connected(reader, writer):
            accepted.set_result((reader, writer))

        server = await asyncio.start_server(connected, '127.0.0.1', 0, start_serving=False)
        ui = SimpleNamespace(started=True, servers=[server])
        try:
            assert not active_listeners(ui)
            await server.start_serving()
            port = server.sockets[0].getsockname()[1]
            assert port > 0
            listeners = active_listeners(ui)
            assert [(str(item.address), item.port) for item in listeners] == [('127.0.0.1', port)]
            assert loopback_target(listeners, port) == '127.0.0.1'
            with ExitStack() as clients:
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                clients.callback(writer.close)
                _, peer = await accepted
                clients.callback(peer.close)
                server.close()
                assert not active_listeners(ui)
                # Stop accepting without losing a reply on an accepted connection.
                peer.write(b'done')
                await peer.drain()
                assert await reader.readexactly(4) == b'done'
            await asyncio.gather(writer.wait_closed(), peer.wait_closed())
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
