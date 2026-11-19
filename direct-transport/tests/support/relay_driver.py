"""Run only in the CI script's offline Linux network namespace."""

import asyncio
import json
import secrets
import ssl
import sys
from contextlib import AsyncExitStack

from vibe.direct_ingress import AsyncioByteStream, DIRECT_INGRESS_SCOPE_KEY, DirectIngress

HOST = "instance-app.example.test"
URL = "https://127.0.0.1:33443/"


async def main(binary, relay):
    token = secrets.token_hex(32)
    body = secrets.token_hex(16).encode()
    async with AsyncExitStack() as cleanup:

        async def spawn(*argv):
            p = await asyncio.create_subprocess_exec(*argv, stdin=-1, stdout=-1)

            async def finish():
                if p.returncode is None:
                    p.kill()
                await p.wait()

            cleanup.push_async_callback(finish)
            return p

        async def line(p, prefix):
            while data := await p.stdout.readline():
                if data.startswith(prefix):
                    return data[len(prefix) :].strip()
            raise RuntimeError(f"child exited before {prefix!r}")

        async def event(p, kind):
            while True:
                value = json.loads(b"{" + await line(p, b"{"))
                if value["type"] == kind:
                    return value
                assert value["type"] != "fatal", value

        def send(p, **value):
            p.stdin.write(json.dumps(value).encode() + b"\n")

        server = await spawn(relay, "--config-path", "relay.toml")
        # Do not race the sidecar's initial network probe against relay startup.
        while True:
            try:
                _, w = await asyncio.open_connection("127.0.0.1", 33443)
            except OSError:
                assert server.returncode is None
                await asyncio.sleep(0.1)
            else:
                w.close()
                await w.wait_closed()
                break

        async def child(role):
            return await spawn(
                binary, "--exact", "relay_interop::" + role, "--ignored", "--nocapture", "--format", "terse"
            )

        client = await child("client")
        peer = (await line(client, b"CLIENT ")).decode()
        assert len(peer) == 64

        async def app(scope, receive, respond):
            assert scope["path"] == "/health" and scope["method"] == "GET"
            assert scope[DIRECT_INGRESS_SCOPE_KEY] == {"peer": peer}
            await respond(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(len(body)).encode())],
                }
            )
            await respond({"type": "http.response.body", "body": body})

        ingress = DirectIngress(app, hostname=HOST, certfile="inner.pem", keyfile="inner.key")
        await ingress.start()
        cleanup.push_async_callback(ingress.stop)

        async def accept(reader, writer):
            try:
                assert await reader.readline() == f"AVDT1 {token} {peer}\n".encode()
                await ingress.handle_stream(AsyncioByteStream(reader, writer), peer=peer)
            finally:
                writer.close()

        async with await asyncio.start_server(accept, "127.0.0.1", 0) as listener:
            sidecar = await child("sidecar")
            send(
                sidecar,
                type="start",
                protocol=1,
                alpn="avibe-direct/1",
                relay={"policy": "public-trial-v1"},
                token=token,
                max_connections=4,
                max_streams_per_connection=4,
                stream_addr=f"127.0.0.1:{listener.sockets[0].getsockname()[1]}",
            )
            ready = await event(sidecar, "ready")
            assert (await event(sidecar, "relay_status"))["url"] == URL
            assert ready["path_reports_v1"] is True
            send(sidecar, type="observe_paths_v1")
            send(sidecar, type="admission", endpoint_id=peer, ttl_seconds=60)
            client.stdin.write(ready["endpoint_id"].encode() + b"\n")
            reader, writer = await asyncio.open_connection("127.0.0.1", int(await line(client, b"BRIDGE ")))
            writer.write(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode())
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")
            await writer.start_tls(ssl.create_default_context(cafile="ca.pem"), server_hostname=HOST)
            writer.write(f"GET /health HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")
            assert await reader.readexactly(len(body)) == body
            writer.close()
            assert await line(client, b"PATH ") == b"local-relay"
            assert await client.wait() == 0
            first = await event(sidecar, "path_report")
            assert first["phase"] == "open"
            report = first
            while report["phase"] != "close":
                report = await event(sidecar, "path_report")
                assert report["ordinal"] == first["ordinal"]
            assert report["selected"] == "relay" and report["families"][0]["rx_bytes"] > 0
            send(sidecar, type="stop")
            assert await sidecar.wait() == 0
            print("Verified relayed CONNECT, hostname-verified inner TLS, and /health response.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(*sys.argv[1:]), 90))
