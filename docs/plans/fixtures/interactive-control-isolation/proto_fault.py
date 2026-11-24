"""Sibling-failure/slow relay probe: spawn two synthetic Session processes. No storage, hence no crash recovery proof."""

import asyncio, multiprocessing as mp, socket, time, json, statistics, sys, os
from proto import history, request, receiver


def child(sock, idx):
    rows = history(idx)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    sock.sendall(b"r")
    receiver(sock, 1)
    if idx == 0:
        request(rows)
        sock.sendall(b"f")
        sock.sendall(b"x" * (4 * 1024 * 1024))
        return
    for _ in range(80):
        request(rows)
        sock.sendall(b"d")
        receiver(sock, 1)
    sock.close()


async def main():
    l = asyncio.get_running_loop()
    ctx = mp.get_context("spawn")
    ps = []
    s = []
    for i in range(2):
        a, b = socket.socketpair()
        p = ctx.Process(target=child, args=(b, i))
        p.start()
        b.close()
        a.setblocking(False)
        ps.append(p)
        s.append(a)
    assert await asyncio.gather(*(l.sock_recv(a, 1) for a in s)) == [b"r", b"r"]
    await asyncio.gather(*(l.sock_sendall(a, b"g") for a in s))
    assert await l.sock_recv(s[0], 1) == b"f"
    done = 0
    gaps = []
    last = time.perf_counter()
    while done < 80:
        assert await l.sock_recv(s[1], 1) == b"d"
        await l.sock_sendall(s[1], b"a")
        done += 1
        if done == 10:
            assert ps[0].is_alive()
            ps[0].terminate()
        now = time.perf_counter()
        gaps.append((now - last) * 1000)
        last = now
    for p in ps:
        p.join(2)
    for a in s:
        a.close()
    print(
        json.dumps(
            dict(
                sibling_attempts=done,
                completed_after_kill=done - 10,
                worker_exit=[p.exitcode for p in ps],
                max_sibling_gap_ms=round(max(gaps), 1),
            )
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
