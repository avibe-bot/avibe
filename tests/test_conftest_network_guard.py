"""No test reaches past loopback, whichever client it uses and whether or not it swallows the refusal.

Two tests downloaded from GitHub and models.dev through product code nothing
had stubbed (#2531), and an SSL read that hung killed a CI shard. The guard in
``tests/network_guard.py`` refuses such a request, names the host, and fails
the test that made it even when the product catches the refusal.

The refused requests here use reserved names (``.invalid``) and documentation
addresses (203.0.113.0/24, 2001:db8::/32), and the in-process checks use UDP
``connect``, which sends nothing: a regressed guard still reaches no real host.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from tests import network_guard

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Each request from a handler that swallows whatever it raises, as product code
# with an offline fallback does; one from a daemon thread; one made while the
# module is collected, outside any test; and an opt-out with no reason.
_REFUSED_REQUESTS = """
import asyncio
import socket
import threading
import urllib.request

import aiohttp
import httpx
import pytest
import requests


def _swallow(call):
    try:
        call()
    except BaseException:
        pass


_swallow(lambda: socket.getaddrinfo("refused-at-collection.invalid", 443))


def _connect(family, kind, address, use_connect_ex=False):
    sock = socket.socket(family, kind)
    try:
        (sock.connect_ex if use_connect_ex else sock.connect)(address)
    finally:
        sock.close()


def test_connect():
    _swallow(lambda: _connect(socket.AF_INET, socket.SOCK_STREAM, ("203.0.113.7", 443)))


def test_connect_ex():
    _swallow(lambda: _connect(socket.AF_INET, socket.SOCK_STREAM, ("203.0.113.8", 443), use_connect_ex=True))


def test_ipv6_connect():
    _swallow(lambda: _connect(socket.AF_INET6, socket.SOCK_STREAM, ("2001:db8::7", 443)))


def test_udp_sendto():
    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("203.0.113.9", 53))

    _swallow(send)


def test_reverse_lookup():
    _swallow(lambda: socket.gethostbyaddr("203.0.113.10"))


def test_create_connection():
    _swallow(lambda: socket.create_connection(("refused-create-connection.invalid", 443), timeout=5))


def test_urllib():
    _swallow(lambda: urllib.request.urlopen("https://refused-urllib.invalid/", timeout=5))


def test_requests():
    _swallow(lambda: requests.get("https://refused-requests.invalid/", timeout=5))


def test_httpx():
    _swallow(lambda: httpx.get("https://refused-httpx.invalid/", timeout=5))


def test_httpx_async():
    async def fetch():
        async with httpx.AsyncClient(timeout=5) as client:
            await client.get("https://refused-httpx-async.invalid/")

    _swallow(lambda: asyncio.run(fetch()))


def test_aiohttp():
    async def fetch():
        async with aiohttp.ClientSession() as session:
            await session.get("https://refused-aiohttp.invalid/")

    _swallow(lambda: asyncio.run(fetch()))


def test_asyncio_ip_literal():
    _swallow(lambda: asyncio.run(asyncio.open_connection("203.0.113.11", 443)))


def test_daemon_thread():
    thread = threading.Thread(
        target=_swallow,
        args=(lambda: urllib.request.urlopen("https://refused-thread.invalid/", timeout=5),),
        daemon=True,
    )
    thread.start()
    thread.join(30)


@pytest.mark.allow_network
def test_opt_out_without_a_reason():
    pass
"""

_REFUSED_TARGETS = {
    "test_connect": "socket.connect to 203.0.113.7:443",
    "test_connect_ex": "socket.connect to 203.0.113.8:443",
    "test_ipv6_connect": "socket.connect to [2001:db8::7]:443",
    "test_udp_sendto": "socket.sendto to 203.0.113.9:53",
    "test_reverse_lookup": "socket.gethostbyaddr to 203.0.113.10",
    "test_create_connection": "socket.getaddrinfo to refused-create-connection.invalid:443",
    "test_urllib": "socket.getaddrinfo to refused-urllib.invalid:443",
    "test_requests": "socket.getaddrinfo to refused-requests.invalid:443",
    "test_httpx": "socket.getaddrinfo to refused-httpx.invalid:443",
    "test_httpx_async": "socket.getaddrinfo to refused-httpx-async.invalid:443",
    "test_aiohttp": "socket.getaddrinfo to refused-aiohttp.invalid:443",
    "test_asyncio_ip_literal": "socket.connect to 203.0.113.11:443",
    "test_daemon_thread": "socket.getaddrinfo to refused-thread.invalid:443",
}


def test_a_swallowed_request_past_loopback_fails_its_test_naming_the_host(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(test_inner=_REFUSED_REQUESTS)
    result = pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider", timeout=120)

    output = result.stdout.str()
    # Each test body passes; the refusal it swallowed fails it at teardown.
    result.assert_outcomes(passed=len(_REFUSED_TARGETS), errors=len(_REFUSED_TARGETS) + 1)
    for test, target in _REFUSED_TARGETS.items():
        section = output.partition(f" ERROR at teardown of {test} ")[2].partition(" ERROR at ")[0]
        assert f"refused network access past loopback:\n{target} (thread " in section, (test, output)
    section = output.partition(" ERROR at setup of test_opt_out_without_a_reason ")[2]
    assert "Failed: @pytest.mark.allow_network needs a reason" in section, output
    # Collection is outside any test: the run fails on its own account.
    assert "network access past loopback was refused outside any test" in output
    assert "socket.getaddrinfo to refused-at-collection.invalid:443" in output
    assert result.ret == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize(
    ("address", "refused"),
    [
        (("127.0.0.1", 9), False),
        (("127.8.9.10", 9), False),
        (("0.0.0.0", 9), False),
        (("localhost", 9), False),
        (("LOCALHOST", 9), False),
        (("::1", 9), False),
        (("::ffff:127.0.0.1", 9), False),
        (("::", 9), False),
        (("192.168.1.1", 9), True),
        (("10.0.0.1", 9), True),
        (("::ffff:203.0.113.12", 9), True),
        (("2001:db8::12", 9), True),
    ],
)
def test_only_loopback_and_unspecified_addresses_are_reachable(address: tuple[str, int], refused: bool) -> None:
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        if refused:
            with pytest.raises(network_guard.NetworkAccessRefused, match="tests may not reach past loopback"):
                sock.connect(address)
            assert len(network_guard.take_refusals()) == 1
        else:
            sock.connect(address)
    finally:
        sock.close()


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "203.0.113.16", "refused-reverse.invalid"])
def test_a_reverse_lookup_past_loopback_is_refused(address: str) -> None:
    with pytest.raises(network_guard.NetworkAccessRefused, match="socket.gethostbyaddr"):
        socket.gethostbyaddr(address)
    assert len(network_guard.take_refusals()) == 1


def test_a_forward_lookup_of_an_address_sends_no_query_and_passes() -> None:
    assert socket.getaddrinfo("203.0.113.13", 443, type=socket.SOCK_STREAM)[0][4][:2] == ("203.0.113.13", 443)


def test_loopback_and_unix_servers_are_reachable() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        for host in ("127.0.0.1", "localhost"):
            socket.create_connection((host, port), timeout=5).close()
    if not hasattr(socket, "AF_UNIX"):
        return
    # AF_UNIX paths are limited to about 100 bytes, so not under tmp_path.
    directory = tempfile.mkdtemp(prefix="ng-")
    try:
        path = f"{directory}/s"
        with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
            server.bind(path)
            server.listen()
            client.connect(path)
    finally:
        shutil.rmtree(directory)


# The opt-out, honoured only on an integration test; and a refusal whose report is
# still being rendered when its test's window closes, which must not be lost.
_WINDOW_EDGES = """
import socket
import threading

import pytest

from tests import network_guard


def _udp_connect(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 9))


@pytest.mark.integration
@pytest.mark.allow_network(reason="proves the opt-out; a UDP connect sends nothing")
def test_an_opted_out_integration_test():
    try:
        _udp_connect("203.0.113.14")
    except network_guard.NetworkAccessRefused:
        raise
    except OSError:
        # The guard let it through; the host has no route there, as in the
        # loopback-only namespace the Linux CI shards run in.
        pass


@pytest.mark.allow_network(reason="not an integration test")
def test_an_opt_out_outside_integration():
    pass


_render = network_guard._origin
_rendering = threading.Event()
_finish_rendering = threading.Event()
_threads = []


def _slow_render():
    _rendering.set()
    _finish_rendering.wait(30)
    return _render()


def test_a_refusal_still_rendering_at_teardown():
    network_guard._origin = _slow_render

    def refuse():
        try:
            _udp_connect("203.0.113.15")
        except OSError:
            pass

    _threads.append(threading.Thread(target=refuse, daemon=True))
    _threads[0].start()
    assert _rendering.wait(30)


def test_the_next_test():
    network_guard._origin = _render
    _finish_rendering.set()
    _threads[0].join(30)
"""


def test_the_opt_out_and_a_closing_window_lose_no_refusal(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makepyfile(test_inner=_WINDOW_EDGES)
    result = pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider", timeout=120)

    output = result.stdout.str()
    result.assert_outcomes(passed=3, errors=2)
    assert "203.0.113.14" not in output
    section = output.partition(" ERROR at setup of test_an_opt_out_outside_integration ")[2]
    assert "Failed: @pytest.mark.allow_network is honoured only on an integration test" in section, output
    # Recorded once rendered, against the window open then, rather than into the closed one.
    section = output.partition(" ERROR at teardown of test_the_next_test ")[2]
    assert "socket.connect to 203.0.113.15:9 (thread " in section, output
