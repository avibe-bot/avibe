"""The instance's own count of a remote session's requests by arrival path."""

from __future__ import annotations

import pytest

from tests.test_direct_rendezvous import (  # the route fixtures of the rendezvous tests
    ORIGIN,
    PATH,
    REMOTE_PEER,
    _body,
    _post,
    _signed_in,
    instance,  # noqa: F401  (a fixture)
)
from vibe import direct_path_use, ui_server
from vibe.direct_path_use import IngressCounts
from vibe.ui_server import app


class _Clock:
    def __init__(self) -> None:
        self.now = 600.0

    def __call__(self) -> float:
        return self.now


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "vibe.direct_path_use"]


def test_requests_are_counted_per_session_and_minute_and_logged_once_the_minute_is_over(caplog):
    clock = _Clock()
    counts = IngressCounts(clock)
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    owner, member = ("sub-owner", "browser-a"), ("sub-member", "browser-b")
    for _ in range(3):
        counts.note(owner, direct=False, owner=True)
    counts.note(owner, direct=True, owner=True)
    counts.note(member, direct=False, owner=False)
    # Nothing while the minute is still counting.
    counts.flush()
    assert _lines(caplog) == []
    # The next request, a minute later, logs the minute that is over.
    clock.now += 60
    counts.note(member, direct=True, owner=False)
    assert _lines(caplog) == [
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 other=3",
        "direct path_use ingress session=2 role=member minutes_ago=1 direct=0 other=1",
    ]
    # With no later request, a flush logs it; a session keeps its number, and
    # a minute is logged once.
    caplog.clear()
    clock.now += 60 * 4
    counts.flush()
    assert _lines(caplog) == ["direct path_use ingress session=2 role=member minutes_ago=4 direct=1 other=0"]
    caplog.clear()
    counts.flush()
    clock.now += 600
    counts.flush()
    assert _lines(caplog) == []
    # Nothing of a session's identity is in a line.
    counts.note(owner, direct=True, owner=True)
    clock.now += 60
    counts.flush()
    assert _lines(caplog) == ["direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 other=0"]
    assert not any(word in line for line in caplog.messages for word in ("sub-", "browser-"))


def test_many_sessions_in_one_minute_share_one_line_past_the_bound(caplog):
    clock = _Clock()
    counts = IngressCounts(clock)
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    bound = direct_path_use._MAX_SESSIONS_PER_MINUTE
    for index in range(bound + 3):
        counts.note(("sub", index), direct=index % 2 == 0, owner=index == 0)
    # A session already counted in the minute keeps its own line.
    counts.note(("sub", 0), direct=True, owner=True)
    clock.now += 60
    counts.flush()
    lines = _lines(caplog)
    assert len(lines) == bound + 1
    assert lines[0] == "direct path_use ingress session=0 role=several minutes_ago=1 direct=2 other=1"
    assert lines[1] == "direct path_use ingress session=1 role=owner minutes_ago=1 direct=2 other=0"
    # The map of numbers is bounded too: the oldest session is forgotten and numbered anew.
    for index in range(direct_path_use._MAX_SESSIONS + 1):
        counts.note(("other", index), direct=False, owner=False)
    assert len(counts._numbers) == direct_path_use._MAX_SESSIONS


@pytest.fixture
def counted(monkeypatch):
    clock = _Clock()
    counts = IngressCounts(clock)
    monkeypatch.setattr(direct_path_use, "counts", counts)
    return clock, counts


def test_a_signed_in_remote_request_is_counted_by_the_path_it_arrived_on(instance, counted, caplog, monkeypatch):  # noqa: F811
    config, _rendezvous = instance
    clock, counts = counted
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    owner = _signed_in(config, role="owner", subject="sub-owner")
    viewer = _signed_in(config)
    # Not through the direct ingress: the rendezvous request itself is one.
    assert _post(owner, _body()).status_code == 200
    assert _post(owner, _body()).status_code == 200
    assert _post(viewer, _body()).status_code == 200
    # Through the direct ingress.
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        assert _post(owner, _body()).status_code == 200
    # Not counted: a request with no session, and the page anyone is served.
    anonymous = app.test_client()
    refused = anonymous.post(PATH, base_url=ORIGIN, environ_base=REMOTE_PEER, json=_body())
    assert refused.status_code == 401
    assert anonymous.get("/health", base_url=ORIGIN, environ_base=REMOTE_PEER).status_code == 200
    assert _lines(caplog) == []
    clock.now += 60
    counts.flush()
    assert _lines(caplog) == [
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 other=2",
        "direct path_use ingress session=2 role=member minutes_ago=1 direct=0 other=1",
    ]


def test_an_accepted_remote_request_is_told_how_it_arrived_and_no_other_is(instance, counted, monkeypatch):  # noqa: F811
    """The page reads this on each answer: its live stream is a connection of
    its own, so only a request's own answer says how the requests arrive."""
    config, _rendezvous = instance
    owner = _signed_in(config, role="owner", subject="sub-owner")
    header = ui_server.REQUEST_VIA_HEADER

    def session(client):
        return client.get("/api/session", base_url=ORIGIN, environ_base=REMOTE_PEER)

    # A gated route and the page's own session check, by the path of each
    # request: the next one says its own, not the last one's.
    assert _post(owner, _body()).headers.get(header) == "other"
    assert session(owner).headers.get(header) == "other"
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        assert _post(owner, _body()).headers.get(header) == "direct"
        assert session(owner).headers.get(header) == "direct"
    assert _post(owner, _body()).headers.get(header) == "other"
    # Nothing is said to a request with no accepted session, whatever its
    # path: a refused one, a session check that finds none, a public page.
    anonymous = app.test_client()
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        refused = anonymous.post(PATH, base_url=ORIGIN, environ_base=REMOTE_PEER, json=_body())
        assert refused.status_code == 401 and header not in refused.headers
        assert header not in session(anonymous).headers
        assert header not in anonymous.get("/health", base_url=ORIGIN, environ_base=REMOTE_PEER).headers


def test_an_answer_a_cache_may_give_again_does_not_say_how_its_request_arrived(
    instance, counted, monkeypatch, tmp_path  # noqa: F811
):
    """Replayed after the client changed paths, or by a shared cache to another
    client, the word would be about a request the instance never saw."""
    config, _rendezvous = instance
    (tmp_path / "logo-unread.png").write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: tmp_path)
    owner = _signed_in(config, role="owner", subject="sub-owner")
    header = ui_server.REQUEST_VIA_HEADER
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        asset = owner.get("/logo-unread.png", base_url=ORIGIN, environ_base=REMOTE_PEER)
        assert asset.status_code == 200 and "max-age" in asset.headers["Cache-Control"]
        assert header not in asset.headers
        # The same session's next API answer says its own word.
        answer = owner.get("/api/session", base_url=ORIGIN, environ_base=REMOTE_PEER)
        assert answer.headers.get(header) == "direct"


@pytest.mark.parametrize(
    ("headers", "reusable"),
    [
        # What the page's API requests get: nothing a cache may give again.
        ({}, False),
        ({"Cache-Control": "no-store"}, False),
        ({"Cache-Control": "no-store, private"}, False),
        ({"Cache-Control": "no-cache", "ETag": '"a"', "Last-Modified": "Fri, 27 Nov 2026 13:20:39 GMT"}, False),
        ({"ETag": '"a"'}, False),
        # A bot's avatar, a static file, a Show runtime asset: a browser's own
        # cache, or a shared one, may answer the next request by itself.
        ({"Cache-Control": "private, max-age=31536000, immutable"}, True),
        ({"Cache-Control": "public, max-age=3600"}, True),
        ({"Cache-Control": "Public"}, True),
        ({"Cache-Control": "s-maxage=60"}, True),
        ({"Expires": "Fri, 27 Nov 2026 13:20:39 GMT"}, True),
        ({"Last-Modified": "Fri, 27 Nov 2026 13:20:39 GMT"}, True),
    ],
)
def test_which_answers_a_cache_may_give_again(headers, reusable):
    assert ui_server._answer_may_be_reused(ui_server.Response(b"", headers=headers)) is reusable


def test_a_later_request_of_the_session_logs_the_minutes_that_are_over(instance, counted, caplog):  # noqa: F811
    config, _rendezvous = instance
    clock, _counts = counted
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    client = _signed_in(config)
    assert _post(client, _body()).status_code == 200
    clock.now += 120
    assert _post(client, _body()).status_code == 200
    assert _lines(caplog) == ["direct path_use ingress session=1 role=member minutes_ago=2 direct=0 other=1"]


def test_the_pages_session_check_is_counted_though_it_passes_no_gate(instance, counted, caplog, monkeypatch):  # noqa: F811
    config, _rendezvous = instance
    clock, counts = counted
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    owner = _signed_in(config, role="owner", subject="sub-owner")

    def session(client):
        answer = client.get("/api/session", base_url=ORIGIN, environ_base=REMOTE_PEER)
        return answer.status_code, answer.get_json().get("authenticated")

    assert session(owner) == (200, True)
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, "_is_direct_ingress_request", lambda: True)
        assert session(owner) == (200, True)
    # A check that finds no session is not a session's request.
    assert session(app.test_client()) == (200, False)
    clock.now += 60
    counts.flush()
    assert _lines(caplog) == ["direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 other=1"]


def test_the_minute_is_read_under_the_lock_so_a_drained_minute_is_never_counted_again(caplog):
    counts = IngressCounts()
    caplog.set_level("INFO", logger="vibe.direct_path_use")
    now, read_unlocked = [600.0], []

    def clock() -> float:
        # Read outside the lock, a request's minute can be drained by the
        # heartbeat before the request is counted in it, and logged twice.
        if not counts._lock.locked():
            read_unlocked.append(now[0])
        return now[0]

    counts._clock = clock
    counts.note(("sub", "browser"), direct=False, owner=True)
    now[0] += 60
    counts.flush()
    counts.note(("sub", "browser"), direct=True, owner=True)
    now[0] += 60
    counts.flush()
    assert read_unlocked == []
    assert _lines(caplog) == [
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=0 other=1",
        "direct path_use ingress session=1 role=owner minutes_ago=1 direct=1 other=0",
    ]
