"""Local synthetic process-boundary experiment; stdlib only. No product state, model or network.
Run separately: python3 proto.py shared 32 80; python3 proto.py isolated 32 80.
External thread in an independent process measures real socketpair control RTT even during a Python GC.
"""

import asyncio, copy, gc, hashlib, json, multiprocessing as mp, os, resource, socket, statistics, struct, sys, time


def history(s):
    rows = []
    for i in range(600):
        rows.extend(
            [
                {"role": "user", "content": [{"text": f"{s}/{i}: " + "some query " * 14}]},
                {
                    "role": "assistant",
                    "calls": [
                        {
                            "name": "read",
                            "files": [{"path": f"p/{i}/{k}", "lines": [k, k + 1, k + 2]} for k in range(8)],
                        }
                    ],
                },
                {"role": "tool", "content": [{"text": f"{s}/{i}: " + "long value for the output. " * 135}]},
            ]
        )
    return rows


def request(rows):
    graph = copy.deepcopy(rows)
    wire = json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode()
    return graph, hashlib.sha256(wire).digest()


def receiver(sock, n):
    data = b""
    while len(data) < n:
        b = sock.recv(n - len(data))
        if not b:
            raise EOFError
        data += b
    return data


def client(sock, stats):
    times = []
    sock.settimeout(90)
    try:
        while True:
            t = time.perf_counter()
            sock.sendall(b"p")
            r = receiver(sock, 1)
            if r == b"q":
                break
            times.append((time.perf_counter() - t) * 1000)
            time.sleep(0.01)
    except (EOFError, BrokenPipeError):
        pass
    stats.send(times)
    stats.close()
    sock.close()


def worker(sock, s, n):
    rows = history(s)
    digest = hashlib.sha256()
    sock.sendall(b"r")
    receiver(sock, 1)
    for i in range(n):
        graph, h = request(rows)
        digest.update(h)
        sock.sendall(b"d")
        receiver(sock, 1)
        del graph
    sock.sendall(b"f" + digest.digest())
    sock.close()


async def main(mode, count, attempts):
    ctx = mp.get_context("spawn")
    loop = asyncio.get_running_loop()
    reader, writer = ctx.Pipe(duplex=False)
    a, b = socket.socketpair()
    p = ctx.Process(target=client, args=(b, writer))
    p.start()
    b.close()
    writer.close()
    a.setblocking(False)
    end = asyncio.Event()
    stop = asyncio.Event()
    gaps = []
    procs = []
    ready = 0
    start = asyncio.Event()
    order = []
    sizes = []

    async def probes():
        while await loop.sock_recv(a, 1):
            await loop.sock_sendall(a, b"q" if stop.is_set() else b"p")
            if stop.is_set():
                return

    async def beat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append((now - last) * 1000)
            last = now

    async def barrier():
        nonlocal ready
        ready += 1
        if ready == count:
            start.set()
        await start.wait()

    async def shared(s):
        rows = history(s)
        digest = hashlib.sha256()
        await barrier()
        for _ in range(attempts):
            if mode == "threaded":
                graph, h = await asyncio.to_thread(request, rows)
            else:
                graph, h = request(rows)
            digest.update(h)
            await asyncio.sleep(0.003)
            del graph
        return digest.digest()

    launch_slots = asyncio.Semaphore(1)

    async def isolated(s):
        paced = mode == "paced"
        if paced:
            await launch_slots.acquire()
        parent, child = socket.socketpair()
        proc = ctx.Process(target=worker, args=(child, s, attempts))
        if paced:
            await asyncio.to_thread(proc.start)
        else:
            proc.start()
        child.close()
        procs.append(proc)
        parent.setblocking(False)

        async def read_n(n):
            b = b""
            while len(b) < n:
                x = await loop.sock_recv(parent, n - len(b))
                if not x:
                    raise EOFError
                b += x
            return b

        try:
            assert await read_n(1) == b"r"
            if paced:
                launch_slots.release()
            await barrier()
            await loop.sock_sendall(parent, b"g")
            for _ in range(attempts):
                assert await read_n(1) == b"d"
                await asyncio.sleep(0.003)
                await loop.sock_sendall(parent, b"a")
            assert await read_n(1) == b"f"
            return await read_n(32)
        finally:
            parent.close()

    ping = asyncio.create_task(probes())
    heart = asyncio.create_task(beat())
    await asyncio.sleep(0.12)
    t = time.perf_counter()
    results = await asyncio.gather(*[(isolated if mode in ("isolated", "paced") else shared)(s) for s in range(count)])
    elapsed = time.perf_counter() - t
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.gather(ping, heart)
    a.close()
    samples = reader.recv()
    reader.close()
    p.join(2)
    for proc in procs:
        proc.join(2)
        assert proc.exitcode == 0

    def tail(a):
        return {
            k: round(sorted(a)[min(len(a) - 1, int(len(a) * q))], 1)
            for k, q in [("p50", 0.5), ("p99", 0.99), ("max", 1)]
        }

    print(
        json.dumps(
            dict(
                mode=mode,
                sessions=count,
                attempts=attempts,
                python=sys.version.split()[0],
                platform=sys.platform,
                body_bytes=len(json.dumps(history(0), separators=(",", ":")).encode()),
                elapsed_s=round(elapsed, 2),
                requests=count * attempts,
                checksums=hashlib.sha256(b"".join(results)).hexdigest(),
                heartbeat_gap_ms=tail(gaps),
                control_rtt_ms=tail(samples),
                control_samples=len(samples),
                controller_peak_rss_native_units=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            ),
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
